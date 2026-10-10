"""表格证据：从受控系统表格抽取**结构化允许记录**（修订稿 §3.1）。

背景
----
章节校验此前只把「系统表格 Markdown 拼成的文本」当成允许数字的来源。系统表格里的
单元格是裸数字，单位写在**表头**（如 ``单位：万元``），于是模型在正文里把单位补上
（``204,709.77万元``）就会被判成「新数字」而整章回退——这是当前最高频的误退原因。

本模块把「数值 + 单位 + 科目 + 期间 + 来源 + 页码 + 坐标」绑成一条结构化记录，
并给出与校验器**完全同口径**的数字比对键。它只负责「记录」，不做单位换算——
换算与比对口径由 :func:`record_number_keys` 复用 ``chapter_guard`` 的既有归一化完成。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# 只有承载业务数据的列才参与数字比对。序号/引用（reference）、科目（subject）、
# 期间名词（period）、说明/资料（disclosure）列里的数字不是业务数值——把它们当数值
# 会把「序号 12」「资料索引 3」误收成允许金额。
VALUE_ROLES = ("period_value", "profile_value", "uploaded_value", "adopted_value")

# 数值类单元格状态：这些格承载可用的业务数值。``adopted`` 是核对表核对后采用的数值，
# ``single_source`` 是单来源披露值，均可作为允许数字。其余状态（not_applicable / blank /
# needs_review / conflict / text）一律不收录：未披露、空值、待核验与冲突占位都不是可用数据。
NUMERIC_STATUSES = ("ok", "zero", "adopted", "single_source")

# 单元格原文里可能残留的单位后缀，解析数值时剥掉（单位另有 cell.unit 承载）。
_UNIT_SUFFIX_RE = re.compile(r"(?:亿元|万元|元|%|％|倍|天|个月|月|年|日|次|人|家|项|股|笔)$")
_NUMBER_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def allowed_table_records(project_id: str, chapter_key: str, *, subject: str) -> list[dict]:
    """从该章的受控系统表格里抽出**结构化允许记录**，供章节校验使用。

    遍历 ``chapter_table_blocks`` 返回的每个 ``TableBlock``，只收录**可用、可追溯的
    业务格**：状态属于数值类、``source_ids`` 非空、``locator`` 里带 ``page``，且能从
    ``normalized`` / ``raw`` 解析出带符号数值。序号/引用列、未披露、空值、待核验、
    冲突占位格一律排除。
    """
    from .report_tables import chapter_table_blocks

    records: list[dict] = []
    for block in chapter_table_blocks(project_id, chapter_key, subject=subject):
        for column_index, column in enumerate(block.columns):
            if column.role not in VALUE_ROLES:
                continue
            period = _period_of(column.label)
            for row_index, row in enumerate(block.rows, start=1):
                cell = row.cell(column_index)
                if cell.status not in NUMERIC_STATUSES:
                    continue
                locator = cell.locator or {}
                page = locator.get("page")
                if not cell.source_ids or page in (None, ""):
                    # 不可追溯的业务格不得成为允许数字：没有来源、没有页码的数据
                    # 与「凭空出现的数字」无从区分。
                    continue
                value = _numeric_value(cell)
                if value is None:
                    continue
                records.append({
                    "value": value,
                    "raw": cell.raw,
                    # 单位优先取单元格（其值为逐格解析后的单位）；单元格没有时回退到列声明
                    # 的单位——「单位写在表头」正是本模块要解决的场景，列单位就是表头单位。
                    "unit": cell.unit or column.unit,
                    "fact_key": cell.fact_key or "",
                    "label": _row_label(block, row),
                    "subject": block.subject,
                    "scope": block.scope,
                    "period": period,
                    "source_ids": tuple(cell.source_ids),
                    "page": page,
                    "bbox": locator.get("bbox"),
                    "cell_id": f"{block.table_id}:r{row_index:02d}c{column_index:02d}",
                    "origin_mode": block.origin_mode,
                    "table_id": block.table_id,
                    "table_type": block.table_type,
                })
    return records


def record_number_keys(records: list[dict]) -> set[tuple[str, str]]:
    """把记录折算成 ``(normalized_value, normalized_unit)`` 集合，用于数字比对。

    复用 ``chapter_guard`` 的既有归一化，保证与校验器**完全同口径**：千分位、小数尾零、
    展示精度一致，且金额按预定义换算表折算到万元（``48000万元 ≡ 4.8亿元``）。

    额外把「带声明单位的金额」与「同值裸数字」互相登记：表格里单元格是裸数字、单位在
    表头（``单位：万元``），模型在正文写 ``204,709.77万元`` 或写裸 ``204,709.77`` 都
    必须能匹配上。只做**严格等值**登记，量级换算仍交给 ``_canonical_measurement``。
    """
    # 局部导入避免模块循环导入（chapter_guard 顶层会导入 report_tables）。
    from .chapter_guard import _canonical_measurement, _normalize_number

    keys: set[tuple[str, str]] = set()
    for record in records:
        value = record.get("value")
        if value is None or value == "":
            continue
        text = str(value)
        unit = str(record.get("unit") or "")
        normalized_value, normalized_unit, _ = _canonical_measurement(text, unit)
        keys.add((normalized_value, normalized_unit))
        if unit:
            # 同值裸数字：单位写在表头、正文只写裸数字时也要能匹配上。
            keys.add((_normalize_number(text), ""))
    return keys


def _row_label(block, row) -> str:
    """科目名：优先取科目列单元格原文，回退到行键。"""
    if block.columns and block.columns[0].role == "subject" and row.cells:
        text = (row.cells[0].raw or "").strip()
        if text:
            return text
    return row.row_key


def _period_of(label: str) -> str:
    """从列标签推断期间，如 ``2025 年末``→``2025``、``2025 年度``→``2025``；取不到留空。"""
    match = _YEAR_RE.search(label or "")
    return match.group(0) if match else ""


def _numeric_value(cell):
    """从 ``cell.normalized`` 或 ``cell.raw`` 解析带符号数值；解析不出返回 ``None``。

    ``normalized`` 是规范化后的数值字符串，优先使用；``raw`` 可能带千分位或单位后缀。
    负号必须保留。
    """
    for candidate in (cell.normalized, cell.raw):
        if candidate is None:
            continue
        text = str(candidate).strip()
        if not text:
            continue
        compact = re.sub(r"[,\s]", "", text).replace("，", "")
        compact = _UNIT_SUFFIX_RE.sub("", compact)
        if not _NUMBER_RE.match(compact):
            continue
        try:
            number = Decimal(compact)
        except (InvalidOperation, ValueError):
            continue
        # 整数值返回 int，带小数返回 float——两者都保留符号。
        return int(number) if number == number.to_integral_value() else float(number)
    return None
