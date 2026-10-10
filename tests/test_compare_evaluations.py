import json

from scripts.check_bond_pilots import validate as validate_pilots
from scripts.check_bond_baseline_pilot import validate as validate_baseline_pilot
from scripts.compare_evaluations import _effective_average_cost, comparison


def _payload(field_accuracy: float, risk_recall: float, total_cost: float, latency_ms: float) -> dict:
    summary = {
        "runs": 2,
        "total_cost_cny": total_cost,
        "field_extraction_accuracy": field_accuracy,
        "risk_recall": risk_recall,
        "risk_precision": risk_recall,
        "risk_f1": risk_recall,
        "latency_ms": latency_ms,
        "per_case_stability": {},
    }
    return {
        "summary": summary,
        "details": [
            {"case": "demo", "status": "completed", "field_extraction_accuracy": field_accuracy, "risk_recall": risk_recall},
            {"case": "demo", "status": "completed", "field_extraction_accuracy": field_accuracy, "risk_recall": risk_recall},
        ],
    }


def test_comparison_uses_total_cost_including_prior_failed_attempts():
    agent = _payload(1.0, 1.0, 2.0, 100)
    baseline = _payload(0.9, 0.7, 1.0, 20)

    report = comparison(agent, baseline, agent)

    assert _effective_average_cost(agent) == 1.0
    assert "有效平均成本(元，含失败重试)" in report
    assert "Agent 加固后 vs Baseline" in report
    assert "+10.0 个百分点" in report
    assert "5.00 倍" in report
    assert "2.00 倍" in report


def test_comparison_marks_empty_baseline_as_diagnostic_only():
    agent = _payload(1.0, 1.0, 2.0, 100)
    baseline = _payload(0.0, 0.0, 1.0, 20)
    for item in baseline["details"]:
        item["empty_baseline_output"] = True

    report = comparison(agent, baseline, agent)

    assert "Baseline 有效性警告" in report
    assert "不作为有效事实抽取或规则能力对照" in report


def test_bond_pilot_gate_requires_expected_taxonomies(tmp_path):
    base = {
        "status": "completed", "invalid_citations": 0, "unsupported_numeric_claims": 0,
        "duplicate_candidate_count": 0, "latency_anomaly_count": 0,
        "candidate_citation_precision": 1.0, "model_cost_cny": 0.2,
        "report_fact_support_rate": 1.0, "latency_ms": 120_000,
        "chapter_fallback_count": 0, "chapter_llm_section_count": 8,
    }
    paths = {}
    for label, case, taxonomies in (
        ("ancheng", "demo-bond-ancheng", []),
        ("jinghe", "demo-bond-jinghe", ["governance"]),
        ("qualitative", "demo-bond-qualitative", ["authenticity"]),
    ):
        path = tmp_path / f"{label}.json"
        path.write_text(json.dumps({"details": [{**base, "case": case, "predicted_candidate_taxonomies": taxonomies}]}), encoding="utf-8")
        paths[label] = path
    assert validate_pilots(paths) == []
    paths["qualitative"].write_text(json.dumps({"details": [{**base, "case": "demo-bond-qualitative", "predicted_candidate_taxonomies": []}]}), encoding="utf-8")
    assert validate_pilots(paths)


def test_bond_pilot_gate_enforces_aggregate_chapter_fallback_rate(tmp_path):
    paths = {}
    for label, case, taxonomies in (
        ("ancheng", "demo-bond-ancheng", []),
        ("jinghe", "demo-bond-jinghe", ["governance"]),
        ("qualitative", "demo-bond-qualitative", ["authenticity"]),
    ):
        path = tmp_path / f"{label}.json"
        path.write_text(json.dumps({"details": [{
            "case": case, "status": "completed", "predicted_candidate_taxonomies": taxonomies,
            "invalid_citations": 0, "unsupported_numeric_claims": 0,
            "duplicate_candidate_count": 0, "latency_anomaly_count": 0,
            "candidate_citation_precision": 1.0, "model_cost_cny": 0.2,
            "report_fact_support_rate": 1.0, "latency_ms": 120_000,
            "chapter_fallback_count": 2, "chapter_llm_section_count": 8,
        }]}), encoding="utf-8")
        paths[label] = path

    assert any("chapter_fallback_rate" in item for item in validate_pilots(paths))


def test_bond_baseline_v4_pilot_gate_reports_empty_output_diagnostics(tmp_path):
    path = tmp_path / "baseline.json"
    base = {
        "case": "demo-bond-huachen", "status": "completed",
        "valid_fact_count": 12, "field_extraction_accuracy": 0.3,
        "risk_recall": 0.2,
        "citation_precision": 1.0, "filtered_invalid_source_fact_count": 0,
        "baseline_empty_reason": "", "baseline_model_fact_count": 12,
        "model_finish_reason": "stop", "model_output_tokens": 500,
        "model_response_field_counts": {"facts": 12}, "baseline_source_char_count": 5000,
        "empty_baseline_output": False, "baseline_status": "valid",
        "baseline_fact_keys": ["bond_issue_amount", "revenue", "total_assets"],
    }
    path.write_text(json.dumps({"mode": "baseline", "details": [base]}), encoding="utf-8")
    assert validate_baseline_pilot(path) == []

    missing_keys = {
        **base, "baseline_fact_keys": ["revenue"],
    }
    path.write_text(json.dumps({"mode": "baseline", "details": [missing_keys]}), encoding="utf-8")
    failures = validate_baseline_pilot(path)
    assert any("发行条款" in item for item in failures)

    empty = {
        **base, "valid_fact_count": 0, "field_extraction_accuracy": 0,
        "citation_precision": 0, "baseline_empty_reason": "model_returned_empty_facts",
        "baseline_model_fact_count": 0, "model_output_tokens": 12,
        "model_response_field_counts": {"facts": 0}, "empty_baseline_output": True,
        "baseline_status": "empty", "baseline_fact_keys": [],
    }
    path.write_text(json.dumps({"mode": "baseline", "details": [empty]}), encoding="utf-8")
    failures = validate_baseline_pilot(path)
    assert any("model_returned_empty_facts" in item for item in failures)
    assert any("valid_fact_count" in item for item in failures)


def test_bond_baseline_v4_pilot_gate_does_not_block_low_risk_recall(tmp_path):
    """基线 gate 固定要求为事实数量、字段覆盖、引用与来源有效性；规则召回不足由对比阶段诊断。"""
    path = tmp_path / "baseline.json"
    detail = {
        "case": "demo-bond-huachen", "status": "completed",
        "valid_fact_count": 12, "field_extraction_accuracy": 1.0,
        "risk_recall": 0.0, "citation_precision": 1.0,
        "filtered_invalid_source_fact_count": 0, "invalid_citations": 0,
        "baseline_empty_reason": "", "baseline_model_fact_count": 12,
        "model_finish_reason": "stop", "model_output_tokens": 500,
        "model_response_field_counts": {"facts": 12}, "baseline_source_char_count": 5000,
        "empty_baseline_output": False, "baseline_status": "valid",
        "baseline_fact_keys": ["bond_issue_amount", "revenue"],
    }
    path.write_text(json.dumps({"mode": "baseline", "details": [detail]}), encoding="utf-8")
    assert validate_baseline_pilot(path) == []
