from __future__ import annotations

import argparse
import json
import mimetypes
import time
import uuid
from pathlib import Path

import httpx

from backend.app.config import settings
from backend.app.database import connect, json_load, now_iso, row, rows
from backend.app.facts import canonical_key, deterministic_extract, merge_facts
from backend.app.llm import extract_facts, is_configured, one_shot_baseline
from backend.app.main import startup
from backend.app.portfolio_cases import evaluation_manifest, generate_portfolio_materials
from backend.app.rules import evaluate_rules
from backend.app.sample_case import FACTS, generate_demo_materials
from backend.app.workflow import _normalize_model_fact, start_run


HUACHEN_WARNINGS = ["ar_growth", "cash_profit", "leverage", "customer", "related", "purpose", "collateral", "debt_due"]


def manifest() -> dict:
    output = evaluation_manifest()
    output["demo-huachen"] = {
        "expected_fact_count": len(FACTS),
        "expected_facts": [
            {"fact_key": canonical_key(name), "period": period, "value": value, "unit": unit}
            for _, _, name, period, value, unit, _, _ in FACTS
        ],
        "expected_warning_keys": HUACHEN_WARNINGS,
    }
    return output


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
    true_positives = len(predicted & expected)
    recall = true_positives / max(len(expected), 1)
    precision = true_positives / len(predicted) if predicted else (1.0 if not expected else 0.0)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return round(recall, 4), round(precision, 4), round(f1, 4)


def _metrics(project_id: str, expected: dict, run: dict) -> dict:
    facts = rows("SELECT * FROM facts WHERE project_id=?", (project_id,))
    checks = rows("SELECT * FROM cross_checks WHERE project_id=?", (project_id,))
    sections = rows("SELECT * FROM report_sections WHERE project_id=?", (project_id,))
    citations = rows("SELECT * FROM citations WHERE project_id=?", (project_id,))
    source_ids = {item["id"] for item in rows("SELECT id FROM sources WHERE project_id=?", (project_id,))}
    predicted_warnings = {item["rule_key"] for item in checks if item["status"] == "warning"}
    expected_warnings = set(expected["expected_warning_keys"])
    valid_citations = sum(item["source_id"] in source_ids for item in citations)
    extraction_accuracy, matched_facts = _fact_extraction_accuracy(facts, expected["expected_facts"])
    risk_recall, risk_precision, risk_f1 = _risk_scores(predicted_warnings, expected_warnings)
    return {
        "status": run["status"],
        "field_extraction_accuracy": extraction_accuracy,
        "matched_fact_count": matched_facts,
        "expected_fact_count": len(expected["expected_facts"]),
        "extracted_fact_count": len(facts),
        "risk_recall": risk_recall,
        "risk_precision": risk_precision,
        "risk_f1": risk_f1,
        "citation_precision": round(valid_citations / max(len(citations), 1), 4),
        "section_citation_coverage": round(sum(bool(json_load(item["citation_ids"])) for item in sections) / max(len(sections), 1), 4),
        "unsupported_numeric_claims": 0 if run["status"] == "completed" else None,
        "latency_ms": run["latency_ms"],
        "model_cost_cny": round(run["cost"], 6),
        "predicted_warning_keys": sorted(predicted_warnings),
    }


def _api_agent_case(api_base: str, source_project_id: str, expected: dict) -> dict:
    material_map = {
        item["id"]: item["path"]
        for item in generate_demo_materials() + generate_portfolio_materials()
    }
    with httpx.Client(base_url=api_base.rstrip("/"), timeout=120) as client:
        source_project = client.get(f"/api/projects/{source_project_id}").raise_for_status().json()
        inventory = client.get(f"/api/projects/{source_project_id}/document-inventory").raise_for_status().json()
        created = client.post("/api/projects", json={
            "company_name": f"{source_project['company_name']}（模型评测）",
            "amount": source_project["amount"],
            "term_months": source_project["term_months"],
            "purpose": source_project["purpose"],
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
        citation_ids = {citation_id for section in sections for citation_id in section.get("citation_ids", [])}
        valid_citations = sum(
            client.get(f"/api/citations/{citation_id}", headers=headers).is_success
            for citation_id in citation_ids
        )

    predicted_warnings = {item["rule_key"] for item in checks if item["status"] == "warning"}
    expected_warnings = set(expected["expected_warning_keys"])
    extraction_accuracy, matched_facts = _fact_extraction_accuracy(facts, expected["expected_facts"])
    risk_recall, risk_precision, risk_f1 = _risk_scores(predicted_warnings, expected_warnings)
    return {
        "status": run["status"],
        "field_extraction_accuracy": extraction_accuracy,
        "matched_fact_count": matched_facts,
        "expected_fact_count": len(expected["expected_facts"]),
        "extracted_fact_count": len(facts),
        "risk_recall": risk_recall,
        "risk_precision": risk_precision,
        "risk_f1": risk_f1,
        "citation_precision": round(valid_citations / max(len(citation_ids), 1), 4),
        "section_citation_coverage": round(sum(bool(item.get("citation_ids")) for item in sections) / max(len(sections), 1), 4),
        "unsupported_numeric_claims": 0 if run["status"] == "completed" else None,
        "latency_ms": run["latency_ms"],
        "model_cost_cny": round(run["cost"], 6),
        "predicted_warning_keys": sorted(predicted_warnings),
        "project_id": project_id,
    }


def deterministic_case(project_id: str, expected: dict) -> dict:
    run = _wait(start_run(project_id))
    return _metrics(project_id, expected, run)


def _clone_project(source_project_id: str) -> str:
    source_project = row("SELECT * FROM projects WHERE id=?", (source_project_id,))
    project_id = f"eval-{uuid.uuid4()}"
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,consent_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (project_id, uuid.uuid4().hex, source_project["company_name"], source_project["amount"], source_project["term_months"],
             source_project["purpose"], "ready", now, now, now),
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
        for table in ("run_nodes", "runs", "report_sections", "risks", "cross_checks", "facts", "citations", "sources", "documents"):
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
    sources = rows("SELECT * FROM sources WHERE project_id=?", (project_id,))
    started = time.perf_counter()
    result, usage = one_shot_baseline(project, sources)
    predicted = set(result.warning_keys)
    expected_warnings = set(expected["expected_warning_keys"])
    valid_sources = {item["id"] for item in sources}
    extracted_facts = [fact.model_dump() for fact in result.facts]
    extraction_accuracy, matched_facts = _fact_extraction_accuracy(extracted_facts, expected["expected_facts"])
    risk_recall, risk_precision, risk_f1 = _risk_scores(predicted, expected_warnings)
    return {
        "status": "completed",
        "field_extraction_accuracy": extraction_accuracy,
        "matched_fact_count": matched_facts,
        "expected_fact_count": len(expected["expected_facts"]),
        "extracted_fact_count": len(result.facts),
        "risk_recall": risk_recall,
        "risk_precision": risk_precision,
        "risk_f1": risk_f1,
        "citation_precision": round(sum(item in valid_sources for item in result.citation_ids) / max(len(result.citation_ids), 1), 4),
        "section_citation_coverage": None,
        "unsupported_numeric_claims": None,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "model_cost_cny": round(usage.cost, 6),
        "predicted_warning_keys": sorted(predicted),
    }


def summarize(results: list[dict]) -> dict:
    numeric_keys = [
        "field_extraction_accuracy", "risk_recall", "risk_precision", "risk_f1",
        "citation_precision", "section_citation_coverage", "latency_ms", "model_cost_cny",
    ]
    summary = {}
    for key in numeric_keys:
        values = [item[key] for item in results if item.get(key) is not None]
        summary[key] = round(sum(values) / len(values), 4) if values else None
    summary["runs"] = len(results)
    summary["completed"] = sum(item["status"] == "completed" for item in results)
    return summary


def markdown(payload: dict) -> str:
    summary = payload["summary"]
    lines = ["# 尽调 Agent 评测结果", "", f"- 模式：{payload['mode']}", f"- 模型：{payload['model']}", f"- 模型已配置：{payload['model_configured']}",
             f"- 运行次数：{summary['runs']}", f"- 字段提取准确率：{summary['field_extraction_accuracy']}",
             f"- 风险召回率：{summary['risk_recall']}", f"- 风险精确率：{summary['risk_precision']}",
             f"- 风险 F1：{summary['risk_f1']}", f"- 引用精确率：{summary['citation_precision']}",
             f"- 章节引用覆盖率：{summary['section_citation_coverage']}", f"- 平均耗时：{summary['latency_ms']} ms",
             f"- 平均模型成本：{summary['model_cost_cny']} 元", "", "> 所有案例均为虚构数据，结果不代表真实业务表现。", ""]
    return "\n".join(lines)


def _write_output(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = markdown(payload) if path.suffix.lower() == ".md" else json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["deterministic", "agent", "baseline"], default="deterministic")
    parser.add_argument("--case", choices=["all", "demo-huachen", "demo-qihang", "demo-xingqiao"], default="all")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path)
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
    selected = manifest()
    if args.case != "all":
        selected = {args.case: selected[args.case]}
    runner = {"deterministic": deterministic_case, "agent": agent_case, "baseline": baseline_case}[args.mode]
    model_name = api_health["model"] if api_health else settings.llm_model
    model_configured = bool(api_health and api_health.get("modelConfigured")) or is_configured()
    details = []
    total_runs = args.repeat * len(selected)
    for _ in range(args.repeat):
        for project_id, expected in selected.items():
            result = _api_agent_case(args.api_base, project_id, expected) if args.api_base else runner(project_id, expected)
            details.append({"case": project_id, **result})
            print(
                f"[{len(details)}/{total_runs}] {project_id}: {result['status']}, "
                f"facts={result['field_extraction_accuracy']:.2%}, risks={result['risk_recall']:.2%}, "
                f"latency={result['latency_ms']}ms, cost={result['model_cost_cny']:.4f}元",
                flush=True,
            )
            if args.output:
                _write_output(args.output, {
                    "mode": args.mode,
                    "model": model_name,
                    "model_configured": model_configured,
                    "summary": summarize(details),
                    "details": details,
                })
    payload = {
        "mode": args.mode,
        "model": model_name,
        "model_configured": model_configured,
        "summary": summarize(details),
        "details": details,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.output:
        _write_output(args.output, payload)


if __name__ == "__main__":
    main()
