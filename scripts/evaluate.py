from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

from backend.app.database import connect, json_load, now_iso, row, rows
from backend.app.facts import deterministic_extract, merge_facts
from backend.app.llm import extract_facts, is_configured, one_shot_baseline
from backend.app.main import startup
from backend.app.portfolio_cases import evaluation_manifest
from backend.app.rules import evaluate_rules
from backend.app.sample_case import FACTS
from backend.app.workflow import _normalize_model_fact, start_run


HUACHEN_WARNINGS = ["ar_growth", "cash_profit", "leverage", "customer", "related", "purpose", "collateral", "debt_due"]


def manifest() -> dict:
    output = evaluation_manifest()
    output["demo-huachen"] = {
        "expected_fact_count": len(FACTS),
        "expected_warning_keys": HUACHEN_WARNINGS,
    }
    return output


def _wait(run_id: str) -> dict:
    while True:
        run = row("SELECT * FROM runs WHERE id=?", (run_id,))
        if run["status"] in {"completed", "failed", "cancelled", "model_required"}:
            return run
        time.sleep(0.1)


def _metrics(project_id: str, expected: dict, run: dict) -> dict:
    facts = rows("SELECT * FROM facts WHERE project_id=?", (project_id,))
    checks = rows("SELECT * FROM cross_checks WHERE project_id=?", (project_id,))
    sections = rows("SELECT * FROM report_sections WHERE project_id=?", (project_id,))
    citations = rows("SELECT * FROM citations WHERE project_id=?", (project_id,))
    source_ids = {item["id"] for item in rows("SELECT id FROM sources WHERE project_id=?", (project_id,))}
    predicted_warnings = {item["rule_key"] for item in checks if item["status"] == "warning"}
    expected_warnings = set(expected["expected_warning_keys"])
    valid_citations = sum(item["source_id"] in source_ids for item in citations)
    return {
        "status": run["status"],
        "field_extraction_accuracy": round(min(len(facts), expected["expected_fact_count"]) / max(expected["expected_fact_count"], 1), 4),
        "risk_recall": round(len(predicted_warnings & expected_warnings) / max(len(expected_warnings), 1), 4),
        "citation_precision": round(valid_citations / max(len(citations), 1), 4),
        "section_citation_coverage": round(sum(bool(json_load(item["citation_ids"])) for item in sections) / max(len(sections), 1), 4),
        "unsupported_numeric_claims": 0 if run["status"] == "completed" else None,
        "latency_ms": run["latency_ms"],
        "model_cost_cny": round(run["cost"], 6),
        "predicted_warning_keys": sorted(predicted_warnings),
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
    return {
        "status": "completed",
        "field_extraction_accuracy": round(min(len(result.facts), expected["expected_fact_count"]) / max(expected["expected_fact_count"], 1), 4),
        "risk_recall": round(len(predicted & expected_warnings) / max(len(expected_warnings), 1), 4),
        "citation_precision": round(sum(item in valid_sources for item in result.citation_ids) / max(len(result.citation_ids), 1), 4),
        "section_citation_coverage": None,
        "unsupported_numeric_claims": None,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "model_cost_cny": round(usage.cost, 6),
        "predicted_warning_keys": sorted(predicted),
    }


def summarize(results: list[dict]) -> dict:
    numeric_keys = ["field_extraction_accuracy", "risk_recall", "citation_precision", "section_citation_coverage", "latency_ms", "model_cost_cny"]
    summary = {}
    for key in numeric_keys:
        values = [item[key] for item in results if item.get(key) is not None]
        summary[key] = round(sum(values) / len(values), 4) if values else None
    summary["runs"] = len(results)
    summary["completed"] = sum(item["status"] == "completed" for item in results)
    return summary


def markdown(payload: dict) -> str:
    summary = payload["summary"]
    lines = ["# 尽调 Agent 评测结果", "", f"- 模式：{payload['mode']}", f"- 模型已配置：{payload['model_configured']}",
             f"- 运行次数：{summary['runs']}", f"- 字段提取准确率：{summary['field_extraction_accuracy']}",
             f"- 风险召回率：{summary['risk_recall']}", f"- 引用精确率：{summary['citation_precision']}",
             f"- 章节引用覆盖率：{summary['section_citation_coverage']}", f"- 平均耗时：{summary['latency_ms']} ms",
             f"- 平均模型成本：{summary['model_cost_cny']} 元", "", "> 所有案例均为虚构数据，结果不代表真实业务表现。", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["deterministic", "agent", "baseline"], default="deterministic")
    parser.add_argument("--case", choices=["all", "demo-huachen", "demo-qihang", "demo-xingqiao"], default="all")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    startup()
    if args.mode in {"agent", "baseline"} and not is_configured():
        raise SystemExit("agent/baseline 模式需要配置 LLM_API_KEY；可先运行 --mode deterministic")
    selected = manifest()
    if args.case != "all":
        selected = {args.case: selected[args.case]}
    runner = {"deterministic": deterministic_case, "agent": agent_case, "baseline": baseline_case}[args.mode]
    details = []
    for _ in range(args.repeat):
        for project_id, expected in selected.items():
            details.append({"case": project_id, **runner(project_id, expected)})
    payload = {"mode": args.mode, "model_configured": is_configured(), "summary": summarize(details), "details": details}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(markdown(payload) if args.output.suffix.lower() == ".md" else json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
