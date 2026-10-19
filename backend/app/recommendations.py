"""承保/拒保/额度/费率建议的结构化与复核（《风控约束分层》方案 §四 line 52~56）。

三条边界，决定了本模块的形态：

1. **建议是提议，不是披露事实**：建议值可以是新值（不需要冻结记录背书），但必须写明依据与
   前提条件，并且**只能进"建议草案"，不能进 `evidence_records`**——否则一个建议值会以
   "已冻结记录"的身份给正文其他数字背书，也可以被其他计算当输入（§四 line 53 明禁）。
2. **每条都要人工确认**：确认只表示"报告采用该建议"，**不代表业务审批**（`decision_scope`）。
3. **确认会失效**：改建议、改正文、改依据之后，原确认必须回到 `stale`（由
   `write_report_section` 的 `reset_section_review` 漏斗统一处理，见 `stale_section_recommendations`）。

本模块只做**结构**（归一、校验、渲染文案），不碰数据库——落库与状态机在
`report_delivery` / `main` 里，便于单测。
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

RECOMMENDATION_VERSION = "recommendations-v1"

# 建议类型白名单（方案 §四 line 52）：承保/拒保/额度/费率 + 条件性建议 + 其他。
RECOMMENDATION_TYPES = ("accept", "reject", "guarantee_amount", "guarantee_rate", "condition", "other")

# 类型别名：模型常写中文或英文变体，归一后再落库（否则前端分组会碎成一堆近似值）。
_TYPE_ALIASES = {
    "approve": "accept", "acceptance": "accept", "承保": "accept", "建议承保": "accept",
    "decline": "reject", "refuse": "reject", "拒保": "reject", "建议拒绝": "reject", "不建议承保": "reject",
    "amount": "guarantee_amount", "额度": "guarantee_amount", "担保额度": "guarantee_amount",
    "rate": "guarantee_rate", "费率": "guarantee_rate", "担保费率": "guarantee_rate",
    "conditional": "condition", "前提": "condition", "条件": "condition",
}

MAX_RECOMMENDATIONS = 6

# 未结构化建议的检测：决策词 + 数字/百分比同句出现。刻意**不含**"建议关注/建议补充材料"
# 这类无决策动作的句子（它们不是承保建议）。
_DECISION_WORDS = ("承保", "拒保", "担保额度", "担保费率", "核定担保")
# ⚠️ 光有"决策词 + 数字"不能判"未结构化建议"：担保章通篇在陈述**拟议结构**
# （"本次担保额度为 5 亿元，占净资产 12%"），那是披露事实不是提议。
# 2026-10-17 打开 `recommendation_mode=structured` 前复核发现，按旧口径这句会被判
# 未结构化建议 → 整章拒稿回退。所以再要求**提议标记**，把"陈述拟议结构"与
# "给出承保建议"分开。既成决定（核定/批准）由 `chapter_guard` 的决策校验单独拦，
# 不靠这条兜。
# 刻意不收"拟"：拟议担保额度、拟提供担保都是材料/结构描述。
_PROPOSAL_MARKERS = ("建议", "提议", "可考虑", "主张", "倾向于", "宜将", "宜对", "宜按")
_SENTENCE_SPLIT_RE = re.compile(r"[。；;！!？?\n]")
_DIGIT_RE = re.compile(r"\d")


def _dump(item: object) -> dict:
    if isinstance(item, Mapping):
        return dict(item)
    dump = getattr(item, "model_dump", None)
    return dict(dump()) if callable(dump) else dict(item or {})


def _normalize_type(raw: object) -> str:
    text = str(raw or "").strip().lower()
    if text in RECOMMENDATION_TYPES:
        return text
    return _TYPE_ALIASES.get(text, "other")


def _number_text(raw: object) -> str:
    """额度/费率统一成字符串（保留模型给的精度）；非数值返回空串。"""
    text = str(raw if raw is not None else "").strip().replace(",", "")
    if not text:
        return ""
    try:
        Decimal(text)
    except InvalidOperation:
        return ""
    return text


def basis_hash(item: Mapping) -> str:
    """确认的失效基准：**建议内容 + 依据 + 参数**的规范化摘要。

    不含 status/确认人/时间——那些是复核动作本身，改了不该让确认失效。
    """
    normalized = {
        "recommendation_type": str(item.get("recommendation_type") or ""),
        "content": str(item.get("content") or "").strip(),
        "basis": str(item.get("basis") or "").strip(),
        "basis_record_ids": sorted(str(value) for value in (item.get("basis_record_ids") or [])),
        "preconditions": [str(value).strip() for value in (item.get("preconditions") or [])],
        "amount_value": str(item.get("amount_value") or ""),
        "amount_unit": str(item.get("amount_unit") or ""),
        "rate_value": str(item.get("rate_value") or ""),
        "rate_unit": str(item.get("rate_unit") or ""),
        "parameters": {str(key): str(value) for key, value in (item.get("parameters") or {}).items()},
    }
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def render_draft(item: Mapping) -> str:
    """服务端渲染并冻结的"建议草案"文案。

    冻结在库里而不是每次现拼：确认时看到的那段文字必须与之后审计看到的一致。
    ⚠️ 必须出现"待人工确认，不代表业务审批"——这是方案 §四 line 54 的措辞要求。
    """
    type_label = {
        "accept": "建议承保", "reject": "建议拒保",
        "guarantee_amount": "担保额度建议", "guarantee_rate": "担保费率建议",
        "condition": "条件性建议", "other": "其他建议",
    }.get(str(item.get("recommendation_type") or ""), "其他建议")
    parts = [f"【{type_label}】{str(item.get('content') or '').strip()}"]
    if item.get("amount_value"):
        parts.append(f"额度：{item.get('amount_value')}{item.get('amount_unit') or ''}")
    if item.get("rate_value"):
        parts.append(f"费率：{item.get('rate_value')}{item.get('rate_unit') or ''}")
    if str(item.get("basis") or "").strip():
        parts.append(f"依据：{str(item.get('basis')).strip()}")
    preconditions = [str(value).strip() for value in (item.get("preconditions") or []) if str(value).strip()]
    if preconditions:
        parts.append("前提条件：" + "；".join(preconditions))
    parts.append("（建议草案，待人工确认；不代表业务担保审批结论）")
    return " ".join(parts)


def normalize_and_validate(recommendations: Iterable[object], context: dict) -> dict:
    """把模型返回的 `recommendations[]` 归一并做结构校验。

    返回 ``{items, hard, review}``：

    - ``hard``：确定的结构错误（缺内容、依据记录不存在、金额/费率不是数值）→ 进**同一次修复**；
    - ``review``：结构可用但依据未结构化（`basis_record_ids` 为空）或 anchor 找不到 → 待核验。

    依据记录必须存在于**本章冻结记录**里；建议值本身**不要求**有记录（它就是提议）。
    """
    frozen = {str(item.get("record_id")) for item in (context.get("evidence_records") or [])}
    content = str(context.get("draft") or "")
    items: list[dict] = []
    hard: list[str] = []
    review: list[dict] = []

    for index, raw in enumerate(recommendations or [], start=1):
        item = _dump(raw)
        rec_id = str(item.get("recommendation_id") or "").strip() or f"r{index}"
        recommendation_type = _normalize_type(item.get("recommendation_type"))
        text = str(item.get("content") or "").strip()
        if not text:
            hard.append(f"建议 {rec_id} 缺少内容")
            continue
        basis_ids = [str(value) for value in (item.get("basis_record_ids") or []) if str(value).strip()]
        unknown = [value for value in basis_ids if value not in frozen]
        if unknown:
            hard.append(f"建议 {rec_id} 引用了不存在的依据记录：{'、'.join(unknown[:3])}")
            continue
        amount_value = _number_text(item.get("amount_value"))
        rate_value = _number_text(item.get("rate_value"))
        if str(item.get("amount_value") or "").strip() and not amount_value:
            hard.append(f"建议 {rec_id} 的额度不是可解析数值")
            continue
        if str(item.get("rate_value") or "").strip() and not rate_value:
            hard.append(f"建议 {rec_id} 的费率不是可解析数值")
            continue

        normalized = {
            "recommendation_id": rec_id,
            "recommendation_type": recommendation_type,
            "content": text,
            "basis": str(item.get("basis") or "").strip(),
            "basis_record_ids": basis_ids,
            "preconditions": [str(value).strip() for value in (item.get("preconditions") or []) if str(value).strip()],
            "amount_value": amount_value,
            "amount_unit": str(item.get("amount_unit") or "").strip(),
            "rate_value": rate_value,
            "rate_unit": str(item.get("rate_unit") or "").strip(),
            "parameters": {str(key): str(value) for key, value in (item.get("parameters") or {}).items()},
            "content_anchor": str(item.get("content_anchor") or "").strip(),
        }
        normalized["draft_text"] = render_draft(normalized)
        normalized["basis_hash"] = basis_hash(normalized)

        if not basis_ids:
            review.append({
                "code": "recommendation_basis_unstructured", "excerpt": text[:120], "position": -1,
                "detail": f"建议 {rec_id} 未指明依据记录，依据需人工核对",
            })
        anchor = normalized["content_anchor"]
        if anchor and content and anchor not in content:
            review.append({
                "code": "recommendation_anchor_not_found", "excerpt": anchor[:120], "position": -1,
                "detail": f"建议 {rec_id} 的正文原句未在正文中找到",
            })
        items.append(normalized)
        if len(items) >= MAX_RECOMMENDATIONS:
            break

    return {"items": items, "hard": hard, "review": review}


def detect_unstructured_proposals(content: str, structured: Iterable[Mapping] = ()) -> list[str]:
    """正文里出现"决策词 + 数字"但**没有**对应结构化建议 → 视为未结构化建议。

    为什么必须有这一条：模型可能把"建议承保 5000 万元"直接写进正文而不返回
    `recommendations[]`。那样它既不会被确认、也不会被交付门拦住，等于绕过人工复核
    （方案 §四 line 54「未结构化返回的建议进入同一次修复，不能绕过复核」）。

    只认**提议标记 + 决策词 + 同句数字**：`建议补充材料/建议关注治理` 这类没有决策动作的句子
    不算；`本次担保额度为 5 亿元` 这类**陈述拟议结构**的句子也不算（那是披露事实，
    不是提议——把它判成未结构化建议会让整个担保章无法通过）。
    """
    anchors = [
        str(item.get("content_anchor") or "").strip()
        for item in (structured or [])
        if str(item.get("content_anchor") or "").strip()
    ]
    findings: list[str] = []
    for clause in _SENTENCE_SPLIT_RE.split(content or ""):
        text = clause.strip()
        if not text:
            continue
        if not any(word in text for word in _PROPOSAL_MARKERS):
            continue
        if not any(word in text for word in _DECISION_WORDS):
            continue
        if not _DIGIT_RE.search(text):
            continue
        # 已被某条结构化建议覆盖（anchor 落在本句）就不再报。
        if any(anchor in text for anchor in anchors):
            continue
        findings.append(text[:120])
    return findings
