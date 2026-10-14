"""Chapter-level evidence scoping, hashing and post-generation validation."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from rank_bm25 import BM25Okapi

from .chapter_prompts import (
    CONTEXT_SCHEMA_VERSION, PROMPT_CONTRACT_VERSION, chapter_prompt, chapter_prompt_is_overridden,
    effective_chapter_config, prompt_config_payload,
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
NUMBER_RE = re.compile(r"(?<![A-Za-z])\-?\d+(?:\.\d+)?")
# 数字比对用：带单位一起匹配（单位不得被模型自行换算，如 48000万元 ≠ 4.8亿元）。
NUMBER_WITH_UNIT_RE = re.compile(
    # **保留负号**：材料写 -1,234 而正文写成 1,234（或反向）是符号错误，必须能分辨。
    # 只认真正压在数字前的 -/−（U+2212），且其前一个字符不能是数字或字母——这样
    # "2023-2024"、"第 28-31 页" 里的连接号不会被当成负号。
    r"(?<![\dA-Za-z])(?P<sign>[-−])?(?P<number>\d[\d,]*(?:\.\d+)?)"
    # 数字与单位之间允许空格与换行（"30,000 万元"、"增加 101.71\n亿元"）；PDF 回流文本
    # 常在数字与单位间断行，否则同一数值在证据侧被读成裸数字、正文写全单位就判"新数字"。
    # ``个百分点`` 单列：它必须作为**独立单位**参与匹配，而不是被拆成裸数字——
    # 否则模型自行计算的"5.18 个百分点"会以裸数字形态去撞任意同值记录而漏放（修订稿 §3.2）。
    r"[ \t\u3000\r\n]*(?P<unit>个百分点|%|‰|倍|万元|亿元|万|亿|元|个月|月|年|天|日|次|人|家|项|股|笔)?"
)
# 标题编号、有序列表编号、引用短别名中的数字都不是业务数字。
MARKDOWN_ORDINAL_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*\d+[.、]?[ \t]*", re.MULTILINE)
LIST_ORDINAL_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.、])[ \t]+", re.MULTILINE)
CITATION_ALIAS_RE = re.compile(r"\bE\d{2,}\b")
# 格式序号（"第1个交易日""前1个交易日"）不是业务数值——收口方案 §一："格式序号…不作为
# 独立金额或指标取值核验"。北投 debt 实测："顺延至其后的第1个交易日" 里的 1 被读成裸
# 数字、在记录里查无同值，造出一条假 ambiguous_attribution 复核项。
ORDINAL_REFERENCE_RE = re.compile(r"(?:第|前)\d+(?:个|期|条|笔|次|名|位|款|项)")
# 完整日期字面量不是业务数字：2029-07-16 / 2029/7/16 / 2029.7.16 / 2029年7月16日。
# 不剔除的话，债券条款句"兑付日期为2029-07-16"会被读成 7 与 16 两个裸数字（验收实测）。
DATE_LITERAL_RE = re.compile(
    r"(?:19|20)\d{2}\s*(?:[-/.]|年)\s*\d{1,2}\s*(?:[-/.]|月)\s*\d{1,2}\s*日?"
)


def _decimal_text(value: Decimal) -> str:
    """Decimal → 十进制字符串：**不**用科学计数法、剥离小数尾零、保留全部有效位。

    与 ``table_evidence._decimal_text`` 同口径（那边保留尾零，这里剥离尾零用于比对）：
    只做无损归一，**不做展示精度舍入**——舍入交给 :func:`_values_match` 按记录精度
    以 ROUND_HALF_UP 处理。
    """
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _normalize_number(raw: str) -> str:
    """数值归一：去千分位、去小数尾零，**不做展示精度舍入**。

    48,000 → 48000；70.0 → 70；1.10 → 1.1；9.8901 → 9.8901。

    取舍（第三点细化方案 §二.2）：此前固定按 2 位小数舍入，会把记录里 4 位小数的
    指标压成 2 位——既丢精度，又无法表达"记录原始精度"这个展示规则。归一化只做
    **无损**处理；"展示精度"改由 :func:`_values_match` 按每条记录的原始 `raw`/`value`
    小数位以 ROUND_HALF_UP 匹配，禁止任意 epsilon 容忍。
    """
    text = str(raw).replace(",", "").strip()
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return text or "0"
    return _decimal_text(value)


def _decimal_places(text: object) -> int:
    """数值文本的小数位数（用于展示精度比对）：``"9.8901"``→4、``"180000"``→0。"""
    body = str(text)
    if "." not in body:
        return 0
    return len(body.split(".", 1)[1])


def _round_half_up(value: Decimal, places: int) -> Decimal:
    """按给定小数位以 ROUND_HALF_UP 舍入（不再是银行家舍入）。"""
    places = max(0, int(places))
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _values_match(a: Decimal, a_places: int, b: Decimal, b_places: int) -> bool:
    """数值相等（含**已批准展示精度**），禁止任意误差容忍。

    - 严格相等 → 命中；
    - 否则把精度更高的一侧按另一侧的位数以 ROUND_HALF_UP 舍入后比较——这对应
      "记录存 4 位小数、报告按 2 位小数展示"的场景，**只允许按另一侧明确给出的
      小数位舍入**，不引入任何 epsilon 或相对误差。
    """
    if a == b:
        return True
    if _round_half_up(b, a_places) == a:
        return True
    if _round_half_up(a, b_places) == b:
        return True
    return False


def _strip_format_numbers(content: str) -> tuple[str, list[str], list[str]]:
    """去掉格式性编号与日期字面量，返回（剩余文本, 被忽略的编号, 被忽略的页码）。

    标题 `### 1. 财务概览`、列表 `1. `、别名 `E01`、引用定位 `〔资料01〕`、日期
    `2029-07-16`、页码 `第 28、31、32 页` 都不是业务数字，此前会被当成"新增数字"误报。
    """
    text = content or ""
    ignored: list[str] = []
    page_numbers: list[str] = []
    for match in PAGE_REFERENCE_RE.findall(text):
        page_numbers.extend(re.findall(r"\d+", match))
    text = PAGE_REFERENCE_RE.sub(" ", text)
    # 「发行文件〔资料01〕」里的编号也是引用定位、不是业务数字（规则与页码同源，
    # 定义在 delivery_wording）。规范化会把材料标签写成该形态；若不剔除，`01` 会被
    # `_number_mentions` 读成一个数值 1，正常引用句在数字绑定里查无同值记录 → 整句
    # 被判"无证据声明"，`validated_content_sha256` 绑不回正文。`p0._numbers` 与
    # `workflow._numbers_supported` 已按同一口径剔除，本函数是最后一处漏网。
    for match in _REFERENCE_PATTERN.findall(text):
        ignored.extend(re.findall(r"\d+", match))
    text = _REFERENCE_PATTERN.sub(" ", text)
    # 完整日期字面量（"兑付日期为2029-07-16"）不是业务数值：它的月/日片段会被读成
    # 7、16 两个裸数字，在记录里查无同值即判"新数字"。
    for match in DATE_LITERAL_RE.findall(text):
        ignored.extend(re.findall(r"\d+", match))
    text = DATE_LITERAL_RE.sub(" ", text)
    for pattern in (MARKDOWN_ORDINAL_RE, LIST_ORDINAL_RE, CITATION_ALIAS_RE, ORDINAL_REFERENCE_RE):
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
from .delivery_wording import REFERENCE_PATTERN as _REFERENCE_PATTERN  # noqa: E402

PAGE_REFERENCE_RE = re.compile(_PAGE_PATTERN)


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
    """结构化键名里的单位线索：due_12m_pct → %，max_age_months → 个月，current_ratio → 倍。"""
    from .evidence_records import number_unit_hint

    return number_unit_hint(key)


def _threshold_unit_hint(item: dict) -> str:
    """阈值键名太泛（minimum/maximum/threshold）时，用规则标题补单位线索。"""
    from .evidence_records import threshold_unit_hint

    return threshold_unit_hint(item)


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
            # 规则阈值以裸数字存在（thresholds={"due_12m_pct": 50.0}），单位信息在键名或
            # 规则标题里：模型按材料写成"超过50%的关注阈值"是正确行为，必须能匹配上。
            # 带单位与裸数字两种形态都登记：单位提示缺失的历史阈值仍按裸数字放行。
            for mapping in (item.get("thresholds"),):
                if isinstance(mapping, dict):
                    for key, entry in mapping.items():
                        if entry is None or entry == "":
                            continue
                        hint = _unit_hint(key) or _threshold_unit_hint(item)
                        texts.append(f"{entry}{hint}")
                        if hint:
                            texts.append(str(entry))
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
    # 期限短语不是独立取值（收口方案 §一）："未来N个月/未来N~M个月" 是展望期限，其数字
    # 不作为金额或指标取值核验（北投 financial 缺口句 "未来12个月到期债务" 实测）。
    duration_spans = [
        (found.start(), found.end())
        for found in re.finditer(r"未来\s*\d+\s*(?:[~～至到—–―~\-]\s*\d+\s*)?个月", text)
    ]
    mentions = []
    for match in NUMBER_WITH_UNIT_RE.finditer(text):
        raw, unit = match.group("number"), match.group("unit") or ""
        sign = match.group("sign")
        if sign == "−":  # U+2212 归一成 ASCII 减号，否则 "-1234" 与 "−1234" 会被判成两个数
            sign = "-"
        raw_token = f"{sign or ''}{raw}"
        unit = UNIT_ALIASES.get(unit, unit)
        normalized, canonical_unit, converted = _canonical_measurement(raw_token, unit)
        if unit in {"", "年"} and re.fullmatch(r"(?:19|20)\d{2}", normalized):
            continue
        # 债务期限档/协议存续期（"1年以内""1年以上""N年内""N年期"）的 N 是期限分类，
        # 不是取值：直接在提取层剔除该数字（文本本身不改，标签比对不受影响）。北投 debt
        # 实测："1年以内合计385.63亿元" 的 1 在支持级被当成"查无记录的数字"。
        if unit == "年" and re.match(r"(?:及)?(?:以内|以上|以下|内|期)",
                                     text[match.end(): match.end() + 3]):
            continue
        if any(start <= match.start() and match.end() <= end for start, end in duration_spans):
            continue
        window_start = max(0, match.start() - 20)
        # 比率分子回指专用：`context` 的 ±20 窗口看不到**前一分句**，而正文常写
        # "其他应收款由…降至…，募集说明书披露其占总资产比重由1.77%降至1.29%"——
        # 分子在前一分句、本分句只剩代词"其"。这里另存一段更宽的前文（不含数字之后的
        # 内容），只给 `_ratio_numerator_labels` 的回指用，**不改动** `context`/`prefix_offset`
        # 已有的语义（它们承担期间/主体/科目窗口，加宽会引入跨分句误判）。
        # 窗口取 160 字：金额本身常占 20+ 字（"1,548,235.80万元"），60 字会把科目名挤掉。
        wide_start = max(0, match.start() - 160)
        mentions.append({
            "raw": f"{raw_token}{unit}", "normalized_value": normalized, "normalized_unit": canonical_unit,
            "converted": converted, "location_type": "body",
            # 正文原始小数位（换算前）：展示精度按它对齐，避免用换算后的位数误判。
            "precision": _decimal_places(raw_token),
            # 正文是否显式写了单位：裸数字要另走"单位/归属是否唯一"的核验。
            "has_unit": bool(match.group("unit")),
            # 截取命中处前后一小段上下文：既用于定位"新数字"，也用于核对数字的**归属**
            # （科目/期间/主体/口径都写在数字前面，±20 字内；不保存整段正文）。
            # 窗口**不加宽**：更宽的窗口会把上一分句的科目/年份/主体捞进来（实测中邮
            # 加宽后凭空多出 party/period 冲突）。年份截断改由 PERIOD_TOKEN_RE 的
            # 左边界解决，而不是靠加宽窗口。
            "context": text[window_start:match.end() + 20].strip(),
            # 命中位置在 ``context`` 内的偏移（数字起点）。归属窗口必须用**偏移**切前文，
            # 不能用 ``context.split(raw)``：模型常把数字与单位写成 "816,849.21 万元"（中间
            # 有空格），而 ``raw`` 是无空格拼法，split 找不到就返回整段 context，
            # ``[-16:]`` 便取到**数字之后**的下一个科目名（实测中邮：把所有者权益 816,849.21
            # 万元读成下一句的"营业收入"），凭空造出 label_mismatch 误退。
            "prefix_offset": match.start() - window_start,
            # 比率回指专用宽前缀（见上）。缺省不参与任何既有判定。
            "wide_prefix": text[wide_start:match.start()],
        })
    return mentions
RATING_RE = re.compile(r"\b(?:AAA|AA|A|BBB|BB|B|CCC|CC|C)(?:[+-])?\b")
# 主体名 = **法定名称**结尾的汉字片段：有限公司 / 股份有限公司 / 集团有限公司 / 有限责任公司。
# 裸"公司"（"上述安排有利于公司""关于发行人公司""其中公司""2025年末公司"）是散文里的普通
# 名词，不是主体名——只认法定结尾，行文片段整体由正则排除，不再依赖词表逐条追打
# （实测两轮验收里行文措辞层出不穷，词表永远追不上；登记简称由 `_entity_is_allowed`
# 的登记表关系承担，不以本规则提取）。
# 注意"有限责任公司"**不以"有限公司"结尾**（有-限-责-任-公-司），必须显式列出。
ENTITY_RE = re.compile(
    r"[\u4e00-\u9fff]{2,30}(?:股份有限公司|集团有限公司|有限责任公司|有限公司)"
)
# PDF 回流把名字拆行的接合规则：只在**汉字与汉字之间**去掉换行，表格分隔符
# （`|`、空格、数字）保持原样，不会把相邻两行粘成一个名字。
_HAN_WRAP_RE = re.compile(r"(?<=[\u4e00-\u9fff])[\n\r]+(?=[\u4e00-\u9fff])")


# 法定名结尾（按**最长优先**排列；「有限责任公司」不以「有限公司」结尾，必须显式列出）。
LEGAL_NAME_ENDINGS = ("股份有限公司", "集团有限公司", "有限责任公司", "有限公司")
# 共享法定名尾段放行的最小**字号**长度（去掉法定后缀后的汉字数）。
# 中邮实测「联合资信评估股份有限公司」字号 6 字；而"浙江省/江苏省交通投资集团有限公司"
# 的公共尾段字号只有 5 字（"省交通投资"）——不达阈值仍拦，防"同尾不同主体"被放行。
MIN_SHARED_NAME_CORE = 6


def _shared_legal_name_tail(left: str, right: str) -> str | None:
    """两侧片段的最长公共后缀里，以法定名结尾且**字号 ≥6 字**的尾段；不满足返回 None。

    ENTITY_RE 会连带吃进名字前的上下文（run-on 头部）：两侧头部不同时"包含"关系不成立，
    同一主体会被误判"新主体"（中邮 rating 实测——引文侧
    "…5855 号/特此公告＋联合资信评估股份有限公司" vs 产出侧
    "评级结论根据＋联合资信评估股份有限公司"）。这里按**共享的完整法定名尾段**（含字号）
    判同一主体；这不是"仅凭后缀相似"：字号为空或过短的公共尾段仍拦
    （"华辰新能源/精密制造"公共尾段只有"有限公司"）。残边界：仅前缀不同、字号与后缀完全
    相同的两家公司会按同一主体放行（如"上海/北京东方资产评估股份有限公司"）。
    """
    limit = min(len(left), len(right))
    length = 0
    while length < limit and left[-1 - length] == right[-1 - length]:
        length += 1
    if not length:
        return None
    tail = left[len(left) - length:]
    for ending in LEGAL_NAME_ENDINGS:
        if tail.endswith(ending):
            core = tail[: len(tail) - len(ending)]
            return tail if len(core) >= MIN_SHARED_NAME_CORE else None
    return None


def _entity_is_allowed(produced: str, allowed: set[str], registry: dict[str, str] | None = None) -> bool:
    """主体名比对：**全等 / 包含 / 共享法定名尾段 / 登记简称**四类，移除"后缀相似"放行。

    修订稿 §3.1 要求"移除仅凭公司名称后缀相似就放行"：后缀相似会把
    "华辰新能源有限公司"与"华辰精密制造有限公司"这类近名公司混为一谈。
    因此只保留：① 字符串全等；② 任一方包含另一方（覆盖 ENTITY_RE 连带吃进的上下文
    头部）；③ **共享完整法定名尾段**（含 ≥6 字字号——两侧 run-on 头部不同、名字本体相同
    时仍能判同一主体，中邮 rating 实测）；④ 两者能通过**项目主体登记表**（规范名称 +
    登记简称）解析到同一主体。真主体名固定在片段尾部时，包含关系仍然成立；近名公司
    不再共享任一关系即被拦下。
    """
    for item in allowed:
        if not item:
            continue
        if produced == item or produced in item or item in produced:
            return True
        if _shared_legal_name_tail(produced, item):
            return True
        if registry:
            resolved = registry.get(produced)
            if resolved and resolved == registry.get(item):
                return True
    return False


def _extract_entities(text: str) -> set[str]:
    """主体名提取：**允许集与产出使用同一函数**（修订稿 §3.1）。

    ENTITY_RE 只认**法定名称**结尾的片段（有限公司/股份有限公司/集团有限公司/
    有限责任公司）：片段头部会连带吃进少量上下文，由 `_entity_is_allowed` 的包含关系
    与登记表消化；裸"公司"是散文里的普通名词（"有利于公司""关于发行人公司""其中公司"），
    整体不进提取结果。两侧必须走同一套提取，否则一侧有、一侧没有就会造出假"新主体"。

    PDF 回流会把名字拆行（引文实测"华夏银行股\\n份有限公司"），先只把**汉字与汉字之间**
    的换行接合：表格分隔符（`|`、空格、数字）保持原样，不会把相邻两行粘成一个名字。
    """
    linear = _HAN_WRAP_RE.sub("", text or "")
    return set(ENTITY_RE.findall(linear))


# 现金流科目登记表（修订稿 §3.1）：经营/投资/筹资三类用**显式登记 + 明确别名**区分，
# 不再只靠逐字包含。把"经营现金流"这类简称与"经营活动产生的现金流量净额"这类全称
# 归到同一类；反之，"投资活动现金流量净额"与"筹资活动现金流量净额"即使字数接近，
# 也分属不同类，防止"把筹资写成投资"被包含关系漏放。
CASH_FLOW_LABEL_REGISTRY: dict[str, tuple[str, ...]] = {
    "operating": (
        "经营活动产生的现金流量净额", "经营活动现金流量净额", "经营活动现金流净额",
        "经营活动现金流", "经营现金流", "经营活动现金流量", "经营活动产生",
    ),
    "investing": (
        "投资活动产生的现金流量净额", "投资活动现金流量净额", "投资活动现金流净额",
        "投资活动现金流", "投资现金流", "投资活动现金流量", "投资活动产生",
    ),
    "financing": (
        "筹资活动产生的现金流量净额", "筹资活动现金流量净额", "筹资活动现金流净额",
        "筹资活动现金流", "筹资现金流", "筹资活动现金流量", "筹资活动产生",
    ),
}


def _cash_flow_category(label: str) -> str:
    """把现金flow类科目标签归到 operating / investing / financing；归不到返回空串。"""
    text = str(label or "")
    for category, aliases in CASH_FLOW_LABEL_REGISTRY.items():
        if any(alias in text for alias in aliases):
            return category
    return ""


# 科目名里的中文数字：表格行常写"1年以上"，正文常写"一年以上"，两者是同一期限档。
# 只在**紧贴量词**时转换（"一年以上"→"1年以上"），且前一字不得仍是中文数字——
# 否则"二十一年"会被切成"二十1年"（把 21 读成 201）。
_CN_NUMERAL_DIGITS = {
    "一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5",
    "六": "6", "七": "7", "八": "8", "九": "9", "十": "10",
}
_CN_NUMERAL_MEASURE_RE = re.compile(
    r"(?<![一二两三四五六七八九十])([一二两三四五六七八九十])(?=[年月日个期倍成季])"
)


def _normalize_label_numerals(text: str) -> str:
    """科目名/期限档里的中文数字 → 阿拉伯数字（仅"数字紧贴量词"的形态）。

    正文写"一年以上到期的长期有息债务"、记录 label 写"1年以上"，语义相同但字面不同，
    不归一化就配不上，会把科目正确的数字判成 label_mismatch（中邮 debt 94.85% 实测：
    回指正确取到"一年以上到期的长期有息债务"，却因"一"≠"1"而报冲突）。
    """
    if not text:
        return text
    return _CN_NUMERAL_MEASURE_RE.sub(
        lambda match: _CN_NUMERAL_DIGITS.get(match.group(1), match.group(1)), text
    )


def _labels_compatible(written_label: str, record_label: str) -> bool:
    """科目名兼容：包含关系，或同属现金流登记表的同一类别。

    包含关系用于覆盖表格行标签多行拼接（正文只写子串）；登记表用于把简称/全称
    归到同一类，同时保证不同类别（经营/投资/筹资）不会互相放行。
    两侧先做中文数字归一（"一年以上" ≡ "1年以上"），再判包含。
    """
    if not written_label or not record_label:
        return False
    written_label = _normalize_label_numerals(written_label)
    record_label = _normalize_label_numerals(record_label)
    if written_label in record_label or record_label in written_label:
        return True
    written_category = _cash_flow_category(written_label)
    record_category = _cash_flow_category(record_label)
    return bool(written_category) and written_category == record_category


def _party_compatible(written: str, subject: str, registry: dict[str, str] | None = None) -> bool:
    """主体名兼容判定：**全等 / 包含 / 登记简称**三类（移除"共享后缀"放行）。

    修订稿 §3.1 要求移除"仅凭后缀相似就放行"：近名公司（华辰新能源 vs 华辰精密制造）
    不再因为共享长后缀而被当作同一主体。保留的三种关系：
      ① 全等；② 任一方包含另一方（覆盖 ENTITY_RE 吃进上下文头部的情形）；
      ③ 通过项目主体登记表（规范名称 + 登记简称）解析到同一主体。
    任一侧缺失时按兼容处理（不判定）。
    """
    if not written or not subject:
        return True
    if written == subject or written in subject or subject in written:
        return True
    if registry:
        resolved = registry.get(written)
        if resolved and resolved == registry.get(subject):
            return True
    return False


_ENTITY_LEGAL_SUFFIX_RE = re.compile(
    r"(?:股份有限公司|集团有限公司|有限责任公司|有限公司|公司)$"
)


def _project_entity_registry(project_id: str) -> dict[str, str]:
    """项目主体登记表：名称变体 → 规范名称（含**登记简称**）。

    规范名称取 ``project_entities.name`` 与绑定主体档案 ``entity_profiles.name``；
    登记简称由规范名称剥离常见法定后缀得到（"北京市基础设施投资有限公司"→
    "北京市基础设施投资"）。查不到（隔离库、单测手搭上下文）返回空字典，
    调用方据此退回"全等 / 包含"判定。
    """
    names: set[str] = set()
    try:
        for item in rows("SELECT name FROM project_entities WHERE project_id=?", (project_id,)):
            name = str(item.get("name") or "").strip()
            if name:
                names.add(name)
    except Exception:
        pass
    try:
        for item in rows(
            "SELECT p.name AS name FROM project_profile_roles r "
            "JOIN entity_profiles p ON p.id=r.profile_id WHERE r.project_id=?",
            (project_id,),
        ):
            name = str(item.get("name") or "").strip()
            if name:
                names.add(name)
    except Exception:
        pass
    registry: dict[str, str] = {}
    for name in names:
        registry[name] = name
        short = _ENTITY_LEGAL_SUFFIX_RE.sub("", name)
        if len(short) >= 2:
            registry.setdefault(short, name)
    return registry


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


# 校验规则版本：任何改变"允许/拒绝"判定的规则调整都应递增（纳入输入哈希，修订稿 §2.1）。
VALIDATION_RULES_VERSION = "v1"


def _materials_fingerprint(project_id: str) -> str:
    """项目材料版本指纹：文档集（id+sha256）排序后取 sha256；查询失败返回空串。"""
    try:
        items = rows(
            "SELECT id,COALESCE(sha256,'') AS sha256 FROM documents WHERE project_id=? ORDER BY id",
            (project_id,),
        )
    except Exception:
        return ""
    joined = "|".join(f"{item['id']}:{item['sha256']}" for item in items)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


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
            # 主体维度随事实进上下文：统一冻结记录（evidence_records）要用它做归属绑定。
            "subject": fact.get("subject", ""), "subject_id": fact.get("subject_id", ""),
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
            "metric_key": metric["metric_key"], "name": metric.get("name", ""),
            "period": metric.get("period", ""), "value": metric.get("value"),
            "unit": metric.get("unit", ""), "formula": metric.get("formula", ""),
            "status": metric.get("status", "calculated"), "source_ids": metric["source_ids"],
            "input_fact_ids": metric["input_fact_ids"],
            # 规则版本随指标一起冻结（计划 §三：「冻结公式、输入记录ID、来源及规则版本」）。
            # `financial_metrics.policy_version` 就是生成这批指标时的策略版本——正文引用
            # 系统计算值时，能回溯到"按哪一版公式算出来的"。
            "rule_version": str(metric.get("policy_version") or ""),
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
    # 三件套单一来源（第三点细化方案 §二.1）：**一次**构建同时产出表格正文、结构化记录与
    # 快照 blocks——禁止三个出口分别重查当前表格。生成期取当前抽取表（current=True），
    # 随后由 write_report_section 在同一事务内冻结快照。表格问题不阻断整章生成。
    issuer = row("SELECT company_name FROM projects WHERE id=?", (project_id,))
    table_subject = str((issuer or {}).get("company_name") or "")
    system_table_block, table_records, table_blocks = "", [], []
    try:
        from .report_tables import build_chapter_tables

        _built_tables = build_chapter_tables(project_id, chapter_key, subject=table_subject, current=True)
        system_table_block = _built_tables.markdown
        table_records = list(_built_tables.records)
        table_blocks = list(_built_tables.blocks)
    except Exception:
        # 表格问题不阻断整章生成：构建失败按"无表"处理（与 _system_table_block 的既有取舍一致）。
        pass
    # 统一冻结记录（三项遗留修复方案 §一 + 收口方案 §一）：一次产出，
    # 随上下文中转，由 write_report_section 与表格快照同事务冻结；章节校验与后置支持
    # 校验读取**同一份**——事实、指标、规则不再因"不在受控表格里"被判无证据；
    # 引文里的披露数字（增幅/子公司/评级指标/年度表行）按证据片段冻结为 disclosure 记录。
    from .evidence_records import build_evidence_records

    evidence_records = build_evidence_records(
        facts=selected_facts, metrics=metrics, checks=checks, table_records=table_records,
        evidence=evidence,
    )
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
    # 结构化允许记录已随三件套**一次构建**产出（见上文）：带符号数值 + 单位 + 科目 + 期间 +
    # 来源 + 页码，供章节校验与后置校验共用；只把表格 Markdown 拼进允许文本是不够的——
    # 表里单元格是裸数字、单位写在表头，模型在正文补上单位就会被判成"新数字"。
    payload = {
        "project_id": project_id, "chapter_key": chapter_key, "prompt_config": config,
        "prompt_version": prompt_version,
        "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(), "draft": deterministic_draft,
        # 生效 Prompt 原文：此前只留 sha256，台账要保存**完整冻结输入**（财务修复计划 §四）
        # 就必须把原文带上——只有哈希无法回答"当时指令到底怎么写的"。
        "prompt_text": prompt_text,
        # Prompt 契约编译版本（三项遗留修复方案 §二）：接口与审计用于判断"库里 Prompt 是否
        # 早于当前编译器"；旧句迁移在 `llm._report_request` 里幂等完成。
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        # 系统表格快照哈希：请求只下发摘要，哈希用于审计"本章分析基于哪一版表格"。
        "table_snapshot_hash": (
            hashlib.sha256(system_table_block.encode("utf-8")).hexdigest() if system_table_block else ""
        ),
        "facts": selected_facts, "metrics": metrics, "cross_checks": checks, "evidence": evidence,
        "conflicts": relevant_conflicts, "conflict_block": conflict_block,
        "system_table_block": system_table_block,
        # 表后分析的章节职责随上下文投递，不依赖可被覆盖的章节 prompt。
        "system_table_role": SYSTEM_TABLE_ROLE if system_table_block else "",
        # 结构化允许记录（受控表格的业务格）：数字校验与"科目绑定"校验都读它。
        "table_records": table_records,
        # 统一冻结记录（四类）：章节校验与后置支持校验的唯一记录来源。
        "evidence_records": evidence_records,
        # 系统冻结块：生成时必须逐字保留，且不计入篇幅统计。
        "required_disclosures": [block for block in (conflict_block, system_table_block, source_attribution_block) if block],
        "source_attribution_block": source_attribution_block,
        "relevant_review_tasks": relevant_tasks, "has_missing": has_missing,
        "allowed_periods": annual_periods, "selected_scope": selected_scope,
        "origin_types": origin_types, "guarantee_applicable": guarantee_applicable,
        "length_policy": config.get("length_policy") or {},
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        # 材料版本与校验规则版本一并纳入输入哈希（第三点细化方案 §二.1）。
        "materials_version": _materials_fingerprint(project_id),
        "validation_rules_version": VALIDATION_RULES_VERSION,
        "llm_applicable": not (chapter_key in {"guarantee", "counter_guarantee"} and not guarantee_applicable),
    }
    payload["input_hash"] = _json_hash(payload)
    # 冻结原表 blocks（表格正文/记录/快照同源）：操作数据不进哈希（正文与记录已在哈希内）。
    payload["table_blocks"] = table_blocks
    payload["allowed_citation_ids"] = [item["id"] for item in evidence]
    # 主体登记表（规范名称 + 登记简称）：供主体归属核对（全等/包含/登记简称）。
    # 属查询派生数据，与 table_blocks 一样在 input_hash 之后附加。
    payload["entity_registry"] = _project_entity_registry(project_id)
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


def _narrative_style_notes(content: str, context: dict) -> list[str]:
    """文风类**写作提示**（计划 §二 line 17）：技术化起句、系统术语、列表形态。

    ⚠️ 返回值**不是失败原因**——它只进诊断，**不触发整章回退**。计划明文：
    「文风、开头和模糊术语检查改为写作提示，不触发整章回退；事实真实性、合法引用及
    禁止审批结论继续强制校验」。这些约束已经作为写作指引写进 Prompt
    （`chapter_prompts` 编译 "不得以以下技术化表达起句：…"/"不得出现以下系统术语：…"），
    这里只是把结果**记下来供人工与接口查看**。

    历史行为：曾 `reasons.extend(...)` → 命中即 `rejected` → 整章回退确定性模板。
    后果是"以'经模型分析'起句"这种纯文风问题会把一章的模型分析全丢掉。

    第三点细化方案 §三.1："无自然段数量限制，无独立正文字符硬上限"。因此本函数
    **不对自然段数量做任何强制**；``narrative_contract`` 里的 min/max_paragraphs
    仅作为写作指引进 Prompt，不参与判定。
    """
    chapter_key = str(context.get("chapter_key") or "")
    if not chapter_key:
        return []
    narrative = (context.get("prompt_config") or {}).get("narrative_contract") or {}
    if not narrative:
        return []
    notes: list[str] = []
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
            notes.append(f"章节以技术化表达起句：{opening}")
    searchable_content = content.replace(str(context.get("conflict_block") or ""), "")
    for term in narrative.get("prohibited_terms", []):
        if term and term.lower() in searchable_content.lower():
            notes.append(f"章节出现禁止的系统术语：{term}")

    if narrative.get("list_mode") == "prohibited":
        controlled = content.replace(str(context.get("conflict_block") or ""), "")
        if any(re.match(r"\s*(?:[-*+]\s+|\d+[.、]\s+)", line) for line in controlled.splitlines()):
            notes.append("章节不应使用项目符号或编号列表")
    return notes


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
        # 归属冲突（科目/期间/主体/口径）：回退原因必须能定位到具体数字与所写归属，
        # 否则界面上只剩一句笼统的"不匹配"。
        "attribution_conflicts": list(validation.get("attribution_conflicts") or []),
        # 需要人工核验的发现（三态里的 needs_review）：归属不明、跨记录拼接、决策语境不确定。
        "needs_review_items": list(validation.get("needs_review_items") or []),
        "needs_review_count": int(validation.get("needs_review_count") or 0),
        "safe_unit_conversions": list(validation.get("safe_unit_conversions") or []),
        "ignored_format_numbers": list(validation.get("ignored_format_numbers") or []),
        "page_numbers_ignored": list(validation.get("page_numbers_ignored") or []),
        "forbidden_matches": list(validation.get("forbidden_matches") or []),
        "source_attribution_restored": bool(validation.get("source_attribution_restored", False)),
        # 篇幅压缩失败时（压缩稿引入新数字/无效引用等，已退回原正文）要能看出来，
        # 否则界面只会显示"偏长"，看不出压缩到底跑没跑、为什么没换成压缩稿。
        "length_repair_failed": bool(validation.get("length_repair_failed", False)),
        "length_repair_reasons": [str(item) for item in (validation.get("length_repair_reasons") or [])][:3],
        # 文风写作提示（计划 §二 line 17）：非阻断，但要在诊断里看得见——
        # 否则"这一章起句技术化"就只剩 Prompt 里一句无人复核的指引。
        "style_notes": [str(item) for item in (validation.get("style_notes") or [])][:10],
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


# 数字与**科目/期间/主体/口径**的绑定（修订稿 §3.1）：数值允许集只保证"这个数在材料里
# 出现过"，不保证"写在对的归属下"。实测需要区分的真实错误：把筹资活动现金流写成投资活动
# 现金流（科目错）、把 2024 年末值写成 2025 年末（期间错）、把别的主体值写到本主体名下
# （主体错）、把母公司口径值写在合并口径下（口径错）。
# 判定原则是**宁可漏报也不误报**：没写归属词、记录缺字段、口径未注明都不报错——那属于
# 待核验，不属于错误。窗口只取命中位置之前的一小段：归属词就写在数字前面。
SUBJECT_BINDING_WINDOW = 16
# 年份令牌要求**左侧非数字、非连字符**：±20 字窗口会把"2023—2025"截成"3—2025"，
# 裸 ``(?:19|20)\d{2}`` 便在残句里读到 2025，把本属 2023 的数字判成期间错（中邮实测）。
# 左侧边界让截断残片无法冒充完整年份；同时把上下文窗口放宽到 ±40，减少截断本身。
PERIOD_TOKEN_RE = re.compile(r"(?<![\d\-—‑])(?:19|20)\d{2}")
# 分句边界：期间归属只看数字所在分句，跨分句的年份属于前一个数字。
_CLAUSE_SPLIT_RE = re.compile(r"[，。；;！？!?\n]")
# 比较基准年（"较2023年末增长""与2023年末相比"）不是数字所属期间，跳过期间核对。
PERIOD_COMPARATIVE_MARKERS = ("较", "比", "同比", "环比", "相比", "相较", "与")
# 只有带"口径"二字的才是报表口径声明；裸"合并/母公司"常是普通词（"合并成本"），
# 误当口径会造出假冲突。口径声明词 → 记录 scope 字段里的核心词。
SCOPE_TERMS = {"合并口径": "合并", "母公司口径": "母公司"}
# 合计/小计/总计是**行角色**，不是科目名：数值命中这些行时，正文在数字旁写的科目描述
# 无从与总行直接比对（账户全称与子项都写得出来），按待核验处理而非硬冲突。
GENERIC_TOTAL_LABELS = ("合计", "小计", "总计")
# 表格行的「其中：」前缀是排版噪声，不是科目名的一部分（正文只写"公司债券"）。
_LABEL_PREFIX_RE = re.compile(r"^其中[：:]?")
# 负数由**词义**表达的记号："亏损118.17亿元""净亏3亿元"——等价于 -118.17亿元。
_LOSS_SIGN_RE = re.compile(r"亏损|净亏|为负|负值|亏损额")
ATTRIBUTION_REASON_MESSAGES = {
    "label_mismatch": "章节数字与所写科目不匹配",
    "period_mismatch": "章节数字与所写期间不匹配",
    "party_mismatch": "章节数字与所写主体不匹配",
    "scope_mismatch": "章节数字与所写口径不匹配",
}


# 「X占Y的比例」「X占Y比重」「X在Y中的占比」——比率陈述里**分子**写在"占/在"之前，
# 分母写在之后。记录 label 记的是分子（"存货""货币资金"），而数字紧跟在分母后面，
# 只取数字前 16 字窗口会**只命中分母**（"资产总额"），把科目正确的比率判成
# label_mismatch（北投 8.32%/3.8% 实测；对照实验：整句改成"存货占比"后冲突 1→0）。
# 命中即把"占/在"之前的分子名也纳入候选科目。
RATIO_NUMERATOR_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9（）()、和与及]{1,24}?)"
    r"\s*(?:占|在)[\u4e00-\u9fffA-Za-z0-9（）()]{1,16}?"
    r"(?:的)?(?:比例|比重|占比|份额)"
)
# 分母侧的收尾词：分子名不能越过这些词继续向左吞并（"资产端，货币资金…占总资产比重"）。
RATIO_NUMERATOR_STOP_RE = re.compile(r"[，。；;：:！？!?\n（(]")
# 分子名左侧的材料/叙述前缀必须剥掉："募集说明书披露其占总资产比重" 的 `group(1)` 是
# "募集说明书披露其"，真分子"其他应收款"在**前一分句**。这些词本身不是科目名。
# 注意：代词（其/该/上述/前述）**不在此列**——它们由 `RATIO_PRONOUN_RE` 单独判定并触发回指；
# 若一并剥空，"募集说明书披露其" 会以空串静默通过，掩盖"分子其实是代词"这一事实。
RATIO_NUMERATOR_PREFIX_RE = re.compile(
    r"^(?:募集说明书|发行文件|评级报告|外部信用评价文件|报告|材料|发行人|公司"
    r"|披露|载明|显示|表明|说明|指出|提及|约定"
    r"|以及|和|与|及)+"
)
# 回指代词：分子写在"占/在"之前但只是代词（"其/该/它/上述"）时，真正的科目名在
# **前一分句**（"其他应收款由…降至…，募集说明书披露其占总资产比重由1.77%降至1.29%"）。
RATIO_PRONOUN_RE = re.compile(r"^(?:其|该|它|上述|前述|以上|之)+$")
# 前缀剥完后若残留一个**尾部代词**（"募集说明书披露其" → "其"），同样说明分子是代词。
RATIO_PRONOUN_TAIL_RE = re.compile(r"(?:其|该|它|上述|前述|以上|之)+$")
# 回指时回溯到**前一分句**后，末尾往往还挂着谓词/连接词（"由""降至""维持在"）、
# 枚举连接符（"和""、"）与数值尾巴，剥掉它们才拿得到科目名主体。
# 与 `RATIO_UNIT_TAIL_RE` 分开：这里剥的是"尾缀串"。
RATIO_SUBJECT_TAIL_STRIP_RE = re.compile(
    r"(?:万元|亿元|个百分点|个月|分别|以及|左右|维持|保持|处于|元|万|亿|%|‰|倍|天|次|家|项|股|笔"
    r"|月|年|末|期|的|止|时|点|约|达|为|至|降|增|升|减|变|动|由|自|从|在|占|和|及|与|、|-|−)+$"
)
# 回溯前一分句时，从末尾取一个科目名形态的片段（汉字开头、可含字母数字括号）。
# 必须排除"金额+单位"尾巴（"…降至1,251,848.55万元" → 不能把"万元"当科目名）。
RATIO_SUBJECT_TAIL_RE = re.compile(r"([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9（）()]*)$")
# 片段开头的"年份/数字壳"不是科目名的一部分："2023年存货" → "存货"；"2025年末" 剥空。
_RATIO_LEAD_SHELL_RE = re.compile(r"^(?:(?:19|20)\d{2}(?:年度|年末|年底|年)?|[0-9]+(?:\.[0-9]+)?%?)\s*")
RATIO_UNIT_TAIL_RE = re.compile(r"^(?:万元|亿元|元|万|亿|%|‰|倍|个百分点|天|次|家|项|股|笔|个月|月|年|末|期)$")
# 科目名不得以这些词**开头**（它们是谓词/连接词残段，不是科目名主体）：回指时若取到的
# 候选以它们开头，说明谓词没剥干净，继续往前找。
RATIO_SUBJECT_REJECT_HEAD_RE = re.compile(
    r"^(?:的|由|自|从|至|为|达|约|降|增|升|减|变|维持|保持|处于|以及|和|与|及|在|点|时)"
)


# 比例结构的"分子在左、分母在右"：提取科目名前先把文本**裁到第一个比例标记
# （占/比重/比例/份额）左侧**——右侧是分母与谓词，左侧才是分子所在。
# "应收账款规模在资产总额中占比较低" → "应收账款规模在资产总额中"；
# "占总资产比重维持在" → ""（本分句自身没有分子 → 交给回指）。
def _cut_ratio_left(text: str) -> str:
    positions = [
        position for marker in ("占", "比重", "比例", "份额")
        if (position := text.find(marker)) >= 0
    ]
    return text[: min(positions)] if positions else text


def _ratio_numerator_candidate(chunk: str) -> str | None:
    """从一个分句里剥出科目名主体；剥不出合形态的科目名就返回 ``None``。

    分句形态常为"其他应收款由2023年末的1,548,235.80万元降至2025年末的1,251,848.55万元"：
    末尾是"数值+单位"，往左还有"年份+的""谓词"。逐层剥掉这些尾巴后，剩下的尾部汉字串
    才是科目名（`其他应收款`）。
    """
    tail_text = chunk.strip()
    if not tail_text:
        return None
    # 反复砍掉末尾的"数字/单位/年份/谓词/连接符"片段，直到不再变化。
    for _ in range(8):
        before = tail_text
        tail_text = re.sub(r"[0-9][0-9,\\.]*\s*$", "", tail_text)
        tail_text = RATIO_SUBJECT_TAIL_STRIP_RE.sub("", tail_text)
        if tail_text == before:
            break
    tail = RATIO_SUBJECT_TAIL_RE.search(tail_text)
    if not tail:
        return None
    candidate = tail.group(1).strip()
    # 年份/数字壳先剥掉（"2023年存货" → "存货"）；剥空或仍以数字打头 → 不是科目名
    # （"2025年末占总资产比例" 的片段、年份残段）。北投 financial "0.14% 应收账款占比" 实测。
    candidate = _RATIO_LEAD_SHELL_RE.sub("", candidate).strip()
    if len(candidate) < 2 or RATIO_UNIT_TAIL_RE.fullmatch(candidate):
        return None
    if re.match(r"[0-9０-９]", candidate):
        return None
    if RATIO_SUBJECT_REJECT_HEAD_RE.match(candidate):
        return None
    if RATIO_PRONOUN_RE.fullmatch(candidate):
        return None
    if not any("\u4e00" <= char <= "\u9fff" for char in candidate):
        return None
    return candidate


def _ratio_numerator_labels(clause: str, preceding: str = "") -> set[str]:
    """从比率分句里取"占/在"**之前**的分子科目名（如"存货占资产总额比重"→"存货"）。

    只用于把**已有记录**的候选池放宽：返回值仍需与记录 label 比对，不会凭空放行。
    取不到或形态可疑时返回空集，退回原窗口行为（宁可漏报成待核验，也不误报冲突）。

    ``preceding`` 传**本分句之前**的文本（宽前缀），供两种"分子不在本分句"的情形回溯：

    1. **代词回指**（"其他应收款由…降至…，募集说明书披露其占总资产比重由1.77%
       降至1.29%"）：本分句只有代词"其"，分子是前一分句的"其他应收款"。
    2. **结构承接**（"非流动资产合计由…增至…，占总资产比重维持在84%左右"）：本分句
       只有"占…比重维持在"，分子是前一分句的主语"非流动资产合计"。

    两种情形在"本分句取不到有效分子"这一点上完全一致，故统一交给
    :func:`_backreference` 处理。
    """
    if not clause or ("占" not in clause and "在" not in clause):
        return set()
    labels: set[str] = set()
    found_local = False

    def _backreference() -> None:
        """回指：从**本分句之前**的分句里往回找最近的科目名。

        `preceding` 的最后一个分句就是本分句（宽前缀含本分句起头），必须**跳过**：
        代词/承接的先行词按定义在它**之前**。跳过本分句后逐分句往回扫，先裁掉比例
        标记右侧（分母/谓词），再剥尾取科目名。回指**不跨句号**（"回指不得跨 `。`"
        是既有纪律）：只在**本句**范围内向前找。
        """
        same_sentence = re.split(r"[。！？!?\n]", str(preceding or ""))[-1]
        parts = [p for p in RATIO_NUMERATOR_STOP_RE.split(same_sentence) if p.strip()]
        # 末段是本分句自身（宽前缀 = …，本分句起头），先行词一定在它之前。
        if parts:
            parts = parts[:-1]
        for chunk in reversed(parts):
            candidate = _ratio_numerator_candidate(_cut_ratio_left(chunk))
            if candidate:
                labels.add(candidate)
                return

    for match in RATIO_NUMERATOR_RE.finditer(clause):
        raw = match.group(1).strip()
        if not raw:
            continue
        # 向左不能越过分句标点；再剥掉常见的连接词尾巴（"…的""…端"）。
        raw = RATIO_NUMERATOR_STOP_RE.split(raw)[-1].strip()
        for tail in ("的", "端", "中", "内"):
            if raw.endswith(tail) and len(raw) > len(tail):
                raw = raw[: -len(tail)]
        if not raw:
            continue
        # 剥掉材料/叙述前缀（"募集说明书披露其" → "其"）。剥空＝分子未在本分句写出；
        # 残留代词＝分子是代词。两种情况都回指前文。
        stripped = RATIO_NUMERATOR_PREFIX_RE.sub("", raw).strip()
        # 年份/数字壳不是科目名："2023年存货" → "存货"；"2025年末" 剥空 → 本分句没有
        # 写出科目名（交给本分句候选/回指路径）。北投 financial "0.14% 应收账款占比" 实测。
        stripped = _RATIO_LEAD_SHELL_RE.sub("", stripped).strip()
        if (
            not stripped
            or RATIO_PRONOUN_RE.fullmatch(stripped)
            or RATIO_PRONOUN_TAIL_RE.search(stripped)
        ):
            # 分子是代词或未在本分句写出 → 回指前文。
            _backreference()
            continue
        if len(stripped) >= 2 and any("\u4e00" <= char <= "\u9fff" for char in stripped):
            labels.add(stripped)
            found_local = True

    # 本分句有"占…比重"结构却一个分子都没取到：先认**本分句自身**的科目名
    # （"营业毛利率在2023年至2025年分别为A、B、C" 的分子就在本句——16 字窗口把
    # 它截掉了；"…，占总资产比重维持在84%左右" 的结构承接写法里本句没有分子）；
    # 取不到才回指前文。此序保证本句已写的科目名优先——"营业毛利率"不继承上一句
    # "净利率"（北投财务实测：-45.98%/-83.96%/-59.54% 三条同因误退）。
    if not found_local and not labels:
        local_candidate = _ratio_numerator_candidate(_cut_ratio_left(clause))
        if local_candidate:
            labels.add(local_candidate)
        else:
            _backreference()
    return labels



def _written_period(prefix: str) -> str | None:
    """正文里数字前的**唯一**年份归属；只看**最后一个分句**，两处年份或比较基准年返回 None。

    跨分句的年份属于前一个数字，不得泄漏到本数字：实测"营业收入由2023年度的48,000万元
    增至2025年度的52,000万元，净利润由2,296万元增至2,800万元"里，2,296 的年份由
    "由…增至…"的并列结构决定，前缀窗口扫到 2025 会把 2023 的正确归属误判成错期间。
    """
    clause = _CLAUSE_SPLIT_RE.split(prefix or "")[-1]
    years = list(PERIOD_TOKEN_RE.finditer(clause))
    if len(years) != 1:
        return None
    found = years[0]
    around = clause[max(0, found.start() - 3):found.end() + 6]
    if any(marker in around for marker in PERIOD_COMPARATIVE_MARKERS):
        return None
    return found.group(0)


def _written_scopes(prefix: str) -> set[str]:
    """正文里数字前的口径声明（合并/母公司），否定语境不算（"不采用母公司口径"）。"""
    scopes: set[str] = set()
    for term, core in SCOPE_TERMS.items():
        for found in re.finditer(re.escape(term), prefix):
            if not _is_negated_mention(prefix, found.start()):
                scopes.add(core)
                break
    return scopes


# ---------------------------------------------------------------------------
# 本句范围与并列期间顺序绑定（旧MD计划剩余项收口执行方案 §一"证据与归属修复"）
# ---------------------------------------------------------------------------
# 句子界：。！？；\n —— 期间序列只在**本句**内认定，不跨句绑定。
_SENTENCE_SPLIT_RE = re.compile(r"[。！？；;!?\n]+")


def _sentence_scope(wide_prefix: str) -> str:
    """从宽前缀（数字前 160 字）截出**本句**：最后一个句界之后的部分。"""
    text = str(wide_prefix or "")
    if not text:
        return ""
    parts = _SENTENCE_SPLIT_RE.split(text)
    return parts[-1] if parts else text


_SERIES_TRIGGER_RE = re.compile(r"(?:分别|依次|各|均)为")
# 后缀必须**长词在前**（"年末/年度" 先于 "年"）：否则 "2023年末" 只吃到 "年"、
# 残留的 "末" 会让年份之间的连接符检查失败（实测踩过）。
_SERIES_YEAR_RE = re.compile(r"(20\d{2})(?:年度|年末|年底|年)?")
# 年份之间、年份与触发词之间允许的连接符（不含其它文本）。
_SERIES_CONNECTOR_RE = re.compile(r"(?:以及|[、，,]|和|及|与|至|到|~|～|—|–|―|-|\s)+")
_SERIES_RANGE_SEP_RE = re.compile(r"[至到~～—–―-]")
# 触发词后、数字前的"值枚举段"白名单：数字 / 小数与千分位 / 百分号 / 单位 / 连接符。
_SERIES_VALUE_SEPARATOR_RE = re.compile(r"(?:以及|[、，,]|和|及|与)")
_SERIES_VALUE_NOISE_RE = re.compile(
    r"(?:[0-9０-９]|\.|,|，|%|％|‰|万元|亿元|个百分点|个月|季度|年末|年度|元|万|亿|倍|月|年|季"
    r"|天|日|次|股|笔|项|家|人|以及|、|和|及|与|-|−|\s)+"
)
_SERIES_TAIL_RE = re.compile(r"[\s\u3000]*(?:年度|年末|年底|年)?[\s\u3000]*")


def _series_years(before: str) -> list[str]:
    """从触发词之前的短语里取**紧邻触发词**的年份序列（列表或区间）。

    年份列表（"2023年末、2024年末和2025年末"）按序使用；两个年份之间是区间符号
    （"2023年至2025年"）时展开为连续年份。年份之间/与触发词之间出现其它文本即认定
    不构成序列（宁可不绑定，也不猜对应关系）。
    """
    spans = list(_SERIES_YEAR_RE.finditer(before or ""))
    if len(spans) < 2:
        return []
    collected = [spans[-1]]
    for span in reversed(spans[:-1]):
        gap = before[span.end():collected[0].start()]
        if not _SERIES_CONNECTOR_RE.fullmatch(gap):
            break
        collected.insert(0, span)
    if len(collected) < 2:
        return []
    if not _SERIES_TAIL_RE.fullmatch(before[collected[-1].end():]):
        return []
    years = [span.group(1) for span in collected]
    if len(years) == 2:
        between = before[collected[0].end():collected[1].start()]
        if _SERIES_RANGE_SEP_RE.search(between):
            start, end = int(years[0]), int(years[1])
            if 0 <= end - start <= 12:
                return [str(year) for year in range(start, end + 1)]
    return years


def _ordered_series_year(scope: str) -> str | None:
    """并列期间顺序绑定："2023年至2025年分别为A、B、C" 的第 k 个值 → 第 k 年。

    收口方案 §一："明确'2023至2025年分别为A、B、C'时按顺序绑定；年份或数值数量
    不一致时待核验，不猜测对应关系。" 只在**本句**内有"分别为/依次为"枚举结构、
    且触发词到数字之间只剩值/连接符/单位时启用；年份与序号对不上返回 None，
    退回既有的单年窗口逻辑（不猜）。
    """
    scope = str(scope or "")
    trigger = None
    for found in _SERIES_TRIGGER_RE.finditer(scope):
        trigger = found
    if trigger is None:
        return None
    segment = scope[trigger.end():]
    # 枚举段只能是"值/连接符/单位"；出现其它中文（"分别为母公司口径…"）即不是值枚举。
    if _SERIES_VALUE_NOISE_RE.sub("", segment).strip(" \t\u3000"):
        return None
    index = 1 + len(_SERIES_VALUE_SEPARATOR_RE.findall(segment))
    years = _series_years(scope[:trigger.start()])
    if not years or index > len(years):
        return None
    return years[index - 1]


def _record_matches_mention(item: dict, record: dict, *, any_unit: bool = False) -> bool:
    """正文数字与记录数值是否一致（含展示精度 ROUND_HALF_UP）。

    非裸数字要求单位也一致；裸数字（``any_unit``）忽略单位，改由调用方核验单位唯一性。
    记录侧精度取**记录原始 ``value`` 字符串的小数位**（第三点细化方案 §二.2：
    "展示精度由记录的原始精度…明确给出"），而非换算后的位数。
    """
    record_value, record_unit, _ = _canonical_measurement(
        record.get("value"), str(record.get("unit") or "")
    )
    if not any_unit and record_unit != item["normalized_unit"]:
        return False
    try:
        left = Decimal(str(item["normalized_value"]))
        right = Decimal(str(record_value))
    except (InvalidOperation, ValueError):
        return False
    record_places = _decimal_places(record.get("value"))
    return _values_match(left, int(item.get("precision") or 0), right, record_places)


def _allowed_number_index(values: set[tuple[str, str]]) -> dict[str, list[tuple[Decimal, int]]]:
    """把 ``(归一化数值, 归一化单位)`` 集合整理成 ``单位 → [(数值, 小数位)]`` 索引。"""
    index: dict[str, list[tuple[Decimal, int]]] = {}
    for value, unit in values:
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        index.setdefault(str(unit), []).append((decimal_value, _decimal_places(value)))
    return index


def _candidate_values(item: dict) -> list[tuple[Decimal, int]]:
    """正文提及可参与匹配的**候选（数值, 小数位）**，按优先级排列。

    金额按换算表折算到万元后会引入**放大倍数**，而分母 10000 除不尽时十进制展开是
    无限的：``816,849.21 万元`` → ``8168492100``？不是；``816849.21 亿元`` → ×10000 →
    ``8168492100``；而 ``2,526,880.06 万元`` 无需换算。真正的问题是**亿元→万元**：
    ``43.40 亿元`` → ``434000``，若折算后按**小数点后位数**（原始 2 位）去匹配记录，
    精度语义就错了。

    再叠加 ``_records_support`` 侧的展示精度舍入（``_values_match`` 会按另一侧的位数
    ROUND_HALF_UP），出现"全量折算值以任意精度被匹配掉"的荒诞结果：正文 ``816,849.21``
    能命中记录 ``81,214.30``（把两者都舍到 0 位小数都是 816849？否——是把**记录**舍到
    正文位数后等于正文的**全量放大值**）。为杜绝这类跨量级误配，这里把候选限定为：

    * 换算后的**全量值**（原行为，precision = 该值的有效小数位）；
    * 若换算引入了放大（单位不同），额外登记一个**相对精度**候选：
      用 ``len(位数) = max(0, places(原值) - places(倍数))`` 表达"展示精度按原值计的
      同量级数值"，即 ``43.40 亿元`` 的候选是 ``(434000, 0)``——只允许记录侧在 0 位
      小数上与 434000 匹配，不再退化成"记录被舍到正文位数"。

    **关键不变式**：候选里只出现"正文自己写出的值"或其严格等值换算；任何一侧都不得
    被舍到与另一侧无关的精度上去凑等——这就是误报的根源。
    """
    raw_value = str(item.get("normalized_value") or "")
    unit = str(item.get("normalized_unit") or "")
    try:
        value = Decimal(raw_value)
    except (InvalidOperation, ValueError):
        return []
    precision = int(item.get("precision") or 0)
    candidates: list[tuple[Decimal, int]] = [(value, precision)]
    if unit == CANONICAL_MONEY_UNIT and item.get("converted"):
        # 折算把单位统一到万元；记录侧若以万元登记、原始却按亿元展示（43.40 亿元），
        # 记录值会同样折成 434000 万元且小数位为 0。给一个"折算后按原始精度推断"的
        # 候选，只补齐尾零差异，不放大匹配范围。
        factor_positions = len(str(abs(MONEY_UNIT_FACTORS.get(unit, 1))).rstrip("0")) - 1
        reduced = max(0, precision - max(0, factor_positions))
        if (value, reduced) != (value, precision):
            candidates.append((value, reduced))
    return candidates


def _mention_allowed(item: dict, index: dict[str, list[tuple[Decimal, int]]]) -> bool:
    """正文数字是否命中允许集（同单位 + 展示精度 ROUND_HALF_UP）。"""
    entries = index.get(str(item.get("normalized_unit") or ""), [])
    if not entries:
        return False
    for value, places in _candidate_values(item):
        for allowed_value, allowed_places in entries:
            if _values_match(value, places, allowed_value, allowed_places):
                return True
    return False
    for allowed_value, allowed_places in index.get(item["normalized_unit"], []):
        if _values_match(value, int(item.get("precision") or 0), allowed_value, allowed_places):
            return True
    return False


def _review_item(item: dict, code: str, detail: str, content: str) -> dict:
    """构造 ``needs_review_items`` 的一项：``{code, excerpt, position, detail}``。"""
    raw = str(item.get("raw") or "")
    position = content.find(raw) if raw else -1
    if position < 0:
        position = content.find(str(item.get("normalized_value") or ""))
    return {
        "code": code,
        "excerpt": str(item.get("context") or ""),
        "position": position,
        "detail": detail,
    }


CONFLICT_REASON_CODES = {
    "label": "label_mismatch",
    "period": "period_mismatch",
    "scope": "scope_mismatch",
    "party": "party_mismatch",
}

# 比较基准不是取值：正文"速动比率三年均低于1倍"里的 1 是**比较上下限**（阈值），
# 不是对速动比率的取值断言。同值记录恰好是别科目的阈值规则时，按科目绑定会造出
# label_mismatch 误退（中邮 financial 实测：written=['速动比率']
# expected=['流动比率', '杠杆与流动性 current_ratio', ...]）。
# 只认**纯比较算子**；"约/近/达到/逾"是取值近似，仍按取值校验，不放宽。
_COMPARISON_BOUND_RE = re.compile(
    r"(?:低于|高于|超过|不足|大于|小于|不低于|不高于|不超过|少于|多于|未超过|未达到)\s*$"
)

# 算子 → 记录值应满足的关系（计划 §四 line 34：核对记录**确实**小于/大于该界限，
# 不要求记录值等于界限）。匹配用 `_bound_relation` 的 `endswith`，长算子排前面。
_COMPARISON_BOUND_OPS = (
    ("不低于", "ge"), ("不高于", "le"), ("不超过", "le"),
    ("未达到", "lt"), ("未超过", "le"),
    ("低于", "lt"), ("小于", "lt"), ("不足", "lt"), ("少于", "lt"),
    ("高于", "gt"), ("大于", "gt"), ("超过", "gt"), ("多于", "gt"),
)
_COMPARISON_RELATIONS = {
    "lt": lambda value, bound: value < bound,
    "le": lambda value, bound: value <= bound,
    "gt": lambda value, bound: value > bound,
    "ge": lambda value, bound: value >= bound,
}


def _bound_relation(prefix: str) -> tuple[str, str] | None:
    """数字紧邻的前文若以比较算子结尾 → ``(算子, 关系)``；否则 ``None``。"""
    tail = (prefix or "").rstrip()
    if not tail:
        return None
    for operator, relation in _COMPARISON_BOUND_OPS:
        if tail.endswith(operator):
            return operator, relation
    return None


def _verify_comparison_bound(item: dict, prefix: str, written_labels: set[str],
                             records: list[dict]) -> tuple[bool | None, str]:
    """核对比较界限：「所写科目**自己的**记录」是否整体满足该界限。

    计划 §四 line 34：`"速动比率低于1倍"` 须核对对应记录**确实小于1**，不要求记录值等于1。
    计划 §四 line 35：比较只覆盖**明确的科目/单位**；无法确定范围时待核验，
    **不能通用豁免所有"超过、低于"句**——所以只有"能核对且全部满足"才算通过。

    返回 ``(True, "")`` 已核对通过；``(None, 说明)`` 无法核对；``(False, 说明)`` 不满足。

    只拿**所写科目自己的**记录比对：同值却属别科目的记录正是误报来源（中邮实测量产过
    `速动比率低于1倍` 被判 `label_mismatch`），不得拿它来"验证"。
    """
    relation = _bound_relation(prefix)
    if relation is None:
        return None, "无法确定比较方向"
    operator, kind = relation
    bound_unit = str(item.get("normalized_unit") or "")
    try:
        bound = Decimal(str(item.get("normalized_value") or ""))
    except (InvalidOperation, TypeError, ValueError):
        return None, "比较基准解析失败"
    if not written_labels:
        return None, "正文未写出可比的科目名"
    compare = _COMPARISON_RELATIONS[kind]
    subject_values: list[Decimal] = []
    for record in records:
        labels = [str(record.get("label") or "")] + [str(alias) for alias in record.get("aliases") or []]
        if not any(
            _labels_compatible(written, label) for written in written_labels for label in labels
        ):
            continue
        normalized, canonical_unit, _ = _canonical_measurement(
            str(record.get("value") or ""), str(record.get("unit") or ""),
        )
        if canonical_unit != bound_unit:
            continue
        try:
            subject_values.append(Decimal(str(normalized)))
        except (InvalidOperation, TypeError, ValueError):
            continue
    if not subject_values:
        return None, "找不到所写科目的可用记录"
    if all(compare(value, bound) for value in subject_values):
        return True, f"已核对：所写科目 {len(subject_values)} 条记录均满足「{operator}{item['raw']}」"
    return False, f"所写科目的记录并非全部满足「{operator}{item['raw']}」"


def _record_binding(
    mentions: list[dict], records: list[dict], registry: dict[str, str] | None,
    content: str, allowed_entries: set[tuple[str, str]],
) -> tuple[list[dict], list[dict]]:
    """数值与所写**科目/期间/主体/口径**的绑定（修订稿 §3.1、细化方案 §二.2）。

    返回 ``(conflicts, review_items)``：

    - ``conflicts``：某维度对**所有**同值记录都不成立 → 真实错误（rejected）；
    - ``review_items``：各维度分别有记录支持、但**没有同一条记录同时满足**（只能是跨记录
      拼接）→ 归属不明，``needs_review``（不得直接通过）。

    裸数字（正文没写单位）：单位与归属必须唯一明确，否则产出 ``ambiguous_attribution``。
    """
    conflicts: list[dict] = []
    review_items: list[dict] = []
    if not records:
        return conflicts, review_items
    # 纯数字/纯符号标签（历史记录里的序号残留，如 "1"~"10"）不进词表：下面的
    # "label in prefix" 是子串匹配，这类标签会命中正文里任何含该数字的句子，
    # 让科目正确的数字被判 label_mismatch（北投 debt 实测）。
    # 表格行的「其中：」前缀不是科目的一部分：正文写"债券融资中，公司债券206,400万元"
    # 时窗口里只有"公司债券"，若词表键仍是"其中：公司债券"就配不上（北投 debt 实测）。
    vocabulary: set[str] = set()
    for record in records:
        label = _LABEL_PREFIX_RE.sub("", str(record.get("label") or "").strip())
        if label and any("\u4e00" <= char <= "\u9fff" for char in label):
            vocabulary.add(label)
            # 带括号后缀的科目名（"关联方往来（未提供合同）"）在正文里常被写成主体词
            # （"关联方往来"），括号是抽取噪声。同时登记去括号后的主体词，避免窗口恰好
            # 只扫到父级科目名（"其他应收款"）时造出假 label_mismatch（华辰实测）。
            stripped = re.sub(r"[（(][^（()）]*[)）]\s*$", "", label).strip()
            if stripped and stripped != label:
                vocabulary.add(stripped)
    seen_conflicts: set[tuple[str, str, str]] = set()
    seen_review: set[tuple[str, str, str]] = set()

    def add_conflict(item: dict, dim: str, extra: dict) -> None:
        dedup = (item["normalized_value"], item["normalized_unit"], dim)
        if dedup in seen_conflicts:
            return
        seen_conflicts.add(dedup)
        conflicts.append({
            "raw": item["raw"], "normalized_value": item["normalized_value"],
            "normalized_unit": item["normalized_unit"], "reason": CONFLICT_REASON_CODES[dim],
            **extra,
        })

    def add_review(item: dict, code: str, detail: str) -> None:
        dedup = (item["normalized_value"], item["normalized_unit"], code)
        if dedup in seen_review:
            return
        seen_review.add(dedup)
        review_items.append(_review_item(item, code, detail, content))

    for item in mentions:
        bare = not item.get("has_unit")
        candidates = [
            record for record in records
            if _record_matches_mention(item, record, any_unit=bare)
        ]
        if not candidates:
            # 裸数字：若同一数值在允许集里挂到多个不同单位上，归属不唯一 → 待核验。
            if bare and not _mention_allowed(item, _allowed_number_index(allowed_entries)):
                continue
            if bare:
                units = {
                    unit for value, unit in allowed_entries
                    if unit and value == str(item["normalized_value"])
                }
                if len(units) > 1:
                    add_review(item, "ambiguous_attribution",
                               "裸数字在允许证据中对应多个不同计量单位，归属不唯一")
            continue
        if bare:
            signatures = {
                (str(record.get("unit") or ""), str(record.get("label") or ""))
                for record in candidates
            }
            determinate = any(str(record.get("unit") or "") for record in candidates)
            if len(signatures) > 1 or not determinate:
                add_review(item, "ambiguous_attribution",
                           "裸数字的计量单位或归属在句内不唯一，无法绑定到单条记录")
                continue

        # 归属窗口 = 数字**之前**的文段。优先用 ``prefix_offset``（精确偏移）；缺失时退回
        # ``split(raw)`` 且要求必须命中——都取不到就置空，绝不把数字之后的文段当"所写科目"
        # （宁可漏报为待核验，也不误报成硬冲突：旧实现正是败在这里）。
        raw_token_text = str(item["raw"])
        context_text = str(item.get("context") or "")
        offset = item.get("prefix_offset")
        if isinstance(offset, int) and 0 <= offset <= len(context_text):
            prefix = context_text[:offset]
        elif raw_token_text and raw_token_text in context_text:
            prefix = context_text.split(raw_token_text, 1)[0]
        else:
            prefix = ""
        # 科目归属只看**数字所在分句**（与 ``_written_period`` 同一纪律）：跨分句的科目名
        # 属于前一个数字。实测华辰"货币资金6,820万元，其中受限资金818万元；2024年末6,206
        # 万元"——6,206 的窗口会扫到上一分句的"受限资金"，把货币资金总额误读成受限资金，
        # 造出 label_mismatch 误退。分句后再取窗口。
        label_clause = _CLAUSE_SPLIT_RE.split(prefix)[-1] if prefix else ""
        # 数字归一化后再取窗口：正文写"一年以上"、词表键是"1年以上"时，直接子串匹配
        # 配不上（中邮 debt 94.85% 实测）。两侧同口径归一，窗口长度不受影响（逐字符替换）。
        label_clause = _normalize_label_numerals(label_clause)
        # 本句范围（自上一句界起、到数字为止）：并列期间顺序绑定用。
        sentence_scope = _sentence_scope(str(item.get("wide_prefix") or ""))
        written_labels = {
            label for label in vocabulary
            if label and _normalize_label_numerals(label) in label_clause[-SUBJECT_BINDING_WINDOW:]
        }
        # 比率陈述：数字紧跟在**分母**后面（"存货占资产总额比重分别为8.32%"），窗口只扫到
        # "资产总额"，而记录 label 记的是分子"存货"。此时把"占/在"之前的分子名并进候选，
        # 让科目正确的比率能绑定到自己的记录（北投财务 3.8%/8.32% 实测误退）。
        # 只在出现的候选记录**清一色是百分数**时启用：那时"占"必是比率结构，不会误扩。
        if str(item.get("normalized_unit") or "") == "%" and candidates and all(
            str(record.get("unit") or "").strip() == "%" for record in candidates
        ):
            # `preceding` 传本分句之前的**整段**宽前缀：分子写成代词（"其占总资产比重"）时
            # 需要回溯前一分句的科目名（新北投 1.77%/1.29% 实测）。交整段而非只交前一分句，
            # 由 `_backreference` 逐层剥掉数值/单位/谓词后再取科目名——前一分句本身也可能
            # 以数值结尾（"…降至1,251,848.55万元"），只给两分句不够用。
            wide_parts = _CLAUSE_SPLIT_RE.split(str(item.get("wide_prefix") or ""))
            wide_clause = wide_parts[-1] if wide_parts else label_clause
            # 回指（收口方案 §一"营业毛利率不继承上一句净利率"）：`_ratio_numerator_labels`
            # 先认**本分句自身**的科目名，取不到才回指前文；回指不跨句号，且首个可用分句
            # 即返回——"营业毛利率在…分别为-45.98%…" 不会继承上一句"净利率为11.46%"。
            preceding = str(item.get("wide_prefix") or "")
            for numerator in _ratio_numerator_labels(wide_clause, preceding):
                for label in vocabulary:
                    if _labels_compatible(numerator, label):
                        written_labels.add(label)
        # 期间绑定：先试**并列期间顺序绑定**（"2023年至2025年分别为A、B、C" 按序号对年），
        # 不适用时退回既有的单年窗口逻辑。
        written_year = _ordered_series_year(sentence_scope) or _written_period(prefix)
        written_scopes = _written_scopes(prefix)
        written_entities = _extract_entities(prefix)

        # 记录只命中「合计/小计/总计」行：总行本身不是科目名，正文在数字旁写的科目描述
        # 无法与它直接比对——可以写账户全称（"应收账款账面余额""占其他应收款总额的"），
        # 也可以写子项（"1年以内"），系统无法判定，按"归属不明"进待核验（实测：
        # 北投 751,716.86万元/59.22%、华辰 12,640万元 均为正确归属被硬拒；而
        # 类型表合计 3,856,300 被写成"1年以内"属于真实错配，同样只能由人工裁决）。
        generic_total_label = bool(written_labels) and candidates and all(
            str(record.get("label") or "").strip() in GENERIC_TOTAL_LABELS for record in candidates
        )

        # 每个可判定的维度 → (维度名, 记录谓词, 诊断字段)。
        filters: list[tuple[str, Any, dict]] = []
        if written_labels and not generic_total_label:
            def _label_compatible(record: dict) -> bool:
                if any(
                    _labels_compatible(label, str(record.get("label") or ""))
                    for label in written_labels
                ):
                    return True
                # 阈值记录的 label 是英文 slug（"杠杆与流动性 current_ratio"），中文科目名
                # 存在 ``aliases`` 里：正文写的中文科目名与之比对（见 evidence_records
                # ``_threshold_aliases``）。
                for alias in record.get("aliases") or []:
                    if any(_labels_compatible(label, str(alias)) for label in written_labels):
                        return True
                # 规则记录的**公式**同样声明了输入科目（"经营现金流/利息支出"）：正文写
                # "经营现金流对利息支出的覆盖倍数为1.71倍"时，写的正是公式里的输入项。
                # 公式是规则自带的、与记录同源的归属声明，命中即视为同一归属。
                formula = str(record.get("formula") or "")
                if formula:
                    for part in re.split(r"[/÷+\-×*，,、；;（）()\s]+", formula):
                        part = part.strip()
                        if part and any(
                            _labels_compatible(label, part) for label in written_labels
                        ):
                            return True
                # 指标/规则记录沿**计算链路**认账：正文写的是输入事实的科目名
                # （"经营现金流对利息支出覆盖倍数为1.71倍"里的"利息支出"）时，
                # 只要该科目命中本记录的输入事实（source_fact_ids），视为同一归属。
                sources = {str(item) for item in record.get("source_fact_ids") or []}
                if not sources:
                    return False
                for other in records:
                    other_id = str(other.get("record_id") or "")
                    if other_id.startswith("fact:"):
                        other_id = other_id[len("fact:"):]
                    if other_id not in sources and str(other.get("fact_key") or "") not in sources:
                        continue
                    if any(
                        _labels_compatible(label, str(other.get("label") or ""))
                        for label in written_labels
                    ):
                        return True
                return False

            # 比较基准豁免：数字紧跟在"低于/高于/不超过…"之后时是**上下限**而非取值，
            # 同值记录碰巧是别科目的阈值规则时不构成科目错配（中邮 financial"速动比率
            # 三年均低于1倍"实测）。此时**先按计划 §四 line 34 核对**："低于1倍"要核对
            # 所写科目自己的记录确实小于1（不要求等于1）；核对通过即通过，核对不了或不满足
            # 才记待核验——不能通用豁免所有"超过、低于"句（§四 line 35）。
            bound_relation = _bound_relation(prefix)
            if bound_relation is not None and not any(
                    _label_compatible(record) for record in candidates):
                checked, detail = _verify_comparison_bound(item, prefix, written_labels, records)
                if checked is True:
                    pass
                else:
                    add_review(item, "comparison_bound", detail)
            else:
                filters.append(("label",
                    _label_compatible,
                    {"expected_labels": sorted({str(r.get("label") or "") for r in candidates}),
                     "written_labels": sorted(written_labels)}))
        if generic_total_label and not any(
            _labels_compatible(label, str(record.get("label") or ""))
            for label in written_labels for record in candidates
        ):
            add_review(item, "generic_total_label",
                       "数值命中合计/小计行，正文所写科目无法与总行直接比对，待人工核验")
        periods = {str(record.get("period") or "").strip() for record in candidates}
        if written_year and periods and "" not in periods:
            filters.append(("period",
                lambda record: str(record.get("period") or "").strip() == written_year,
                {"written_period": written_year, "expected_periods": sorted(periods)}))
        scopes = {str(record.get("scope") or "").strip() for record in candidates}
        if written_scopes and scopes and all(s and "未注明" not in s for s in scopes):
            filters.append(("scope",
                lambda record: any(core in str(record.get("scope") or "") for core in written_scopes),
                {"written_scopes": sorted(written_scopes), "expected_scopes": sorted(scopes)}))
        subjects = {str(record.get("subject") or "").strip() for record in candidates}
        if written_entities and subjects and all(subjects):
            # 记录主体是"发行人/担保人"口径，但交易对手行（如"华东汽车集团（虚构） 1,921万元"）
            # 的**行标签本身就是主体名**——只比 subject 会把写在正确对手方下的数字误判成
            # 主体不匹配（验收实测）。标签命中同等有效；真正的第三方主体两边都不命中。
            filters.append(("party",
                lambda record: any(
                    _party_compatible(entity, str(record.get("subject") or ""), registry)
                    or _party_compatible(entity, str(record.get("label") or ""), registry)
                    for entity in written_entities
                ),
                {"written_entities": sorted(written_entities)[:3],
                 "expected_subjects": sorted(subjects)[:3]}))

        empty_dims = [
            dim for dim, predicate, _meta in filters
            if not any(predicate(record) for record in candidates)
        ]
        if empty_dims:
            for dim in empty_dims:
                meta = next(m for name, _p, m in filters if name == dim)
                add_conflict(item, dim, meta)
            continue
        if filters:
            pool = list(candidates)
            for _dim, predicate, _meta in filters:
                pool = [record for record in pool if predicate(record)]
            if not pool:
                add_review(item, "record_binding_ambiguous",
                           "数字的科目/期间/主体/口径各维分别有记录支持，但无同一条记录同时满足（禁止跨记录拼接）")
    return conflicts, review_items


# 项目 Prompt 里"仅/只/限定 + 口径"的显式范围声明（细化方案 §二.2：项目明确限定时遵守）。
PROMPT_SCOPE_LIMIT_RE = re.compile(
    r"(?:仅|只|限定|一律)(?:采用|使用|引用|分析|按)?(合并口径|母公司口径)"
)


def _generation_mode(context: dict) -> str:
    """本章生成模式：``generate`` 直接撰写 / ``rewrite`` 受控改写（默认）。"""
    return str(((context.get("prompt_config") or {}).get("generation_mode")) or "rewrite")


def _prompt_scope_limits(context: dict) -> set[str]:
    """从项目 Prompt（goal/rules/附加要求）里取**显式**口径限定；无则空集。"""
    config = context.get("prompt_config") or {}
    text = json.dumps(config, ensure_ascii=False)
    return set(PROMPT_SCOPE_LIMIT_RE.findall(text))


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
    #
    # 文风类提示**不进 `reasons`**（计划 §二 line 17）：技术化起句/系统术语/列表形态
    # 只记诊断，不触发整章回退。它们已经作为写作指引写进 Prompt。
    style_notes = _narrative_style_notes(content, context)
    forbidden_matches: list[dict] = []
    # 需要人工核验、但不阻断单章保存的发现（修订稿 §3.1、细化方案 §二.2）：
    # 归属不明、决策语境不确定等一律进这里，作为独立指标阻止**最终交付**，但不构成
    # 单章安全失败。每项固定 ``{code, excerpt, position, detail}``。
    needs_review_items: list[dict] = []
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
            if classification == "safe_description":
                # 语境不确定：既不是我们方的决策动作，也不是否定声明或材料引用。
                # 描述性提及本身不算越权，但需人工确认其边界 → 待核验（不阻断单章）。
                needs_review_items.append({
                    "code": "decision_context_uncertain",
                    "excerpt": content[max(0, match.start() - 20):match.end() + 20].strip(),
                    "position": match.start(),
                    "detail": f"决策相关表述“{pattern}”的语境无法自动判定为否定/引用/决策动作",
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
        # 修订稿 §3.1：**移除"季度/半年度一律禁止"与"混入非采用口径一律禁止"**。
        # 期间与口径改为"与记录一致即可分别引用"——合并/母公司、年度/中期各自的记录都能
        # 单独引用，由四维归属核对（数字→同一条记录）承担一致性；不在这里一刀切拦。
        allowed_periods = set(context.get("allowed_periods", []))
        produced_years = _year_mentions(content)
        # generate 模式下确定性草稿不进入允许文本，也不得用它给正文年度背书。
        draft_text = "" if _generation_mode(context) == "generate" else str(context.get("draft") or "")
        draft_years = _year_mentions(draft_text)
        unauthorized_years = sorted(produced_years - allowed_periods - draft_years)
        if unauthorized_years:
            # 带上具体年份：回退原因必须可定位，不能只给笼统一句
            reasons.append("章节包含未授权的完整年度：" + "、".join(unauthorized_years))
        # 仅当**项目 Prompt 明确限定口径范围**时才恢复硬拦（细化方案 §二.2：项目限定时遵守）。
        for limited_scope in _prompt_scope_limits(context):
            opposite = "母公司口径" if limited_scope == "合并口径" else "合并口径"
            mentions = [match for match in re.finditer(re.escape(opposite), content)
                        if not _is_negated_mention(content, match.start())]
            if mentions:
                reasons.append("章节范围与项目Prompt限定的报表口径不符")
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
    # 允许集 = 模型能看到的内容：证据原文、事实/指标/核验、冲突断言，以及章节契约文本
    # （规则与阈值口径，如"超过 50% 的关注阈值"）。这条校验的语义是"不得出现材料里没有
    # 的数字"，因此允许集必须覆盖模型的全部可见输入。
    # generate 模式（修订稿 §3.1 / 细化方案 §三.1）：**确定性分析草稿不进入请求、也不进入
    # 数字允许集**（草稿保留在服务端作备用）；证据/事实/指标/规则/系统表格照常进入。
    # rewrite 模式保持原行为（草稿仍是允许文本的一部分）。
    generation_mode = _generation_mode(context)
    allowed_text_parts = [
        " ".join(item.get("quote", "") for item in context.get("evidence", [])),
        json.dumps(context.get("facts", []), ensure_ascii=False),
        json.dumps(context.get("metrics", []), ensure_ascii=False),
        json.dumps(context.get("cross_checks", []), ensure_ascii=False),
        json.dumps(context.get("conflicts", []), ensure_ascii=False),
        json.dumps(context.get("prompt_config", {}), ensure_ascii=False),
        " ".join(_measurement_texts(context)),
        # 系统冻结块（冲突块/系统数据表）由服务端生成，任何模式下都是允许内容。
        str(context.get("conflict_block") or ""),
        str(context.get("system_table_block") or ""),
    ]
    if generation_mode != "generate":
        allowed_text_parts.insert(0, str(context.get("draft") or ""))
    allowed_text = " ".join(allowed_text_parts)
    # 数字比对做格式归一：千分位、小数尾零，展示精度按记录原始小数位以 ROUND_HALF_UP 匹配
    # （9.8901≡9.89 属已批准展示精度），并在预定义换算表范围内接受等值量级换算
    # （20000万元≡2亿元）。真正新增的金额/比例/评级数字仍然拒绝。
    allowed_mentions = _number_mentions(allowed_text)
    allowed_entries = {(item["normalized_value"], item["normalized_unit"]) for item in allowed_mentions}
    # "亏损118.17亿元""净亏3亿元"：负数由**词义**表达时，同时登记带负号的同值形态——
    # 模型把亏损写进数字符号（"-118.17亿元"）是等价表述（中邮实测被误判新数字）。
    for item in allowed_mentions:
        value = str(item["normalized_value"])
        if value not in ("", "0") and not value.startswith("-") and _LOSS_SIGN_RE.search(
                str(item.get("context") or "")):
            allowed_entries.add(("-" + value, item["normalized_unit"]))
    # 受控表格的**结构化记录**并进允许集：表里单元格是裸数字、单位写在表头，记录把两者绑定，
    # 于是模型正文写"204,709.77万元"或写裸数字都能匹配上，不再被判成"新数字"。
    # 统一冻结记录（table/fact/metric/rule 四类）并进允许集：章节校验与后置支持校验
    # 读取**同一份**记录（三项遗留修复方案 §一）。旧上下文只有 table_records 时就地迁移；
    # 冲突记录不进任何允许集。
    from .evidence_records import evidence_number_keys, matchable_records, records_from_legacy

    evidence_records = context.get("evidence_records")
    if not evidence_records:
        evidence_records = records_from_legacy(context.get("table_records") or [])
    binding_records = matchable_records(evidence_records)
    allowed_entries |= evidence_number_keys(binding_records)
    allowed_index = _allowed_number_index(allowed_entries)
    # 引文侧对某数值**只有裸数字形态**时（表格行原文、换行把单位与数字分离），正文写成
    # 带单位的同值一律认账：数值的存在性已被证明，单位在这类来源里本就无从核对。
    # **只认证据引文**——配置/指标 JSON 里的裸数字（如 max_evidence_chars=20000）不是
    # 材料文本，不得为正文数值背书。记录支持的数值不受影响：记录会把 (值,单位) 与同值
    # 裸数字一并登记，只要存在任一带单位形态，本兜底即被禁用，单位错误照旧在绑定环节拦截。
    quote_entries = {
        (item["normalized_value"], item["normalized_unit"])
        for item in _number_mentions(
            " ".join(str(entry.get("quote") or "") for entry in context.get("evidence", []))
        )
    }
    quote_bare_only = (
        {value for value, unit in quote_entries if not unit}
        - {value for value, unit in quote_entries if unit}
    )
    # 裸数字兜底的**换算侧**（实测北投 cashflow）：引文里"经营活动产生现金流量净额（亿元）
    # 174.97 90.31 43.40"这类**表格行**，单位声明在行标签/表头、数字本身是裸值，
    # 归一后无单位（43.4）；正文写"43.40亿元"却会按金额换算表折成 434000 万元。
    # 旧兜底只比 ``normalized_value``（434000 ∉ {43.4,...}），该句因此被判新数字。
    # 修法：数字所在**分句**若声明了金额单位，则把裸值按该单位折算后登记——
    # 与 ``_canonical_measurement`` 同一张换算表，等值量级仍受"换算表内等值"约束。
    # 只对**金额**做量级换算（百分比/倍数额本就同形）；非金额、或分句未声明金额单位时
    # 不放行，保持"单位必须唯一"的既有约束。
    #
    # 登记形态是 ``(折算值, "pN")``：N = 该裸值的有效小数位。"pN" 与 ``p<正文位数>``
    # 相等才命中，等价于要求**正文与引文写同一位数**（43.40 ↔ 43.40），杜绝"把数量级
    # 相差万倍的裸值按某侧精度硬凑成相等"的跨量级误配。
    quote_bare_converted: set[tuple[str, str]] = set()
    for entry in context.get("evidence", []):
        text = str(entry.get("quote") or "")
        if not any(marker in text for marker in MONEY_UNIT_FACTORS):
            continue
        for mention in _number_mentions(text):
            if mention["normalized_unit"]:
                continue
            raw_token = str(mention["raw"])
            context_text = str(mention.get("context") or "")
            # 切出**数字之前**的文本必须用 prefix_offset，不能用 ``context.split(raw)``：
            # 窗口从数字前 20 字起截，若单位（"（亿元）"）落在窗口之外，split 会返回
            # **整段**上下文，导致窗口里看不到表头单位声明（实测引文"（亿元）\n…43.40"
            # 全部落空）。与正文侧同一口径（见 ``_record_binding`` 的 prefix 计算）。
            offset = mention.get("prefix_offset")
            if isinstance(offset, int) and 0 <= offset <= len(context_text):
                prefix = context_text[:offset]
            elif raw_token and raw_token in context_text:
                prefix = context_text.split(raw_token, 1)[0]
            else:
                prefix = ""
            # 就近单位声明：数字前**同一分句**内的金额单位；取**结束位置最靠后**的那个
            # （"元"是"万元/亿元"的子串，必须让更长的单位胜出）。
            clause = _CLAUSE_SPLIT_RE.split(prefix)[-1]
            declared = ""
            best_key = (-1, -1)   # (结束位置, 单位长度)：长度参与比较，避免"元"与"亿元"同终点时误取"元"
            for marker in MONEY_UNIT_FACTORS:
                position = clause.rfind(marker)
                if position < 0:
                    continue
                key = (position + len(marker), len(marker))
                if key > best_key:
                    declared, best_key = marker, key
            if not declared:
                # 分句内未声明单位时**回落整段引文表头**（"表 12 …（亿元）\n 科目 43.40"）：
                # 单位可以声明在表头行，而数字行的分句里看不到。剔除**被更长单位包含**的
                # 子串（"元"⊂"亿元"/"万元"），否则仅一处"亿元"会同时命中两个 marker、
                # 被误判成"多单位并列表头"而放弃回落。回落只在表头**唯一**声明一种金额
                # 单位时生效，多单位并列（"单位：万元、%"）不猜。
                header = text[:len(text) - len(text.split("\n")[-1])] if "\n" in text else ""
                header_units = {m for m in MONEY_UNIT_FACTORS if m in header}
                header_units = {
                    m for m in header_units
                    if not any(m != other and m in other for other in header_units)
                }
                if len(header_units) == 1:
                    declared = next(iter(header_units))
                else:
                    continue
            converted_value, converted_unit, _changed = _canonical_measurement(
                str(mention["normalized_value"]), declared
            )
            if converted_unit == CANONICAL_MONEY_UNIT:
                quote_bare_converted.add((converted_value, converted_unit))
                # 同一等值的**另一展示精度**形态：引文裸值 "43.40" 折算成 434000，
                # 正文若写 "43.40亿元" 提到的是同一数量级，precision 由位数差决定。
                # 只登记"折算值 + 原值有效位数"这一个额外形态，不引入任何取舍数。
                quote_bare_converted.add(
                    (converted_value, f"p{_decimal_places(mention['normalized_value'])}")
                )
    produced_mentions = _number_mentions(content)
    unexpected_numbers: list[dict] = []
    safe_unit_conversions: list[dict] = []
    seen_numbers: set[tuple[str, str]] = set()
    for item in produced_mentions:
        key = (item["normalized_value"], item["normalized_unit"])
        if key in seen_numbers:
            continue
        seen_numbers.add(key)
        allowed = _mention_allowed(item, allowed_index) or (
            bool(item["normalized_unit"]) and str(item["normalized_value"]) in quote_bare_only
        )
        if not allowed:
            produced_value = str(item["normalized_value"])
            produced_unit = str(item["normalized_unit"])
            if (produced_value, produced_unit) in quote_bare_converted:
                allowed = True
            elif (produced_value, f"p{int(item.get('precision') or 0)}") in quote_bare_converted:
                # 引文裸值折算后按**正文自己写的位数**匹配：引文 "43.40" 在分句声明了
                # "亿元"，正文写 "43.40 亿元"（同值同位数）即命中；正文若写别的数量级
                # 则位数不同、折不成同一形态，依然判新数字。
                allowed = True
        if allowed:
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
    # 数字在允许集里还不够：还要看它**写在哪个科目/期间/主体/口径下**。把筹资活动现金流
    # 写成投资活动现金流时两个数都在材料里，只有**归属绑定**能拦住（修订稿 §3.1）。
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
    # 兜底：冻结块的 `replace` 依赖"重建表 == 冻结表"这一前提，而重建读的是**当前**抽取表、
    # 正文里存的是写入时冻结的渲染文本，两者会因表格版本漂移而不再逐字相等（中邮 rating
    # 实测：system_table_block 11006 字，`in content` 为 False）。此时表格单元格里的数字会
    # 留在 analysis_text 里，拿表格**行名**当"所写科目"去比对，（"…自有负债…111.99…"）
    # 造出假 label_mismatch。表格数字的归属由表格本身承载，一律不参与正文归属绑定：
    # 直接按行剔除所有 Markdown 表格行，与表格来源无关、与版本漂移无关。
    analysis_text = _MARKDOWN_TABLE_ROW.sub("", analysis_text)
    entity_registry = context.get("entity_registry") or {}
    attribution_conflicts, binding_reviews = _record_binding(
        _number_mentions(analysis_text), binding_records, entity_registry, content, allowed_entries,
    )
    for reason_code in sorted({item["reason"] for item in attribution_conflicts}):
        reasons.append(ATTRIBUTION_REASON_MESSAGES[reason_code])
    needs_review_items.extend(binding_reviews)
    _, ignored_ordinals, page_numbers = _strip_format_numbers(content)
    ignored_format_numbers = sorted(set(ignored_ordinals), key=lambda value: (len(value), value))
    page_numbers_ignored = sorted(set(page_numbers), key=lambda value: (len(value), value))
    allowed_ratings = set(RATING_RE.findall(allowed_text))
    produced_ratings = set(RATING_RE.findall(content))
    if not produced_ratings <= allowed_ratings:
        reasons.append("章节包含确定性草稿和证据之外的新评级")
    # 允许集与产出用**同一提取函数**（修订稿 §3.1）：两侧过滤口径一致（报告口径词汇、
    # 代词型散文片段），只拦真正新增的主体。主体匹配收敛为 全等 / 包含 / 登记简称。
    allowed_entities = _extract_entities(allowed_text)
    produced_entities = _extract_entities(content)
    unknown_entities = [
        item for item in sorted(produced_entities)
        if not _entity_is_allowed(item, allowed_entities, entity_registry)
    ]
    if unknown_entities:
        # 带上具体片段，便于区分“模型真的引入了新主体”与“改写换了上下文”。
        shown = "、".join(item[-20:] for item in unknown_entities[:3])
        reasons.append(f"章节包含确定性草稿和证据之外的新主体：{shown}")
    length = assess_chapter_length(content, context)
    # 三态（修订稿 §2.2 / §3.1）：有硬失败 → rejected；否则有需要人工核验的发现 →
    # needs_review（不阻断单章保存，但计入独立指标、阻止最终交付）；否则 passed。
    if reasons:
        status = "rejected"
    elif needs_review_items:
        status = "needs_review"
    else:
        status = "passed"
    # 去重（按 code + 原句 + 位置），保持稳定顺序。
    deduped_reviews: list[dict] = []
    seen_reviews: set[tuple[str, str, int]] = set()
    for review in needs_review_items:
        dedup = (str(review.get("code")), str(review.get("excerpt")), int(review.get("position") or -1))
        if dedup in seen_reviews:
            continue
        seen_reviews.add(dedup)
        deduped_reviews.append(review)
    # ``reasons`` 现在只剩安全原因：篇幅已在上面移出判定，所以"只有篇幅不合格"
    # 等价于"安全校验全过"。保留这个标志位是为了让调用方据此触发一次压缩修复。
    return {
        "status": status,
        "reasons": reasons,
        "safety_reasons": reasons,
        "invalid_citations": invalid,
        "length": length,
        "length_only_failure": not reasons and length["status"] == "long",
        # 需要人工核验、不阻断单章保存的发现（固定 {code, excerpt, position, detail}）。
        "needs_review_items": deduped_reviews[:20],
        "needs_review_count": len(deduped_reviews),
        # 非敏感诊断（不保存被拒绝的模型正文）
        "unexpected_numbers": unexpected_numbers[:10],
        "attribution_conflicts": attribution_conflicts[:10],
        "safe_unit_conversions": safe_unit_conversions[:10],
        "ignored_format_numbers": ignored_format_numbers[:20],
        "page_numbers_ignored": page_numbers_ignored[:20],
        "forbidden_matches": forbidden_matches[:10],
        # 文风类写作提示（计划 §二 line 17）：**不参与三态判定**、不阻断保存与交付，
        # 只供人工与接口查看。放这里而不是 needs_review_items——后者会拖住最终交付，
        # 而"起句技术化"不该有那个分量。
        "style_notes": style_notes[:10],
    }


# 别名：修订稿 §2.2 的纯计算接口名 ``validate_chapter_candidate(candidate, frozen_context)``。
# 冻结签名仍为 ``(content, citation_ids, context)``，故别名保持同一可调用对象。
validate_chapter_candidate = validate_chapter_output
