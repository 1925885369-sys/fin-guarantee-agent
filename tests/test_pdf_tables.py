"""PDF 财务表格渲染器的契约测试。

这组测试钉住 ``docs/financial-table-structure-reference.md`` 第 13 节对渲染器的硬要求：
表外三行前导块、分组行留白、合计行、稳定单元格标识、**跨页续表重复表头而不重复表名与单位行**、
以及「文字放不下时缩小或截断，绝不整格消失」。

旧渲染器（``_draw_table``）这些一条都不满足：等宽、单层表头、固定行高，且没有分页能力。
"""
from __future__ import annotations

import pytest
import fitz

from backend.app.bond_documents import CASE_BY_ID, CASE_SPECS, _page_tables
from backend.app.pdf_tables import CAPTION_HEIGHT, FinancialTable, PdfWriter, TableCell, render_table


def _writer(pages: int = 1) -> tuple[fitz.Document, PdfWriter]:
    doc = fitz.open()
    writer = PdfWriter(doc, decorate=lambda page, number: None, accent=(0.1, 0.28, 0.25))
    return doc, writer


def _simple_table(row_count: int) -> FinancialTable:
    rows = tuple(
        (
            TableCell(f"科目{index:02d}", "normal", "left"),
            TableCell(f"{1000 + index:,}", "normal", "right"),
        )
        for index in range(row_count)
    )
    return FinancialTable(
        table_id="t-demo", caption="演示表", unit="万元",
        columns=("项目", "2025 年末"), rows=rows, notes=("注：本表数据仅用于演示。",),
    )


def _render(table: FinancialTable) -> tuple[fitz.Document, object]:
    doc, writer = _writer()
    writer.start_section("第五节 发行人财务状况", "演示", cover=False)
    rendered = render_table(writer, table)
    return doc, rendered


def test_table_starts_with_caption_unit_then_header() -> None:
    doc, rendered = _render(_simple_table(3))
    lines = [line.strip() for line in doc[rendered.start_page - 1].get_text().split("\n") if line.strip()]
    assert "演示表" in lines
    assert "单位：万元" in lines
    assert lines.index("单位：万元") == lines.index("演示表") + 1, "单位行必须紧跟表名行"
    assert "项目" in lines, "单位行之后是表头首行"


def test_caption_and_cells_appear_exactly_once_in_text_layer() -> None:
    """材料要被逐格解析，文字层里每个数值只能出现一次。

    上一版用「同一位置画两遍」模拟加粗，PDF 文字层会出现重复内容，
    解析器会把单元格读成两遍。改用 TextWriter 的填充＋描边渲染模式后必须唯一。
    """
    doc, rendered = _render(_simple_table(5))
    text = doc[rendered.start_page - 1].get_text()
    assert text.count("演示表") == 1, "表名在文字层重复"
    assert text.count("科目00") == 1, "单元格文字在文字层重复"
    assert text.count("1,000") == 1, "数值在文字层重复"


def test_group_rows_leave_other_cells_empty() -> None:
    doc, writer = _writer()
    writer.start_section("t", "", cover=False)
    table = FinancialTable(
        table_id="t-group", caption="分组演示", unit="万元",
        columns=("项目", "2025 年末"),
        rows=((TableCell("资产：", "group", "left"), TableCell("", "group", "right")),),
    )
    rendered = render_table(writer, table)
    body_cells = [cell for cell in rendered.cells if cell.row == 1]
    assert body_cells[0].text == "资产："
    assert all(cell.text == "" for cell in body_cells[1:]), "分组行除首格外必须留白"


def test_subtotal_rows_are_marked() -> None:
    for case in CASE_SPECS:
        tables = _page_tables(case, "prospectus", "financial_2")
        assert tables, f"{case.short} 资产负债表缺失"
        kinds = {cell.kind for row in tables[0].rows for cell in row}
        assert "subtotal" in kinds, f"{case.short} 资产负债表没有合计行"
        assert "group" in kinds, f"{case.short} 资产负债表没有分组行"


def test_long_table_spills_and_repeats_header_only() -> None:
    """跨页续表：重复表头行，不重复表名与单位行。"""
    doc, rendered = _render(_simple_table(80))
    assert rendered.end_page > rendered.start_page, "80 行表应当跨页"
    assert rendered.header_repeat_pages, "续表页必须记录为表头重复页"

    first = doc[rendered.start_page - 1].get_text()
    assert "演示表" in first and "单位：万元" in first
    for page_no in rendered.header_repeat_pages:
        lines = [line.strip() for line in doc[page_no - 1].get_text().split("\n") if line.strip()]
        assert "项目" in lines and "2025 年末" in lines, f"第 {page_no} 页缺少重复表头"
        assert "演示表" not in lines, f"第 {page_no} 页不应重复表名"
        assert "单位：万元" not in lines, f"第 {page_no} 页不应重复单位行"


def test_cells_carry_stable_ids_and_coordinates() -> None:
    doc, rendered = _render(_simple_table(5))
    ids = [cell.cell_id for cell in rendered.cells]
    assert len(ids) == len(set(ids)), "单元格标识必须唯一"
    assert "t-demo:r00c00" in ids, "表头首格标识必须是 table_id:rRRcCC"
    for cell in rendered.cells:
        x0, y0, x1, y1 = cell.rect
        assert x1 > x0 and y1 > y0
        assert rendered.start_page <= cell.page <= rendered.end_page


def test_overflowing_text_shrinks_instead_of_disappearing() -> None:
    """窄列里的长文本必须仍然可见——旧渲染器会整格丢弃。"""
    long_text = "信用期内，按合同账期结算并已取得客户确认函"
    table = FinancialTable(
        table_id="t-narrow", caption="窄列演示", unit="万元",
        columns=("债务人名称", "是否为关联方", "期末余额", "未收回原因"),
        rows=((TableCell("某公司（虚构）", "normal", "left"), TableCell("否", "normal", "center"),
               TableCell("1,234", "normal", "right"), TableCell(long_text, "normal", "left")),),
    )
    doc, rendered = _render(table)
    page = doc[rendered.start_page - 1]
    extracted = page.get_text().replace("\n", "").replace(" ", "")
    assert "信用期内" in extracted, "窄列长文本被整格丢弃"
    assert "未收回原因" in page.get_text(), "表头不应因列窄而消失"


@pytest.mark.parametrize("short", [case.short for case in CASE_SPECS])
def test_every_case_produces_tables_on_declared_pages(short: str) -> None:
    case = CASE_BY_ID[f"demo-bond-{short}"]
    for key, expected in (
        ("financial_2", "资产负债表"), ("financial_3", "利润项目表"),
        ("financial_4", "现金流量项目表"), ("financial_7", "账龄"),
    ):
        tables = _page_tables(case, "prospectus", key)
        assert len(tables) == 1, f"{short} {key} 应有一张表"
        assert expected in tables[0].caption, f"{short} {key} 表名不符：{tables[0].caption}"


def test_balance_sheet_totals_match_the_model() -> None:
    """表内合计行数值必须等于模型值——渲染不能引入第二套数字。"""
    case = CASE_BY_ID["demo-bond-huachen"]
    table = _page_tables(case, "prospectus", "financial_2")[0]
    latest = case.statements[-1]
    by_label = {row[0].text: row[1:] for row in table.rows}
    assert by_label["资产总计"][0].text == f"{latest.balance_sheet.total_assets:,}"
    assert by_label["负债和所有者权益总计"][0].text == f"{latest.balance_sheet.total_liabilities + latest.balance_sheet.equity:,}"
    assert by_label["流动资产合计"][0].text == f"{latest.balance_sheet.current_assets:,}"
    assert by_label["负债合计"][0].text == f"{latest.balance_sheet.total_liabilities:,}"


def test_year_columns_are_newest_first() -> None:
    """参考材料的年度列是倒序，最新年度在最左。"""
    case = CASE_BY_ID["demo-bond-huachen"]
    table = _page_tables(case, "prospectus", "financial_2")[0]
    assert table.columns == ("项目", "2025 年末", "2024 年末", "2023 年末")


def test_zero_values_render_as_dash() -> None:
    from backend.app.pdf_tables import cell_value

    assert cell_value(0) == "-"
    assert cell_value(None) == "—"
    assert cell_value(-3240) == "-3,240"


def _parsed_source_rows(materials: list[dict], role: str) -> tuple[list[dict], dict]:
    material = next(item for item in materials if item["role"] == role)
    rows = [
        {
            "id": f"{role}-p{fragment.page:03d}", "page": fragment.page,
            "text": fragment.text, "document_id": f"material-{role}",
        }
        for fragment in material["parsed"].fragments
    ]
    return rows, material


def test_toc_page_never_anchors_document_sections(tmp_path) -> None:
    """目录页不能被当成章节起点。

    目录里逐行列出全部章节标题，若目录页没被识别为目录，页面上每个标题都会成为
    章节锚点，于是**所有章节都被定位到目录页**。实测后果：抽取按「该节允许的事实
    字段」白名单拒绝掉正文里的事实（例如发行条款页的本期发行规模），来源冲突随之
    消失，`bond-summary` 会静默采用单一数值。
    """
    from backend.app import bond_sections
    from backend.app.bond_documents import generate_case_materials

    case = CASE_BY_ID["demo-bond-huachen"]
    materials = generate_case_materials(tmp_path, case)
    rows, material = _parsed_source_rows(materials, "prospectus")
    toc_page = material["key_pages"]["toc"]

    located, _ = bond_sections.locate_document_sections(rows, "prospectus")
    starts = {item.role: item.start_page for item in located if item.start_page}
    assert starts, "未定位到任何章节"
    assert all(page != toc_page for page in starts.values()), f"章节被定位到目录页：{starts}"
    assert starts["offering_terms"] == material["key_pages"]["terms_1"]
    assert starts["financial_statements"] == material["key_pages"]["financial_1"]


def test_toc_lines_are_recognisable_as_table_of_contents(tmp_path) -> None:
    """目录行必须带引导符页码，否则缺少识别依据。"""
    from backend.app import bond_sections
    from backend.app.bond_documents import generate_case_materials

    materials = generate_case_materials(tmp_path, CASE_BY_ID["demo-bond-huachen"])
    rows, material = _parsed_source_rows(materials, "prospectus")
    toc_text = next(row["text"] for row in rows if row["page"] == material["key_pages"]["toc"])
    toc_lines = [line for line in toc_text.split("\n") if "节" in line]
    assert toc_lines, "目录页没有章节行"
    assert all(bond_sections._looks_like_toc_line(line) for line in toc_lines), toc_lines[:3]


def test_oversized_paragraph_raises_instead_of_being_silently_dropped() -> None:
    """放不下的段落必须报错，不能像旧实现那样被静默裁掉。"""
    doc, writer = _writer()
    writer.start_section("t", "", cover=False)
    with pytest.raises(ValueError, match="段落过长"):
        writer.paragraph("很长的段落。" * 4000)
