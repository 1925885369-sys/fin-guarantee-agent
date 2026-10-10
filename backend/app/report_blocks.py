"""报告章节的 ``blocks``：正文分块 + 系统表格块。

契约
----
``GET /api/projects/{id}/report`` 的每个 section 带一个**有序**的 ``blocks`` 列表：

- ``{"kind": "heading"}`` / ``{"kind": "list"}`` / ``{"kind": "quote"}`` /
  ``{"kind": "paragraph", "text": ...}``：正文分块，形状沿用
  ``report_outline.content_blocks``（前端按 ``kind`` 渲染），段落按空行切分；
- ``{"kind": "table", ...}``：系统表格块。

两条纪律
--------
1. **表格数据只有一个来源**：``report_tables.chapter_table_blocks`` 返回的
   ``TableBlock``。表格块里的每个数值都由 ``TableBlock.to_dict()`` 序列化而来，
   **不在正文 Markdown 上做任何反解**——正文里的表格只是同一份数据的渲染结果，
   从 Markdown 回读会把渲染格式当成数据契约（改一次排版就换一套数字）。
2. **正文与表格分开**：系统表格块（含表名、表注、校验行）与冲突块属于冻结披露，
   它们不进段落块。段落块只写分析文字，否则前端会在表格上下各渲染一份表格文本。

表格块的字段约定
----------------
- ``table``：完整的 ``TableBlock.to_dict()``（``columns``／``rows``／``notes``／
  ``validation_status``／``origin_mode``／``source_refs``／逐格来源与定位）。
  结构化的行在这里，消费方读 ``block["table"]["rows"]``。
- 块级扁平字段（``table_id``／``table_key``／``table_type``／``title``／``unit``／
  ``scope``／``origin_mode``／``validation_status``／``columns``／``notes``／
  ``present_in_content``）供前端直接取用，值与 ``table`` 同源。
- ``rows``：**Markdown 表格行**（历史契约，前端表格渲染仍在读它），与
  ``table["rows"]`` 不是同一件东西。两者同源于同一个 ``TableBlock``，因此
  不可能出现数值不一致。

``present_in_content``
----------------------
表示**当前** ``TableBlock`` 渲染出的表格（表头＋数据行＋表注）是否逐字出现在正文里。
事实在报告生成后被更新时，历史正文里的仍是旧数值——此时为 ``false``，前端应据此
提示「正文中的表与当前数据不一致」，而不是静默按正文渲染。
"""
from __future__ import annotations

import re

from .report_outline import content_blocks
from .report_tables import chapter_table_blocks, render_table_block
from .table_block import TableBlock, table_block_from_dict


# 报告表格的业务键：与材料原表的 ``table_key`` 同一命名体例，供前端与后续轨道把
# 「报告表」对回「材料原表」。**只登记确有对应原表的表**；没有对应原表的表（冲突
# 对照表、预测表等）留空，不编造映射。
REPORT_TABLE_KEYS: dict[str, str] = {
    "report-financial-balance": "consolidated_balance_sheet",
    "report-financial-receivables": "receivables_aging",
    "report-financial-income": "income_statement",
    "report-financial-cashflow": "cash_flow_statement",
    "report-financial-debt": "interest_bearing_debt_detail",
    "report-cashflow-sources": "cash_flow_statement",
    "report-debt-structure": "interest_bearing_debt_detail",
    "report-rating-reconciliation": "rating_financial_summary",
}

# 正文里区块的识别标记。用**渲染出来的原样标记**而不是行号：正文会被模型改写、
# 章节会被单独重生成，位置靠不住。
SYSTEM_TABLE_MARK = "系统整理数据表"
CONFLICT_MARK = "冲突事实"
# 表名行（``**表 1/2：三年主要资产负债项目表**（单位：…）``）。Word 出口据此把表名与
# 表体绑在同一页，因此与区块识别共用同一个正则。
# 两种表标题形式都接受：``**表 1：…**`` 与 ``**表 1/5：…**``。
# 分母形式已不再生成（``N/M`` 与「模型采用率 x/y」形状相同，会被成品守卫误拦），
# 但历史数据里可能还有，解析要宽容。
TABLE_CAPTION_RE = re.compile(r"^\*\*表\s*\d+\s*(?:/\s*\d+\s*)?：")
_VALIDATION_QUOTE_RE = re.compile(r"^本表校验状态：")
_NOTE_MARK = "注："


def section_blocks(project_id: str, section: dict, *, subject: str) -> list[dict]:
    """一个章节的 ``blocks``：正文分块 + 该章的系统表格块。

    章节与表格的对应关系由 ``section_key`` 决定（``chapter_table_blocks`` 按章分派）；
    没有系统表格的章节只有正文分块。表格取数失败**不让整章 API 失败**——与
    ``chapter_guard._system_table_block`` 同一取舍：表格问题不阻断正文。
    """
    content = str(section.get("content") or "")
    try:
        blocks = chapter_table_blocks(project_id, str(section.get("section_key") or ""), subject=subject)
    except Exception:
        blocks = []

    notes = {note for block in blocks for note in block.notes}
    captions = {block.title for block in blocks}
    rendered = {block.table_id: _markdown_rows(block) for block in blocks}
    present = {block.table_id: _present_in_content(block, content) for block in blocks}

    output: list[dict] = []
    emitted = False
    in_system_table = False
    in_conflict = False
    for block in content_blocks(content):
        kind = str(block.get("kind") or "")
        text = str(block.get("text") or "")
        if kind == "heading":
            if text.startswith(SYSTEM_TABLE_MARK):
                if not emitted:
                    output.extend(_payloads(blocks, rendered, present))
                    emitted = True
                in_system_table, in_conflict = True, False
                continue
            if text.startswith(CONFLICT_MARK):
                in_system_table, in_conflict = False, True
                continue
            in_system_table = in_conflict = False
            output.append(block)
            continue
        if in_system_table and _system_table_member(kind, block, notes, captions):
            continue
        in_system_table = False
        if in_conflict and _conflict_member(kind, block):
            continue
        in_conflict = False
        output.append(block)

    if not emitted and blocks:
        # 正文里没有表格区块（历史数据、或正文本就缺表）：表格仍然返回，
        # 只是标成 ``present_in_content: false``，由消费方决定如何提示。
        output.extend(_payloads(blocks, rendered, present))
    return output


def _payloads(blocks: list[TableBlock], rendered: dict[str, list[str]], present: dict[str, bool]) -> list[dict]:
    return [
        table_block_payload(
            block, markdown_rows=rendered.get(block.table_id) or [],
            present_in_content=present.get(block.table_id, False),
        )
        for block in blocks
    ]


def table_block_payload(block: TableBlock, *, markdown_rows: list[str], present_in_content: bool) -> dict:
    """``TableBlock`` → API 表格块。除 ``rows``（Markdown 行）外全部取自 ``to_dict()``。"""
    payload = block.to_dict()
    return {
        "kind": "table",
        "table_id": payload["table_id"],
        "table_key": REPORT_TABLE_KEYS.get(payload["table_id"], "") or next(
            (str(cell.locator["table_key"]) for cell in block.cells() if cell.locator.get("table_key")), "",
        ),
        "table_type": payload["table_type"],
        "title": payload["title"],
        "unit": payload["unit"],
        "scope": payload["scope"],
        "origin_mode": payload["origin_mode"],
        "validation_status": payload["validation_status"],
        "columns": payload["columns"],
        "notes": payload["notes"],
        "present_in_content": present_in_content,
        # 前端表格渲染读的是 Markdown 行；结构化数据在 ``table`` 里。
        "rows": list(markdown_rows),
        "table": payload,
    }


def escape_markdown_cell(text: str) -> str:
    """单元格文本的 Markdown 转义。

    竖线是列分隔符、换行是行分隔符——只要单元格原文里出现其一，整张表就会
    「多出几列」甚至退化成段落。这里把竖线转义为 ``\\|``、换行折叠为 ``<br>``，
    保证渲染出来的仍是**一张**结构完整的表。
    """
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(part.strip() for part in normalized.split("\n"))
    return normalized.replace("|", "\\|").replace("\n", "<br>")


def escaped_table_block(block: TableBlock) -> TableBlock:
    """返回一份**单元格原文已转义**的 ``TableBlock`` 副本。

    只改副本，不改冻结快照：渲染版式（表名／单位／表注／校验行）全部复用
    ``render_table_block``，因此数值与证据与网页、Word 完全同版；转义只发生在
    Markdown 输出这一层。
    """
    payload = block.to_dict()
    for row in payload.get("rows", []):
        for cell in row.get("cells", []):
            cell["raw"] = escape_markdown_cell(str(cell.get("raw") or ""))
    return table_block_from_dict(payload)


def render_table_block_markdown(block: TableBlock, *, index: int = 1, total: int = 1) -> str:
    """Markdown 出口的表格渲染：与网页、Word 同源，但单元格已做转义。

    渲染前对每个单元格原文转义，避免 ``|`` 或换行把表格切碎；其余（表名、单位、
    口径、来源、表注、校验行）与 ``report_tables.render_table_block`` 一致。
    """
    return render_table_block(escaped_table_block(block), index=index, total=total)


def _markdown_rows(block: TableBlock) -> list[str]:
    """该表在正文里对应的 Markdown 表格行（表头／分隔／数据行）。

    返回给前端的 ``rows`` 同样要能安全地按 ``|`` 切列，因此与 Markdown 出口共用
    同一套转义渲染。
    """
    return [
        line for line in render_table_block_markdown(block).splitlines()
        if line.strip().startswith("|")
    ]


def _present_in_content(block: TableBlock, content: str) -> bool:
    """表格（表头＋数据行＋表注）是否逐字出现在正文里。

    判据是**去掉表序号后的渲染体**：表序号会随表数变化（同一份数据可能渲染成
    ``表 1/2`` 或 ``表 1/3``），拿整段渲染去比对会出现「数据没变却判成不在正文里」。
    """
    body = _table_body(block)
    return bool(body) and body in content


def _table_body(block: TableBlock) -> str:
    rendered = render_table_block(block, index=1, total=1)
    for part in rendered.split("\n\n", 1)[1:]:
        return part.strip()
    return ""


def _system_table_member(kind: str, block: dict, notes: set[str], captions: set[str]) -> bool:
    """正文区块是否属于系统表格区域（表名行、表体、表注、校验行）。

    正文里的系统表格块由 ``render_table_block`` 生成，形状固定：表名行（``**表 i/n：…**``）、
    表体（Markdown 表格）、表注（``注：…``）、校验行（``> 本表校验状态：…``）。
    按形状识别而不是按内容逐字匹配：事实更新过之后，正文里的仍是旧数值。
    """
    if kind == "table":
        rows = block.get("rows") or []
        return bool(rows) and all(line.strip().startswith("|") for line in rows)
    if kind == "quote":
        return bool(_VALIDATION_QUOTE_RE.match(str(block.get("text") or "").strip()))
    if kind == "paragraph":
        text = str(block.get("text") or "").strip()
        if TABLE_CAPTION_RE.match(text):
            return True
        if _table_body_paragraph(text):
            return True
        return text in notes or text.startswith(_NOTE_MARK)
    return False


def _table_body_paragraph(text: str) -> bool:
    """被换行撑断的 Markdown 表体会退化成段落。

    正文写入侧（``report_tables.render_table_block``）不转义单元格，单元格里一旦有
    换行，表格被切成多行、``content_blocks`` 就不再把它当作 table，而是拼成一个段落。
    这种段落仍然属于系统表格区域，不能让它在导出里以纯文本冒充表格。
    """
    stripped = text.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _conflict_member(kind: str, block: dict) -> bool:
    """正文区块是否属于冲突块（``### 冲突事实…`` 之下的列表／段落）。"""
    if kind == "list":
        items = [str(item) for item in (block.get("items") or [])]
        return bool(items) and any(("披露值" in item) or ("来源：" in item) for item in items)
    if kind == "paragraph":
        text = str(block.get("text") or "")
        return "披露值" in text or text.rstrip().endswith("：")
    return False
