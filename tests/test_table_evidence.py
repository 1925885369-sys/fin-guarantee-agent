"""``table_evidence``：结构化允许记录的抽取口径测试。

背景（修订稿 §3.1）：章节校验此前只把「系统表格 Markdown 拼成的文本」当允许数字来源，
表格单元格是裸数字、单位写在表头（``单位：万元``），模型在正文补上单位就会被判成新数字
而误退整章。这里的测试保证：结构化记录字段齐全、只收可用可追溯的业务格，且
``record_number_keys`` 同时给出「带单位」与「裸数字」两种形态。

全部用**纯内存构造** ``TableBlock`` 并 monkeypatch ``report_tables.chapter_table_blocks``，
不触数据库、不动任何生产文件。
"""
from __future__ import annotations

import re

from backend.app import report_tables
from backend.app.table_block import (
    TableBlock, TableCell, TableColumn, TableRow, annual_series_block, cell_from_text,
)
from backend.app.table_evidence import allowed_table_records, record_number_keys


# 每条记录至少应包含的字段（价值/原文/单位/科目/期间/来源/页码/坐标）。
RECORD_FIELDS = {
    "value", "raw", "unit", "fact_key", "label", "subject", "scope", "period",
    "source_ids", "page", "bbox", "cell_id", "origin_mode",
}


def _use_blocks(monkeypatch, blocks):
    """把该章的受控表格替换为给定的内存块。"""
    monkeypatch.setattr(
        report_tables, "chapter_table_blocks",
        lambda project_id, chapter_key, *, subject: list(blocks),
    )


def _records(monkeypatch, blocks, *, chapter_key="financial", subject="华辰公司"):
    _use_blocks(monkeypatch, blocks)
    return allowed_table_records("p", chapter_key, subject=subject)


def _value_cell(raw, *, unit="万元", page=9, source_ids=("s1",)):
    return cell_from_text(
        raw, unit=unit, origin_type="derived", source_ids=source_ids,
        locator={"page": page, "bbox": [1.0, 2.0, 3.0, 4.0], "row": 1, "column": 1},
    )


def _three_year_block() -> TableBlock:
    return annual_series_block(
        table_id="t-annual", title="三年主要财务数据", subject="华辰公司", scope="合并口径",
        unit="万元", period_labels=["2025 年末", "2024 年末", "2023 年末"],
        rows=[
            ("货币资金", (_value_cell("204,709.77"), _value_cell("180,000.00"), _value_cell("150,000"))),
            ("资产总计", (_value_cell("1,234,567.89"), _value_cell("1,100,000"), _value_cell("900,000"))),
            ("未分配利润", (_value_cell("-1,234"), _value_cell("-2,000"), _value_cell("-3,000"))),
        ],
        source_refs=["s1"],
    )


# 1. 正例 -------------------------------------------------------------------


def test_three_year_series_yields_complete_records(monkeypatch):
    """正例：三年序列表每条业务格都产出记录，字段齐全，且键同时含带单位与裸数字两种形态。"""
    records = _records(monkeypatch, [_three_year_block()])
    assert len(records) == 9, "3 行 × 3 年 = 9 条业务格记录"

    for record in records:
        assert RECORD_FIELDS <= set(record), f"记录字段不齐：{sorted(RECORD_FIELDS - set(record))}"
        assert record["subject"] == "华辰公司"
        assert record["scope"] == "合并口径"
        assert record["unit"] == "万元"
        assert record["origin_mode"] == "profile_facts"
        assert isinstance(record["source_ids"], tuple) and record["source_ids"]
        assert record["page"] == 9
        assert record["cell_id"].startswith("t-annual:r")
        assert record["period"] in {"2025", "2024", "2023"}

    cash = next(r for r in records if r["label"] == "货币资金" and r["period"] == "2025")
    assert cash["raw"] == "204,709.77"
    assert cash["value"] == 204709.77
    assert cash["unit"] == "万元"
    assert cash["fact_key"] == ""

    keys = record_number_keys(records)
    # 修订稿点名的形态：带声明单位的金额与同值裸数字都要登记。
    assert ("204709.77", "万元") in keys
    assert ("204709.77", "") in keys
    # 千分位与尾零不影响归一化。
    assert ("1234567.89", "万元") in keys
    assert ("180000", "万元") in keys
    assert ("180000", "") in keys


# 2. 负例：未披露 -----------------------------------------------------------


def test_not_applicable_cells_are_excluded(monkeypatch):
    """负例：``未披露``（not_applicable）的格不入记录。"""
    missing = TableCell(
        raw="未披露", status="not_applicable", origin_type="uploaded_cell",
        source_ids=("s1",), locator={"page": 9},
    )
    block = annual_series_block(
        table_id="t-na", title="t", subject="华辰公司", scope="合并口径", unit="万元",
        period_labels=["2025 年末"], rows=[("短期借款", (missing,))], source_refs=["s1"],
    )
    assert _records(monkeypatch, [block]) == []


# 3. 负例：不可追溯 ---------------------------------------------------------


def test_untraceable_cells_are_excluded(monkeypatch):
    """负例：没有 ``source_ids`` 或没有 ``locator.page`` 的格不入记录。"""
    no_source = TableCell(
        raw="6,820", normalized="6820", status="ok", origin_type="derived", locator={"page": 9},
    )
    no_page = TableCell(
        raw="6,820", normalized="6820", status="ok", origin_type="derived", source_ids=("s1",),
    )
    block = annual_series_block(
        table_id="t-trace", title="t", subject="华辰公司", scope="合并口径", unit="万元",
        period_labels=["2025 年末"],
        rows=[("缺来源", (no_source,)), ("缺页码", (no_page,))], source_refs=["s1"],
    )
    assert _records(monkeypatch, [block]) == []


# 4. 负例：序号/引用类列 ----------------------------------------------------


def test_reference_period_and_disclosure_columns_are_not_numeric(monkeypatch):
    """负例：序号/引用、期间名词、说明/资料列的数字不得当数值收录，只有 ``period_value`` 列算数。"""
    def raw_cell(raw):
        return TableCell(
            raw=raw, normalized=re.sub(r"[,\s]", "", raw), status="ok", origin_type="derived",
            source_ids=("s1",), locator={"page": 9},
        )

    block = TableBlock(
        table_id="t-roles", table_type="annual_series", title="t", subject="华辰公司",
        scope="合并口径", currency="CNY", unit="万元",
        columns=(
            TableColumn(key="subject", label="科目", role="subject"),
            TableColumn(key="ref", label="序号", role="reference"),
            TableColumn(key="period", label="期间", role="period"),
            TableColumn(key="disclosure", label="资料", role="disclosure"),
            TableColumn(key="value", label="2025 年末", role="period_value", unit="万元"),
        ),
        rows=(TableRow(row_key="货币资金", cells=(
            TableCell(raw="货币资金", status="text"),
            raw_cell("12"),      # 序号：不是金额
            raw_cell("2025"),    # 期间名词：不是金额
            raw_cell("3,000"),   # 资料索引：不是金额
            raw_cell("6,820"),   # 真正的业务数值
        )),),
    )
    records = _records(monkeypatch, [block])
    assert len(records) == 1
    assert records[0]["label"] == "货币资金"
    assert records[0]["value"] == 6820
    assert records[0]["unit"] == "万元"
    assert records[0]["period"] == "2025"  # 期间从列标签「2025 年末」推断


# 5. 负例：冲突状态的格 -----------------------------------------------------


def test_conflict_cells_are_excluded(monkeypatch):
    """负例：``conflict`` 状态的格不入记录。"""
    conflict = TableCell(
        raw="6,820", normalized="6820", status="conflict", origin_type="derived",
        source_ids=("s1",), locator={"page": 9},
    )
    block = annual_series_block(
        table_id="t-conflict", title="t", subject="华辰公司", scope="合并口径", unit="万元",
        period_labels=["2025 年末"], rows=[("存在冲突的口径", (conflict,))], source_refs=["s1"],
    )
    assert _records(monkeypatch, [block]) == []


# 6. 负例：冲突占位值（— / 空） --------------------------------------------


def test_placeholder_cells_are_excluded(monkeypatch):
    """负例：``—``/空占位格里只有正常业务值入记录。"""
    def placeholder(raw, status):
        return TableCell(
            raw=raw, normalized=None, status=status, origin_type="derived",
            source_ids=("s1",), locator={"page": 9},
        )

    block = annual_series_block(
        table_id="t-placeholder", title="t", subject="华辰公司", scope="合并口径", unit="万元",
        period_labels=["2025 年末"],
        rows=[
            ("破折号占位", (placeholder("—", "ok"),)),
            ("空占位", (placeholder("", "blank"),)),
            ("正常值", (_value_cell("6,820"),)),
        ],
        source_refs=["s1"],
    )
    records = _records(monkeypatch, [block])
    assert [record["label"] for record in records] == ["正常值"]


# 7. 负号保留 ---------------------------------------------------------------


def test_negative_values_keep_the_minus_sign(monkeypatch):
    """负号：``-1,234`` 这类格产出记录且 ``value`` 保留负号。"""
    block = annual_series_block(
        table_id="t-negative", title="t", subject="华辰公司", scope="合并口径", unit="万元",
        period_labels=["2025 年度"],
        rows=[("经营活动现金流量净额", (_value_cell("-1,234"),))], source_refs=["s1"],
    )
    records = _records(monkeypatch, [block])
    assert len(records) == 1
    assert records[0]["value"] == -1234
    assert str(records[0]["value"]).startswith("-")
    assert records[0]["raw"] == "-1,234"
    assert records[0]["period"] == "2025"  # 「2025 年度」→ 2025

    keys = record_number_keys(records)
    assert ("-1234", "万元") in keys
    assert ("-1234", "") in keys
