"""Validate the one-case V4 bond baseline before spending on a full matrix."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


EXPECTED_CASE = "demo-bond-huachen"
MIN_VALID_FACTS = 5

# 发行条款事实：baseline 至少返回其中一项
TERMS_FACT_KEYS = {
    "bond_name", "registered_issue_amount", "bond_issue_amount", "bond_term_years",
    "coupon_rate", "interest_frequency", "principal_repayment", "bond_maturity_date",
    "bond_exchange",
}
# 财务事实：baseline 至少返回其中一项
FINANCIAL_FACT_KEYS = {
    "revenue", "net_profit", "operating_cash_flow", "total_assets", "total_liabilities",
    "equity", "current_assets", "current_liabilities", "interest_bearing_debt",
    "interest_expense", "accounts_receivable", "total_overdue_receivables", "inventory",
    "debt_due_12m",
}


def validate(path: Path) -> list[str]:
    failures: list[str] = []
    if not path.is_file():
        return [f"missing checkpoint: {path}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid checkpoint: {exc}"]
    details = payload.get("details") or []
    if len(details) != 1:
        return [f"expected exactly one pilot detail, got {len(details)}"]
    detail = details[0]
    if payload.get("mode") != "baseline":
        failures.append(f"mode={payload.get('mode')!r}, expected 'baseline'")
    if detail.get("case") != EXPECTED_CASE:
        failures.append(f"case={detail.get('case')!r}, expected {EXPECTED_CASE!r}")
    if detail.get("status") != "completed":
        failures.append(f"status={detail.get('status')!r}")
    required_diagnostics = (
        "baseline_empty_reason", "baseline_model_fact_count", "model_finish_reason",
        "model_output_tokens", "model_response_field_counts", "baseline_source_char_count",
        "baseline_status", "baseline_fact_keys",
    )
    for key in required_diagnostics:
        if key not in detail:
            failures.append(f"missing diagnostic: {key}")
    if detail.get("empty_baseline_output") is True or detail.get("baseline_status") in {"empty", "invalid"}:
        failures.append(
            "empty output: "
            f"reason={detail.get('baseline_empty_reason') or 'unknown'}, "
            f"model_facts={detail.get('baseline_model_fact_count')}, "
            f"finish_reason={detail.get('model_finish_reason') or 'unknown'}, "
            f"output_tokens={detail.get('model_output_tokens')}, "
            f"response_counts={detail.get('model_response_field_counts')}"
        )
    if int(detail.get("valid_fact_count") or 0) < MIN_VALID_FACTS:
        failures.append(f"valid_fact_count={detail.get('valid_fact_count')}, expected >= {MIN_VALID_FACTS}")
    fact_keys = set(detail.get("baseline_fact_keys") or [])
    if not (fact_keys & TERMS_FACT_KEYS):
        failures.append("缺少发行条款事实（terms），如 bond_issue_amount / bond_term_years")
    if not (fact_keys & FINANCIAL_FACT_KEYS):
        failures.append("缺少财务事实（financial），如 revenue / total_assets")
    if float(detail.get("field_extraction_accuracy") or 0) <= 0:
        failures.append("field_extraction_accuracy must be greater than 0")
    if detail.get("model_finish_reason") == "length":
        failures.append("model_finish_reason=length; output is truncated")
    if float(detail.get("citation_precision") or 0) < 0.95:
        failures.append(f"citation_precision={detail.get('citation_precision')}, expected >=0.95")
    if int(detail.get("invalid_citations") or 0) != 0:
        failures.append(f"invalid_citations={detail.get('invalid_citations')}, expected 0")
    if int(detail.get("filtered_invalid_source_fact_count") or 0) != 0:
        failures.append(
            f"filtered_invalid_source_fact_count={detail.get('filtered_invalid_source_fact_count')}, expected 0"
        )
    return failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", type=Path,
        default=Path("docs/assets/bond-baseline-pilot-v4.json"),
    )
    args = parser.parse_args()
    failures = validate(args.input)
    if failures:
        print("Baseline V4 pilot gate FAILED")
        print("\n".join(f"- {item}" for item in failures))
        raise SystemExit(1)
    print("Baseline V4 pilot gate PASSED: huachen returned cited, non-empty facts")


if __name__ == "__main__":
    main()
