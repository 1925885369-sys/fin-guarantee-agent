"""``TableBlock v1`` 契约测试。

这是**冻结契约**：表型、来源模式、单元格字段、以及三条安全不变量。
契约一旦变更，前端、Markdown 与 Word 三个出口必须同步——所以这里既测结构，
也测「契约被悄悄放宽」的情况。
"""
from __future__ import annotations

import json

import pytest

from backend.app.bond_documents import CASE_BY_ID, _page_tables
from backend.app.table_block import (
    ORIGIN_MODES, TABLE_BLOCK_VERSION, TABLE_TYPES, TableBlock, TableCell, TableColumn, TableRow,
    annual_series_block, block_from_extracted, block_from_financial_table, cell_from_text,
    conflict_comparison_block, profile_facts_block, reconciliation_block, validate_table_block,
    validated,
)
from backend.app.table_extraction import ExtractedTable, extract_tables


CELL_FIELDS = {
    "raw", "normalized", "unit", "status", "origin_type", "fact_key", "metric_key",
    "source_ids", "locator", "adoption_basis", "manual_note",
}


def _profile_block() -> TableBlock:
    return annual_series_block(
        table_id="t-profile", title="主要财务数据", subject="某公司", scope="合并口径", unit="万元",
        period_labels=["2025 年末", "2024 年末"],
        rows=[
            ("货币资金", (cell_from_text("6,820", source_ids=["s1"]), cell_from_text("6,206", source_ids=["s1"]))),
            ("应收账款", (cell_from_text("12,000", source_ids=["s1"]), cell_from_text("10,920", source_ids=["s1"]))),
        ],
        source_refs=["s1"],
    )


def test_contract_exposes_exactly_three_table_types_and_origin_modes() -> None:
    assert TABLE_TYPES == ("annual_series", "dual_source_reconciliation", "conflict_comparison")
    assert ORIGIN_MODES == ("profile_facts", "uploaded_table", "merged")
    assert TABLE_BLOCK_VERSION == "table-block-v1"


def test_cell_carries_the_full_traceability_field_set() -> None:
    """单元格字段齐全是契约的一部分——少一个字段，下游就少一条追溯依据。"""
    assert set(cell_from_text("1,234").to_dict()) == CELL_FIELDS


def test_unknown_enum_values_are_rejected() -> None:
    """契约不允许悄悄放宽：未知表型/来源模式/列角色/单元格状态一律报错。"""
    with pytest.raises(ValueError):
        TableColumn(key="x", label="x", role="不存在")
    with pytest.raises(ValueError):
        TableCell(status="大概行吧")
    with pytest.raises(ValueError):
        annual_series_block(
            table_id="t", title="t", subject="s", scope="c", unit="万元",
            period_labels=["2025"], rows=[], origin_mode="瞎猜",
        )


def test_row_width_must_match_column_count() -> None:
    with pytest.raises(ValueError, match="单元格数与列数不一致"):
        TableBlock(
            table_id="t", table_type="annual_series", title="t", subject="s", scope="c",
            currency="CNY", unit="万元",
            columns=(TableColumn(key="subject", label="科目", role="subject"),),
            rows=(TableRow(row_key="r", cells=(TableCell(raw="a"), TableCell(raw="b"))),),
        )


def test_unknown_currency_is_rejected() -> None:
    with pytest.raises(ValueError, match="人民币"):
        TableBlock(
            table_id="t", table_type="annual_series", title="t", subject="s", scope="c",
            currency="USD", unit="万元", columns=(), rows=(),
        )


def test_annual_series_block_has_dynamic_period_columns() -> None:
    block = _profile_block()
    assert [column.role for column in block.columns] == ["subject", "period_value", "period_value"]
    assert [column.label for column in block.period_columns] == ["2025 年末", "2024 年末"]
    assert block.origin_mode == "profile_facts"
    assert block.validation_status == "passed"


def test_reconciliation_uses_dynamic_five_columns() -> None:
    block = reconciliation_block(
        table_id="t-recon", title="双来源核对", subject="某公司", scope="合并口径", period="2025",
        profile_cells={"货币资金": cell_from_text("6,820", source_ids=["p1"])},
        uploaded_cells={"货币资金": cell_from_text("6,820", source_ids=["u1"])},
    )
    assert [column.label for column in block.columns] == ["科目", "档案值", "上传值", "采用值", "状态"]
    assert block.table_type == "dual_source_reconciliation"
    assert block.origin_mode == "merged"


def test_agreed_sources_are_adopted_with_basis() -> None:
    block = reconciliation_block(
        table_id="t", title="t", subject="s", scope="c", period="2025",
        profile_cells={"货币资金": cell_from_text("6,820", source_ids=["p1"])},
        uploaded_cells={"货币资金": cell_from_text("6,820", source_ids=["u1"])},
    )
    adopted = block.rows[0].cell(3)
    assert adopted.status == "adopted"
    assert adopted.normalized == "6820"
    assert adopted.adoption_basis
    assert set(adopted.source_ids) == {"p1", "u1"}, "采用值必须同时指向两个来源"
    assert block.validation_status == "passed"


def test_disagreeing_sources_must_not_be_auto_adopted() -> None:
    """核心安全不变量：两来源取值不同时，采用值必须为空，不得自动择一。"""
    block = reconciliation_block(
        table_id="t", title="t", subject="s", scope="c", period="2025",
        profile_cells={"货币资金": cell_from_text("6,820", source_ids=["p1"])},
        uploaded_cells={"货币资金": cell_from_text("5,940", source_ids=["u1"])},
    )
    row = block.rows[0]
    assert row.cell(1).raw == "6,820", "档案值必须保留"
    assert row.cell(2).raw == "5,940", "上传值必须保留"
    adopted = row.cell(3)
    assert adopted.status == "conflict"
    assert adopted.normalized in {None, ""}, "冲突时采用值必须为空"
    assert adopted.adoption_basis
    assert block.validation_status == "passed", "并列保留即为合规，不应判为阻断"


def test_single_source_rows_record_which_source() -> None:
    block = reconciliation_block(
        table_id="t", title="t", subject="s", scope="c", period="2025",
        profile_cells={"货币资金": cell_from_text("6,820", source_ids=["p1"])},
        uploaded_cells={},
    )
    adopted = block.rows[0].cell(3)
    assert adopted.status == "adopted"
    assert adopted.origin_type == "profile_fact"
    assert "档案" in adopted.adoption_basis


def test_untraceable_cell_blocks_the_block() -> None:
    """有来源可查才叫业务数据；凭空出现的数字必须阻断交付。"""
    block = annual_series_block(
        table_id="t", title="t", subject="s", scope="c", unit="万元",
        period_labels=["2025"], rows=[("货币资金", (cell_from_text("6,820"),))],
    )
    assert block.validation_status == "blocked"
    assert "货币资金" in block.validation_summary["untraceable"]


def test_manual_cell_needs_a_basis_and_is_traceable_with_one() -> None:
    without_basis = TableCell(raw="1", normalized="1", status="ok", origin_type="manual")
    assert not without_basis.traceable
    with_basis = TableCell(raw="1", normalized="1", status="ok", origin_type="manual", adoption_basis="人工核对合同")
    assert with_basis.traceable


def test_conflict_cell_in_an_uploaded_block_must_have_a_locator() -> None:
    block = block_from_extracted(
        _extracted_fixture(with_bbox=True), table_id="t", subject="s", document_id="doc",
    )
    assert block.validation_status == "passed"
    stripped = block_from_extracted(
        _extracted_fixture(with_bbox=False), table_id="t", subject="s", document_id="doc",
    )
    assert stripped.validation_status == "blocked"
    assert stripped.validation_summary["unattributed_cells"]


def _extracted_fixture(*, with_bbox: bool) -> ExtractedTable:
    from backend.app.table_extraction import ExtractedCell

    def cell(row: int, column: int, text: str):
        bbox = (52.0, 100.0 + row * 20, 543.0, 120.0 + row * 20) if with_bbox else None
        return ExtractedCell(row=row, column=column, text=text, bbox=bbox, page=9)

    return ExtractedTable(
        table_key="p009-t01", caption="合并资产负债表", unit="万元", page=9, pages=[9],
        columns=["项目", "2025 年末"],
        rows=[[cell(1, 0, "货币资金"), cell(1, 1, "6,820")]],
        confidence=1.0, status="extracted",
    )


def test_conflict_comparison_keeps_every_disclosure() -> None:
    """冲突表要有意义，就必须把所有披露值都摆出来——它没有「采用值」列。"""
    block = conflict_comparison_block(
        table_id="t-conflict", title="来源冲突", subject="某公司",
        conflicts=[{
            "fact_key": "bond_issue_amount", "period": "2026", "scope": "standalone",
            "assertions": [
                {"raw": "20,000", "source_ids": ["a"], "locator": {"document_id": "募集说明书", "page": 9}},
                {"raw": "24,000", "source_ids": ["b"], "locator": {"document_id": "评级报告", "page": 3}},
            ],
        }],
    )
    assert [column.label for column in block.columns] == ["事项", "期间与口径", "资料及定位", "披露值", "状态"]
    assert all(column.role != "adopted_value" for column in block.columns), "冲突表不得设采用值列"
    disclosure = block.rows[0].cell(3)
    assert "20,000" in disclosure.raw and "24,000" in disclosure.raw
    assert disclosure.status == "conflict"


def test_profile_facts_block_does_not_claim_source_extraction() -> None:
    """档案只有事实时生成的汇总，不得声称是从原始表提取的。"""
    block = profile_facts_block(
        table_id="t-profile", title="主体档案主要财务数据（事实汇总）", subject="某公司", unit="万元",
        facts=[
            {"fact_key": "cash", "name": "货币资金", "period": "2025", "value": 6820, "raw_value": "6820",
             "unit": "万元", "source_ids": ["s1"]},
        ],
    )
    assert block.origin_mode == "profile_facts"
    assert any("不是从原始报表逐格提取" in note for note in block.notes)
    assert block.rows[0].cell(1).origin_type == "profile_fact"


def test_block_is_immutable_and_hashable_by_content() -> None:
    block = _profile_block()
    assert block.content_hash() == _profile_block().content_hash()
    changed = annual_series_block(
        table_id="t-profile", title="主要财务数据", subject="某公司", scope="合并口径", unit="万元",
        period_labels=["2025 年末", "2024 年末"],
        rows=[("货币资金", (cell_from_text("6,821", source_ids=["s1"]), cell_from_text("6,206", source_ids=["s1"]))),
              ("应收账款", (cell_from_text("12,000", source_ids=["s1"]), cell_from_text("10,920", source_ids=["s1"])))],
        source_refs=["s1"],
    )
    assert changed.content_hash() != block.content_hash()


def test_validated_returns_a_new_block_without_mutating_the_original() -> None:
    raw = annual_series_block(
        table_id="t", title="t", subject="s", scope="c", unit="万元",
        period_labels=["2025"], rows=[("货币资金", (cell_from_text("6,820"),))],
    )
    assert raw.validation_status == "blocked"
    again = validated(raw)
    assert again.validation_status == "blocked"
    assert raw is not again


def test_round_trip_from_renderer_declared_tables() -> None:
    """渲染器的声明表能无损转成契约，且数值与表内原文一致。"""
    case = CASE_BY_ID["demo-bond-huachen"]
    declared = _page_tables(case, "prospectus", "financial_2")[0]
    block = block_from_financial_table(
        declared, table_id="huachen-balance-sheet", subject=case.company, source_refs=["s1"],
    )
    assert block.table_type == "annual_series"
    assert [column.label for column in block.period_columns] == ["2025 年末", "2024 年末", "2023 年末"]
    row = next(item for item in block.rows if item.row_key == "资产总计")
    latest = case.statements[-1]
    assert row.cell(1).normalized == str(latest.balance_sheet.total_assets)
    assert block.validation_status == "passed"


def test_round_trip_from_extracted_tables_keeps_page_and_bbox(tmp_path) -> None:
    """上传材料的原表必须逐格可定位：页码与坐标都要带进契约。"""
    from backend.app.bond_documents import generate_case_materials

    case = CASE_BY_ID["demo-bond-huachen"]
    materials = generate_case_materials(tmp_path, case)
    extracted = next(
        table for table in extract_tables(materials[0]["path"]) if table.caption == "合并资产负债表"
    )
    block = block_from_extracted(
        extracted, table_id="uploaded-balance-sheet", subject=case.company, document_id="doc-1",
    )
    assert block.origin_mode == "uploaded_table"
    assert block.validation_status == "passed"
    located = [cell for cell in block.cells() if cell.status in {"ok", "zero"}]
    assert located
    for cell in located:
        assert cell.locator.get("page")
        assert cell.locator.get("bbox")


def test_json_round_trip_is_stable() -> None:
    """契约要能被前端/导出直接消费：序列化必须稳定、可反序列化。"""
    block = _profile_block()
    payload = json.dumps(block.to_dict(), ensure_ascii=False, sort_keys=True)
    restored = json.loads(payload)
    assert restored["version"] == TABLE_BLOCK_VERSION
    assert restored["columns"][1]["role"] == "period_value"
    assert set(restored["rows"][0]["cells"][0]) == CELL_FIELDS


# ---------------------------------------------------------------------------
# 多层表头按「原表几何关系」恢复（修订稿 §2.3）
# ---------------------------------------------------------------------------


def _grid_table(headers, body, columns_x, *, unit="万元、%"):
    """按列 x 区间构造抽取表，使 bbox 能真正区分列（而非所有列共用同一区间）。"""
    from backend.app.table_extraction import ExtractedCell, ExtractedTable

    def cell(row: int, column: int, text: str):
        x0, x1 = columns_x[column]
        return ExtractedCell(
            row=row, column=column, text=text,
            bbox=(float(x0), 20.0 + row * 20, float(x1), 40.0 + row * 20), page=9,
        )

    return ExtractedTable(
        table_key="p009-t01", caption="多层表头", unit=unit, page=9, pages=[9],
        columns=headers,
        rows=[[cell(r, c, text) for c, text in enumerate(row)] for r, row in enumerate(body)],
        confidence=1.0, status="extracted",
    )


def test_parent_header_covers_only_its_own_columns_by_geometry() -> None:
    """正例：父表头只覆盖它实际跨越的列，不污染相邻的独立列。"""
    table = _grid_table(
        ["项目", "2025 年末", "", "备注"],
        [["", "金额", "占比", ""], ["银行存款", "1,000", "99.00", "说明"]],
        columns_x=[(50, 120), (120, 200), (200, 280), (280, 360)],
    )
    block = block_from_extracted(table, table_id="t", subject="公司")
    labels = [column.label for column in block.columns]
    assert labels[1] == "2025 年末 · 金额（万元）"
    assert labels[2] == "2025 年末 · 占比"
    assert labels[3] == "备注" and "2025 年末" not in labels[3]


def test_parent_header_must_not_cross_a_geometric_break() -> None:
    """负例（禁止跨父级左填）：父表头只跨前 2 列，后 2 列几何上另成一组，不得继承。

    原「左填」实现会把「2025 年末」一路推到与之无关的后两列；这里后两列的 x 区间
    与前一组之间存在明显断裂，父表头的跨越就应当在此终止。
    """
    table = _grid_table(
        ["项目", "2025 年末", "", "", ""],
        [["", "金额", "占比", "金额", "占比"], ["银行存款", "1,000", "99.00", "2,000", "98.00"]],
        columns_x=[(50, 120), (120, 200), (200, 280), (330, 410), (410, 490)],
    )
    block = block_from_extracted(table, table_id="t", subject="公司")
    labels = [column.label for column in block.columns]
    assert labels[1].startswith("2025 年末")
    assert labels[2].startswith("2025 年末")
    assert "2025 年末" not in labels[3], "几何断裂之后的列不得被错误继承父标签"
    assert "2025 年末" not in labels[4]


def test_continuation_first_row_of_business_data_is_kept() -> None:
    """负例：续页首行是业务数据（表头留在上一页）时必须保留，不得当作子表头剥离。"""
    table = _grid_table(
        ["项目", "2025 年末", "2024 年末"],
        [["", "6,820", "6,206"], ["银行存款", "5,000", "4,000"]],
        columns_x=[(50, 120), (120, 200), (200, 280)],
    )
    block = block_from_extracted(table, table_id="t", subject="公司")
    assert len(block.rows) == 2, "续页首行业务数据不能被吞掉"
    assert block.rows[0].cell(1).raw == "6,820"
    assert block.rows[1].cell(1).raw == "5,000"


def test_header_inheritance_falls_back_when_geometry_is_missing() -> None:
    """回退：bbox 全为 None 或所有列共用同一 x 区间时，行为与改动前一致（左填）。"""
    from backend.app.table_extraction import ExtractedCell, ExtractedTable

    headers = ["项目", "2025 年末", ""]
    body = [["", "金额", "占比"], ["银行存款", "1,000", "99.00"]]

    def build(bbox_for):
        return ExtractedTable(
            table_key="t", caption="c", unit="万元、%", page=1, pages=[1], columns=headers,
            rows=[[ExtractedCell(r, c, text, bbox_for(r, c), page=1)
                   for c, text in enumerate(row)] for r, row in enumerate(body)],
            confidence=1.0, status="extracted",
        )

    no_bbox = block_from_extracted(build(lambda r, c: None), table_id="t", subject="公司")
    same_bbox = block_from_extracted(
        build(lambda r, c: (10.0, 20.0 + r * 10, 90.0, 30.0 + r * 10)),
        table_id="t", subject="公司",
    )
    for block in (no_bbox, same_bbox):
        labels = [column.label for column in block.columns]
        assert labels[1] == "2025 年末 · 金额（万元）"
        assert labels[2] == "2025 年末 · 占比"


def test_cell_level_needs_review_is_aggregated_to_the_whole_block() -> None:
    """格级待核验必须**汇总**成整表待核验并阻断最终交付（修订稿 §2.3）。

    阻断判据是 ``validation_status != "passed"``（``report_tables.blocked_reason`` 就是这么判的），
    所以这里既断言返回状态，也断言 ``validated()`` 落下来的 ``validation_status``。
    """
    from dataclasses import replace as dc_replace

    cell = dc_replace(
        cell_from_text("6,820", source_ids=["s1"], origin_type="uploaded_cell",
                       locator={"page": 9, "bbox": [1.0, 2.0, 3.0, 4.0]}),
        status="needs_review",
    )
    block = annual_series_block(
        table_id="t", title="t", subject="s", scope="c", unit="万元",
        period_labels=["2025"], rows=[("货币资金", (cell,))],
    )
    status, summary = validate_table_block(block)
    assert status == "needs_review"
    assert summary["cells_needing_review"] == ["货币资金"]
    assert validated(block).validation_status == "needs_review"


def test_block_without_cell_review_flags_stays_passed() -> None:
    """负例：没有格级待核验时不得误汇总成待核验——否则整表闸门会被放大成噪声。"""
    ok = cell_from_text("6,820", source_ids=["s1"], origin_type="uploaded_cell",
                        locator={"page": 9, "bbox": [1.0, 2.0, 3.0, 4.0]})
    block = annual_series_block(
        table_id="t", title="t", subject="s", scope="c", unit="万元",
        period_labels=["2025"], rows=[("货币资金", (ok,))],
    )
    status, summary = validate_table_block(block)
    assert status == "passed"
    assert summary["cells_needing_review"] == []
