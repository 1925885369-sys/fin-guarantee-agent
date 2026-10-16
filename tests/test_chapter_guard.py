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


def test_only_expected_bond_chapters_use_llm():
    """直接撰写的模型章节：原 6 个高风险章 **+ governance + operations + proceeds**。

    修订稿 §2/§3.2 要求"现有模型章节及 governance 使用 generation_mode=generate"，
    本次 `PLAN_new_p0_p1.md` P0-2 / `PLAN.md` P0-B 再把第一部分 operations、proceeds
    一并切为模型直接撰写，因此从 7 章扩到 9 章——这是**预期变更**，不是放宽校验。
    """
    enabled = {key for key in BOND_CHAPTER_KEYS if chapter_uses_llm(key)}
    assert enabled == {
        "governance", "operations", "proceeds",
        "financial", "cashflow", "debt", "rating", "guarantee", "counter_guarantee",
    }


def test_first_part_chapters_use_generate_mode_and_narrative_contract():
    """P0-2/P0-B：第一部分三章（governance/operations/proceeds）均为直接撰写模式。

    `generation_mode: generate` 才让请求体不带确定性草稿；narrative_contract 给出
    篇幅目标（2000–4000 字符）与行文约束，且按要求**不写 paragraph_order**。
    """
    payload = prompt_config_payload(bond=True)
    for key in ("governance", "operations", "proceeds"):
        config = payload["chapters"][key]
        assert config["llm"] is True, key
        assert config["generation_mode"] == "generate", key
        narrative = config["narrative_contract"]
        assert narrative["preferred_min_chars"] == 2000 and narrative["preferred_max_chars"] == 4000, key
        assert narrative["tone"] == "professional_due_diligence", key
        assert not narrative["paragraph_order"], f"{key} 不应强制正文顺序"


def test_no_required_phrase_demands_the_legacy_source_wording():
    """P0-3 回归：任何章节的成品契约都不得再要求逐字输出旧称"募集说明书披露"。

    成品旧称由服务端在写入时统一规范化（delivery_wording），模型侧与成品契约均
    不应再把它列为 required_phrases——否则模型被逼逐字写旧称，成品又得改回去。
    """
    payload = prompt_config_payload(bond=True)
    offenders = [
        (key, phrase)
        for key, config in payload["chapters"].items()
        for phrase in config["output_contract"]["required_phrases"]
        if "募集说明书" in phrase
    ]
    assert offenders == []



def test_llm_chapters_define_report_narrative_and_concise_citations():
    payload = prompt_config_payload(bond=True)
    for key, config in payload["chapters"].items():
        if not config["llm"]:
            continue
        narrative = config["narrative_contract"]
        assert narrative["tone"] == "professional_due_diligence"
        assert narrative["min_paragraphs"] >= 3
        assert narrative["max_paragraphs"] >= narrative["min_paragraphs"]
        # 引用仍然必须**有界**。财务章是计划 §二 的试点：上限 3 → 8（财务引用处多，
        # 3 条会把合法引用截断成"论据不全"）；其余模型章维持 ≤3 的精简引用契约。
        limit = int(config["output_contract"]["max_citations"])
        assert limit <= 8
        if key == "financial":
            assert limit == 8, "财务章试点引用上限：8 条"
        else:
            assert limit <= 3


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
    """文风检查保留为**写作提示**，但不再触发整章回退（计划 §二 line 17）。

    计划明文：「文风、开头和模糊术语检查改为写作提示，**不触发整章回退**；
    事实真实性、合法引用及禁止审批结论继续强制校验」。技术化起句与系统术语仍然被识别、
    记进 `style_notes`（供人工与接口查看），只是**不进 `reasons`**——否则一句
    "系统识别到"起句就会把整章的模型分析丢掉。
    """
    context = _context("financial", missing=True)
    context["chapter_key"] = "financial"
    context["prompt_config"] = load_chapter_config("financial")
    result = validate_chapter_output(
        "系统识别到，规则状态为 warning。披露信息待核验，材料不足。",
        ["c-source-1"], context,
    )
    # 内容本身合规（必填表述齐全、无新数字/无效引用）→ 不得因文风被拒
    assert result["status"] != "rejected", result["reasons"]
    assert not any("技术化" in reason or "系统术语" in reason for reason in result["reasons"])
    # 但提示要看得见：至少命中技术化起句与系统术语（warning / 系统识别到）
    notes = result["style_notes"]
    assert any("技术化" in item for item in notes), notes
    assert any("系统术语" in item for item in notes), notes


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


def test_scope_prohibition_replaced_by_record_consistency():
    """**预期变更**：口径/期间改为"与记录一致"，移除"混入非采用口径一律禁止"。

    细化方案 §二.2：中期/另一口径各自有记录支持即可分别引用（由四维归属核对承担）；
    仅当项目 Prompt **明确限定**口径范围时才硬拦。这里验证：
    - 否定/边界表述仍放行；
    - "另一口径"不再被一律拦——无记录支持的值按"新数字"拒绝；
    - 同一口径有记录支持时可正常通过。
    """
    context = _context("financial", missing=True)
    context["chapter_key"] = "financial"
    context["selected_scope"] = "consolidated"
    context["draft"] = "据募集说明书披露，材料不足。"
    # 否定/边界表述依旧放行
    ok = validate_chapter_output(
        "据募集说明书披露，本章统一采用合并口径，不采用母公司口径；材料不足。", ["c-source-1"], context)
    assert not [reason for reason in ok["reasons"] if "口径" in reason]
    # "另一口径"本身不再被一律拦；无记录支持的值按新数字拒绝（"无记录即拒"）
    bad = validate_chapter_output(
        "据募集说明书披露，母公司口径下资产负债率为65%；材料不足。", ["c-source-1"], context)
    assert [reason for reason in bad["reasons"] if "新数字" in reason]
    assert not [reason for reason in bad["reasons"] if "混入" in reason]
    # 有记录支持时，另一口径可以分别引用（"有记录即过"）
    context["table_records"] = [{
        "value": "65", "unit": "%", "label": "资产负债率", "period": "2025",
        "subject": "华辰精密制造有限公司", "scope": "母公司口径",
        "source_ids": ["s-1"], "page": 1,
    }]
    context["allowed_periods"] = ["2025"]
    good = validate_chapter_output(
        "据募集说明书披露，母公司口径下2025年资产负债率为65%；材料不足。", ["c-source-1"], context)
    assert not [reason for reason in good["reasons"] if "口径" in reason]
    assert not [reason for reason in good["reasons"] if "新数字" in reason]


def test_project_prompt_can_still_restrict_scope_explicitly():
    """项目 Prompt 明确限定口径范围时仍遵守（细化方案 §二.2 末句）。"""
    context = _context("financial", missing=True)
    context["chapter_key"] = "financial"
    context["prompt_config"] = {**load_chapter_config("financial"),
                                "rules": ["本章仅采用合并口径分析。"]}
    context["draft"] = "据募集说明书披露，材料不足。"
    result = validate_chapter_output(
        "据募集说明书披露，母公司口径下资产负债率为70%；材料不足。", ["c-source-1"], context)
    assert any("限定" in reason for reason in result["reasons"])


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


def _attribution_context(records: list[dict]) -> dict:
    """归属核对测试上下文：debt 章 + 受控表格记录（记录同时充当允许数字的来源）。"""
    context = _context("debt", missing=True)
    context["chapter_key"] = "debt"
    context["allowed_periods"] = ["2023", "2024", "2025"]
    context["table_records"] = records
    return context


def _attribution_record(value: float, **overrides) -> dict:
    record = {
        "value": value, "unit": "万元", "label": "应付债券", "period": "2024",
        "subject": "华辰精密制造有限公司", "scope": "合并口径",
        "source_ids": ["s-1"], "page": 1,
    }
    record.update(overrides)
    return record


def _attribution_codes(sentence: str, context: dict) -> set[str]:
    """跑一次校验，返回四维归属不匹配的维度集合。"""
    result = validate_chapter_output(
        f"据募集说明书披露，{sentence}；材料不足。", ["c-source-1"], context)
    mapping = {
        "所写科目不匹配": "label", "所写期间不匹配": "period",
        "所写主体不匹配": "party", "所写口径不匹配": "scope",
    }
    return {code for text, code in mapping.items() if any(text in reason for reason in result["reasons"])}


def test_number_attribution_triggers_only_on_real_mismatches():
    """数值和单位相同仍需核对**科目/期间/主体/口径**（修订稿 §3.1）。

    只在"数字确实是某条记录的值、而正文把它写在另一个归属下"时报；归属不明
    （比较基准年、并列年份、否定表述、代词片段）不报——那属于待核验，不属于错误。
    """
    context = _attribution_context([
        _attribution_record(100, period="2024", scope="母公司口径"),
        _attribution_record(200, period="2025"),
        _attribution_record(300, label="经营活动现金流量净额", period="2025"),
        _attribution_record(400, label="投资活动现金流量净额", period="2025"),
    ])
    # 科目：把经营现金流的值写成投资现金流
    assert _attribution_codes("投资活动现金流量净额为300万元", context) == {"label"}
    assert _attribution_codes("经营活动现金流量净额为300万元", context) == set()
    # 期间：2024 年末值写成 2025 年末
    assert _attribution_codes("应付债券2025年末为100万元", context) == {"period"}
    assert _attribution_codes("应付债券2024年末为100万元", context) == set()
    # 比较基准年（"较2024年末"）与并列年份不是数字所属期间 → 不判
    assert _attribution_codes("应付债券较2024年末增加200万元", context) == set()
    assert _attribution_codes("应付债券在2024年末和2025年末均为100万元", context) == set()
    # 口径：母公司口径值写在合并口径下；"不采用母公司口径"是边界表述
    assert _attribution_codes("合并口径下应付债券为100万元", context) == {"scope"}
    assert _attribution_codes("公司不采用母公司口径，应付债券为100万元", context) == set()
    # 主体：他公司名要拦；代词片段（"报告期内该公司"）不判
    assert _attribution_codes("华辰新能源有限公司应付债券为100万元", context) == {"party"}
    assert _attribution_codes("华辰精密制造有限公司应付债券为100万元", context) == set()
    assert _attribution_codes("报告期内该公司应付债券为100万元", context) == set()


def test_written_period_scope_is_the_clause():
    """期间归属只看数字所在**分句**：前一数字的年份不得泄漏到本数字。

    实测句式："营业收入由2023年度的48,000万元增至2025年度的52,000万元，净利润由
    2,296万元增至2,800万元"——2,296 的年份由并列结构决定，前缀窗口若扫到 2025 会把
    2023 的正确归属误判成错期间。
    """
    from backend.app.chapter_guard import _written_period

    assert _written_period("应付债券2025年末为") == "2025"
    assert _written_period("…增至2025年度的52,000万元，净利润由") is None
    assert _written_period("净利润由") is None
    assert _written_period("应付债券较2024年末增加") is None


def test_entity_extraction_is_shared_and_filters_prose_fragments():
    """主体名只认**法定名称**结尾的片段；裸「公司」是散文里的普通名词（修订稿 §3.1）。"""
    from backend.app.chapter_guard import _extract_entities

    # 报告口径/期间词汇、代词型片段、时间状语——裸「公司」结尾一律不提取
    assert _extract_entities("不得混用半年度或母公司口径") == set()
    assert _extract_entities("报告期内该公司应付债券") == set()
    assert _extract_entities("其子公司") == set()
    # 真实验收两轮累计 13 个行文片段样本：措辞层出不穷，靠法定结尾整体排除
    assert _extract_entities("2024年以来公司营业收入保持增长") == set()
    assert _extract_entities("2025年末公司资产总额为100万元") == set()
    assert _extract_entities("评级机构认为公司信用状况良好") == set()
    assert _extract_entities("本章未对公司重大事项发表意见") == set()
    assert _extract_entities("其分析时将公司纳入合并范围") == set()
    assert _extract_entities("上述安排有利于公司与下级公司") == set()
    assert _extract_entities("关于发行人公司的说明") == set()
    assert _extract_entities("募集说明书所载公司信息") == set()
    # 非注册约定俗成的简称（"京投公司"）同样不提取——由登记表关系承担，不由本规则兜底
    assert _extract_entities("京投公司营业收入") == set()
    # 嵌着真实主体名的长片段不过滤——否则会把真正新增主体一起漏掉
    assert _extract_entities("该公司系华辰精密制造有限公司") == {"该公司系华辰精密制造有限公司"}
    assert _extract_entities("本公司认为华辰新能源有限公司") == {"本公司认为华辰新能源有限公司"}
    assert _extract_entities("华辰新能源有限公司") == {"华辰新能源有限公司"}
    # 「有限责任公司」不以「有限公司」结尾，必须显式收录
    assert _extract_entities("中诚信国际信用评级有限责任公司") == {"中诚信国际信用评级有限责任公司"}


def test_page_references_are_not_business_numbers():
    """页码引用（第 28、31、32 页）不是业务数字。"""
    context = _number_context()
    result = validate_chapter_output(
        "据募集说明书披露，拟议发行规模为20000万元（详见第 28、31、32 页）；披露信息待核验，材料不足。",
        ["c-source-1"], context)
    assert not [reason for reason in result["reasons"] if "新数字" in reason]
    assert {"28", "31", "32"} <= set(result["page_numbers_ignored"])


def test_split_unit_and_bare_number_evidence_do_not_false_reject():
    """证据侧单位被换行/表格行吃掉时，同值带单位写法不得判"新数字"（真实验收实测）。"""
    context = _number_context()
    # ① 数字与单位在证据里被换行断开（"增加 101.71\\n亿元"）→ 允许侧应合成同值同单位
    context["evidence"] = [{
        "id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书",
        "quote": "有息负债较上年末增加101.71\n亿元；披露信息待核验，材料不足。",
    }]
    result = validate_chapter_output(
        "据募集说明书披露，有息负债较上年末增加101.71亿元。", ["c-source-1"], context)
    assert not [reason for reason in result["reasons"] if "新数字" in reason]

    # ② 证据里只有裸数字（表格行原文），正文写全单位 → 数值级认账
    context["evidence"] = [{
        "id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书",
        "quote": "北京京港地铁有限公司 技术服务 182.08 - | 资源服务支出 138.42",
    }]
    result = validate_chapter_output(
        "据募集说明书披露，技术服务费为182.08万元。", ["c-source-1"], context)
    assert not [reason for reason in result["reasons"] if "新数字" in reason]

    # ③ 负例：证据有带单位形态时兜底不生效——等值换算放行，错误量级仍拒绝
    context["evidence"] = [{
        "id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书",
        "quote": "发行规模20000万元",
    }]
    assert not [reason for reason in validate_chapter_output(
        "据募集说明书披露，拟议发行规模为2亿元。", ["c-source-1"], context)["reasons"]
        if "新数字" in reason]
    assert [reason for reason in validate_chapter_output(
        "据募集说明书披露，拟议发行规模为2000万元。", ["c-source-1"], context)["reasons"]
        if "新数字" in reason]


def test_date_literals_are_not_business_numbers():
    """债券条款里的完整日期（2029-07-16 / 2036年7月16日）不是业务数字。"""
    context = _number_context()
    result = validate_chapter_output(
        "据募集说明书披露，本期债券兑付日期为2029-07-16，另一品种兑付日期为2036年7月16日；材料不足。",
        ["c-source-1"], context)
    assert not [reason for reason in result["reasons"] if "新数字" in reason]


def test_loss_word_and_negative_sign_are_equivalent():
    """材料"亏损118.17亿元"与正文"-118.17亿元"是等价表述（中邮实测）。"""
    context = _number_context()
    context["evidence"] = [{
        "id": "c-source-1", "source_id": "source-1", "document_kind": "信用评级报告",
        "quote": "综上，母公司净利润转为亏损118.17亿元；盈利能力指标同比下降。",
    }]
    result = validate_chapter_output(
        "据信用评级报告披露，母公司净利润为-118.17亿元。", ["c-source-1"], context)
    assert not [reason for reason in result["reasons"] if "新数字" in reason]

    # 负例：材料没有"亏损/为负"记号时，符号仍必须与材料一致
    context["evidence"] = [{
        "id": "c-source-1", "source_id": "source-1", "document_kind": "信用评级报告",
        "quote": "母公司净利润为118.17亿元。",
    }]
    result = validate_chapter_output(
        "据信用评级报告披露，母公司净利润为-118.17亿元。", ["c-source-1"], context)
    assert [reason for reason in result["reasons"] if "新数字" in reason]


def test_subitem_label_prefix_stays_matchable():
    """表格行「其中：公司债券」的前缀不参与比对：正文写"债券融资中，公司债券"应通过。"""
    context = _attribution_context([
        _attribution_record(1_586_600, label="债券融资"),
        _attribution_record(206_400, label="其中：公司债券"),
    ])
    assert _attribution_codes("债券融资中，公司债券206,400万元", context) == set()


def test_coverage_threshold_renders_with_unit_hint():
    """阈值单位从键名/规则标题恢复：interest_coverage {"minimum": 1.5} → “1.5倍”且保留裸形态。"""
    from backend.app.chapter_guard import _measurement_texts

    texts = _measurement_texts({"cross_checks": [{
        "rule_key": "interest_coverage", "title": "利息保障倍数",
        "thresholds": {"minimum": 1.5}, "values": [],
    }]})
    assert "1.5倍" in texts
    assert "1.5" in texts   # 裸形态仍保留：没有单位线索的历史阈值照旧按裸数字放行


def test_counterparty_row_label_satisfies_party_binding():
    """交易对手行：记录标签本身是主体名时，写在该数字旁不判“主体不匹配”。"""
    context = _context("financial")
    context["evidence"] = [{
        "id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书",
        "quote": "华东汽车集团股份有限公司（虚构）应收款项1,921万元",
    }]
    context["table_records"] = [{
        "record_id": "r1", "value": "1921", "unit": "万元",
        "label": "华东汽车集团股份有限公司（虚构）",
        "subject": "华辰精密制造有限公司", "role": "发行人", "scope": "合并口径",
        "period": "", "period_type": "", "source_ids": ["s-1"], "page": 38,
    }]
    result = validate_chapter_output(
        "据募集说明书披露，华东汽车集团股份有限公司的应收款项为1,921万元；材料不足。",
        ["c-source-1"], context)
    assert not [reason for reason in result["reasons"] if "主体" in reason]
    assert not [reason for reason in result["reasons"] if "新数字" in reason]


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


# ---------------------------------------------------------------------------
# 数字归属绑定：2026-10-11 验收五类误退的定点回归（正例 + 负例）
# 每条都对应一次真实误退的根因，负例确保修复没有把"真错误"一并放过。
# ---------------------------------------------------------------------------

def _binding_context(records: list[dict], evidence_quotes: list[str] | None = None) -> dict:
    context = _context("financial", missing=True)
    context["chapter_key"] = "financial"
    context["prompt_config"] = load_chapter_config("financial")
    context["evidence_records"] = records
    if evidence_quotes is not None:
        context["evidence"] = [
            {"id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书", "quote": q}
            for q in evidence_quotes
        ]
    return context


def _table_record(label: str, value, unit: str = "万元", *, period: str = "", subject: str = "") -> dict:
    return {
        "record_id": f"table:{label}:{value}", "record_type": "table", "label": label,
        "value": str(value), "unit": unit, "raw": "", "period": period, "period_type": "",
        "subject": subject, "subject_role": "", "scope": "", "source_ids": ["source-1"],
        "locator": {"page": 1}, "page": 1, "source_fact_ids": [], "formula": "",
        "rule_version": "", "origin_type": "document", "profile_snapshot_id": "",
        "status": "verified", "record_version": "evidence-records-v1",
    }


def _threshold_record(label: str, value, unit: str, aliases: list[str] | None = None) -> dict:
    """阈值规则记录：label 是英文 slug 前缀（"杠杆与流动性 current_ratio"），
    中文科目名走 ``aliases``（见 ``evidence_records._threshold_aliases``）。"""
    record = _table_record(label, value, unit)
    record.update({
        "record_id": f"rule:{label}:{value}", "record_type": "rule",
        "status": "derived", "origin_type": "cross_check", "aliases": list(aliases or []),
    })
    return record


def test_label_window_uses_only_text_before_number_not_following_clause():
    """① 窗口必须切在数字**之前**：数字与单位间有空格时，旧 split 退化成整段窗口，
    会把下一分句的科目名读成所写科目（中邮把所有者权益读成营业收入）。

    构造：所有者权益 809,000 万元、营业收入 81,000 万元各有记录（用**规范展示值**，
    避免千分位/裸值与记录对不上而被判新数字）；正文把两个数分别写在各自科目名下。
    旧实现的窗口会切在数字**之后**，令 809,000 读到下一分句的"营业收入"，
    从而报出 label_mismatch；正确实现只读数字之前的分句，两个数各自归属正确。
    """
    records = [_table_record("所有者权益合计", "809000"), _table_record("营业收入", "81000")]
    context = _binding_context(records)
    content = (
        "所有者权益合计为 809,000 万元。"
        "同期营业收入为 81,000 万元。披露信息待核验，材料不足。"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"])
    assert not any(item["reason"] == "party_mismatch" for item in result["attribution_conflicts"])


def test_label_window_is_scoped_to_the_number_own_clause():
    """② 窗口分句化：上一分句的科目名不得泄漏给本数字（华辰货币资金/受限资金）。"""
    records = [_table_record("货币资金", "6206"), _table_record("受限资金", "745")]
    context = _binding_context(records)
    content = "其中受限资金818万元；2024年末6,206万元，受限745万元。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"])


def test_threshold_record_alias_binds_chinese_metric_name():
    """③ 阈值记录的中文科目名通过 aliases 绑定：正文写"资产负债率低于70%"是正确引用。"""
    records = [{
        "record_id": "rule:leverage_liquidity:threshold:debt_ratio_pct", "record_type": "rule",
        "label": "杠杆与流动性 debt_ratio_pct", "value": "70", "unit": "%", "raw": "",
        "period": "", "period_type": "", "subject": "", "subject_role": "", "scope": "",
        "source_ids": ["source-1"], "locator": {}, "page": None, "source_fact_ids": [],
        "formula": "", "rule_version": "evidence-records-v1", "origin_type": "cross_check",
        "profile_snapshot_id": "", "status": "derived", "record_version": "evidence-records-v1",
        "aliases": ["资产负债率"],
    }]
    context = _binding_context(records)
    content = "系统核验结论为通过，即资产负债率低于70%。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"])


def test_threshold_alias_builder_maps_slug_to_metric_name():
    """③b 别名构造器：slug → METRIC_NAMES 中文名。"""
    from backend.app.evidence_records import _threshold_aliases

    check = {"rule_key": "leverage_liquidity", "title": "杠杆与流动性", "values": []}
    assert _threshold_aliases("debt_ratio_pct", {}, check) == ["资产负债率"]
    assert _threshold_aliases("current_ratio", {}, check) == ["流动比率"]
    # 非指标键不得凭空造出别名（负例）。
    assert _threshold_aliases("tolerance_pct", {}, check) == []


def test_rule_formula_binds_written_input_subject():
    """④ 规则公式里的输入科目算同一归属："经营现金流对利息支出覆盖 1.71 倍"。"""
    records = [{
        "record_id": "rule:interest_coverage:value:0", "record_type": "rule",
        "label": "利息覆盖倍数", "value": "1.71", "unit": "倍", "raw": "", "period": "",
        "period_type": "", "subject": "", "subject_role": "", "scope": "", "source_ids": ["source-1"],
        "locator": {}, "page": None, "source_fact_ids": [], "formula": "经营现金流/利息支出",
        "rule_version": "evidence-records-v1", "origin_type": "cross_check",
        "profile_snapshot_id": "", "status": "derived", "record_version": "evidence-records-v1",
    }]
    context = _binding_context(records)
    content = "经营现金流对利息支出的覆盖倍数为1.71倍。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"])


def test_parenthesised_label_also_registers_bare_subject():
    """⑤ 带括号后缀的记录名同时登记主体词："关联方往来（未提供合同）"↔"关联方往来"。"""
    records = [_table_record("关联方往来（未提供合同）", "1200"), _table_record("其他应收款", "2880")]
    context = _binding_context(records)
    content = "其他应收款中关联方往来1,200万元未提供合同。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"])


def _disclosure_record(label: str, value, unit: str = "", *, status: str = "verified") -> dict:
    """来自引文的披露记录（单位可缺省——引文窗口常看不到表头单位）。"""
    record = _table_record(label, value, unit)
    record.update({
        "record_id": f"disclosure:{label}:{value}", "record_type": "disclosure", "status": status,
    })
    return record


def test_blank_unit_disclosure_record_candidates_for_written_money_mention():
    """⑤b 单位未注明的**披露**记录按金额量纲折算后参与候选（2026-10-16 北投 debt 158.66 亿实测）。

    评级报告到期分布表以文本形态出现在引文里（"公司信用类债券 158.66 …"），单位"（亿元）"
    印在表头、不在引文窗口内 → 披露记录单位为空。正文写"公司信用类债券158.66亿元"时，
    该记录若不进候选，数字会改绑到同值的别表记录（"债券融资"，万元）并造出假 label_mismatch。
    正例：空单位披露记录按亿元折算命中 → 科目兼容 → 无冲突。
    负例①：fact/metric 的裸数字不跨量纲参与（配置裸数字不得为正文数值背书）；
    负例②：科目确实写错时仍报冲突。
    """
    records = [_disclosure_record("信用类债券", "158.66"), _table_record("债券融资", "1586600")]
    context = _binding_context(records)
    content = "债务到期分布显示，公司信用类债券158.66亿元、其他有息债务95.65亿元。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )

    # 负例①：同一空单位值挂在 fact 上时不得跨量纲匹配 → 亿元数字按"新数字"拦截。
    fact_record = _table_record("债券融资", "158.66", "")
    fact_record.update({"record_id": "fact:x", "record_type": "fact"})
    result_fact = validate_chapter_output(
        "公司信用类债券158.66亿元。披露信息待核验，材料不足。",
        ["c-source-1"], _binding_context([fact_record]),
    )
    assert result_fact["unexpected_numbers"], "fact 空单位记录不得跨量纲为亿元数字背书"

    # 负例②：科目确实写错（词表内科目"应付债券"对 158.66 亿元）时仍报冲突。
    records_with_subject = [*records, _table_record("应付债券", "999999")]
    result_wrong = validate_chapter_output(
        "应付债券158.66亿元。披露信息待核验，材料不足。",
        ["c-source-1"], _binding_context(records_with_subject),
    )
    assert any(item["reason"] == "label_mismatch" for item in result_wrong["attribution_conflicts"]), (
        result_wrong["attribution_conflicts"]
    )


def test_entity_noise_word_stripped_before_label_compatibility():
    """⑤c 科目名内的主体噪声词（"公司"）两侧去噪后再判包含（2026-10-16 中邮 operations 实测）。

    记录标签（引文截断）为"占公司主营业务收入的比例"，正文写"投资交易业务收入占主营业务
    收入的比例分别为31.36%、29.31%和35.35%"——原样包含判定失败、三个数字全被判
    label_mismatch。去噪后两侧同形 → 兼容；后位值（29.31%/35.35%）经"分别"枚举头继承
    同样命中。
    负例：其它科目名（"营业收入"）与比率标签仍不兼容，不得借去噪放行。
    """
    from backend.app.chapter_guard import _labels_compatible

    assert _labels_compatible("占主营业务收入的比例", "占公司主营业务收入的比例")
    assert not _labels_compatible("营业收入", "占公司主营业务收入的比例")
    assert not _labels_compatible("投资收益", "占公司主营业务收入的比例")

    records = [
        _disclosure_record("占公司主营业务收入的比例", "31.36", "%"),
        _disclosure_record("占公司主营业务收入的比例", "29.31", "%"),
        _disclosure_record("占公司主营业务收入的比例", "35.35", "%"),
    ]
    content = "投资交易业务收入占主营业务收入的比例分别为31.36%、29.31%和35.35%。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], _binding_context(records))
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_bare_quote_number_with_declared_money_unit_backs_body_written_amount():
    """⑥ 引文表格行"（亿元）…43.40"是裸数字，正文写"43.40 亿元"必须认账。"""
    records = [_table_record("其他项目", "9999")]
    context = _binding_context(
        records,
        evidence_quotes=["表 12：近年来公司现金流相关指标情况（亿元）\n"
                         "经营活动产生的现金流量净额 43.40 90.31 174.97"],
    )
    content = "经营活动产生的现金流量净额分别为 43.40 亿元、90.31 亿元和 174.97 亿元。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert result["unexpected_numbers"] == [], result["unexpected_numbers"]


def test_bare_quote_number_does_not_back_a_fabricated_magnitude():
    """⑥b 负例：引文里没有的值，即便同一引文声明了金额单位，也不得放行。"""
    records = [_table_record("其他项目", "9999")]
    context = _binding_context(
        records,
        evidence_quotes=["表 12：近年来公司现金流相关指标情况（亿元）\n"
                         "经营活动产生的现金流量净额 43.40 90.31 174.97"],
    )
    content = "经营活动产生的现金流量净额为 999.99 亿元。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert any(item["raw"] == "999.99亿元" for item in result["unexpected_numbers"])


def test_year_range_prefix_is_not_read_as_the_numbers_period():
    """⑦ 年份区间"2023—2025 年"被 ±20 窗口截断后，残片不得冒充数字的期间归属。"""
    records = [
        _table_record("资产总额", "2114692.49", period="2023"),
        _table_record("资产总额", "2526880.06", period="2024"),
    ]
    context = _binding_context(records)
    content = ("2023—2025 年合并口径资产总额分别为 2,114,692.49 万元、2,526,880.06 万元。"
               "披露信息待核验，材料不足。")
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "period_mismatch" for item in result["attribution_conflicts"])


def test_ratio_denominator_in_window_does_not_hide_the_numerator_subject():
    """⑧ 比率陈述里数字紧跟**分母**：记录 label 是分子（存货），正文写"存货占资产总额
    比重为 8.32%"，数字前的 16 字窗口只扫到"资产总额"，旧实现把科目正确的比率判成
    label_mismatch（北投财务实测：改成"存货占比"后冲突 1→0）。分子必须被纳入候选。"""
    records = [
        _table_record("存货", "8.32", "%", period="2023"),
        _table_record("资产总额", "2114692.49", "万元", period="2023"),
    ]
    context = _binding_context(records)
    content = "存货占资产总额比重为 8.32%。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_ratio_numerator_allowance_does_not_rescue_a_wrong_subject():
    """⑨ 负例：分子放宽**只**补进"占/在"之前的词，不能为真正写错科目的比率背书。
    记录里只有"营业收入"的百分比，正文却写"存货占资产总额比重 8.32%"，仍须报 label_mismatch。"""
    records = [
        _table_record("营业收入", "8.32", "%", period="2023"),
        _table_record("资产总额", "2114692.49", "万元", period="2023"),
    ]
    context = _binding_context(records)
    content = "存货占资产总额比重为 8.32%。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"])


def test_ratio_numerator_allowance_leaves_non_percent_labels_alone():
    """⑩ 负例：分子补全只在候选记录**清一色是百分数**时启用。金额型科目名里出现
    "占"字（"交易性金融资产占款"）不得因此放宽归属窗口。"""
    records = [
        _table_record("货币资金", "3262", "万元", period="2023"),
        _table_record("资产总额", "2114692.49", "万元", period="2023"),
    ]
    context = _binding_context(records)
    content = "存货占资产总额口径的金额为 3,262 万元。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"])


def test_markdown_table_rows_never_take_part_in_subject_binding():
    """⑪ 表格单元格里的数字不做归属绑定：表格**行名**会被当成"所写科目"，与记录的
    label 天然重名，混进正文会造出假 label_mismatch（中邮 rating 实测：正文里冻结的
    渲染表格与当前重建表不再逐字相等，`replace(frozen,"")` 失效，111.99 因此被拿"自有负债"
    行名比对而误拒）。表格行按行剔除，与表格来源 / 版本漂移无关。"""
    records = [_table_record("负债总额", "137.51", "万元", period="2025")]
    context = _binding_context(records)
    content = (
        "负债总额为 137.51 万元。披露信息待核验，材料不足。\n\n"
        "| 科目 | 2025 年末 |\n"
        "| --- | --- |\n"
        "| 按权属分：自有负债 | 111.99 |\n"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_markdown_table_row_removal_does_not_disable_binding_for_prose():
    """⑫ 负例：剔除表格行不得顺带放过正文里的真实错配。

    正文写"资产总额 137.51 万元"，而 137.51 只挂在"负债总额"记录下——
    "资产总额"在词表里但与该记录不兼容，须报 label_mismatch。
    """
    records = [
        _table_record("负债总额", "137.51", "万元", period="2025"),
        _table_record("资产总额", "999.99", "万元", period="2025"),
    ]
    context = _binding_context(records)
    content = (
        "| 科目 | 金额 |\n| --- | --- |\n| 自有负债 | 137.51 |\n\n"
        "资产总额为 137.51 万元。披露信息待核验，材料不足。\n"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_ratio_numerator_pronoun_backreference_reaches_previous_clause():
    """⑬ 分子写成代词时，真正的科目名在**前一分句**。

    正文实测（北投财务）：
    "其他应收款由2023年末的1,548,235.80万元降至2025年末的1,251,848.55万元（E01），
     募集说明书披露其占总资产比重由1.77%降至1.29%"

    1.77% 的 160 字宽前缀里，本分句只有"募集说明书披露其占总资产比重由"，
    "占"之前是材料前缀+代词"其"，分子"其他应收款"在**前一分句**。旧实现把
    "募集说明书披露其"整段当所写科目（或退化成"总资产"），制造 label_mismatch 误退。
    """
    records = [
        _table_record("其他应收款", "1.77", "%", period="2023"),
        _table_record("总资产", "87473278.45", "万元", period="2023"),
    ]
    context = _binding_context(records)
    content = (
        "其他应收款由2023年末的1,548,235.80万元降至2025年末的1,251,848.55万元，"
        "募集说明书披露其占总资产比重由1.77%降至1.29%。披露信息待核验，材料不足。"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_ratio_numerator_structural_continuation_reaches_previous_clause():
    """⑭ 分子不在"占"所在分句，而是**前一分句的主语**（结构承接写法）。

    正文实测（北投财务）：
    "非流动资产合计由2023年末的73,608,329.67万元增至2025年末的81,581,809.57万元，
     占总资产比重维持在84%左右"

    "占总资产比重维持在"这一分句里没有任何科目名，分子是上一分句的主语
    "非流动资产合计"。旧实现取不到分子，只命中分母"总资产"，制造 label_mismatch 误退。
    """
    records = [
        _table_record("非流动资产合计", "84", "%", period="2025"),
        _table_record("总资产", "96717233.20", "万元", period="2025"),
    ]
    context = _binding_context(records)
    content = (
        "非流动资产合计由2023年末的73,608,329.67万元增至2025年末的81,581,809.57万元，"
        "占总资产比重维持在84%左右。披露信息待核验，材料不足。"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_ratio_backreference_does_not_rescue_a_wrong_subject():
    """⑮ 负例：回指只补进"前一分句真实存在的科目名"，不能为写错科目的比率背书。

    前一分句主语是"货币资金"，正文却拿 84% 去比"非流动资产合计"的记录，仍须报冲突。
    """
    records = [
        _table_record("非流动资产合计", "84", "%", period="2025"),
        _table_record("总资产", "96717233.20", "万元", period="2025"),
    ]
    context = _binding_context(records)
    content = (
        "货币资金由2023年末的1,000.00万元增至2025年末的2,000.00万元，"
        "占总资产比重维持在84%左右。披露信息待核验，材料不足。"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_ratio_backreference_never_crosses_a_sentence_boundary():
    """⑯ 负例：回指不得越过句号——跨句的科目名属于上一句，不是本句比率的分子。

    上一句主语"非流动资产合计"与下一句"占总资产比重维持在84%"之间是**句号**，
    正文换了话题，不该把上一句的科目名搬来给本句背书。
    """
    records = [
        _table_record("非流动资产合计", "84", "%", period="2025"),
        _table_record("总资产", "96717233.20", "万元", period="2025"),
    ]
    context = _binding_context(records)
    content = (
        "非流动资产合计由2023年末的73,608,329.67万元增至2025年末的81,581,809.57万元。"
        "公司整体经营情况良好，占总资产比重维持在84%左右。披露信息待核验，材料不足。"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )



def test_chinese_numeral_label_matches_arabic_record_label():
    """⑰ 期限档的中文数字与阿拉伯数字是同一档。

    记录 label 是表格行"1年以上"，正文写"一年以上到期的长期有息债务…占有息债务合计的
    94.85%"（中邮 debt 实测）：分子回指已正确取到科目名，却因"一"≠"1"而报 label_mismatch。
    """
    records = [
        _table_record("1年以上", "94.85", "%", period="2025"),
        _table_record("有息债务合计", "46409400", "万元", period="2025"),
    ]
    context = _binding_context(records)
    content = (
        "一年以上到期的长期有息债务为44,017,400万元，占有息债务合计的94.85%。"
        "披露信息待核验，材料不足。"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_numeral_normalisation_does_not_corrupt_compound_numbers():
    """⑱ 负例：归一化只在"数字紧贴量词"处逐字符替换，不得把"二十一年"切成"二十1年"。

    "二十一年" 属复合数字，不做转换（"21年" 配不上它）——宁可漏报（待人工），
    也不把 21 读成 201 这类改错。同时验证对照面："一年以上" 确实被转成 "1年以上"。
    """
    from backend.app.chapter_guard import _labels_compatible, _normalize_label_numerals

    assert _normalize_label_numerals("一年以上") == "1年以上"
    assert _normalize_label_numerals("十年") == "10年"
    # 复合数字：前一字是中文数字时不再转换，"二十一年" 保持原样（不得变成 "二十1年"）。
    assert _normalize_label_numerals("二十一年以上") == "二十一年以上"
    assert _labels_compatible("二十一年到期的长期借款", "21年") is False
    assert _labels_compatible("一年以上到期的长期有息债务", "1年以上") is True


def test_comparison_bound_is_review_not_label_conflict():
    """⑲ 比较基准不是取值：数字紧跟"低于/不超过…"时是上下限，不是取值断言。

    实测（中邮 financial）："速动比率三年均低于1倍"里的 1 命中别科目的阈值规则
    （流动比率／杠杆与流动性 current_ratio），把科目正确的定性表述判成 label_mismatch。

    构造：同值（1 倍）记录只有流动比率的阈值，正文写的是速动比率。
    速动比率自身的记录取 0.32 倍——既不等于 1，又保证"速动比率"进词表（可被写出来）。
    """
    records = [
        _threshold_record("杠杆与流动性 current_ratio", "1", "倍", aliases=["流动比率"]),
        _table_record("速动比率", "0.32", "倍", period="2025"),
    ]
    context = _binding_context(records)
    content = "流动比率与速动比率虽呈逐年小幅改善，但速动比率三年均低于1倍。材料不足，披露信息待核验。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )
    # 计划 §四 line 34：核对"所写科目自己的记录确实小于1"——0.32 < 1 成立 → 通过，
    # 既不判冲突、也不再挂复核项（此前的实现只降级到待核验，仍留一条噪声）。
    codes = {item["code"] for item in result["needs_review_items"]}
    assert "comparison_bound" not in codes, result["needs_review_items"]


def test_comparison_bound_that_records_contradict_goes_to_review():
    """⑲b 比较界限**核对不通过** → 待核验（不判硬冲突）。

    正文写"速动比率三年均低于1倍"，而所写科目自己的记录是 1.5 倍（≥1）——不能通用豁免
    "低于"句（计划 §四 line 35），但也不把它当科目错配：界限本身无法机检到"三年均"的
    范围，交人工最稳。

    记录取 **1.5** 而不是 1.2：正文写的是 `1倍`（0 位精度），1.2 会被舍入成 1 而**本身成为
    候选**，于是走"科目相符"的正常路径、根本到不了界限核对（实测踩过）。1.5 舍入到 2，
    不进候选，才能真正验到核对分支。
    """
    records = [
        _threshold_record("杠杆与流动性 current_ratio", "1", "倍", aliases=["流动比率"]),
        _table_record("速动比率", "1.5", "倍", period="2025"),
    ]
    context = _binding_context(records)
    content = "流动比率与速动比率虽呈逐年小幅改善，但速动比率三年均低于1倍。材料不足，披露信息待核验。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )
    codes = {item["code"] for item in result["needs_review_items"]}
    assert "comparison_bound" in codes, result["needs_review_items"]


def test_comparison_bound_over_a_share_is_verified_across_periods():
    """⑲c 正例："三个年度均超过83%"逐期间核对（计划 §五 line 46-47）。

    非流动资产占比三年为 84.1495/83.9207/84.3509，均 > 83 → 通过；
    把其中一年改成 82.0 → 核对不通过，转待核验。
    """
    from backend.app.chapter_guard import _verify_comparison_bound

    item = {"raw": "83%", "normalized_value": "83", "normalized_unit": "%"}
    prefix = "非流动资产占资产总额的比例在三个年度均超过"
    assert _verify_comparison_bound(item, prefix, {"非流动资产占比"}, [
        _table_record("非流动资产占比", "84.1495", "%", period="2023"),
        _table_record("非流动资产占比", "83.9207", "%", period="2024"),
        _table_record("非流动资产占比", "84.3509", "%", period="2025"),
    ])[0] is True
    assert _verify_comparison_bound(item, prefix, {"非流动资产占比"}, [
        _table_record("非流动资产占比", "82.0", "%", period="2023"),
    ])[0] is False


def test_comparison_bound_needs_review_when_it_cannot_be_checked():
    """⑲d 负例：核不了就待核验——缺科目名 / 缺同单位记录 / 无比较算子。

    计划 §四 line 35："无法确定范围时待核验，不能通用豁免所有'超过、低于'句"。
    """
    from backend.app.chapter_guard import _verify_comparison_bound

    item = {"raw": "1倍", "normalized_value": "1", "normalized_unit": "倍"}
    # 没写出科目名
    assert _verify_comparison_bound(item, "但三年均低于", set(), [
        _table_record("速动比率", "0.5", "倍")])[0] is None
    # 单位不可比（记录是 %，界限是 倍）
    assert _verify_comparison_bound(item, "但速动比率低于", {"速动比率"}, [
        _table_record("速动比率", "0.5", "%")])[0] is None
    # 找不到所写科目的记录
    assert _verify_comparison_bound(item, "但速动比率低于", {"速动比率"}, [
        _table_record("流动比率", "1.5", "倍")])[0] is None
    # 没有比较算子 → 不是界限，不走这条路
    assert _verify_comparison_bound(item, "但速动比率为", {"速动比率"}, [
        _table_record("速动比率", "0.5", "倍")])[0] is None


def test_comparison_bound_does_not_rescue_a_plain_value_mismatch():
    """⑳ 负例：没有比较算子时，科目错配仍是硬冲突——豁免只认比较算子。

    "速动比率为 1 倍"（无"低于"）读作取值断言，而同值记录属流动比率，须报 label_mismatch。
    """
    records = [
        _threshold_record("杠杆与流动性 current_ratio", "1", "倍", aliases=["流动比率"]),
        _table_record("速动比率", "0.32", "倍", period="2025"),
    ]
    context = _binding_context(records)
    content = "速动比率为 1 倍。材料不足，披露信息待核验。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_comparison_bound_does_not_exempt_a_matching_subject():
    """㉑ 负例：所写科目与同值记录**本来就相符**时走正常路径，不产生 comparison_bound 复核项。

    "流动比率低于1倍"的 1 命中流动比率的阈值记录（经 aliases 绑定），科目相符 →
    正常通过，不该多出一条待核验噪声（豁免只在"会对不上"时才生效）。
    """
    records = [
        _threshold_record("杠杆与流动性 current_ratio", "1", "倍", aliases=["流动比率"]),
        _table_record("速动比率", "0.32", "倍", period="2025"),
    ]
    context = _binding_context(records)
    content = "流动比率低于1倍。材料不足，披露信息待核验。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )
    codes = {item["code"] for item in result["needs_review_items"]}
    assert "comparison_bound" not in codes, result["needs_review_items"]


# ---------------------------------------------------------------------------
# 旧MD计划剩余项收口执行方案 §一：完整句识别 / 并列期间顺序绑定 / 格式序号
# 每条都对应北投 0685da5d 回放样本（首稿/修复稿原文）里的实测项。
# ---------------------------------------------------------------------------

def test_sentence_subject_binds_series_head_subject_across_window():
    """㉒ 完整句识别：系列值从第二个起，窗口只剩"至2025年分别为…"，句子主语
    "营业毛利率"落在窗口之外——必须按**本句最先出现的科目名**绑定。

    北投财务实测（0685da5d 首稿/修复稿）："营业毛利率在2023年至2025年分别为
    -45.98%、-83.96%、-59.54%" 三个值被上一句的"净利率"混淆，连报三条
    label_mismatch；"净利率"不得从上一句继承进来。
    """
    records = [
        _table_record("营业毛利率（%）", "-45.98", "%", period="2023"),
        _table_record("营业毛利率（%）", "-83.96", "%", period="2024"),
        _table_record("营业毛利率（%）", "-59.54", "%", period="2025"),
        _table_record("净利率", "11.46", "%", period="2025"),
    ]
    context = _binding_context(records)
    content = (
        "系统计算的净利率为11.46%。"
        "募集说明书披露的营业毛利率在2023年至2025年分别为-45.98%、-83.96%、-59.54%。"
        "披露信息待核验，材料不足。"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    conflicts = result["attribution_conflicts"]
    assert not any(item["reason"] in {"label_mismatch", "period_mismatch"} for item in conflicts), conflicts


def test_sentence_subject_does_not_rescue_a_wrong_record_label():
    """㉒b 负例：句子主语写什么就绑什么——正文把数写成"净利率"，同值记录却属"营业毛利率"，
    仍须报 label_mismatch（完整句识别只认**写出来的**主语，不为写错科目背书）。"""
    records = [
        _table_record("营业毛利率（%）", "-45.98", "%", period="2023"),
        _table_record("净利率", "11.46", "%", period="2025"),
    ]
    context = _binding_context(records)
    content = "净利率在2023年至2025年分别为-45.98%。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_ordered_series_period_binds_by_sequence_position():
    """㉓ 并列期间顺序绑定："2023年末、2024年末和2025年末分别为A、B、C" 按序号对年。

    北投财务实测：中段值 83.92% 被前缀窗口绑到 2025（区间里的最后一个年份）而误报
    period_mismatch——正例：三个值按顺序对上 2023/2024/2025，不再有期间冲突。
    """
    records = [
        _table_record("非流动资产占比", "84.15", "%", period="2023"),
        _table_record("非流动资产占比", "83.92", "%", period="2024"),
        _table_record("非流动资产占比", "84.35", "%", period="2025"),
    ]
    context = _binding_context(records)
    content = (
        "从资产结构看，非流动资产占比在2023年末、2024年末和2025年末分别为84.15%、83.92%和84.35%。"
        "披露信息待核验，材料不足。"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "period_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_ordered_series_period_does_not_hide_a_misplaced_value():
    """㉓b 负例：顺序绑定让"值写错位置"照旧暴露——84.35 放在 2024 的位子上即报期间不匹配。"""
    records = [
        _table_record("非流动资产占比", "84.15", "%", period="2023"),
        _table_record("非流动资产占比", "83.92", "%", period="2024"),
        _table_record("非流动资产占比", "84.35", "%", period="2025"),
    ]
    context = _binding_context(records)
    content = (
        "非流动资产占比在2023年末、2024年末和2025年末分别为84.15%、84.35%和83.92%。"
        "披露信息待核验，材料不足。"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert any(item["reason"] == "period_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_ordered_series_period_ignores_thousand_separators():
    """㉓c 千分位逗号不是值分隔符：'…分别为204,709.77万元、307,496.94万元和405,806.33万元'
    的第二个值应绑 2024——旧实现把千分位逗号当分隔符、序号 +1 → 误绑 2025，与记录
    造出假 period_mismatch（中邮 debt 实测）。"""
    records = [
        _table_record("应付债券", "204709.77", "万元", period="2023"),
        _table_record("应付债券", "307496.94", "万元", period="2024"),
        _table_record("应付债券", "405806.33", "万元", period="2025"),
    ]
    context = _binding_context(records)
    content = (
        "从应付债券科目看，2023年末、2024年末和2025年末分别为204,709.77万元、307,496.94万元和405,806.33万元。"
        "披露信息待核验，材料不足。"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "period_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_excluded_subject_before_wai_is_not_a_written_label():
    """㉓d "X外/除X外"里 X 是被**排除**的对象，不当所写科目。

    中邮 debt 实测："银行借款与公司债券外其他公司信用类债券余额之和…占比14.00%"——
    被排除的"公司债券"误当科目，与记录行"债务融资工具"造出假 label_mismatch。
    负例：没有"外"的"公司债券占有息负债余额的比例为14.00%"仍照常按科目不匹配拦截。
    """
    records = [
        _table_record("债务融资工具", "201152.73", "万元"),
        _table_record("债务融资工具", "14", "%"),
        _table_record("其中：公司债券", "405806.33", "万元"),
    ]
    context = _binding_context(records)
    good = validate_chapter_output(
        "银行借款与公司债券外其他公司信用类债券余额之和为20.12亿元，"
        "占有息负债余额的比例为14.00%。披露信息待核验，材料不足。",
        ["c-source-1"], context,
    )
    assert not any(
        item["reason"] == "label_mismatch" for item in good["attribution_conflicts"]
    ), good["attribution_conflicts"]
    bad = validate_chapter_output(
        "公司债券占有息负债余额的比例为14.00%。披露信息待核验，材料不足。",
        ["c-source-1"], context,
    )
    assert any(
        item["reason"] == "label_mismatch" for item in bad["attribution_conflicts"]
    ), bad["attribution_conflicts"]


def test_ordinal_format_numbers_are_not_business_values():
    """㉔ 格式序号（"第1个交易日""前1个交易日"）不是业务数值。

    北投 debt 实测："顺延至其后的第1个交易日" 里的 1 被读成裸数字、查无同值记录，
    造出一条假 ambiguous_attribution 复核项。收口方案 §一："格式序号…不作为独立
    金额或指标取值核验"。
    """
    stripped, ignored, _ = chapter_guard._strip_format_numbers("顺延至其后的第1个交易日")
    assert "第1个" not in stripped and "1" in ignored

    records = [_table_record("负债总额", "137.51", "万元", period="2025")]
    context = _binding_context(records)
    content = (
        "本期债券兑付日期如遇法定节假日或休息日则顺延至其后的第1个交易日，"
        "兑付登记日为兑付日的前1个交易日。披露信息待核验，材料不足。"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not result["unexpected_numbers"], result["unexpected_numbers"]
    assert not any(item["code"] == "ambiguous_attribution" for item in result["needs_review_items"]), (
        result["needs_review_items"]
    )


def test_duration_phrases_are_not_independent_values():
    """㉕ 期限短语不是独立取值（收口方案 §一）："1年以内/1年以上"、"未来N个月"、
    "N年期" 里的数字不作为金额或指标取值核验。

    北投 debt 实测："1年以内合计385.63亿元" 的 1 在支持级被当成"查无记录的数字"；
    financial 缺口句 "未来12个月到期债务" 同理。
    """
    from backend.app.chapter_guard import _number_mentions

    assert [m["raw"] for m in _number_mentions(
        "1年以内合计385.63亿元，1年以上合计4,401.74亿元。")] == ["385.63亿元", "4,401.74亿元"]
    assert _number_mentions("有息负债合计及未来12~18个月到期债务未披露。") == []
    assert _number_mentions("本期债券品种一为3年期固定利率债券。") == []
    # 负例：普通数值不受影响
    assert [m["raw"] for m in _number_mentions("2025年末有息负债为1,234万元。")] == ["1,234万元"]


def test_page_reference_with_fullwidth_dash_is_stripped():
    """㉖ "第77—81页"（全角破折号）不是业务数字（北投 financial #55 实测）。"""
    stripped, _ignored, pages = chapter_guard._strip_format_numbers("详见第77—81页。")
    assert pages == ["77", "81"]
    assert "77" not in stripped and "81" not in stripped


def test_ratio_numerator_picks_the_marker_adjacent_amount():
    """⑰ 同分句多个并列比率：分子取**紧邻本比率标记**的那个金额左侧科目名。

    正文实测（北投 debt #21）：
    "短期借款以保证借款678,040.93万元、占比63.71%和信用借款386,298.82万元、占比36.29%"

    36.29% 的 16 字窗口只剩"款386,298.82万元、占比"——第一项分子挤出了窗口；
    旧实现 `_cut_ratio_left` 切在**第一个**"占"上，把第一项分子"保证借款"错配给第二项，
    制造 label_mismatch 误退。改为取**最后一个**标记左侧的分子后，两项各归其位。
    """
    records = [
        _table_record("保证借款", "63.71", "%", period="2025"),
        _table_record("信用借款", "36.29", "%", period="2025"),
        _table_record("保证借款", "678040.93", "万元", period="2025"),
        _table_record("信用借款", "386298.82", "万元", period="2025"),
    ]
    context = _binding_context(records)
    content = (
        "短期借款以保证借款678,040.93万元、占比63.71%和信用借款386,298.82万元、"
        "占比36.29%构成。披露信息待核验，材料不足。"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_ratio_numerator_multi_marker_does_not_rescue_wrong_subject():
    """⑱ 负例：多标记取"最右"只补进**该位置真实**的科目名，不为写错的比率背书。

    第二项比率（36.29%）本应属"信用借款"，正文却写成**另一个记录科目**"保证借款"
    （词表里存在）——取"最右"补进的是"保证借款"，与 36.29% 的信用借款记录不兼容，
    仍须报冲突（若实现错把第一项位置当分母、或错误放行整段，此断言会失败）。
    """
    records = [
        _table_record("保证借款", "63.71", "%", period="2025"),
        _table_record("信用借款", "36.29", "%", period="2025"),
    ]
    context = _binding_context(records)
    content = (
        "短期借款以保证借款678,040.93万元、占比63.71%和信用借款386,298.82万元、"
        "占比63.71%构成。披露信息待核验，材料不足。"
    )
    result = validate_chapter_output(content, ["c-source-1"], context)
    assert any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_period_bucket_labels_never_become_written_subjects():
    """期间桶标签（"1年以内/1年以上"）不进科目词表（2026-10-16 北投 debt 实测）。

    "银行贷款1年以内131.15亿元、1年以上3,287.00亿元"——第二个值的窗口只剩桶名，
    桶标签进词表会把它当成"所写科目"，令数值正确（3,287.00 亿元 = 银行贷款记录）
    的句子被判 label_mismatch。桶是期间维度、不是科目。
    """
    records = [
        _table_record("1年以上", "44017400"),
        _table_record("银行贷款", "32870000"),
    ]
    content = "到期分布中，1年以上3,287.00亿元。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], _binding_context(records))
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )
    # 对照：桶标签**就是候选记录的科目**时（"一年以上到期的有息债务占…94.85%"）仍按科目核对。
    records_bucket = [_table_record("1年以上", "94.85", "%"), _table_record("有息债务合计", "46409400")]
    content_bucket = "一年以上到期的长期有息债务为44,017,400万元，占有息债务合计的94.85%。披露信息待核验，材料不足。"
    result_bucket = validate_chapter_output(content_bucket, ["c-source-1"], _binding_context(records_bucket))
    assert not any(item["reason"] == "label_mismatch" for item in result_bucket["attribution_conflicts"]), (
        result_bucket["attribution_conflicts"]
    )


def test_entity_noise_strip_does_not_touch_instrument_names():
    """去噪不得把"公司债券"剥成"债券"（2026-10-16 中邮 debt 20.00 亿元实测）。

    "本期债券发行金额为不超过人民币20.00亿元"的窗口里有"债券"二字；若"公司债券"
    被去噪成"债券"，窗口包含判定会把它当成所写科目、对同值的"本期发行规模"记录
    报假 label_mismatch。工具名（公司债券/公司债）里的"公司"是名字的一部分，不剥。
    """
    from backend.app.chapter_guard import _strip_entity_noise

    assert _strip_entity_noise("公司债券") == "公司债券"
    assert _strip_entity_noise("占公司主营业务收入的比例") == "占主营业务收入的比例"

    records = [_table_record("公司债券", "9999"), _table_record("本期发行规模", "200000")]
    content = "募集说明书披露，本期债券发行金额为不超过人民币20.00亿元（含20.00亿元）。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], _binding_context(records))
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_total_synonym_asset_total_and_asset_zongji():
    """“总计”≡“总额”：资产总计/资产总额是同一行的两种写法（2026-10-16 中邮 financial 实测）。"""
    from backend.app.chapter_guard import _labels_compatible

    assert _labels_compatible("资产总额", "资产总计")
    assert not _labels_compatible("资产总额", "负债总额")

    records = [_table_record("资产总计", "2729368.97"), _table_record("负债和所有者权益总计", "2729368.97")]
    content = "母公司口径资产总额2025年末为2,729,368.97万元。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], _binding_context(records))
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )


def test_coverage_phrase_binds_metric_alias_and_formula():
    """覆盖率/覆盖倍数的**谓词短语**并入候选标签（2026-10-16 华辰 debt 0.29 倍/中邮 596.66 倍实测）。

    正文把指标写成"A对B的覆盖倍数"时，窗口词表只剩分子（可用现金）；记录却是指标
    规范名（现金短债覆盖）＋叙述别名（短期债务的覆盖）。短语并入后经别名/公式路径命中。
    负例：短语不含记录别名/公式输入项时仍报冲突。
    """
    from backend.app.chapter_guard import _coverage_phrase_labels

    assert "可用现金对短期债务的覆盖倍数" in _coverage_phrase_labels("可用现金对短期债务的覆盖倍数为")
    # 年份尾巴（"2025年偿债覆盖倍数"）剥掉年份后仍保留短语本体。
    assert "偿债覆盖倍数" in _coverage_phrase_labels("2025年偿债覆盖倍数为")

    metric = _table_record("现金短债覆盖", "0.2927", "倍")
    metric.update({
        "record_id": "metric:cash-short-debt", "record_type": "metric", "status": "derived",
        "formula": "(货币资金-受限资金)/一年内到期债务",
        "label_aliases": ["短期债务的覆盖", "到期债务的覆盖"],
    })
    content = "可用现金对短期债务的覆盖倍数为0.29倍。披露信息待核验，材料不足。"
    result = validate_chapter_output(content, ["c-source-1"], _binding_context([metric]))
    assert not any(item["reason"] == "label_mismatch" for item in result["attribution_conflicts"]), (
        result["attribution_conflicts"]
    )
