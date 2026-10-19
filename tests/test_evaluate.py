import json

from backend.app import llm
from backend.app.main import startup
from backend.app.bond_documents import evaluation_manifest as bond_evaluation_manifest
from backend.app.schemas import ExtractedFact, OneShotResult
from scripts import evaluate
from scripts.evaluate import _fact_extraction_accuracy, _risk_scores


def test_fact_extraction_accuracy_checks_values_and_explicit_periods():
    expected = [
        {"fact_key": "revenue", "period": "2025", "value": 1000, "unit": "万元"},
        {"fact_key": "debt_due_12m", "period": "2026", "value": 300, "unit": "万元"},
    ]
    facts = [
        {"fact_key": "revenue", "period": "2024", "value": 1000},
        {"fact_key": "revenue", "period": "2025", "value": 900},
        {"fact_key": "debt_due_12m", "period": "", "value": 300},
    ]

    accuracy, matched = _fact_extraction_accuracy(facts, expected)

    assert accuracy == 0.5
    assert matched == 1


def test_risk_scores_penalize_false_positives_and_missed_risks():
    recall, precision, f1 = _risk_scores({"customer", "collateral"}, {"customer", "purpose"})

    assert recall == 0.5
    assert precision == 0.5
    assert f1 == 0.5


def test_bond_baseline_uses_policy_rules_and_stratified_sources(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm, "is_configured", lambda: True)

    def fake_call(_system, prompt, _schema, **_kwargs):
        captured.update(json.loads(prompt))
        return {"facts": [], "warning_keys": [], "citation_ids": []}, llm.LlmUsage(
            input_tokens=100, output_tokens=12, finish_reason="stop", response_chars=48,
            response_keys=["citation_ids", "facts", "warning_keys"],
            response_field_counts={"facts": 0, "warning_keys": 0, "citation_ids": 0},
            response_value_types={"facts": "list", "warning_keys": "list", "citation_ids": "list"},
        )

    monkeypatch.setattr(llm, "_structured_call", fake_call)
    sources = [
        {"id": "terms", "kind": "募集说明书", "text": "发行规模10000万元，债券期限3年。"},
        {"id": "finance", "kind": "募集说明书", "text": "营业收入20000万元，净利润1000万元。"},
        {"id": "rating", "kind": "信用评级报告", "text": "主体评级AA-，债项评级AA。"},
        {"id": "guarantee", "kind": "募集说明书", "text": "保证范围覆盖本金和利息，设置反担保质押。"},
    ]
    result, _ = llm.one_shot_baseline({
        "project_type": "bond_guarantee", "company_name": "测试公司", "amount": 10000,
        "term_months": 36, "purpose": "项目建设",
    }, sources)
    rule_keys = {item["key"] for item in captured["policy"]["rules"]}
    assert captured["policy"]["profile"] == "bond-v1"
    assert {"bond_term_consistency", "counter_guarantee", "legal_issuance"} <= rule_keys
    assert {item["id"] for item in captured["sources"]} == {"S01", "S02", "S03", "S04"}
    assert {item["kind"] for item in captured["sources"]} == {"募集说明书", "信用评级报告"}
    assert any("facts不得返回空数组" in item for item in captured["output_rules"])
    assert len(captured["field_registry"]) < len(llm.FACT_REGISTRY)
    assert getattr(result, "baseline_source_count") == 4
    assert getattr(result, "baseline_empty_reason") == "model_returned_empty_facts"
    assert getattr(result, "baseline_model_fact_count") == 0
    assert getattr(result, "baseline_status") == "empty"


def test_bond_baseline_maps_short_source_aliases_back_to_real_ids(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: True)

    def fake_call(_system, prompt, _schema, **kwargs):
        payload = json.loads(prompt)
        alias = payload["sources"][0]["id"]
        assert alias == "S01"
        assert kwargs["allowed_source_ids"] == {"S01"}
        return {
            "facts": [{"fact_key": "bond_issue_amount", "period": "2026", "value": 10000,
                       "unit": "万元", "source_ids": [alias]}],
            "warning_keys": [], "citation_ids": [alias],
        }, llm.LlmUsage(response_field_counts={"facts": 1})

    monkeypatch.setattr(llm, "_structured_call", fake_call)
    result, _ = llm.one_shot_baseline({
        "project_type": "bond_guarantee", "company_name": "测试公司", "amount": 10000,
        "term_months": 36, "purpose": "补流",
    }, [{"id": "real-source-id", "kind": "募集说明书", "text": "发行规模10000万元。"}])

    assert result.facts[0].source_ids == ["real-source-id"]
    assert result.citation_ids == ["real-source-id"]


def test_empty_baseline_output_records_missing_facts(monkeypatch):
    startup()
    expected = bond_evaluation_manifest()["demo-bond-huachen"]
    monkeypatch.setattr(evaluate, "one_shot_baseline", lambda *_args, **_kwargs: (OneShotResult(), llm.LlmUsage()))
    result = evaluate.baseline_case("demo-bond-huachen", expected)
    assert result["status"] == "completed"
    assert result["empty_baseline_output"] is True
    assert result["valid_fact_count"] == 0
    assert result["baseline_status"] == "empty"
    assert result["baseline_fact_keys"] == []
    assert len(result["missing_expected_facts"]) == len(expected["expected_facts"])
    assert result["baseline_empty_reason"] == ""
    assert result["model_finish_reason"] == ""


def test_valid_baseline_records_status_and_fact_keys(monkeypatch):
    startup()
    expected = bond_evaluation_manifest()["demo-bond-huachen"]
    baseline = OneShotResult(
        facts=[
            ExtractedFact(fact_key="bond_issue_amount", period="2026", value=20000, unit="万元", source_ids=["s1"]),
            ExtractedFact(fact_key="revenue", period="2025", value=48000, unit="万元", source_ids=["s2"]),
        ],
        warning_keys=["debt_maturity"],
        citation_ids=["s1", "s2"],
    )
    monkeypatch.setattr(evaluate, "one_shot_baseline", lambda *_args, **_kwargs: (baseline, llm.LlmUsage()))
    result = evaluate.baseline_case("demo-bond-huachen", expected)
    assert result["status"] == "completed"
    assert result["empty_baseline_output"] is False
    assert result["baseline_status"] == "valid"
    assert result["baseline_fact_keys"] == ["bond_issue_amount", "revenue"]
    assert result["valid_fact_count"] == 2
    assert result["matched_fact_count"] >= 1


def test_bond_baseline_filters_unknown_source_ids(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: True)

    def fake_call(_system, prompt, _schema, **kwargs):
        alias = json.loads(prompt)["sources"][0]["id"]
        return {
            "facts": [
                {"fact_key": "bond_issue_amount", "period": "2026", "value": 10000,
                 "unit": "万元", "source_ids": [alias]},
                {"fact_key": "revenue", "period": "2025", "value": 1000,
                 "unit": "万元", "source_ids": ["not-a-real-source"]},
            ],
            "warning_keys": [], "citation_ids": [alias],
        }, llm.LlmUsage(response_field_counts={"facts": 2})

    monkeypatch.setattr(llm, "_structured_call", fake_call)
    result, _ = llm.one_shot_baseline({
        "project_type": "bond_guarantee", "company_name": "测试公司", "amount": 10000,
        "term_months": 36, "purpose": "补流",
    }, [{"id": "real-source-id", "kind": "募集说明书", "text": "发行规模10000万元。"}])

    assert len(result.facts) == 1
    assert result.facts[0].fact_key == "bond_issue_amount"
    assert getattr(result, "baseline_filtered_fact_count") == 1
    assert getattr(result, "baseline_status") == "valid"


def test_bond_baseline_marks_all_filtered_model_facts_invalid(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: True)

    def fake_call(_system, prompt, _schema, **_kwargs):
        alias = json.loads(prompt)["sources"][0]["id"]
        return {
            "facts": [{
                "fact_key": "not_registered", "period": "2026", "value": 1,
                "unit": "万元", "source_ids": [alias],
            }],
            "warning_keys": [], "citation_ids": [alias],
        }, llm.LlmUsage(response_field_counts={"facts": 1})

    monkeypatch.setattr(llm, "_structured_call", fake_call)
    result, _ = llm.one_shot_baseline({
        "project_type": "bond_guarantee", "company_name": "测试公司", "amount": 10000,
        "term_months": 36, "purpose": "补流",
    }, [{"id": "real-source-id", "kind": "募集说明书", "text": "发行规模10000万元。"}])

    assert result.facts == []
    assert getattr(result, "baseline_status") == "invalid"
    assert getattr(result, "baseline_empty_reason") == "validation_filtered_all_facts"


def test_candidate_micro_metrics_negative_false_positive_and_latency_anomaly():
    results = [
        {
            "case": "positive", "status": "completed", "candidate_positive_case": True,
            "candidate_true_positive_count": 1, "candidate_false_positive_count": 0,
            "candidate_false_negative_count": 0, "candidate_recall": 1.0, "candidate_precision": 1.0,
            "duplicate_candidate_count": 0, "chapter_llm_section_count": 8, "chapter_fallback_count": 1,
            "chapter_fallback_rate": 0.125, "latency_anomaly_count": 1,
        },
        {
            "case": "negative-1", "status": "completed", "candidate_positive_case": False,
            "candidate_true_positive_count": 0, "candidate_false_positive_count": 1,
            "candidate_false_negative_count": 0, "candidate_recall": 1.0, "candidate_precision": 0.0,
            "candidate_negative_case_false_positive": True, "duplicate_candidate_count": 1,
            "chapter_llm_section_count": 8, "chapter_fallback_count": 0, "chapter_fallback_rate": 0.0,
            "latency_anomaly_count": 0,
        },
        {
            "case": "negative-2", "status": "completed", "candidate_positive_case": False,
            "candidate_true_positive_count": 0, "candidate_false_positive_count": 0,
            "candidate_false_negative_count": 0, "candidate_recall": 1.0, "candidate_precision": 1.0,
            "candidate_negative_case_false_positive": False, "duplicate_candidate_count": 0,
            "chapter_llm_section_count": 8, "chapter_fallback_count": 0, "chapter_fallback_rate": 0.0,
            "latency_anomaly_count": 0,
        },
    ]
    summary = evaluate.summarize(results)
    assert summary["positive_case_candidate_recall"] == 1.0
    assert summary["negative_case_false_positive_rate"] == 0.5
    assert summary["candidate_micro_precision"] == 0.5
    assert summary["candidate_micro_recall"] == 1.0
    assert summary["duplicate_candidate_total"] == 1
    assert summary["chapter_fallback_rate"] == 0.0417
    assert summary["latency_anomaly_count"] == 1


def test_chapter_attempt_metrics_use_delivery_fields_not_support_status():
    """回退/采用只依据交付一致性字段：validation_status 已是证据支持状态。"""
    sections = [
        # 模型被替换成模板：generation_status=fallback，但证据支持是 passed
        {"section_key": "financial", "generation_status": "fallback", "content_origin": "deterministic",
         "validation_status": "passed", "fallback_stage": "report_support", "delivery_status": "needs_review"},
        # 模型结果被采用
        {"section_key": "rating", "generation_status": "model_passed", "content_origin": "model",
         "validation_status": "passed", "delivery_status": "eligible"},
        # 确定性章节（`bond_chapters.yaml` 中 llm: false）不计入 LLM 统计。
        # 此处曾用 governance，但它在当前配置里已是 llm: true——fixture 与配置脱节，
        # 会让"仅 LLM 章节"的断言按 3 而不是 2 计算（2026-10-18 全量回归发现）。
        {"section_key": "scope", "generation_status": "deterministic", "content_origin": "deterministic",
         "validation_status": "passed", "delivery_status": "eligible"},
    ]
    attempts = [
        {"task_type": "report", "status": "rejected", "error_type": "report_validation", "latency_ms": 1000},
        {"task_type": "candidate", "status": "completed", "error_type": "latency_anomaly", "latency_ms": 91000},
    ]
    metrics = evaluate._chapter_attempt_metrics(sections, attempts)

    assert metrics["chapter_llm_section_count"] == 2          # 仅 LLM 章节
    assert metrics["chapter_model_attempted_count"] == 2
    assert metrics["chapter_model_adopted_count"] == 1        # 只有 rating 真正采用了模型
    assert metrics["chapter_model_adoption_rate"] == 0.5
    assert metrics["chapter_fallback_count"] == 1
    assert metrics["chapter_fallback_rate"] == 0.5
    assert metrics["chapter_post_support_fallback_count"] == 1
    assert metrics["chapter_needs_review_count"] == 1
    assert metrics["chapter_fallback_sections"] == [{
        "section_key": "financial", "title": "", "content_origin": "deterministic",
        "fallback_stage": "report_support", "delivery_status": "needs_review",
        "reason": "未记录具体回退原因",
    }]
    assert metrics["report_validation_attempts"] == 1
    assert metrics["latency_anomaly_count"] == 1
