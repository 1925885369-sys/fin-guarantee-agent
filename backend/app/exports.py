from __future__ import annotations

import re
from io import BytesIO

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .database import json_load, row, rows
from .facts import material_inventory
from .report_blocks import TABLE_CAPTION_RE, render_table_block_markdown, section_blocks
from .report_outline import cn_number, grouped_sections
from .report_delivery import review_notice
from .review import unresolved_items
from .review_lifecycle import report_state
from .table_block import table_block_from_dict

# ---------------------------------------------------------------------------
# Word typography spec (通用机构报告规范).  Single source of truth for the
# exported layout; edit here rather than in individual helpers.
# ---------------------------------------------------------------------------
BLACK = RGBColor(0, 0, 0)
TYPO = {
    # 正文：宋体 四号(14pt)、行距**固定值 24pt**、首行缩进 2 字符
    # （固定值以 Pt 传入 paragraph_format.line_spacing，python-docx 会写成
    #  w:line="480" w:lineRule="exact"，而不是 1.5 倍这类倍数行距）
    "body_font": "宋体",
    "body_size": 14,
    "body_line_spacing_pt": 24,
    "body_space_after_pt": 6,
    # 标题层级：分部与二级章节黑体三号(16) / 正文小标题黑体四号(14) / 更深标题同四号，全黑
    "heading_font": "黑体",
    "title_size": 22,
    "h1_size": 16,
    "h2_size": 16,
    "h3_size": 14,
    "heading_color": BLACK,
    "heading_space_before_pt": (18, 12, 6, 6),
    # 表格与页眉页脚：表头黑体、单元格宋体、同用五号(10.5)
    "table_font": "宋体",
    "table_header_font": "黑体",
    "table_size": 10.5,
    # 表格段落：显式固定改动前的行距／段后／首行缩进。
    # 样式优先级是 段落样式 > 表格样式（Word 文档默认 → 表格样式 → 编号 → 段落 →
    # 字符 → 直接格式），所以单元格段落实际继承的是 Normal。正文默认样式一旦改成
    # 固定 24pt、首行缩进按 14pt 算，表格就会跟着变高变缩进——这三个值把表格按在
    # 改动前的样子上（缩进 24pt 即原先 12pt × 2 字符）。
    "table_line_spacing": 1.5,
    "table_space_after_pt": 6,
    "table_first_line_indent_pt": 24,
    "header_footer_font": "宋体",
    "header_footer_size": 9,
    # 西文/数字统一映射（中文文档常规做法）
    "latin_font": "Times New Roman",
}


def _product_titles(project_id: str, sections: list[dict]) -> list[dict]:
    """章节标题也走成品口径（见 ``delivery_wording``）。

    正文在写入时已规范化，标题却是 ``report_sections`` 里的一列、不经过正文写入
    入口，因此库里仍可能是「评级报告复核及评级差异」这类禁用表述——不处理就会原样
    印进成品的目录与章标题。
    """
    from .delivery_wording import material_numbers, normalize_report_text

    numbers = material_numbers(project_id)
    return [
        {**section, "title": normalize_report_text(str(section.get("title") or ""), numbers)}
        for section in sections
    ]


def _open_items_text(project_id: str) -> tuple[bool, list[str]]:
    unresolved = unresolved_items(project_id)
    lines = [f"事实冲突：{item.get('name', item.get('title', '未命名事项'))}" for item in unresolved["conflicts"]]
    lines += [f"待确认候选风险：{item['title']}（{item['severity']}）" for item in unresolved["candidates"]]
    lines += [f"开放行动项：{item['title']}（{item['priority']}）" for item in unresolved["tasks"]]
    return unresolved["has_open_major_items"], lines


def _report_level(project_id: str, project: dict) -> str:
    if project.get("project_type") != "bond_guarantee":
        return "完整尽调"
    inventory = material_inventory(project_id)
    return "完整尽调" if inventory["complete_due_diligence_ready"] else "材料不足"


def _report_title(project: dict) -> str:
    if project.get("project_type") == "bond_guarantee":
        return f"{project.get('bond_name') or project['company_name']}债券发行融资担保尽调报告"
    return f"{project['company_name']}融资担保尽调报告"


def _citation_records(project_id: str, sections: list[dict]) -> tuple[dict[str, int], list[dict]]:
    """Build one project-wide source index in order of first in-body reference."""
    numbers: dict[str, int] = {}
    records: list[dict] = []
    for section in sections:
        for citation_id in json_load(section.get("citation_ids", "[]")):
            if citation_id in numbers:
                continue
            citation = row("SELECT * FROM citations WHERE id=? AND project_id=?", (citation_id, project_id))
            if not citation:
                continue
            numbers[citation_id] = len(records) + 1
            records.append(citation)
    return numbers, records


def _material_label(citation: dict) -> str:
    """「来源文件」列：成品口径的材料名，**不带页码**。

    直接写 ``citation["label"]`` 会把原件名带进成品（``募集说明书（申报稿）.pdf``）——
    那是工作台才该显示的文件名。优先用 ``source_reference`` 的成品口径
    （``发行文件〔资料01〕``），回退时才用规范化后的 label。
    """
    from .delivery_wording import PAGE_REFERENCE_PATTERN, normalize_report_text
    from .report_tables import source_reference

    reference = source_reference(str(citation.get("source_id") or ""))
    if reference:
        return re.sub(PAGE_REFERENCE_PATTERN, "", reference).strip()
    return normalize_report_text(str(citation.get("label") or "来源文件").strip(), {})


def _source_label(citation: dict, short: bool = False) -> str:
    """来源标签：成品口径的材料定位。

    导出件与报告正文必须用同一套用词（见 ``PLAN3.md`` §3）：优先渲染成
    「发行文件〔资料01〕第 29 页」，回退时才用 citation 自带的 label，
    并且同样去掉禁用表述——否则导出件里会出现「募集说明书（申报稿）.pdf」
    这种只在工作台才该显示的原件名。
    """
    from .report_tables import source_reference

    reference = source_reference(str(citation.get("source_id") or ""))
    if reference:
        return reference
    label = _material_label(citation)
    locator = str(citation.get("locator") or "定位待补充").strip()
    return f"{label}，{locator}" if short else f"{label}（{locator}）"



def _short_quote(value: str, limit: int = 220) -> str:
    normalized = re.sub(r"\s+", " ", value or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "…"


def _section_source_line(section: dict, citation_numbers: dict[str, int], records_by_id: dict[str, dict]) -> str:
    items = []
    for citation_id in json_load(section.get("citation_ids", "[]")):
        citation = records_by_id.get(citation_id)
        number = citation_numbers.get(citation_id)
        if citation and number:
            items.append(f"[{number}] {_source_label(citation, short=True)}")
    return "本章来源：" + "；".join(items) if items else "本章来源：材料不足，暂无可定位证据。"


def _markdown_evidence_index(records: list[dict], citation_numbers: dict[str, int], material_labels: dict[str, str]) -> list[str]:
    if not records:
        return ["材料不足，暂无可定位证据。"]
    from .delivery_wording import normalize_report_text

    lines: list[str] = []
    for citation in records:
        number = citation_numbers[citation["id"]]
        raw_quote = normalize_report_text(str(citation.get("quote") or ""), material_labels)
        quote = _short_quote(raw_quote) or "原文摘录为空。"
        lines.append(f"{number}. {_source_label(citation)}：{quote}")
    return lines


def _blocks_markdown(blocks: list[dict]) -> str:
    """把 ``report_blocks.section_blocks`` 的分块渲染成 Markdown。

    系统表格块**从 ``TableBlock`` 快照渲染**（单元格已做转义），不再回读正文里的
    Markdown 表格文本——网页、Markdown、Word 因此共用同一份快照，标题、数值与
    证据只有一个版本。正文分块（标题／列表／引用／段落）的渲染规则与
    ``report_outline.markdown_blocks`` 保持一致（章标题下一级）。
    """
    output: list[str] = []
    table_index = 0
    for block in blocks:
        kind = str(block.get("kind") or "")
        if kind == "table":
            if "table" in block:
                table_index += 1
                output.append(render_table_block_markdown(
                    table_block_from_dict(block["table"]), index=table_index, total=table_index,
                ))
            else:
                output.append("\n".join(block.get("rows") or []))
            continue
        if kind == "heading":
            output.append(f"{'#' * (block['level'] + 1)} {block['label']}")
        elif kind == "list":
            if block.get("ordered"):
                output.append("\n".join(f"{index}. {item}" for index, item in enumerate(block["items"], start=1)))
            else:
                output.append("\n".join(f"- {item}" for item in block["items"]))
        elif kind == "quote":
            output.append(f"> {block['text']}")
        else:
            output.append(str(block.get("text") or ""))
    return "\n\n".join(output)


def markdown_report(project_id: str, variant: str = "draft") -> str:
    project = row("SELECT * FROM projects WHERE id=?", (project_id,))
    sections = _product_titles(
        project_id, rows("SELECT * FROM report_sections WHERE project_id=? ORDER BY rowid", (project_id,)),
    )
    subject = str(project.get("company_name") or "")
    has_open, open_lines = _open_items_text(project_id)
    title = _report_title(project)
    state = report_state(project_id)
    citation_numbers, records = _citation_records(project_id, sections)
    records_by_id = {item["id"]: item for item in records}
    from .delivery_wording import material_numbers
    material_labels = material_numbers(project_id)

    output = [f"# {title}", "", f"> **报告级别：{_report_level(project_id, project)}**", ""]
    notice = review_notice(project_id) if variant == "draft" else ""
    if notice:
        # 草稿可导出，但必须带待人工复核警示与问题摘要（交付一致性方案 §三.3）
        output.extend([f"> **{notice}**", ""])
    if has_open:
        output.extend(["> **存在未决重大事项：本报告包含尚未解决的事实冲突、高风险候选项或高优先级行动项。**", ""])
    if variant == "draft" or state["report_stale"]:
        output.extend(["> **草稿标识：本文件尚未形成最终已复核版本，最新复核决议可能尚未应用至报告。**", ""])
    output.extend(["> 本报告由尽调辅助 Agent 生成，需经人工复核，不构成自动审批意见。", ""])

    # 论文式层级：一级 `## 一、分部`、二级 `### （一）章`、三级 `#### 1. 分块`。
    # 编号在此按层级计算，不写入正文，避免模型改写或章节重生成后编号丢失。
    groups = grouped_sections(sections)
    for group in groups:
        output.extend([f"## {group['label']}", ""])
        for chapter in group["chapters"]:
            section = chapter["section"]
            output.extend([f"### {chapter['label']}", "",
                           _blocks_markdown(section_blocks(project_id, section, subject=subject)), "",
                           f"> {_section_source_line(section, citation_numbers, records_by_id)}", ""])
        fallback = group.get("section")
        if fallback is not None:
            output.extend([_blocks_markdown(section_blocks(project_id, fallback, subject=subject)), "",
                           f"> {_section_source_line(fallback, citation_numbers, records_by_id)}", ""])
    if open_lines:
        output.extend([f"## {cn_number(len(groups) + 1)}、未决事项附录", "", *[f"- {line}" for line in open_lines], ""])
    output.extend([f"## {cn_number(len(groups) + 2)}、附录：证据索引", "", *_markdown_evidence_index(records, citation_numbers, material_labels), ""])
    return "\n".join(output)


def _set_east_asia_font(element, font_name: str, latin: str | None = None) -> None:
    r_pr = element.get_or_add_rPr()
    r_fonts = r_pr.rFonts
    if r_fonts is None:
        r_fonts = OxmlElement("w:rFonts")
        r_pr.insert(0, r_fonts)
    r_fonts.set(qn("w:eastAsia"), font_name)
    latin = latin or str(TYPO["latin_font"])
    r_fonts.set(qn("w:ascii"), latin)
    r_fonts.set(qn("w:hAnsi"), latin)


def _configure_style(style, font_name: str, size: float, *, bold: bool = False,
                     color: RGBColor | None = None, first_line_indent: bool = False,
                     line_spacing=None) -> None:
    """样式级排版。``line_spacing`` 传 ``Pt`` 即固定行距，不传沿用 1.5 倍。"""
    style.font.name = font_name
    style.font.size = Pt(size)
    style.font.bold = bold
    if color:
        style.font.color.rgb = color
    _set_east_asia_font(style._element, font_name)
    style.paragraph_format.space_after = Pt(8)
    style.paragraph_format.line_spacing = 1.5 if line_spacing is None else line_spacing
    if first_line_indent:
        style.paragraph_format.first_line_indent = Pt(size * 2)


def _add_heading_text(document: Document, text: str, level: int):
    """写标题：编号与名称连续输出，不插入制表符或空白填充。

    编号在 ``report_outline`` 里已经拼进 label（``一、分部``／``（一）章节``），
    这里只把异常空白折叠掉——标题任何位置出现制表符或连续空白，都会在 Word 里
    把编号和名称撑开，破坏「编号紧邻名称」的体例。
    """
    return document.add_heading(re.sub(r"\s+", " ", str(text or "")).strip(), level=level)


def _add_page_number(paragraph) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_after = Pt(0)
    run = paragraph.add_run("第 ")
    run.font.size = Pt(TYPO["header_footer_size"])
    run.font.color.rgb = BLACK
    _set_east_asia_font(run._element, TYPO["header_footer_font"])
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instruction)
    run._r.append(end)
    tail = paragraph.add_run(" 页")
    tail.font.size = Pt(TYPO["header_footer_size"])
    tail.font.color.rgb = BLACK
    _set_east_asia_font(tail._element, TYPO["header_footer_font"])


def _header_text(project: dict) -> str:
    if project.get("project_type") == "bond_guarantee":
        return "债券发行融资担保尽调报告"
    return "融资担保尽调报告"


def _configure_document(document: Document, project: dict) -> None:
    """Apply the typography spec at style level; runs are enforced by
    ``_enforce_typography`` afterwards so no built-in Word default (blue
    heading colour, Arial/Calibri latin fallback) can leak through."""
    section = document.sections[0]
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(2.7)
    section.right_margin = Cm(2.7)

    body_font, body_size = TYPO["body_font"], TYPO["body_size"]
    heading_font = TYPO["heading_font"]
    body_spacing = Pt(TYPO["body_line_spacing_pt"])
    _configure_style(document.styles["Normal"], body_font, body_size, color=BLACK,
                     first_line_indent=True, line_spacing=body_spacing)
    document.styles["Normal"].paragraph_format.space_after = Pt(TYPO["body_space_after_pt"])

    _configure_style(document.styles["Title"], heading_font, TYPO["title_size"], bold=True, color=BLACK)
    headings = (
        ("Heading 1", TYPO["h1_size"], 0),
        ("Heading 2", TYPO["h2_size"], 1),
        ("Heading 3", TYPO["h3_size"], 2),
        ("Heading 4", TYPO["h3_size"], 3),
    )
    for name, size, index in headings:
        # 标题不传 line_spacing：沿用 1.5 倍，改动只落在字号上。
        _configure_style(document.styles[name], heading_font, size, bold=True, color=BLACK)
        style = document.styles[name]
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.space_before = Pt(TYPO["heading_space_before_pt"][index])
        style.paragraph_format.space_after = Pt(6)
        style.paragraph_format.first_line_indent = Pt(0)
    # 正文（段落／列表／引用）统一固定行距 24pt；标题与表注不在此列。
    _configure_style(document.styles["Quote"], body_font, body_size,
                     color=RGBColor(82, 94, 110), line_spacing=body_spacing)
    document.styles["Quote"].paragraph_format.first_line_indent = Pt(0)
    _configure_style(document.styles["List Bullet"], body_font, body_size, line_spacing=body_spacing)
    _configure_style(document.styles["List Number"], body_font, body_size, line_spacing=body_spacing)
    try:
        source_style = document.styles["Source Note"]
    except KeyError:
        source_style = document.styles.add_style("Source Note", WD_STYLE_TYPE.PARAGRAPH)
        source_style.base_style = document.styles["Normal"]
    _configure_style(source_style, body_font, 10.5, color=RGBColor(82, 94, 110))
    source_style.paragraph_format.first_line_indent = Pt(0)
    source_style.paragraph_format.space_before = Pt(4)
    source_style.paragraph_format.space_after = Pt(10)

    header = section.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.CENTER
    header.paragraph_format.space_after = Pt(0)
    header_run = header.add_run(_header_text(project))
    header_run.font.size = Pt(TYPO["header_footer_size"])
    header_run.font.color.rgb = BLACK
    _set_east_asia_font(header_run._element, TYPO["header_footer_font"])
    _add_page_number(section.footer.paragraphs[0])


def _enforce_typography(document: Document) -> None:
    """Run-level enforcement so exported text cannot inherit template defaults.

    Every run is given the spec font/size according to its paragraph style;
    headings are forced pure black and never keep the built-in blue theme
    colour.  Runs that already carry an explicit colour (red warnings, grey
    notes) are left untouched.
    """
    heading_sizes = {
        "Title": TYPO["title_size"],
        "Heading 1": TYPO["h1_size"],
        "Heading 2": TYPO["h2_size"],
        "Heading 3": TYPO["h3_size"],
        "Heading 4": TYPO["h3_size"],
    }
    for paragraph in document.paragraphs:
        style_name = paragraph.style.name
        for run in paragraph.runs:
            element = run._element
            if run.font.color.rgb is None:
                run.font.color.rgb = BLACK
            if run.font.size is not None:
                # Runs with an explicitly designed size (cover subtitle, red
                # cover warning) keep their layout; only ensure they are black
                # unless already coloured.
                continue
            if style_name == "Title":
                size, font = TYPO["title_size"], TYPO["heading_font"]
            elif style_name.startswith("Heading"):
                size = heading_sizes.get(style_name, TYPO["h3_size"])
                font = TYPO["heading_font"]
            elif style_name in ("List Bullet", "List Number"):
                size, font = TYPO["body_size"], TYPO["body_font"]
            elif style_name in ("Quote", "Source Note"):
                continue  # keep their grey style-level appearance
            else:
                size, font = TYPO["body_size"], TYPO["body_font"]
            run.font.size = Pt(size)
            _set_east_asia_font(element, font)

    table_size = TYPO["table_size"]
    table_spacing = TYPO["table_line_spacing"]
    table_space_after = Pt(TYPO["table_space_after_pt"])
    table_indent = Pt(TYPO["table_first_line_indent_pt"])
    for table in document.tables:
        for row_index, row in enumerate(table.rows):
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    # 单元格段落继承的是 Normal（段落样式优先级高于表格样式），正文改成
                    # 固定 24pt 后会连表格一起改。这里把行距／段后／缩进显式写回改动前的
                    # 值，表格外观与正文默认样式的调整解耦。
                    paragraph.paragraph_format.line_spacing = table_spacing
                    paragraph.paragraph_format.space_after = table_space_after
                    paragraph.paragraph_format.first_line_indent = table_indent
                    for run in paragraph.runs:
                        run.font.size = Pt(table_size)
                        if run.font.color.rgb is None:
                            run.font.color.rgb = BLACK
                        if row_index == 0:
                            run.font.bold = True
                            _set_east_asia_font(run._element, TYPO["table_header_font"])
                        else:
                            _set_east_asia_font(run._element, TYPO["table_font"])


def _add_inline_markdown(paragraph, text: str) -> None:
    for part in re.split(r"(\*\*.+?\*\*)", text):
        if not part:
            continue
        bold = part.startswith("**") and part.endswith("**")
        run = paragraph.add_run(part[2:-2] if bold else part)
        run.bold = bold
        _set_east_asia_font(run._element, "宋体")


def _repeat_header_row(table) -> None:
    """标记首行为标题行：长表格跨页时 Word 自动重复表头。

    不标记的话，系统数据表在第 2 页起只剩裸数字——读者无法判断列是哪个年度，
    跨页表格也就不可读。
    """
    row = table.rows[0]
    properties = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    properties.append(header)


def _add_markdown_table(document: Document, lines: list[str]) -> None:
    parsed = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]
    if len(parsed) < 2:
        return
    data = [cells for index, cells in enumerate(parsed) if index != 1 or not all(re.fullmatch(r"[-: ]+", cell) for cell in cells)]
    if not data:
        return
    width = max(len(item) for item in data)
    table = document.add_table(rows=1, cols=width)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for column, value in enumerate(data[0]):
        cell = table.rows[0].cells[column]
        cell.text = value
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        for run in cell.paragraphs[0].runs:
            run.bold = True
            _set_east_asia_font(run._element, "黑体")
    for values in data[1:]:
        cells = table.add_row().cells
        for column, value in enumerate(values):
            cells[column].text = value
            cells[column].vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            for run in cells[column].paragraphs[0].runs:
                _set_east_asia_font(run._element, "宋体")
    _repeat_header_row(table)


def _word_cell_text(text: str) -> str:
    """Word 单元格文本规整：折叠换行，避免在格内制造额外段落。

    Word 单元格是独立容器，竖线不会像 Markdown 那样切列；但仍统一折叠空白，
    以免同一格里的原文换行把表格撑变形。
    """
    return re.sub(r"\n\s*", " ", str(text or "")).strip()


def _add_table_block(document: Document, block: dict, *, index: int) -> None:
    """把一个系统表格块写成 Word **原生表格**，直接取自 ``TableBlock`` 快照。

    - 保留**全部行**（分组行／小计行／``未披露`` 行），改一行就是改数据，不截断；
    - 表名行带期间列名对应的单位／口径／来源，与网页、Markdown 同版；
    - 保留表注与校验提示，表头跨页重复；
    - **绝不**先把正文里的 Markdown 表格反解成文本行——那条路会把渲染版式当数据契约。
    """
    from .report_tables import _cell_text, render_table_block

    source = table_block_from_dict(block["table"])
    caption = render_table_block(source, index=index, total=index).splitlines()[0]
    paragraph = document.add_paragraph()
    _add_inline_markdown(paragraph, caption)
    # 表名与表体不分页：标题落在上一页页脚时，表格就失去了自己的名字。
    paragraph.paragraph_format.keep_with_next = True

    table = document.add_table(rows=1, cols=len(source.columns))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for column_index, column in enumerate(source.columns):
        cell = table.rows[0].cells[column_index]
        cell.text = _word_cell_text(column.label)
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        for run in cell.paragraphs[0].runs:
            run.bold = True
            _set_east_asia_font(run._element, "黑体")
    for row in source.rows:
        cells = table.add_row().cells
        for column_index, value in enumerate(row.cells):
            cells[column_index].text = _word_cell_text(_cell_text(value))
            cells[column_index].vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            for run in cells[column_index].paragraphs[0].runs:
                _set_east_asia_font(run._element, "宋体")
    _repeat_header_row(table)
    for note in source.notes:
        _add_source_note(document, note)
    if source.validation_status != "passed":
        warning = document.add_paragraph(style="Quote")
        _add_inline_markdown(warning, f"本表校验状态：{source.validation_status}，需人工确认后方可作为数据使用。")


def _add_section_blocks(document: Document, blocks: list[dict]) -> None:
    """按 ``report_blocks.section_blocks`` 的顺序写入章内分块与系统表格。

    系统表格块（``kind == "table"`` 且带 ``table`` 快照）走 Word 原生表格；
    正文里遗留的、非系统的 Markdown 表格仍按文本表格渲染。层级由
    ``report_outline`` 统一计算，模型输出的更深标记不会升到章标题之上。
    """
    table_index = 0
    for block in blocks:
        kind = str(block.get("kind") or "")
        if kind == "table":
            if "table" in block:
                table_index += 1
                _add_table_block(document, block, index=table_index)
            else:
                _add_markdown_table(document, block.get("rows") or [])
            continue
        if kind == "heading":
            _add_heading_text(document, block["label"], int(block["level"]))
            continue
        if kind == "list":
            style = "List Number" if block.get("ordered") else "List Bullet"
            for item in block["items"]:
                paragraph = document.add_paragraph(style=style)
                _add_inline_markdown(paragraph, item)
            continue
        if kind == "quote":
            paragraph = document.add_paragraph(style="Quote")
            _add_inline_markdown(paragraph, str(block.get("text") or ""))
            continue
        paragraph = document.add_paragraph()
        _add_inline_markdown(paragraph, str(block.get("text") or ""))
        if TABLE_CAPTION_RE.match(str(block.get("text") or "").strip()):
            # 表名与表体不分页：标题落在上一页页脚时，表格就失去了自己的名字。
            paragraph.paragraph_format.keep_with_next = True


def _add_source_note(document: Document, text: str) -> None:
    paragraph = document.add_paragraph(style="Source Note")
    _add_inline_markdown(paragraph, text)


def _add_cover(document: Document, project: dict, title: str, level: str, has_open: bool,
               *, variant: str, state: dict) -> None:
    document.add_paragraph()
    heading = _add_heading_text(document, title, 0)
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle = document.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = subtitle.add_run("债券发行融资担保尽调报告")
    run.font.size = Pt(16)
    run.bold = True
    _set_east_asia_font(run._element, "黑体")

    document.add_paragraph()
    table = document.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    details = (
        ("发行人", str(project.get("company_name") or "")),
        ("报告级别", level),
        ("生成依据", "项目材料、主体档案快照及确定性核验结果"),
        ("报告版本时间", str(project.get("updated_at") or "")),
    )
    for label, value in details:
        cells = table.add_row().cells
        cells[0].text = label
        cells[1].text = value
        for cell_index, cell in enumerate(cells):
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            for run in cell.paragraphs[0].runs:
                run.bold = cell_index == 0
                _set_east_asia_font(run._element, "黑体" if cell_index == 0 else "宋体")
    document.add_paragraph()
    disclaimer = document.add_paragraph(style="Quote")
    _add_inline_markdown(disclaimer, "本报告由尽调辅助 Agent 生成，仅呈示现有材料、确定性核验结果及待处理事项，需经人工复核，不构成自动审批、承保、评级或投资建议。")
    if variant == "draft" or state.get("report_stale"):
        draft_warning = document.add_paragraph(style="Quote")
        draft_run = draft_warning.add_run("草稿文件：尚未形成最终已复核版本，请结合未决事项和最新复核修订使用。")
        draft_run.bold = True
        draft_run.font.color.rgb = RGBColor(190, 55, 55)
        _set_east_asia_font(draft_run._element, "宋体")
    if has_open:
        warning = document.add_paragraph(style="Quote")
        warning_run = warning.add_run("存在未决重大事项：请在事实冲突、高风险候选项或高优先级行动项关闭前，将本报告作为复核草稿使用。")
        warning_run.bold = True
        warning_run.font.color.rgb = RGBColor(190, 55, 55)
        _set_east_asia_font(warning_run._element, "宋体")
    document.add_page_break()


def _add_contents(document: Document, sections: list[dict]) -> None:
    _add_heading_text(document, "报告目录", 1)
    for group in grouped_sections(sections):
        paragraph = document.add_paragraph()
        run = paragraph.add_run(group["label"])
        run.bold = True
        _set_east_asia_font(run._element, "黑体")
        for chapter in group["chapters"]:
            entry = document.add_paragraph()
            entry.paragraph_format.left_indent = Cm(0.6)
            _add_inline_markdown(entry, chapter["label"])
        fallback = group.get("section")
        if fallback is not None:
            entry = document.add_paragraph()
            entry.paragraph_format.left_indent = Cm(0.6)
            _add_inline_markdown(entry, fallback["title"])
    document.add_page_break()


def _add_evidence_appendix(document: Document, records: list[dict], citation_numbers: dict[str, int],
                           material_labels: dict[str, str], heading: str = "附录：证据索引") -> None:
    from .delivery_wording import normalize_report_text

    document.add_page_break()
    _add_heading_text(document, heading, 1)
    intro = document.add_paragraph()
    _add_inline_markdown(intro, "本附录集中列示正文实际引用的证据定位及短摘录。完整原文仍以系统证据抽屉及原始文件为准。")
    if not records:
        document.add_paragraph("材料不足，暂无可定位证据。")
        return
    table = document.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    headers = ("序号", "来源文件", "定位", "支持摘录")
    for index, label in enumerate(headers):
        cell = table.rows[0].cells[index]
        cell.text = label
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        for run in cell.paragraphs[0].runs:
            run.bold = True
            _set_east_asia_font(run._element, "黑体")
    for citation in records:
        cells = table.add_row().cells
        values = (
            str(citation_numbers[citation["id"]]),
            _material_label(citation),
            str(citation.get("locator") or "定位待补充"),
            _short_quote(normalize_report_text(str(citation.get("quote") or ""), material_labels)) or "原文摘录为空。",
        )
        for index, value in enumerate(values):
            cells[index].text = value
            cells[index].vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            for run in cells[index].paragraphs[0].runs:
                _set_east_asia_font(run._element, "宋体")


def docx_report(project_id: str, variant: str = "draft") -> bytes:
    project = row("SELECT * FROM projects WHERE id=?", (project_id,))
    sections = _product_titles(
        project_id, rows("SELECT * FROM report_sections WHERE project_id=? ORDER BY rowid", (project_id,)),
    )
    has_open, open_lines = _open_items_text(project_id)
    title = _report_title(project)
    level = _report_level(project_id, project)
    subject = str(project.get("company_name") or "")
    state = report_state(project_id)
    citation_numbers, records = _citation_records(project_id, sections)
    records_by_id = {item["id"]: item for item in records}
    from .delivery_wording import material_numbers
    material_labels = material_numbers(project_id)

    document = Document()
    _configure_document(document, project)
    _add_cover(document, project, title, level, has_open, variant=variant, state=state)
    notice = review_notice(project_id) if variant == "draft" else ""
    if notice:
        paragraph = document.add_paragraph(notice)
        paragraph.runs[0].bold = True
    _add_contents(document, sections)

    # 与 Markdown 出口共用同一套论文式编号：一级 一、二级 （一）三级 1.
    groups = grouped_sections(sections)
    for group in groups:
        _add_heading_text(document, group["label"], 1)
        for chapter in group["chapters"]:
            section = chapter["section"]
            _add_heading_text(document, chapter["label"], 2)
            _add_section_blocks(document, section_blocks(project_id, section, subject=subject))
            _add_source_note(document, _section_source_line(section, citation_numbers, records_by_id))
        fallback = group.get("section")
        if fallback is not None:
            _add_section_blocks(document, section_blocks(project_id, fallback, subject=subject))
            _add_source_note(document, _section_source_line(fallback, citation_numbers, records_by_id))
    if open_lines:
        _add_heading_text(document, f"{cn_number(len(groups) + 1)}、未决事项附录", 1)
        for line in open_lines:
            paragraph = document.add_paragraph(style="List Bullet")
            _add_inline_markdown(paragraph, line)
    _add_evidence_appendix(document, records, citation_numbers, material_labels, f"{cn_number(len(groups) + 2)}、附录：证据索引")
    _enforce_typography(document)

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()
