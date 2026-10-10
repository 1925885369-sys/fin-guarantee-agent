from __future__ import annotations

from backend.app import chapter_guard
from backend.app.chapter_guard import build_chapter_context, validate_chapter_output
from backend.app.chapter_prompts import (
    BOND_CHAPTER_KEYS, BOND_CHAPTER_PART, BOND_CHAPTER_PARTS, BOND_CHAPTER_TITLES,
    _length_guidance, chapter_uses_llm, load_chapter_config, prompt_config_payload,
)
from backend.app.main import startup
from backend.app.professional import sync_fact_assertions


def _context(key: str, *, missing: bool = False, conflicts: list | None = None) -> dict:
    config = load_chapter_config(key)
    conflict_block = "### 冲突事实（以下断言必须并列保留）\n- 营业收入（2025，字段 revenue）：\n  - 断言 a-1：100万元；来源：s-1" if conflicts else ""
    return {
        "prompt_config": config,
        "prompt_version": "2.1.0",
        "prompt_sha256": "prompt-hash",
        "draft": "据募集说明书披露，发行规模为20000万元；披露信息待核验，材料不足。",
        "facts": [], "metrics": [], "cross_checks": [],
        "evidence": [{"id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书", "quote": "发行规模20000万元"}],
        "conflicts": conflicts or [], "conflict_block": conflict_block, "has_missing": missing,
        "allowed_citation_ids": ["c-source-1"],
    }


def test_bond_prompt_pack_has_all_chapters_and_contracts():
    payload = prompt_config_payload(bond=True)
    assert set(payload["chapters"]) == set(BOND_CHAPTER_KEYS)
    for key, config in payload["chapters"].items():
        assert "input_contract" in config and "output_contract" in config
        if config["llm"]:
            contract = config["input_contract"]
            assert any(contract[field] for field in ("fact_keys", "metric_keys", "rule_keys", "source_kinds")), key


def test_bond_chapter_metadata_covers_every_chapter_in_order():
    assert set(BOND_CHAPTER_TITLES) == set(BOND_CHAPTER_KEYS)
    for key, title in BOND_CHAPTER_TITLES.items():
        assert title.strip() and title != key, key
    flattened = tuple(key for _, keys in BOND_CHAPTER_PARTS for key in keys)
    assert flattened == BOND_CHAPTER_KEYS
    assert set(BOND_CHAPTER_PART) == set(BOND_CHAPTER_KEYS)


def test_only_six_high_risk_bond_chapters_use_llm():
    """直接撰写的模型章节：原 6 个高风险章 **+ governance**。

    修订稿 §2/§3.2 明确要求"现有模型章节及 governance 使用 generation_mode=generate"，
    因此这里从 6 章扩到 7 章——这是**预期变更**，不是放宽校验。
    """
    enabled = {key for key in BOND_CHAPTER_KEYS if chapter_uses_llm(key)}
    assert enabled == {
        "governance",
        "financial", "cashflow", "debt", "rating", "guarantee", "counter_guarantee",
    }


def test_llm_chapters_define_report_narrative_and_concise_citations():
    payload = prompt_config_payload(bond=True)
    for key, config in payload["chapters"].items():
        if not config["llm"]:
            continue
        narrative = config["narrative_contract"]
        assert narrative["tone"] == "professional_due_diligence"
        assert narrative["min_paragraphs"] >= 3
        assert narrative["max_paragraphs"] >= narrative["min_paragraphs"]
        assert config["output_contract"]["max_citations"] <= 3


def test_context_is_scoped_to_rating_sources_and_hash_changes():
    startup()
    sync_fact_assertions("demo-bond-huachen")
    first = build_chapter_context("demo-bond-huachen", "rating", "评级机构认为主体评级AA-")
    assert first["evidence"]
    assert {item["document_kind"] for item in first["evidence"]} <= {"信用评级报告"}
    changed = build_chapter_context("demo-bond-huachen", "rating", "评级机构认为主体评级AA+", [])
    assert first["input_hash"] != changed["input_hash"]


def test_validation_rejects_invalid_citation_new_number_and_forbidden_language():
    context = _context("bond_overview", missing=True)
    result = validate_chapter_output(
        "据募集说明书披露，拟议发行规模为20000万元，建议承保额度为30000万元，材料不足。",
        ["c-does-not-exist"], context,
    )
    assert result["status"] == "rejected"
    assert result["invalid_citations"] == ["c-does-not-exist"]
    assert any("新数字" in reason for reason in result["reasons"])
    assert any("禁止" in reason for reason in result["reasons"])


def test_validation_requires_conflict_and_missing_markers():
    context = _context("financial", missing=True, conflicts=[{"fact_key": "revenue", "period": "2025", "status": "conflicted"}])
    result = validate_chapter_output("据募集说明书披露，材料不足。", ["c-source-1"], context)
    assert result["status"] == "rejected"
    assert any("冲突" in reason for reason in result["reasons"])


def test_chapter_missing_state_ignores_unrelated_task_and_keeps_relevant_task(monkeypatch):
    tasks = [{
        "id": "task-1", "task_type": "material_gap", "title": "补充法律意见",
        "description": "核验重大诉讼", "related_ids": "[]", "required_evidence": "法律意见",
    }]

    def fake_rows(query, _params=()):
        if "FROM facts" in query:
            return [{
                "id": "fact-1", "fact_key": "revenue", "period": "2025", "value": 100,
                "unit": "万元", "text_value": "", "raw_value": "100万元", "review_status": "confirmed",
                "source_ids": '["source-1"]', "name": "营业收入",
            }]
        if "FROM cross_checks" in query or "FROM financial_metrics" in query or "FROM canonical_facts" in query:
            return []
        if "FROM review_tasks" in query:
            return tasks
        return []

    monkeypatch.setattr(chapter_guard, "rows", fake_rows)
    monkeypatch.setattr(chapter_guard, "_source_rows", lambda *_: [{
        "id": "source-1", "document_id": "doc-1", "locator_type": "page", "locator": "第1页",
        "text": "营业收入100万元", "page": 1, "kind": "募集说明书", "filename": "募集说明书.pdf",
    }])
    unrelated = build_chapter_context("project-1", "financial", "据募集说明书披露，营业收入100万元。")
    assert unrelated["has_missing"] is False
    assert unrelated["relevant_review_tasks"] == []

    tasks[0] = {**tasks[0], "title": "补充审计报告", "description": "核验营业收入", "required_evidence": "审计报告"}
    relevant = build_chapter_context("project-1", "financial", "据募集说明书披露，营业收入100万元。")
    assert relevant["has_missing"] is True
    assert [item["id"] for item in relevant["relevant_review_tasks"]] == ["task-1"]


def test_conflict_context_contains_every_assertion_and_deterministic_block(monkeypatch):
    def fake_rows(query, _params=()):
        if "FROM facts" in query or "FROM cross_checks" in query or "FROM financial_metrics" in query or "FROM review_tasks" in query:
            return []
        if "FROM canonical_facts" in query:
            return [{
                "id": "canonical-1", "fact_key": "revenue", "name": "营业收入", "period": "2025",
                "status": "conflicted", "assertion_ids": '["assertion-1","assertion-2"]',
            }]
        if "FROM fact_assertions" in query:
            return [
                {"id": "assertion-1", "value": 100, "unit": "万元", "text_value": "", "raw_value": "100万元", "source_ids": '["source-1"]', "extraction_method": "native", "created_at": "1"},
                {"id": "assertion-2", "value": 120, "unit": "万元", "text_value": "", "raw_value": "120万元", "source_ids": '["source-2"]', "extraction_method": "native", "created_at": "2"},
            ]
        return []

    monkeypatch.setattr(chapter_guard, "rows", fake_rows)
    monkeypatch.setattr(chapter_guard, "_source_rows", lambda *_: [{
        "id": "source-1", "document_id": "doc-1", "locator_type": "page", "locator": "第1页",
        "text": "营业收入100万元", "page": 1, "kind": "募集说明书", "filename": "募集说明书.pdf",
    }])
    context = build_chapter_context("project-1", "financial", "据募集说明书披露，营业收入存在冲突。")
    assert [item["id"] for item in context["conflicts"][0]["assertions"]] == ["assertion-1", "assertion-2"]
    assert "100万元" in context["conflict_block"] and "120万元" in context["conflict_block"]
    assert context["conflict_block"] in context["draft"]


def test_valid_output_passes_with_allowed_number_and_attribution():
    context = _context("bond_overview", missing=True)
    result = validate_chapter_output("据募集说明书披露，拟议发行规模为20000万元；披露信息待核验，材料不足。", ["c-source-1"], context)
    assert result["status"] == "passed"


def test_negated_counter_guarantee_boundary_is_not_forbidden():
    context = _context("counter_guarantee", missing=False)
    result = validate_chapter_output(
        "募集说明书披露拟议安排；未取得权证，不得表述为已具备可执行覆盖。",
        ["c-source-1"], context,
    )
    assert not any("已具备可执行覆盖" in reason for reason in result["reasons"])


def test_narrative_validator_keeps_technical_checks_without_paragraph_limit():
    context = _context("financial", missing=True)
    context["chapter_key"] = "financial"
    context["prompt_config"] = load_chapter_config("financial")
    result = validate_chapter_output(
        "系统识别到，规则状态为 warning。披露信息待核验，材料不足。",
        ["c-source-1"], context,
    )
    assert result["status"] == "rejected"
    assert any("技术化" in reason or "系统术语" in reason for reason in result["reasons"])


def test_narrative_validator_does_not_reject_many_paragraphs():
    context = _context("financial", missing=False)
    context["chapter_key"] = "financial"
    context["prompt_config"] = load_chapter_config("financial")
    content = "\n\n".join(f"第{i}段：据募集说明书披露，材料不足。" for i in range(12))
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any("自然段" in reason for reason in result["reasons"])


def test_entity_check_matches_names_not_the_prose_around_them():
    """改写会改变 ENTITY_RE 抓到的上下文，同一主体不该被判成“新主体”。"""
    context = _context("bond_overview", missing=True)
    context["draft"] = "据募集说明书披露，发行规模为20000万元，发行人华辰精密制造有限公司；材料不足。"
    context["evidence"] = [{
        "id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书",
        "quote": "发行人华辰精密制造有限公司，发行规模20000万元",
    }]
    rewritten = validate_chapter_output(
        "据募集说明书披露，华辰精密制造有限公司系本期债券发行人，拟议发行规模为20000万元；材料不足。",
        ["c-source-1"], context,
    )
    assert not any("新主体" in reason for reason in rewritten["reasons"])

    introduced = validate_chapter_output(
        "据募集说明书披露，华辰精密制造有限公司由华辰新能源有限公司提供担保，拟议发行规模为20000万元；材料不足。",
        ["c-source-1"], context,
    )
    assert any("新主体" in reason for reason in introduced["reasons"])


def test_length_guidance_is_the_user_range_without_a_system_cap():
    """篇幅指引只进 Prompt，而且是**用户设置的目标区间本身**。

    系统不再有独立字符硬顶，因此这里的提示值不会再被第二套上限钳制——
    钳制会逼模型写出必被拒绝的长度，结果反而比目标下限更短。
    """
    guidance = _length_guidance
    narrative = {"preferred_min_chars": 1000, "preferred_max_chars": 3000}
    assert guidance(narrative, {}) == "正文篇幅目标为1000至3000字。"
    # 即便调用方仍传旧的输出契约，也不再改变指引值。
    assert guidance(narrative, {"max_output_chars": 2400}) == "正文篇幅目标为1000至3000字。"
    assert guidance({"preferred_max_chars": 2400}, {}) == "正文篇幅目标不超过2400字。"
    assert guidance({}, {}) == ""


# ------------------------------------------------------------------ 数字校验 --

def _number_context() -> dict:
    context = _context("bond_overview", missing=True)
    context["draft"] = "据募集说明书披露，发行规模为20000万元，资产负债率70%，流动比率1.1；披露信息待核验，材料不足。"
    context["evidence"] = [{
        "id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书",
        "quote": "发行规模20000万元，资产负债率70%，流动比率1.1",
    }]
    return context


_TEMPLATE = "据募集说明书披露，拟议发行规模为{amount}，资产负债率为{ratio}，流动比率{liquidity}；披露信息待核验，材料不足。"


def _number_reasons(content: str, context: dict) -> list[str]:
    result = validate_chapter_output(content, ["c-source-1"], context)
    return [reason for reason in result["reasons"] if "新数字" in reason]


def test_number_check_normalizes_formatting_but_keeps_units():
    context = _number_context()
    assert _number_reasons(_TEMPLATE.format(amount="20000万元", ratio="70%", liquidity="1.1"), context) == []
    # 千分位与小数尾零是同一数值
    assert _number_reasons(_TEMPLATE.format(amount="20,000万元", ratio="70.0%", liquidity="1.10"), context) == []
    # 真正新增的数值仍然拒绝
    assert _number_reasons(_TEMPLATE.format(amount="2万元", ratio="70%", liquidity="1.1"), context)
    assert _number_reasons(_TEMPLATE.format(amount="20000万元", ratio="75%", liquidity="1.1"), context)
    # 等值量级换算允许（20000万元 = 2亿元），换算后不等的仍拒绝
    assert _number_reasons(_TEMPLATE.format(amount="2亿元", ratio="70%", liquidity="1.1"), context) == []
    assert _number_reasons(_TEMPLATE.format(amount="2.5亿元", ratio="70%", liquidity="1.1"), context)


def test_number_check_ignores_heading_list_and_alias_ordinals():
    context = _number_context()
    content = (
        "### 1. 财务概览\n\n"
        "据募集说明书披露，拟议发行规模为20000万元；披露信息待核验，材料不足。\n\n"
        "### 2. 财务结构\n\n- 引用来源 E01 与 E02。"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not [reason for reason in result["reasons"] if "新数字" in reason]
    assert {"1", "2"}.issubset(set(result["ignored_format_numbers"]))


def test_number_diagnostics_are_reported_without_model_text():
    context = _number_context()
    result = validate_chapter_output(_TEMPLATE.format(amount="2万元", ratio="70%", liquidity="1.1"), ["c-source-1"], context)
    assert result["status"] == "rejected"
    entry = result["unexpected_numbers"][0]
    assert {key: entry[key] for key in ("raw", "normalized_value", "normalized_unit", "reason")} == {
        "raw": "2万元", "normalized_value": "2", "normalized_unit": "万元", "reason": "new_numeric_value",
    }
    # 只带命中处的一小段上下文，不携带整篇被拒正文
    assert "2万元" in entry["context"] and len(entry["context"]) < 60
    assert "content" not in result


# ---------------------------------------------------------------- 禁止表达 --

def _forbidden_reasons(content: str, key: str = "debt") -> list[str]:
    context = _context(key, missing=True)
    result = validate_chapter_output(content, ["c-source-1"], context)
    return [reason for reason in result["reasons"] if "禁止" in reason]


def test_negated_boundary_statements_are_not_forbidden_matches():
    """“不得/不应/不构成…”是合规边界表述，不能判成禁止表达。"""
    assert _forbidden_reasons("据募集说明书披露，本报告不构成投资建议；材料不足。") == []
    assert _forbidden_reasons("据募集说明书披露，不得推断续融资可行性或授信获批概率；材料不足。") == []
    assert _forbidden_reasons("据募集说明书披露，不得表述为已具备可执行覆盖；材料不足。", key="counter_guarantee") == []


def test_affirmative_recommendations_are_still_rejected():
    assert _forbidden_reasons("据募集说明书披露，建议投资本期债券；材料不足。")
    assert _forbidden_reasons("据募集说明书披露，预计授信能够获批；材料不足。")
    assert _forbidden_reasons("据募集说明书披露，授信获批概率较高；材料不足。")
    # “不排除”是hedge，不得当作否定语境豁免
    assert _forbidden_reasons("据募集说明书披露，不排除建议投资本期债券；材料不足。")


def test_forbidden_diagnostics_report_the_matched_pattern():
    context = _context("debt", missing=True)
    result = validate_chapter_output("据募集说明书披露，授信获批概率较高；材料不足。", ["c-source-1"], context)
    assert result["status"] == "rejected"
    assert any(item["term"] == "授信获批" and item["classification"] == "forbidden"
               for item in result["forbidden_matches"])


def test_source_attribution_block_is_required_restorable_and_not_counted():
    """来源归属改为确定性保留块：模型漏写由系统补回，不再要求复述固定措辞。"""
    from backend.app.chapter_guard import SOURCE_ATTRIBUTION_BLOCK, count_report_chars
    from backend.app.workflow import _attribution_block_missing, _preserve_chapter_boundaries

    context = _context("cashflow", missing=True)
    context["origin_types"] = ["profile_snapshot"]
    context["source_attribution_block"] = SOURCE_ATTRIBUTION_BLOCK
    body = "据募集说明书披露，发行规模为20000万元；披露信息待核验，材料不足。"

    # 模型漏写来源块 → 判缺失（且不再因为缺少“冻结主体档案”字样而拒绝）
    missing = validate_chapter_output(body, ["c-source-1"], context)
    assert any("来源边界块" in reason for reason in missing["reasons"])
    assert not any("冻结快照来源归属" in reason for reason in missing["reasons"])

    # 系统补回后重新校验通过
    assert _attribution_block_missing(body, context) is True
    restored_content = _preserve_chapter_boundaries(body, context)
    assert SOURCE_ATTRIBUTION_BLOCK in restored_content
    restored = validate_chapter_output(restored_content, ["c-source-1"], context)
    assert not any("来源边界块" in reason for reason in restored["reasons"])
    assert _attribution_block_missing(restored_content, context) is False

    # 来源块不计入用户配置的正文篇幅
    assert count_report_chars(restored_content) == count_report_chars(body)


def test_allowed_periods_keep_complete_years_beside_future_maturity_years():
    """未来到期年份不得把合法完整年度挤出允许集（债务章的 2026 与 2023）。"""
    from backend.app.chapter_guard import _allowed_periods

    fact_years = ["2023", "2024", "2025", "2026"]
    evidence = [{"quote": "本期债券 2026 年到期，2023 年末有息债务 12000万元"}]
    allowed = _allowed_periods(fact_years, evidence, current_year=2026)
    assert {"2023", "2024", "2025"} <= set(allowed)   # 三个完整年度
    assert "2026" in allowed                          # 材料里出现的未来年度
    # 材料里没有的年度仍然不允许
    assert "2019" not in allowed


def test_fallback_keeps_validation_diagnostics():
    """回退时不能丢掉诊断：否则界面上只剩一句笼统的“新数字”。"""
    from backend.app.chapter_guard import validation_diagnostics

    rejected = {
        "status": "rejected",
        "reasons": ["章节包含确定性草稿和证据之外的新数字"],
        "unexpected_numbers": [{"raw": "3", "normalized": "3", "unit": "", "location_type": "body"}],
        "ignored_format_numbers": ["1"],
        "forbidden_matches": [],
    }
    carried = {"status": "fallback", "reasons": ["..."], **validation_diagnostics(rejected)}
    assert carried["unexpected_numbers"] == rejected["unexpected_numbers"]
    assert validation_diagnostics(carried)["ignored_format_numbers"] == ["1"]


def test_number_check_normalizes_unit_synonyms():
    """同一单位的常见写法视为同一个数；不同量级不合并。"""
    context = _number_context()
    context["draft"] = "据募集说明书披露，发行规模为12000万元，资产负债率70%；披露信息待核验，材料不足。"
    context["evidence"] = [{
        "id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书",
        "quote": "发行规模12000万元",
    }]
    template = "据募集说明书披露，拟议发行规模为{amount}；披露信息待核验，材料不足。"
    assert _number_reasons(template.format(amount="12000万"), context) == []
    assert _number_reasons(template.format(amount="12000万元"), context) == []
    assert _number_reasons(template.format(amount="1.2亿元"), context) == []      # 等值换算
    assert _number_reasons(template.format(amount="1.3亿元"), context)            # 换算后不等 → 仍拒绝


def test_number_check_accepts_system_metric_quoted_with_unit():
    """结构化指标 value 与 unit 是两个字段；模型照抄成 "9.8901%" 不能判成新数字。"""
    context = _context("financial", missing=True)
    context["draft"] = "据募集说明书披露，材料不足。"
    context["evidence"] = [{"id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书", "quote": "无"}]
    context["metrics"] = [{"metric_key": "revenue_growth", "period": "2025", "value": 9.8901,
                           "unit": "%", "status": "calculated"}]

    ok = validate_chapter_output("据募集说明书披露，营业收入增长率为9.8901%；材料不足。", ["c-source-1"], context)
    assert not [reason for reason in ok["reasons"] if "新数字" in reason]

    bad = validate_chapter_output("据募集说明书披露，营业收入增长率为12.3456%；材料不足。", ["c-source-1"], context)
    assert [reason for reason in bad["reasons"] if "新数字" in reason]


def test_four_digit_amount_is_not_read_as_a_year():
    from backend.app.chapter_guard import _year_mentions

    assert _year_mentions("应收账款1939万元") == set()
    assert _year_mentions("25.0%") == set()
    assert _year_mentions("2025年末应收账款") == {"2025"}


def test_number_check_accepts_two_decimal_rounding_of_metrics():
    """系统指标存 4 位小数，报告按 2 位小数引用是惯例，不能判成新数字。"""
    context = _number_context()
    context["draft"] = "据募集说明书披露，材料不足。"
    context["evidence"] = [{"id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书", "quote": "无"}]
    context["metrics"] = [
        {"metric_key": "revenue_growth", "period": "2025", "value": 9.8901, "unit": "%", "status": "calculated"},
        {"metric_key": "interest_bearing_debt_capitalization", "period": "2025", "value": 58.9354, "unit": "%", "status": "calculated"},
    ]
    ok = validate_chapter_output(
        "据募集说明书披露，营业收入增速为9.89%，有息负债资本化比率为58.94%；材料不足。", ["c-source-1"], context)
    assert not [reason for reason in ok["reasons"] if "新数字" in reason]
    # 四舍五入后仍不同的数值继续拒绝
    bad = validate_chapter_output(
        "据募集说明书披露，营业收入增速为9.79%；材料不足。", ["c-source-1"], context)
    assert [reason for reason in bad["reasons"] if "新数字" in reason]


def test_negated_scope_statement_is_not_called_scope_mixing():
    """“不采用母公司口径”是边界表述，不算混入口径。"""
    context = _context("financial", missing=True)
    context["chapter_key"] = "financial"      # 口径检查只对财务/现金流/债务章启用
    context["selected_scope"] = "consolidated"
    context["draft"] = "据募集说明书披露，材料不足。"
    ok = validate_chapter_output(
        "据募集说明书披露，本章统一采用合并口径，不采用母公司口径；材料不足。", ["c-source-1"], context)
    assert not [reason for reason in ok["reasons"] if "口径" in reason]
    bad = validate_chapter_output(
        "据募集说明书披露，母公司口径下资产负债率为65%；材料不足。", ["c-source-1"], context)
    assert [reason for reason in bad["reasons"] if "口径" in reason]


def test_scope_vocabulary_is_not_treated_as_a_new_entity():
    context = _context("financial", missing=True)
    result = validate_chapter_output(
        "据募集说明书披露，不得混用半年度或母公司口径；材料不足。", ["c-source-1"], context)
    assert not [reason for reason in result["reasons"] if "新主体" in reason]


def test_equivalent_unit_conversion_is_allowed_and_recorded():
    """20000万元 与 2亿元 是同一个数：允许，并记为 safe_unit_conversion。"""
    context = _number_context()
    result = validate_chapter_output(
        "据募集说明书披露，拟议发行规模为2亿元；披露信息待核验，材料不足。", ["c-source-1"], context)
    assert not [reason for reason in result["reasons"] if "新数字" in reason]
    assert result["safe_unit_conversions"][0]["reason"] == "safe_unit_conversion"
    assert result["safe_unit_conversions"][0]["normalized_unit"] == "万元"


def test_page_references_are_not_business_numbers():
    """页码引用（第 28、31、32 页）不是业务数字。"""
    context = _number_context()
    result = validate_chapter_output(
        "据募集说明书披露，拟议发行规模为20000万元（详见第 28、31、32 页）；披露信息待核验，材料不足。",
        ["c-source-1"], context)
    assert not [reason for reason in result["reasons"] if "新数字" in reason]
    assert {"28", "31", "32"} <= set(result["page_numbers_ignored"])


def test_safe_negative_expression_is_recorded_but_does_not_reject():
    context = _context("rating", missing=True)
    result = validate_chapter_output(
        "据募集说明书披露，本报告不构成投资建议；材料不足。", ["c-source-1"], context)
    assert not [reason for reason in result["reasons"] if "禁止" in reason]
    assert any(item["classification"] == "safe_negative" and item["term"] == "投资建议"
               for item in result["forbidden_matches"])


def test_conflict_assertion_renders_missing_unit(monkeypatch):
    """抽取出的 raw_value 常常没单位；冲突块必须补上，否则模型照材料写带单位的值会被判新增数字。"""
    def fake_rows(query, _params=()):
        if "FROM facts" in query or "FROM cross_checks" in query or "FROM financial_metrics" in query or "FROM review_tasks" in query:
            return []
        if "FROM canonical_facts" in query:
            return [{"id": "canonical-1", "fact_key": "operating_cash_flow", "name": "经营现金流净额",
                     "period": "2025", "status": "conflicted", "assertion_ids": '["assertion-1"]'}]
        if "FROM fact_assertions" in query:
            return [{"id": "assertion-1", "value": 9000.0, "unit": "万元", "text_value": "",
                     "raw_value": "9000.0", "source_ids": '["source-1"]',
                     "extraction_method": "native", "created_at": "1"}]
        return []

    monkeypatch.setattr(chapter_guard, "rows", fake_rows)
    monkeypatch.setattr(chapter_guard, "_source_rows", lambda *_: [])
    context = build_chapter_context("project-1", "cashflow", "据募集说明书披露，经营活动现金流存在冲突。")
    assert "9000.0万元" in context["conflict_block"]

    context["chapter_key"] = "cashflow"
    context["evidence"] = []
    result = validate_chapter_output(
        "据募集说明书披露，" + context["conflict_block"] + "\n另两处为9,000万元，上述差异应并列保留。",
        [], context)
    assert not [reason for reason in result["reasons"] if "新数字" in reason]


def test_negation_governs_a_whole_enumeration():
    """“不输出 A、B、C…投资建议”里，否定词管着整个枚举列表。"""
    context = _context("rating", missing=True)
    enumerated = "据评级报告披露，本复核不输出承保、拒保、担保额度、担保费率、债券评级或投资建议，亦不将评级观点作为担保判断依据；材料不足。"
    result = validate_chapter_output(enumerated, ["c-source-1"], context)
    assert not [reason for reason in result["reasons"] if "禁止" in reason]
    assert all(item["classification"] == "safe_negative"
               for item in result["forbidden_matches"] if item["term"] in {"担保额度", "担保费率", "投资建议"})


def test_affirmative_mention_after_a_negated_sentence_is_still_rejected():
    """上一句的否定不能豁免下一句的肯定表述。"""
    context = _context("rating", missing=True)
    content = "据评级报告披露，本章不输出担保额度。担保额度为3亿元，投资建议为买入；材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert [reason for reason in result["reasons"] if "禁止" in reason]


def test_required_phrases_are_rendered_into_both_compilers():
    """校验器要求逐字出现的表述，必须同时进入出厂与结构化两条 Prompt 编译路径。"""
    from backend.app.chapter_prompts import (
        chapter_system_prompt, compile_structured_prompt, editable_config_from_factory, prompt_edit_state,
    )

    phrases = load_chapter_config("rating")["output_contract"]["required_phrases"]
    assert phrases, "rating 应声明必需表述，否则本测试失去意义"
    for prompt in (chapter_system_prompt("rating"), compile_structured_prompt("rating", editable_config_from_factory("rating"))):
        assert "必须出现以下表述：" in prompt
        for phrase in phrases:
            assert phrase in prompt


def test_number_and_unit_may_be_separated_by_a_space():
    """“30,000 万元” 与材料里的 “30000万元” 是同一个数。"""
    context = _number_context()
    context["draft"] = "据募集说明书披露，注册规模为30000万元；披露信息待核验，材料不足。"
    context["evidence"] = [{"id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书", "quote": "注册规模30000万元"}]
    ok = validate_chapter_output(
        "据募集说明书披露，注册规模为 30,000 万元；披露信息待核验，材料不足。", ["c-source-1"], context)
    assert not [reason for reason in ok["reasons"] if "新数字" in reason]


def test_rule_thresholds_from_check_text_are_allowed():
    """规则阈值（cross_checks.thresholds）不带单位，但 formula/summary 里写了“50%”。"""
    context = _context("debt", missing=True)
    context["draft"] = "据募集说明书披露，材料不足。"
    context["evidence"] = [{"id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书", "quote": "无"}]
    context["cross_checks"] = [{
        "rule_key": "leverage_liquidity", "status": "warning", "title": "杠杆与流动性",
        "summary": "有息负债/总资产 66.1%，超过 50% 的关注阈值",
        "formula": "有息负债/总资产 > 50%", "thresholds": {"debt_ratio": 50}, "values": [],
    }]
    ok = validate_chapter_output(
        "据募集说明书披露，有息负债66.1%，超过50%的关注阈值；材料不足。", ["c-source-1"], context)
    assert not [reason for reason in ok["reasons"] if "新数字" in reason]


def test_rule_threshold_values_get_units_from_their_keys():
    """阈值以裸数字存在（due_12m_pct: 50.0），单位在键名里——模型写“50%”必须能匹配。"""
    context = _context("debt", missing=True)
    context["draft"] = "据募集说明书披露，材料不足。"
    context["evidence"] = [{"id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书", "quote": "无"}]
    context["cross_checks"] = [{
        "rule_key": "debt_maturity", "status": "warning", "title": "债务到期结构",
        "summary": "一年内到期债务占有息负债66.1%。", "formula": "一年内到期债务/有息负债",
        "thresholds": {"due_12m_pct": 50.0}, "values": [],
    }]
    ok = validate_chapter_output(
        "据募集说明书披露，一年内到期债务占有息负债66.1%，超过50%的关注阈值；材料不足。",
        ["c-source-1"], context)
    assert not [reason for reason in ok["reasons"] if "新数字" in reason]
    # 材料里没有的阈值仍然拒绝
    bad = validate_chapter_output(
        "据募集说明书披露，该比例超过80%的关注阈值；材料不足。", ["c-source-1"], context)
    assert [reason for reason in bad["reasons"] if "新数字" in reason]


# ------------------------------------------------------------ 证据选取排序 --

def _src(sid: str, page: int, text: str) -> dict:
    return {"id": sid, "document_id": "doc-1", "kind": "募集说明书", "filename": "f.pdf",
            "locator": f"第{page}页", "page": page, "text": text, "origin_type": "document"}


def test_evidence_selection_prioritises_chapter_fact_sources():
    """该章事实引用的来源页必须优先进入证据，不能被页码顺序挤掉。"""
    from backend.app.chapter_guard import select_evidence

    sources = [_src(f"p{page}", page, f"第{page}页的一般性描述文本") for page in range(1, 12)]
    sources.append(_src("key-page", 40, "预测经营现金流与偿债安排的具体内容"))
    selected = select_evidence(sources, {"key-page"}, "经营现金流 偿债安排", max_items=4, max_chars=100000)

    assert "key-page" in {item["source_id"] for item in selected}
    assert selected[0]["source_id"] == "key-page"      # 优先项排在第一


def test_evidence_selection_uses_relevance_for_the_remaining_slots():
    """非优先项按相关度取，而不是按页码取。"""
    from backend.app.chapter_guard import select_evidence

    sources = [_src(f"early{page}", page, "目录与声明等无关内容") for page in (1, 2, 3)]
    sources += [_src("late-relevant", 50, "募集资金专户与用途闭环核验安排")]
    selected = select_evidence(sources, set(), "募集资金专户 用途闭环", max_items=2, max_chars=100000)

    assert "late-relevant" in {item["source_id"] for item in selected}


def test_evidence_selection_caps_items_per_page_but_still_fills_the_budget():
    """同一页最多两条，且不会因为限流而取不满预算。"""
    from backend.app.chapter_guard import select_evidence

    sources = [_src(f"p1-{i}", 1, f"第一页片段{i}") for i in range(4)] + [_src("p2", 2, "第二页片段")]
    selected = select_evidence(sources, set(), "", max_items=3, max_chars=100000)
    ids = [item["source_id"] for item in selected]

    assert len(ids) == 3
    assert sum(1 for sid in ids if sid.startswith("p1-")) <= 2
    assert "p2" in ids


def test_evidence_selection_respects_char_budget():
    from backend.app.chapter_guard import select_evidence

    sources = [_src("a", 1, "甲" * 900), _src("b", 2, "乙" * 900), _src("c", 3, "丙" * 900)]
    selected = select_evidence(sources, set(), "", max_items=10, max_chars=1000)
    assert len(selected) == 1          # 首条允许超预算，之后必须受限


def test_decision_words_are_judged_by_context_not_by_bare_word():
    """"担保额度/担保费率"按**完整语境**判定，不按裸词拦（修订稿 §3.1）。

    三类必须分开：① 我们方做出担保决策 → 拒绝；② 否定声明 → 放行；③ 引用材料原文 → 放行。
    描述性提及（"反担保覆盖担保额度"）同样放行——描述责任上限不是越权结论。
    这条同时守住"**不能用固定窗口共现替代语义边界**"：判定顺序是 否定 → 引用 → 决策动词。
    """
    from backend.app.chapter_guard import DEFAULT_FORBIDDEN, _decision_classification

    # 裸词必须已经不在默认禁词表里，否则上下文判定形同虚设。
    assert "担保额度" not in DEFAULT_FORBIDDEN
    assert "担保费率" not in DEFAULT_FORBIDDEN

    def classify(text: str) -> str:
        term = "担保额度" if "担保额度" in text else "担保费率"
        return _decision_classification(text, text.index(term))

    # ① 肯定决策 → 拒绝
    assert classify("本章建议担保额度为2亿元，用于覆盖本期债券。") == "forbidden"
    assert classify("核定担保费率为1.2%，按年收取。") == "forbidden"
    # ② 否定声明 → 放行
    assert classify("不得输出承保、拒保、担保额度、担保费率或投资建议。") == "safe_negative"
    assert classify("本报告不构成投资建议，也不建议设定担保额度。") == "safe_negative"
    # ③ 引用材料原文 → 放行
    assert classify("据募集说明书载明，担保额度为2亿元，保证合同另有约定。") == "safe_quote"
    assert classify("材料披露的担保费率为1.2%。") == "safe_quote"
    # ④ 描述性提及 → 放行
    assert classify("反担保覆盖担保额度，覆盖倍数约1.2倍。") == "safe_description"
