"""模型自主计算的**按候选**复算与派生记录装配（《风控约束分层》方案 §三.1~§三.3）。

为什么单独一层，而不是塞进 `workflow` 或 `chapter_guard`：

- **按候选**：首稿、JSON 修复稿、安全修复稿、篇幅修复稿各自独立复算。如果按"章节"维护
  记录，修复稿会继承首稿里那些**已被否决**的计算结果——方案 §三.3 明令禁止；
- **时序**：修复稿是否被采用，在 `workflow.repair_chapter_*` **内部**就定了
  （它们自己调 `validate_chapter_output` 后立刻返回采用/回退）。所以复算必须挂在那两个
  函数内部、其 `validate` **之前**，挂在 workflow 收口处一律无效；
- **纯逻辑**：输入全部来自 `chapter_context`，不读库、不调模型。每章最多一次修复的配额
  与"正常路径不增加模型调用"（§三.1）因此天然成立。

数据流（每个候选）：

    prepare_candidate(result, context, round_no=N, blocked=上一轮被否决明细)
        → 逐条 verify_calculation（calculations.py）
        → verdicts / records(=基线 + 本轮 verified 记录) / trace / hard / review
    apply_round(context, prep)      # 显式切换，防止跨轮串档
    validation = validate_chapter_output(...)
    merge_validation(validation, prep)   # 硬错误并入 reasons，未决并入 needs_review_items
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Iterable, Mapping

from .calculations import verify_calculation

CALC_RULE_VERSION = "calculations-v1"

# 合并进校验结果时的展示上限：修复提示还要塞进 2000 字符的预算里。
MAX_HARD_REASONS = 5
MAX_REVIEW_ITEMS = 10


def _dump(item: Any) -> dict:
    """`LlmCalculation` 或 dict → 普通 dict（verify_calculation 只认 dict）。"""
    if isinstance(item, Mapping):
        return dict(item)
    dump = getattr(item, "model_dump", None)
    return dict(dump()) if callable(dump) else dict(item or {})


def _signature(calc: dict) -> tuple[str, tuple[str, ...], str]:
    """"同一份东西"的指纹：公式 + 输入 + 声明值。

    ⚠️ **不按 `calculation_id` 判"继承"**：修复稿用同一个 ID 但改对了公式，属于**合法修复**，
    必须允许；只有"原样重发同一份被否决的计算"才算继承。
    """
    return (
        str(calc.get("formula") or "").strip(),
        tuple(str(item) for item in (calc.get("input_record_ids") or [])),
        str(calc.get("value") or "").strip(),
    )


def prepare_candidate(
    result: Any,
    context: dict,
    *,
    round_no: int = 1,
    blocked: Mapping[str, tuple[str, tuple[str, ...], str]] | None = None,
) -> dict:
    """为一个候选稿复算全部 `calculations`，并装配该轮的记录集。

    ``blocked`` 是**上一轮被否决**的 `{calculation_id: 签名}`：同一 ID 若原样重发
    （公式、输入、声明值都没变）直接判 `calculation_inherited_rejected`；
    改了公式/输入则按新候选正常复算。

    返回 ``{verdicts, records, trace, hard, review, elapsed_ms, ignored_by_mode, blocked_map}``。
    **不修改** `context`——切换由 `apply_round` 显式执行。
    """
    # 基线必须是**未经本轮改写的纯净记录集**。若回落到 `evidence_records`，
    # 上一轮 `apply_round` 追加的 calculation 记录会被当成"输入记录"继承下去
    # ——这正是方案 §三.3 禁止的"继承被否决的计算结果"。优先顺序：
    # 构建上下文时冻结的 `base_evidence_records` > 首轮记账的 `_calc_base` > 当前记录集。
    base = list(context.get("base_evidence_records") or context.get("_calc_base")
                or context.get("evidence_records") or [])
    context.setdefault("_calc_base", list(base))
    calculations = [_dump(item) for item in (getattr(result, "calculations", None) or [])]
    mode = str((context.get("calculation_policy") or {}).get("mode") or "system_only")

    empty = {
        "verdicts": [], "records": base, "trace": [], "hard": [], "review": [],
        "elapsed_ms": 0, "round": round_no, "ignored_by_mode": False,
        "blocked_map": dict(blocked or {}),
    }
    if not calculations:
        return empty
    if mode != "model":
        # 模式关闭：正文按既有规则校验（数字仍须能对上记录），不让"模型多给了计算"改变判定。
        return {**empty, "ignored_by_mode": True}

    blocked = blocked or {}
    started = time.perf_counter()
    verdicts: list[dict] = []
    trace: list[dict] = []
    hard: list[str] = []
    review: list[dict] = []
    from .evidence_records import build_calculation_records

    for calc in calculations:
        calc_id = str(calc.get("calculation_id") or "").strip()
        anchor = str(calc.get("content_anchor") or "").strip()
        signature = _signature(calc)
        if calc_id and calc_id in blocked and blocked[calc_id] == signature:
            verdict = {
                "calculation_id": calc_id, "status": "rejected",
                "reason_code": "calculation_inherited_rejected",
                "detail": "该计算在上一轮已被否决，且公式、输入与结果均未变化",
                "computed": "", "declared": str(calc.get("value") or ""), "inputs": [],
            }
        else:
            verdict = verify_calculation(calc, base)
            # anchor 命中：模型说"正文这句依赖该指标"。找不到说明它没写该分析，
            # 但数值仍可核对——降为待核验，不硬拒（方案 §三.3「定义或输入状态不明确时
            # 保留待核验稿」）。
            if verdict["status"] == "verified" and anchor and anchor not in str(getattr(result, "content", "") or ""):
                verdict = {**verdict, "status": "needs_review", "reason_code": "anchor_not_found",
                           "detail": "正文中未找到该计算对应的分析原句"}

        # `verify_calculation` 只回状态与错误码，不回 ID——这里补上，
        # 否则下游（blocked_map / 台账 / 修复提示）无从关联。
        verdict["calculation_id"] = calc_id
        verdicts.append(verdict)
        trace.append({
            "calculation_id": calc_id,
            "name": str(calc.get("name") or ""),
            "definition": str(calc.get("definition") or ""),
            "formula": str(calc.get("formula") or ""),
            "input_record_ids": [str(item) for item in (calc.get("input_record_ids") or [])],
            # 输入**实际指向**哪条记录：修复模型必须看到"你填的两个 ID 其实是同一行"，
            # 否则它只会反复换个数字（实测 inputs=[r03c01, r03c01] → 复算恒等于 1）。
            "inputs": [
                {
                    "record_id": str(item.get("record_id") or ""),
                    "label": str(item.get("label") or ""),
                    "period": str(item.get("period") or ""),
                    "value": str(item.get("value") or ""),
                    "unit": str(item.get("unit") or ""),
                }
                for item in (verdict.get("inputs") or [])
            ],
            "value": str(calc.get("value") or ""),
            "computed": str(verdict.get("computed") or ""),
            "unit": str(calc.get("unit") or ""),
            "subject": str(calc.get("subject") or ""),
            "period": str(calc.get("period") or ""),
            "scope": str(calc.get("scope") or ""),
            "precision": calc.get("precision"),
            "status": verdict["status"],
            "reason_code": verdict.get("reason_code") or "",
            "anchor": anchor,
        })
        if verdict["status"] == "rejected":
            code = verdict.get("reason_code") or "formula_invalid"
            label = trace[-1]["name"] or calc_id or "未命名指标"
            hard.append(f"计算未通过复算：{label}（{code}）")
        elif verdict["status"] == "needs_review":
            review.append({
                "code": f"calculation_{verdict.get('reason_code') or 'unclear'}",
                "excerpt": (anchor or trace[-1]["name"] or calc_id)[:120],
                "position": -1,
                "detail": str(verdict.get("detail") or "计算依据待核验"),
            })

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    records = base + build_calculation_records(
        verdicts, calculations, section_key=str(context.get("chapter_key") or ""),
        frozen_records=base,
    )
    return {
        "verdicts": verdicts, "records": records, "trace": trace, "hard": hard, "review": review,
        "elapsed_ms": elapsed_ms, "round": round_no, "ignored_by_mode": False,
        # 下一轮要用"本轮被否决"做继承判定
        "blocked_map": {str(calc.get("calculation_id") or ""): _signature(calc)
                        for verdict, calc in zip(verdicts, calculations)
                        if verdict["status"] == "rejected" and str(calc.get("calculation_id") or "").strip()},
    }


def apply_round(context: dict, prep: dict) -> dict:
    """把某一轮的记录集**显式**装回上下文。

    必须显式：修复稿被拒、采用首稿时若不切回第 1 轮，后续支持评估与写库会串用
    被拒稿的记录（"串档"，B2 的必测项）。
    """
    context["evidence_records"] = prep["records"]
    context["_calc_current"] = prep
    return prep


def merge_validation(validation: dict, prep: dict) -> dict:
    """把复算结论并入章节三态。

    - 硬错误（`HARD_ERROR_CODES`：未冻结输入 / 公式非法 / 除零 / 值不符 / 量纲错配）
      → 追加 `reasons` 并置 `rejected`，交由**同一次修复**消费（§三.3）；
    - 待核验类（`REVIEW_CODES`）→ 进 `needs_review_items`，不阻断单章保存、阻断最终交付。
    """
    if not prep or prep.get("ignored_by_mode"):
        return validation
    hard = list(prep.get("hard") or [])
    if hard:
        validation.setdefault("reasons", []).extend(hard[:MAX_HARD_REASONS])
        validation["status"] = "rejected"
        validation["length_only_failure"] = False
        validation["calculation_failed"] = True
    review = list(prep.get("review") or [])
    if review:
        validation.setdefault("needs_review_items", []).extend(review[:MAX_REVIEW_ITEMS])
        validation["needs_review_count"] = len(validation["needs_review_items"])
        # 三态要在**这里重算**：`validate_chapter_output` 的三态是在它内部按自己的
        # `needs_review_items` 算的，我们在它之后追加，不重算就会停在 passed
        # ——"算出来但待核验"的计算会被当成完全通过。
        if not hard and str(validation.get("status") or "") == "passed":
            validation["status"] = "needs_review"
    # 台账与修复提示都读这几个键（形状固定，渲染层不用再猜）。
    validation["calculation_trace"] = list(prep.get("trace") or [])
    validation["calculation_summary"] = {
        "count": len(prep.get("verdicts") or []),
        "verified": len([item for item in prep.get("verdicts") or [] if item["status"] == "verified"]),
        "needs_review": len([item for item in prep.get("verdicts") or [] if item["status"] == "needs_review"]),
        "rejected": len([item for item in prep.get("verdicts") or [] if item["status"] == "rejected"]),
        "elapsed_ms": int(prep.get("elapsed_ms") or 0),
        "round": int(prep.get("round") or 1),
    }
    return validation


def trace_hash(trace: Iterable[dict]) -> str:
    """被采用候选的规范化摘要（写 attempts / report_sections 列，供重放比对）。

    ⚠️ 用**固定字段序 + 紧凑分隔符**，不是 `json.dumps(trace)` 原样：字典键序、空白、
    浮点文本差异都会让同一份内容算出不同哈希，那这个列就没有可比性了。
    """
    normalized = [
        {
            "calculation_id": str(item.get("calculation_id") or ""),
            "name": str(item.get("name") or ""),
            "definition": str(item.get("definition") or ""),
            "formula": str(item.get("formula") or ""),
            "input_record_ids": [str(value) for value in (item.get("input_record_ids") or [])],
            "value": str(item.get("value") or ""),
            "computed": str(item.get("computed") or ""),
            "unit": str(item.get("unit") or ""),
            "subject": str(item.get("subject") or ""),
            "period": str(item.get("period") or ""),
            "scope": str(item.get("scope") or ""),
            "precision": item.get("precision"),
            "status": str(item.get("status") or ""),
            "reason_code": str(item.get("reason_code") or ""),
            "anchor": str(item.get("anchor") or ""),
        }
        for item in (trace or [])
    ]
    if not normalized:
        return ""
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def repair_details(validation: dict) -> list[dict]:
    """给修复模型的逐条计算错误（`llm._safety_repair_details` 消费）。"""
    items: list[dict] = []
    for trace in (validation.get("calculation_trace") or []):
        if trace.get("status") != "rejected":
            continue
        items.append({
            "calculation_id": str(trace.get("calculation_id") or ""),
            "name": str(trace.get("name") or "")[:40],
            "reason_code": str(trace.get("reason_code") or ""),
            "formula": str(trace.get("formula") or "")[:120],
            "input_record_ids": list(trace.get("input_record_ids") or [])[:6],
            "declared": str(trace.get("value") or ""),
            "computed": str(trace.get("computed") or ""),
            "anchor": str(trace.get("anchor") or "")[:120],
        })
        if len(items) >= MAX_HARD_REASONS:
            break
    return items
