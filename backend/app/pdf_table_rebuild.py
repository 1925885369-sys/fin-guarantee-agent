"""无边框/弱线框表格的**坐标重建**（``find_tables`` 碎片化时的回退路径）。

为什么需要它
------------
``page.find_tables()`` 默认按**矢量线框**识别表格。中诚信/联合资信这类评级报告大量使用
无边框三线表：没有完整网格线，PyMuPDF 会把**每一行文字**当成一张独立的 1 行表。实测北投
评级报告 p4「财务概况」10 张碎片表全部 ``数据行=0``，其中真实数值（8,747.33 / 9,276.21 …）
一个字都没进结构化存储——报告侧 ``if not table.columns or not table.rows`` 直接跳过，
评级口径财务数据因此完全不可用。

设计约束
--------
1. **只在碎片页启用**：``_fragmentary()`` 判定同页多张 ≤1 行表时才回退；正常表（中邮
   「图表1…图表25」那种有框图表）走原路径，行为逐字不变。
2. **只重建能识别出表头的表**：没有表头就无法确定列语义，硬猜列名会把数据行当表头，
   比不重建更糟（与 ``_caption_block`` "证据不足保持未恢复"同一纪律）。
3. **不做语义合并**：跨行标签、无标签续行一律保留为独立行、首列留空，由下游标
   ``needs_review``；猜错行归属会污染数值绑定。
4. 抽取结果标 ``extraction_method="coordinates"``，与线框路径可区分、可回查。
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Callable, Iterable

import fitz

from .table_extraction import ExtractedCell, ExtractedTable, normalize_cell

# 列间最小空白（pt）。同页词间空格约 2–4pt，数值列间隔 25–40pt，15pt 是实测的稳定分界：
# 北投 p4/p9/p16 在 12–15pt 都能切出正确列数，18pt 起会把表头相邻列黏在一起。
COLUMN_GAP = 15.0
# 同一文本行的纵向容差（pt）。行距实测 12–16pt，3pt 不会把相邻行并进来。
ROW_TOLERANCE = 3.0
MIN_REGION_ROWS = 3
MIN_COLUMNS = 3
# 区域断开阈值：行距超过「本区域中位行距 × 2.2」或绝对 34pt 即视为另一张表。
# 北投 p16 表 11 内有一处 25.8pt 的跨行标签，不能被切断。
REGION_BREAK_ABSOLUTE = 34.0
REGION_BREAK_RATIO = 2.2

_PERIOD_COLUMN_RE = re.compile(
    r"^(?:(?:19|20)\d{2}\s*(?:年|\.\d+|/\d|~|-|$)|单位|项目|序号|指标|名称|企业名称|公司名称)"
)
_HEADER_WORDS = {"项目", "单位", "序号", "指标", "名称", "企业名称", "公司名称", "科目"}
_LABEL_UNIT_RE = re.compile(r"亿元|万元|元")


def _infer_table_unit(body: list[list[_Span]]) -> str:
    """从行标签的「（亿元）」推断**整表单位**，供报告侧换算与置信度使用。

    评级报告常把单位写在每行科目后（``总资产（亿元）``），表格上方没有「单位：」行；
    不推断就永远缺 unit → 每张表都被判 needs_review，交付门被迫要求人工逐表确认。
    规则保守：同一货币单位出现在 ≥2 行、且占带单位行的 60% 以上才采纳；混合单位
    （亿元 + % + X）不会被误判成整表亿元——% 行仍按行标签自身单位处理。
    """
    units = []
    for row in body:
        if not row:
            continue
        found = _LABEL_UNIT_RE.findall(row[0].text or "")
        if found:
            units.append(found[0])
    if len(units) < 2 or len(set(units)) != 1:
        return ""
    if len(units) < max(2, int(len(body) * 0.6)):
        return ""
    return units[0]


@dataclass
class _Span:
    """一个单元格在页面上的词集合。"""

    words: list[tuple[float, float, float, float, str, int, int, int]]

    @property
    def text(self) -> str:
        return " ".join(item[4] for item in sorted(self.words, key=lambda w: w[0])).strip()

    @property
    def bbox(self) -> tuple[float, float, float, float] | None:
        if not self.words:
            return None
        return (
            min(item[0] for item in self.words), min(item[1] for item in self.words),
            max(item[2] for item in self.words), max(item[3] for item in self.words),
        )

    @property
    def center(self) -> float:
        box = self.bbox
        return (box[0] + box[2]) / 2 if box else 0.0


def _text_rows(page: fitz.Page) -> list[tuple[float, list]]:
    """按 y 中心把词聚成文本行（容差 ``ROW_TOLERANCE``）。"""
    rows: list[list] = []
    for item in page.get_text("words"):
        yc = (item[1] + item[3]) / 2
        if rows and abs(yc - rows[-1][0]) <= ROW_TOLERANCE:
            rows[-1][1].append(item)
        else:
            rows.append([yc, [item]])
    return [(float(yc), items) for yc, items in rows]


def _split_row(words: list, gap: float = COLUMN_GAP) -> list[_Span]:
    """按 x 空白把一行词切成单元格。空白 > ``gap`` 才切，词间空格（2–4pt）不切。"""
    spans: list[_Span] = []
    current: list = []
    prev_x1: float | None = None
    for item in sorted(words, key=lambda w: w[0]):
        if prev_x1 is not None and item[0] - prev_x1 > gap:
            spans.append(_Span(current))
            current = []
        current.append(item)
        prev_x1 = max(prev_x1 or 0.0, item[2])
    if current:
        spans.append(_Span(current))
    return spans


def _looks_like_header(spans: list[_Span]) -> bool:
    """首行是不是表头：期间列名（2023/2024年/2026.1~3）或表头词，且首列非纯数值。"""
    if not spans:
        return False
    texts = [span.text for span in spans]
    if not texts[0].strip():
        return False
    if normalize_cell(texts[0])[0] in {"number", "zero"}:
        return False
    for text in texts:
        compact = text.strip()
        if compact in _HEADER_WORDS or _PERIOD_COLUMN_RE.match(compact):
            return True
    # 整行没有一个真数值 → 也是表头（如「企业名称 | 京投公司 | 广州地铁」）
    values = [text for text in texts[1:] if normalize_cell(text)[0] in {"number", "zero"}]
    return not values and len(texts) >= 2


def _columns_from_header(header: Iterable[list[_Span]], body: list[list[_Span]]) -> list[str]:
    """把表头行映射到数据列：列数一致直接取；不一致按 x 中心落位。"""
    header_rows = [row for row in header if row]
    if not header_rows:
        return []
    first = header_rows[-1]  # 多行表头取最贴近数据的一行
    width = len(body[0]) if body else 0
    if width and len(first) == width:
        return [span.text for span in first]
    # x 区间落位：数据列的横向范围由全部数据行决定（数值右对齐，x0 会漂）。
    ranges = []
    for index in range(width):
        lefts = [row[index].bbox[0] for row in body if index < len(row) and row[index].bbox]
        rights = [row[index].bbox[2] for row in body if index < len(row) and row[index].bbox]
        ranges.append((min(lefts), max(rights)) if lefts else (0.0, 0.0))
    columns = ["" for _ in range(width)]
    for span in first:
        center = span.center
        slot = min(range(width), key=lambda i: abs(center - (ranges[i][0] + ranges[i][1]) / 2))
        columns[slot] = f"{columns[slot]} {span.text}".strip() if columns[slot] else span.text
    return columns


def _regions(rows: list[tuple[float, list[_Span]]]) -> list[list[tuple[float, list[_Span]]]]:
    """把连续的多单元格行聚成候选表区。单单元格行（正文/表注）天然断开。

    例外：**夹在多列行之间的单列行**若紧贴上一条多列行（行距在断开阈值内），按"跨行标签"
    保留在区域内，不切断——北投 p16 表 11 的「以公允价值计量且其变动计入当期损益的金融
    资产」就折成两行写在表中间，一旦切断会同时丢掉表头和首行数据。
    """
    output: list[list] = []
    current: list = []
    pending_single: tuple[float, list[_Span]] | None = None
    for yc, spans in rows:
        if len(spans) < 2:
            if current:
                pending_single = (yc, spans)
            continue
        if current:
            gaps = [b[0] - a[0] for a, b in zip(current, current[1:])]
            median = statistics.median(gaps) if gaps else 0.0
            limit = max(REGION_BREAK_ABSOLUTE, median * REGION_BREAK_RATIO)
            if yc - current[-1][0] > limit:
                output.append(current)
                current = []
            elif pending_single is not None and pending_single[0] < yc and yc - pending_single[0] <= limit:
                current.append(pending_single)
        current.append((yc, spans))
        pending_single = None
    if current:
        output.append(current)
    # 跨行标签可能在被并入后才遇到更靠上的行，统一按 y 排序，保证 region[0] 是最上面一行。
    for region in output:
        region.sort(key=lambda item: item[0])
    return output


def _pad_missing_label(spans: list[_Span]) -> list[_Span]:
    """缺行标签的行：首格是数值 → 说明标签被折到别的文本行，补空标签、数值不丢。

    实测北投 p16 表 11「以公允价值计量且其变动计入当期损益的金融资产」：标签折成两行、
    数值垂直居中，坐标切分后得到 4 个数值格（列数比正文少 1），原实现把它整行丢弃，
    等于吞掉 104.13 / 94.77 / 92.73 / 282.24 四个真实数值。
    """
    if not spans:
        return spans
    if normalize_cell(spans[0].text)[0] in {"number", "zero"}:
        return [_Span([])] + spans
    return spans


def _fragmentary(found: list) -> bool:
    """``find_tables`` 是否把一页拆成了碎片。

    判据：同页 ≥4 张表，且其中 ≥60% 是 ≤1 行的"单行表"（北投 p4=10/10、p9=10/10、
    p16=6/7 命中；中邮 p6=2 张、p11=1 张不受影响）。宁可漏判也不能把正文页误当碎片。
    """
    if len(found) < 4:
        return False
    single = 0
    for raw in found:
        try:
            rows = raw.extract()
        except Exception:
            rows = []
        if len(rows) <= 1:
            single += 1
    return single >= 3 and single >= len(found) * 0.6


def rebuild_page_tables(
    page: fitz.Page,
    page_no: int,
    *,
    caption_for: Callable[[float, float], tuple[str, str, str]],
    previous_bottom: float = 0.0,
) -> list[ExtractedTable]:
    """按坐标重建一页里的无边框表。``caption_for(lower, upper)`` 复用线框路径的表名逻辑。"""
    rows = [(yc, _split_row(words)) for yc, words in _text_rows(page)]
    output: list[ExtractedTable] = []
    lower = previous_bottom
    for index, region in enumerate(_regions(rows), start=1):
        if len(region) < MIN_REGION_ROWS:
            continue
        counts = [len(spans) for _, spans in region]
        body_width = statistics.mode(counts)
        if body_width < MIN_COLUMNS:
            continue
        # 缺标签行（列数少 1、首格是数值）补齐后再参与：宁可标签留空，也不能吞掉数值。
        padded: list[list[_Span]] = []
        missing_label = False
        for _, spans in region:
            if len(spans) == body_width - 1 and spans and normalize_cell(spans[0].text)[0] in {"number", "zero"}:
                spans = _pad_missing_label(spans)
                missing_label = True
            padded.append(spans)
        body = [spans for spans in padded if len(spans) == body_width]
        if len(body) < 2:
            continue
        # 表头 = 首个数据行之前的行；若首行本身像表头（列数一致时）也算进去。
        first_body = next(i for i, spans in enumerate(padded) if len(spans) == body_width)
        header = padded[:first_body]
        if not header and _looks_like_header(padded[first_body]):
            header = [padded[first_body]]
            body = body[1:]
        if not header:
            # 没有表头就不重建：列语义不明，硬猜列名比不重建更糟。
            continue
        columns = _columns_from_header(header, body)
        if not columns or not any(columns):
            continue
        top = min(span.bbox[1] for span in region[0][1] if span.bbox)
        bottom = max(span.bbox[3] for _, spans in region for span in spans if span.bbox)
        # 表名的上界必须取**表头行自身**的 y：同一行里右侧单元格的 y0 常比首列小几十分之
        # 一（实测北投 p4「同行业比较」把「武汉地铁」当成了表名），用整块 min y0 会让同行
        # 其它单元格落进候选窗口。行级 y 是行顶、比词中心小约 5pt，故再留 9pt 余量。
        caption, unit, caption_origin = caption_for(lower, region[0][0] - 2 * ROW_TOLERANCE - 3)
        if not unit:
            # 没有「单位：」行时，用行标签里反复出现的货币单位兜底（见 _infer_table_unit）。
            unit = _infer_table_unit(body)
        cells_rows: list[list[ExtractedCell]] = []
        for row_index, spans in enumerate(body):
            cells = []
            for column_index in range(body_width):
                span = spans[column_index] if column_index < len(spans) else None
                cells.append(ExtractedCell(
                    row=row_index, column=column_index,
                    text=span.text if span else "",
                    bbox=span.bbox if span else None, page=page_no,
                ))
            cells_rows.append(cells)
        confidence = 0.7 if len(body) >= 3 else 0.55
        if missing_label:
            # 有行标签未能恢复：数值可用但行归属待核，按 needs_review 交付。
            confidence = min(confidence, 0.55)
        output.append(ExtractedTable(
            table_key=f"p{page_no:03d}-c{index:02d}",
            caption=caption, unit=unit, page=page_no, pages=[page_no],
            caption_origin=caption_origin, columns=columns, rows=cells_rows,
            header_rows=len(header), bbox=(top, top, top, bottom),
            extraction_method="coordinates", confidence=confidence,
            status="extracted" if confidence >= 0.6 else "needs_review",
            notes=(
                ["注：本表按坐标重建；有行标签在原文中折行、未能自动恢复，数值已保留，行归属待人工核对。"]
                if missing_label else []
            ),
        ))
        lower = bottom
    return output
