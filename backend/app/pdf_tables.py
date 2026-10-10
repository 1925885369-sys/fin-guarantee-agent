"""通用财务报表 PDF 渲染器。

旧的 ``bond_documents._draw_table`` 只能画等宽、单层表头、固定行高的表，并且
**没有分页能力**：表高超出页面时行会画到页面外，正文段落也被 ``min(y + height, 775)``
静默裁剪。它无法承载三张主表。

本模块提供三件东西：

1. ``FinancialTable``：与渲染无关的表格描述（表名、单位、表头、行、表注）；
   每个单元格有稳定标识 ``{table_id}:rRRcCC``，供金标与核对接口引用。
2. ``PdfWriter``：拥有分页能力的页面写入器。内容超出内容区时自动开新页，
   新页重绘章节标题与页脚。
3. ``render_table``：按参考材料的结构约定绘制表格——表外三行前导块
   （表名 / 单位 / 表头）、分组行首格有字其余留白、合计行加粗、
   **跨页时重复表头而不重复表名与单位行**。

结构依据见 ``docs/financial-table-structure-reference.md``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Iterable

import fitz


PAGE_WIDTH = 595.0
PAGE_HEIGHT = 842.0
CONTENT_LEFT = 52.0
CONTENT_RIGHT = 543.0
CONTENT_BOTTOM = 792.0
FOOTER_RULE_Y = 802.0

HEADER_ROW_HEIGHT = 26.0
GROUP_ROW_HEIGHT = 21.0
BODY_ROW_HEIGHT = 21.0
CAPTION_HEIGHT = 20.0
UNIT_HEIGHT = 16.0
TABLE_GAP = 14.0
NOTE_LINE_HEIGHT = 13.0

TABLE_FONT_SIZE = 8.6
CAPTION_FONT_SIZE = 10.5
UNIT_FONT_SIZE = 8.6
NOTE_FONT_SIZE = 8.8
CELL_PADDING = 5.0
# CJK 内置字体没有粗体字面量，合计行用 TextWriter 的「填充＋描边」渲染模式模拟加粗：
# 单次绘制，文字层不会出现重复内容，跨平台也不必嵌入系统字体。


def cell_value(value: object) -> str:
    """把报表数值渲染成披露口径的字符串。

    千分位是参考材料的硬约定（见 ``docs/financial-table-structure-reference.md`` 第 7 节），
    而报表模型的数值是 ``Decimal``——必须显式处理，否则整份材料都会退化成无分隔的裸数字。
    零值按参考材料写作 ``-``，无数据写作 ``—``。
    """
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        text = f"{value:,.0f}"
    elif isinstance(value, int):
        text = f"{value:,}"
    elif isinstance(value, float):
        text = f"{value:,.0f}"
    else:
        text = str(value).strip()
    if text in {"0", "-0", ""}:
        return "-"
    return text


@dataclass(frozen=True)
class TableCell:
    text: str
    kind: str = "normal"  # header | group | subtotal | normal
    align: str = "right"  # left | right | center


@dataclass(frozen=True)
class FinancialTable:
    # ``table_id`` 在项目内唯一（含案例前缀）；``table_key`` 是与案例无关的规范标识
    # （如 consolidated_balance_sheet），供金标、manifest 与核对接口跨案例引用。
    table_id: str
    caption: str
    unit: str
    columns: tuple[str, ...]
    rows: tuple[tuple[TableCell, ...], ...]
    notes: tuple[str, ...] = ()
    table_key: str = ""
    scope: str = "consolidated"

    def cell_id(self, row_index: int, column_index: int) -> str:
        return f"{self.table_id}:r{row_index:02d}c{column_index:02d}"

    @property
    def column_count(self) -> int:
        return len(self.columns)


@dataclass
class RenderedCell:
    cell_id: str
    row: int
    column: int
    page: int
    rect: tuple[float, float, float, float]
    text: str
    kind: str


@dataclass
class RenderedTable:
    table_id: str
    caption: str
    start_page: int
    end_page: int
    cells: list[RenderedCell] = field(default_factory=list)
    header_repeat_pages: list[int] = field(default_factory=list)
    # 该表实际占用的页码（含续页），供 manifest 与核对接口记录跨页关系。
    declared_pages: list[int] = field(default_factory=list)
    # 渲染所依据的声明结构。金标据此生成，无需再去反查案例定义。
    declared: "FinancialTable | None" = None


def column_widths(table: FinancialTable) -> list[float]:
    """第一列放科目名，占宽一些；年度列等分剩余宽度。"""
    total = CONTENT_RIGHT - CONTENT_LEFT
    count = max(table.column_count, 1)
    first_ratio = 0.42 if count <= 3 else (0.38 if count == 4 else 0.34)
    first = total * first_ratio
    rest = (total - first) / max(count - 1, 1)
    return [first] + [rest] * (count - 1)


def _row_height(cells: Iterable[TableCell]) -> float:
    kinds = {cell.kind for cell in cells}
    if "header" in kinds:
        return HEADER_ROW_HEIGHT
    if "group" in kinds:
        return GROUP_ROW_HEIGHT
    return BODY_ROW_HEIGHT


ALIGN_CODES = {"left": 0, "center": 1, "right": 2}


class PdfWriter:
    """带分页的页面写入器。

    每次开新页都会调用 ``decorate``，由调用方绘制页眉顶栏、页脚横线与页脚文字。
    章节标题由本类负责：首页用大标题块，续页用紧凑的「（续）」标题块，因此跨页表
    的后续页面仍明确属于同一章节。
    """

    def __init__(
        self,
        doc: fitz.Document,
        *,
        decorate: Callable[[fitz.Page, int], None],
        accent: tuple[float, float, float],
    ) -> None:
        self.doc = doc
        self.decorate = decorate
        self.accent = accent
        self.font = fitz.Font("china-s")
        self.page: fitz.Page | None = None
        self.page_number = 0
        self.y = CONTENT_BOTTOM
        self.section_title = ""
        self.section_subtitle = ""

    # -- 页面生命周期 ----------------------------------------------------
    def start_section(self, title: str, subtitle: str, *, cover: bool = False) -> fitz.Page:
        """开始新章节：另起一页并绘制章节标题块。``cover`` 用封面的大标题块。"""
        self.section_title = title
        self.section_subtitle = subtitle
        self._new_page()
        if cover:
            assert self.page is not None
            self.page.insert_textbox(
                fitz.Rect(46, 82, 549, 178), title,
                fontsize=16.5, lineheight=1.35, fontname="china-s", color=self.accent, align=0,
            )
            if subtitle:
                self.page.insert_textbox(
                    fitz.Rect(46, 180, 549, 210), subtitle,
                    fontsize=12, fontname="china-s", color=(0.35, 0.35, 0.35),
                )
            self.y = 230
        else:
            self._draw_section_heading(continued=False)
        return self.page  # type: ignore[return-value]

    def _new_page(self) -> fitz.Page:
        self.page = self.doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        self.page_number += 1
        self.decorate(self.page, self.page_number)
        self.y = CONTENT_BOTTOM
        return self.page

    def ensure_space(self, height: float) -> fitz.Page:
        """确保当前页还能放下 ``height``；放不下就开续页并重绘章节标题。"""
        assert self.page is not None
        if self.y + height <= CONTENT_BOTTOM:
            return self.page
        self._new_page()
        self._draw_section_heading(continued=True)
        return self.page

    def _draw_section_heading(self, *, continued: bool) -> None:
        assert self.page is not None
        label = f"{self.section_title}（续）" if continued else self.section_title
        self.page.insert_textbox(
            fitz.Rect(46, 70, 549, 116), label,
            fontsize=15 if continued else 17, fontname="china-s", color=self.accent, align=0,
        )
        if self.section_subtitle and not continued:
            self.page.insert_textbox(
                fitz.Rect(46, 114, 549, 140), self.section_subtitle,
                fontsize=10.5, fontname="china-s", color=(0.35, 0.35, 0.35),
            )
            self.y = 152
        else:
            self.y = 128

    # -- 基础绘制 --------------------------------------------------------
    def _fit(self, rect: fitz.Rect, text: str, *, size: float, min_size: float) -> tuple[float, str]:
        """把文字收进 ``rect``：先逐级缩小字号，仍放不下才截断并补省略号。

        宽度用 ``Font.text_length`` 精确测量。不靠 ``insert_textbox`` 试错——它放不下时
        什么都不画，窄列里的长科目名会整格消失且没有任何痕迹。
        """
        available = rect.width - 2 * CELL_PADDING
        current = size
        while current >= min_size:
            if self.font.text_length(text, fontsize=current) <= available:
                return current, text
            current -= 0.4
        trimmed = text
        while trimmed and self.font.text_length(f"{trimmed}…", fontsize=min_size) > available:
            trimmed = trimmed[:-1]
        return min_size, (f"{trimmed}…" if trimmed != text else text)

    def put(
        self,
        rect: fitz.Rect,
        text: str,
        *,
        size: float,
        align: str = "left",
        color: tuple[float, float, float] = (0.12, 0.12, 0.12),
        bold: bool = False,
        min_size: float = 6.4,
    ) -> None:
        """写入单元格/标题文字。

        用 ``TextWriter`` 而不是「同位置画两遍」来模拟加粗：后者会让 PDF 文字层出现
        重复内容，而这份材料正是要被逐格解析的——解析器会读到两遍同一个数值。
        ``render_mode=2``（填充＋描边）是单次绘制，提取结果唯一。
        """
        assert self.page is not None
        fitted, display = self._fit(rect, text, size=size, min_size=min_size)
        width = self.font.text_length(display, fontsize=fitted)
        if align == "right":
            x = rect.x1 - CELL_PADDING - width
        elif align == "center":
            x = rect.x0 + (rect.width - width) / 2
        else:
            x = rect.x0 + CELL_PADDING
        baseline = rect.y0 + (rect.height + fitted * 0.72) / 2
        writer = fitz.TextWriter(self.page.rect)
        writer.append((x, baseline), display, font=self.font, fontsize=fitted)
        writer.write_text(self.page, color=color, render_mode=2 if bold else 0)

    def text_block(
        self,
        rect: fitz.Rect,
        text: str,
        *,
        size: float,
        color: tuple[float, float, float] = (0.28, 0.28, 0.28),
        lineheight: float = 1.35,
    ) -> float:
        """在给定矩形内排一段可换行的说明文字，返回实际占用高度。

        与 ``put`` 的区别：``put`` 是单元格用的单行绘制（精确测宽＋对齐），
        说明性段落需要 ``insert_textbox`` 的自动换行。
        """
        assert self.page is not None
        unused = self.page.insert_textbox(rect, text, fontsize=size, lineheight=lineheight, fontname="china-s", color=color, align=0)
        return rect.height - max(unused, 0.0)

    def paragraph(self, text: str, *, size: float = 10.2, gap: float = 8.0) -> None:
        """写入一段正文。

        高度由 ``insert_textbox`` 的实际返回值决定，不靠字数估算——旧实现按估算裁剪，
        长段落会被 ``min(y + height, 775)`` 悄悄截掉。放不下时换页重试。
        """
        if not text:
            return
        self.ensure_space(size * 1.6)
        for is_retry in (False, True):
            assert self.page is not None
            available = CONTENT_BOTTOM - self.y
            unused = self.page.insert_textbox(
                fitz.Rect(CONTENT_LEFT, self.y, CONTENT_RIGHT, self.y + available),
                text, fontsize=size, lineheight=1.45, fontname="china-s", color=(0.12, 0.12, 0.12), align=0,
            )
            if unused >= 0:
                self.y += (available - unused) + gap
                return
            if is_retry:
                raise ValueError("段落过长，超过单页可用高度，请在材料定义中拆分后再渲染")
            self._new_page()
            self._draw_section_heading(continued=True)


def render_table(writer: PdfWriter, table: FinancialTable) -> RenderedTable:
    """绘制一张表，必要时跨页并重复表头。返回单元格坐标供金标引用。"""
    widths = column_widths(table)
    rendered = RenderedTable(
        table_id=table.table_id, caption=table.caption,
        start_page=writer.page_number, end_page=writer.page_number, declared=table,
    )

    writer.ensure_space(CAPTION_HEIGHT + UNIT_HEIGHT + HEADER_ROW_HEIGHT + BODY_ROW_HEIGHT)
    rendered.start_page = writer.page_number
    _draw_caption(writer, table)
    _draw_header(writer, table, widths, rendered)

    for row_index, row in enumerate(table.rows, start=1):
        height = _row_height(row)
        if writer.y + height > CONTENT_BOTTOM:
            # 跨页：只重复表头，不重复表名与单位行（与参考材料一致）。
            writer.ensure_space(height + HEADER_ROW_HEIGHT)
            rendered.header_repeat_pages.append(writer.page_number)
            _draw_header(writer, table, widths, rendered)
        _draw_row(writer, table, row, row_index, widths, rendered)

    rendered.end_page = writer.page_number
    rendered.declared_pages = sorted({cell.page for cell in rendered.cells})
    _draw_notes(writer, table)
    writer.y += TABLE_GAP
    return rendered


def _draw_caption(writer: PdfWriter, table: FinancialTable) -> None:
    assert writer.page is not None
    writer.put(
        fitz.Rect(CONTENT_LEFT, writer.y, CONTENT_RIGHT, writer.y + CAPTION_HEIGHT),
        table.caption, size=CAPTION_FONT_SIZE, color=writer.accent, bold=True,
    )
    writer.y += CAPTION_HEIGHT
    writer.put(
        fitz.Rect(CONTENT_LEFT, writer.y, CONTENT_RIGHT, writer.y + UNIT_HEIGHT),
        f"单位：{table.unit}", size=UNIT_FONT_SIZE, color=(0.35, 0.35, 0.35),
    )
    writer.y += UNIT_HEIGHT


def _draw_header(
    writer: PdfWriter, table: FinancialTable, widths: list[float], rendered: RenderedTable,
) -> None:
    assert writer.page is not None
    x = CONTENT_LEFT
    fill = (0.91, 0.94, 0.93)
    for column_index, label in enumerate(table.columns):
        rect = fitz.Rect(x, writer.y, x + widths[column_index], writer.y + HEADER_ROW_HEIGHT)
        writer.page.draw_rect(rect, color=(0.72, 0.75, 0.74), fill=fill, width=0.45)
        writer.put(
            fitz.Rect(rect.x0 + 5, rect.y0 + 6, rect.x1 - 5, rect.y1 - 2),
            label, size=TABLE_FONT_SIZE, align="left" if column_index == 0 else "right",
            color=writer.accent, bold=True,
        )
        rendered.cells.append(RenderedCell(
            cell_id=table.cell_id(0, column_index), row=0, column=column_index,
            page=writer.page_number, rect=(rect.x0, rect.y0, rect.x1, rect.y1),
            text=label, kind="header",
        ))
        x += widths[column_index]
    writer.y += HEADER_ROW_HEIGHT


def _draw_row(
    writer: PdfWriter,
    table: FinancialTable,
    row: tuple[TableCell, ...],
    row_index: int,
    widths: list[float],
    rendered: RenderedTable,
) -> None:
    assert writer.page is not None
    height = _row_height(row)
    x = CONTENT_LEFT
    for column_index in range(table.column_count):
        cell = row[column_index] if column_index < len(row) else TableCell("")
        rect = fitz.Rect(x, writer.y, x + widths[column_index], writer.y + height)
        if cell.kind == "subtotal":
            fill = (0.955, 0.965, 0.955)
        elif cell.kind == "group":
            fill = (0.975, 0.975, 0.965)
        else:
            fill = (0.985, 0.985, 0.98)
        writer.page.draw_rect(rect, color=(0.80, 0.82, 0.81), fill=fill, width=0.4)
        if cell.text:
            writer.put(
                fitz.Rect(rect.x0 + 5, rect.y0 + 5, rect.x1 - 5, rect.y1 - 1),
                cell.text, size=TABLE_FONT_SIZE, align=cell.align,
                bold=cell.kind in {"subtotal", "group"},
            )
        rendered.cells.append(RenderedCell(
            cell_id=table.cell_id(row_index, column_index), row=row_index, column=column_index,
            page=writer.page_number, rect=(rect.x0, rect.y0, rect.x1, rect.y1),
            text=cell.text, kind=cell.kind,
        ))
        x += widths[column_index]
    writer.y += height


def _draw_notes(writer: PdfWriter, table: FinancialTable) -> None:
    if not table.notes:
        return
    height = NOTE_LINE_HEIGHT * len(table.notes) + 6
    writer.ensure_space(height)
    for note in table.notes:
        assert writer.page is not None
        writer.put(
            fitz.Rect(CONTENT_LEFT, writer.y, CONTENT_RIGHT, writer.y + NOTE_LINE_HEIGHT),
            note, size=NOTE_FONT_SIZE, color=(0.35, 0.35, 0.35),
        )
        writer.y += NOTE_LINE_HEIGHT
    writer.y += 6
