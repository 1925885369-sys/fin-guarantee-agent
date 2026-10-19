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
RECORD_VERSION = "evidence-records-v2"
# v2 新增 `calculation`：模型自主计算、经系统复算通过的派生记录（风控约束分层 §三.2）。
RECORD_TYPES = ("fact", "table", "metric", "rule", "disclosure", "calculation")
RECORD_STATUSES = ("verified", "derived", "needs_review", "conflict")
# 事实/指标里的 scope 是英文枚举；表格块里的口径是中文标签。统一成中文口径展示口径，
# 供 ``_record_binding`` 的口径维度直接比对。
_SCOPE_LABELS = {"consolidated": "合并口径", "standalone": "母公司口径"}


def number_unit_hint(key: str) -> str:
    """结构化键名/中文科目里的单位线索：due_12m_pct → %，max_age_months → 个月，current_ratio → 倍。

    中文补充（2026-10-15 回退章复核）：规则结果值的 ``label`` 是**中文科目名**
    （"到期集中度""逾期应收占比""其他应收/净资产"），原实现只认英文键 → 单位线索为空
    → 记录 unit=""，正文写"66.1%"/"13.3%"永远匹配不上空单位记录（华辰 debt/governance
    回退的判定侧根因）。只收语义明确的词，不碰裸"率"（周转率=次、资产负债率=% 混在
    同一后缀里，裸词会互相误伤）。
    """
    text = str(key).lower()
    if "pct" in text or "percent" in text:
        return "%"
    if "month" in text:
        return "个月"
    if "coverage" in text or "ratio" in text or "倍" in text:
        return "倍"
    if any(term in text for term in ("占比", "比例", "百分比", "集中度")):
        return "%"
    # "覆盖率"是**明确的百分比**语义（"到期本金现金流覆盖率45%"；同规则阈值键
    # minimum_coverage_pct=100% 同口径）——2026-10-16 华辰 debt #20 实测：单位线索为空
    # → 记录 unit=""，正文"45.0%"永远绑不上。仍不碰裸"率"（见上）。
    if "覆盖率" in text:
        return "%"
    if "/" in text:  # 「A/净资产」「A/总资产」式的比率科目
        return "%"
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
    """可追溯：有来源 ID、冻结主体档案快照、页级定位（表格记录），
    或**输入链 + 公式**（派生计算）。

    ⚠️ 必须有 `formula` 才认 `source_fact_ids`：裸 ID 列表不足以证明"这个值是怎么算出来的"，
    认了会让任意记录靠拼 ID 混进"可支持"集。metric/calculation 两类都走这条。
    """
    if record.get("source_ids") or record.get("profile_snapshot_id") \
            or record.get("locator") or record.get("page"):
        return True
    return bool(record.get("source_fact_ids") and str(record.get("formula") or "").strip())


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
    try:
        from .metrics import METRIC_LABEL_ALIASES
    except Exception:  # pragma: no cover - 防御性
        METRIC_LABEL_ALIASES = {}
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
                # 叙述别名（正文把指标写成说明性短语时的绑定用词）：随记录**一起冻结**，
                # 词表侧按别名子串匹配——避免"可用资金对未来12个月到期债务的覆盖"这类
                # 说明句因配不上规范名被判假 label_mismatch（华辰 financial #28 实测）。
                "label_aliases": list(
                    METRIC_LABEL_ALIASES.get(str(metric.get("metric_key") or ""), ())
                ),
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
    # 2026-10-15 回退章复核扩展：单值披露的实际用语（"经营活动现金流量净额9,000万元"
    # "银行借款余额为0.00万元"）；发行条款单值（"票面利率4.8%""期限3年"——华辰 debt
    # #14 实测：条款句因利率/期限无记录被判无支持）。
    "净额", "余额", "利率", "期限",
    # 2026-10-16 旧计划剩余项复验扩展：单值披露科目词（北投 operations #5/#6 "实现…收入
    # 81.34亿元/117.76亿元"；北投 debt #12 "1年以内银行贷款131.15亿元…债券…债务"；
    # 中邮 debt #8/#9 "有息债务为103.07亿元""资产款为72.92亿元"；华辰 operations #21
    # "反担保折后价值16,000万元"；北投 debt #21 "信用借款规模3,094.36亿元"）。
    # 均为**科目名结尾词**，仍要求前面有数值 + 单位，模式不命中即不登记（宁漏不错）。
    "收入", "贷款", "债券", "债务", "价值", "资产款", "规模", "比例", "上限",
    # 2026-10-16 第三轮试跑复核扩展："占比"（中邮 financial "实收资本和资本公积合计
    # 占比为81.61%"——"比例"覆盖不到"占比"这一措辞；同样要求前面有数值+单位）。
    "占比",
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
    r"(?:(?P<year>20\d{2})\s*年(?:度)?\s*)?"
    r"(?P<noun>" + "|".join(_DISCLOSURE_NOUNS) + r")\s*"
    # 连接词：省略/为/的/达/占（"占主营业务收入的43.36%"——名词后跟"的"再数值，
    # 2026-10-16 北投 operations #5 实测；原模式只认可省"为"，漏这类比率）。
    # 2026-10-16 追加"名词 + 之和/合计/总额/小计 + 为"形态（"…其他公司信用类债券余额
    # **之和为**20.12亿元"——中邮 debt #4 实测：名词与数值间夹了汇总词，原模式漏抽）。
    r"(?:之和|合计|总额|小计)?\s*"
    r"(?:为|的|达|占)?\s*"
    r"(?P<value>-?[\d,]+(?:\.\d+)?)\s*(?P<unit>亿元|万元|元|%|倍|年)"
)
# ④ "…分别为 A、B 和 C" 系列句（2026-10-15 回退章复核新增）：投资活动现金流净额、
#    有息负债余额、覆盖倍数等"最近三年分别为…"的披露形态——原三类模式都不覆盖
#    （北投 cashflow -497.09 亿系列、中邮 debt 79.83% 系列、中邮 financial 9803.03 倍
#    实测，均为"引文有、记录无"的记录覆盖缺口）。
_SERIES_FACT_RE = re.compile(
    r"(?P<head>[\u4e00-\u9fffA-Za-z0-9（）()、/]{2,30}?)"
    # 触发词：既有"分别/依次/均/各" + 2026-10-16 复验扩展"占比/比例/比重"（"占经营活动
    # 现金流出的比例为10.11%、19.26%和14.76%"——第二段系列句用"比例为"而非"分别为"，
    # 原模式漏抽，北投 operations #13 实测）。后接"为"（可省）与值枚举段。
    r"(?:分别|依次|均|各|占比|比例|比重)\s*(?:为)?\s*"
    r"(?P<values>(?:-?\d[\d,]*(?:\.\d+)?\s*(?:亿元|万元|元|%|％|倍)\s*(?:[、，,]?\s*)?(?:和|及)?\s*){2,8})"
)
_SERIES_VALUE_RE = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|元|%|％|倍)")
# 系列句续项：单值句的值之后紧跟 "、19.26%" / "和14.76%" / "及55.36亿元" 形态 → 该值是
# 枚举首项（不是独立命名值），交 ④ 系列抽取处理。
_SERIES_CONTINUATION_RE = re.compile(r"\s*(?:[、，,]|和|及|与)\s*-?\d")
# ⑩ 汇总句与占比句（2026-10-16 第四轮试跑复核新增）：两类"引文有、记录无"的常见形态
#    （中邮 cashflow #20 实测）——
#    ① 汇总：`…金融投资等高流动性资产，合计达169.02亿元`（科目与汇总词之间夹逗号，
#       既有 ③ 命名值句的连接词位不含标点 → 整句漏抽）；
#    ② 占比：`占总资产（扣除代理买卖证券款）的比重达73.55%`（"占…的比重"整段是科目，
#       不是"比例"类名词后缀 → 既有 ③ 覆盖不到）。
#    两者都只在**科目短语 ≥4 字**时登记，避免把"合计达…"抽成无主语的裸记录。
_AGGREGATE_VALUE_RE = re.compile(
    r"(?P<head>[^，。；、]{2,40})[，,]?\s*(?:合计|总计|共计|累计)\s*(?:达|为|至)\s*"
    r"(?P<value>-?[\d,]+(?:\.\d+)?)\s*(?P<unit>亿元|万元|元)"
)
_COVERAGE_VALUE_RE = re.compile(
    r"(?P<label>占[^，。；]{2,40}?的?(?:比重|比例|占比))\s*(?:达|为|是)?\s*"
    r"(?P<value>-?[\d,]+(?:\.\d+)?)\s*(?P<unit>%|％)"
)

# ⑤ "科目：X 单位" 冒号式单值（"反担保折后价值：16,000万元"，华辰 guarantee 实测）。
_COLON_VALUE_RE = re.compile(
    r"(?P<head>[^，。；、\s|:：]{2,20})[：:]\s*"
    r"(?P<value>-?[\d,]+(?:\.\d+)?)\s*(?P<unit>亿元|万元|元|%|％|倍)"
)

# ⑥ 空格分列的表格行（2026-10-16 旧计划剩余项复验新增）：现金流量表等 PDF 引文以
#    "科目 值1 值2 值3"**空格**分列（**无 Markdown 管道**），`_quote_metric_rows` 只认
#    "|"表格 → 一类完全不落记录。样本：北投 operations 现金流量表（1,749,719.14 万元系列）、
#    北投 debt / 中邮 debt / 华辰 financial 的数值表（实测每章尚有 16~87 个数字未成记录，
#    是当前剩余回退的**最大缺口**）。
#    保守条件（宁漏不错）：
#      - 行首为**含中日韩字符、无数字**的科目标签（2~40 字），后接**纯数字 token**（含千分位/小数/负号）；
#      - 该行数字 token ≥2（单值行交既有 ③ 命名值句处理，避免与叙述句混淆）；
#      - **连续两行**同构才算表格（叙述段落罕见连续两行"纯标签+纯数字串"）；
#      - 单位：**缺省置空**（"绝不按数值大小猜单位"）——空白单位在 `_record_matches_mention`
#        不否决同值匹配，且 blank period 使期间过滤跳过，交由值+科目+主体绑定。
#    期间：本行无年份表头可绑 → period==""（needs_review 之外的 verified：值+科目可唯一绑定，
#        但与年度列的对应关系未知，故取 needs_review 更稳——见下）。
_SPACE_ROW_RE = re.compile(
    r"^(?P<label>[\u4e00-\u9fff][\u4e00-\u9fff（）()、/·\-%％]{1,39})"
    r"(?P<values>(?:\s+-?[\d,]+(?:\.\d+)?){2,})$"
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
    """从"较/为/分别为"之前的前缀里取科目名：剥年份壳、数字残片与叙述词。

    "2024年末发行人存货" → "存货"；"‑15.36 是 京投发展" → "京投发展"（数字残片
    "36" 与标志列"是"会粘在前缀上）；括号补充说明（"（11933.29亿元）"）一并剥离。
    """
    cleaned = re.sub(r"[\s\u3000]+", "", str(prefix or ""))
    # 括号内多为补充数值/注释（"资产总额（196060.68亿元）"）——剥掉后再取科目名；
    # 系列句的 30 字窗口可能把左括号截在窗外，只剩"亿元）对…"的悬空右括号——
    # 无左括号时剥到第一个右括号（"亿元）对本期…"→"对本期…"）。
    cleaned = re.sub(r"[（(][^）)]*[）)]", "", cleaned)
    if "（" not in cleaned and "(" not in cleaned and ("）" in cleaned or ")" in cleaned):
        cleaned = re.sub(r"^[^）)]*[）)]", "", cleaned)
    cleaned = re.sub(r"(?:19|20)\d{2}(?:年度|年末|年底|年)?", "", cleaned)
    cleaned = re.sub(r"^[\d.、:：（）()]+", "", cleaned)
    cleaned = re.sub(r"^(?:是|为|的|了|有)+", "", cleaned)
    cleaned = re.sub(r"^(?:截至|其中|报告期内|报告期各期末|报告期|最近三年?|近三年?|以及|和|与|及"
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
            year = re.fullmatch(r"(20\d{2})\s*年?(?:度|末|底)?", cell)
            periods.append(year.group(1) if year else "")
        if sum(1 for item in periods if item) < 2:
            continue
        for row in lines[header_index + 1: header_index + 40]:
            if not row.strip():
                break
            if "|" not in row:
                # 表内**分节标签行**（"资产：""负债：""其中："——表头与数据行之间插入的
                # 无管道短标签）跳过继续，而非中断：资产负债表/现金流量表常按"资产：/负债：/
                # 经营活动产生的现金流量："分段，原逻辑在首个分节标签处 break，导致整张表
                # 0 记录（2026-10-16 华辰 financial #28、北投 debt #21 实测）。
                # 保守条件：短（≤12 字）、无数字、以冒号结尾的纯中文字符行——叙述句/跨表
                # 文本（含数字或长句）仍按原样 break。
                stripped = row.strip()
                if (
                    len(stripped) <= 12
                    and stripped.endswith(("：", ":"))
                    and not re.search(r"\d", stripped)
                    and re.search(r"[\u4e00-\u9fff]", stripped)
                ):
                    continue
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
        # 单值句的值后面紧跟**枚举续项**（"…比例为10.11%、19.26%和14.76%"）时，它是系列句
        # 的首项，不是独立命名值——交 ④ `_quote_series_facts` 处理，避免同一数值被两路
        # 以不同 label 重复登记（"占经营活动现金流出比例" vs "占经营活动现金流出"，北投
        # operations #13 实测）。
        if _SERIES_CONTINUATION_RE.match(flat[found.end():]):
            continue
        entries.append({
            "label": label, "value": found.group("value"), "unit": found.group("unit"),
            "period": found.group("year") or "", "raw": found.group(0)[:120], "status": "verified",
        })
    entries.extend(_quote_actual_backref_facts(flat))
    return entries


# ⑦ "（年份）实际 + 值 + 单位" 的回指式单值（2026-10-16 旧计划剩余项复验新增）：
#    披露句常把已实现数与预测数**并列对比**（"预测经营活动现金流量净额9,000万元…该口径与
#    财务表所载2025年实际6,500万元不同期间"），已实现值前**没有紧邻科目名**（科目在对比
#    分句里，距离超出 16 字窗口）。此时从**同引文内最近的命名值句**取科目名回指，年份取
#    "实际"前写的年份。保守条件（宁漏不错）：
#      - 只在"同引文已有**一个**命名值句（label 可剥出）"时启用——回指对象唯一才安全；
#      - 值前须有"实际"（或"实际数"）且**紧邻**年份（"2025年实际6,500万元"）；
#      - 状态记 ``needs_review``：值可接住，但科目是回指、期间为对比期，须人工确认。
_ACTUAL_BACKREF_RE = re.compile(
    r"(?:20\d{2})\s*年(?:度)?\s*(?:实际|实际数|实现)\s*"
    r"(?P<value>-?[\d,]+(?:\.\d+)?)\s*(?P<unit>亿元|万元|元)"
)


def _quote_actual_backref_facts(flat: str) -> list[dict]:
    """⑦ "年份+实际+值" 单值：科目从同引文唯一命名值句回指（见上方说明）。"""
    entries: list[dict] = []
    # 同引文的命名值句（不含本回指模式自身产物）：作为唯一回指对象。
    bases: list[dict] = []
    for found in _NAMED_VALUE_RE.finditer(flat):
        label = f"{_disclosure_label(found.group('head'))}{found.group('noun')}"[:40]
        if label:
            bases.append({"label": label, "value": found.group("value")})
    if len({item["label"] for item in bases}) != 1:
        return entries
    anchor_label = bases[0]["label"]
    for found in _ACTUAL_BACKREF_RE.finditer(flat):
        value = found.group("value")
        # 与已登记命名值句同值 → 已由 ③ 覆盖，不重复登记（避免干扰既有判定）。
        if any(item["value"] == value for item in bases):
            continue
        year = re.search(r"(20\d{2})", found.group(0))
        entries.append({
            "label": anchor_label, "value": value, "unit": found.group("unit"),
            "period": year.group(1) if year else "", "raw": found.group(0)[:120],
            "status": "needs_review",
        })
    return entries


def _quote_series_facts(flat: str) -> list[dict]:
    """④ "…分别为 A、B 和 C" 系列句（2026-10-15 回退章复核新增）。

    值序列逐项登记（label 相同、period 从 head 前缀的年份窗取）；head 剥不出科目名时
    整组跳过（宁可漏、不可错——与三类既有模式同口径）。
    """
    entries: list[dict] = []
    for found in _SERIES_FACT_RE.finditer(flat):
        label = _disclosure_label(found.group("head"))
        if not label:
            continue
        prefix_years = re.findall(r"(?:19|20)\d{2}", found.group("head"))
        period = prefix_years[-1] if prefix_years else ""
        raws = [f"{m.group(1)}{m.group(2)}" for m in _SERIES_VALUE_RE.finditer(found.group("values"))]
        if len(raws) < 2:
            continue
        for match in _SERIES_VALUE_RE.finditer(found.group("values")):
            unit = "％" if match.group(2) == "％" else match.group(2)
            entries.append({
                "label": label, "value": match.group(1), "unit": unit,
                "period": period, "raw": "、".join(raws)[:120], "status": "verified",
            })
    return entries


def _quote_colon_facts(flat: str) -> list[dict]:
    """⑤ "科目：X 单位" 冒号式单值（"反担保折后价值：16,000万元"）。"""
    entries: list[dict] = []
    for found in _COLON_VALUE_RE.finditer(flat):
        head = re.sub(r"^[（(]\d+[）)、.]+", "", str(found.group("head") or ""))
        label = _disclosure_label(head)
        if not label:
            continue
        unit = "％" if found.group("unit") == "％" else found.group("unit")
        entries.append({
            "label": label, "value": found.group("value"), "unit": unit,
            "period": "", "raw": found.group(0)[:120], "status": "verified",
        })
    return entries


# ⑧ 比较句后值（2026-10-16 本轮试跑复核新增）：披露句用"由 A 提高为 B"把**变动前后两值
#    并列**，后值 B 前面没有紧邻科目名、也不属于既有任何形态 → 不落记录（中邮 debt #26：
#    "资产负债率水平由原先的64.45%提高为67.30%"——64.45% 有记录、67.30% 无，整句判缺记录）。
#    保守条件（宁漏不错）：
#      - 形态固定：`由 [≤8字修饰] A 单位 [≤6字] (提高|上升|增至|升至|降至|下降|降低)(为|至) B 单位`；
#      - 前后值**单位同类**（"64.45% → 2.85 个百分点"这类跨单位不登记：不是同口径比较）；
#      - 科目名取"由"之前 ≤24 字（剥壳）；状态 ``needs_review``——科目为就近推断、
#        期间未定（"模拟报表"口径），值可直接参与数字绑定。
_COMPARISON_BACK_RE = re.compile(
    r"由[^。；\d]{0,8}?(?P<before>-?[\d,]+(?:\.\d+)?)\s*(?P<unit1>亿元|万元|元|%|％|倍)"
    r"[^。；]{0,6}?(?:提高|上升|增至|升至|降至|下降|降低)(?:为|至)\s*"
    r"(?P<after>-?[\d,]+(?:\.\d+)?)\s*(?P<unit2>亿元|万元|元|%|％|倍)"
)


def _quote_comparison_back_facts(flat: str) -> list[dict]:
    """⑧ "由 A 提高为 B" 比较句的**后值**（科目就近推断，见上方说明）。"""
    entries: list[dict] = []
    for found in _COMPARISON_BACK_RE.finditer(flat):
        unit1 = "％" if found.group("unit1") == "％" else found.group("unit1")
        unit2 = "％" if found.group("unit2") == "％" else found.group("unit2")
        if unit1 != unit2:
            continue
        # 科目名：取"由"之前 ≤24 字里的**最后一个分句**，再取最后一个"的"之后的名词
        # （"…合并报表口径的资产负债率水平" → "资产负债率水平"）。
        before = re.split(r"[，。；、,]", flat[max(0, found.start() - 24):found.start()])[-1]
        parts = [part for part in before.split("的") if part]
        label = _disclosure_label(parts[-1] if parts else before)
        if not label:
            continue
        entries.append({
            "label": label, "value": found.group("after"), "unit": unit2,
            "period": "", "raw": found.group(0)[:120], "status": "needs_review",
        })
    return entries


# ⑨ 分号分隔的"科目+值+单位"系列（2026-10-16 本轮试跑复核新增）：披露引文把整页科目压成
#    一行"科目值单位；科目值单位；…"（华辰 debt #10："货币资金6,820万元；受限资金818万元；
#    应收账款12,000万元；…"——6,820/818 有引文无记录 → 整句判缺记录）。保守条件：
#      - 每项**以分号结尾**（末项无分号不登记；单项不构成系列，交③/⑤处理）；
#      - 科目头 2~12 个中文字符（不含数字与分隔符，自然止于前一个分号）；
#      - **≥2 项**才算系列；
#      - 科目头以既有披露名词（余额/净额/收入…）结尾时跳过——那类由③负责，避免重复登记。
_SEMICOLON_SERIES_RE = re.compile(
    r"(?P<head>[\u4e00-\u9fff（）()]{2,12})(?P<value>-?[\d,]+(?:\.\d+)?)\s*"
    r"(?P<unit>亿元|万元|元|%|％|倍)\s*[；;]"
)


def _quote_semicolon_series(flat: str) -> list[dict]:
    """⑨ 分号分隔的科目值系列（见上方说明）。"""
    found = list(_SEMICOLON_SERIES_RE.finditer(flat))
    if len(found) < 2:
        return []
    entries: list[dict] = []
    for match in found:
        raw_head = str(match.group("head") or "")
        if any(raw_head.endswith(noun) for noun in _DISCLOSURE_NOUNS):
            continue
        label = _disclosure_label(raw_head)
        if not label:
            continue
        unit = "％" if match.group("unit") == "％" else match.group("unit")
        entries.append({
            "label": label, "value": match.group("value"), "unit": unit,
            "period": "", "raw": match.group(0)[:120], "status": "needs_review",
        })
    return entries


def _quote_aggregate_facts(flat: str) -> list[dict]:
    """⑩-① 汇总句："…金融投资等高流动性资产，合计达169.02亿元"（2026-10-16 中邮 cashflow #20）。

    科目取"合计/总计"前**最后一个分句**，并剥括号补充说明；短语 <4 字时丢弃（宁漏不错）。
    """
    entries: list[dict] = []
    for found in _AGGREGATE_VALUE_RE.finditer(flat):
        head = str(found.group("head") or "").split("、")[-1]
        label = _disclosure_label(head)
        if len(label) < 4:
            continue
        entries.append({
            "label": label, "value": found.group("value"), "unit": found.group("unit"),
            "period": "", "raw": found.group(0)[:120], "status": "needs_review",
        })
    return entries


def _quote_coverage_facts(flat: str) -> list[dict]:
    """⑩-② 占比句："占总资产（扣除代理买卖证券款）的比重达73.55%"（2026-10-16 中邮 cashflow #20）。

    与 `chapter_guard._coverage_phrase_labels` 同形（"占…的(比重|比例|占比)"），
    正文与记录两侧用同一短语，归属不依赖"比例"类名词后缀。
    """
    entries: list[dict] = []
    for found in _COVERAGE_VALUE_RE.finditer(flat):
        # **保留括号口径**（不做 `_disclosure_label` 的括号剥离）：正文写的是
        # "占总资产（扣除代理买卖证券款）的比重"，记录若存成"占总资产的比重"，
        # 两侧包含判定会因括号插入而失配（中邮 cashflow #20 实测）。
        label = re.sub(r"[\s\u3000]+", "", str(found.group("label") or ""))
        if len(label) < 4:
            continue
        # 单值句的值后面紧跟枚举续项（"…的比例为10.11%、19.26%和14.76%"）时它是系列句
        # 首项，交 ④ 处理——否则同一句会多出一条 ⑩ 记录（既有系列测试实测）。
        if _SERIES_CONTINUATION_RE.match(flat[found.end():]):
            continue
        unit = "％" if found.group("unit") == "％" else "%"
        entries.append({
            "label": label, "value": found.group("value"), "unit": unit,
            "period": "", "raw": found.group(0)[:120], "status": "needs_review",
        })
    return entries


def _quote_space_rows(quote: str) -> list[dict]:
    """⑥ "科目 值1 值2 值3" 空格分列表格行（2026-10-16 复验新增）。

    只在**连续 ≥2 行**同构时登记（叙述段落罕见连续两行"纯标签 + 纯数字串"）；单位缺省置空
    （不猜量纲）；期间缺省置空（引文无年份表头，值↔年对应关系未知）→ 记 ``needs_review``：
    值 + 科目可唯一绑定（能接住数字），但因期间未定，**不当作直接支持**（与表格"非年度列"
    同口径：接得住、但需人工确认年度归属）。
    """
    entries: list[dict] = []
    lines = [line for line in str(quote or "").split("\n")]
    matches: list[tuple[int, re.Match]] = []
    for index, line in enumerate(lines):
        found = _SPACE_ROW_RE.match(line.strip())
        if found:
            matches.append((index, found))
    if len(matches) < 2:
        return entries
    for index, found in matches:
        label = _disclosure_label(found.group("label"))
        if not label:
            continue
        values = [token for token in re.split(r"\s+", found.group("values").strip()) if token]
        if len(values) < 2:
            continue
        for value in values:
            entries.append({
                "label": label, "value": value, "unit": "",
                "period": "", "raw": found.group(0)[:120], "status": "needs_review",
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
        entries.extend(_quote_space_rows(quote))
        flat = _lookup_flat(quote)
        entries.extend(_quote_change_facts(flat))
        entries.extend(_quote_named_facts(flat))
        # 2026-10-15 复核新增：系列句（"分别为…"）与冒号式单值——原三类模式未覆盖的
        # 记录缺口形态（北投 cashflow/中邮 debt/financial、华辰 guarantee 实测）。
        entries.extend(_quote_series_facts(flat))
        entries.extend(_quote_colon_facts(flat))
        # 2026-10-16 本轮试跑复核新增：⑧ 比较句后值（"由 A 提高为 B"）与 ⑨ 分号系列
        # （"科目值单位；科目值单位；…"）——两类"引文有、记录无"的缺口形态（中邮 debt #26、
        # 华辰 debt #10 实测）。
        entries.extend(_quote_comparison_back_facts(flat))
        entries.extend(_quote_semicolon_series(flat))
        # 2026-10-16 第四轮试跑复核新增：⑩-① 汇总句（"…资产，合计达X亿元"）与
        # ⑩-② 占比句（"占…的比重达X%"）——中邮 cashflow #20 的 169.02 亿元 / 73.55%
        # 在材料里（p23/p122/p133）却因连接词位不含标点、科目是"占…的比重"整段而漏抽。
        entries.extend(_quote_aggregate_facts(flat))
        entries.extend(_quote_coverage_facts(flat))
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


def build_calculation_records(verdicts, calculations, *, section_key: str,
                             frozen_records=()) -> list[dict]:
    """把**复算通过**的模型计算装配成 `calculation` 记录（方案 §三.2）。

    三条硬规则：

    1. **`value` 用系统复算值 `computed`，不是模型声明值**——声明值只进 `raw`/`extra.declared`。
       若用声明值，一个算错的数会以"已冻结记录"的身份进入正文的数字允许集，
       等于自己给自己背书；
    2. `source_ids` / `locator` / `page` / `profile_snapshot_id` **从输入记录汇总**：
       计算不直连 PDF 页，只能靠输入链追溯；且只有"全部输入一致"时才复制定位，
       否则会给出错误的归属；
    3. `rejected` 不落记录（被否决的计算不得为正文任何数字背书）。
    """
    by_id = {str(item.get("record_id")): item for item in (frozen_records or [])}
    records: list[dict] = []
    for verdict, calc in zip(verdicts or [], calculations or []):
        status = str(verdict.get("status") or "")
        if status not in {"verified", "needs_review"}:
            continue
        input_ids = [str(item) for item in (calc.get("input_record_ids") or []) if str(item).strip()]
        inputs = [by_id[item] for item in input_ids if item in by_id]
        source_ids: list[str] = []
        for item in inputs:
            for source_id in item.get("source_ids") or []:
                if source_id and str(source_id) not in source_ids:
                    source_ids.append(str(source_id))

        def _unanimous(key: str):
            """全部输入在某一字段上一致时返回该值，否则 None（避免给出错误归属）。"""
            values = {str(item.get(key)) for item in inputs if item.get(key)}
            return values.pop() if len(values) == 1 else None

        page = _unanimous("page")
        records.append(_record(
            record_id=f"calc:{section_key}:{calc.get('calculation_id') or ''}",
            record_type="calculation",
            # ⚠️ 复算值，不是声明值
            value=verdict.get("computed"),
            unit=str(calc.get("unit") or ""),
            raw=str(calc.get("value") or ""),
            label=str(calc.get("name") or "").strip(),
            period=str(calc.get("period") or "") or (_unanimous("period") or ""),
            subject=str(calc.get("subject") or "") or (_unanimous("subject") or ""),
            scope=str(calc.get("scope") or "") or (_unanimous("scope") or ""),
            source_ids=source_ids,
            page=page,
            locator=dict(inputs[0].get("locator") or {}) if inputs and _unanimous("page") else None,
            source_fact_ids=input_ids,
            formula=str(calc.get("formula") or ""),
            # verified → derived（与 metric/rule 同一语义：系统派生）；待核验保持 needs_review
            status="derived" if status == "verified" else "needs_review",
            origin_type="model_calculation",
            rule_version=str((verdict.get("rule_version") or "calculations-v1")),
            extra={
                "calculation_id": str(calc.get("calculation_id") or ""),
                "definition": str(calc.get("definition") or ""),
                "precision": calc.get("precision"),
                "cross_period_reason": str(calc.get("cross_period_reason") or ""),
                "declared": str(calc.get("value") or ""),
                "computed": str(verdict.get("computed") or ""),
                "reason_code": str(verdict.get("reason_code") or ""),
                "content_anchor": str(calc.get("content_anchor") or ""),
                "input_record_ids": input_ids,
            },
        ))
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
    from .chapter_guard import (
        CANONICAL_MONEY_UNIT, MONEY_UNIT_FACTORS, _canonical_measurement, _normalize_number,
    )

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
        elif str(record.get("record_type") or "") == "disclosure":
            # 披露记录的单位常**只印在 PDF 页眉/表头**、不在引文窗口内（空格分列表格行
            # ⑥ 实测：现金流量表"经营活动产生的现金流量净额 1,749,719.14 903,091.39 …"
            # 的引文里没有"单位：万元"标注）。这类记录**来自引文**（"引文裸数字兜底只认
            # evidence quotes"），单位未知时把同一数值按**三种金额量纲**折算后登记到
            # 万元口径（正文金额统一折算到万元），并登记 `%`——让正文带单位的数字能绑定到
            # 它（否则 `_mention_allowed` 按单位分桶永远查不到，明明有记录却判
            # missing_record）。因单位不确定，多量纲登记**只可能降级为待核验、不可能给出
            # 直接支持**（记录本身 status=needs_review），符合"宁漏不误报"。
            # **不涉及整页数值池**：值仍由引文片段唯一绑定；fact/metric 的裸数字不在此列
            # （"配置/指标 JSON 裸数字不得为正文数值背书"）。
            for money_unit in MONEY_UNIT_FACTORS:
                normalized_money, _, _ = _canonical_measurement(text, money_unit)
                keys.add((normalized_money, CANONICAL_MONEY_UNIT))
            keys.add((_normalize_number(text), "%"))
    return keys
