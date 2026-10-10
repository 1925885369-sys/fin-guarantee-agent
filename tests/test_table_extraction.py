"""表格抽取与金标的契约测试。

这组测试要证明的不是「抽取器能跑通」，而是**抽取结果确实对得上金标，而且对不上时
一定会被发现**。为此除了正例，还有四组负例（篡改、换年、跨主体、缺前导块）——
没有负例的 100% 是空转。
"""
from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from backend.app.bond_documents import CASE_BY_ID, CASE_SPECS, case_table_gold, generate_case_materials
from backend.app.config import settings
from backend.app.parsers import parse_document
from backend.app.table_extraction import (
    TABLE_PARSER_VERSION, compare_with_gold, extract_tables, normalize_cell,
)


@pytest.fixture(scope="module")
def huachen(tmp_path_factory) -> tuple:
    case = CASE_BY_ID["demo-bond-huachen"]
    root = tmp_path_factory.mktemp("tables-huachen")
    materials = generate_case_materials(root, case)
    gold = case_table_gold(case, materials)
    extracted = []
    for material in materials:
        extracted.extend(parse_document(material["path"]).tables)
    return case, materials, gold, extracted


def test_normalize_cell_distinguishes_zero_from_not_applicable() -> None:
    """``-`` 是零，``—`` 是无数据，空白是空单元格——三者不能混为一谈。"""
    assert normalize_cell("1,234") == ("number", Decimal("1234"))
    assert normalize_cell("-") == ("zero", Decimal("0"))
    assert normalize_cell("—") == ("not_applicable", None)
    assert normalize_cell("") == ("blank", None)
    assert normalize_cell("资产：") == ("text", None)


def test_flagship_tables_match_gold_exactly(huachen) -> None:
    """华辰：表召回、单元格、表名、单位、跨页关系全部命中，且无待人工确认。"""
    _, _, gold, extracted = huachen
    result = compare_with_gold(extracted, gold).summary()
    assert result["expected_tables"] == 11
    assert result["matched_tables"] == 11
    assert result["table_recall"] == 1.0
    assert result["cell_accuracy"] == 1.0
    assert result["cell_total"] >= 330
    assert result["caption_matches"] == 11
    assert result["unit_matches"] == 11
    assert result["continuation_found"] == result["continuation_expected"] == 1
    assert result["needs_review"] == 0
    assert result["unmatched"] == []


@pytest.mark.parametrize("short", [case.short for case in CASE_SPECS])
def test_every_fictional_case_matches_gold(short: str, tmp_path) -> None:
    case = CASE_BY_ID[f"demo-bond-{short}"]
    materials = generate_case_materials(tmp_path, case)
    extracted = []
    for material in materials:
        extracted.extend(parse_document(material["path"]).tables)
    result = compare_with_gold(extracted, case_table_gold(case, materials)).summary()
    assert result["table_recall"] == 1.0, result
    assert result["cell_accuracy"] == 1.0, result
    assert result["unmatched"] == []


def test_continuation_pages_merge_into_one_logical_table(huachen) -> None:
    """跨页续表合并为一张逻辑表：行数累加、页码范围覆盖两页、表头只算一次。"""
    case, _, gold, extracted = huachen
    declared = next(
        item["expected"] for item in gold
        if item["annotation_type"] == "table" and item["expected"]["caption"] == "合并资产负债表"
    )
    found = next(item for item in extracted if item.caption == "合并资产负债表")
    assert len(declared["pages"]) == 2, "金标本身就应当跨页"
    assert found.pages == declared["pages"], f"续表未合并：{found.pages}"
    assert found.row_count == declared["row_count"]
    assert found.columns == declared["columns"]


def test_same_page_tables_are_not_merged(huachen) -> None:
    """同页两张表各有自己的表名前导块，不得被并成一张。"""
    _, _, _, extracted = huachen
    same_page = [item for item in extracted if item.page == 36]
    assert {item.caption for item in same_page} == {"货币资金明细", "其他应收款明细"}


def test_cell_coordinates_and_pages_are_recorded(huachen) -> None:
    """逐格可追溯：每个单元格都要有页码与坐标，且坐标落在页面内。"""
    _, _, _, extracted = huachen
    for table in extracted:
        assert table.pages, table.table_key
        assert len(table.pages) == len({cell.page for cell in table.cells()})
        for cell in table.cells():
            assert cell.page in table.pages
            x0, y0, x1, y1 = cell.bbox
            assert 0 <= x0 < x1 <= 595 and 0 <= y0 < y1 <= 842


def test_table_notes_are_kept_separate_from_data_rows(huachen) -> None:
    """表注单独保留，不得混进数据行；金标声明有表注的表，抽取也必须拿到。"""
    _, _, gold, extracted = huachen
    documented = {
        item["expected"]["table_id"] for item in gold if item["annotation_type"] == "table_note"
    }
    assert documented, "金标应当包含表注"
    by_caption = {table.caption: table for table in extracted}
    for item in gold:
        if item["annotation_type"] != "table_note":
            continue
        caption = next(
            entry["expected"]["caption"] for entry in gold
            if entry["annotation_type"] == "table" and entry["expected"]["table_id"] == item["expected"]["table_id"]
        )
        assert by_caption[caption].notes, f"{caption} 丢失表注"
    for table in extracted:
        for row in table.rows:
            assert not any(cell.text.startswith("注：") for cell in row), table.table_key


def test_tampered_cell_is_detected(huachen) -> None:
    """负例：改一个单元格的数值，比对必须报出差异——否则 100% 是空转。"""
    _, _, gold, extracted = huachen
    baseline = compare_with_gold(extracted, gold).cell_accuracy
    assert baseline == 1.0

    tampered = copy.deepcopy(extracted)
    target = next(item for item in tampered if item.caption == "合并资产负债表")
    cell = next(item for item in target.rows[2] if item.column == 1)
    original = cell.text
    cell.text = "9,999"
    result = compare_with_gold(tampered, gold)
    assert result.cell_accuracy < baseline
    assert result.cell_exact == result.cell_total - 1
    cell.text = original
    assert compare_with_gold(tampered, gold).cell_accuracy == 1.0


def test_changed_year_column_is_detected(huachen) -> None:
    """负例：把年度列标签换掉，表结构比对必须失败（表头是年度口径的唯一来源）。"""
    _, _, gold, extracted = huachen
    tampered = copy.deepcopy(extracted)
    target = next(item for item in tampered if item.caption == "合并资产负债表")
    target.columns = ["项目", "2026 年末", "2024 年末", "2023 年末"]
    result = compare_with_gold(tampered, gold).summary()
    # 换年后这张表不再是「同一张表」：年度是口径的唯一来源，必须判为未匹配，
    # 而不是「匹配上了但值不同」。
    assert result["matched_tables"] == result["expected_tables"] - 1
    assert "huachen-prospectus-balance-sheet" in result["unmatched"]
    assert result["cell_accuracy"] < 1.0


def test_cross_case_values_are_detected(huachen, tmp_path) -> None:
    """负例：跨主体引用。

    表名与页位在不同发行人之间可能重合（例如两家都有「评级采用的主要财务指标」
    位于第 4 页），因此**不能**只靠「匹配数为 0」判断跨主体——真正的守卫是数值层：
    拿别家的抽取结果去比对，单元格几乎全错。
    """
    _, _, _, huachen_extracted = huachen
    other = CASE_BY_ID["demo-bond-ancheng"]
    other_gold = case_table_gold(other, generate_case_materials(tmp_path, other))
    result = compare_with_gold(huachen_extracted, other_gold).summary()
    assert result["cell_accuracy"] < 0.2, result
    assert result["unmatched"], "至少应当有表因数值/行数不符而未能匹配"


def test_gold_is_not_derived_from_the_parser(huachen) -> None:
    """金标只描述材料应当是什么样，不含解析器的编号。

    金标一旦引用 ``table_key``（解析器自己的页码编号），准确率就变成自证。
    """
    _, _, gold, _ = huachen
    for item in gold:
        expected = item["expected"]
        assert not str(expected.get("table_id", "")).startswith("p0"), expected
        assert expected.get("parser_version", TABLE_PARSER_VERSION) == TABLE_PARSER_VERSION


@pytest.mark.parametrize("filename", [
    "demo-北投/募集说明书.pdf",
    "demo-北投/信用评级报告.pdf",
    "real-bond-zhongyou-2026-02/中邮03募集说明书.pdf",
    "real-bond-zhongyou-2026-02/中邮03信用评级报告.pdf",
])
def test_reference_materials_locate_tables_without_scoring(filename: str) -> None:
    """中邮、北投只做版式兼容：能定位到表格即可，不计入单元格准确率。

    实测发现两份评级报告的表格**没有表名前导块**（表直接跟在页眉或节标题之后），
    因此解析器把它们标为待人工确认而不是编造表名——这正是 Gold 分层要区分的情况。
    """
    path = settings.resolved_data_dir / "demo_materials" / filename
    if not path.exists():
        pytest.skip(f"参考材料缺失：{filename}")
    tables = extract_tables(path)
    assert len(tables) >= 10, f"{filename} 未定位到表格"
    assert all(table.row_count >= 0 and table.column_count >= 2 for table in tables)
    assert all(table.pages for table in tables)
