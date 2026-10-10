"""Validate the three bond-agent V2 pilot checkpoints before the full matrix."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


EXPECTED = {
    "ancheng": ("demo-bond-ancheng", set()),
    "jinghe": ("demo-bond-jinghe", {"governance"}),
    "qualitative": ("demo-bond-qualitative", {"authenticity"}),
}


def _load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    details = payload.get("details") or []
    if len(details) != 1:
        raise ValueError(f"{path}: expected exactly one pilot detail")
    return details[0]


def validate(paths: dict[str, Path]) -> list[str]:
    failures: list[str] = []
    fallback_count = 0
    llm_section_count = 0
    for label, (expected_case, expected_taxonomies) in EXPECTED.items():
        path = paths[label]
        if not path.is_file():
            failures.append(f"{label}: missing checkpoint {path}")
            continue
        try:
            detail = _load(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{label}: invalid checkpoint: {exc}")
            continue
        actual_taxonomies = set(detail.get("predicted_candidate_taxonomies") or detail.get("candidate_taxonomies") or [])
        if detail.get("case") != expected_case:
            failures.append(f"{label}: case={detail.get('case')!r}, expected {expected_case!r}")
        if detail.get("status") != "completed":
            failures.append(f"{label}: status={detail.get('status')!r}")
        if actual_taxonomies != expected_taxonomies:
            failures.append(f"{label}: candidates={sorted(actual_taxonomies)!r}, expected={sorted(expected_taxonomies)!r}")
        for key in ("invalid_citations", "unsupported_numeric_claims", "duplicate_candidate_count", "latency_anomaly_count"):
            value = detail.get(key)
            if not isinstance(value, (int, float)):
                failures.append(f"{label}: {key} is missing")
            elif value != 0:
                failures.append(f"{label}: {key}={value}, expected 0")
        if not isinstance(detail.get("model_cost_cny"), (int, float)):
            failures.append(f"{label}: model_cost_cny is missing")
        elif float(detail["model_cost_cny"]) >= 2:
            failures.append(f"{label}: model_cost_cny={detail.get('model_cost_cny')} is not below 2")
        if float(detail.get("candidate_citation_precision") or 0) < 0.95:
            failures.append(f"{label}: candidate_citation_precision={detail.get('candidate_citation_precision')}")
        if not isinstance(detail.get("report_fact_support_rate"), (int, float)):
            failures.append(f"{label}: report_fact_support_rate is missing")
        elif float(detail["report_fact_support_rate"]) < 0.9:
            failures.append(f"{label}: report_fact_support_rate={detail['report_fact_support_rate']}, expected >=0.9")
        if not isinstance(detail.get("latency_ms"), (int, float)):
            failures.append(f"{label}: latency_ms is missing")
        elif float(detail["latency_ms"]) > 300_000:
            failures.append(f"{label}: latency_ms={detail['latency_ms']}, expected <=300000")
        fallback_count += int(detail.get("chapter_fallback_count") or 0)
        llm_section_count += int(detail.get("chapter_llm_section_count") or 0)
    if llm_section_count == 0:
        failures.append("all pilots: chapter_llm_section_count is 0")
    elif fallback_count / llm_section_count > 0.2:
        failures.append(
            f"all pilots: chapter_fallback_rate={fallback_count / llm_section_count:.4f}, expected <=0.2"
        )
    return failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ancheng", type=Path, default=Path("docs/assets/bond-pilot-v2-ancheng.json"))
    parser.add_argument("--jinghe", type=Path, default=Path("docs/assets/bond-pilot-v2-jinghe.json"))
    parser.add_argument("--qualitative", type=Path, default=Path("docs/assets/bond-pilot-v2-qualitative.json"))
    args = parser.parse_args()
    failures = validate({label: getattr(args, label) for label in EXPECTED})
    if failures:
        print("Pilot gate FAILED")
        print("\n".join(f"- {item}" for item in failures))
        raise SystemExit(1)
    print("Pilot gate PASSED: ancheng=none, jinghe=governance, qualitative=authenticity")


if __name__ == "__main__":
    main()
