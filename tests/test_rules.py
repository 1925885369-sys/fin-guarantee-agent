from backend.app.rules import evaluate_rules, risks_from_checks
from backend.app.sample_case import FACTS


def sample_facts():
    return [
        {"id": fact_id, "name": name, "period": period, "value": value, "source_ids": source_ids}
        for fact_id, _, name, period, value, _, _, source_ids in FACTS
    ]


def test_all_demo_rules_and_risks_are_detected():
    checks = evaluate_rules(sample_facts(), 800)
    risks = risks_from_checks(checks)
    assert len(checks) == 10
    assert len(risks) == 8
    assert sum(item["severity"] == "high" for item in risks) == 6
    assert {item.key for item in checks if item.status == "warning"} == {
        "ar_growth", "cash_profit", "leverage", "customer", "related", "purpose", "collateral", "debt_due",
    }


def test_deterministic_financial_ratios():
    checks = {item.key: item for item in evaluate_rules(sample_facts(), 800)}
    assert checks["leverage"].values[0]["value"] == 68.3
    assert checks["leverage"].values[1]["value"] == 1.17
    assert checks["collateral"].values[0]["value"] == 700
    assert checks["revenue_tax"].status == "passed"
    assert checks["revenue_bank"].status == "passed"


def test_missing_fields_are_insufficient_not_zero_value_risks():
    checks = evaluate_rules([], 500)
    assert len(checks) == 10
    assert all(item.status == "insufficient" for item in checks)
    assert risks_from_checks(checks) == []


def test_rules_use_latest_common_period():
    facts = sample_facts() + [
        {"id": "future-revenue", "name": "营业收入", "period": "2026", "value": 10000, "source_ids": ["future"]},
    ]
    checks = {item.key: item for item in evaluate_rules(facts, 800)}
    assert "2025年" in checks["revenue_tax"].summary

