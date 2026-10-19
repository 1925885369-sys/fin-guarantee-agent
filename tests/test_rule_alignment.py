"""B0 规则统一：计算/建议口径"一处定义、三处消费"+ 趋势因果窄检查。

依据《风控约束分层与模型分析能力调整方案》：
- §二 line 18：统一模型请求、修复指令和后端校验中的计算及建议规则；
- §二 line 20：允许引用表格关键数字、分析趋势与解释评级关注因素；禁止无依据的原因、
  趋势及确定性预测；
- §二 line 21：来源/评级/缺口边界由系统附加，取消逐字固定句的拒绝条件。

本文件守三件事：
1. 计算/建议句**只在一处定义**，编译 Prompt 与运行时请求都从它派生（否则必然再次漂移）；
2. 默认值（`system_only` / `off`）**不改变既有行为**——B1 才接可编辑字段；
3. 趋势/因果/预测只落 `needs_review`，不硬拒，且带引文的合法分析不得误报。
"""
from __future__ import annotations

from backend.app import llm
from backend.app.chapter_guard import _trend_causal_advisories, validate_chapter_output
from backend.app.chapter_prompts import (
    CALCULATION_SENTENCE_MODEL, CALCULATION_SENTENCE_SYSTEM_ONLY, DEFAULT_CALCULATION_MODE,
    DEFAULT_RECOMMENDATION_MODE, GENERATE_BOUNDARY_SENTENCE, RECOMMENDATION_SENTENCE_OFF,
    RECOMMENDATION_SENTENCE_STRUCTURED, REWRITE_BOUNDARY_SENTENCE,
    calculation_boundary_sentence, claim_boundary_sentence, compile_structured_prompt,
    load_chapter_config, recommendation_boundary_sentence,
)


def _config(**overrides) -> dict:
    """以**出厂默认**章节为基线。

    2026-10-17 起 `financial` 等四章出厂即 `calculation_mode=model`、`guarantee` 等两章
    `recommendation_mode=structured`；若仍拿 financial 当"默认值"基线，
    `test_defaults_keep_previous_behaviour` 会因为出厂配置变化而失真。
    """
    config = load_chapter_config("governance")
    config.update(overrides)
    return config


# --------------------------------------------------------------- 口径单一来源 --

def test_defaults_keep_previous_behaviour():
    """B0 只铺管道、不改行为：默认仍是"禁自算 + 禁建议"。"""
    assert DEFAULT_CALCULATION_MODE == "system_only"
    assert DEFAULT_RECOMMENDATION_MODE == "off"
    assert calculation_boundary_sentence(_config()) == CALCULATION_SENTENCE_SYSTEM_ONLY
    assert recommendation_boundary_sentence(_config()) == RECOMMENDATION_SENTENCE_OFF


def test_boundary_sentence_is_composed_from_three_parts():
    """写作边界 + 计算句 + 建议句三段拼装；不应出现"既禁止又允许自算"的自相矛盾。"""
    composed = claim_boundary_sentence(_config())
    assert composed.startswith(GENERATE_BOUNDARY_SENTENCE + " ") or \
        composed.startswith(REWRITE_BOUNDARY_SENTENCE + " ")
    assert CALCULATION_SENTENCE_SYSTEM_ONLY in composed
    assert RECOMMENDATION_SENTENCE_OFF in composed
    # 生成模式边界本身不再内嵌计算/建议句（拆出去才不会被拼装重复）
    assert "不得自行计算或换算" not in GENERATE_BOUNDARY_SENTENCE
    assert "可以给出承保" not in GENERATE_BOUNDARY_SENTENCE

    enabled = claim_boundary_sentence(_config(calculation_mode="model", recommendation_mode="structured"))
    assert CALCULATION_SENTENCE_MODEL in enabled
    assert RECOMMENDATION_SENTENCE_STRUCTURED in enabled
    # 放开自算后不得再残留"只能引用系统预计算结果"
    assert CALCULATION_SENTENCE_SYSTEM_ONLY not in enabled


def test_rewrite_mode_no_longer_claims_draft_is_only_boundary():
    """方案 §二 line 18：移除"确定性草稿是**唯一**边界"。"""
    assert "唯一" not in REWRITE_BOUNDARY_SENTENCE
    assert "冻结记录" in REWRITE_BOUNDARY_SENTENCE


def test_runtime_request_matches_compiled_prompt():
    """编译 Prompt 与运行时请求必须同时变——只改一处就是"指令冲突"。"""
    compiled_default = compile_structured_prompt("financial", _config())
    assert CALCULATION_SENTENCE_SYSTEM_ONLY in compiled_default

    compiled_open = compile_structured_prompt("financial", _config(calculation_mode="model"))
    assert CALCULATION_SENTENCE_MODEL in compiled_open
    assert CALCULATION_SENTENCE_SYSTEM_ONLY not in compiled_open

    _system, request, *_ = llm._report_request(
        "财务分析", "正文。", [], None, "financial",
        {"project_id": "p", "prompt_config": _config(calculation_mode="model",
                                                     generation_mode="generate")},
    )
    assert CALCULATION_SENTENCE_MODEL in request["generation_scope"]
    assert CALCULATION_SENTENCE_SYSTEM_ONLY not in request["generation_scope"]


def test_legacy_sentences_are_stripped_only_when_mode_allows(monkeypatch):
    """旧禁令按模式剥离：允许建议时剥，off 时保留（剥了反而少一道约束）。"""
    legacy_prompt = (
        "业务目标：评估偿债能力。\n"
        "确定性草稿是允许主张的唯一边界；只能改善表达，不得增加、删除或改变事实、数字、规则状态和风险状态。\n"
        "不得由模型计算金额、比例、覆盖率、评级差异、偿债指标或现金流预测，只能引用系统确定性结果。"
    )
    monkeypatch.setattr(llm, "chapter_prompt", lambda *_: legacy_prompt)

    opened = llm._report_request(
        "财务分析", "正文。", [], None, "financial",
        {"project_id": "p", "prompt_config": _config(calculation_mode="model")},
    )[0]
    assert "唯一边界" not in opened
    assert "不得由模型计算" not in opened
    assert "业务目标：评估偿债能力。" in opened          # 用户内容保留

    closed = llm._report_request(
        "财务分析", "正文。", [], None, "financial",
        {"project_id": "p", "prompt_config": _config()},
    )[0]
    assert "不得由模型计算" in closed


# ------------------------------------------------------- 趋势/因果/预测窄检查 --

def _advisories(text: str) -> list[str]:
    return [item["code"] for item in _trend_causal_advisories(text)]


def test_causal_trend_without_citation_needs_review():
    codes = _advisories("营业收入上升，主要系销量增长所致。")
    assert "unsupported_causal_claim" in codes


def test_causal_trend_with_citation_is_not_flagged():
    """同一句里有引用就是合法分析——按逗号切会把它误判成"无依据"。"""
    assert "unsupported_causal_claim" not in _advisories("营业收入上升（E01），主要系销量增长所致。")


def test_deterministic_forecast_is_flagged():
    assert "deterministic_forecast" in _advisories("预计未来三年营业收入将达10亿元。")
    assert "deterministic_forecast" not in _advisories("预计未来三年营业收入将达10亿元（E01）。")


def test_causal_marker_does_not_match_words_containing_shou():
    """单字"受"会命中'受托管理人''接受'——必须用 `受…影响` 结构。"""
    assert "unsupported_causal_claim" not in _advisories("受托管理人按约定履职，资产规模保持稳定。")


def test_trend_advisories_never_hard_reject():
    config = load_chapter_config("financial")
    result = validate_chapter_output(
        "据募集说明书披露，材料不足。营业收入上升，主要系销量增长所致。",
        [], {"prompt_config": config, "chapter_key": "financial", "evidence": [],
             "allowed_citation_ids": [], "has_missing": True},
    )
    assert result["status"] != "rejected", result["reasons"]
    assert any(item["code"] == "unsupported_causal_claim"
               for item in result.get("needs_review_items") or [])


# --------------------------------------------------- 真实试跑误报（2026-10-17 北投）--


def _codes(items):
    return [item["code"] for item in items]


def test_forecast_check_ignores_precomputed_wording():
    """「系统预计算」不是预测：`预计` 是 `预计算` 的子串，裸子串匹配会误报 14 条。

    2026-10-17 北投 financial 真实试跑实测：15 条预测告警中 14 条来自该词。
    """
    from backend.app.chapter_guard import _trend_causal_advisories

    assert _codes(_trend_causal_advisories("以系统预计算的资产负债率观察，2025年末回落至64.84%。")) == []
    assert _codes(_trend_causal_advisories("系统预计算的2025年度营业收入增速为32.14%。")) == []


def test_forecast_check_still_flags_real_forecast():
    from backend.app.chapter_guard import _trend_causal_advisories

    assert _codes(_trend_causal_advisories("发行人未来几年的投资规模预计仍将保持较高水平。")) == [
        "deterministic_forecast"
    ]
    assert _codes(_trend_causal_advisories("项目投产后有望显著改善现金流。")) == ["deterministic_forecast"]


def test_contract_evidence_check_only_applies_to_guarantee_contracts():
    """「监管协议已签署」不该要求担保合同证据（北投 proceeds #1 实测误报）。"""
    from backend.app.chapter_guard import _type_evidence_advisories

    evidence = {"evidence": [{"id": "e1", "document_kind": "募集说明书"}]}
    assert _codes(_type_evidence_advisories("认定专户已开立、监管协议已签署或资金已全额归集。", evidence)) == []
    assert _codes(_type_evidence_advisories("本期债券承销协议已签署。", evidence)) == []
    # 真正的担保合同结论仍要被点名
    assert _codes(_type_evidence_advisories("保证合同已生效，担保责任已承担。", evidence)) == [
        "contract_evidence_missing"
    ]


def test_contract_evidence_needs_both_contract_and_effective_conclusion():
    """只有合同名、没有"已生效/已签署"结论 → 不是"合同已生效"的主张。"""
    from backend.app.chapter_guard import _type_evidence_advisories

    evidence = {"evidence": [{"id": "e1", "document_kind": "募集说明书"}]}
    assert _codes(_type_evidence_advisories("依据担保合同约定，反担保措施如下。", evidence)) == []


def test_factory_enables_new_modes_per_chapter():
    """出厂配置按章启用新能力（2026-10-17 用户确认"打开新模式"）。

    守的是"别悄悄改回去"：如果哪天有人把这四章的 calculation_mode 改回 system_only，
    自主计算与复算链路就永远跑不到，而所有测试仍然全绿。
    """
    from backend.app.chapter_prompts import load_chapter_config

    for key in ("financial", "cashflow", "debt", "rating"):
        assert load_chapter_config(key)["calculation_mode"] == "model", key
    for key in ("guarantee", "counter_guarantee"):
        assert load_chapter_config(key)["recommendation_mode"] == "structured", key
    # 未启用的章节必须保持不动
    for key in ("governance", "scope", "operations", "legal"):
        assert load_chapter_config(key)["calculation_mode"] == "system_only", key
        assert load_chapter_config(key)["recommendation_mode"] == "off", key


# ------------------------------------------- 正文出现内部记录 ID（2026-10-17 真实试跑）--


def test_internal_record_id_in_body_is_rejected():
    """正文把 `metric:debt_ratio:2023` 当引用别名写进去 → 硬拒（走同一次修复）。

    真实试跑实测："…回落约1.22个百分点（metric:debt_ratio:2023、metric:debt_ratio:2024）"。
    记录 ID 只允许出现在 `calculations.input_record_ids`。
    """
    result = validate_chapter_output(
        "2025年末资产负债率较2024年末回落约1.22个百分点（metric:debt_ratio:2024、metric:debt_ratio:2025）。",
        [], {"prompt_config": load_chapter_config("governance"), "chapter_key": "governance",
             "evidence": [], "allowed_citation_ids": [], "has_missing": True},
    )
    assert result["status"] == "rejected"
    assert result.get("internal_record_ids"), result
    assert any("内部记录 ID" in str(item) for item in result["reasons"])


def test_plain_citation_alias_is_not_treated_as_record_id():
    from backend.app.chapter_guard import _internal_record_ids_in

    assert _internal_record_ids_in("营业收入为1,234万元（E01），较上年增长5%。") == []
    # 表格/事实等各类前缀都要认出来
    found = _internal_record_ids_in("（fact:8f2a、table:t1:r01c01、calc:financial:c1）")
    assert found == ["fact:8f2a", "table:t1:r01c01", "calc:financial:c1"], found
