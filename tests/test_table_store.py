"""表格落库与双来源核对。

在此之前，材料里的表格只是解析过程中的临时产物：``parse_document`` 产出后即被丢弃，
只有文本片段进库。于是「上传原表 → 核对」这条链路没有数据可依，报告也只能退回
``profile_facts`` 汇总。

这组测试钉住三件事：

1. 表格与单元格能原样往返（文本、页码、坐标都不能丢）；
2. ``uploaded_table`` 与 ``profile_facts`` 两种来源可以分别独立生成；
3. 双来源核对按「主体＋口径＋单位＋科目＋年度」匹配——**年度列错一位会让整表变成
   假冲突**，这是实测踩过的坑。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.database import rows
from backend.app.main import app
from backend.app.report_tables import reconciliation_tables, uploaded_table_blocks
from backend.app.table_store import load_tables, table_inventory

PROJECT = "demo-bond-huachen"
SUBJECT = "华辰精密制造有限公司"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module", autouse=True)
def seeded(client):
    client.get(f"/api/projects/{PROJECT}/bond-summary")


def test_tables_are_persisted_for_every_material() -> None:
    count = rows("SELECT COUNT(*) n FROM document_tables WHERE project_id=?", (PROJECT,))[0]["n"]
    assert count > 0, "材料里的表格必须落库，否则核对链路无据可依"
    inventory = table_inventory(PROJECT)
    assert inventory["total"] == count
    assert all(item["start_page"] for item in inventory["tables"]), "每张表都要记录页码"


def test_round_trip_keeps_cell_text_page_and_bbox() -> None:
    tables = load_tables(PROJECT)
    assert tables
    cells = [cell for table in tables for row in table.rows for cell in row]
    assert cells
    for cell in cells[:40]:
        assert isinstance(cell.text, str)
        assert cell.page > 0
    located = [cell for cell in cells if cell.bbox]
    assert located and len(located[0].bbox) == 4


def test_headers_and_continuation_are_preserved() -> None:
    balance = next(table for table in load_tables(PROJECT) if table.caption == "合并资产负债表")
    assert balance.columns[0] == "项目"
    assert len(balance.columns) == 4
    assert balance.row_count > 25
    assert len(balance.pages) == 2, "跨页表要记录页码范围"


def test_uploaded_table_blocks_are_independent_of_profile_facts() -> None:
    """两种来源模式可以分别生成——这是「不把档案汇总说成原表提取」的前提。"""
    uploaded = uploaded_table_blocks(PROJECT, subject=SUBJECT)
    assert uploaded
    assert all(block.origin_mode == "uploaded_table" for block in uploaded)
    assert all(block.table_type == "annual_series" for block in uploaded)
    located = [cell for block in uploaded for cell in block.cells() if cell.status in {"ok", "zero"}]
    assert located and all(cell.locator.get("page") and cell.locator.get("bbox") for cell in located)


def test_profile_blocks_declare_themselves_as_summaries(client) -> None:
    from backend.app.report_tables import financial_table_blocks

    blocks = financial_table_blocks(PROJECT, subject=SUBJECT)
    assert blocks
    for block in blocks:
        if block.table_type == "conflict_comparison":
            continue
        assert block.origin_mode == "profile_facts"
        assert any("不是从原始报表逐格提取" in note for note in block.notes)


def test_reconciliation_matches_by_year_and_scope() -> None:
    """同口径同值 → 采用值可填；年度错位会让整表变成假冲突，这里直接钉住对应关系。"""
    blocks = reconciliation_tables(PROJECT, subject=SUBJECT)
    assert blocks, "档案与上传材料都在，应当能生成核对表"
    years = {block.scope.split("／")[-1] for block in blocks}
    assert any("2025" in item for item in years)
    for block in blocks:
        assert block.table_type == "dual_source_reconciliation"
        assert block.origin_mode == "merged"
        assert block.validation_status == "passed", block.validation_summary
        for row in block.rows:
            profile, uploaded, adopted = row.cell(1).raw, row.cell(2).raw, row.cell(3)
            if profile == "—" or uploaded == "—":
                continue
            # 同一科目同一年度：档案值与上传值必须相等，否则就是年度列错位
            assert adopted.status != "conflict" or profile != uploaded, row.row_key


def test_reconciliation_adopts_only_when_sources_agree() -> None:
    blocks = reconciliation_tables(PROJECT, subject=SUBJECT)
    consistent = [
        row for block in blocks for row in block.rows
        if row.cell(1).raw not in {"—", ""} and row.cell(2).raw not in {"—", ""}
    ]
    assert consistent
    for row in consistent:
        assert row.cell(3).status == "adopted"
        assert row.cell(3).adoption_basis


def test_single_source_rows_say_which_source() -> None:
    blocks = reconciliation_tables(PROJECT, subject=SUBJECT)
    rows_ = [row for block in blocks for row in block.rows]
    single = [row for row in rows_ if row.cell(2).raw in {"—", ""} and row.cell(1).raw not in {"—", ""}]
    assert single, "应当存在只有档案一侧取值的科目"
    for row in single:
        assert "档案" in row.cell(3).adoption_basis


def test_disagreeing_values_are_never_auto_adopted() -> None:
    """核对表的核心安全不变量：同口径不同值时采用值必须留空。"""
    from backend.app.table_block import reconciliation_block, cell_from_text

    block = reconciliation_block(
        table_id="t", title="t", subject="某公司", scope="合并口径", period="2025",
        profile_cells={"货币资金": cell_from_text("6,820", unit="万元", source_ids=["p1"])},
        uploaded_cells={"货币资金": cell_from_text("5,940", unit="万元", source_ids=["u1"])},
    )
    adopted = block.rows[0].cell(3)
    assert adopted.status == "conflict"
    assert adopted.normalized in {None, ""}
    assert block.rows[0].cell(1).raw == "6,820"
    assert block.rows[0].cell(2).raw == "5,940"
