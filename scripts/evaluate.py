from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import statistics
import time
import uuid
from pathlib import Path

import httpx

from backend.app.config import settings
from backend.app.chapter_prompts import chapter_uses_llm, prompt_config_payload, prompt_config_sha256
from backend.app.database import connect, json_load, now_iso, row, rows
from backend.app.facts import FACT_REGISTRY, canonical_key, deterministic_extract, material_inventory, merge_facts
from backend.app.llm import (
    BASELINE_SYSTEM_PROMPT,
    CANDIDATE_SYSTEM_PROMPT,
    CANDIDATE_EVIDENCE_TERMS,
    CANDIDATE_TAXONOMY_BOUNDARIES,
    EXTRACT_SYSTEM_PROMPT,
    REPORT_CITATION_RULES,
    SYSTEM_PROMPT,
    deterministic_governance_candidates,
    extract_facts,
    is_configured,
    one_shot_baseline,
)
from backend.app.main import startup
from backend.app.p0 import report_quality
from backend.app.bond_case import PROJECT_ID as BOND_PROJECT_ID, _materials as generate_flagship_bond_materials
from backend.app.bond_portfolio_cases import bond_evaluation_manifest, generate_bond_portfolio_materials
from backend.app.policy import load_policy
from backend.app.professional import canonical_fact_rows, fact_groups, sync_fact_assertions
from backend.app.workflow import _normalize_model_fact, start_run
from backend.app.bond_rules import evaluate_bond_rules


BOND_EVALUATION_VERSION = "bond-professional-v3.2"
STANDARD_ERRORS = {
    "transport", "rate_limit", "authentication", "model_not_found", "invalid_json", "schema_error",
    "invalid_source", "scope_error", "hallucination", "candidate_miss", "report_validation",
    "budget_exhausted", "unknown",
}


def _sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluation_metadata(suite: str = "bond") -> dict:
    policy = load_policy("bond-v1")
    evaluation_version = BOND_EVALUATION_VERSION
    registry = {key: value.label for key, value in FACT_REGISTRY.items()}
    prompts = {
        "extract": EXTRACT_SYSTEM_PROMPT,
        "candidate": CANDIDATE_SYSTEM_PROMPT,
        "candidate_taxonomy_boundaries": CANDIDATE_TAXONOMY_BOUNDARIES,
        "candidate_evidence_terms": CANDIDATE_EVIDENCE_TERMS,
        "report": SYSTEM_PROMPT,
        "report_citation_rules": REPORT_CITATION_RULES,
        "chapter_prompts": prompt_config_payload(),
        "baseline": BASELINE_SYSTEM_PROMPT,
    }
    result = {
        "evaluation_version": evaluation_version,
        "temperature": 0.2,
        "prompt_version": evaluation_version,
        "prompt_sha256": _sha256(prompts),
        "chapter_prompt_sha256": prompt_config_sha256(),
        "field_registry_sha256": _sha256(registry),
        "policy_profile": policy.profile,
        "policy_version": policy.version,
        "policy_sha256": policy.sha256,
    }
    return result


def evaluation_key(mode: str, case: str, iteration: int, model: str, metadata: dict) -> str:
    identity = [mode, case, iteration, model, metadata["prompt_version"], metadata["policy_sha256"]]
    return _sha256(identity)


def classify_error(exc: Exception) -> str:
    typed = getattr(exc, "error_type", "")
    if typed in STANDARD_ERRORS:
        return typed
    message = str(exc).lower()
    for error_type in STANDARD_ERRORS:
        if error_type in message:
            return error_type
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429 or "rate limit" in message or "限流" in message:
        return "rate_limit"
    if status in {401, 403} or "api key" in message or "authentication" in message or "unauthorized" in message:
        return "authentication"
    if status == 404 or "model_not_found" in message or "model not found" in message or "模型不存在" in message:
        return "model_not_found"
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, TimeoutError)):
        return "transport"
    if isinstance(status, int) and status >= 500:
        return "transport"
    if "json" in message:
        return "invalid_json"
    if "validation" in message or "schema" in message or exc.__class__.__name__ == "ValidationError":
        return "schema_error"
    if "source_id" in message or "source id" in message or "无效引用" in message:
        return "invalid_source"
    if "主体" in message or "scope" in message or "口径" in message:
        return "scope_error"
    if "新增数字" in message or "无证据" in message or "hallucination" in message:
        return "hallucination"
    if "候选" in message and ("缺失" in message or "miss" in message):
        return "candidate_miss"
    if "报告" in message or "主张" in message or "引用" in message:
        return "report_validation"
    return "unknown"


HUACHEN_WARNINGS = [
    "ar_growth", "overdue_receivables", "cash_profit", "leverage", "dscr", "customer_concentration",
    "related_occupancy", "purpose_coverage", "collateral", "debt_maturity",
]


def manifest(suite: str = "bond") -> dict:
    """评测清单。产品只做债券担保，因此只有债券套件。"""
    return bond_evaluation_manifest()


def _wait(run_id: str) -> dict:
    while True:
        run = row("SELECT * FROM runs WHERE id=?", (run_id,))
        if run["status"] in {"completed", "failed", "cancelled", "model_required"}:
            return run
        time.sleep(0.1)


def _fact_extraction_accuracy(facts: list[dict], expected_facts: list[dict]) -> tuple[float, int]:
    matched = 0
    for expected in expected_facts:
        for fact in facts:
            if fact.get("fact_key") != expected["fact_key"]:
                continue
            expected_period = str(expected.get("period") or "")
            actual_period = str(fact.get("period") or "")
            if expected_period and actual_period and expected_period != actual_period:
                continue
            expected_value = expected.get("value")
            actual_value = fact.get("value")
            if expected_value is None:
                if actual_value is not None:
                    continue
            elif actual_value is None or abs(float(actual_value) - float(expected_value)) > max(0.01, abs(float(expected_value)) * 0.001):
                continue
            matched += 1
            break
    return round(matched / max(len(expected_facts), 1), 4), matched


def _risk_scores(predicted: set[str], expected: set[str]) -> tuple[float, float, float]:
    if not expected:
        return (1.0, 1.0, 1.0) if not predicted else (1.0, 0.0, 0.0)
    true_positives = len(predicted & expected)
    recall = true_positives / max(len(expected), 1)
    precision = true_positives / len(predicted) if predicted else (1.0 if not expected else 0.0)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return round(recall, 4), round(precision, 4), round(f1, 4)


def _candidate_counts(predicted: set[str], expected: set[str]) -> tuple[int, int, int]:
    return len(predicted & expected), len(predicted - expected), len(expected - predicted)


def _duplicate_candidate_count(candidates: list[dict]) -> int:
    unique: list[tuple[str, str, set[str]]] = []
    duplicates = 0
    for candidate in candidates:
        taxonomy = str(candidate.get("taxonomy") or "")
        title = "".join(str(candidate.get("title") or "").split()).lower()
        support = candidate.get("support_source_ids", [])
        if isinstance(support, str):
            support = json_load(support)
        support_set = {str(item) for item in support}
        matched = any(
            taxonomy == existing_taxonomy
            and ((title and title == existing_title) or bool(support_set & existing_support))
            for existing_taxonomy, existing_title, existing_support in unique
        )
        if matched:
            duplicates += 1
        else:
            unique.append((taxonomy, title, support_set))
    return duplicates


def _chapter_attempt_metrics(sections: list[dict], attempts: list[dict]) -> dict:
    """章节模型采用/回退指标。

    只依据 `generation_status` / `content_origin` / `fallback_stage` / `delivery_status`
    这些**交付一致性字段**——`validation_status` 自状态分离后表示证据支持状态，
    再用它推算回退会把"模型被替换成模板但模板有证据支持"的章节误判成采用成功。
    """
    llm_sections = [item for item in sections if chapter_uses_llm(item.get("section_key", ""))]
    attempted = [item for item in llm_sections if item.get("generation_status") in {"model_passed", "fallback"}]
    adopted = [
        item for item in attempted
        if item.get("generation_status") == "model_passed"
        and item.get("content_origin") == "model"
        and item.get("delivery_status") in {"eligible", "pending"}
    ]
    fallback_sections = [
        item for item in llm_sections
        if item.get("generation_status") == "fallback" or item.get("content_origin") == "deterministic"
    ]
    fallback_details = [
        {
            "section_key": item.get("section_key", ""),
            "title": item.get("title", ""),
            "content_origin": item.get("content_origin", ""),
            "fallback_stage": item.get("fallback_stage", ""),
            "delivery_status": item.get("delivery_status", ""),
            "reason": item.get("fallback_reason") or "未记录具体回退原因",
        }
        for item in fallback_sections
    ]
    fallback_reason_counts: dict[str, int] = {}
    for item in fallback_details:
        reason = item["reason"]
        fallback_reason_counts[reason] = fallback_reason_counts.get(reason, 0) + 1
    report_attempts = [item for item in attempts if item.get("task_type") == "report"]
    validation_attempts = [
        item for item in report_attempts
        if item.get("status") == "rejected" or item.get("error_type") == "report_validation"
    ]
    anomaly_threshold_ms = int(settings.llm_latency_anomaly_seconds * 1000)
    anomalies = [
        item for item in attempts
        if item.get("error_type") == "latency_anomaly" or int(item.get("latency_ms") or 0) > anomaly_threshold_ms
    ]
    return {
        "chapter_llm_section_count": len(llm_sections),
        "chapter_model_attempted_count": len(attempted),
        "chapter_model_adopted_count": len(adopted),
        "chapter_model_adoption_rate": round(len(adopted) / max(len(attempted), 1), 4),
        "chapter_fallback_count": len(fallback_sections),
        "chapter_fallback_rate": round(len(fallback_sections) / max(len(llm_sections), 1), 4),
        "chapter_fallback_sections": fallback_details,
        "chapter_fallback_reason_counts": fallback_reason_counts,
        "chapter_post_support_fallback_count": len(
            [item for item in llm_sections if item.get("fallback_stage") == "report_support"]
        ),
        "chapter_needs_review_count": len(
            [item for item in llm_sections if item.get("delivery_status") == "needs_review"]
        ),
        "chapter_disclosure_missing_count": len(
            [item for item in llm_sections if item.get("disclosure_status") == "warning"]
        ),
        "report_attempts": len(report_attempts),
        "report_validation_attempts": len(validation_attempts),
        "latency_anomaly_count": len(anomalies),
    }


def _fact_descriptor(fact: dict) -> dict:
    return {
        "fact_key": fact.get("fact_key", ""), "period": str(fact.get("period") or ""),
        "value": fact.get("value"), "unit": fact.get("unit", ""),
        "source_ids": json_load(fact["source_ids"]) if isinstance(fact.get("source_ids"), str) else fact.get("source_ids", []),
    }


def _matches_expected_fact(fact: dict, expected: dict) -> bool:
    _, matched = _fact_extraction_accuracy([fact], [expected])
    return bool(matched)


def _metrics(project_id: str, expected: dict, run: dict) -> dict:
    facts = rows("SELECT * FROM facts WHERE project_id=?", (project_id,))
    checks = rows("SELECT * FROM cross_checks WHERE project_id=?", (project_id,))
    sections = rows("SELECT * FROM report_sections WHERE project_id=?", (project_id,))
    citations = rows("SELECT * FROM citations WHERE project_id=?", (project_id,))
    source_ids = {item["id"] for item in rows("SELECT id FROM sources WHERE project_id=?", (project_id,))}
    candidates = rows("SELECT * FROM candidate_findings WHERE project_id=?", (project_id,))
    quality = report_quality(project_id)
    conflicts = rows("SELECT fact_key,period FROM canonical_facts WHERE project_id=? AND status='conflicted'", (project_id,))
    attempts = rows(
        "SELECT node_key,task_type,attempt,status,input_tokens,output_tokens,cost,latency_ms,error_type,error_message "
        "FROM llm_attempts WHERE run_id=? ORDER BY created_at",
        (run["id"],),
    )
    node_rows = rows("SELECT status,attempt FROM run_nodes WHERE run_id=?", (run["id"],))
    predicted_warnings = {item["rule_key"] for item in checks if item["status"] == "warning"}
    expected_warnings = set(expected["expected_warning_keys"])
    valid_citations = sum(item["source_id"] in source_ids for item in citations)
    extraction_accuracy, matched_facts = _fact_extraction_accuracy(facts, expected["expected_facts"])
    risk_recall, risk_precision, risk_f1 = _risk_scores(predicted_warnings, expected_warnings)
    predicted_candidates = {item["taxonomy"] for item in candidates}
    expected_candidates = set(expected.get("expected_candidate_taxonomies", []))
    candidate_recall, candidate_precision, candidate_f1 = _risk_scores(predicted_candidates, expected_candidates)
    candidate_tp, candidate_fp, candidate_fn = _candidate_counts(predicted_candidates, expected_candidates)
    chapter_metrics = _chapter_attempt_metrics(sections, attempts)
    candidate_source_ids = [source_id for item in candidates for source_id in json_load(item["support_source_ids"])]
    run_error = run.get("error") or ""
    failed_attempts = [item for item in attempts if item["status"] in {"failed", "rejected"}]
    result = {
        "status": run["status"],
        "field_extraction_accuracy": extraction_accuracy,
        "matched_fact_count": matched_facts,
        "expected_fact_count": len(expected["expected_facts"]),
        "extracted_fact_count": len(facts),
        "risk_recall": risk_recall,
        "risk_precision": risk_precision,
        "risk_f1": risk_f1,
        "candidate_recall": candidate_recall,
        "candidate_precision": candidate_precision,
        "candidate_f1": candidate_f1,
        "expected_candidate_taxonomies": sorted(expected_candidates),
        "predicted_candidate_taxonomies": sorted(predicted_candidates),
        "candidate_true_positive_count": candidate_tp,
        "candidate_false_positive_count": candidate_fp,
        "candidate_false_negative_count": candidate_fn,
        "candidate_positive_case": bool(expected_candidates),
        "candidate_negative_case_false_positive": not expected_candidates and bool(predicted_candidates),
        "duplicate_candidate_count": _duplicate_candidate_count(candidates),
        "candidate_citation_precision": round(sum(item in source_ids for item in candidate_source_ids) / len(candidate_source_ids), 4) if candidate_source_ids else 1.0,
        "citation_precision": round(valid_citations / max(len(citations), 1), 4),
        "section_citation_coverage": quality["factual_section_citation_coverage"],
        "report_fact_support_rate": quality["support_rate"],
        "unsupported_claims": quality["unsupported_claims"],
        "unsupported_numeric_claims": sum(bool(json_load(item["unsupported_numbers"])) for item in rows("SELECT unsupported_numbers FROM report_claims WHERE project_id=?", (project_id,))),
        "invalid_citations": quality["invalid_citations"],
        "conflict_recall": round(sum(any(group["fact_key"] == item["fact_key"] and group["period"] == item["period"] for group in conflicts) for item in expected.get("expected_conflicts", [])) / max(len(expected.get("expected_conflicts", [])), 1), 4) if expected.get("expected_conflicts") else 1.0,
        "subject_scope_error_count": row("SELECT COUNT(*) count FROM canonical_facts WHERE project_id=? AND (subject_id='' OR scope='unknown')", (project_id,))["count"],
        "model_attempts": len(attempts),
        "model_attempt_error_counts": {
            error_type: sum(item["error_type"] == error_type for item in failed_attempts)
            for error_type in sorted({item["error_type"] for item in failed_attempts if item["error_type"]})
        },
        "last_model_error": failed_attempts[-1]["error_message"] if failed_attempts else "",
        "model_attempt_summaries": attempts,
        "node_retries": sum(max(0, int(item["attempt"]) - 1) for item in node_rows),
        "degraded_nodes": sum(item["status"] == "degraded" for item in node_rows),
        **chapter_metrics,
        "latency_ms": run["latency_ms"],
        "model_cost_cny": round(run["cost"], 6),
        "predicted_warning_keys": sorted(predicted_warnings),
        "missed_warning_keys": sorted(expected_warnings - predicted_warnings),
        "unexpected_warning_keys": sorted(predicted_warnings - expected_warnings),
        "missing_expected_facts": [
            item for item in expected["expected_facts"]
            if not any(_matches_expected_fact(fact, item) for fact in facts)
        ],
        "candidate_taxonomies": sorted(predicted_candidates),
        "candidate_findings": [
            {
                "taxonomy": item["taxonomy"], "title": item["title"],
                "support_source_ids": json_load(item["support_source_ids"]),
                "counter_source_ids": json_load(item["counter_source_ids"]),
            }
            for item in candidates
        ],
        "conflicted_facts": [
            {"fact_key": item["fact_key"], "period": item["period"]}
            for item in conflicts
        ],
        "error_type": classify_error(RuntimeError(run_error)) if run["status"] == "failed" else None,
        "error_message": run_error[:500] if run["status"] == "failed" else "",
    }
    node_status = {item["node_key"]: item["status"] for item in rows("SELECT node_key,status FROM run_nodes WHERE run_id=?", (run["id"],))}
    if node_status.get("candidate") != "completed":
        for key in ("candidate_recall", "candidate_precision", "candidate_f1", "candidate_citation_precision"):
            result[key] = None
        for key in ("candidate_true_positive_count", "candidate_false_positive_count", "candidate_false_negative_count"):
            result[key] = None
        result["candidate_positive_case"] = None
        result["candidate_negative_case_false_positive"] = None
    # A warning verify node still produced a report and its quality metrics are meaningful.
    # Only skip report metrics when verification did not execute at all.
    if node_status.get("verify") not in {"completed", "warning"}:
        for key in ("citation_precision", "section_citation_coverage", "report_fact_support_rate", "unsupported_claims", "unsupported_numeric_claims", "invalid_citations"):
            result[key] = None
    return result


def _api_agent_case(api_base: str, source_project_id: str, expected: dict) -> dict:
    material_map = {
        item["id"]: item["path"]
        for item in (
            generate_demo_materials()
            + generate_portfolio_materials()
            + generate_flagship_bond_materials(settings.resolved_data_dir / "demo_materials" / BOND_PROJECT_ID)
            + generate_bond_portfolio_materials()
        )
    }
    with httpx.Client(base_url=api_base.rstrip("/"), timeout=120) as client:
        source_project = client.get(f"/api/projects/{source_project_id}").raise_for_status().json()
        inventory = client.get(f"/api/projects/{source_project_id}/document-inventory").raise_for_status().json()
        created = client.post("/api/projects", json={
            "company_name": f"{source_project['company_name']}（模型评测）",
            "amount": source_project["amount"],
            "term_months": source_project["term_months"],
            "purpose": source_project["purpose"],
            "project_type": source_project.get("project_type", "loan_guarantee"),
            "bond_name": source_project.get("bond_name", ""),
            "data_consent": True,
        }).raise_for_status().json()
        project_id = created["id"]
        headers = {"X-Project-Token": created["accessToken"]}
        for document in inventory["documents"]:
            path = Path(material_map[document["id"]])
            media_type = document.get("media_type") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            with path.open("rb") as stream:
                client.post(
                    f"/api/projects/{project_id}/documents",
                    headers=headers,
                    files={"file": (path.name, stream, media_type)},
                ).raise_for_status()
        run_id = client.post(
            f"/api/projects/{project_id}/runs", headers=headers, json={"focus": "全面尽调"},
        ).raise_for_status().json()["id"]
        deadline = time.monotonic() + 300
        while True:
            run = client.get(f"/api/runs/{run_id}", headers=headers).raise_for_status().json()
            if run["status"] in {"completed", "failed", "cancelled", "model_required"}:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"评测任务 {run_id} 超过 5 分钟")
            time.sleep(1)
        facts = client.get(f"/api/projects/{project_id}/facts", headers=headers).raise_for_status().json()
        checks = client.get(f"/api/projects/{project_id}/cross-checks", headers=headers).raise_for_status().json()
        sections = client.get(f"/api/projects/{project_id}/report", headers=headers).raise_for_status().json()
        candidates = client.get(f"/api/projects/{project_id}/candidate-findings", headers=headers).raise_for_status().json()
        quality = client.get(f"/api/projects/{project_id}/report-quality", headers=headers).raise_for_status().json()
        groups = client.get(f"/api/projects/{project_id}/fact-groups", headers=headers).raise_for_status().json()
        nodes = client.get(f"/api/runs/{run_id}/nodes", headers=headers).raise_for_status().json()
        attempts = client.get(f"/api/runs/{run_id}/attempts", headers=headers).raise_for_status().json()
        citation_ids = {citation_id for section in sections for citation_id in section.get("citation_ids", [])}
        valid_citations = sum(
            client.get(f"/api/citations/{citation_id}", headers=headers).is_success
            for citation_id in citation_ids
        )
        candidate_source_ids = [source_id for item in candidates for source_id in item.get("support_source_ids", [])]
        valid_candidate_citations = sum(
            client.get(f"/api/citations/c-{source_id}", headers=headers).is_success
            for source_id in candidate_source_ids
        )

    predicted_warnings = {item["rule_key"] for item in checks if item["status"] == "warning"}
    expected_warnings = set(expected["expected_warning_keys"])
    extraction_accuracy, matched_facts = _fact_extraction_accuracy(facts, expected["expected_facts"])
    risk_recall, risk_precision, risk_f1 = _risk_scores(predicted_warnings, expected_warnings)
    predicted_candidates = {item["taxonomy"] for item in candidates}
    expected_candidates = set(expected.get("expected_candidate_taxonomies", []))
    candidate_recall, candidate_precision, candidate_f1 = _risk_scores(predicted_candidates, expected_candidates)
    candidate_tp, candidate_fp, candidate_fn = _candidate_counts(predicted_candidates, expected_candidates)
    chapter_metrics = _chapter_attempt_metrics(sections, attempts)
    result = {
        "status": run["status"],
        "field_extraction_accuracy": extraction_accuracy,
        "matched_fact_count": matched_facts,
        "expected_fact_count": len(expected["expected_facts"]),
        "extracted_fact_count": len(facts),
        "risk_recall": risk_recall,
        "risk_precision": risk_precision,
        "risk_f1": risk_f1,
        "candidate_recall": candidate_recall,
        "candidate_precision": candidate_precision,
        "candidate_f1": candidate_f1,
        "expected_candidate_taxonomies": sorted(expected_candidates),
        "predicted_candidate_taxonomies": sorted(predicted_candidates),
        "candidate_true_positive_count": candidate_tp,
        "candidate_false_positive_count": candidate_fp,
        "candidate_false_negative_count": candidate_fn,
        "candidate_positive_case": bool(expected_candidates),
        "candidate_negative_case_false_positive": not expected_candidates and bool(predicted_candidates),
        "duplicate_candidate_count": _duplicate_candidate_count(candidates),
        "candidate_citation_precision": round(valid_candidate_citations / len(candidate_source_ids), 4) if candidate_source_ids else 1.0,
        "citation_precision": round(valid_citations / max(len(citation_ids), 1), 4),
        "section_citation_coverage": quality["factual_section_citation_coverage"],
        "report_fact_support_rate": quality["support_rate"],
        "unsupported_claims": quality["unsupported_claims"],
        "unsupported_numeric_claims": quality.get("unsupported_numeric_claims", 0),
        "invalid_citations": quality["invalid_citations"],
        "conflict_recall": round(sum(any(group["fact_key"] == item["fact_key"] and group["period"] == item["period"] and group["status"] == "conflicted" for group in groups) for item in expected.get("expected_conflicts", [])) / max(len(expected.get("expected_conflicts", [])), 1), 4) if expected.get("expected_conflicts") else 1.0,
        "subject_scope_error_count": sum(not item.get("subject_id") or item.get("scope") == "unknown" for item in groups),
        "node_retries": sum(max(0, int(item.get("attempt", 1)) - 1) for item in nodes),
        "degraded_nodes": sum(item.get("status") == "degraded" for item in nodes),
        **chapter_metrics,
        "latency_ms": run["latency_ms"],
        "model_cost_cny": round(run["cost"], 6),
        "predicted_warning_keys": sorted(predicted_warnings),
        "candidate_findings": [
            {
                "taxonomy": item["taxonomy"], "title": item["title"],
                "support_source_ids": item.get("support_source_ids", []),
                "counter_source_ids": item.get("counter_source_ids", []),
            }
            for item in candidates
        ],
        "conflicted_facts": [
            {"fact_key": item["fact_key"], "period": item["period"]}
            for item in groups if item["status"] == "conflicted"
        ],
        "project_id": project_id,
        "error_type": classify_error(RuntimeError(run.get("error") or "")) if run["status"] == "failed" else None,
        "error_message": (run.get("error") or "")[:500] if run["status"] == "failed" else "",
    }
    node_status = {item["node_key"]: item["status"] for item in nodes}
    if node_status.get("candidate") != "completed":
        for key in ("candidate_recall", "candidate_precision", "candidate_f1", "candidate_citation_precision"):
            result[key] = None
        for key in ("candidate_true_positive_count", "candidate_false_positive_count", "candidate_false_negative_count"):
            result[key] = None
        result["candidate_positive_case"] = None
        result["candidate_negative_case_false_positive"] = None
    if node_status.get("verify") not in {"completed", "warning"}:
        for key in ("citation_precision", "section_citation_coverage", "report_fact_support_rate", "unsupported_claims", "unsupported_numeric_claims", "invalid_citations"):
            result[key] = None
    return result


def deterministic_case(source_project_id: str, expected: dict) -> dict:
    started = time.perf_counter()
    project_id = _clone_project(source_project_id)
    try:
        project = row("SELECT * FROM projects WHERE id=?", (project_id,))
        source_rows = rows(
            "SELECT s.*,d.kind FROM sources s JOIN documents d ON d.id=s.document_id WHERE s.project_id=?",
            (project_id,),
        )
        extracted = deterministic_extract(source_rows)
        sync_fact_assertions(project_id, extracted)
        groups = fact_groups(project_id)
        conflicts = [item for item in groups if item["status"] == "conflicted"]
        facts_for_rules = canonical_fact_rows(project_id)
        inventory = material_inventory(project_id)
        checks = evaluate_bond_rules(
            facts_for_rules, float(project["amount"]), policy=load_policy("bond-v1"),
            material_gaps=inventory["gaps"], conflicts=conflicts,
        )
        predicted = {item.key for item in checks if item.status == "warning"}
        expected_warnings = set(expected["expected_warning_keys"])
        extraction_accuracy, matched = _fact_extraction_accuracy(extracted, expected["expected_facts"])
        recall, precision, f1 = _risk_scores(predicted, expected_warnings)
        candidates = deterministic_governance_candidates(source_rows)
        predicted_candidates = {item["taxonomy"] for item in candidates}
        expected_candidates = set(expected.get("expected_candidate_taxonomies", []))
        candidate_recall, candidate_precision, candidate_f1 = _risk_scores(
            predicted_candidates, expected_candidates,
        )
        candidate_tp, candidate_fp, candidate_fn = _candidate_counts(predicted_candidates, expected_candidates)
        return {
            "status": "completed", "field_extraction_accuracy": extraction_accuracy,
            "matched_fact_count": matched, "expected_fact_count": len(expected["expected_facts"]),
            "extracted_fact_count": len(extracted), "risk_recall": recall, "risk_precision": precision,
            "risk_f1": f1, "citation_precision": 1.0, "section_citation_coverage": None,
            "candidate_recall": candidate_recall, "candidate_precision": candidate_precision,
            "candidate_f1": candidate_f1, "candidate_citation_precision": 1.0,
            "expected_candidate_taxonomies": sorted(expected_candidates),
            "predicted_candidate_taxonomies": sorted(predicted_candidates),
            "candidate_true_positive_count": candidate_tp,
            "candidate_false_positive_count": candidate_fp,
            "candidate_false_negative_count": candidate_fn,
            "candidate_positive_case": bool(expected_candidates),
            "candidate_negative_case_false_positive": not expected_candidates and bool(predicted_candidates),
            "duplicate_candidate_count": _duplicate_candidate_count(candidates),
            "chapter_llm_section_count": 0, "chapter_fallback_count": 0,
            "chapter_fallback_rate": 0.0, "latency_anomaly_count": 0,
            "unsupported_numeric_claims": 0, "latency_ms": int((time.perf_counter() - started) * 1000),
            "model_cost_cny": 0.0, "predicted_warning_keys": sorted(predicted),
            "missing_expected_facts": [
                item for item in expected["expected_facts"]
                if not any(_matches_expected_fact(fact, item) for fact in extracted)
            ],
            "conflict_recall": round(sum(any(group["fact_key"] == item["fact_key"] and group["period"] == item["period"] for group in conflicts) for item in expected.get("expected_conflicts", [])) / max(len(expected.get("expected_conflicts", [])), 1), 4) if expected.get("expected_conflicts") else 1.0,
        }
    finally:
        _cleanup_clone(project_id)


def _clone_project(source_project_id: str) -> str:
    source_project = row("SELECT * FROM projects WHERE id=?", (source_project_id,))
    project_id = f"eval-{uuid.uuid4()}"
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,project_type,bond_name,consent_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, uuid.uuid4().hex, source_project["company_name"], source_project["amount"], source_project["term_months"],
             source_project["purpose"], "ready", now, now, source_project.get("project_type", "loan_guarantee"), source_project.get("bond_name", ""), now),
        )
        document_map = {}
        for document in rows("SELECT * FROM documents WHERE project_id=?", (source_project_id,)):
            new_id = str(uuid.uuid4())
            document_map[document["id"]] = new_id
            conn.execute(
                "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,warning,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, project_id, document["filename"], document["kind"], document["media_type"], document["sha256"], document["size"],
                 document["parse_status"], document["quality"], document["locator_count"], document["warning"], now),
            )
        for source in rows("SELECT * FROM sources WHERE project_id=?", (source_project_id,)):
            conn.execute("INSERT INTO sources (id,project_id,document_id,locator_type,locator,text) VALUES (?,?,?,?,?,?)",
                         (str(uuid.uuid4()), project_id, document_map[source["document_id"]], source["locator_type"], source["locator"], source["text"]))
    return project_id


def _cleanup_clone(project_id: str) -> None:
    with connect() as conn:
        for table in (
            "llm_attempts", "run_artifacts", "audit_events", "evidence_edges", "report_claims", "gold_annotations",
            "candidate_findings", "review_tasks", "policy_snapshots", "financial_tie_outs", "financial_metrics",
            "project_entities", "canonical_facts", "fact_assertions", "run_nodes", "runs", "report_sections", "risks",
            "cross_checks", "facts", "citations", "sources", "documents",
        ):
            conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def agent_case(source_project_id: str, expected: dict) -> dict:
    project_id = _clone_project(source_project_id)
    try:
        run = _wait(start_run(project_id))
        return _metrics(project_id, expected, run)
    finally:
        _cleanup_clone(project_id)


def baseline_case(project_id: str, expected: dict) -> dict:
    project = row("SELECT * FROM projects WHERE id=?", (project_id,))
    sources = rows(
        "SELECT s.*,d.kind,d.filename FROM sources s JOIN documents d ON d.id=s.document_id WHERE s.project_id=? ORDER BY d.kind,s.page,s.id",
        (project_id,),
    )
    started = time.perf_counter()
    result, usage = one_shot_baseline(project, sources)
    predicted = set(result.warning_keys)
    expected_warnings = set(expected["expected_warning_keys"])
    valid_sources = {item["id"] for item in sources}
    extracted_facts = [fact.model_dump() for fact in result.facts]
    extraction_accuracy, matched_facts = _fact_extraction_accuracy(extracted_facts, expected["expected_facts"])
    risk_recall, risk_precision, risk_f1 = _risk_scores(predicted, expected_warnings)
    missing_expected_facts = [
        item for item in expected["expected_facts"]
        if not any(_matches_expected_fact(fact, item) for fact in extracted_facts)
    ]
    return {
        "status": "completed",
        "field_extraction_accuracy": extraction_accuracy,
        "matched_fact_count": matched_facts,
        "expected_fact_count": len(expected["expected_facts"]),
        "extracted_fact_count": len(result.facts),
        "valid_fact_count": len(result.facts),
        "raw_fact_count": int(getattr(result, "baseline_raw_fact_count", len(result.facts))),
        "filtered_invalid_source_fact_count": int(getattr(result, "baseline_filtered_fact_count", 0)),
        "baseline_source_count": int(getattr(result, "baseline_source_count", len(sources))),
        "baseline_source_char_count": int(getattr(result, "baseline_source_char_count", 0)),
        "baseline_source_kind_counts": getattr(result, "baseline_source_kind_counts", {}),
        "baseline_field_registry_count": int(getattr(result, "baseline_field_registry_count", 0)),
        "baseline_prompt_chars": int(getattr(result, "baseline_prompt_chars", 0)),
        "baseline_model_fact_count": getattr(result, "baseline_model_fact_count", None),
        "baseline_status": getattr(result, "baseline_status", "valid" if result.facts else "empty"),
        "baseline_fact_keys": [fact.fact_key for fact in result.facts],
        "baseline_empty_reason": getattr(result, "baseline_empty_reason", "") if not result.facts else "",
        "model_input_tokens": usage.input_tokens,
        "model_output_tokens": usage.output_tokens,
        "model_finish_reason": usage.finish_reason,
        "model_response_chars": usage.response_chars,
        "model_response_keys": usage.response_keys,
        "model_response_field_counts": usage.response_field_counts,
        "model_response_value_types": usage.response_value_types,
        "empty_baseline_output": not result.facts,
        "missing_expected_facts": missing_expected_facts,
        "risk_recall": risk_recall,
        "risk_precision": risk_precision,
        "risk_f1": risk_f1,
        "candidate_recall": "not_applicable",
        "candidate_precision": "not_applicable",
        "candidate_f1": "not_applicable",
        "candidate_citation_precision": "not_applicable",
        "citation_precision": round(sum(item in valid_sources for item in result.citation_ids) / max(len(result.citation_ids), 1), 4),
        "section_citation_coverage": "not_applicable",
        "report_fact_support_rate": "not_applicable",
        "unsupported_claims": "not_applicable",
        "unsupported_numeric_claims": "not_applicable",
        "invalid_citations": 0,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "model_cost_cny": round(usage.cost, 6),
        "predicted_warning_keys": sorted(predicted),
        "returned_warning_keys": sorted(result.warning_keys),
    }


def summarize(results: list[dict]) -> dict:
    numeric_keys = [
        "field_extraction_accuracy", "risk_recall", "risk_precision", "risk_f1",
        "candidate_recall", "candidate_precision", "candidate_f1", "candidate_citation_precision",
        "citation_precision", "section_citation_coverage", "report_fact_support_rate", "conflict_recall",
        "latency_ms", "model_cost_cny", "unsupported_claims", "unsupported_numeric_claims",
        "invalid_citations", "subject_scope_error_count", "node_retries", "degraded_nodes",
        "duplicate_candidate_count", "chapter_fallback_rate", "latency_anomaly_count",
    ]
    summary = {}
    distributions = {}
    for key in numeric_keys:
        values = [float(item[key]) for item in results if isinstance(item.get(key), (int, float))]
        summary[key] = round(sum(values) / len(values), 4) if values else None
        distributions[key] = {
            "count": len(values),
            "min": round(min(values), 4) if values else None,
            "max": round(max(values), 4) if values else None,
            "mean": round(statistics.mean(values), 4) if values else None,
            "stddev": round(statistics.pstdev(values), 4) if values else None,
        }
    summary["runs"] = len(results)
    summary["completed"] = sum(item["status"] == "completed" for item in results)
    summary["success_rate"] = round(summary["completed"] / max(len(results), 1), 4)
    summary["total_cost_cny"] = round(sum(
        float(item.get("model_cost_cny") or 0) + float(item.get("prior_failed_cost_cny") or 0) for item in results
    ), 6)
    summary["error_counts"] = {key: sum(item.get("error_type") == key for item in results) for key in sorted(STANDARD_ERRORS) if any(item.get("error_type") == key for item in results)}
    completed = [item for item in results if item.get("status") == "completed"]
    candidate_completed = [item for item in completed if isinstance(item.get("candidate_true_positive_count"), int)]
    positive = [item for item in candidate_completed if item.get("candidate_positive_case")]
    negative = [item for item in candidate_completed if item.get("candidate_positive_case") is False]
    candidate_tp = sum(int(item.get("candidate_true_positive_count") or 0) for item in candidate_completed)
    candidate_fp = sum(int(item.get("candidate_false_positive_count") or 0) for item in candidate_completed)
    candidate_fn = sum(int(item.get("candidate_false_negative_count") or 0) for item in candidate_completed)
    positive_tp = sum(int(item.get("candidate_true_positive_count") or 0) for item in positive)
    positive_fn = sum(int(item.get("candidate_false_negative_count") or 0) for item in positive)
    micro_precision = candidate_tp / (candidate_tp + candidate_fp) if candidate_tp + candidate_fp else 1.0
    micro_recall = candidate_tp / (candidate_tp + candidate_fn) if candidate_tp + candidate_fn else 1.0
    summary["positive_case_candidate_recall"] = round(positive_tp / max(positive_tp + positive_fn, 1), 4) if positive else None
    summary["negative_case_false_positive_rate"] = round(
        sum(bool(item.get("candidate_negative_case_false_positive")) for item in negative) / len(negative), 4
    ) if negative else None
    summary["candidate_micro_precision"] = round(micro_precision, 4) if candidate_completed else None
    summary["candidate_micro_recall"] = round(micro_recall, 4) if candidate_completed else None
    summary["candidate_micro_f1"] = round(
        2 * micro_precision * micro_recall / (micro_precision + micro_recall), 4
    ) if candidate_completed and micro_precision + micro_recall else (0.0 if candidate_completed else None)
    summary["candidate_true_positive_count"] = candidate_tp if candidate_completed else None
    summary["candidate_false_positive_count"] = candidate_fp if candidate_completed else None
    summary["candidate_false_negative_count"] = candidate_fn if candidate_completed else None
    summary["duplicate_candidate_total"] = sum(int(item.get("duplicate_candidate_count") or 0) for item in candidate_completed) if candidate_completed else None
    llm_section_count = sum(int(item.get("chapter_llm_section_count") or 0) for item in completed)
    fallback_count = sum(int(item.get("chapter_fallback_count") or 0) for item in completed)
    summary["chapter_fallback_rate"] = round(fallback_count / max(llm_section_count, 1), 4) if llm_section_count else None
    summary["chapter_fallback_count"] = fallback_count
    summary["chapter_llm_section_count"] = llm_section_count
    summary["latency_anomaly_count"] = sum(int(item.get("latency_anomaly_count") or 0) for item in completed)
    summary["statistics"] = distributions
    by_case = {}
    for case in sorted({item["case"] for item in results}):
        case_items = [item for item in results if item["case"] == case and item.get("status") == "completed"]
        by_case[case] = {}
        for key in ("field_extraction_accuracy", "risk_recall", "candidate_recall", "report_fact_support_rate"):
            values = [float(item[key]) for item in case_items if isinstance(item.get(key), (int, float))]
            by_case[case][key] = {
                "runs": len(values), "min": min(values) if values else None, "max": max(values) if values else None,
                "range": round(max(values) - min(values), 4) if values else None,
            }
    summary["per_case_stability"] = by_case
    return summary


def markdown(payload: dict) -> str:
    summary = payload["summary"]
    lines = ["# 尽调 Agent 评测结果", "", f"- 模式：{payload['mode']}", f"- 模型：{payload['model']}", f"- 模型已配置：{payload['model_configured']}",
             f"- 运行次数：{summary['runs']}", f"- 字段提取准确率：{summary['field_extraction_accuracy']}",
             f"- 风险召回率：{summary['risk_recall']}", f"- 风险精确率：{summary['risk_precision']}",
             f"- 风险 F1：{summary['risk_f1']}", f"- 候选风险召回率：{summary['candidate_recall']}",
             f"- 候选风险精确率：{summary['candidate_precision']}", f"- 候选风险引用精确率：{summary['candidate_citation_precision']}",
             f"- 引用精确率：{summary['citation_precision']}",
             f"- 章节引用覆盖率：{summary['section_citation_coverage']}", f"- 平均耗时：{summary['latency_ms']} ms",
             f"- 平均模型成本：{summary['model_cost_cny']} 元", "", "> 所有案例均为虚构数据，结果不代表真实业务表现。", ""]
    return "\n".join(lines)


def _write_output(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = markdown(payload) if path.suffix.lower() == ".md" else json.dumps(payload, ensure_ascii=False, indent=2)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["deterministic", "agent", "baseline"], default="deterministic")
    parser.add_argument("--suite", choices=["bond"], default="bond", help="评测套件；产品只做债券发行担保")
    all_case_ids = sorted(set(manifest("bond")) | set(manifest("loan")))
    parser.add_argument("--case", choices=["all", *all_case_ids], default="all")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume", action="store_true", help="从现有 JSON 检查点继续，跳过已完成组合并重试失败组合")
    parser.add_argument("--budget-cny", type=float, default=None, help="本批次模型成本停止线（元）")
    parser.add_argument("--api-base", help="通过已配置模型的本地后端执行 agent 评测，例如 http://127.0.0.1:8001")
    args = parser.parse_args()
    startup()
    api_health = None
    if args.api_base:
        if args.mode != "agent":
            raise SystemExit("--api-base 仅支持 agent 模式")
        api_health = httpx.get(f"{args.api_base.rstrip('/')}/api/health", timeout=5).raise_for_status().json()
        if not api_health.get("modelConfigured"):
            raise SystemExit("指定后端尚未配置模型")
    if not args.api_base and args.mode in {"agent", "baseline"} and not is_configured():
        raise SystemExit("agent/baseline 模式需要配置 LLM_API_KEY；可先运行 --mode deterministic")
    selected = manifest(args.suite)
    if args.case != "all":
        if args.case not in selected:
            raise SystemExit(f"案例 {args.case} 不属于 {args.suite} 评测套件")
        selected = {args.case: selected[args.case]}
    runner = {"deterministic": deterministic_case, "agent": agent_case, "baseline": baseline_case}[args.mode]
    model_name = api_health["model"] if api_health else settings.llm_model
    model_configured = bool(api_health and api_health.get("modelConfigured")) or is_configured()
    metadata = evaluation_metadata(args.suite)
    details: list[dict] = []
    if args.resume and args.output and args.output.is_file() and args.output.suffix.lower() == ".json":
        try:
            previous = json.loads(args.output.read_text(encoding="utf-8"))
            if previous.get("mode") != args.mode or previous.get("model") != model_name:
                raise SystemExit("检查点的 mode/model 与当前参数不一致，拒绝混合评测结果")
            details = list(previous.get("details", []))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"检查点不是有效 JSON：{exc}") from exc
    completed_keys = {item.get("evaluation_key") for item in details if item.get("evaluation_key") and item.get("status") == "completed"}
    recorded_keys = {item.get("evaluation_key") for item in details if item.get("evaluation_key")}
    spent = sum(float(item.get("model_cost_cny") or 0) + float(item.get("prior_failed_cost_cny") or 0) for item in details)
    total_runs = args.repeat * len(selected)
    stopped_reason = ""
    for iteration in range(1, args.repeat + 1):
        for project_id, expected in selected.items():
            key = evaluation_key(args.mode, project_id, iteration, model_name, metadata)
            if key in completed_keys:
                print(f"[resume] 跳过 {project_id} 第{iteration}次", flush=True)
                continue
            if args.budget_cny is not None and spent >= args.budget_cny:
                stopped_reason = "budget_exhausted"
                break
            started = time.perf_counter()
            try:
                result = _api_agent_case(args.api_base, project_id, expected) if args.api_base else runner(project_id, expected)
                result.setdefault("error_type", None)
                result.setdefault("error_message", "")
            except Exception as exc:
                failed_usage = getattr(exc, "usage", None)
                result = {
                    "status": "failed", "error_type": classify_error(exc), "error_message": str(exc)[:500],
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "model_cost_cny": round(float(getattr(failed_usage, "cost", 0.0)), 6),
                }
                if args.mode == "baseline":
                    result["baseline_status"] = "invalid"
            detail = {
                "case": project_id, "iteration": iteration, "evaluation_key": key,
                "model": model_name, "prompt_version": metadata["prompt_version"],
                "policy_sha256": metadata["policy_sha256"], **result,
            }
            prior_cost = sum(
                float(item.get("model_cost_cny") or 0) + float(item.get("prior_failed_cost_cny") or 0)
                for item in details if item.get("evaluation_key") == key
            )
            if prior_cost:
                detail["prior_failed_cost_cny"] = round(prior_cost, 6)
            details = [item for item in details if item.get("evaluation_key") != key]
            details.append(detail)
            recorded_keys.add(key)
            if result["status"] == "completed":
                completed_keys.add(key)
            spent += float(result.get("model_cost_cny") or 0)
            fact_score = result.get("field_extraction_accuracy")
            risk_score = result.get("risk_recall")
            print(f"[{len(recorded_keys)}/{total_runs}] {project_id} 第{iteration}次: {result['status']}, "
                  f"facts={fact_score:.2%}, risks={risk_score:.2%}, latency={result['latency_ms']}ms, "
                  f"cost={float(result.get('model_cost_cny') or 0):.4f}元" if isinstance(fact_score, (int, float)) and isinstance(risk_score, (int, float))
                  else f"[{len(recorded_keys)}/{total_runs}] {project_id} 第{iteration}次: {result['status']}, error={result.get('error_type')}", flush=True)
            if args.output:
                _write_output(args.output, {
                    "suite": args.suite,
                    "mode": args.mode,
                    "model": model_name,
                    "model_configured": model_configured,
                    "metadata": metadata,
                    "budget_cny": args.budget_cny,
                    "stopped_reason": stopped_reason,
                    "summary": summarize(details),
                    "details": details,
                })
            if args.budget_cny is not None and spent >= args.budget_cny:
                stopped_reason = "budget_exhausted"
                break
        if stopped_reason:
            break
    payload = {
        "suite": args.suite,
        "mode": args.mode,
        "model": model_name,
        "model_configured": model_configured,
        "metadata": metadata,
        "budget_cny": args.budget_cny,
        "stopped_reason": stopped_reason,
        "summary": summarize(details),
        "details": details,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.output:
        _write_output(args.output, payload)


if __name__ == "__main__":
    main()
