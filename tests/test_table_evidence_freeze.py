"""``table_evidence`` / ``report_tables`` 记录层冻结测试（第三点细化方案 §二.1）。

覆盖：一次构建产出三件套、Decimal 精度与十进制字符串、科学计数法与负号、单位继承链、
PDF 与 Word/Excel 定位差别、来源归属校验、字段完整性。

全部用**纯内存构造** ``TableBlock`` 并 monkeypatch 取数/来源查询，不触数据库、
不改动任何生产文件。
"""
from __future__ import annotations

import json

from backend.app import report_tables, table_evidence
from backend.app.table_block import (
    TableBlock, TableCell, TableColumn, TableRow, annual_series_block, cell_from_text,
)
from backend.app.table_evidence import allowed_table_records

RECORD_FIELDS = {
    "record_id", "value", "raw", "unit", "subject_key", "fact_key", "label", "period",
    "period_type", "subject", "role", "scope", "source_ids", "locator", "page", "bbox",
    "cell_id", "origin_mode", "table_id", "table_type", "parser_version", "verification",
}


def _use_blocks(monkeypatch, blocks, *, known_sources=None):
    monkeypatch.setattr(
        report_tables, "chapter_table_blocks",
        lambda project_id, chapter_key, *, subject: list(blocks),
    )
    if known_sources is None:
        known_sources = {source for block in blocks for cell in block.cells() for source in cell.source_ids}
    monkeypatch.setattr(table_evidence, "project_source_ids", lambda project_id: set(known_sources))
    monkeypatch.setattr(table_evidence, "subject_role_labels", lambda project_id: {})


def _records(monkeypatch, blocks, *, known_sources=None, chapter_key="financial", subject="华辰公司"):
    _use_blocks(monkeypatch, blocks, known_sources=known_sources)
    return allowed_table_records("p", chapter_key, subject=subject)


def _value_cell(raw, *, unit="万元", page=9, source_ids=("s1",), origin_type="derived", locator=None):
    return cell_from_text(
        raw, unit=unit, origin_type=origin_type, source_ids=source_ids,
        locator=locator if locator is not None else {"page": page, "bbox": [1.0, 2.0, 3.0, 4.0]},
    )


def _series_block(rows, *, unit="万元", period="2025 年末", table_id="t-series"):
    return annual_series_block(
        table_id=table_id, title="三年主要财务数据", subject="华辰公司", scope="合并口径",
        unit=unit, period_labels=[period], rows=rows, source_refs=["s1"],
    )


def _one_cell_block(*, block_unit, column_unit, row_label, cell, period_label="2025 年末"):
    return TableBlock(
        table_id="t-unit", table_type="annual_series", title="单位继承", subject="华辰公司",
        scope="合并口径", currency="CNY", unit=block_unit,
        columns=(
            TableColumn(key="subject", label="科目", role="subject"),
            TableColumn(key="period_0", label=period_label, role="period_value", unit=column_unit),
        ),
        rows=(TableRow(row_key=row_label, cells=(
            TableCell(raw=row_label, status="text", origin_type="derived"),
            cell,
        )),),
    )


# 1. Decimal 精度与十进制字符串 ----------------------------------------------


def test_decimal_precision_and_decimal_string(monkeypatch):
    """大数序列化为十进制字符串：不丢尾零、不出现 float，JSON round-trip 后仍是 str。"""
    block = _series_block([
        ("资产总计", (_value_cell("96,717,233.20"),)),
        ("货币资金", (_value_cell("180,000"),)),
    ])
    records = _records(monkeypatch, [block])
    by_label = {record["label"]: record for record in records}

    assert by_label["资产总计"]["value"] == "96717233.20"  # 尾零保留
    assert by_label["货币资金"]["value"] == "180000"
    for record in records:
        assert isinstance(record["value"], str), "value 必须是字符串，不能是 float"

    # JSON round-trip：数值仍是字符串，且精度不变。
    payload = json.loads(json.dumps(records, ensure_ascii=False))
    assert isinstance(payload[0]["value"], str)
    assert {item["label"]: item["value"] for item in payload}["资产总计"] == "96717233.20"


# 2. 科学计数法与负号 --------------------------------------------------------


def test_scientific_notation_and_unicode_minus(monkeypatch):
    """``1.2E+5`` 作为一个数值解析；``−1,234.50``（Unicode 负号）符号保留。"""
    sci = _value_cell("1.2E+5")
    # normalized=None 模拟规范化失败，强制走 raw 分支的 Unicode 负号处理。
    unicode_minus = TableCell(
        raw="−1,234.50", normalized=None, status="ok", unit="万元", origin_type="derived",
        source_ids=("s1",), locator={"page": 9},
    )
    block = _series_block(
        [("预测经营现金流", (sci,)), ("净额", (unicode_minus,))], period="2025 年度",
    )
    records = _records(monkeypatch, [block])
    by_label = {record["label"]: record for record in records}

    assert by_label["预测经营现金流"]["value"] == "120000"  # 一个数值，不是 1.2 与 5
    assert by_label["净额"]["value"] == "-1234.50"
    assert by_label["净额"]["raw"] == "−1,234.50"
    assert by_label["净额"]["period_type"] == "annual"


# 3. 单位继承链 --------------------------------------------------------------


def test_unit_inherited_from_cell(monkeypatch):
    cell = TableCell(raw="6,820", normalized="6820", status="ok", unit="万元",
                     origin_type="derived", source_ids=("s1",), locator={"page": 9})
    block = _one_cell_block(block_unit="", column_unit="", row_label="货币资金", cell=cell)
    record = _records(monkeypatch, [block])[0]
    assert record["unit"] == "万元"
    assert record["verification"] == "ok"


def test_unit_inherited_from_row_subject(monkeypatch):
    cell = TableCell(raw="6,820", normalized="6820", status="ok", unit="",
                     origin_type="derived", source_ids=("s1",), locator={"page": 9})
    block = _one_cell_block(block_unit="", column_unit="", row_label="货币资金（万元）", cell=cell)
    record = _records(monkeypatch, [block])[0]
    assert record["unit"] == "万元"
    assert record["verification"] == "ok"


def test_unit_inherited_from_column(monkeypatch):
    cell = TableCell(raw="6,820", normalized="6820", status="ok", unit="",
                     origin_type="derived", source_ids=("s1",), locator={"page": 9})
    block = _one_cell_block(block_unit="", column_unit="万元", row_label="货币资金", cell=cell)
    record = _records(monkeypatch, [block])[0]
    assert record["unit"] == "万元"
    assert record["verification"] == "ok"


def test_unit_inherited_from_table(monkeypatch):
    cell = TableCell(raw="6,820", normalized="6820", status="ok", unit="",
                     origin_type="derived", source_ids=("s1",), locator={"page": 9})
    block = _one_cell_block(block_unit="万元", column_unit="", row_label="货币资金", cell=cell)
    record = _records(monkeypatch, [block])[0]
    assert record["unit"] == "万元"
    assert record["verification"] == "ok"


def test_unit_ambiguity_marks_needs_review(monkeypatch):
    """单位在所有层级都取不到 → 无法唯一确定，标 needs_review。"""
    cell = TableCell(raw="6,820", normalized="6820", status="ok", unit="",
                     origin_type="derived", source_ids=("s1",), locator={"page": 9})
    block = _one_cell_block(block_unit="", column_unit="", row_label="货币资金", cell=cell)
    record = _records(monkeypatch, [block])[0]
    assert record["unit"] == ""
    assert record["verification"] == "needs_review"


def test_conflicting_units_mark_needs_review(monkeypatch):
    """行科目「万元」与列标注「%」冲突 → 标 needs_review。"""
    cell = TableCell(raw="6,820", normalized="6820", status="ok", unit="",
                     origin_type="derived", source_ids=("s1",), locator={"page": 9})
    block = _one_cell_block(block_unit="", column_unit="%", row_label="占比（万元）", cell=cell)
    record = _records(monkeypatch, [block])[0]
    assert record["unit"] == "万元"
    assert record["verification"] == "needs_review"


# 4. 定位：PDF 与 Word/Excel ------------------------------------------------


def test_pdf_cells_require_page_and_bbox(monkeypatch):
    """PDF（uploaded_table）：缺页码或缺坐标的格不入记录，齐全的收录。"""
    ok = TableCell(raw="6,820", normalized="6820", status="ok", origin_type="uploaded_cell",
                   source_ids=("s1",), locator={"page": 5, "bbox": [1.0, 2.0, 3.0, 4.0]})
    no_page = TableCell(raw="6,821", normalized="6821", status="ok", origin_type="uploaded_cell",
                        source_ids=("s1",), locator={"bbox": [1.0, 2.0, 3.0, 4.0]})
    no_bbox = TableCell(raw="6,822", normalized="6822", status="ok", origin_type="uploaded_cell",
                        source_ids=("s1",), locator={"page": 5})
    block = annual_series_block(
        table_id="t-pdf", title="上传原表", subject="华辰公司", scope="合并口径", unit="万元",
        period_labels=["2025 年末"],
        rows=[("正常", (ok,)), ("缺页码", (no_page,)), ("缺坐标", (no_bbox,))],
        source_refs=["s1"], origin_mode="uploaded_table",
    )
    records = _records(monkeypatch, [block])
    assert [record["label"] for record in records] == ["正常"]


def test_snapshot_cells_do_not_require_pdf_page(monkeypatch):
    """Word/Excel/主体快照（profile_facts）：无页码定位也收录，用自身来源定位。"""
    cell = TableCell(raw="6,820", normalized="6820", status="ok", unit="万元",
                     origin_type="profile_fact", source_ids=("s1",), locator={})
    block = _one_cell_block(block_unit="万元", column_unit="万元", row_label="货币资金", cell=cell)
    record = _records(monkeypatch, [block])[0]
    assert record["value"] == "6820"
    assert record["locator"] == {}


# 5. 来源不属于本项目 --------------------------------------------------------


def test_sources_outside_project_are_rejected(monkeypatch):
    """来源 ID 不在本项目来源清单内 → 该格不收录。"""
    block = _series_block([("货币资金", (_value_cell("6,820", source_ids=("s-foreign",)),))])
    records = _records(monkeypatch, [block], known_sources={"s-other"})
    assert records == []


# 6. 字段完整性 --------------------------------------------------------------


def test_every_record_carries_full_contract(monkeypatch):
    block = _series_block([("资产总计", (_value_cell("1,234,567.89"),))])
    record = _records(monkeypatch, [block])[0]
    assert RECORD_FIELDS <= set(record), f"字段不齐：{sorted(RECORD_FIELDS - set(record))}"
    assert record["record_id"] == f"{record['cell_id']}@{table_evidence.PARSER_VERSION}"
    assert record["parser_version"] == table_evidence.PARSER_VERSION
    assert record["period_type"] == "annual"
    assert record["source_ids"] == ("s1",)
    assert record["locator"]["page"] == 9
    assert record["subject_key"] == record["fact_key"]


# 7. 三件套一致性 ------------------------------------------------------------


def test_build_chapter_tables_builds_once_and_shares_blocks(monkeypatch):
    """monkeypatch ``chapter_table_blocks`` 计数=1：markdown/records/snapshot 同源。"""
    calls = {"count": 0}
    blocks = [_series_block([("资产总计", (_value_cell("96,717,233.20"),))])]

    def _fake(project_id, chapter_key, *, subject):
        calls["count"] += 1
        return list(blocks)

    monkeypatch.setattr(report_tables, "chapter_table_blocks", _fake)
    monkeypatch.setattr(table_evidence, "project_source_ids", lambda project_id: {"s1"})
    monkeypatch.setattr(table_evidence, "subject_role_labels", lambda project_id: {})

    built = report_tables.build_chapter_tables("p", "financial", subject="华辰公司")

    assert calls["count"] == 1, "三件套必须来自一次取表"
    assert built.markdown.startswith(report_tables.SYSTEM_TABLE_HEADING)
    assert blocks[0].title in built.markdown
    assert [block.table_id for block in built.snapshot["blocks"]] == [block.table_id for block in built.blocks]
    assert built.snapshot["input_hash"] == report_tables.table_blocks_hash(list(built.blocks))
    assert built.records  # 记录非空
    # 记录、markdown、快照都指向同一批 blocks。
    assert {record["table_id"] for record in built.records} == {block.table_id for block in built.blocks}
    assert list(built.records) == table_evidence.records_from_blocks(blocks, project_id="p")
    assert calls["count"] == 1


def test_build_chapter_tables_skips_markdown_for_non_table_chapter(monkeypatch):
    """非表格章：正文为空串，但三件套仍来自同一次取表。"""
    calls = {"count": 0}

    def _fake(project_id, chapter_key, *, subject):
        calls["count"] += 1
        return []

    monkeypatch.setattr(report_tables, "chapter_table_blocks", _fake)
    monkeypatch.setattr(table_evidence, "project_source_ids", lambda project_id: set())
    built = report_tables.build_chapter_tables("p", "governance", subject="华辰公司")
    assert calls["count"] == 1
    assert built.markdown == ""
    assert built.records == ()
