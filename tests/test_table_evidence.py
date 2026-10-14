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

from backend.app import report_tables, table_evidence
from backend.app.table_block import (
    TableBlock, TableCell, TableColumn, TableRow, annual_series_block, cell_from_text,
)
from backend.app.table_evidence import allowed_table_records, record_number_keys


# 每条记录至少应包含的字段（价值/原文/单位/科目/期间/来源/定位/解析版本/核验状态）。
RECORD_FIELDS = {
    "record_id", "value", "raw", "unit", "subject_key", "fact_key", "label", "period",
    "period_type", "subject", "role", "scope", "source_ids", "locator", "page", "bbox",
    "cell_id", "origin_mode", "table_id", "table_type", "parser_version", "verification",
}


def _use_blocks(monkeypatch, blocks, *, known_sources=None):
    """把该章的受控表格替换为给定的内存块，并把项目来源清单固定下来。"""
    monkeypatch.setattr(
        report_tables, "chapter_table_blocks",
        lambda project_id, chapter_key, *, subject: list(blocks),
    )
    if known_sources is None:
        known_sources = {source for block in blocks for cell in block.cells() for source in cell.source_ids}
    monkeypatch.setattr(table_evidence, "project_source_ids", lambda project_id: set(known_sources))
    monkeypatch.setattr(table_evidence, "subject_role_labels", lambda project_id: {})


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
    # 数值序列化为**十进制字符串**（不是 float），精度与尾零不丢。
    assert isinstance(cash["value"], str)
    assert cash["value"] == "204709.77"
    assert cash["unit"] == "万元"
    assert cash["fact_key"] == ""
    assert cash["period_type"] == "annual"
    assert cash["verification"] == "ok"
    assert cash["parser_version"] == table_evidence.PARSER_VERSION
    assert cash["record_id"].endswith(table_evidence.PARSER_VERSION)

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
    """负例：没有 ``source_ids`` 的格不入记录——来源是硬门槛（不论有无页码）。"""
    no_source = TableCell(
        raw="6,820", normalized="6820", status="ok", origin_type="derived", locator={"page": 9},
    )
    block = annual_series_block(
        table_id="t-trace", title="t", subject="华辰公司", scope="合并口径", unit="万元",
        period_labels=["2025 年末"],
        rows=[("缺来源", (no_source,)), ("正常值", (_value_cell("6,821"),))], source_refs=["s1"],
    )
    records = _records(monkeypatch, [block])
    assert [record["label"] for record in records] == ["正常值"]


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
    assert records[0]["value"] == "6820"
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
    assert records[0]["value"] == "-1234"
    assert str(records[0]["value"]).startswith("-")
    assert records[0]["raw"] == "-1,234"
    assert records[0]["period"] == "2025"  # 「2025 年度」→ 2025

    keys = record_number_keys(records)
    assert ("-1234", "万元") in keys
    assert ("-1234", "") in keys


# 8. 序号列与行标签（验收实测：序号列当科目 → 纯数字标签污染科目词表） ----------


def _text_cell(raw):
    return TableCell(raw=raw, status="text", origin_type="uploaded_cell",
                     source_ids=("s1",), locator={"page": 9})


def _num_cell(raw, *, unit=""):
    return cell_from_text(
        raw, unit=unit, origin_type="uploaded_cell", source_ids=("s1",),
        locator={"page": 9, "bbox": [1.0, 2.0, 3.0, 4.0]},
    )


def test_sequential_header_is_reference_even_at_column_zero():
    """表头写明「序号」的第 0 列不是科目列——否则行标签会退化成「1」。"""
    from backend.app.table_block import business_column_role

    assert business_column_role("序号", 0) == "reference"
    assert business_column_role("序", 0) == "reference"
    assert business_column_role("编号", 0) == "reference"
    assert business_column_role("借款类别", 0) == "subject"
    assert business_column_role("期末余额", 1) == "period_value"


def test_sequential_number_column_never_becomes_the_row_label(monkeypatch):
    """正例：第一列是序号列时，行标签取名称列（银行授信表实测）。"""
    block = TableBlock(
        table_id="t-banks", table_type="annual_series", title="银行授信情况",
        subject="华辰公司", scope="合并口径", currency="CNY", unit="万元",
        columns=(
            TableColumn(key="column_0", label="序号", role="reference"),
            TableColumn(key="column_1", label="授信银行", role="disclosure"),
            TableColumn(key="column_2", label="授信额度", role="period_value", unit="万元"),
        ),
        rows=(
            TableRow(row_key="1", cells=(
                _text_cell("1"), _text_cell("国家开发银行"), _num_cell("8,275,200"),)),
            TableRow(row_key="2", cells=(
                _text_cell("2"), _text_cell("工商银行"), _num_cell("6,058,500"),)),
        ),
        source_refs=("s1",),
    )
    records = _records(monkeypatch, [block])
    assert [record["label"] for record in records] == ["国家开发银行", "工商银行"]
    assert {record["value"] for record in records} == {"8275200", "6058500"}
    assert all(record["unit"] == "万元" for record in records)
    # 序号数字不得成为允许值
    keys = record_number_keys(records)
    assert ("8275200", "万元") in keys
    assert all(value != "1" for value, _unit in keys)


# 9. 整表单位注解「万元、%」的多族拆分 ---------------------------------------


def test_merged_table_unit_annotation_is_split_by_column_label(monkeypatch):
    """「单位：万元、%」：金额列取万元、占比列取 %；两族都判不出时标记待核验。"""
    block = TableBlock(
        table_id="t-longterm", table_type="annual_series", title="长期借款构成情况",
        subject="华辰公司", scope="合并口径", currency="CNY", unit="万元、%",
        columns=(
            TableColumn(key="column_0", label="借款类别", role="subject"),
            TableColumn(key="column_1", label="期末余额", role="period_value", unit=""),
            TableColumn(key="column_2", label="占比", role="period_value", unit="%"),
            TableColumn(key="column_3", label="其他", role="period_value", unit=""),
        ),
        rows=(
            TableRow(row_key="信用借款", cells=(
                _text_cell("信用借款"), _num_cell("30,943,607.74"),
                _num_cell("94.14", unit="%"), _num_cell("1,234.56"),)),
        ),
        source_refs=("s1",),
    )
    records = _records(monkeypatch, [block])
    by_column = {record["cell_id"][-3:]: record for record in records}
    # 期末余额（金额族）→ 万元，且确定（不再把"整表也写了单位"当冲突）
    assert by_column["c01"]["unit"] == "万元"
    assert by_column["c01"]["verification"] == "ok"
    assert by_column["c02"]["unit"] == "%"
    assert by_column["c02"]["verification"] == "ok"
    # 「其他」列两族都无从对应 → 取第一族并标记待核验
    assert by_column["c03"]["unit"] == "万元"
    assert by_column["c03"]["verification"] == "needs_review"
    # 带单位的金额在该记录上有同单位键（正文写「30,943,607.74万元」必须能命中）
    keys = record_number_keys(records)
    assert ("30943607.74", "万元") in keys


def test_placeholder_column_resolves_unit_by_caption_or_value_shape(monkeypatch):
    """「列待核验」列 + 双族注解：先用表内名称格恢复列名；恢复不出按整列数值形态判族。"""
    block = TableBlock(
        table_id="t-inventory", table_type="annual_series", title="存货情况",
        subject="华辰公司", scope="合并口径", currency="CNY", unit="万元、%",
        columns=(
            TableColumn(key="column_0", label="项目", role="subject"),
            TableColumn(key="column_1", label="列待核验", role="period_value", unit=""),
            TableColumn(key="column_2", label="列待核验", role="period_value", unit=""),
            TableColumn(key="column_3", label="列待核验", role="period_value", unit=""),
        ),
        rows=(
            # 续表重复表头行：c01 能干净恢复「占比」；c02 的恢复结果带换行不可信 → 数值形态；
            # c03 无名称格 → 也走数值形态
            TableRow(row_key="h", cells=(
                _text_cell(""), _text_cell("占比"), _text_cell("账面\n价值"), _text_cell(""))),
            TableRow(row_key="库存商品", cells=(
                _text_cell("库存商品"), _num_cell("18.66"), _num_cell("1,575,073.57"), _num_cell("13.71"))),
            TableRow(row_key="其他", cells=(
                _text_cell("其他"), _num_cell("49.97"), _num_cell("4,218,593.88"), _num_cell("47.85"))),
        ),
        source_refs=("s1",),
    )
    records = _records(monkeypatch, [block])
    by_column = {record["cell_id"][-3:]: record for record in records}
    assert by_column["c01"]["unit"] == "%"         # 列名恢复「占比」
    assert by_column["c02"]["unit"] == "万元"      # 数值形态：存在 > 100 的金额
    assert by_column["c02"]["verification"] == "needs_review"
    assert by_column["c03"]["unit"] == "%"         # 数值形态：整列 ≤ 100 → 占比族
    assert by_column["c03"]["verification"] == "needs_review"
    # 数值形态与列名恢复都是启发式判定 → 仍标注待核验，不冒充精确结论
    keys = record_number_keys(records)
    assert ("18.66", "%") in keys
    assert ("1575073.57", "万元") in keys



# 6. 科目登记（量纲词尾 → 单位）----------------------------------------------


def _dimension_block(*rows) -> TableBlock:
    """行名只写科目、**不写单位后缀**的表：单位只能靠科目登记给出。"""
    return annual_series_block(
        table_id="t-dimension", title="主要财务指标", subject="华辰公司", scope="合并口径",
        unit="", period_labels=["2025 年度", "2024 年度"],
        rows=[(label, tuple(_num_cell(value) for value in values)) for label, values in rows],
        source_refs=["s1"],
    )


def test_dimension_registry_supplies_missing_units(monkeypatch):
    """正例：行名没写单位但科目名自带量纲 → 登记表补「次」「倍」。

    实测（北投 p086-t02）：原文在部分行写「（%）」「（亿元）」，而周转率与流动/速动比率行
    **什么都没写**，四行因 `unit=""` 被判 needs_review，正文写「11.75次」反而成"新数字"。
    计划 §三允许按**科目登记/表注/公式**恢复单位；这里证明「次」「倍」被正确恢复，
    且恢复后四条记录不再待核验。
    """
    records = _records(monkeypatch, [_dimension_block(
        ("应收账款周转率", ("12.27", "8.14")),
        ("存货周转率", ("0.36", "0.34")),
        ("流动比率", ("1.39", "1.53")),
        ("速动比率", ("0.66", "0.68")),
    )])
    by_label = {record["label"]: record for record in records}
    assert by_label["应收账款周转率"]["unit"] == "次"
    assert by_label["存货周转率"]["unit"] == "次"
    assert by_label["流动比率"]["unit"] == "倍"
    assert by_label["速动比率"]["unit"] == "倍"
    for label in ("应收账款周转率", "存货周转率", "流动比率", "速动比率"):
        assert by_label[label]["verification"] == "ok", label


def test_dimension_registry_never_guesses_from_value_magnitude(monkeypatch):
    """负例：登记表只认**词尾**，不认数值大小。

    "总资产""其他应收款"这类金额科目名不在登记表里（金额单位必须由表头/列名给出），
    数值再小也不得被猜成「次」或「倍」（计划 §三：禁止按数值大小猜单位）。
    """
    records = _records(monkeypatch, [_dimension_block(
        ("总资产", ("0.36", "1.39")),
        ("其他应收款", ("12.27", "8.14")),
    )])
    for record in records:
        assert record["unit"] == "", record
        assert record["verification"] == "needs_review", record


def test_explicit_unit_is_not_overridden_by_the_registry(monkeypatch):
    """负例：行名**已经写了**单位时，登记表不得改写它。

    "存货周转率（%）"是原文显式声明（哪怕不合常理），必须以原文为准；这也保证
    「（亿元、%）」这类并列两族的写法仍走原歧义路径，不会被登记表"修好"。
    """
    records = _records(monkeypatch, [_dimension_block(
        ("存货周转率（%）", ("0.36", "0.34")),
        ("速动比率（亿元、%）", ("1.39", "1.53")),
    )])
    by_label = {record["label"]: record for record in records}
    assert by_label["存货周转率（%）"]["unit"] == "%"
    # 并列两族：不得被登记表改成「倍」，退回列/表层级（此处都为空）→ 待核验
    assert by_label["速动比率（亿元、%）"]["unit"] != "倍"
    assert by_label["速动比率（亿元、%）"]["verification"] == "needs_review"
