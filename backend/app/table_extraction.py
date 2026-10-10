"""PDF 表格抽取：把材料里的受控表格还原成结构化对象。

设计原则
--------
1. **只用 PDF 本身的信息。** 抽取器不读取案例定义、不查 ``key_pages``、不查数据库。
   金标准来自案例模型、抽取结果来自 PDF，两者相互独立才有资格谈准确率。
2. **表名与单位靠位置锚定，不靠字面。** 参考材料里表名前缀有 ``表：``、``表/图``、
   无前缀三种，只有「``单位：`` 行的上一行是表名」这条规则稳定
   （见 ``docs/financial-table-structure-reference.md`` 第 2 节）。
3. **跨页续表只重复表头**，不重复表名与单位行。据此区分「续表」与「同页另一张表」：
   续表没有表名前导块，而同页另一张表有。
4. **无法稳定恢复的表格标记待人工确认**（``status='needs_review'``），不让模型补齐。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import fitz


TABLE_PARSER_VERSION = "table-extractor-v3"

UNIT_LINE = re.compile(r"^单位\s*[:：]\s*(?P<unit>.+)$")
NOTE_LINE = re.compile(r"^注\s*[:：]")
# 续表必须出现在页面上部（标题块之下、正文之前）。续页的章节标题块大约占 132pt，
# 因此这里放宽到 200：只看「够靠上」，不试图识别标题文字。
CONTINUATION_TOP_MAX = 200.0
HEADER_REPEAT_MIN_ROWS = 2


@dataclass
class ExtractedCell:
    row: int
    column: int
    text: str
    bbox: tuple[float, float, float, float] | None
    page: int = 0

    @property
    def is_blank(self) -> bool:
        return not self.text.strip()


@dataclass
class ExtractedTable:
    table_key: str
    caption: str
    unit: str
    page: int
    pages: list[int] = field(default_factory=list)
    continued_from: int | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list[ExtractedCell]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    header_rows: int = 1
    scope: str = ""
    currency: str = "CNY"
    extraction_method: str = "find_tables"
    confidence: float = 0.0
    status: str = "extracted"
    # 表名恢复来源：``unit_anchor``（单位行强锚点）| ``title_hint``（弱标题，仅近似）|
    # ``none``（未恢复）。修订稿要求记录恢复来源：弱标题不可与强锚点同等看待，
    # 也不能被当成"业务值可信"的依据。
    caption_origin: str = "none"
    document_id: str = ""
    storage_id: str = ""
    parser_version: str = TABLE_PARSER_VERSION

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return len(self.columns)

    def cells(self) -> list[ExtractedCell]:
        return [cell for row in self.rows for cell in row]

    @property
    def normalizable_ratio(self) -> float:
        """可归一化为数值的单元格占比——剔除分组行与空白后计算。"""
        meaningful = [cell for cell in self.cells() if normalize_cell(cell.text)[0] != "blank"]
        if not meaningful:
            return 0.0
        numeric = sum(1 for cell in meaningful if normalize_cell(cell.text)[0] in {"number", "zero"})
        return round(numeric / len(meaningful), 4)

    def signature(self) -> tuple[str, int]:
        """用于匹配金标准的签名：表名 + 表头列数。"""
        return (self.caption, self.column_count)


def normalize_cell(text: str) -> tuple[str, Decimal | None]:
    """把单元格原文规范化成 ``(状态, 数值)``。

    状态取值：``number`` / ``zero`` / ``blank``（空单元格）/ ``not_applicable``（``—``）
    / ``text``。参考材料里零值写作 ``-``、无数据写作 ``—``、分组行其余列为空。
    """
    raw = (text or "").strip()
    if not raw:
        return ("blank", None)
    if raw in {"—", "——", "－", "--"}:
        return ("not_applicable", None)
    if raw in {"-", "－"}:
        return ("zero", Decimal("0"))
    cleaned = raw.replace(",", "").replace("，", "").replace(" ", "")
    cleaned = cleaned.replace("（", "").replace("）", "").replace("(", "").replace(")", "")
    try:
        return ("number", Decimal(cleaned))
    except (InvalidOperation, ValueError):
        return ("text", None)


def _lines_with_positions(page: fitz.Page) -> list[tuple[float, str]]:
    """返回页面上每一行文字及其基线 y，用于按位置锚定表名与单位。"""
    output: list[tuple[float, str]] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                output.append((float(line["bbox"][1]), text))
    output.sort(key=lambda item: item[0])
    return output


# 弱标题（没有「单位：」行时的表名恢复）只允许这两类反证之外的一行，
# 且必须离表格足够近——再往上多半已经是正文或上一张表的表注。
CAPTION_MAX_DISTANCE = 60.0
CAPTION_MAX_CHARS = 30
_SECTION_HEADING_RE = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百\d]+[节章]|[一二三四五六七八九十]+[、.]"
    r"|附[一二三四五六七八九十\d]+[：:]?|\d+(?:\.\d+)*[、.]?\s)"
)
_CONTINUATION_SUFFIX_RE = re.compile(r"[（(]\s*续\s*[)）]\s*$")
_SENTENCE_PUNCT_RE = re.compile(r"[。！？；，,;]")
_NUMERIC_TOKEN_RE = re.compile(r"\d[\d,]*\.?\d*")


def _is_caption_candidate(text: str, page_headers: frozenset[str], *, distance: float) -> bool:
    """弱标题候选：像表名才接受，命中任何一类反证都否决。

    排除对象（修订稿 §2.3）：重复页眉、章节标题、续表标签（…（续））、正文句、
    表注与「单位：」行，以及**含多段数值的数据行**。
    """
    compact = re.sub(r"\s+", "", text or "")
    if not compact or distance > CAPTION_MAX_DISTANCE:
        return False
    if compact in page_headers:
        return False
    if _SECTION_HEADING_RE.match(text or "") or _CONTINUATION_SUFFIX_RE.search(compact):
        return False
    if len(compact) > CAPTION_MAX_CHARS or _SENTENCE_PUNCT_RE.search(compact):
        return False
    if compact.endswith(("：", ":")) or NOTE_LINE.match(text or "") or UNIT_LINE.match(text or ""):
        return False
    if len(_NUMERIC_TOKEN_RE.findall(compact)) >= 2:
        return False
    return True


def _row_looks_like_data(row: list[ExtractedCell]) -> bool:
    """首行究竟是**表头行**还是**业务数据**。

    续页常常不重复表头，此时 PyMuPDF 给出的第一行就是业务数据；按表头处理会**丢掉一行真实
    数据**（修订稿 §2.3 明确要求保留）。判定：首列非空，且除首列外至少两个"真数值"格——
    纯年度（``2025``）不算数值，它是期间列名。
    """
    if not row or not row[0].text.strip():
        return False

    def is_value(text: str) -> bool:
        compact = text.strip()
        if re.fullmatch(r"(?:19|20)\d{2}", compact):
            return False
        return normalize_cell(compact)[0] in {"number", "zero"}

    return sum(1 for cell in row[1:] if is_value(cell.text)) >= 2


def _repeated_header_lines(document) -> frozenset[str]:
    """重复出现的页眉文本。**短文档也必须识别得出**。

    阈值随页数退化：少于 4 页时 2 次即算重复——若一律要求 3 次以上，短文档永远达不到，
    页眉就会被当成表名候选（修订稿 §2.3）。
    """
    counts: dict[str, int] = {}
    for page in document:
        for _y, text in _lines_with_positions(page):
            compact = re.sub(r"\s+", "", text or "")
            if compact and _y <= 90:
                counts[compact] = counts.get(compact, 0) + 1
    pages = max(1, int(getattr(document, "page_count", 0) or 1))
    threshold = 2 if pages < 4 else max(3, int(pages * 0.3))
    return frozenset(text for text, count in counts.items() if count >= threshold)


def _caption_block(
    lines: list[tuple[float, str]], lower: float, upper: float,
    page_headers: frozenset[str] = frozenset(),
) -> tuple[str, str, str]:
    """在 ``lower..upper`` 纵区间内取表名前导块，并**记录恢复来源**。

    参考材料的固定结构是「表名行 → ``单位：`` 行 → 表头行」，**单位行强锚点优先**：
    命中即按原语义取表名（``unit_anchor``），现有素材行为逐字不变。

    没有单位行时（评级报告的表格就是这种）才走**弱标题**恢复（``title_hint``）：只在表格
    正上方 ``CAPTION_MAX_DISTANCE`` 内、且通过 ``_is_caption_candidate`` 全部反证检验的行
    才算候选。**证据不足时保持未恢复（``none``）**，绝不硬猜——猜错表名比没有表名更糟。
    """
    window = [(y, text) for y, text in lines if lower < y < upper - 2]
    if not window:
        return ("", "", "none")
    match = UNIT_LINE.match(window[-1][1])
    if match:
        caption = window[-2][1] if len(window) >= 2 else ""
        return (caption, match.group("unit").strip(), "unit_anchor")
    for y, text in reversed(window):
        if _is_caption_candidate(text, page_headers, distance=upper - y):
            return (text.strip(), "", "title_hint")
    return ("", "", "none")



def _table_page(page: fitz.Page, page_no: int, lines: list[tuple[float, str]],
                page_headers: frozenset[str] = frozenset()) -> list[ExtractedTable]:
    """把一页上的所有物理表格抽出来（尚未合并续表）。"""
    found = sorted(page.find_tables().tables, key=lambda item: float(item.bbox[1]))
    output: list[ExtractedTable] = []
    previous_bottom = 0.0
    for index, raw in enumerate(found, start=1):
        top = float(raw.bbox[1])
        caption, unit, caption_origin = _caption_block(lines, previous_bottom, top, page_headers)
        matrix = raw.extract()
        column_count = max((len(row) for row in matrix), default=0)
        # PyMuPDF exposes ``Table.rows[*].cells[*]`` in row-major order.  It is
        # the authoritative geometry mapping because merged cells make the
        # flattened ``Table.cells`` array sparse and column-major.
        row_geometry = getattr(raw, "rows", None) or []
        geometry = raw.cells or []
        # PyMuPDF 的 ``Table.cells`` 是**列优先**（先列 0 的全部行，再列 1……），
        # 而 ``extract()`` 给的是行优先矩阵。用行优先去索引它就整体错位——实测华辰
        # 资产负债表 106 个非空格里只有 4 个的坐标能对上自己的值，其余坐标指向的是
        # 别的科目。契约声称「页码与坐标可定位」，坐标错了等于没定位。
        geometry_rows = int(getattr(raw, "row_count", 0) or len(matrix))
        rows: list[list[ExtractedCell]] = []
        for row_index, row in enumerate(matrix):
            cells: list[ExtractedCell] = []
            for column_index in range(column_count):
                text = row[column_index] if column_index < len(row) else ""
                if row_geometry and row_index < len(row_geometry):
                    row_cells = getattr(row_geometry[row_index], "cells", row_geometry[row_index]) or []
                    bbox = row_cells[column_index] if column_index < len(row_cells) else None
                else:
                    slot = column_index * geometry_rows + row_index
                    bbox = geometry[slot] if 0 <= slot < len(geometry) else None
                cells.append(ExtractedCell(
                    row=row_index, column=column_index, text=(text or "").strip(),
                    bbox=tuple(float(value) for value in bbox) if bbox else None,
                    page=page_no,
                ))
            rows.append(cells)
        # 首行是业务数据还是表头行：续页不重复表头时，把它当表头会**丢掉一行真实数据**
        # （修订稿 §2.3）。此时不给列名（列为空），由续表比对按列数兼容判定并保留该行。
        header_is_data = bool(rows) and _row_looks_like_data(rows[0])
        table = ExtractedTable(
            table_key=f"p{page_no:03d}-t{index:02d}",
            caption=caption, unit=unit, page=page_no, pages=[page_no],
            caption_origin=caption_origin,
            columns=[] if header_is_data else ([cell.text for cell in rows[0]] if rows else []),
            rows=rows if header_is_data else rows[1:],
            header_rows=0 if header_is_data else 1,
            bbox=tuple(float(value) for value in raw.bbox),
        )
        _attach_notes(table, lines, lower=float(raw.bbox[3]), upper=_next_top(found, index, top))
        output.append(table)
        previous_bottom = float(raw.bbox[3])
    return output


def _next_top(found: list, index: int, current_top: float) -> float:
    """下一张表的上边界；表注只可能在当前表底与下一张表顶之间。"""
    if index < len(found):
        return float(found[index].bbox[1])
    return float("inf")


def _same_header(left: ExtractedTable, right: ExtractedTable) -> bool:
    """表头兼容性。

    ``right`` 有自己的表头行时按表头逐字比对（原语义）。续页**没有**表头行（首行就是业务
    数据）时只能按列数兼容判定——此时绝不能顺手把那行丢掉。这**不**等于"缺强锚点就自动
    当续表"：其余条件仍由 ``_is_continuation`` 把守。
    """
    if not left.columns:
        return False
    if right.columns:
        return [item.strip() for item in left.columns] == [item.strip() for item in right.columns]
    if not right.rows:
        return False
    return len(right.rows[0]) == len(left.columns)


def _is_continuation(table: ExtractedTable, previous: ExtractedTable | None, previous_bottom: float) -> bool:
    """续表判定：相邻页、贴在页首、上表画到页底附近、表头兼容、单位一致。

    第 4 条是关键：**同页另一张表**（例如同页的货币资金明细与其他应收款明细）
    也有表名前导块；而**母公司资产负债表**是另一张逻辑表，它有自己的表名与单位，
    因此不会被并进合并报表。区分依据只能是前导块，不能是页序或数值相似度。
    """
    if previous is None or table.row_count < HEADER_REPEAT_MIN_ROWS:
        return False
    # 只有**强锚点**前导块才否定续表。弱标题是"猜"来的：不能仅凭它就把续页判成新表，
    # 但也**不允许**它让续页自动成立——下面的条件继续把守。
    if table.caption_origin == "unit_anchor" or table.unit:
        return False
    # 两页都恢复了弱标题且**不相同** → 不是同一张表，不合并（修订稿：弱标题不同即不合并）。
    if (table.caption_origin == "title_hint" and previous.caption_origin == "title_hint"
            and table.caption.strip() != previous.caption.strip()):
        return False
    # 单位必须一致：两页各标一个不同单位，说明不是同一张表。
    if previous.unit and table.unit and previous.unit.strip() != table.unit.strip():
        return False
    if table.bbox[1] > CONTINUATION_TOP_MAX:
        return False
    if previous_bottom < 700:
        return False
    return _same_header(previous, table)


def _attach_notes(table: ExtractedTable, lines: list[tuple[float, str]], *, lower: float, upper: float) -> None:
    """表注位于表格下方、下一张表之前，以 ``注：`` 开头；单独保留，不混入数据行。"""
    for y, text in lines:
        if y <= lower - 1 or y >= upper:
            continue
        if NOTE_LINE.match(text):
            table.notes.append(text)


def extract_tables(path: Path) -> list[ExtractedTable]:
    """从 PDF 抽取表格。跨页续表合并为同一逻辑表。"""
    document = fitz.open(path)
    output: list[ExtractedTable] = []
    previous: ExtractedTable | None = None
    last_physical_bottom = 0.0
    previous_page = 0
    # 页眉集先算一次：弱标题恢复必须能排除重复页眉，否则页眉会被当成表名。
    page_headers = _repeated_header_lines(document)
    for page_no, page in enumerate(document, start=1):
        lines = _lines_with_positions(page)
        physical = _table_page(page, page_no, lines, page_headers)
        for table in physical:
            if previous_page == page_no - 1 and _is_continuation(table, previous, last_physical_bottom):
                assert previous is not None
                # 续表的重复表头已在 rows 之外，但行号是各自页内的，直接追加会撞车：
                # 第二页的第 1 行会和第一页的第 1 行共用 (row, column)。必须重编号。
                offset = previous.row_count
                for row in table.rows:
                    for cell in row:
                        cell.row += offset
                previous.rows.extend(table.rows)
                previous.pages.append(page_no)
                previous.notes.extend(table.notes)
            else:
                # 没并入续表、且没有列名（首行被判成业务数据）→ 它其实是一张独立表：
                # 首行就是它的表头，按表头解释，别让下游拿到"没有列名"的表。
                if not table.columns and table.rows:
                    table.columns = [cell.text for cell in table.rows[0]]
                    table.rows = table.rows[1:]
                    table.header_rows = 1
                output.append(table)
                previous = table
            last_physical_bottom = table.bbox[3]
        if physical:
            previous_page = page_no
    for table in output:
        table.confidence = _confidence(table)
        table.status = "extracted" if table.confidence >= 0.9 else "needs_review"
    document.close()
    return output


@dataclass
class TableAccuracy:
    """抽取结果与金标的逐项比对。

    匹配按 ``(表名, 列数)`` 且页码区间相交——不用 ``table_key``，因为那是解析器
    自己的编号；金标只声明「应该有什么」，不告诉解析器它会在哪。
    """

    expected_tables: int = 0
    matched_tables: int = 0
    caption_matches: int = 0
    unit_matches: int = 0
    cell_total: int = 0
    cell_exact: int = 0
    continuation_expected: int = 0
    continuation_found: int = 0
    needs_review: int = 0
    unmatched: list[str] = field(default_factory=list)

    @property
    def table_recall(self) -> float:
        return round(self.matched_tables / self.expected_tables, 4) if self.expected_tables else 0.0

    @property
    def cell_accuracy(self) -> float:
        return round(self.cell_exact / self.cell_total, 4) if self.cell_total else 0.0

    def summary(self) -> dict:
        return {
            "expected_tables": self.expected_tables, "matched_tables": self.matched_tables,
            "table_recall": self.table_recall, "caption_matches": self.caption_matches,
            "unit_matches": self.unit_matches, "cell_total": self.cell_total,
            "cell_exact": self.cell_exact, "cell_accuracy": self.cell_accuracy,
            "continuation_expected": self.continuation_expected,
            "continuation_found": self.continuation_found,
            "needs_review": self.needs_review, "unmatched": self.unmatched,
        }


def compare_with_gold(extracted: list[ExtractedTable], gold: list[dict]) -> TableAccuracy:
    """把抽取结果与表格金标逐项比对。"""
    accuracy = TableAccuracy()
    expected_tables = [item["expected"] for item in gold if item["annotation_type"] == "table"]
    gold_cells: dict[str, dict[tuple[int, int], dict]] = {}
    for item in gold:
        if item["annotation_type"] == "table_cell":
            gold_cells.setdefault(item["expected"]["table_id"], {})[
                (item["expected"]["row"], item["expected"]["column"])
            ] = item["expected"]
    gold_continuations = {
        item["expected"]["table_id"]: item["expected"]
        for item in gold if item["annotation_type"] == "table_relation"
    }
    accuracy.expected_tables = len(expected_tables)
    accuracy.continuation_expected = len(gold_continuations)

    for expected in expected_tables:
        match = _match_table(extracted, expected)
        if match is None:
            accuracy.unmatched.append(expected["table_id"])
            accuracy.cell_total += len(gold_cells.get(expected["table_id"], {}))
            continue
        accuracy.matched_tables += 1
        if match.caption == expected["caption"]:
            accuracy.caption_matches += 1
        if match.unit == expected["unit"]:
            accuracy.unit_matches += 1
        if expected["table_id"] in gold_continuations and len(match.pages) > 1:
            accuracy.continuation_found += 1
        cells = {(cell.row, cell.column): cell for cell in match.cells()}
        for key, gold_cell in gold_cells.get(expected["table_id"], {}).items():
            accuracy.cell_total += 1
            found = cells.get(key)
            if found is not None and _same_cell(found.text, gold_cell):
                accuracy.cell_exact += 1
    accuracy.needs_review = sum(1 for table in extracted if table.status == "needs_review")
    return accuracy


def _match_table(extracted: list[ExtractedTable], expected: dict) -> ExtractedTable | None:
    """按结构身份匹配：表名、表头文字（含年度）、列数、行数、页码区间。

    **表头文字必须逐一相同。** 只比列数是不够的——把「2025 年末」改成「2026 年末」
    列数不变、行数不变，但整张表的口径已经变了。年度是口径的唯一来源，
    换年必须被判定为「不是同一张表」，而不是「同一张表但值不同」。
    """
    for table in extracted:
        if table.caption != expected["caption"]:
            continue
        if [item.strip() for item in table.columns] != [item.strip() for item in expected["columns"]]:
            continue
        if table.column_count != expected["column_count"]:
            continue
        if table.row_count != expected["row_count"]:
            continue
        if not (set(table.pages) & set(expected["pages"])):
            continue
        return table
    return None


def _same_cell(found: str, gold_cell: dict) -> bool:
    """单元格比对：文字按原文比对，数值按规范化后的数值比对。

    「6820」与「6,820」是同一笔数据——比对的是数据，不是书写格式。
    """
    left, right = (found or "").strip(), (gold_cell.get("raw") or "").strip()
    if left == right:
        return True
    left_kind, left_value = normalize_cell(left)
    right_kind, right_value = normalize_cell(right)
    if left_kind == right_kind and left_value == right_value:
        return True
    if left_kind in {"number", "zero"} and right_kind in {"number", "zero"}:
        return left_value == right_value
    return False


def _confidence(table: ExtractedTable) -> float:
    """置信度衡量的是**抽取是否可靠**，不是这张表有没有用。

    缺表名、缺单位说明前导块没能恢复，几何退化说明行列没读全——这些才降置信度。
    「一个可识别数值都没有」不算不可靠（发行要素表本来就全是「20,000万元」这样的
    带单位文本），它由 ``normalizable_ratio`` 单独反映，供准确率统计使用。
    """
    score = 1.0
    if not table.caption:
        score -= 0.4
    if not table.unit:
        score -= 0.15
    if table.row_count == 0 or table.column_count < 2:
        score -= 0.5
    return max(0.0, round(score, 2))
