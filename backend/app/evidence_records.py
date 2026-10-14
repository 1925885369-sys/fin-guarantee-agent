"""统一冻结记录（``evidence_records``）：fact / table / metric / rule / disclosure 的统一构建入口。

「三项遗留修复方案」§一：章节校验与后置支持校验必须读取**同一份**冻结记录
（``evidence_records_json``）。此前的"结构化记录"只覆盖受控表格，事实、系统指标与
规则核验是模型同样可见的知识来源，却被后置支持一律判"无证据"（确定性模板、指标句、
规则结论三类误报的主要来源）。「旧MD计划剩余项收口执行方案」§一进一步把**引文里的
披露数字**按证据片段冻结为 ``disclosure`` 记录（增幅/子公司/评级指标/年度表行）。

设计要点（与方案逐条对应）：

- 记录统一字段：``record_id`` / ``record_type`` / 值、单位、原值 / 科目、期间、
  期间类型 / 主体、主体角色、报表口径 / 来源与定位 / ``source_fact_ids``、公式、规则版本 /
  ``status`` ∈ {verified, derived, needs_review, conflict}。
- ``metric`` 必须能回溯到输入事实与公式；只有名称没有计算链路 → ``needs_review``，
  不得作为支持。
- ``rule`` 只能支持规则结论：阈值与规则自身的计算结果可核对，但不为无对应事实的
  业务金额背书（其记录仍受同一条"归属绑定"校验约束）。
- ``disclosure`` 只从引文的"片段结构"（年份表头表格行 / 变动句 / 命名值句）抽取，
  无法唯一绑定（无期间）→ ``needs_review``；不做整页数值池。
- 只有（可追溯的）``verified`` / ``derived`` 记录能作为**支持**；``needs_review`` 记录
  命中时主张降级为待核验；``conflict`` 记录不进任何允许集。
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation

# 记录契约版本：随章节正文一起冻结，接口与审计可见（方案 §一"support_record_version"）。
RECORD_VERSION = "evidence-records-v1"
RECORD_TYPES = ("fact", "table", "metric", "rule", "disclosure")
RECORD_STATUSES = ("verified", "derived", "needs_review", "conflict")
# 事实/指标里的 scope 是英文枚举；表格块里的口径是中文标签。统一成中文口径展示口径，
# 供 ``_record_binding`` 的口径维度直接比对。
_SCOPE_LABELS = {"consolidated": "合并口径", "standalone": "母公司口径"}


def number_unit_hint(key: str) -> str:
    """结构化键名里的单位线索：due_12m_pct → %，max_age_months → 个月，current_ratio → 倍。"""
    text = str(key).lower()
    if "pct" in text or "percent" in text:
        return "%"
    if "month" in text:
        return "个月"
    if "coverage" in text or "ratio" in text or "倍" in text:
        return "倍"
    return ""


def threshold_unit_hint(item: dict) -> str:
    """阈值键名太泛（minimum/maximum/threshold）时，用规则标题补单位线索。"""
    text = f"{item.get('title') or ''}{item.get('name') or ''}{item.get('rule_key') or ''}"
    if any(term in text for term in ("倍数", "覆盖", "保障")):
        return "倍"
    if "率" in text:
        return "%"
    return ""


def _number_text(value) -> str:
    """数值 → 十进制字符串（与表格记录同口径）；无法解析返回空串。"""
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, Decimal):
            decimal_value = value
        else:
            decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ""
    text = format(decimal_value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _record(*, record_id: str, record_type: str, value, unit: str = "", raw: str = "",
            label: str = "", period: str = "", period_type: str = "",
            subject: str = "", subject_role: str = "", scope: str = "",
            source_ids=(), locator: dict | None = None, page=None,
            source_fact_ids=(), formula: str = "", rule_version: str = "",
            origin_type: str = "document", profile_snapshot_id: str = "",
            status: str = "verified", extra: dict | None = None) -> dict:
    record = {
        "record_id": record_id, "record_type": record_type,
        "value": _number_text(value), "unit": str(unit or ""), "raw": str(raw or ""),
        "label": str(label or "").strip(), "period": str(period or ""),
        "period_type": str(period_type or ""),
        "subject": str(subject or "").strip(), "subject_role": str(subject_role or ""),
        "scope": _SCOPE_LABELS.get(str(scope or ""), str(scope or "")),
        "source_ids": [str(item) for item in (source_ids or []) if item],
        "locator": dict(locator or {}), "page": page,
        "source_fact_ids": [str(item) for item in (source_fact_ids or []) if item],
        "formula": str(formula or ""), "rule_version": str(rule_version or ""),
        "origin_type": str(origin_type or "document"),
        "profile_snapshot_id": str(profile_snapshot_id or ""),
        "status": status if status in RECORD_STATUSES else "needs_review",
        "record_version": RECORD_VERSION,
    }
    if extra:
        record.update(extra)
    return record


def _traceable(record: dict) -> bool:
    """可追溯：有来源 ID、冻结主体档案快照，或页级定位（表格记录）。"""
    return bool(record.get("source_ids") or record.get("profile_snapshot_id")
                or record.get("locator") or record.get("page"))


# ---------------------------------------------------------------------------
# 四类记录的构建
# ---------------------------------------------------------------------------


def _fact_records(facts) -> list[dict]:
    records: list[dict] = []
    for fact in facts or []:
        if fact.get("value") is None or fact.get("value") == "":
            continue
        status = "conflict" if str(fact.get("assertion_status") or "") == "conflict" else "verified"
        record = _record(
            record_id=f"fact:{fact.get('id')}", record_type="fact",
            value=fact.get("value"), unit=fact.get("unit", ""), raw=fact.get("raw_value", ""),
            label=fact.get("name") or fact.get("fact_key") or "",
            period=fact.get("period", ""), period_type=fact.get("period_type", ""),
            subject=fact.get("subject", ""), subject_role=fact.get("subject_role", ""),
            scope=fact.get("scope", ""), source_ids=fact.get("source_ids") or [],
            origin_type=fact.get("origin_type", "document"),
            profile_snapshot_id=fact.get("profile_snapshot_id", ""),
            status=status,
            extra={"fact_key": fact.get("fact_key", "")},
        )
        if record["value"] == "":
            continue
        if status == "verified" and not _traceable(record):
            # 无来源、无快照的历史种子事实：不作支持依据，chapter 允许集仍可见其值。
            record["status"] = "needs_review"
        records.append(record)
    return records


def _table_records(table_records) -> list[dict]:
    records: list[dict] = []
    for item in table_records or []:
        value = item.get("value")
        if value is None or value == "":
            continue
        verification = str(item.get("verification") or "ok")
        status = "needs_review" if verification == "needs_review" else "verified"
        records.append(_record(
            record_id=str(item.get("record_id") or f"table:{item.get('cell_id') or id(item)}"),
            record_type="table", value=value, unit=item.get("unit", ""), raw=item.get("raw", ""),
            label=item.get("label", ""), period=item.get("period", ""),
            period_type=item.get("period_type", ""),
            subject=item.get("subject", ""), subject_role=item.get("role", ""),
            scope=item.get("scope", ""), source_ids=item.get("source_ids") or [],
            locator=item.get("locator") or {}, page=item.get("page"),
            origin_type=item.get("origin_mode", "uploaded_table"),
            status=status,
            extra={
                "cell_id": item.get("cell_id", ""), "table_id": item.get("table_id", ""),
                "table_type": item.get("table_type", ""), "fact_key": item.get("fact_key", ""),
            },
        ))
        if records[-1]["value"] == "":
            records.pop()
    return records


def _metric_records(metrics) -> list[dict]:
    records: list[dict] = []
    for index, metric in enumerate(metrics or []):
        value = metric.get("value")
        if value is None or value == "":
            continue
        formula = str(metric.get("formula") or "")
        input_fact_ids = metric.get("input_fact_ids") or []
        if formula and input_fact_ids:
            status = "derived"
        else:
            # 只有指标名称、没有计算链路：不得作为支持（方案 §一）。
            status = "needs_review"
        record = _record(
            record_id=f"metric:{metric.get('metric_key')}:{metric.get('period') or index}",
            record_type="metric", value=value, unit=metric.get("unit", ""),
            label=metric.get("name") or metric.get("metric_key") or "",
            period=metric.get("period", ""), scope=metric.get("scope", ""),
            source_ids=metric.get("source_ids") or [], source_fact_ids=input_fact_ids,
            formula=formula, status=status,
            # 规则版本（计划 §三）：指标按哪一版公式/策略算出，随记录一起冻结。
            rule_version=str(metric.get("rule_version") or ""),
            origin_type="metric",
            extra={
                "metric_key": metric.get("metric_key", ""),
                "metric_status": metric.get("status", ""),
            },
        )
        if record["value"] == "":
            continue
        if status == "derived" and not (record["source_ids"] or record["source_fact_ids"]):
            record["status"] = "needs_review"
        records.append(record)
    return records


def _threshold_aliases(key: str, value_labels: dict[str, str], check: dict) -> list[str]:
    """阈值键 → 中文科目别名：``debt_ratio_pct`` → ``资产负债率``。

    两个来源，按序取：
      ① 权威指标登记表 ``metrics.METRIC_NAMES``（去单位后缀后命中 slug）；
      ② 同规则对象 ``values[].label/name``（有 ``key`` 时按前缀命中）。
    中文正文里的阈值引用（"资产负债率低于70%"）因此能绑定到阈值记录。
    """
    aliases: list[str] = []
    slug = str(key).lower()
    for suffix in ("_pct", "_ratio", "_months", "_count", "_minimum", "_maximum", "_min", "_max"):
        if slug.endswith(suffix):
            slug = slug[: -len(suffix)]
            break
    # ① 权威登记表：METRIC_NAMES 的键可能与 slug 完全同名或为其前缀。
    try:
        from .metrics import METRIC_NAMES
    except Exception:  # pragma: no cover - 防御性
        METRIC_NAMES = {}
    for metric_key, (name, _unit) in METRIC_NAMES.items():
        mk = str(metric_key).lower()
        if slug and (slug == mk or mk.startswith(slug) or slug.startswith(mk)):
            if name not in aliases:
                aliases.append(name)
    # ② 同规则对象 values 的中文标签。
    for entry in check.get("values") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("label") or entry.get("name") or "").strip()
        if not name:
            continue
        entry_key = str(entry.get("key") or "").lower()
        if slug and entry_key and (slug == entry_key or slug.startswith(entry_key)):
            if name not in aliases:
                aliases.append(name)
    return aliases


def _rule_records(checks) -> list[dict]:
    records: list[dict] = []
    for check in checks or []:
        rule_key = str(check.get("rule_key") or "")
        rule_version = str(check.get("rule_version") or "") or RECORD_VERSION
        title = str(check.get("title") or rule_key)
        source_ids = check.get("source_ids") or []
        fact_ids = check.get("source_fact_ids") or check.get("fact_ids") or []
        formula = str(check.get("formula") or "")
        # 阈值的中文科目名：同一规则对象的 ``values`` 已带中文标签（"资产负债率"“流动比率”），
        # 而 ``thresholds`` 的键是英文 slug（debt_ratio_pct / current_ratio）。模型在正文里
        # 写的必然是中文科目名（"资产负债率低于70%"），若阈值记录只有 slug 标签，
        # ``_labels_compatible("资产负债率", "杠杆与流动性 debt_ratio_pct")`` 永远不成立，
        # 正确的阈值引用就被硬判 label_mismatch（实测北投/华辰）。按**同名 slug 前缀**
        # 把 values 的中文标签挂到阈值记录上作别名（debt_ratio_pct ↔ debt_ratio 值）。
        value_labels: dict[str, str] = {}
        for entry in check.get("values") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("label") or entry.get("name") or "").strip()
            if name:
                value_labels[name] = name
        # 阈值：单位线索从键名或规则标题恢复（与章节校验的测量文本同口径）。
        for key, entry in (check.get("thresholds") or {}).items():
            if entry is None or entry == "":
                continue
            hint = number_unit_hint(key) or threshold_unit_hint(check)
            if hint and str(entry).endswith(hint):
                value, unit = str(entry)[: -len(hint)], hint
            else:
                value, unit = entry, hint
            aliases = _threshold_aliases(key, value_labels, check)
            record = _record(
                record_id=f"rule:{rule_key}:threshold:{key}", record_type="rule",
                value=value, unit=unit, label=f"{title} {key}".strip(),
                source_ids=source_ids, source_fact_ids=fact_ids, formula=formula,
                rule_version=rule_version, origin_type="cross_check",
                status="derived",
                extra={"rule_key": rule_key, "result_status": check.get("status", ""),
                       "aliases": aliases},
            )
            if record["value"] != "":
                records.append(record)
        # 规则自身的计算结果（如利息覆盖倍数 1.71）：属于规则结论的一部分。
        for index, entry in enumerate(check.get("values") or []):
            if isinstance(entry, dict):
                number = entry.get("value")
                if number is None or number == "":
                    continue
                hint = str(entry.get("unit") or "") or number_unit_hint(
                    entry.get("name") or entry.get("label") or "")
                record = _record(
                    record_id=f"rule:{rule_key}:value:{index}", record_type="rule",
                    value=number, unit=hint, label=str(entry.get("label") or entry.get("name") or title),
                    source_ids=source_ids, source_fact_ids=fact_ids, formula=formula,
                    rule_version=rule_version, origin_type="cross_check",
                    status="derived",
                    extra={"rule_key": rule_key},
                )
                if record["value"] != "":
                    records.append(record)
            elif isinstance(entry, (int, float)):
                records.append(_record(
                    record_id=f"rule:{rule_key}:value:{index}", record_type="rule",
                    value=entry, label=title, source_ids=source_ids, source_fact_ids=fact_ids,
                    formula=formula, rule_version=rule_version, origin_type="cross_check",
                    status="derived", extra={"rule_key": rule_key},
                ))
    return records


# ---------------------------------------------------------------------------
# 披露数字记录（收口方案 §一："将模型实际收到的披露数字按证据片段冻结为结构化记录，
# 保留主体、期间、单位、科目和来源；无法唯一绑定时待核验，禁止整页同值放行"）
# ---------------------------------------------------------------------------
# 输入 = 本章**发放给模型的引文片段**（evidence 的 quote）。只从三类可唯一绑定的结构中
# 抽取，每个数字都必须落在一条"片段 + 科目 + 期间"上：
#   ① 带年份表头的管道表格行（评级报告财务指标表："指标 | 2023 | 2024 | 2025 | …"）；
#   ② "较上年增加/减少 X 亿元，增幅 Y%" 变动句；
#   ③ "主体+年份+科目+为/同比±X" 命名值句（含 "同比增长 X%"）。
# 抽不出科目名、或所在列不是年度列 → 状态 needs_review（不得算支持，但可接住数字）。
# 不做整页数值池：模式不命中即不登记。

_DISCLOSURE_NOUNS = (
    "净利润", "营业收入", "利润总额", "营业成本", "资产总额", "负债总额", "净资产",
    "总资产", "总负债", "毛利率", "资产负债率",
)
_CHANGE_FACT_RE = re.compile(
    r"(?P<prefix>[\u4e00-\u9fffA-Za-z0-9（）()、/]{0,28}?)"
    r"较\s*(?P<base>20\d{2})\s*年(?:末|度)?\s*"
    r"(?P<verb>增加|减少|增长|下降|上升|回落|提升)\s*"
    r"(?P<value>-?[\d,]+(?:\.\d+)?)\s*"
    r"(?P<unit>亿元|万元|元)"
    r"(?:\s*[，,；;]?\s*(?:增幅|降幅|变动)(?:为)?\s*(?P<pct>-?[\d,]+(?:\.\d+)?)\s*%?)?"
)
_YOY_FACT_RE = re.compile(
    r"(?P<head>[\u4e00-\u9fffA-Za-z0-9（）()]{0,14}?)"
    r"(?P<year>20\d{2})\s*年(?:度)?"
    r"(?P<tail>[^\s，。；：、%|]{2,16}?)\s*"
    r"较\s*20\d{2}\s*年(?:同期)?\s*(?:同比)?\s*"
    r"(?P<verb>增长|下降|增加|减少|上升|回落)\s*"
    r"(?P<value>[\d,]+(?:\.\d+)?)\s*%"
)
_NAMED_VALUE_RE = re.compile(
    r"(?P<head>[\u4e00-\u9fffA-Za-z0-9（）()]{0,12}?)"
    r"(?P<year>20\d{2})\s*年(?:度)?\s*"
    r"(?P<noun>" + "|".join(_DISCLOSURE_NOUNS) + r")\s*为\s*"
    r"(?P<value>-?[\d,]+(?:\.\d+)?)\s*(?P<unit>亿元|万元|元|%|倍)"
)


def _lookup_flat(text: str) -> str:
    """检索用平整文本：去掉全部空白与换行（"101.71 ⏎ 亿元" → "101.71亿元"）。"""
    return re.sub(r"[\s\u3000]+", "", str(text or ""))


def _pipe_cells(line: str) -> list[str]:
    cells = [cell.strip() for cell in str(line or "").split("|")]
    while cells and not cells[0]:
        cells.pop(0)
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _disclosure_label(prefix: str) -> str:
    """从"较/为"之前的前缀里取科目名：剥年份壳、数字残片与叙述词。

    "2024年末发行人存货" → "存货"；"‑15.36 是 京投发展" → "京投发展"（数字残片
    "36" 与标志列"是"会粘在前缀上）。
    """
    cleaned = re.sub(r"[\s\u3000]+", "", str(prefix or ""))
    cleaned = re.sub(r"(?:19|20)\d{2}(?:年度|年末|年底|年)?", "", cleaned)
    cleaned = re.sub(r"^[\d.、:：（）()]+", "", cleaned)
    cleaned = re.sub(r"^(?:是|为|的|了|有)+", "", cleaned)
    cleaned = re.sub(r"^(?:截至|其中|报告期内|报告期各期末|最近三年?|近三年?|以及|和|与|及"
                     r"|发行人|公司|主要子公司|子公司|发行人下属)+", "", cleaned)
    cleaned = re.sub(r"(?:的|为|达|至|由|较|比|末)+$", "", cleaned)
    return cleaned[:40]


def _label_unit(label: str) -> str:
    found = re.search(r"[（(](%|％|倍|X|x|亿元|万元|元)[)）]", str(label or ""))
    if not found:
        return ""
    return {"％": "%", "x": "倍", "X": "倍"}.get(found.group(1), found.group(1))


def _quote_metric_rows(quote: str) -> list[dict]:
    """① 带年份表头的管道表格行：每个数值格 = 一行 (科目, 期间) 记录。"""
    entries: list[dict] = []
    lines = str(quote or "").split("\n")
    for header_index, header in enumerate(lines):
        header_cells = _pipe_cells(header)
        if len(header_cells) < 4:
            continue
        periods: list[str] = []
        for cell in header_cells[1:]:
            year = re.fullmatch(r"(20\d{2})\s*年?", cell)
            periods.append(year.group(1) if year else "")
        if sum(1 for item in periods if item) < 2:
            continue
        for row in lines[header_index + 1: header_index + 40]:
            if not row.strip() or "|" not in row:
                break
            cells = _pipe_cells(row)
            if len(cells) < 3:
                break
            label = cells[0]
            if not any("\u4e00" <= char <= "\u9fff" for char in label):
                break
            for column, period in enumerate(periods):
                cell = cells[column + 1] if column + 1 < len(cells) else ""
                value_match = re.fullmatch(r"(-?[\d,]+(?:\.\d+)?)", cell)
                if not value_match:
                    continue
                entries.append({
                    "label": label, "value": value_match.group(1), "unit": _label_unit(label),
                    "period": period, "raw": f"{label} | {cell}",
                    # 非年度列（"2026.3/2026.1~3"）期间无法唯一绑定 → 待核验。
                    "status": "verified" if period else "needs_review",
                })
        if entries:
            break
    return entries


def _quote_change_facts(flat: str) -> list[dict]:
    """② "较上年增加/减少 X 亿元（，增幅 Y%）" 变动句。"""
    entries: list[dict] = []
    for found in _CHANGE_FACT_RE.finditer(flat):
        label = _disclosure_label(found.group("prefix"))
        if not label:
            continue
        prefix_years = re.findall(r"(?:19|20)\d{2}", found.group("prefix"))
        period = prefix_years[-1] if prefix_years else found.group("base")
        entries.append({
            "label": label, "value": found.group("value"), "unit": found.group("unit"),
            "period": period, "raw": found.group(0)[:120], "status": "verified",
        })
        if found.group("pct"):
            entries.append({
                "label": f"{label}增幅", "value": found.group("pct"), "unit": "%",
                "period": period, "raw": found.group(0)[:120], "status": "verified",
            })
    return entries


def _quote_named_facts(flat: str) -> list[dict]:
    """③ "主体+年份+科目 为/同比±X" 命名值句。"""
    entries: list[dict] = []
    for found in _YOY_FACT_RE.finditer(flat):
        subject = _disclosure_label(found.group("head"))
        tail = re.sub(r"^[的及与和为]", "", str(found.group("tail") or ""))
        label = f"{subject}{tail}"[:40] if tail else ""
        if not label:
            continue
        entries.append({
            "label": label, "value": found.group("value"), "unit": "%",
            "period": found.group("year"), "raw": found.group(0)[:120], "status": "verified",
        })
    for found in _NAMED_VALUE_RE.finditer(flat):
        subject = _disclosure_label(found.group("head"))
        label = f"{subject}{found.group('noun')}"[:40]
        if not label:
            continue
        entries.append({
            "label": label, "value": found.group("value"), "unit": found.group("unit"),
            "period": found.group("year"), "raw": found.group(0)[:120], "status": "verified",
        })
    return entries


def _disclosure_records(evidence) -> list[dict]:
    """把本章引文里的披露数字冻结为结构化记录（按"片段"绑定，不做整页数值池）。"""
    records: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in evidence or []:
        quote = str(item.get("quote") or "")
        if not quote:
            continue
        source_ids = [item.get("source_id")] if item.get("source_id") else []
        page = item.get("page")
        entries = _quote_metric_rows(quote)
        flat = _lookup_flat(quote)
        entries.extend(_quote_change_facts(flat))
        entries.extend(_quote_named_facts(flat))
        for entry in entries:
            label = str(entry.get("label") or "").strip()
            if not label:
                continue
            key = (label, str(entry["value"]), str(entry["unit"]), str(entry["period"]))
            if key in seen:
                continue
            seen.add(key)
            record = _record(
                record_id="disclosure:" + hashlib.sha1(
                    "|".join((*key, *source_ids, str(page))).encode("utf-8")
                ).hexdigest()[:20],
                record_type="disclosure",
                value=str(entry["value"]).replace(",", ""), unit=entry["unit"],
                raw=entry.get("raw", ""), label=label, period=entry["period"],
                source_ids=source_ids, page=page, origin_type="document",
                status=entry.get("status", "verified"),
            )
            if record["value"] == "":
                continue
            records.append(record)
    return records


def build_evidence_records(*, facts=(), metrics=(), checks=(), table_records=(),
                           evidence=()) -> list[dict]:
    """构建章节的统一冻结记录（一次产出）。返回顺序固定：table → fact → metric → rule → disclosure。"""
    records: list[dict] = []
    records.extend(_table_records(table_records))
    records.extend(_fact_records(facts))
    records.extend(_metric_records(metrics))
    records.extend(_rule_records(checks))
    records.extend(_disclosure_records(evidence))
    return records


def records_from_legacy(table_records) -> list[dict]:
    """历史章节迁移：只有 ``table_records_json`` 时按 record_type=table 读入（方案 §一）。"""
    return _table_records(table_records)


# ---------------------------------------------------------------------------
# 允许集与状态
# ---------------------------------------------------------------------------


def matchable_records(records) -> list[dict]:
    """章节校验的匹配集：全部非冲突记录（needs_review 也需可见，命中后按归属判定）。"""
    return [item for item in records or [] if str(item.get("status")) != "conflict"]


def supportable_records(records) -> list[dict]:
    """后置支持的**支持**集：只有可追溯的 verified / derived 记录。"""
    return [
        item for item in records or []
        if str(item.get("status")) in ("verified", "derived") and _traceable(item)
    ]


def review_records(records) -> list[dict]:
    """命中即降级待核验的记录（needs_review；或不可追溯的 verified）。"""
    return [item for item in records or [] if str(item.get("status")) == "needs_review"]


def evidence_number_keys(records) -> set[tuple[str, str]]:
    """把记录折算成 ``(归一化数值, 归一化单位)`` 集合（与 ``chapter_guard`` 完全同口径）。

    与 ``table_evidence.record_number_keys`` 的语义一致：金额按预定义换算表折算到万元，
    并把"带单位"与"同值裸数字"互相登记。差异只在**输入**：这里是四类统一记录。
    """
    from .chapter_guard import _canonical_measurement, _normalize_number

    keys: set[tuple[str, str]] = set()
    for record in records or []:
        value = record.get("value")
        if value is None or value == "":
            continue
        text = str(value)
        unit = str(record.get("unit") or "")
        normalized_value, normalized_unit, _ = _canonical_measurement(text, unit)
        keys.add((normalized_value, normalized_unit))
        if unit:
            keys.add((_normalize_number(text), ""))
    return keys
