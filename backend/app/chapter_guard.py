"""Chapter-level evidence scoping, hashing and post-generation validation."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from rank_bm25 import BM25Okapi

from .chapter_prompts import (
    CONTEXT_SCHEMA_VERSION, chapter_prompt, chapter_prompt_is_overridden, effective_chapter_config,
    prompt_config_payload,
)
from .database import json_load, row, rows
from .professional import analysis_fact_rows
from .report_tables import SYSTEM_TABLE_ROLE
from .retrieval import tokenize


DEFAULT_FORBIDDEN = (
    "建议承保", "建议拒绝", "建议通过", "投资建议",
    "审批通过", "审批拒绝", "核定担保", "建议不承保",
    # 承诺性投资/授信结论（否定语境如“不构成投资建议”另行豁免）
    "建议投资", "推荐投资", "具备投资价值", "预计授信",
)
# "担保额度/担保费率"**不能按裸词拦截**：它们在材料里是正当业务表述（募集说明书载明的
# 责任上限、反担保覆盖范围、费用约定）。按裸词拦会把"复述原文"也判成越权——实测它正是
# 高频回退原因之一。修订稿 §3.1 要求按**完整语境**检查，区分三类：
#   ① 我们方做出担保决策（同小句出现决策动词）→ 拒绝；
#   ② 否定声明（同小句有否定词）→ 放行；
#   ③ 引用材料原文（同句出现材料名/载明/约定等）→ 放行。
# 其余（纯描述性提及）也放行：描述责任上限不是越权。**不允许**退化成"固定窗口共现"。
DECISION_FORBIDDEN_TERMS = ("担保额度", "担保费率")
DECISION_VERBS = (
    "建议", "核定", "核准", "批准", "审批", "同意", "决定", "确定", "主张", "倾向",
    "给予", "授予", "设定", "批复", "决议",
)
SOURCE_QUOTE_MARKERS = (
    "募集说明书", "发行文件", "评级报告", "外部信用评价文件", "材料载明", "载明",
    "原文", "披露", "约定", "据材料", "根据材料", "保证合同", "担保协议",
)
DECISION_CLAUSE_WINDOW = 40


def _decision_classification(content: str, index: int) -> str:
    """"担保额度/担保费率"命中的归类：forbidden / safe_negative / safe_quote / safe_description。

    顺序有意义：**先否定、再引用、最后才看决策动词**——"不得输出担保额度"里虽然有"输出"，
    但否定已经把它变成边界声明；"募集说明书载明担保额度为…"是在复述材料，两者都不该拒绝。
    """
    window = content[max(0, index - DECISION_CLAUSE_WINDOW):index]
    clause = NEGATION_CLAUSE_SPLIT_RE.split(window)[-1]
    if any(hedge in clause for hedge in NEGATION_HEDGES):
        negated = False
    else:
        negated = any(marker in clause for marker in NEGATION_MARKERS)
    if negated:
        return "safe_negative"
    sentence = content[max(0, index - DECISION_CLAUSE_WINDOW * 2):index]
    if any(marker in sentence for marker in SOURCE_QUOTE_MARKERS):
        return "safe_quote"
    if any(verb in clause for verb in DECISION_VERBS):
        return "forbidden"
    return "safe_description"
# 命中位置之前出现否定词，说明写的是“不得/不应如何”，属于合规边界表述。
NEGATION_MARKERS = ("不", "非", "禁止", "避免", "切勿", "未", "无须", "无需")
NEGATION_WINDOW = 40
# 只在句末与转折处断句：中文枚举用顿号/逗号连接（“不输出承保、拒保、担保额度、…投资建议”），
# 按逗号切会把管着整个列表的否定词切掉，导致后半段枚举项被误判成禁止表达。
NEGATION_CLAUSE_SPLIT_RE = re.compile(r"[。；;！!？?\n]|，(?:但|而|不过|然而|同时)")
# 这些不是否定，而是把结论藏在“不排除”之类措辞后面——不豁免。
NEGATION_HEDGES = ("不排除", "不否认", "并非不")
# 报告口径词汇不是主体名（ENTITY_RE 会把“半年度或母公司”当成公司名）。
REPORT_VOCABULARY = ("母公司", "合并", "半年度", "季度", "口径")
NUMBER_RE = re.compile(r"(?<![A-Za-z])\-?\d+(?:\.\d+)?")
# 数字比对用：带单位一起匹配（单位不得被模型自行换算，如 48000万元 ≠ 4.8亿元）。
NUMBER_WITH_UNIT_RE = re.compile(
    # **保留负号**：材料写 -1,234 而正文写成 1,234（或反向）是符号错误，必须能分辨。
    # 只认真正压在数字前的 -/−（U+2212），且其前一个字符不能是数字或字母——这样
    # "2023-2024"、"第 28-31 页" 里的连接号不会被当成负号。
    r"(?<![\dA-Za-z])(?P<sign>[-−])?(?P<number>\d[\d,]*(?:\.\d+)?)"
    # 数字与单位之间允许空格（"30,000 万元"）；否则单位抓不到，会与材料里的"30000万元"判成两个数
    r"[ \t]*(?P<unit>%|‰|倍|万元|亿元|万|亿|元|个月|月|年|天|日|次|人|家|项|股|笔)?"
)
# 标题编号、有序列表编号、引用短别名中的数字都不是业务数字。
MARKDOWN_ORDINAL_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*\d+[.、]?[ \t]*", re.MULTILINE)
LIST_ORDINAL_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.、])[ \t]+", re.MULTILINE)
CITATION_ALIAS_RE = re.compile(r"\bE\d{2,}\b")


def _normalize_number(raw: str) -> str:
    """千分位、小数尾零与展示精度不影响数值判定。

    48,000 → 48000；70.0 → 70；1.10 → 1.1；9.8901 → 9.89。
    最后一条很关键：系统指标存 4 位小数，报告里按 2 位小数引用是行业惯例，
    此前会把"9.89%"判成与"9.8901%"不同的新数字。
    """
    text = str(raw).replace(",", "")
    try:
        value = round(float(text), 2)
    except ValueError:
        return text or "0"
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _strip_format_numbers(content: str) -> tuple[str, list[str], list[str]]:
    """去掉格式性编号，返回（剩余文本, 被忽略的编号, 被忽略的页码）。

    标题 `### 1. 财务概览`、列表 `1. `、别名 `E01`、页码 `第 28、31、32 页`
    都不是业务数字，此前会被当成"新增数字"误报。
    """
    text = content or ""
    ignored: list[str] = []
    page_numbers: list[str] = []
    for match in PAGE_REFERENCE_RE.findall(text):
        page_numbers.extend(re.findall(r"\d+", match))
    text = PAGE_REFERENCE_RE.sub(" ", text)
    for pattern in (MARKDOWN_ORDINAL_RE, LIST_ORDINAL_RE, CITATION_ALIAS_RE):
        for match in pattern.findall(text):
            ignored.extend(re.findall(r"\d+", match))
        text = pattern.sub(" ", text)
    return text, ignored, page_numbers


# 同一单位的常见写法归一（"12000万" 与 "12000万元" 是同一个数）。
UNIT_ALIASES = {"万": "万元", "亿": "亿元", "％": "%"}
# 合法等值换算表（折算到"元"）：只接受严格等值的量级换算，不做近似或口径替换。
MONEY_UNIT_FACTORS = {"元": Decimal(1), "万元": Decimal(10_000), "亿元": Decimal(100_000_000)}
CANONICAL_MONEY_UNIT = "万元"
# 页码引用不是业务数字：第 28、31、32 页 / 第28-31页 / 第 5 页
# 规则定义在 delivery_wording，那里还要处理「〔资料01〕」形式的引用定位。
from .delivery_wording import PAGE_REFERENCE_PATTERN as _PAGE_PATTERN  # noqa: E402

PAGE_REFERENCE_RE = re.compile(_PAGE_PATTERN)


def _decimal_text(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(value.to_integral_value())
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _canonical_measurement(raw_number: str, unit: str) -> tuple[str, str, bool]:
    """返回 (normalized_value, normalized_unit, converted)。

    金额按预定义换算表折算到万元：20000万元 与 2亿元 是同一个数（converted=True）。
    只做严格等值的量级换算；非金额单位保持原样。
    """
    text = str(raw_number).replace(",", "")
    if unit in MONEY_UNIT_FACTORS and unit != CANONICAL_MONEY_UNIT:
        try:
            value = Decimal(text) * MONEY_UNIT_FACTORS[unit] / MONEY_UNIT_FACTORS[CANONICAL_MONEY_UNIT]
        except (InvalidOperation, ValueError):
            return _normalize_number(raw_number), unit, False
        return _decimal_text(value), CANONICAL_MONEY_UNIT, True
    return _normalize_number(raw_number), unit, False


YEAR_RE = re.compile(r"(?<![\d.,])((?:19|20)\d{2})(?![\d.,])")
# 4 位数后面紧跟这些单位说明它是金额/比率（1939万元、25.0%），不是年度。
YEAR_UNIT_TAILS = ("万元", "亿元", "元", "万", "亿", "%", "‰", "倍", "天", "个", "股", "人")


def _year_mentions(text: str) -> set[str]:
    """正文里的年度。排除 4 位数金额/比率（1939万元 不是 1939 年）。"""
    years: set[str] = set()
    for match in YEAR_RE.finditer(text or ""):
        tail = (text or "")[match.end():match.end() + 2]
        if any(tail.startswith(unit) for unit in YEAR_UNIT_TAILS):
            continue
        years.add(match.group(1))
    return years


def _unit_hint(key: str) -> str:
    """结构化键名里的单位线索：due_12m_pct → %，max_age_months → 个月。"""
    text = str(key).lower()
    if "pct" in text or "percent" in text:
        return "%"
    if "month" in text:
        return "个月"
    return ""


def _measurement_texts(context: dict) -> list[str]:
    """把 value 与 unit 合成 "9.8901%"、"12000万元" 形态，供数字比对使用。

    结构化数据里 value 与 unit 是两个独立字段，直接 ``json.dumps`` 后数字旁边没有
    单位，归一化成的 (数值, 单位) 对就永远匹配不上模型正文里带单位的写法——
    模型照抄系统指标反而被判成"新数字"。
    """
    texts: list[str] = []
    for group in ("facts", "metrics", "cross_checks"):
        for item in context.get(group) or []:
            value, unit = item.get("value"), item.get("unit") or ""
            if value is not None and value != "":
                texts.append(f"{value}{unit}")
            if item.get("text_value"):
                texts.append(str(item["text_value"]))
            for key in ("formula", "summary", "title", "name"):
                if item.get(key):
                    texts.append(str(item[key]))
            # 规则阈值以裸数字存在（thresholds={"due_12m_pct": 50.0}），单位信息在键名里：
            # 模型按材料写成"超过50%的关注阈值"是正确行为，必须能匹配上。
            for mapping in (item.get("thresholds"),):
                if isinstance(mapping, dict):
                    for key, entry in mapping.items():
                        if entry is None or entry == "":
                            continue
                        texts.append(f"{entry}{_unit_hint(key)}")
            for entry in item.get("values") or []:
                if isinstance(entry, dict):
                    number = entry.get("value")
                    if number is None or number == "":
                        continue
                    hint = entry.get("unit") or _unit_hint(entry.get("name") or entry.get("label") or "")
                    texts.append(f"{number}{hint}")
                elif isinstance(entry, (int, float)):
                    texts.append(str(entry))
    return texts


def _number_mentions(content: str) -> list[dict]:
    """提取（归一化数值, 归一化单位）序列。年度由 allowed_periods 单独约束，不计入数字比对。"""
    text, _, _ = _strip_format_numbers(content)
    mentions = []
    for match in NUMBER_WITH_UNIT_RE.finditer(text):
        raw, unit = match.group("number"), match.group("unit") or ""
        sign = match.group("sign")
        if sign == "−":  # U+2212 归一成 ASCII 减号，否则 "-1234" 与 "−1234" 会被判成两个数
            sign = "-"
        raw = f"{sign or ''}{raw}"
        unit = UNIT_ALIASES.get(unit, unit)
        normalized, canonical_unit, converted = _canonical_measurement(raw, unit)
        if unit in {"", "年"} and re.fullmatch(r"(?:19|20)\d{2}", normalized):
            continue
        mentions.append({
            "raw": f"{raw}{unit}", "normalized_value": normalized, "normalized_unit": canonical_unit,
            "converted": converted, "location_type": "body",
            # 截取命中处前后一小段上下文，便于定位"新数字"到底是什么（不保存整段正文）
            "context": text[max(0, match.start() - 12):match.end() + 12].strip(),
        })
    return mentions
RATING_RE = re.compile(r"\b(?:AAA|AA|A|BBB|BB|B|CCC|CC|C)(?:[+-])?\b")
ENTITY_RE = re.compile(r"[\u4e00-\u9fff]{2,30}(?:有限公司|股份有限公司|集团有限公司|公司)")


def _entity_is_allowed(produced: str, allowed: set[str]) -> bool:
    """主体名比对，而不是比对整段上下文。

    ENTITY_RE 只能定位到以“公司”结尾的汉字片段，片段头部会连带吃进上下文
    （“发行人华辰精密制造有限公司”）。模型改写句子时头部必然变化，若按字符串
    全等比较，同一家公司就会被判成“新主体”，改写越认真越容易被拒。主体名固定
    在片段尾部，因此只要一方是另一方的后缀就视为同一主体；真正新增的主体不会
    与允许集构成后缀关系，仍会被拦下。
    """
    return any(produced == item or produced.endswith(item) or item.endswith(produced) for item in allowed)


def _json_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decode_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json_load(value)
        except (TypeError, ValueError):
            value = []
    return [str(item) for item in (value or []) if item]


def _json_value(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list, int, float, bool)) or value is None:
        return default if value is None else value
    try:
        decoded = json_load(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return decoded if decoded is not None else default


def _source_rows(project_id: str, source_ids: set[str], allowed_kinds: set[str], include_all: bool = False) -> list[dict]:
    source_rows = rows(
        "SELECT s.id,s.document_id,s.locator_type,s.locator,s.text,s.page,s.origin_type,s.snapshot_id,"
        "s.profile_id,s.profile_version,s.entity_role,d.kind,d.document_role,d.filename "
        "FROM analysis_sources s JOIN analysis_documents d ON d.id=s.document_id AND d.project_id=s.project_id "
        "WHERE s.project_id=? ORDER BY s.page,s.id",
        (project_id,),
    )
    selected = [
        item for item in source_rows
        if (include_all or item["id"] in source_ids)
        and (not allowed_kinds or item["kind"] in allowed_kinds)
    ]
    return selected


def _conflict_context(project_id: str, fact_keys: set[str]) -> tuple[list[dict], set[str]]:
    conflicts: list[dict] = []
    source_ids: set[str] = set()
    canonical_rows = rows(
        "SELECT * FROM canonical_facts WHERE project_id=? AND status='conflicted' ORDER BY fact_key,period,id",
        (project_id,),
    )
    for canonical in canonical_rows:
        if fact_keys and canonical["fact_key"] not in fact_keys:
            continue
        assertion_ids = _decode_ids(canonical.get("assertion_ids"))
        assertions = []
        if assertion_ids:
            placeholders = ",".join("?" for _ in assertion_ids)
            assertion_rows = rows(
                f"SELECT * FROM fact_assertions WHERE project_id=? AND id IN ({placeholders}) ORDER BY created_at,id",
                (project_id, *assertion_ids),
            )
            assertion_by_id = {item["id"]: item for item in assertion_rows}
            assertion_rows = [assertion_by_id[item_id] for item_id in assertion_ids if item_id in assertion_by_id]
        else:
            assertion_rows = []
        for assertion in assertion_rows:
            assertion_source_ids = _decode_ids(assertion.get("source_ids"))
            source_ids.update(assertion_source_ids)
            assertions.append({
                "id": assertion["id"], "value": assertion.get("value"), "unit": assertion.get("unit", ""),
                "text_value": assertion.get("text_value", ""), "raw_value": assertion.get("raw_value", ""),
                "source_ids": assertion_source_ids, "extraction_method": assertion.get("extraction_method", ""),
            })
        conflicts.append({
            "id": canonical["id"], "fact_key": canonical["fact_key"], "name": canonical.get("name", ""),
            "period": canonical.get("period", ""), "status": canonical["status"],
            "assertion_ids": assertion_ids, "assertions": assertions,
        })
    return conflicts, source_ids


def _allowed_periods(fact_years: list[str], evidence: list[dict], current_year: int) -> list[str]:
    """允许引用的年度：最近三个**完整**年度 ∪ 证据材料里出现的年度。

    不能直接取"最近三个年度"：债务类事实带未来到期年份（如 2026 年到期债务），
    会把合法的完整年度（2023）挤出允许集，模型照材料写 2023 反而被判"未授权年度"。
    材料里根本没有的年度仍然会被拒绝。
    """
    complete_years = [year for year in fact_years if int(year) <= current_year - 1][-3:]
    allowed = set(complete_years) or set(fact_years[-3:])
    for item in evidence:
        allowed.update(_year_mentions(str(item.get("quote") or "")))
    return sorted(allowed)


UNIT_SUFFIXES = ("%", "‰", "倍", "万元", "亿元", "元", "万", "亿", "天", "个月", "年", "次", "人", "家", "项", "股", "笔")


def assertion_display(assertion: dict) -> str:
    """渲染断言值，缺单位时补上。

    抽取出的 ``raw_value`` 常常只有数字（``9000.0``），而模型按报告习惯写 ``9,000万元``。
    不补单位会让"照材料并列保留冲突值"的正确行为被判成新增数字。
    """
    unit = str(assertion.get("unit") or "")
    from .facts import strip_canonical_prefix

    displayed = strip_canonical_prefix(
        assertion.get("raw_value") or assertion.get("text_value") or ""
    ).strip()
    if not displayed and assertion.get("value") is not None:
        return f"{assertion['value']:g}{unit}"
    if unit and displayed and not any(displayed.endswith(suffix) for suffix in UNIT_SUFFIXES):
        displayed = f"{displayed}{unit}"
    return displayed


# 同一页最多取几条证据：防止相关度排序把预算全压在某一页上。
EVIDENCE_PER_PAGE_CAP = 2


def _evidence_item(source: dict) -> dict:
    return {
        "id": f"c-{source['id']}", "source_id": source["id"], "document_id": source["document_id"],
        "document_kind": source["kind"], "filename": source["filename"], "locator": source["locator"],
        "page": source.get("page"), "quote": str(source.get("text") or "")[:1800],
        "origin_type": source.get("origin_type", "document"),
        "snapshot_id": source.get("snapshot_id", ""), "profile_id": source.get("profile_id", ""),
        "profile_version": source.get("profile_version", 0), "entity_role": source.get("entity_role", ""),
    }


def select_evidence(sources: list[dict], priority_ids: set[str], query: str,
                    max_items: int, max_chars: int) -> list[dict]:
    """选取送入模型的证据：**该章事实来源优先 → BM25 相关度 → 同页限流**。

    此前按页码顺序取前 N 条，候选一多就只取到文档最前面的几页——实测 cashflow 有 94 个
    候选、跨 69 页，却只选中第 1–6 页，「预测经营现金流」「偿债安排」等后段关键页永远
    进不了模型视野，该章只能写"材料不足"。

    返回顺序即相关度顺序，同时作为 `allowed_citation_ids` 的默认优先级
    （模型未返回引用时按此顺序取引用）。
    """
    if not sources:
        return []
    priority = sorted((s for s in sources if s["id"] in priority_ids),
                      key=lambda s: (s.get("page") or 0, s["id"]))
    rest = [s for s in sources if s["id"] not in priority_ids]
    if rest and query.strip():
        corpus = [tokenize(str(s.get("text") or "")) for s in rest]
        scores = BM25Okapi(corpus).get_scores(tokenize(query))
        rest = [source for _score, source in sorted(
            zip(scores, rest), key=lambda pair: (-float(pair[0]), pair[1].get("page") or 0, pair[1]["id"]))]
    ordered = [*priority, *rest]

    selected: list[dict] = []
    per_page: dict[object, int] = {}
    total_chars = 0
    for source in ordered:
        if len(selected) >= max_items:
            break
        quote = str(source.get("text") or "")[:1800]
        if selected and total_chars + len(quote) > max_chars:
            continue
        page = source.get("page")
        if page is not None and per_page.get(page, 0) >= EVIDENCE_PER_PAGE_CAP:
            continue
        selected.append(_evidence_item(source))
        per_page[page] = per_page.get(page, 0) + 1
        total_chars += len(quote)

    if len(selected) < max_items:      # 同页限流导致未取满时放宽一次
        chosen = {item["source_id"] for item in selected}
        for source in ordered:
            if len(selected) >= max_items:
                break
            if source["id"] in chosen:
                continue
            quote = str(source.get("text") or "")[:1800]
            if selected and total_chars + len(quote) > max_chars:
                continue
            selected.append(_evidence_item(source))
            total_chars += len(quote)
    return selected


def _conflict_block(conflicts: list[dict]) -> str:
    """确定性冲突块。

    这个块既进模型上下文、又被要求**逐字保留在成品里**，所以它的用词必须是成品口径：
    不出现断言 UUID、内部字段键与内部来源 ID——它们在成品里既无意义，也违反
    「内部 UUID 和字段键不进入成品」。来源一律渲染成「发行文件〔资料01〕第 29 页」。
    """
    if not conflicts:
        return ""
    from .report_tables import source_reference

    lines = ["### 冲突事实（以下披露值必须并列保留）"]
    for conflict in conflicts:
        label = conflict.get("name") or conflict["fact_key"]
        period = conflict.get("period") or "未注明期间"
        lines.append(f"- {label}（{period}）：")
        for assertion in conflict.get("assertions", []):
            displayed = assertion_display(assertion)
            reference = "、".join(
                item for item in (source_reference(source) for source in assertion.get("source_ids", []))
                if item
            ) or "未标注来源"
            lines.append(f"  - 披露值 {displayed or '空值'}；来源：{reference}")
    return "\n".join(lines)


def _relevant_open_tasks(project_id: str, related_ids: set[str], source_kinds: set[str], labels: set[str]) -> list[dict]:
    relevant = []
    for task in rows(
        "SELECT id,task_type,title,description,related_ids,required_evidence FROM review_tasks "
        "WHERE project_id=? AND status='open' ORDER BY rowid",
        (project_id,),
    ):
        task_related_ids = set(_decode_ids(task.get("related_ids")))
        task_text = " ".join(str(task.get(key) or "") for key in ("title", "description", "required_evidence"))
        if task_related_ids & related_ids or any(kind and kind in task_text for kind in source_kinds) or any(label and label in task_text for label in labels):
            task["related_ids"] = sorted(task_related_ids)
            relevant.append(task)
    return relevant


def _system_table_block(project_id: str, chapter_key: str, subject: str) -> str:
    """该章的系统数据表。生成失败返回空串——表格问题不应让整章生成失败。"""
    try:
        from .report_tables import system_table_markdown

        return system_table_markdown(project_id, chapter_key, subject=subject, current=True)
    except Exception:
        return ""


def build_chapter_context(project_id: str, chapter_key: str, draft: str, source_ids: list[str] | None = None) -> dict:
    # The effective config (project > global > YAML) is what the model is told and
    # what the validator measures against; reading the factory YAML here made an
    # edited paragraph/prose range a false rejection.
    config = effective_chapter_config(chapter_key, project_id)
    contract = config["input_contract"]
    fact_keys = set(contract["fact_keys"])
    rule_keys = set(contract["rule_keys"])
    source_kinds = set(contract["source_kinds"])
    try:
        facts = analysis_fact_rows(project_id)
    except ValueError:
        # Legacy callers and isolated contract tests may not have a project
        # entity row yet. Keep their deterministic facts available without
        # weakening subject isolation for normal projects.
        facts = rows("SELECT * FROM facts WHERE project_id=? ORDER BY rowid", (project_id,))
    selected_facts = []
    referenced_sources: set[str] = set(source_ids or [])
    for fact in facts:
        if fact.get("fact_key") not in fact_keys:
            continue
        fact["source_ids"] = _decode_ids(fact.get("source_ids"))
        referenced_sources.update(fact["source_ids"])
        selected_facts.append({
            "id": fact["id"], "fact_key": fact["fact_key"], "name": fact.get("name", ""),
            "period": fact.get("period", ""),
            "period_type": fact.get("period_type", "unknown"), "scope": fact.get("scope", "unknown"),
            "value": fact.get("value"), "unit": fact.get("unit", ""), "text_value": fact.get("text_value", ""),
            "raw_value": fact.get("raw_value", ""), "review_status": fact.get("review_status", "unreviewed"),
            "assertion_status": fact.get("assertion_status", "unreviewed"),
            "source_ids": fact["source_ids"], "origin_type": fact.get("origin_type", "document"),
            "profile_snapshot_id": fact.get("profile_snapshot_id", ""),
        })
    checks = []
    for check in rows("SELECT * FROM cross_checks WHERE project_id=? ORDER BY rowid", (project_id,)):
        if check.get("rule_key") not in rule_keys:
            continue
        check["source_ids"] = _decode_ids(check.get("source_ids"))
        check["fact_ids"] = _decode_ids(check.get("fact_ids"))
        check["values"] = _json_value(check.get("values_json", "[]"), [])
        check["thresholds"] = _json_value(check.get("thresholds", "{}"), {})
        referenced_sources.update(check["source_ids"])
        checks.append({
            "id": check["id"],
            "rule_key": check["rule_key"], "status": check["status"], "title": check["title"],
            "summary": check["summary"], "formula": check.get("formula", ""),
            "result_value": check.get("result_value"), "values": check["values"],
            "thresholds": check["thresholds"], "source_ids": check["source_ids"],
        })
    metrics = []
    metric_keys = set(contract["metric_keys"])
    for metric in rows("SELECT * FROM financial_metrics WHERE project_id=? ORDER BY period,metric_key", (project_id,)):
        if metric.get("metric_key") not in metric_keys:
            continue
        metric["source_ids"] = _decode_ids(metric.get("source_ids"))
        metric["input_fact_ids"] = _decode_ids(metric.get("input_fact_ids"))
        referenced_sources.update(metric["source_ids"])
        metrics.append({
            "metric_key": metric["metric_key"], "period": metric.get("period", ""), "value": metric.get("value"),
            "unit": metric.get("unit", ""), "formula": metric.get("formula", ""),
            "status": metric.get("status", "calculated"), "source_ids": metric["source_ids"],
        })
    if source_ids:
        referenced_sources.update(source_ids)
    relevant_conflicts, conflict_source_ids = _conflict_context(project_id, fact_keys)
    referenced_sources.update(conflict_source_ids)
    source_rows = _source_rows(project_id, referenced_sources, source_kinds, True)
    max_items = contract["max_evidence_items"]
    max_chars = contract["max_evidence_chars"]
    # 优先保证"该章事实/指标/核验/冲突所引用的来源页"进入证据，其余按相关度补足。
    priority_ids = (
        {sid for item in selected_facts for sid in item.get("source_ids") or []}
        | {sid for item in metrics for sid in item.get("source_ids") or []}
        | {sid for item in checks for sid in item.get("source_ids") or []}
        | set(conflict_source_ids)
    )
    evidence_query = " ".join([
        *[str(item.get("name") or item.get("fact_key") or "") for item in selected_facts],
        *[str(item.get("metric_key") or "") for item in metrics],
        *[str(item.get("title") or item.get("rule_key") or "") for item in checks],
        str(config.get("goal") or ""), " ".join(config.get("rules") or []),
    ])
    from .retrieval import retrieve_chunks

    priority_order = list(dict.fromkeys(
        source_id
        for collection in (
            *[item.get("source_ids") or [] for item in selected_facts],
            *[item.get("source_ids") or [] for item in metrics],
            *[item.get("source_ids") or [] for item in checks],
            *[item.get("source_ids") or [] for item in relevant_conflicts],
        )
        for source_id in collection
        if source_id in priority_ids
    ))
    retrieved = retrieve_chunks(
        project_id,
        evidence_query or str(config.get("goal") or ""),
        top_k=max(30, max_items * 3),
        allowed_kinds=source_kinds or None,
        priority_source_ids=priority_order,
    )
    source_by_id = {str(item["id"]): item for item in source_rows}
    retrieved_sources: list[dict] = []
    used_source_ids: set[str] = set()
    for chunk in retrieved:
        source_id = str(chunk["source_id"])
        source = source_by_id.get(source_id)
        if not source or source_id in used_source_ids:
            continue
        retrieved_sources.append({
            **source,
            "text": str(chunk.get("text") or ""),
            "retrieval_chunk_id": str(chunk.get("chunk_id") or ""),
            "retrieval_score": float(chunk.get("score") or 0),
            "retrieval_chunk_type": str(chunk.get("chunk_type") or "text"),
        })
        used_source_ids.add(source_id)
    if not retrieved_sources and priority_ids:
        retrieved_sources = [item for item in source_rows if item["id"] in priority_ids]
    evidence = select_evidence(retrieved_sources, priority_ids, evidence_query, max_items, max_chars)
    related_ids = {item["id"] for item in selected_facts}
    related_ids.update(item["id"] for item in relevant_conflicts)
    related_ids.update(assertion_id for item in relevant_conflicts for assertion_id in item.get("assertion_ids", []))
    related_ids.update(item["id"] for item in checks)
    labels = {
        str(item.get("name") or item.get("fact_key") or "") for item in selected_facts
    } | {
        str(item.get("title") or item.get("rule_key") or "") for item in checks
    } | {
        str(item.get("name") or item.get("fact_key") or "") for item in relevant_conflicts
    }
    relevant_tasks = _relevant_open_tasks(project_id, related_ids, source_kinds, labels)
    conflict_block = _conflict_block(relevant_conflicts)
    source_attribution_block = (
        SOURCE_ATTRIBUTION_BLOCK
        if "profile_snapshot" in {str(item.get("origin_type") or "") for item in selected_facts}
        else ""
    )
    # 财务章的数据表由系统生成：不依赖模型是否愿意输出 Markdown 表格，
    # 也不受模型回退影响。与冲突块同一套机制——进上下文、逐字保留、不计篇幅。
    issuer = row("SELECT company_name FROM projects WHERE id=?", (project_id,))
    system_table_block = _system_table_block(project_id, chapter_key, str((issuer or {}).get("company_name") or ""))
    deterministic_draft = draft
    for block in (conflict_block, system_table_block, source_attribution_block):
        if block and block not in deterministic_draft:
            deterministic_draft = f"{deterministic_draft}\n\n{block}"
    has_missing = bool(relevant_tasks) or not evidence
    # 允许年度 = 最近三个**完整**年度 ∪ 材料中出现的年度。
    # 不能直接取"最近三个年度"：债务类事实会有未来到期年份（如 2026 年到期债务），
    # 那会把合法的完整年度（2023）挤出允许集，模型照材料写 2023 反而被判"未授权年度"。
    current_year = datetime.now(timezone.utc).year
    fact_years = sorted({
        str(item.get("period")) for item in selected_facts
        if re.fullmatch(r"(?:19|20)\d{2}", str(item.get("period") or ""))
    })
    annual_periods = _allowed_periods(fact_years, evidence, current_year)
    financial_scopes = {item.get("scope") for item in selected_facts if item.get("scope") in {"consolidated", "standalone"}}
    selected_scope = "consolidated" if "consolidated" in financial_scopes else "standalone" if "standalone" in financial_scopes else "unknown"
    try:
        from .facts import material_inventory
        guarantee_applicable = bool(material_inventory(project_id).get("guarantee_applicable", True))
    except Exception:
        guarantee_applicable = True
    origin_types = sorted({str(item.get("origin_type") or "document") for item in selected_facts})
    prompt_text = chapter_prompt(chapter_key, project_id)
    pack_version = prompt_config_payload(bond=True)["version"]
    prompt_version = f"custom-{hashlib.sha256(prompt_text.encode('utf-8')).hexdigest()[:12]}" if chapter_prompt_is_overridden(chapter_key, project_id) else pack_version
    # 结构化允许记录：把系统表格的**业务格**（带符号数值 + 单位 + 科目 + 期间 + 来源 + 页码）
    # 单独冻结下来，随上下文一起进 ``input_hash``，供章节校验与后置校验共用。
    # 只把表格 Markdown 拼进允许文本是不够的——表里单元格是裸数字、单位写在表头，
    # 模型在正文补上单位就会被判成"新数字"（实测最高频的整章回退原因）。
    from .table_evidence import allowed_table_records

    table_records = allowed_table_records(
        project_id, chapter_key, subject=str((issuer or {}).get("company_name") or ""),
    )
    payload = {
        "project_id": project_id, "chapter_key": chapter_key, "prompt_config": config,
        "prompt_version": prompt_version,
        "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(), "draft": deterministic_draft,
        "facts": selected_facts, "metrics": metrics, "cross_checks": checks, "evidence": evidence,
        "conflicts": relevant_conflicts, "conflict_block": conflict_block,
        "system_table_block": system_table_block,
        # 表后分析的章节职责随上下文投递，不依赖可被覆盖的章节 prompt。
        "system_table_role": SYSTEM_TABLE_ROLE if system_table_block else "",
        # 结构化允许记录（受控表格的业务格）：数字校验与"科目绑定"校验都读它。
        "table_records": table_records,
        # 系统冻结块：生成时必须逐字保留，且不计入篇幅统计。
        "required_disclosures": [block for block in (conflict_block, system_table_block, source_attribution_block) if block],
        "source_attribution_block": source_attribution_block,
        "relevant_review_tasks": relevant_tasks, "has_missing": has_missing,
        "allowed_periods": annual_periods, "selected_scope": selected_scope,
        "origin_types": origin_types, "guarantee_applicable": guarantee_applicable,
        "length_policy": config.get("length_policy") or {},
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        "llm_applicable": not (chapter_key in {"guarantee", "counter_guarantee"} and not guarantee_applicable),
    }
    payload["input_hash"] = _json_hash(payload)
    payload["allowed_citation_ids"] = [item["id"] for item in evidence]
    return payload


def _narrative_paragraphs(content: str) -> list[str]:
    """Return prose paragraphs while ignoring Markdown labels and controlled lists."""
    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = []
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith(">"):
                continue
            if re.match(r"(?:[-*+]\s+|\d+[.、]\s+)", stripped):
                continue
            lines.append(stripped)
        prose = " ".join(lines).strip()
        if prose:
            paragraphs.append(prose)
    return paragraphs


def _validate_narrative_shape(content: str, context: dict) -> list[str]:
    """Validate report prose without weakening factual validation elsewhere."""
    chapter_key = str(context.get("chapter_key") or "")
    if not chapter_key:
        return []
    narrative = (context.get("prompt_config") or {}).get("narrative_contract") or {}
    if not narrative:
        return []
    reasons: list[str] = []
    # 系统生成的块不是模型正文，不能计入行文形态诊断。
    prose = content
    for block in (
        str(context.get("system_table_block") or ""),
        str(context.get("conflict_block") or ""),
        str(context.get("source_attribution_block") or ""),
    ):
        if block:
            prose = prose.replace(block, "")
    paragraphs = _narrative_paragraphs(prose)

    for opening in narrative.get("avoid_openings", []):
        if any(paragraph.startswith(opening) for paragraph in paragraphs):
            reasons.append(f"章节以技术化表达起句：{opening}")
    searchable_content = content.replace(str(context.get("conflict_block") or ""), "")
    for term in narrative.get("prohibited_terms", []):
        if term and term.lower() in searchable_content.lower():
            reasons.append(f"章节出现禁止的系统术语：{term}")

    if narrative.get("list_mode") == "prohibited":
        controlled = content.replace(str(context.get("conflict_block") or ""), "")
        if any(re.match(r"\s*(?:[-*+]\s+|\d+[.、]\s+)", line) for line in controlled.splitlines()):
            reasons.append("章节不应使用项目符号或编号列表")
    return reasons


# Markdown 表格行：整行以竖线开头结尾。正文里正常不会出现这种形状。
_MARKDOWN_TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.MULTILINE)

_MD_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.MULTILINE)
_MD_LIST_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.、])[ \t]+", re.MULTILINE)
_MD_QUOTE_RE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
_MD_EMPHASIS_RE = re.compile(r"\*\*|`")


def count_report_chars(content: str, conflict_block: str = "",
                       required_disclosures: list[str] | None = None,
                       system_table_block: str = "") -> int:
    """正文字符数（可见口径）。

    - 不计系统性生成、且被要求逐字保留的确定性块（冲突块、来源边界块）；
    - 计标题/列表/引用的文字，但不计其 Markdown 标记；
    - 不计空白与换行；保留中文、英文、数字与标点。

    前端只展示后端算出的这个数，不另行实现一套口径。
    """
    text = content or ""
    # 系统生成的确定性块（冲突块、系统数据表、来源归属块）不计入篇幅。
    # 计划要求「表格不计入模型分析正文篇幅」——否则一张大表会把章节顶到上限。
    for block in (conflict_block, system_table_block, SOURCE_ATTRIBUTION_BLOCK, *(required_disclosures or [])):
        if block and block in text:
            text = text.replace(block, "")
    for pattern in (_MD_HEADING_RE, _MD_LIST_RE, _MD_QUOTE_RE):
        text = pattern.sub("", text)
    text = _MD_EMPHASIS_RE.sub("", text)
    return len(re.sub(r"\s+", "", text))


# 使用冻结主体档案时的数据来源边界。由系统生成、逐字保留，不依赖模型复述固定措辞。
SOURCE_ATTRIBUTION_BLOCK = (
    "> 数据来源边界：本章使用项目冻结主体档案、募集说明书和信用评级报告中的允许事实；"
    "未经独立原件核验的内容仅作为披露信息使用。"
)


def _is_negated_mention(content: str, index: int) -> bool:
    """命中位置前一小句内出现否定词 → 是“禁止做某事”的边界表述，而非承诺性结论。

    例如“不得推断授信获批概率”“不构成投资建议”“不得表述为已具备可执行覆盖”必须放行；
    而“预计授信能够获批”“授信获批概率较高”仍然拒绝。此前靠逐词字符串替换处理，
    每加一个词都要写一次例外，这里改成统一的否定语境判断。
    """
    window = content[max(0, index - NEGATION_WINDOW):index]
    clause = NEGATION_CLAUSE_SPLIT_RE.split(window)[-1]
    if any(hedge in clause for hedge in NEGATION_HEDGES):
        return False
    return any(marker in clause for marker in NEGATION_MARKERS)


FALLBACK_REASON_CODES = (
    ("新数字", "unexpected_number"),
    ("禁止表达", "forbidden_expression"),
    ("未授权引用", "invalid_citation"),
    ("来源边界块", "source_attribution_missing"),
    ("冻结快照来源归属", "source_attribution_missing"),
    ("未授权的完整年度", "unauthorized_period"),
    ("季度或半年度", "unauthorized_period"),
    ("冲突断言块", "conflict_block_missing"),
    # 下列两条只用于**历史报告**：系统硬顶移除后，正文不再因篇幅回退，
    # 新生成的章节不会再产生这两个原因。保留映射是为了旧行仍能显示分类。
    ("篇幅修复后仍超出", "length_unrepaired"),
    ("超过输出长度", "length_exceeded"),
    ("模型调用或结构校验失败", "model_error"),
)


def fallback_reason_code(reason: str) -> str:
    """把回退原因归成稳定的短代码，便于审计与统计（原文仍保留在 fallback_reason）。"""
    text = str(reason or "")
    if not text:
        return ""
    for needle, code in FALLBACK_REASON_CODES:
        if needle in text:
            return code
    return "other"


def validation_diagnostics(validation: dict) -> dict:
    """非敏感诊断：只记判定结果（异常数字、被忽略的格式编号、禁止表达、来源块是否补回），
    不保存被拒绝的模型正文。
    """
    return {
        "unexpected_numbers": list(validation.get("unexpected_numbers") or []),
        "safe_unit_conversions": list(validation.get("safe_unit_conversions") or []),
        "ignored_format_numbers": list(validation.get("ignored_format_numbers") or []),
        "page_numbers_ignored": list(validation.get("page_numbers_ignored") or []),
        "forbidden_matches": list(validation.get("forbidden_matches") or []),
        "source_attribution_restored": bool(validation.get("source_attribution_restored", False)),
        # 篇幅压缩失败时（压缩稿引入新数字/无效引用等，已退回原正文）要能看出来，
        # 否则界面只会显示"偏长"，看不出压缩到底跑没跑、为什么没换成压缩稿。
        "length_repair_failed": bool(validation.get("length_repair_failed", False)),
        "length_repair_reasons": [str(item) for item in (validation.get("length_repair_reasons") or [])][:3],
    }


# 确定性章节经单章重生成时的占位原因（不是模型回退）
NO_MODEL_REASON = "该章节配置为确定性生成，不调用模型"
LENGTH_MESSAGES = {
    "short": "章节短于目标篇幅",
    "long": "章节超出目标篇幅上限",
}


def assess_chapter_length(content: str, context: dict) -> dict:
    """篇幅诊断。只判长度，不碰事实、引用、数字与审批边界。

    长度是**写作目标**，不是正确性属性：

    - 偏短（``effective_min_chars``）只作为质量告警，不触发修复、不回退——证据稀薄
      的章节本就该写得短；
    - 偏长（``effective_max_chars``）触发一次压缩；压缩后仍偏长但事实与证据校验通过
      时，保留正文，不回退、不阻断交付；
    - 系统没有独立的字符硬顶，字数也不参与 ``validate_chapter_output`` 的安全判定。
    """
    policy = context.get("length_policy") or {}
    raw_output_chars = len(content or "")
    # 只统计**分析正文**：系统表格、冲突披露块与来源边界块都不计入，
    # 否则一张后端附入的大表会把章节顶到目标上限。
    visible_content_chars = count_report_chars(
        content or "", str(context.get("conflict_block") or ""),
        context.get("required_disclosures") or None,
        str(context.get("system_table_block") or ""),
    )
    effective_max = int(policy.get("effective_max_chars") or 0)
    effective_min = int(policy.get("effective_min_chars") or 0)

    if effective_max and visible_content_chars > effective_max:
        status, target = "long", effective_max
    elif effective_min and visible_content_chars < effective_min:
        status, target = "short", effective_min
    else:
        status, target = "passed", 0
    return {
        "status": status,
        "raw_output_chars": raw_output_chars,          # 全文字符数：仅供审计
        "visible_content_chars": visible_content_chars,
        "effective_min_chars": effective_min,
        "effective_max_chars": effective_max,
        "delta": 0 if status == "passed" else visible_content_chars - target,
        "unit": "visible_chars",
        "message": LENGTH_MESSAGES.get(status, ""),
    }


# 数字与科目的绑定：数值允许集只保证"这个数在材料里出现过"，不保证"写在它名下的科目对"。
# 实测需要区分的真实错误是把筹资活动现金流写成投资活动现金流——两者数值都在材料里，
# 单看数字放行，看科目才拦得住。窗口只取命中位置之前的一小段：科目名就写在数字前面。
SUBJECT_BINDING_WINDOW = 16


def _record_label_index(records: list[dict]) -> tuple[dict[tuple[str, str], set[str]], set[str]]:
    """返回（数值 → 该数值对应的科目集合，本章出现的全部科目名）。"""
    index: dict[tuple[str, str], set[str]] = {}
    vocabulary: set[str] = set()
    for record in records:
        label = str(record.get("label") or "").strip()
        if not label:
            continue
        vocabulary.add(label)
        value, unit, _ = _canonical_measurement(record.get("value"), str(record.get("unit") or ""))
        index.setdefault((value, unit), set()).add(label)
        index.setdefault((value, ""), set()).add(label)
    return index, vocabulary


def _subject_binding_conflicts(mentions: list[dict], records: list[dict]) -> list[dict]:
    """数值与**紧邻科目**不一致 → 真实错误。

    只在"命中的数确实是某个科目的值、而正文在它前面刚点了**另一个**科目"时才报。
    没点科目（归属不明）不报错——那属于待核验，不属于错误。
    """
    if not records:
        return []
    index, vocabulary = _record_label_index(records)
    conflicts: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in mentions:
        labels = index.get((item["normalized_value"], item["normalized_unit"])) or index.get(
            (item["normalized_value"], "")
        )
        if not labels:
            continue
        prefix = str(item.get("context") or "").split(str(item["raw"]), 1)[0][-SUBJECT_BINDING_WINDOW:]
        written = {label for label in vocabulary if label and label in prefix}
        # 科目名用**包含**关系判定，不用全等：表格行的标签常常是多行拼起来的
        # （"归属于母公司所有者权益（或股东权益）\n合计"），正文里只写"合计"是它的子串，
        # 按全等比较会把这些正常引用判成"写了另一个科目"（实测误报）。
        if not written or any(
            written_label in label or label in written_label
            for written_label in written for label in labels
        ):
            continue
        key = (item["normalized_value"], item["normalized_unit"])
        if key in seen:
            continue
        seen.add(key)
        conflicts.append({
            "raw": item["raw"], "normalized_value": item["normalized_value"],
            "normalized_unit": item["normalized_unit"], "reason": "subject_mismatch",
            "context": item["context"],
            "expected_labels": sorted(labels), "written_labels": sorted(written),
        })
    return conflicts


def validate_chapter_output(content: str, citation_ids: list[str], context: dict) -> dict:
    config = context["prompt_config"]
    output = config["output_contract"]
    reasons: list[str] = []
    allowed = set(context.get("allowed_citation_ids", []))
    invalid = sorted(set(citation_ids) - allowed)
    if invalid:
        reasons.append(f"存在未授权引用：{','.join(invalid[:5])}")
    if not citation_ids and context.get("evidence"):
        reasons.append("章节未返回引用")
    # 篇幅不在这里判：字数超出用户设置的目标只触发一次压缩，不构成安全失败，
    # 也不能让整章回退到确定性草稿。篇幅诊断见 ``assess_chapter_length``。
    reasons.extend(_validate_narrative_shape(content, context))
    forbidden_matches: list[dict] = []
    # 裸词组与"决策语境"组分流：后者必须按完整语境判定（否定 / 引用原文 / 决策动词），
    # 不能用固定窗口共现糊过去。
    forbidden_patterns = [
        (pattern, "literal")
        for pattern in DEFAULT_FORBIDDEN + tuple(output.get("forbidden_patterns", []))
    ] + [(term, "decision") for term in DECISION_FORBIDDEN_TERMS]
    for pattern, kind in forbidden_patterns:
        if not pattern:
            continue
        seen_classifications: set[str] = set()
        for match in re.finditer(re.escape(pattern), content):
            if kind == "decision":
                classification = _decision_classification(content, match.start())
            else:
                classification = "safe_negative" if _is_negated_mention(content, match.start()) else "forbidden"
            if classification in seen_classifications:
                continue
            seen_classifications.add(classification)
            forbidden_matches.append({
                "term": pattern,
                "context": content[max(0, match.start() - 12):match.end() + 12].strip(),
                "classification": classification,
            })
    forbidden_terms = sorted({item["term"] for item in forbidden_matches if item["classification"] == "forbidden"})
    if forbidden_terms:
        reasons.append("命中禁止表达：" + "、".join(forbidden_terms))
    for phrase in output.get("required_phrases", []):
        conditional = {
            "披露信息待核验": context.get("has_missing"),
            "未取得签署版保证合同": not any(item.get("document_kind") == "担保合同" for item in context.get("evidence", [])),
        }
        if phrase and phrase not in content and conditional.get(phrase, True):
            reasons.append(f"缺少必需表达：{phrase}")
    if context.get("has_missing"):
        markers = output.get("required_missing_markers", [])
        if markers and not any(marker in content for marker in markers):
            reasons.append("材料缺口未明确标记")
    conflict_block = context.get("conflict_block", "")
    if context.get("conflicts") and conflict_block and conflict_block not in content:
        reasons.append("确定性冲突断言块未原样保留")
    table_block = str(context.get("system_table_block") or "")
    if table_block and table_block not in content:
        reasons.append("系统整理数据表未原样保留")
    if table_block:
        # 表格一律由系统生成。模型自行画表属于越权，且会与系统表形成重复披露。
        # 判定前先摘掉系统块本身，否则会把系统表误判成模型输出。
        analysis = content.replace(table_block, "")
        if conflict_block:
            analysis = analysis.replace(conflict_block, "")
        if _MARKDOWN_TABLE_ROW.search(analysis):
            reasons.append("章节正文自行输出了表格")
    if context.get("chapter_key") in {"financial", "cashflow", "debt"}:
        disallowed_periods = set(re.findall(r"(?:19|20)\d{2}(?:Q[1-4]|年(?:一|二|三|四)季度|年半年度)", content, flags=re.IGNORECASE))
        if disallowed_periods:
            reasons.append("章节包含季度或半年度财务期间")
        allowed_periods = set(context.get("allowed_periods", []))
        produced_years = _year_mentions(content)
        draft_years = _year_mentions(context.get("draft", ""))
        unauthorized_years = sorted(produced_years - allowed_periods - draft_years)
        if unauthorized_years:
            # 带上具体年份：回退原因必须可定位，不能只给笼统一句
            reasons.append("章节包含未授权的完整年度：" + "、".join(unauthorized_years))
        selected_scope = context.get("selected_scope")
        opposite_scope = "母公司口径" if selected_scope == "consolidated" else "合并口径" if selected_scope == "standalone" else ""
        if opposite_scope and opposite_scope not in context.get("draft", ""):
            # “不采用母公司口径/未混用母公司口径”是合规边界表述，不是混入口径。
            mentions = [match for match in re.finditer(re.escape(opposite_scope), content)
                        if not _is_negated_mention(content, match.start())]
            if mentions:
                reasons.append("章节混入非采用报表口径")
    # 来源归属改为确定性保留块：系统把块写进草稿并在模型漏写时补回，
    # 因此只要块在正文里即可，不再要求模型自行写出某一句固定措辞。
    attribution_block = str(context.get("source_attribution_block") or "")
    if attribution_block and attribution_block not in content:
        reasons.append("主体档案数据来源边界块未原样保留")
    elif not attribution_block and "profile_snapshot" in context.get("origin_types", []) and "冻结主体档案" not in content:
        reasons.append("主体档案事实缺少冻结快照来源归属")
    if any(item.get("assertion_status") == "single_source" for item in context.get("facts", [])):
        if "尚未独立验证" not in content:
            reasons.append("单一来源事实缺少未独立验证标记")
    if not context.get("guarantee_applicable", True) and context.get("chapter_key") in {"guarantee", "counter_guarantee"}:
        if "不适用" not in content:
            reasons.append("无增信项目未明确标记章节不适用")
    # 允许集 = 模型能看到的内容：确定性草稿、证据原文、事实/指标/核验、冲突断言，
    # 以及章节契约文本（规则与阈值口径，如"超过 50% 的关注阈值"）。
    # 这条校验的语义是"不得出现材料里没有的数字"，因此允许集必须覆盖模型的全部可见输入，
    # 否则模型照抄材料、只是格式不同（空格/单位/精度）就会被误判。
    allowed_text = " ".join([
        context.get("draft", ""),
        " ".join(item.get("quote", "") for item in context.get("evidence", [])),
        json.dumps(context.get("facts", []), ensure_ascii=False),
        json.dumps(context.get("metrics", []), ensure_ascii=False),
        json.dumps(context.get("cross_checks", []), ensure_ascii=False),
        json.dumps(context.get("conflicts", []), ensure_ascii=False),
        json.dumps(context.get("prompt_config", {}), ensure_ascii=False),
        " ".join(_measurement_texts(context)),
    ])
    # 数字比对做格式归一：千分位、小数尾零、展示精度（9.8901≡9.89），并在预定义换算表
    # 范围内接受等值量级换算（20000万元≡2亿元）。真正新增的金额/比例/评级数字仍然拒绝。
    allowed_numbers = {(item["normalized_value"], item["normalized_unit"]) for item in _number_mentions(allowed_text)}
    # 受控表格的**结构化记录**并进允许集：表里单元格是裸数字、单位写在表头，记录把两者绑定，
    # 于是模型正文写"204,709.77万元"或写裸数字都能匹配上，不再被判成"新数字"。
    table_records = context.get("table_records") or []
    if table_records:
        from .table_evidence import record_number_keys

        allowed_numbers |= record_number_keys(table_records)
    produced_mentions = _number_mentions(content)
    unexpected_numbers: list[dict] = []
    safe_unit_conversions: list[dict] = []
    seen_numbers: set[tuple[str, str]] = set()
    for item in produced_mentions:
        key = (item["normalized_value"], item["normalized_unit"])
        if key in seen_numbers:
            continue
        seen_numbers.add(key)
        if key in allowed_numbers:
            if item["converted"]:
                safe_unit_conversions.append({
                    "raw": item["raw"], "normalized_value": item["normalized_value"],
                    "normalized_unit": item["normalized_unit"], "reason": "safe_unit_conversion",
                })
            continue
        unexpected_numbers.append({
            "raw": item["raw"], "normalized_value": item["normalized_value"],
            "normalized_unit": item["normalized_unit"], "reason": "new_numeric_value",
            "location_type": item["location_type"], "context": item["context"],
        })
    if unexpected_numbers:
        reasons.append("章节包含确定性草稿和证据之外的新数字")
    # 数字在允许集里还不够：还要看它**写在哪个科目名下**。把筹资活动现金流写成投资活动
    # 现金流时两个数都在材料里，只有科目绑定能拦住（修订稿 §3.1）。
    # 只看**模型自己写的正文**：系统表格/冲突块/来源边界是系统生成的，其内部行名与表格
    # 标签天然重名，混进来会造出一批假"科目不匹配"（实测过）。
    analysis_text = content
    for frozen in (
        str(context.get("system_table_block") or ""),
        str(context.get("conflict_block") or ""),
        str(context.get("source_attribution_block") or ""),
    ):
        if frozen:
            analysis_text = analysis_text.replace(frozen, "")
    subject_conflicts = _subject_binding_conflicts(_number_mentions(analysis_text), table_records)
    if subject_conflicts:
        reasons.append("章节数字与所写科目不匹配")
    _, ignored_ordinals, page_numbers = _strip_format_numbers(content)
    ignored_format_numbers = sorted(set(ignored_ordinals), key=lambda value: (len(value), value))
    page_numbers_ignored = sorted(set(page_numbers), key=lambda value: (len(value), value))
    allowed_ratings = set(RATING_RE.findall(allowed_text))
    produced_ratings = set(RATING_RE.findall(content))
    if not produced_ratings <= allowed_ratings:
        reasons.append("章节包含确定性草稿和证据之外的新评级")
    allowed_entities = set(ENTITY_RE.findall(allowed_text))
    produced_entities = set(ENTITY_RE.findall(content))
    # 报告口径词汇（“半年度或母公司”这类）不是主体名，ENTITY_RE 会把它误当成公司名。
    produced_entities = {item for item in produced_entities
                         if not any(term in item for term in REPORT_VOCABULARY)}
    unknown_entities = [item for item in sorted(produced_entities) if not _entity_is_allowed(item, allowed_entities)]
    if unknown_entities:
        # 带上具体片段，便于区分“模型真的引入了新主体”与“改写换了上下文”。
        shown = "、".join(item[-20:] for item in unknown_entities[:3])
        reasons.append(f"章节包含确定性草稿和证据之外的新主体：{shown}")
    length = assess_chapter_length(content, context)
    # ``reasons`` 现在只剩安全原因：篇幅已在上面移出判定，所以"只有篇幅不合格"
    # 等价于"安全校验全过"。保留这个标志位是为了让调用方据此触发一次压缩修复。
    return {
        "status": "passed" if not reasons else "rejected",
        "reasons": reasons,
        "safety_reasons": reasons,
        "invalid_citations": invalid,
        "length": length,
        "length_only_failure": not reasons and length["status"] == "long",
        # 非敏感诊断（不保存被拒绝的模型正文）
        "unexpected_numbers": unexpected_numbers[:10],
        "subject_mismatches": subject_conflicts[:10],
        "safe_unit_conversions": safe_unit_conversions[:10],
        "ignored_format_numbers": ignored_format_numbers[:20],
        "page_numbers_ignored": page_numbers_ignored[:20],
        "forbidden_matches": forbidden_matches[:10],
    }
