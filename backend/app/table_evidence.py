"""表格证据：从受控系统表格抽取**结构化允许记录**（修订稿 §3.1、第三点细化方案 §二.1）。

背景
----
章节校验此前只把「系统表格 Markdown 拼成的文本」当成允许数字的来源。系统表格里的
单元格是裸数字，单位写在**表头**（如 ``单位：万元``），于是模型在正文里把单位补上
（``204,709.77万元``）就会被判成「新数字」而整章回退——这是当前最高频的误退原因。

本模块把「数值 + 单位 + 科目 + 期间 + 来源 + 页码 + 坐标」绑成一条结构化记录，
并给出与校验器**完全同口径**的数字比对键。

记录契约（第三点细化方案 §二.1）
--------------------------------
- ``record_id``：稳定标识（单元格地址 + 解析版本）；
- ``value``：**Decimal 解析、序列化输出十进制字符串**（如 ``"4405922.45"``），
  科学计数法按**一个**数值解析、负号保留、尾零不丢；不得出现 ``float``；
- ``unit``：按「单元格 → 行科目 → 列 → 整表」依次继承，仅限已识别的业务数值格；
  无法唯一确定时 ``verification="needs_review"``；
- 标准科目 ``subject_key``（取 ``fact_key``，无则空）与原始名称 ``label``；
- ``period`` + ``period_type``（列标签推断：年末/年度→``annual``、半年度→``interim`` 等）；
- ``subject`` + ``role``（主体角色，能推就推、推不出留空）；
- ``scope``、``source_ids``、``locator``（page/bbox）、``raw``、``parser_version``、
  ``verification``。

安全边界
--------
- 只收录**可用、可追溯**的业务格：状态属于数值类、来源属于本项目；
- PDF（``uploaded_table``）记录必须有页码与坐标且与取值同格；Word/Excel/主体快照用各自
  定位，不强迫伪造 PDF 页码；
- 排除序号/说明/期间名词列、空值、未披露、待核验与冲突占位格。

本模块只负责「记录」，不做单位换算——换算与比对口径由 :func:`record_number_keys`
复用 ``chapter_guard`` 的既有归一化完成。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# 记录结构/解析版本：进入每条记录，便于按版本隔离历史。
PARSER_VERSION = "table-evidence-v2"

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
# 数值：整数/小数，**可选科学计数法指数**；负号保留。科学计数法必须整体成一个数值，
# 否则下游按「多个数字」拆分（``1.2E+5`` 被拆成 ``1.2`` 与 ``5``）会误判。
_NUMBER_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
# 行科目标签里可继承的单位（金额/比例/倍数）；刻意不含「年/月」这类会误命中的词。
_LABEL_UNIT_RE = re.compile(r"亿元|万元|元|%|％|倍")

# 需要「有来源」才算可追溯：这些都是业务数值格的状态。
_UNICODE_MINUS = ("\u2212", "\uff0d", "\u2013")  # − ／ － ／ –


def allowed_table_records(project_id: str, chapter_key: str, *, subject: str) -> list[dict]:
    """从该章的受控系统表格里抽出**结构化允许记录**，供章节校验使用。

    遍历 ``chapter_table_blocks`` 返回的每个 ``TableBlock``，只收录**可用、可追溯的
    业务格**：状态属于数值类、``source_ids`` 属于本项目、符合各来源模式的定位要求，
    且能从 ``normalized`` / ``raw`` 解析出带符号数值。序号/引用列、未披露、空值、
    待核验、冲突占位格一律排除。
    """
    from .report_tables import chapter_table_blocks

    blocks = chapter_table_blocks(project_id, chapter_key, subject=subject)
    return records_from_blocks(blocks, project_id=project_id)


def records_from_blocks(
    blocks, *, project_id: str = "", source_ids: set[str] | None = None,
    subject_roles: dict[str, str] | None = None,
) -> list[dict]:
    """从**同一份** ``TableBlock`` 集合构建结构化记录（三件套之一）。

    ``source_ids`` / ``subject_roles`` 允许调用方注入（一次查询后复用，避免逐格回查）；
    为 ``None`` 时各自惰性查询一次。``source_ids`` 为 ``None``（含查询失败）时不做来源
    归属过滤；为空集时视为「该项目没有可用来源」，从严拒绝全部来源。
    """
    if source_ids is None:
        source_ids = project_source_ids(project_id)
    if subject_roles is None:
        subject_roles = subject_role_labels(project_id)

    records: list[dict] = []
    for block in blocks:
        role = subject_roles.get(_normalize_subject(block.subject), "")
        for column_index, column in enumerate(block.columns):
            if column.role not in VALUE_ROLES:
                continue
            period = _period_of(column.label)
            period_type = _period_type(column.label)
            for row_index, row in enumerate(block.rows, start=1):
                cell = row.cell(column_index)
                if cell.status not in NUMERIC_STATUSES:
                    continue
                value = _numeric_value(cell)
                if value is None:
                    continue
                locator = dict(cell.locator or {})
                if not _locator_ok(block, cell, locator):
                    # PDF 记录必须页码与坐标齐全；缺定位的业务格不得成为允许数字。
                    continue
                valid_sources = _valid_sources(cell.source_ids, source_ids)
                if not valid_sources:
                    # 不可追溯的业务格不得成为允许数字：没有来源、来源不属于本项目的数据
                    # 与「凭空出现的数字」无从区分。
                    continue
                unit, unit_ambiguous = _resolve_unit(block, row, column, cell, column_index=column_index)
                cell_id = f"{block.table_id}:r{row_index:02d}c{column_index:02d}"
                fact_key = cell.fact_key or ""
                records.append({
                    "record_id": f"{cell_id}@{PARSER_VERSION}",
                    # 十进制字符串而非 float：JSON round-trip 后仍是 str，尾零/精度不丢。
                    "value": _decimal_text(value),
                    "raw": cell.raw,
                    "unit": unit,
                    # 标准科目（事实键）与原始名称（行标签）。
                    "subject_key": fact_key,
                    "fact_key": fact_key,
                    "label": _row_label(block, row),
                    "period": period,
                    "period_type": period_type,
                    "subject": block.subject,
                    "role": role,
                    "scope": block.scope,
                    "source_ids": valid_sources,
                    # locator 是取值所在格的定位（page/bbox 等）；page/bbox 另留平铺字段兼容旧消费方。
                    "locator": locator,
                    "page": locator.get("page"),
                    "bbox": locator.get("bbox"),
                    "cell_id": cell_id,
                    "origin_mode": block.origin_mode,
                    "table_id": block.table_id,
                    "table_type": block.table_type,
                    "parser_version": PARSER_VERSION,
                    "verification": "needs_review" if unit_ambiguous else "ok",
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


# ---------------------------------------------------------------------------
# 项目范围查询（一次查询、复用；查询失败返回 None 表示「无法校验」）
# ---------------------------------------------------------------------------


def project_source_ids(project_id: str) -> set[str] | None:
    """该项目可用的来源 ID 集合（``sources`` 表 + 绑定到项目的主体档案来源）。

    查询失败（如隔离库未初始化）返回 ``None``，调用方据此跳过来源归属过滤，避免把
    可用记录一律误伤；返回空集表示项目确实没有来源，此时从严拒绝。
    """
    try:
        from .database import rows

        ids = {str(item["id"]) for item in rows(
            "SELECT id FROM sources WHERE project_id=?", (project_id,),
        )}
    except Exception:
        return None
    try:
        for item in rows(
            "SELECT s.id AS id FROM entity_profile_sources s "
            "JOIN project_profile_roles r ON r.profile_id=s.profile_id WHERE r.project_id=?",
            (project_id,),
        ):
            ids.add(str(item["id"]))
    except Exception:
        pass
    return ids


def subject_role_labels(project_id: str) -> dict[str, str]:
    """主体名 → 角色中文标签（发行人/担保人）。查不到返回空字典（角色留空）。"""
    try:
        from .database import rows

        records = rows(
            "SELECT r.role AS role,p.name AS name FROM project_profile_roles r "
            "JOIN entity_profiles p ON p.id=r.profile_id WHERE r.project_id=?",
            (project_id,),
        )
    except Exception:
        return {}
    labels = {"issuer": "发行人", "guarantor": "担保人"}
    output: dict[str, str] = {}
    for item in records:
        name = _normalize_subject(item.get("name"))
        if name:
            output[name] = labels.get(str(item.get("role") or ""), str(item.get("role") or ""))
    return output


def _normalize_subject(name: object) -> str:
    return str(name or "").strip()


# ---------------------------------------------------------------------------
# 单元格解析
# ---------------------------------------------------------------------------


def _row_label(block, row) -> str:
    """科目名：取**名称类列**的单元格原文（科目列优先，其次名称/机构等 caption 列）。

    序号列（role=reference）与数值列不参与。表格第一列若是序号列，取它会得到
    "1"、"2" 这类标签——纯数字标签进 `_record_binding` 词表后，子串匹配会命中
    正文里任何含该数字的句子，把科目正确的数字误判成 label_mismatch（北投实测）。
    全无名称类列时回退到行键。
    """
    if not row.cells:
        return row.row_key
    for index, column in enumerate(block.columns or ()):
        if column.role not in ("subject", "disclosure") or index >= len(row.cells):
            continue
        text = (row.cells[index].raw or "").strip()
        if text:
            return text
    return row.row_key


def _period_of(label: str) -> str:
    """从列标签推断期间，如 ``2025 年末``→``2025``、``2025 年度``→``2025``；取不到留空。"""
    match = _YEAR_RE.search(label or "")
    return match.group(0) if match else ""


def _period_type(label: str) -> str:
    """从列标签推断期间类型：半年度/中期→``interim``、季度→``quarterly``、
    月度→``monthly``、年末/年度→``annual``；推不出留空。"""
    text = label or ""
    if re.search(r"半年度|中期|上半年|半年", text):
        return "interim"
    if re.search(r"季度|季", text):
        return "quarterly"
    if re.search(r"月度|月末|当月", text):
        return "monthly"
    if re.search(r"年末|年度|年", text):
        return "annual"
    return ""


def _numeric_value(cell) -> Decimal | None:
    """从 ``cell.normalized`` 或 ``cell.raw`` 解析带符号数值；解析不出返回 ``None``。

    ``normalized`` 是规范化后的数值字符串，优先使用（保留 Decimal 精度与尾零）；
    ``raw`` 可能带千分位、Unicode 负号或单位后缀。返回 ``Decimal``，负号必须保留。
    """
    for candidate in (cell.normalized, cell.raw):
        compact = _numeric_text(candidate)
        if compact is None:
            continue
        try:
            return Decimal(compact)
        except (InvalidOperation, ValueError):
            continue
    return None


def _numeric_text(candidate: object) -> str | None:
    """把单元格候选串清理成可解析的数值串；不可解析返回 ``None``。

    清理：去空白/千分位、把 Unicode 负号与全角负号归一成 ASCII ``-``、剥掉单位后缀。
    """
    if candidate is None:
        return None
    text = str(candidate).strip()
    if not text:
        return None
    for minus in _UNICODE_MINUS:
        text = text.replace(minus, "-")
    compact = re.sub(r"[,\s，]", "", text)
    compact = _UNIT_SUFFIX_RE.sub("", compact)
    return compact if _NUMBER_RE.match(compact) else None


def _decimal_text(value: Decimal) -> str:
    """Decimal → 十进制字符串：不用科学计数法、保留尾零（``96717233.20`` 不丢尾零）。"""
    return format(value, "f")


# ---------------------------------------------------------------------------
# 定位与来源
# ---------------------------------------------------------------------------


def _locator_ok(block, cell, locator: dict) -> bool:
    """定位是否满足该来源模式的要求。

    - PDF 原表（``origin_mode == "uploaded_table"``）：必须同时有页码与坐标；
    - 其它上传格（``origin_type == "uploaded_cell"``，如逐份材料命中的单元格）：必须有页码；
    - Word/Excel/主体快照（``profile_facts`` / ``merged``）：用各自定位，不强迫 PDF 页码。
    """
    if block.origin_mode == "uploaded_table":
        return locator.get("page") not in (None, "") and bool(locator.get("bbox"))
    if cell.origin_type == "uploaded_cell":
        return locator.get("page") not in (None, "")
    return True


def _valid_sources(cell_sources, source_index: set[str] | None) -> tuple[str, ...]:
    """去重后的来源 ID；空或（在给定来源集合内）不属于本项目时返回空元组。"""
    ids = tuple(dict.fromkeys(str(source) for source in (cell_sources or ()) if str(source)))
    if not ids:
        return ()
    if source_index is None:
        # 无法取得项目来源清单：不启用归属过滤（避免把可用记录一律误伤）。
        return ids
    return tuple(source for source in ids if source in source_index)


# ---------------------------------------------------------------------------
# 单位继承链
# ---------------------------------------------------------------------------


# 整表单位注解里的单位族：原文常写「单位：万元、%」（金额列万元、占比列 %）。
_TABLE_MONEY_UNITS = ("亿元", "万元", "元")
_TABLE_RATIO_UNITS = ("%", "％", "‰", "倍")
_COLUMN_MONEY_HINT_RE = re.compile(r"金额|余额|额度|规模|本金|面值|总额|净额|净值|价值")
_COLUMN_RATIO_HINT_RE = re.compile(r"占比|比例|比率|率|%|％|‰|倍数")
# 列标签未恢复时的占位（见 ``table_block.business_column_role`` 的「列待核验」）。
_COLUMN_PLACEHOLDER_LABELS = ("", "列待核验")
# 占比/率列的数值不会超过 100；双族注解、列名又无提示时用它区分金额列。
_RATIO_MAX_PLAUSIBLE = Decimal(100)


def _column_caption(block, column, column_index: int | None) -> str:
    """列的语义标题：列标签为空/占位时，从该列在表内的**文本格**恢复（续表重复表头）。

    列标签恢复失败的列（占位「列待核验」）若直接拿整表注解取首族，会把占比列判成金额列
    （北投存货表的占比列实测）。带换行等杂字符的恢复结果不可信，落回占位。
    """
    label = str(getattr(column, "label", "") or "").strip()
    if label not in _COLUMN_PLACEHOLDER_LABELS:
        return label
    if column_index is None:
        return label
    for row in getattr(block, "rows", ()) or ():
        try:
            cell = row.cell(column_index)
        except (IndexError, AttributeError):
            continue
        if cell is None or str(getattr(cell, "status", "")) != "text":
            continue
        text = str(cell.raw or "").strip()
        if text and "\n" not in text:
            return text
    return label


def _column_numeric_values(block, column_index: int | None) -> list[Decimal]:
    """该列全部可解析数值（用于双族注解下的形态兜底判断）。"""
    if column_index is None:
        return []
    values: list[Decimal] = []
    for row in getattr(block, "rows", ()) or ():
        try:
            cell = row.cell(column_index)
        except (IndexError, AttributeError):
            continue
        if cell is None:
            continue
        try:
            value = Decimal(str(cell.normalized if cell.normalized not in (None, "") else cell.raw).replace(",", ""))
        except (InvalidOperation, ValueError):
            continue
        values.append(value)
    return values


def _table_unit_for(annotation: str, column_label: str,
                    column_values: list[Decimal] | None = None) -> tuple[str, bool]:
    """整表单位注解 → 本列单位；多族合并注解按列名语义取一族，取不出标记待核验。

    列名无提示时再用**该列数值形态**兜底：整列数值均 ≤ 100 → 占比/率列（"100.00" 是
    占比合计的常见写法）；出现大于 100 的数值 → 金额列。两者都不是精确判定，
    一律标注待核验。
    """
    tokens = [token for token in re.split(r"[、,，/／;；\s]+", annotation or "") if token]
    if not tokens:
        return "", True
    if len(set(tokens)) == 1:
        return tokens[0], False
    money = [token for token in tokens if token in _TABLE_MONEY_UNITS]
    ratio = [token for token in tokens if token in _TABLE_RATIO_UNITS]
    money_hint = bool(_COLUMN_MONEY_HINT_RE.search(column_label or ""))
    ratio_hint = bool(_COLUMN_RATIO_HINT_RE.search(column_label or ""))
    if len(money) == 1 and money_hint and not ratio_hint:
        return money[0], False
    if len(ratio) == 1 and ratio_hint and not money_hint:
        return ratio[0], False
    if len(money) == 1 and len(ratio) == 1 and money[0] == "万元" and ratio[0] == "%" and column_values:
        if all(value <= _RATIO_MAX_PLAUSIBLE for value in column_values):
            return ratio[0], True
        return money[0], True
    return tokens[0], True


def _resolve_unit(block, row, column, cell, *, column_index: int | None = None) -> tuple[str, bool]:
    """按「单元格 → 行科目 → 列 → 整表」确定单位，返回 ``(单位, 是否歧义)``。

    三个层级**依次继承**（第三点细化方案 §二.1）：先出现的层级确定性胜出，不再把
    "列写明单位、整表也写单位"当成冲突。只在行科目与列标注同时给出**不同**单位时
    算真冲突（保留更具体的行级单位并标记歧义）。整表注解可能是多族合并写法
    （原文「单位：万元、%」），按列名语义取一族；取不出唯一族才标记待核验。

    行科目名**完全没写单位**时（"应收账款周转率""流动比率"）查**科目登记**
    （计划 §三：结合科目登记/表注/公式恢复单位，"次""倍"这类量纲由科目名本身决定）。
    只在标签里**一个单位记号都没有**时才启用登记——标签写了「（亿元、%）」属于并列
    两族，仍按歧义处理，不得被登记表"修好"。**绝不按数值大小猜单位**。
    """
    if cell.unit:
        return cell.unit, False
    row_label = _row_label(block, row)
    row_unit = _label_unit(row_label)
    if not row_unit and not _LABEL_UNIT_RE.search(row_label or ""):
        row_unit = _dimension_unit(row_label)
    column_unit = _label_unit(str(getattr(column, "unit", "") or ""))
    if row_unit and column_unit and row_unit != column_unit:
        return row_unit, True
    if row_unit:
        return row_unit, False
    if column_unit:
        return column_unit, False
    return _table_unit_for(
        str(getattr(block, "unit", "") or ""),
        _column_caption(block, column, column_index),
        _column_numeric_values(block, column_index),
    )


def _label_unit(label: str) -> str:
    """从标签里取唯一单位（金额/比例/倍数）；出现多种或没有时返回空。"""
    found = _LABEL_UNIT_RE.findall(label or "")
    return found[0] if len(set(found)) == 1 else ""


# 科目登记：行科目名的**量纲词尾** → 单位。原文没写单位后缀、但科目名本身已经确定量纲
# （"应收账款周转率"必是"次"，"速动比率"必是"倍"）时唯一可用的依据。
# 顺序敏感：`周转天数`/`周转率` 必须排在泛化的 `率` 之前——周转次数的单位是「次」，
# 不是「%」。不在表里的词尾一律返回空（不猜）。
_LABEL_DIMENSION_UNITS = (
    ("周转天数", "天"),
    ("周转率", "次"),
    ("保障倍数", "倍"),
    ("覆盖倍数", "倍"),
    ("流动比率", "倍"),
    ("速动比率", "倍"),
    # 2026-10-15 回退章复核：语义明确的百分比词尾（表行名无单位记号时恢复）——
    # "营业净利率"曾按表头"万元"标注，正文"5.80%"匹配不上（中邮 financial 实测）。
    ("净利率", "%"),
    ("毛利率", "%"),
    ("资产负债率", "%"),
    ("资本化率", "%"),
    ("集中度", "%"),
    ("占比", "%"),
    ("比例", "%"),
)


def _dimension_unit(label: str) -> str:
    """科目登记查表：按行科目名的量纲词尾取单位，查不到返回空。"""
    text = re.sub(r"\s+", "", label or "")
    if not text:
        return ""
    for suffix, unit in _LABEL_DIMENSION_UNITS:
        if text.endswith(suffix):
            return unit
    return ""
