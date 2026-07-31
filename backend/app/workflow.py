from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass

from .database import connect, json_dump, json_load, now_iso, row, rows
from .facts import FACT_REGISTRY, deterministic_extract, material_inventory, merge_facts, normalize_numeric, normalize_period, replace_extracted_facts
from .llm import LlmUsage, extract_facts, is_configured, rewrite_section
from .retrieval import retrieve
from .rules import RuleResult, evaluate_rules, risks_from_checks


NODES = [
    ("inventory", 10, "校验材料清单"),
    ("extract", 24, "抽取材料事实"),
    ("normalize", 36, "标准化期间、金额和科目"),
    ("cross_check", 52, "执行跨材料一致性核验"),
    ("risk", 66, "生成风险和待核验事项"),
    ("draft", 82, "生成证据化报告草稿"),
    ("verify", 94, "校验引用、数字和事实支持"),
    ("review", 100, "等待人工复核"),
]
TERMINAL = {"completed", "failed", "cancelled", "model_required"}


@dataclass
class SectionTemplate:
    key: str
    title: str
    content: str
    source_ids: list[str]


def _cancelled(run_id: str) -> bool:
    item = row("SELECT cancel_requested FROM runs WHERE id=?", (run_id,))
    return bool(item and item["cancel_requested"])


def _update_run(run_id: str, node: str, progress: int, message: str, status: str = "running") -> None:
    with connect() as conn:
        conn.execute("UPDATE runs SET current_node=?,progress=?,message=?,status=? WHERE id=?",
                     (node, progress, message, status, run_id))


def _node_start(run_id: str, project_id: str, node: str, progress: int, message: str) -> None:
    _update_run(run_id, node, progress, message)
    with connect() as conn:
        existing = conn.execute("SELECT attempt FROM run_nodes WHERE run_id=? AND node_key=?", (run_id, node)).fetchone()
        attempt = int(existing[0]) + 1 if existing else 1
        conn.execute(
            "INSERT INTO run_nodes (id,run_id,project_id,node_key,status,progress,attempt,started_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id,node_key) DO UPDATE SET status='running',progress=excluded.progress,attempt=excluded.attempt,started_at=excluded.started_at,error=''",
            (str(uuid.uuid4()), run_id, project_id, node, "running", progress, attempt, now_iso()),
        )


def _node_finish(run_id: str, node: str, summary: str = "", usage: LlmUsage | None = None, status: str = "completed", error: str = "") -> None:
    usage = usage or LlmUsage()
    with connect() as conn:
        conn.execute(
            "UPDATE run_nodes SET status=?,finished_at=?,input_tokens=?,output_tokens=?,cost=?,error=?,output_summary=? WHERE run_id=? AND node_key=?",
            (status, now_iso(), usage.input_tokens, usage.output_tokens, usage.cost, error, summary[:1000], run_id, node),
        )


def _check_cancel(run_id: str, project_id: str, node: str, progress: int) -> bool:
    if not _cancelled(run_id):
        return False
    _node_finish(run_id, node, "用户取消", status="cancelled")
    _update_run(run_id, node, progress, "任务已取消", "cancelled")
    with connect() as conn:
        conn.execute("UPDATE projects SET status='ready',updated_at=? WHERE id=?", (now_iso(), project_id))
    return True


def _normalize_model_fact(fact: dict) -> dict:
    definition = FACT_REGISTRY[fact["fact_key"]]
    value = fact.get("value")
    unit = fact.get("unit", "")
    if value is not None:
        value, unit = normalize_numeric(value, unit, definition.unit_kind)
    return {
        **fact, "id": str(uuid.uuid4()), "category": definition.category, "name": definition.label,
        "period": normalize_period(fact.get("period", "")), "value": value, "unit": unit,
        "extraction_method": "model", "review_status": "unreviewed",
    }


def _extract_project_facts(run_id: str, project_id: str) -> tuple[int, LlmUsage]:
    existing = rows("SELECT * FROM facts WHERE project_id=?", (project_id,))
    if existing and all(item.get("extraction_method", "seed") in {"seed", "human"} for item in existing):
        return len(existing), LlmUsage()
    source_rows = rows(
        "SELECT s.*,d.kind FROM sources s JOIN documents d ON d.id=s.document_id WHERE s.project_id=? ORDER BY d.created_at,s.rowid",
        (project_id,),
    )
    deterministic = deterministic_extract(source_rows)
    if not is_configured():
        replace_extracted_facts(project_id, deterministic)
        raise RuntimeError("MODEL_REQUIRED")

    model_facts: list[dict] = []
    total_usage = LlmUsage()
    by_document: dict[str, list[dict]] = {}
    for source in source_rows:
        by_document.setdefault(source["document_id"], []).append(source)
    for document_sources in by_document.values():
        batch: list[dict] = []
        characters = 0
        for source in document_sources:
            size = len(source["text"])
            if batch and characters + size > 24000:
                extracted, usage = extract_facts(batch)
                model_facts.extend(_normalize_model_fact(item) for item in extracted)
                _add_usage(total_usage, usage)
                batch, characters = [], 0
            batch.append(source)
            characters += size
        if batch:
            extracted, usage = extract_facts(batch)
            model_facts.extend(_normalize_model_fact(item) for item in extracted)
            _add_usage(total_usage, usage)
        if _cancelled(run_id):
            break
    replace_extracted_facts(project_id, merge_facts(deterministic, model_facts))
    return len(rows("SELECT id FROM facts WHERE project_id=?", (project_id,))), total_usage


def _add_usage(total: LlmUsage, current: LlmUsage) -> None:
    total.input_tokens += current.input_tokens
    total.output_tokens += current.output_tokens
    total.latency_ms += current.latency_ms
    total.cost += current.cost


def _citations(project_id: str, source_ids: list[str]) -> list[str]:
    citation_ids = []
    for source_id in dict.fromkeys(source_ids):
        source = row(
            "SELECT s.*,d.filename FROM sources s JOIN documents d ON d.id=s.document_id WHERE s.id=? AND s.project_id=?",
            (source_id, project_id),
        )
        if not source:
            continue
        citation_id = f"c-{source_id}"
        with connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO citations (id,project_id,source_id,document_id,label,quote,locator_type,locator) VALUES (?,?,?,?,?,?,?,?)",
                (citation_id, project_id, source_id, source["document_id"], source["filename"],
                 source["text"][:1200], source["locator_type"], source["locator"]),
            )
        citation_ids.append(citation_id)
    return citation_ids


def _source_ids(items: list[dict]) -> list[str]:
    output: list[str] = []
    for item in items:
        source_ids = item.get("source_ids", [])
        source_ids = json_load(source_ids) if isinstance(source_ids, str) else source_ids
        output.extend(source_ids)
    return list(dict.fromkeys(output))


def _fact_sentence(facts: list[dict], keys: list[str]) -> str:
    parts = []
    for key in keys:
        candidates = [item for item in facts if item.get("fact_key") == key]
        if not candidates:
            continue
        fact = max(candidates, key=lambda item: item.get("period", ""))
        definition = FACT_REGISTRY[key]
        value = fact.get("text_value") or (f"{fact['value']:g}{fact['unit']}" if fact.get("value") is not None else "")
        period = f"{fact['period']}年" if fact.get("period", "").isdigit() else ""
        if value:
            parts.append(f"{period}{definition.label}{value}")
    return "；".join(parts)


def _templates(project: dict, facts: list[dict], checks: list[dict], risks: list[dict], inventory: dict) -> list[SectionTemplate]:
    check_map = {item["rule_key"]: item for item in checks}
    narrative = retrieve(project["id"], "企业 实际控制人 主营业务 客户 供应商 关联方", 8)
    narrative_sources = [item["id"] for item in narrative]
    finance_keys = ["revenue", "net_profit", "operating_cash_flow", "accounts_receivable", "total_assets", "total_liabilities", "current_assets", "current_liabilities"]
    debt_keys = ["interest_bearing_debt", "debt_due_12m", "external_guarantee"]
    finance_facts = [item for item in facts if item.get("fact_key") in finance_keys]
    debt_facts = [item for item in facts if item.get("fact_key") in debt_keys]

    def check_text(keys: list[str]) -> str:
        selected = [check_map[key] for key in keys if key in check_map]
        return "\n".join(f"- {item['summary']}" for item in selected) or "材料不足：当前材料无法完成对应交叉核验。"

    gaps = "、".join(item["group"] for item in inventory["gaps"]) or "未识别核心材料缺口"
    risk_text = "\n".join(f"- [{item['severity']}] {item['title']}：{item['description']}" for item in risks) or "当前确定性规则未识别风险；仍须由人工结合原始材料复核。"
    mitigation = "\n".join(f"- {item['action']}" for item in risks) or "根据后续人工核验结果设置风险缓释条件。"
    return [
        SectionTemplate("application", "项目及融资申请概况",
                        f"{project['company_name']}申请{project['amount']:.0f}万元融资担保，期限{project['term_months']}个月，资金用途为{project['purpose']}。本报告仅作为风险审查辅助，不构成自动审批意见。",
                        [item["id"] for item in retrieve(project["id"], "申请金额 融资期限 资金用途", 3)]),
        SectionTemplate("parties", "企业、实控人及关联方情况",
                        "根据现有材料识别企业主体、实际控制人与关联方。未被材料明确支持的信息标记为材料不足。",
                        narrative_sources),
        SectionTemplate("operations", "主营业务与上下游分析",
                        "根据合同、客户、供应商及访谈材料分析主营业务与上下游集中度；未提供订单稳定性或客户函证时需补充核验。",
                        narrative_sources),
        SectionTemplate("financial", "财务状况及偿债能力",
                        (_fact_sentence(facts, finance_keys) or "材料不足：未形成可验证的标准化财务事实。") + "\n" + check_text(["ar_growth", "cash_profit", "leverage"]),
                        _source_ids(finance_facts) + _source_ids([check_map[key] for key in ("ar_growth", "cash_profit", "leverage") if key in check_map])),
        SectionTemplate("reconciliation", "流水、纳税与收入交叉核验",
                        check_text(["revenue_tax", "revenue_bank"]),
                        _source_ids([check_map[key] for key in ("revenue_tax", "revenue_bank") if key in check_map])),
        SectionTemplate("debt", "负债、对外担保及或有风险",
                        (_fact_sentence(facts, debt_keys) or "材料不足：未形成可验证的债务与对外担保事实。") + "\n" + check_text(["debt_due"]),
                        _source_ids(debt_facts) + _source_ids([check_map["debt_due"]] if "debt_due" in check_map else [])),
        SectionTemplate("collateral", "抵押物与反担保措施", check_text(["collateral"]),
                        _source_ids([check_map["collateral"]] if "collateral" in check_map else [])),
        SectionTemplate("risks", "核心风险和待核验事项", risk_text, _source_ids(risks)),
        SectionTemplate("mitigation", "风险缓释建议", mitigation, _source_ids(risks)),
        SectionTemplate("evidence", "证据索引与材料缺口",
                        f"材料完整度：{inventory['readiness']}。待补充材料类别：{gaps}。缺失材料不得由模型推断补全。",
                        [item["id"] for item in retrieve(project["id"], "材料 财务 流水 纳税 合同 抵押 债务", 8)]),
    ]


def _numbers_supported(content: str, original: str, evidence: list[dict]) -> bool:
    allowed_text = original + " " + " ".join(item["quote"] for item in evidence)
    allowed = set(re.findall(r"-?\d+(?:\.\d+)?", allowed_text))
    produced = set(re.findall(r"-?\d+(?:\.\d+)?", content))
    return produced <= allowed


def _run(run_id: str, project_id: str) -> None:
    started = time.perf_counter()
    total_usage = LlmUsage()
    current_node = "inventory"
    current_progress = 10
    try:
        _node_start(run_id, project_id, "inventory", 10, "校验材料清单")
        inventory = material_inventory(project_id)
        _node_finish(run_id, "inventory", f"{len(inventory['documents'])}份材料，{len(inventory['gaps'])}类缺口")
        if _check_cancel(run_id, project_id, "inventory", 10):
            return

        current_node, current_progress = "extract", 24
        _node_start(run_id, project_id, current_node, current_progress, "抽取材料事实")
        try:
            fact_count, usage = _extract_project_facts(run_id, project_id)
        except RuntimeError as exc:
            if str(exc) == "MODEL_REQUIRED":
                _node_finish(run_id, current_node, "需要配置模型", status="model_required", error="未配置模型 API Key")
                _update_run(run_id, current_node, current_progress, "自定义项目需要配置模型后继续分析", "model_required")
                with connect() as conn:
                    conn.execute("UPDATE runs SET error=?,finished_at=? WHERE id=?", ("未配置模型 API Key", now_iso(), run_id))
                    conn.execute("UPDATE projects SET status='model_required',updated_at=? WHERE id=?", (now_iso(), project_id))
                return
            raise
        _add_usage(total_usage, usage)
        _node_finish(run_id, current_node, f"抽取{fact_count}项事实", usage)
        if _check_cancel(run_id, project_id, current_node, current_progress):
            return

        current_node, current_progress = "normalize", 36
        _node_start(run_id, project_id, current_node, current_progress, "标准化期间、金额和科目")
        facts = rows("SELECT * FROM facts WHERE project_id=?", (project_id,))
        _node_finish(run_id, current_node, f"标准化{len(facts)}项事实")

        current_node, current_progress = "cross_check", 52
        _node_start(run_id, project_id, current_node, current_progress, "执行跨材料一致性核验")
        project = row("SELECT * FROM projects WHERE id=?", (project_id,))
        checks = evaluate_rules(facts, float(project["amount"]))
        with connect() as conn:
            conn.execute("DELETE FROM cross_checks WHERE project_id=?", (project_id,))
            for check in checks:
                conn.execute(
                    "INSERT INTO cross_checks (id,project_id,rule_key,title,severity,status,summary,values_json,source_ids,formula,fact_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), project_id, check.key, check.title, check.severity, check.status,
                     check.summary, json_dump(check.values), json_dump(check.source_ids), check.formula, json_dump(check.fact_ids)),
                )
        _node_finish(run_id, current_node, f"完成{len(checks)}项规则，{sum(item.status == 'insufficient' for item in checks)}项材料不足")

        current_node, current_progress = "risk", 66
        _node_start(run_id, project_id, current_node, current_progress, "生成风险和待核验事项")
        generated_risks = risks_from_checks(checks)
        with connect() as conn:
            conn.execute("DELETE FROM risks WHERE project_id=?", (project_id,))
            for risk in generated_risks:
                conn.execute(
                    "INSERT INTO risks (id,project_id,severity,category,title,description,impact,action,source_ids) VALUES (?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), project_id, risk["severity"], risk["category"], risk["title"],
                     risk["description"], risk["impact"], risk["action"], json_dump(risk["source_ids"])),
                )
        _node_finish(run_id, current_node, f"生成{len(generated_risks)}项风险")
        if _check_cancel(run_id, project_id, current_node, current_progress):
            return

        current_node, current_progress = "draft", 82
        _node_start(run_id, project_id, current_node, current_progress, "生成证据化报告草稿")
        stored_risks = rows("SELECT * FROM risks WHERE project_id=?", (project_id,))
        stored_checks = rows("SELECT * FROM cross_checks WHERE project_id=?", (project_id,))
        with connect() as conn:
            conn.execute("DELETE FROM report_sections WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM citations WHERE project_id=?", (project_id,))
        section_count = 0
        for template in _templates(project, facts, stored_checks, stored_risks, inventory):
            if _check_cancel(run_id, project_id, current_node, current_progress):
                return
            citation_ids = _citations(project_id, template.source_ids)
            evidence = []
            for citation_id in citation_ids:
                citation = row("SELECT id,quote FROM citations WHERE id=?", (citation_id,))
                if citation:
                    evidence.append(citation)
            result, usage = rewrite_section(template.title, template.content, evidence)
            _add_usage(total_usage, usage)
            safe_citations = [citation_id for citation_id in result.citation_ids if citation_id in citation_ids]
            content = result.content
            if (citation_ids and not safe_citations) or not _numbers_supported(content, template.content, evidence):
                content, safe_citations = template.content, citation_ids
            with connect() as conn:
                conn.execute(
                    "INSERT INTO report_sections (id,project_id,section_key,title,content,citation_ids,review_status,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), project_id, template.key, template.title, content,
                     json_dump(safe_citations or citation_ids), "draft", now_iso()),
                )
            section_count += 1
        _node_finish(run_id, current_node, f"生成{section_count}个章节", total_usage)

        current_node, current_progress = "verify", 94
        _node_start(run_id, project_id, current_node, current_progress, "校验引用、数字和事实支持")
        invalid = row(
            "SELECT COUNT(*) count FROM report_sections r WHERE r.project_id=? AND EXISTS (SELECT 1 FROM json_each(r.citation_ids) j LEFT JOIN citations c ON c.id=j.value WHERE c.id IS NULL)",
            (project_id,),
        )["count"]
        if invalid:
            raise RuntimeError(f"发现{invalid}个章节包含无效引用")
        _node_finish(run_id, current_node, "引用校验通过")

        current_node, current_progress = "review", 100
        _node_start(run_id, project_id, current_node, current_progress, "等待人工复核")
        _node_finish(run_id, current_node, "工作流完成")
        latency = int((time.perf_counter() - started) * 1000)
        with connect() as conn:
            conn.execute("UPDATE projects SET status='review',updated_at=? WHERE id=?", (now_iso(), project_id))
            conn.execute(
                "UPDATE runs SET status='completed',current_node='review',progress=100,message='等待人工复核',finished_at=?,latency_ms=?,input_tokens=?,output_tokens=?,cost=? WHERE id=?",
                (now_iso(), latency, total_usage.input_tokens, total_usage.output_tokens, total_usage.cost, run_id),
            )
    except Exception as exc:
        _node_finish(run_id, current_node, "执行失败", status="failed", error=str(exc))
        with connect() as conn:
            conn.execute("UPDATE runs SET status='failed',error=?,message='分析失败',finished_at=? WHERE id=?", (str(exc), now_iso(), run_id))
            conn.execute("UPDATE projects SET status='failed',updated_at=? WHERE id=?", (now_iso(), project_id))


def start_run(project_id: str) -> str:
    run_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            "INSERT INTO runs (id,project_id,status,current_node,progress,message,cancel_requested,started_at,finished_at,latency_ms,input_tokens,output_tokens,cost,error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, project_id, "queued", "queued", 0, "任务已进入队列", 0, now_iso(), None, 0, 0, 0, 0.0, ""),
        )
        conn.execute("UPDATE projects SET status='running',updated_at=? WHERE id=?", (now_iso(), project_id))
    threading.Thread(target=_run, args=(run_id, project_id), daemon=True).start()
    return run_id


def retry_run(run_id: str) -> str:
    previous = row("SELECT project_id,status FROM runs WHERE id=?", (run_id,))
    if not previous:
        raise ValueError("任务不存在")
    if previous["status"] not in {"failed", "cancelled", "model_required"}:
        raise ValueError("仅失败、取消或等待模型配置的任务可以重试")
    return start_run(previous["project_id"])
