"""B2 复算与派生记录：按候选复算、记录装配、继承判定、三态合并、轨迹摘要。

对应《风控约束分层与模型分析能力调整方案》§三.1~§三.3。本文件守的几条最容易写错：
1. 派生记录的值必须是**系统复算值**，不是模型声明值（否则错值会自己给自己背书）；
2. 修复稿"原样重发"被否决的计算要判继承，但**同 ID 改对公式必须放行**；
3. 复算结论要在**校验之后**并入三态，且没有硬错误时要自己把 passed 降成 needs_review；
4. 每轮记录集必须显式切换，修复失败回退首稿时不能残留修复稿的记录。
"""
from __future__ import annotations

from backend.app import chapter_calculations as cc
from backend.app.evidence_records import _traceable, supportable_records
from backend.app.schemas import LlmSection


def _rec(record_id: str, value, *, unit="万元", label="", period="2025",
         subject="发行人", status="verified") -> dict:
    return {
        "record_id": record_id, "record_type": "fact", "value": str(value), "unit": unit,
        "label": label, "period": period, "subject": subject, "scope": "合并口径",
        "status": status, "source_ids": [f"src-{record_id}"], "page": 12,
        "locator": {"page": 12}, "formula": "", "source_fact_ids": [],
    }


BASE = [_rec("fact:rev", 1000, label="营业收入"), _rec("fact:cost", 600, label="营业成本")]
CONTEXT = {"chapter_key": "financial", "evidence_records": BASE,
           "calculation_policy": {"mode": "model"}}
GOOD = {"calculation_id": "c1", "name": "毛利率", "definition": "毛利/营业收入",
        "formula": "(营业收入-营业成本)/营业收入", "input_record_ids": ["fact:rev", "fact:cost"],
        "value": "0.4", "unit": "倍", "subject": "发行人", "period": "2025", "scope": "合并口径",
        "content_anchor": "毛利率为40%"}
BAD = {**GOOD, "value": "0.55"}


def _section(calcs, content="毛利率为40%。"):
    return LlmSection.model_validate({"content": content, "calculations": calcs})


def _context() -> dict:
    """每次都建新 dict：`_calc_base` 会挂在上下文上，复用会互相污染。"""
    return {"chapter_key": "financial", "evidence_records": list(BASE),
            "calculation_policy": {"mode": "model"}}


# ------------------------------------------------------------------ 模式边界 --

def test_mode_off_ignores_calculations_without_changing_verdict():
    """模式关闭时"模型多给了计算"不改变既有判定，记录集原样返回。"""
    prep = cc.prepare_candidate(_section([GOOD]), {**_context(), "calculation_policy": {"mode": "system_only"}})
    assert prep["ignored_by_mode"] is True
    assert prep["records"] == BASE
    assert not prep["hard"] and not prep["review"]


# -------------------------------------------------------------- 派生记录装配 --

def test_verified_calculation_becomes_traceable_derived_record():
    prep = cc.prepare_candidate(_section([GOOD]), _context())
    assert prep["verdicts"][0]["status"] == "verified"
    records = [item for item in prep["records"] if item["record_type"] == "calculation"]
    assert len(records) == 1
    record = records[0]
    # 值用复算值；声明值只能在 raw / extra 里
    assert record["value"] == "0.4" and record["raw"] == "0.4"
    assert record["status"] == "derived"
    assert record["rule_version"] == cc.CALC_RULE_VERSION
    # 来源从输入汇总，且因输入链 + 公式而可追溯
    assert set(record["source_ids"]) == {"src-fact:rev", "src-fact:cost"}
    assert record["source_fact_ids"] == ["fact:rev", "fact:cost"]
    assert _traceable(record)
    assert any(item["record_id"] == record["record_id"] for item in supportable_records(prep["records"]))


def test_rejected_calculation_leaves_no_record_and_raises_hard_reason():
    prep = cc.prepare_candidate(_section([BAD]), _context())
    assert prep["verdicts"][0]["status"] == "rejected"
    assert not [item for item in prep["records"] if item["record_type"] == "calculation"]
    assert any("计算未通过复算" in reason for reason in prep["hard"])


def test_needs_review_input_downgrades_record_and_result():
    base = [_rec("fact:rev", 1000, label="营业收入", status="needs_review"),
            _rec("fact:cost", 600, label="营业成本")]
    prep = cc.prepare_candidate(_section([GOOD]), {"chapter_key": "financial",
                                                  "evidence_records": base,
                                                  "calculation_policy": {"mode": "model"}})
    assert prep["verdicts"][0]["status"] == "needs_review"
    records = [item for item in prep["records"] if item["record_type"] == "calculation"]
    assert records and records[0]["status"] == "needs_review"


def test_anchor_missing_is_needs_review_not_rejection():
    prep = cc.prepare_candidate(
        _section([{**GOOD, "content_anchor": "正文里没有这句"}], content="本段无该分析。"),
        _context(),
    )
    assert prep["verdicts"][0]["status"] == "needs_review"
    assert prep["verdicts"][0]["reason_code"] == "anchor_not_found"


# ------------------------------------------------------------------ 继承判定 --

def test_inheritance_requires_identical_signature_but_allows_fixing_formula():
    first = cc.prepare_candidate(_section([BAD]), _context())
    blocked = first["blocked_map"]
    assert "c1" in blocked

    # 原样重发 → 判继承
    same = cc.prepare_candidate(_section([BAD]), _context(), round_no=2, blocked=blocked)
    assert same["verdicts"][0]["reason_code"] == "calculation_inherited_rejected"

    # 同 ID 改对公式 → 正常复算通过（否则修复永远不可能改对）
    fixed = cc.prepare_candidate(_section([GOOD]), _context(), round_no=2, blocked=blocked)
    assert fixed["verdicts"][0]["status"] == "verified"


# ------------------------------------------------------------------ 三态合并 --

def test_merge_validation_sets_rejected_and_marks_calculation_failed():
    prep = cc.prepare_candidate(_section([BAD]), _context())
    merged = cc.merge_validation({"status": "passed", "reasons": [], "needs_review_items": []}, prep)
    assert merged["status"] == "rejected"
    assert merged["calculation_failed"] is True
    assert merged["length_only_failure"] is False
    assert merged["calculation_summary"]["rejected"] == 1


def test_merge_validation_downgrades_passed_to_needs_review():
    """没有硬错误但有待核验计算时，三态必须从 passed 降级——validate 不会替我们重算。"""
    base = [_rec("fact:rev", 1000, label="营业收入", status="needs_review"),
            _rec("fact:cost", 600, label="营业成本")]
    prep = cc.prepare_candidate(_section([GOOD]), {"chapter_key": "financial",
                                                  "evidence_records": base,
                                                  "calculation_policy": {"mode": "model"}})
    merged = cc.merge_validation({"status": "passed", "reasons": [], "needs_review_items": []}, prep)
    assert merged["status"] == "needs_review"


def test_merge_validation_is_noop_when_mode_off():
    prep = cc.prepare_candidate(_section([GOOD]), {**_context(), "calculation_policy": {"mode": "off"}})
    validation = {"status": "passed", "reasons": [], "needs_review_items": []}
    assert cc.merge_validation(validation, prep) is validation


# --------------------------------------------------------------- 轮次不串档 --

def test_apply_round_switches_records_between_candidates():
    context = _context()
    first = cc.prepare_candidate(_section([GOOD]), context, round_no=1)
    cc.apply_round(context, first)
    assert len(context["evidence_records"]) == len(BASE) + 1

    second = cc.prepare_candidate(_section([BAD]), context, round_no=2)
    cc.apply_round(context, second)
    # 第 2 轮被否决 → 只剩基线，不得残留第 1 轮的计算记录
    assert len(context["evidence_records"]) == len(BASE)

    cc.apply_round(context, first)
    assert len(context["evidence_records"]) == len(BASE) + 1


def test_candidate_base_ignores_records_added_by_previous_round():
    """基线取冻结副本：上一轮追加的计算记录不能被当成下一轮的"输入记录"。"""
    context = _context()
    context["base_evidence_records"] = list(BASE)
    first = cc.prepare_candidate(_section([GOOD]), context, round_no=1)
    cc.apply_round(context, first)
    second = cc.prepare_candidate(_section([{**GOOD, "input_record_ids": ["calc:financial:c1"]}]),
                                  context, round_no=2)
    assert second["verdicts"][0]["reason_code"] == "input_not_frozen"


# ---------------------------------------------------------------- 轨迹摘要 --

def test_trace_hash_is_stable_and_order_insensitive():
    one = [{"calculation_id": "c1", "formula": "a/b", "value": "0.4", "status": "verified"}]
    same_content = [{"status": "verified", "value": "0.4", "formula": "a/b", "calculation_id": "c1"}]
    assert cc.trace_hash(one) == cc.trace_hash(same_content)
    assert cc.trace_hash(one) != cc.trace_hash([{**one[0], "value": "0.5"}])
    assert cc.trace_hash([]) == ""


def test_repair_details_only_reports_rejected():
    prep = cc.prepare_candidate(_section([BAD]), _context())
    merged = cc.merge_validation({"status": "passed", "reasons": [], "needs_review_items": []}, prep)
    details = cc.repair_details(merged)
    assert details and details[0]["calculation_id"] == "c1"
    assert details[0]["formula"] and details[0]["computed"]
