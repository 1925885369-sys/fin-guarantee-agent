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
