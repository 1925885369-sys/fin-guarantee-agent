from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass

from .database import connect, execute, json_dump, json_load, now_iso, row, rows
from .bond_tranches import extract_bond_tranches, tranches_input_hash_component
from .bond_sections import build_section_extraction_plans, contract_sha256, load_extraction_contract, offering_term_sources
from .chapter_prompts import chapter_uses_llm, load_chapter_config
from .chapter_guard import (
    assess_chapter_length, build_chapter_context, fallback_reason_code, validate_chapter_output,
    validation_diagnostics,
)
from .facts import (
    FACT_REGISTRY, FINANCIAL_FACT_KEYS, deterministic_extract, financial_scope_by_source,
    material_inventory, merge_facts, normalize_numeric, normalize_period, replace_extracted_facts,
    source_supports_fact, source_supports_fact_value,
)
from .config import settings
from .llm import LlmCallContext, LlmUsage, StructuredLlmError, extract_facts, identify_candidate_findings, is_configured, rewrite_section
from .metrics import compute_and_store_metrics
from .policy import load_policy, save_policy_snapshot
from .p0 import build_evidence_graph, compute_financial_tie_outs, default_scope, period_type, sync_project_entities, validate_report_support
from .professional import analysis_fact_rows, audit_event, canonical_fact_rows, fact_groups, sync_fact_assertions
from .profile_evidence import analysis_source, materialize_snapshot_evidence
from .retrieval import retrieve, retrieve_chunks, retrieval_signature
from .review import decode_candidates, decode_tasks, generate_review_tasks, replace_candidates, sync_confirmed_candidate_risks
from .report_delivery import content_sha256, write_report_section
from .rules import RuleResult, risks_from_checks


NODES = [
    ("inventory", 8, "校验材料清单"),
    ("extract", 20, "抽取事实断言"),
    ("conflict", 30, "识别事实冲突"),
    ("metrics", 40, "计算专业财务指标"),
    ("cross_check", 54, "执行版本化风控规则"),
    ("candidate", 66, "检索证据并生成候选风险"),
    ("tasks", 74, "生成尽调复核任务"),
    ("risk", 82, "形成确认风险与担保条件"),
    ("draft", 92, "生成专业审查报告"),
    ("verify", 97, "校验引用、数字和事实支持"),
    ("review", 100, "等待人工复核"),
]
TERMINAL = {"completed", "failed", "cancelled", "model_required"}
NODE_KEYS = [item[0] for item in NODES]

TAXONOMY_QUERIES = {
    "governance": "公司治理 实控人 审批权限 内控制度 关联方 决策 网银 印章 制衡 否决",
    "continuity": "持续经营 订单 稳定性 停产 核心客户 供应链 访谈 流失 取消 中断",
    "authenticity": "交易真实性 合同 发票 回款 物流 验收 异常 未盖章 共用联系人 差异",
    "fund_diversion": "资金用途 挪用 关联转账 流水 去向 支付对象 异常支付 流向不明",
    "key_person": "关键人物 实控人 管理层 依赖 履历 变动 授权 离职 继任 不可替代",
    "collateral_enforceability": "抵押物 权属 查封 顺位 估值 可执行性 处置 未登记 重复抵押 质押通知",
}

TAXONOMY_SIGNAL_TERMS = {
    "governance": ("同时控制合同审批", "同时控制印章", "网银复核", "无法独立否决", "缺少制衡", "审批权限集中"),
    "continuity": ("停产", "订单取消", "客户流失", "供应链中断", "无法持续经营", "未续约"),
    "authenticity": ("缺少客户盖章", "未盖章", "共用联系人", "合同真实性", "无物流", "无发票", "无验收", "回款异常", "交易异常"),
    "fund_diversion": ("资金挪用", "资金流向不明", "异常支付", "关联转账", "用途不符", "无合同资金"),
    "key_person": ("关键人员离职", "核心人员离职", "无人交接", "不可替代", "无继任", "关键人物依赖"),
    "collateral_enforceability": ("质押通知未送达", "未完成登记", "重复抵押", "在先抵押", "权属不清", "查封", "无法处置"),
}


def _signal_excerpt(text: str, terms: tuple[str, ...], max_chars: int = 1800) -> str:
    compact_chars: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        if not char.isspace():
            compact_chars.append(char)
            offsets.append(index)
    compact = "".join(compact_chars)
    matches = [
        (compact.find(re.sub(r"\s+", "", term)), len(re.sub(r"\s+", "", term)))
        for term in terms
        if term and compact.find(re.sub(r"\s+", "", term)) >= 0
    ]
    if not matches:
        return text[:max_chars]
    match_start, match_length = max(matches, key=lambda item: item[1])
    raw_match = offsets[match_start]
    left = max(0, raw_match - max_chars // 3)
    right = min(len(text), left + max_chars)
    if right < len(text):
        line_end = text.find("\n", right)
        if line_end >= right and line_end - left <= max_chars + 160:
            right = line_end
    if right <= raw_match + match_length:
        right = min(len(text), raw_match + match_length + max_chars // 2)
    return text[left:right]

EXTRACTION_TOPIC_TERMS = {
    "bond_terms": ("发行规模", "注册规模", "债券期限", "票面利率", "付息", "还本", "到期日", "发行对象", "主承销商"),
    "financial": ("营业收入", "净利润", "资产总额", "负债总额", "经营活动现金流", "应收账款", "利息支出"),
    "debt": ("有息负债", "短期借款", "长期借款", "一年内到期", "债务期限", "交叉违约", "授信"),
    "rating": ("主体信用等级", "债项信用等级", "评级展望", "评级下调", "调级因素", "评级方法", "有息负债"),
    "governance": ("实际控制人", "股权结构", "董事会", "关联方", "公司治理", "审批权限", "内部控制"),
    "operations": ("主营业务", "主要客户", "主要供应商", "客户集中", "供应商集中", "在手订单", "产能利用率"),
    "proceeds": ("募集资金用途", "募集资金运用", "专户", "用途变更", "资金使用计划", "历史募集资金"),
    "repayment": ("偿债来源", "偿债安排", "偿债专户", "经营现金流", "再融资", "股东支持", "本金偿付"),
    "guarantee": ("保证人", "保证方式", "保证范围", "保证期间", "责任上限", "增信措施", "担保责任"),
    "counter_guarantee": ("反担保", "抵押", "质押", "权属", "顺位", "估值", "登记", "查封"),
    "protection_legal": ("投资者保护", "持有人会议", "受托管理", "违约事项", "纠纷解决", "重大诉讼", "法律意见"),
    "risk": ("风险提示", "关注事项", "重大不利", "逾期", "或有负债", "关联交易", "资金挪用", "合同真实性"),
}


def retrieve_candidate_evidence(project_id: str, per_taxonomy: int = 8, cap: int = 36) -> list[dict]:
    evidence_map: dict[str, dict] = {}
    all_sources = rows(
        "SELECT s.*,d.kind,d.document_role FROM analysis_sources s JOIN analysis_documents d "
        "ON d.id=s.document_id AND d.project_id=s.project_id WHERE s.project_id=? ORDER BY s.id",
        (project_id,),
    )
    # Reserve a small number of explicit qualitative signal pages before BM25 so
    # a high-signal risk paragraph cannot be displaced by boilerplate pages.
    for taxonomy, terms in TAXONOMY_SIGNAL_TERMS.items():
        ranked = sorted(
            (source for source in all_sources if any(term in re.sub(r"\s+", "", source.get("text", "")) for term in terms)),
            key=lambda source: sum(re.sub(r"\s+", "", source.get("text", "")).count(term) for term in terms),
            reverse=True,
        )
        for source in ranked[:2]:
            excerpt = _signal_excerpt(str(source.get("text") or ""), terms)
            evidence_map.setdefault(
                source["id"],
                {**source, "text": excerpt},
            )
    for query in TAXONOMY_QUERIES.values():
        for source in retrieve(project_id, query, per_taxonomy):
            evidence_map.setdefault(source["id"], source)
    return list(evidence_map.values())[:cap]


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


def _node_finish(run_id: str, node: str, summary: str = "", usage: LlmUsage | None = None, status: str = "completed", error: str = "",
                 artifact_details: dict | None = None) -> None:
    usage = usage or LlmUsage()
    measured = row(
        "SELECT COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens,"
        "COALESCE(SUM(cost),0) cost,COALESCE(SUM(latency_ms),0) latency_ms FROM llm_attempts WHERE run_id=? AND node_key=?",
        (run_id, node),
    )
    if measured and (measured["input_tokens"] or measured["output_tokens"] or measured["cost"]):
        usage = LlmUsage(int(measured["input_tokens"]), int(measured["output_tokens"]), int(measured["latency_ms"]), float(measured["cost"]))
    with connect() as conn:
        conn.execute(
            "UPDATE run_nodes SET status=?,finished_at=?,input_tokens=?,output_tokens=?,cost=?,error=?,output_summary=? WHERE run_id=? AND node_key=?",
            (status, now_iso(), usage.input_tokens, usage.output_tokens, usage.cost, error, summary[:1000], run_id, node),
        )
    _record_artifact(run_id, node, status, details=artifact_details)


def _node_input_hash(project_id: str, node: str) -> str:
    index = NODE_KEYS.index(node)
    digest = hashlib.sha256()
    parts: list[object] = [
        rows("SELECT filename,kind,document_role,sha256,size,parse_status,quality,locator_count FROM documents WHERE project_id=? ORDER BY filename,sha256", (project_id,)),
        rows("SELECT document_id,locator_type,locator,content_hash FROM sources WHERE project_id=? ORDER BY document_id,locator", (project_id,)),
    ]
    if index >= NODE_KEYS.index("extract"):
        parts.append(retrieval_signature())
        snapshot = row(
            "SELECT s.id,s.version,s.content_hash FROM projects p JOIN project_profile_snapshots s "
            "ON s.id=p.profile_snapshot_id WHERE p.id=?",
            (project_id,),
        )
        parts.append(snapshot or {})
        parts.append(tranches_input_hash_component())
        parts.append(contract_sha256())
        parts.append(rows(
            "SELECT document_id,section_role,start_source_id,end_source_id,status,content_hash "
            "FROM document_sections WHERE project_id=? ORDER BY document_id,section_role",
            (project_id,),
        ))
    if index >= NODE_KEYS.index("metrics"):
        parts.append(rows("SELECT fact_key,subject_id,scope,period,status,value,unit,text_value,resolution_note FROM canonical_facts WHERE project_id=? ORDER BY fact_key,subject_id,scope,period", (project_id,)))
        parts.append(load_policy().sha256)
    if index >= NODE_KEYS.index("risk"):
        parts.append(rows("SELECT taxonomy,title,status,decision_note FROM candidate_findings WHERE project_id=? ORDER BY taxonomy,title", (project_id,)))
    if index >= NODE_KEYS.index("verify"):
        parts.append(rows("SELECT section_key,title,content,citation_ids,review_status FROM report_sections WHERE project_id=? ORDER BY section_key", (project_id,)))
    for part in parts:
        digest.update(json.dumps(part, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    return digest.hexdigest()


def invalidate_project_analysis(project_id: str, from_node: str = "extract") -> None:
    """Discard system-derived output after source inputs change.

    Original documents, source fragments, run history, audit history and facts
    explicitly verified by a human are retained. A later run rebuilds every
    derived table from the new material inventory.
    """
    if from_node not in NODE_KEYS:
        raise ValueError(f"未知失效节点：{from_node}")
    stale_nodes = NODE_KEYS[NODE_KEYS.index(from_node):]
    human_fact_ids = [
        item["id"] for item in rows(
            "SELECT id FROM facts WHERE project_id=? AND review_status='human_verified'",
            (project_id,),
        )
    ]
    with connect() as conn:
        conn.execute(
            f"UPDATE run_artifacts SET status='stale' WHERE project_id=? AND node_key IN ({','.join('?' for _ in stale_nodes)})",
            (project_id, *stale_nodes),
        )
        conn.execute("DELETE FROM facts WHERE project_id=? AND review_status!='human_verified'", (project_id,))
        conn.execute("DELETE FROM fact_assertions WHERE project_id=? AND extraction_method!='human'", (project_id,))
        if from_node == "extract":
            conn.execute("DELETE FROM document_sections WHERE project_id=?", (project_id,))
        for table in (
            "canonical_facts", "bond_tranches", "financial_metrics", "financial_tie_outs",
            "cross_checks", "candidate_findings", "review_tasks", "risks", "report_claims",
            "evidence_edges", "report_sections", "citations",
        ):
            conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        # Transaction subjects entered during project intake are project master
        # data, not Agent output. Keep them while discarding entities inferred
        # from the previous assertion set.
        conn.execute(
            "DELETE FROM project_entities WHERE project_id=? "
            "AND COALESCE(json_extract(attributes_json,'$.source'),'')!='project_intake'",
            (project_id,),
        )
        conn.execute(
            "UPDATE projects SET status='draft',analysis_stale=1,report_stale=1,"
            "report_stale_reasons=?,report_reviewed_revision=0,updated_at=? WHERE id=?",
            (json_dump(["analysis_input_changed"]), now_iso(), project_id),
        )
    if human_fact_ids:
        sync_fact_assertions(project_id)


def _artifact_ids(project_id: str, node: str) -> list[str]:
    mapping = {
        "inventory": ("documents",), "extract": ("facts", "fact_assertions"), "conflict": ("canonical_facts",),
        "metrics": ("financial_metrics", "financial_tie_outs"), "cross_check": ("cross_checks",),
        "candidate": ("candidate_findings",), "tasks": ("review_tasks",), "risk": ("risks",),
        "draft": ("report_sections", "citations"), "verify": ("report_claims", "evidence_edges"),
    }
    output = []
    for table in mapping.get(node, ()):
        output.extend(f"{table}:{item['id']}" for item in rows(f"SELECT id FROM {table} WHERE project_id=?", (project_id,)))
    return output


def _record_artifact(run_id: str, node: str, status: str, source_run_id: str = "", details: dict | None = None) -> None:
    run = row("SELECT project_id FROM runs WHERE id=?", (run_id,))
    if not run:
        return
    project_id = run["project_id"]
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO run_artifacts (id,run_id,project_id,node_key,input_hash,artifact_type,artifact_ids,status,source_run_id,details_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"artifact-{run_id}-{node}", run_id, project_id, node, _node_input_hash(project_id, node), node,
             json_dump(_artifact_ids(project_id, node)), status, source_run_id, json_dump(details or {}), now_iso()),
        )


def _reuse_node(run_id: str, project_id: str, node: str, progress: int, source_run_id: str) -> None:
    _update_run(run_id, node, progress, f"复用上游节点：{node}")
    previous = row("SELECT * FROM run_artifacts WHERE run_id=? AND node_key=? AND status IN ('completed','degraded')", (source_run_id, node))
    current_hash = _node_input_hash(project_id, node)
    if not previous or previous["input_hash"] != current_hash:
        raise RuntimeError(f"节点 {node} 输入已变化，不能复用上游产物")
    with connect() as conn:
        conn.execute(
            "INSERT INTO run_nodes (id,run_id,project_id,node_key,status,progress,attempt,started_at,finished_at,output_summary) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), run_id, project_id, node, "completed", progress, 0, now_iso(), now_iso(), f"复用自 {source_run_id}"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO run_artifacts (id,run_id,project_id,node_key,input_hash,artifact_type,artifact_ids,status,source_run_id,details_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"artifact-{run_id}-{node}", run_id, project_id, node, previous["input_hash"], previous["artifact_type"],
             previous["artifact_ids"], "completed", source_run_id, previous.get("details_json", "{}"), now_iso()),
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


def _apply_borrower_context(project_id: str, facts: list[dict]) -> list[dict]:
    project = row("SELECT company_name FROM projects WHERE id=?", (project_id,)) or {}
    borrower = project.get("company_name", "")
    output = []
    for fact in facts:
        item = dict(fact)
        definition = FACT_REGISTRY.get(item.get("fact_key", ""))
        if not definition:
            continue
        item["subject"] = item.get("subject") or borrower
        if item.get("scope") in {None, "", "unknown"}:
            item["scope"] = default_scope(definition.category)
        if item.get("period_type") in {None, "", "unknown"}:
            item["period_type"] = period_type(item["fact_key"], item.get("period", ""))
        output.append(item)
    return output


def _split_extraction_sources(document_sources: list[dict], max_chars: int, max_sources: int) -> list[list[dict]]:
    batches: list[list[dict]] = []
    batch: list[dict] = []
    characters = 0
    max_sources = max(1, max_sources)
    for source in document_sources:
        size = len(source["text"])
        if batch and (characters + size > max_chars or len(batch) >= max_sources):
            batches.append(batch)
            batch, characters = [], 0
        batch.append(source)
        characters += size
    if batch:
        batches.append(batch)
    return batches


def _select_model_extraction_sources(
    source_rows: list[dict],
    deterministic_facts: list[dict],
    *,
    max_sources: int,
    per_topic: int,
) -> list[dict]:
    """Select broad, evidence-rich pages for model-assisted fact extraction.

    Deterministic extraction always runs over every source. This selector only
    bounds the supplemental model pass for long documents.
    """
    max_sources = max(0, max_sources)
    per_topic = max(1, per_topic)
    if not source_rows or not max_sources:
        return []

    unique_sources: list[dict] = []
    seen_ids: set[str] = set()
    for source in source_rows:
        source_id = str(source.get("id", ""))
        if not source_id or source_id in seen_ids:
            continue
        seen_ids.add(source_id)
        unique_sources.append(source)
    if len(unique_sources) <= max_sources:
        return unique_sources

    fact_keys_by_source: dict[str, set[str]] = {}
    for fact in deterministic_facts:
        source_ids = fact.get("source_ids", [])
        source_ids = json_load(source_ids) if isinstance(source_ids, str) else source_ids
        for source_id in source_ids or []:
            fact_keys_by_source.setdefault(str(source_id), set()).add(str(fact.get("fact_key", "")))

    scored: list[dict] = []
    for index, source in enumerate(unique_sources):
        source_id = str(source["id"])
        compact = re.sub(r"\s+", "", str(source.get("text", "")))
        topic_scores = {
            topic: sum(compact.count(term) for term in terms)
            for topic, terms in EXTRACTION_TOPIC_TERMS.items()
        }
        matched_fields = sum(
            1
            for definition in FACT_REGISTRY.values()
            if definition.label in compact or any(alias in compact for alias in definition.aliases)
        )
        deterministic_fields = len(fact_keys_by_source.get(source_id, set()))
        scored.append({
            "source": source,
            "index": index,
            "topic_scores": topic_scores,
            "deterministic_fields": deterministic_fields,
            "matched_fields": matched_fields,
            "score": deterministic_fields * 100 + matched_fields * 10 + sum(topic_scores.values()),
        })

    selected: dict[str, dict] = {}

    def add(candidate: dict) -> None:
        if len(selected) < max_sources:
            selected.setdefault(str(candidate["source"]["id"]), candidate)

    retrieval_candidates = sorted(
        (item for item in scored if item["source"].get("_retrieval_priority")),
        key=lambda item: (-float(item["source"].get("_retrieval_priority") or 0), item["index"]),
    )
    for candidate in retrieval_candidates[:max(1, max_sources // 3)]:
        add(candidate)

    # Reserve evidence for each topic before globally high-scoring financial
    # pages consume the model context.
    for topic in EXTRACTION_TOPIC_TERMS:
        ranked = sorted(
            (item for item in scored if item["topic_scores"][topic] > 0),
            key=lambda item: (
                item["topic_scores"][topic], item["deterministic_fields"],
                item["matched_fields"], -item["index"],
            ),
            reverse=True,
        )
        for candidate in ranked[:per_topic]:
            add(candidate)

    # Keep at least one page from each uploaded document, including supporting
    # documents whose terminology may not match the registry.
    by_document: dict[str, list[dict]] = {}
    for item in scored:
        by_document.setdefault(str(item["source"].get("document_id", "")), []).append(item)
    for document_sources in by_document.values():
        add(max(document_sources, key=lambda item: (item["score"], -item["index"])))

    for candidate in sorted(scored, key=lambda item: (item["score"], -item["index"]), reverse=True):
        add(candidate)
        if len(selected) >= max_sources:
            break

    # Restore source order so adjacent pages from the same document remain
    # coherent in each model batch.
    return [item["source"] for item in sorted(selected.values(), key=lambda item: item["index"])]


def _run_model_totals(run_id: str) -> tuple[int, float]:
    if not run_id:
        return 0, 0.0
    totals = row(
        "SELECT COUNT(*) calls,COALESCE(SUM(cost),0) cost FROM llm_attempts WHERE run_id=?",
        (run_id,),
    ) or {"calls": 0, "cost": 0}
    return int(totals["calls"]), float(totals["cost"])


def _extraction_budget_available(run_id: str) -> bool:
    calls, cost = _run_model_totals(run_id)
    call_ceiling = max(0, settings.max_run_model_calls - settings.llm_downstream_reserved_calls)
    # One structured request can consume up to three attempts. Reserve those
    # attempts before starting a batch so downstream nodes retain their quota.
    has_call_budget = calls + 3 <= call_ceiling
    cost_ceiling = settings.max_run_model_cost_cny * settings.llm_extract_max_cost_ratio
    has_cost_budget = cost < cost_ceiling
    return has_call_budget and has_cost_budget


def _filter_redundant_model_facts(
    deterministic: list[dict], model_facts: list[dict], source_text: dict[str, str] | None = None,
    strict_source_support: bool = False,
) -> list[dict]:
    """Reject unsupported model assertions and deterministic/model overlaps."""
    deterministic_contexts: set[tuple] = set()
    deterministic_values: dict[tuple, set[str]] = {}
    deterministic_field_values: dict[tuple[str, str], set[tuple[float, str]]] = {}
    for fact in deterministic:
        source_ids = fact.get("source_ids", [])
        source_ids = json_load(source_ids) if isinstance(source_ids, str) else source_ids
        context = (
            fact["fact_key"], fact.get("subject", ""), fact.get("scope", "unknown"), fact.get("period", ""),
        )
        value = fact.get("value")
        value_key = (round(float(value), 6), fact.get("unit", "")) if value is not None else None
        for source_id in source_ids:
            deterministic_contexts.add((source_id, *context))
            if value_key is not None:
                deterministic_values.setdefault((source_id, fact.get("period", ""), *value_key), set()).add(fact["fact_key"])
                deterministic_field_values.setdefault((source_id, fact["fact_key"]), set()).add(value_key)

    output = []
    for fact in model_facts:
        source_ids = fact.get("source_ids", [])
        source_ids = json_load(source_ids) if isinstance(source_ids, str) else source_ids
        if source_text is not None and not any(
            source_id in source_text and source_supports_fact(fact["fact_key"], source_text[source_id])
            for source_id in source_ids
        ):
            continue
        if strict_source_support and (not source_ids or not all(
            source_id in source_text and source_supports_fact_value(fact, source_text[source_id])
            for source_id in source_ids
        )):
            continue
        context = (
            fact["fact_key"], fact.get("subject", ""), fact.get("scope", "unknown"), fact.get("period", ""),
        )
        if any((source_id, *context) in deterministic_contexts for source_id in source_ids):
            continue
        value = fact.get("value")
        if value is not None:
            value_key = (round(float(value), 6), fact.get("unit", ""))
            if not fact.get("period"):
                known_values = set().union(*(
                    deterministic_field_values.get((source_id, fact["fact_key"]), set())
                    for source_id in source_ids
                )) if source_ids else set()
                if known_values and value_key not in known_values:
                    continue
            owners = set().union(*(
                deterministic_values.get((source_id, fact.get("period", ""), *value_key), set())
                for source_id in source_ids
            )) if source_ids else set()
            if owners and fact["fact_key"] not in owners:
                continue
        output.append(fact)
    return output


_GENERIC_ISSUER_SUBJECTS = {"", "发行人", "本公司", "公司", "发行主体"}
_SUBSIDIARY_FINANCIAL_DISCLOSURE = re.compile(
    r"(?:子公司|下属公司|控股公司|孙公司).{0,80}(?:营业收入|净利润|资产总额|资产总计|负债总额|负债合计|财务状况|财务指标)"
)


def _compact_subject(value: object) -> str:
    return re.sub(r"[\s（）()\-]", "", str(value or ""))


def _source_has_subsidiary_financial_disclosure(source_text: str) -> bool:
    """Detect a named subsidiary financial passage without treating it as issuer data.

    The generic labelled-row extractor has no entity resolver.  In this narrow
    case rejection is safer than silently attaching a subsidiary's number to
    the issuer.  A later entity-profile import can retain the data under its
    own named subject with its original source.
    """
    return bool(_SUBSIDIARY_FINANCIAL_DISCLOSURE.search(re.sub(r"\s+", "", source_text or "")))


def _model_fact_section_reason(
    fact: dict, *, issuer: str, source_text: dict[str, str], source_sections: dict[str, object],
    source_scopes: dict[str, str] | None = None,
) -> str:
    """Return an empty string only for a model fact valid for its section contract."""
    source_ids = [str(source_id) for source_id in fact.get("source_ids", [])]
    sections = [source_sections.get(source_id) for source_id in source_ids]
    if not source_ids or any(section is None for section in sections):
        return "section_source_not_mapped"
    first = sections[0]
    if any(section.role != first.role for section in sections[1:]):
        return "cross_section_evidence"
    if fact.get("fact_key") not in set(first.allowed_fact_keys):
        return "fact_key_not_allowed_for_section"
    declared_scope = str(fact.get("scope") or "unknown")
    if declared_scope != "unknown" and declared_scope not in set(first.scope_policy):
        return "scope_not_allowed_for_section"
    if fact.get("fact_key") in FINANCIAL_FACT_KEYS and source_scopes:
        scopes = {source_scopes.get(source_id) for source_id in source_ids if source_scopes.get(source_id)}
        if len(scopes) > 1:
            return "mixed_financial_scope"
        inferred_scope = next(iter(scopes), None)
        if inferred_scope and declared_scope != inferred_scope:
            return "financial_scope_mismatch"
        if not inferred_scope and first.role in {"financial_statements", "financial_analysis"}:
            return "financial_scope_missing"
    if first.subject_policy == "issuer":
        subject = _compact_subject(fact.get("subject"))
        allowed_subjects = {_compact_subject(value) for value in (*_GENERIC_ISSUER_SUBJECTS, issuer)}
        if subject not in allowed_subjects:
            return "non_issuer_subject"
        if fact.get("value") is not None and any(
            _source_has_subsidiary_financial_disclosure(source_text.get(source_id, ""))
            for source_id in source_ids
        ):
            return "subsidiary_financial_disclosure"
    return ""


def _deterministic_fact_is_issuer_safe(fact: dict, section: object, source_text: dict[str, str]) -> bool:
    if section.subject_policy != "issuer" or fact.get("value") is None:
        return True
    source_ids = fact.get("source_ids", [])
    source_ids = json_load(source_ids) if isinstance(source_ids, str) else source_ids
    return not any(_source_has_subsidiary_financial_disclosure(source_text.get(str(source_id), "")) for source_id in source_ids)


def _extract_project_facts(run_id: str, project_id: str) -> tuple[int, LlmUsage, dict]:
    existing = rows("SELECT * FROM facts WHERE project_id=?", (project_id,))
    project_kind = row("SELECT project_type,amount FROM projects WHERE id=?", (project_id,)) or {}
    section_contract_current = bool(rows(
        "SELECT 1 FROM document_sections WHERE project_id=? AND contract_version=? LIMIT 1",
        (project_id, load_extraction_contract().version),
    )) if project_kind.get("project_type") == "bond_guarantee" else True
    # Bond disclosures must always pass through the active chapter contract.
    # Reference-project seed rows are retained only as stale historical audit
    # records; they must never satisfy this shortcut.
    can_reuse_existing = (
        existing
        and all(item.get("extraction_method", "seed") in {"seed", "human"} for item in existing)
        and (project_kind.get("project_type") != "bond_guarantee" or all(item.get("extraction_method") == "human" for item in existing))
        and (project_kind.get("project_type") != "bond_guarantee" or section_contract_current)
    )
    if can_reuse_existing:
        sync_fact_assertions(project_id, existing)
        source_count = row("SELECT COUNT(*) count FROM sources WHERE project_id=?", (project_id,)) or {"count": 0}
        return len(existing), LlmUsage(), {
            "total_sources": int(source_count["count"]), "selected_sources": 0, "planned_batches": 0,
            "completed_batches": 0, "budget_truncated": False,
        }
    source_rows = rows(
        "SELECT s.*,d.kind,d.document_role FROM sources s JOIN documents d ON d.id=s.document_id WHERE s.project_id=? ORDER BY d.created_at,s.rowid",
        (project_id,),
    )
    source_scopes = financial_scope_by_source(source_rows)
    project = row("SELECT project_type,amount,company_name FROM projects WHERE id=?", (project_id,)) or {}
    section_plans = []
    section_diagnostics: list[dict] = []
    section_usage = LlmUsage()
    if project.get("project_type") == "bond_guarantee":
        section_plans, section_diagnostics, section_usage = build_section_extraction_plans(
            project_id, source_rows, LlmCallContext(project_id, run_id, "extract", "section_locator"),
        )
        scoped_plan_sources = [
            {**source, "_section_role": plan.section.role}
            for plan in section_plans
            for source in plan.sources
        ]
        source_scopes.update(financial_scope_by_source(scoped_plan_sources))
        extract_bond_tranches(
            project_id, offering_term_sources(section_plans), project.get("amount"),
            context=LlmCallContext(project_id, run_id, "extract", "tranche"),
        )
    if section_plans:
        deterministic = []
        source_text = {str(source["id"]): str(source.get("text") or "") for source in source_rows}
        for plan in section_plans:
            extracted = deterministic_extract(list(plan.sources), set(plan.section.allowed_fact_keys))
            deterministic.extend({
                **item, "section_role": plan.section.role, "origin_ref": f"section:{plan.section.role}",
            } for item in extracted if _deterministic_fact_is_issuer_safe(item, plan.section, source_text))
    else:
        deterministic = []      # 债券章节均走分节抽取计划，此处不再有贷款式整篇抽取
    deterministic = _apply_borrower_context(project_id, deterministic)
    if not is_configured():
        sync_fact_assertions(project_id, deterministic)
        replace_extracted_facts(project_id, deterministic)
        if not deterministic:
            raise RuntimeError("MODEL_REQUIRED")
        return len(rows("SELECT id FROM facts WHERE project_id=?", (project_id,))), section_usage, {
            "total_sources": len(source_rows), "selected_sources": 0, "planned_batches": 0,
            "completed_batches": 0, "budget_truncated": False, "section_diagnostics": section_diagnostics,
        }

    model_facts: list[dict] = []
    total_usage = section_usage
    selected_sources: list[dict] = []
    planned_batches: list[list[dict]] = []
    retrieval_queries = 0
    retrieval_unchecked_fields = 0
    retrieval_unchecked_fact_keys: list[str] = []
    retrieval_no_hit_fact_keys: list[str] = []
    if project.get("project_type") == "bond_guarantee":
        by_document: dict[str, list[dict]] = {}
        allowed_by_source: dict[str, set[str]] = {}
        section_role_by_source: dict[str, str] = {}
        section_by_source: dict[str, object] = {}
        for plan in section_plans:
            for source in plan.sources:
                source_id = str(source["id"])
                by_document.setdefault(str(source["document_id"]), []).append(source)
                # A source at an ambiguous overlap is owned by its first
                # stable section.  Do not union two allowlists, which would
                # let one section's evidence authorize another's field.
                allowed_by_source.setdefault(source_id, set(plan.section.allowed_fact_keys))
                section_role_by_source.setdefault(source_id, plan.section.role)
                section_by_source.setdefault(source_id, plan.section)
        source_by_id = {str(item["id"]): item for item in source_rows}
        deterministic_keys_by_section: dict[str, set[str]] = {}
        for item in deterministic:
            deterministic_keys_by_section.setdefault(str(item.get("section_role") or ""), set()).add(str(item.get("fact_key") or ""))
        retrieved_extraction_sources: dict[str, dict] = {}
        for plan in section_plans:
            section_source_ids = {str(item["id"]) for item in plan.sources}
            allowed_keys = list(plan.section.allowed_fact_keys)
            missing_keys = [
                key for key in allowed_keys
                if key not in deterministic_keys_by_section.get(plan.section.role, set())
            ]
            for fact_key in missing_keys:
                if retrieval_queries >= settings.retrieval_extract_query_limit:
                    retrieval_unchecked_fields += 1
                    retrieval_unchecked_fact_keys.append(fact_key)
                    continue
                definition = FACT_REGISTRY.get(fact_key)
                if not definition or not section_source_ids:
                    continue
                query = " ".join([definition.label, *definition.aliases])
                retrieval_queries += 1
                try:
                    hits = retrieve_chunks(
                        project_id, query, top_k=2, source_ids=section_source_ids,
                    )
                except Exception:
                    hits = []
                if not hits:
                    retrieval_no_hit_fact_keys.append(fact_key)
                for rank, hit in enumerate(hits, 1):
                    source_id = str(hit["source_id"])
                    if section_role_by_source.get(source_id) != plan.section.role:
                        continue
                    original = source_by_id.get(source_id)
                    quote = str(hit.get("text") or "").strip()
                    if not original or not quote:
                        continue
                    entry = retrieved_extraction_sources.setdefault(source_id, {
                        **original, "text": "", "_retrieval_priority": 0.0,
                    })
                    if quote not in entry["text"]:
                        entry["text"] = "\n".join(filter(None, (entry["text"], quote)))
                    entry["_retrieval_priority"] += 1 / (60 + rank)
        for source in retrieved_extraction_sources.values():
            by_document.setdefault(str(source["document_id"]), []).append(source)
        for document_sources in by_document.values():
            unique = list({str(item["id"]): item for item in document_sources}.values())
            selected = _select_model_extraction_sources(
                unique, deterministic,
                max_sources=max(1, settings.llm_extract_batch_max_sources * 4),
                per_topic=settings.llm_extract_sources_per_topic,
            )
            if selected:
                planned_batches.append(selected)
                selected_sources.extend(selected)
    else:
        max_model_sources = settings.llm_extract_max_batches * settings.llm_extract_batch_max_sources
        selected_sources = _select_model_extraction_sources(
            source_rows, deterministic, max_sources=max_model_sources,
            per_topic=settings.llm_extract_sources_per_topic,
        )
        planned_batches = _split_extraction_sources(
            selected_sources, settings.llm_batch_max_chars, settings.llm_extract_batch_max_sources,
        )[:settings.llm_extract_max_batches]
    completed_batches = 0
    budget_truncated = False
    rejected_model_diagnostics: list[dict] = []
    for batch in planned_batches:
        if _cancelled(run_id):
            break
        if not _extraction_budget_available(run_id):
            budget_truncated = True
            break
        try:
            allowed_keys = None
            if project.get("project_type") == "bond_guarantee":
                allowed_keys = set().union(*(allowed_by_source.get(str(item["id"]), set()) for item in batch))
            extracted, usage = extract_facts(
                batch, LlmCallContext(project_id, run_id, "extract", "extract"),
                allowed_fact_keys=allowed_keys,
                source_section_roles=(
                    {str(item["id"]): section_role_by_source[str(item["id"])] for item in batch}
                    if project.get("project_type") == "bond_guarantee" else None
                ),
            )
        except StructuredLlmError as exc:
            if exc.error_type != "budget_exhausted":
                raise
            _add_usage(total_usage, exc.usage)
            budget_truncated = True
            break
        if project.get("project_type") == "bond_guarantee":
            accepted = []
            for item in extracted:
                source_ids = [str(source_id) for source_id in item.get("source_ids", [])]
                reason = ""
                if not source_ids or not item.get("section_role"):
                    reason = "missing_section_or_source"
                elif any(section_role_by_source.get(source_id) != item["section_role"] for source_id in source_ids):
                    reason = "source_section_mismatch"
                elif any(item["fact_key"] not in allowed_by_source.get(source_id, set()) for source_id in source_ids):
                    reason = "fact_key_not_allowed_for_source"
                else:
                    reason = _model_fact_section_reason(
                        item,
                        issuer=str(project.get("company_name") or ""),
                        source_text={str(source["id"]): str(source.get("text") or "") for source in source_rows},
                        source_sections=section_by_source,
                        source_scopes=source_scopes,
                    )
                if reason:
                    rejected_model_diagnostics.append({
                        "reason": reason,
                        "fact_key": str(item.get("fact_key") or ""),
                        "source_ids": source_ids,
                    })
                    continue
                accepted.append(item)
            extracted = accepted
        model_facts.extend(_apply_borrower_context(project_id, [{
            **_normalize_model_fact(item), "origin_ref": f"section:{item.get('section_role', '')}",
        } for item in extracted]))
        _add_usage(total_usage, usage)
        completed_batches += 1
    model_facts = _filter_redundant_model_facts(
        deterministic, model_facts, {source["id"]: source["text"] for source in source_rows},
        strict_source_support=project.get("project_type") == "bond_guarantee",
    )
    human = [item for item in existing if item.get("review_status") == "human_verified"]
    sync_fact_assertions(project_id, [*deterministic, *model_facts, *human])
    replace_extracted_facts(project_id, merge_facts(deterministic, model_facts))
    extraction_stats = {
        "total_sources": len(source_rows),
        "selected_sources": len(selected_sources),
        "planned_batches": len(planned_batches),
        "completed_batches": completed_batches,
        "budget_truncated": budget_truncated,
        "section_diagnostics": section_diagnostics,
        "retrieval_queries": retrieval_queries,
        "retrieval_unchecked_fields": retrieval_unchecked_fields,
        "retrieval_unchecked_fact_keys": retrieval_unchecked_fact_keys,
        "retrieval_no_hit_fact_keys": retrieval_no_hit_fact_keys,
    }
    # Keep the historical stats shape for non-bond projects while exposing
    # richer diagnostics for the chapter-scoped bond extractor.
    if project.get("project_type") == "bond_guarantee":
        extraction_stats["extraction_diagnostics"] = [*section_diagnostics, *rejected_model_diagnostics]
    return len(rows("SELECT id FROM facts WHERE project_id=?", (project_id,))), total_usage, extraction_stats


def _add_usage(total: LlmUsage, current: LlmUsage) -> None:
    total.input_tokens += current.input_tokens
    total.output_tokens += current.output_tokens
    total.latency_ms += current.latency_ms
    total.cost += current.cost


def _reject_last_llm_attempt(run_id: str, node_key: str, error_type: str, message: str) -> None:
    latest = row(
        "SELECT id FROM llm_attempts WHERE run_id=? AND node_key=? AND status='completed' ORDER BY created_at DESC LIMIT 1",
        (run_id, node_key),
    )
    if latest:
        with connect() as conn:
            conn.execute("UPDATE llm_attempts SET status='rejected',error_type=?,error_message=? WHERE id=?",
                         (error_type, message[:200], latest["id"]))


def _citations(project_id: str, source_ids: list[str]) -> list[str]:
    citation_ids = []
    for source_id in dict.fromkeys(source_ids):
        source = analysis_source(project_id, source_id)
        if not source:
            continue
        citation_id = f"c-{source_id}"
        label = source["filename"]
        if source.get("origin_type") == "profile_snapshot":
            label = f"{label}（主体档案第{source.get('profile_version') or '?'}版）"
        with connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO citations (id,project_id,source_id,document_id,label,quote,locator_type,locator) VALUES (?,?,?,?,?,?,?,?)",
                (citation_id, project_id, source_id, source["document_id"], label,
                 source["text"][:1200], source["locator_type"], source["locator"]),
            )
        citation_ids.append(citation_id)
    return citation_ids


def _limit_chapter_citations(citation_ids: list[str], context: dict | None = None,
                             chapter_key: str | None = None) -> list[str]:
    """Keep report citations concise without restricting the evidence visible to the model.

    ``allowed_citation_ids`` preserves the deterministic evidence ranking from the
    chapter context.  It is deliberately used only as a fallback: when the model
    returns valid references, its ordering remains visible in the final report.
    """
    config = (context or {}).get("prompt_config") or load_chapter_config(chapter_key or "")
    limit = int((config.get("output_contract") or {}).get("max_citations", 3))
    if limit <= 0:
        return []
    allowed_order = (
        list((context or {}).get("allowed_citation_ids") or [])
        if context is not None
        else list(dict.fromkeys(citation_ids))
    )
    allowed = set(allowed_order)
    selected: list[str] = []
    for citation_id in citation_ids:
        if citation_id in allowed and citation_id not in selected:
            selected.append(citation_id)
    for citation_id in allowed_order:
        if citation_id not in selected:
            selected.append(citation_id)
    return selected[:limit]


def _source_ids(items: list[dict]) -> list[str]:
    output: list[str] = []
    for item in items:
        source_ids = item.get("source_ids", [])
        source_ids = json_load(source_ids) if isinstance(source_ids, str) else source_ids
        output.extend(source_ids)
    return list(dict.fromkeys(output))


def _stored_rule_results(project_id: str) -> tuple[list[RuleResult], list[dict]]:
    stored = rows("SELECT * FROM cross_checks WHERE project_id=? ORDER BY rowid", (project_id,))
    results = [RuleResult(
        item["rule_key"], item["title"], item["severity"], item["status"], item["summary"],
        json_load(item["values_json"]), json_load(item["source_ids"]), item["formula"], json_load(item["fact_ids"]),
        json_load(item["thresholds"]), item["result_value"], item["rationale"], item["policy_version"],
    ) for item in stored]
    decoded = [{**item, "values": json_load(item["values_json"]), "source_ids": json_load(item["source_ids"]),
                "fact_ids": json_load(item["fact_ids"]), "thresholds": json_load(item["thresholds"])} for item in stored]
    return results, decoded


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
    """报告章节模板。产品已收敛为**仅债券担保**，因此只走债券生成器。"""
    from .bond_report import generate_bond_chapters

    chapters = generate_bond_chapters(project["id"], False)
    return [SectionTemplate(item.key, item.title, item.content, item.source_ids) for item in chapters]


def _numbers_supported(content: str, original: str, evidence: list[dict]) -> bool:
    from .delivery_wording import strip_references

    # 资料编号与页码是引用定位，不是数据值；两侧都先剔除，否则「〔资料01〕」
    # 会被读成数字 01，正文被误判为含未支持数字。
    allowed_text = strip_references(original) + " " + " ".join(strip_references(item["quote"]) for item in evidence)
    allowed = set(re.findall(r"-?\d+(?:\.\d+)?", allowed_text))
    produced = set(re.findall(r"-?\d+(?:\.\d+)?", strip_references(content)))
    return produced <= allowed


def _report_boundary_safe(content: str) -> bool:
    prohibited = (
        "建议提供担保", "建议通过担保", "建议暂缓担保", "建议拒绝担保", "同意担保", "拒绝担保",
        "审批通过", "核定担保额度", "核定担保费率", "审批倾向",
    )
    return not any(item in content for item in prohibited)


def _attribution_block_missing(content: str, context: dict) -> bool:
    """模型的正文是否漏掉了系统生成的数据来源边界块。"""
    block = str(context.get("source_attribution_block") or "")
    return bool(block) and block not in content


def _preserve_chapter_boundaries(content: str, context: dict) -> str:
    """Re-attach deterministic blocks the model is required to preserve."""
    additions: list[str] = []
    conflict_block = context.get("conflict_block", "")
    if conflict_block and conflict_block not in content:
        additions.append(conflict_block)
    # 系统数据表与冲突块同理：模型或回退都不得删除。计划明确要求
    # 「模型或报告回退不得删除表格和冲突披露」。
    table_block = str(context.get("system_table_block") or "")
    if table_block and table_block not in content:
        additions.append(table_block)
    attribution_block = str(context.get("source_attribution_block") or "")
    if attribution_block and attribution_block not in content:
        additions.append(attribution_block)
    if context.get("has_missing"):
        markers = context.get("prompt_config", {}).get("output_contract", {}).get("required_missing_markers", [])
        if markers and not any(marker in content for marker in markers):
            additions.append("材料不足：以上章节须取得对应原件后再行核验。")
    return content.rstrip() + ("\n\n" + "\n\n".join(additions) if additions else "")


def repair_chapter_length(title: str, content: str, evidence: list[dict], *, chapter_key: str,
                          chapter_context: dict, call_context: LlmCallContext,
                          original_result: "LlmSection", original_validation: dict) -> tuple["LlmSection", dict, LlmUsage]:
    """篇幅超出目标上限时的一次定向压缩。完整生成与单章再生成共用这一份口径。

    返回 ``(采用的正文, 校验结果, 本次用量)``：

    - 压缩稿安全通过 → 采用压缩稿，**仍然偏长也保留**：篇幅是写作目标，不是交付门槛；
    - 压缩稿引入新数字、无效引用或禁止结论 → 保留压缩前的合格正文，只记录这次压缩失败；
    - 两条分支都不会因为篇幅回退到确定性草稿，也不会阻断交付。

    只在安全校验全过、仅篇幅超限时调用；函数自身不再触发任何修复，杜绝
    「润色 → 压缩 → 再压缩」的调用链。
    """
    repaired, usage = rewrite_section(
        title, content, evidence, call_context,
        chapter_key=chapter_key, chapter_context=chapter_context,
        strict_citations=True, length_repair=original_validation["length"],
    )
    # 先量"模型有没有漏掉来源边界块"，再补齐——补齐之后这个判断恒为真。
    attribution_restored = _attribution_block_missing(repaired.content, chapter_context)
    repaired.content = _preserve_chapter_boundaries(repaired.content, chapter_context)
    revalidation = validate_chapter_output(repaired.content, repaired.citation_ids, chapter_context)
    revalidation["source_attribution_restored"] = attribution_restored
    if not _report_boundary_safe(repaired.content):
        revalidation["reasons"].append("章节包含禁止的承保或审批结论")
        revalidation["status"] = "rejected"
        revalidation["length_only_failure"] = False
    if revalidation["status"] == "passed":
        return repaired, revalidation, usage
    return original_result, {
        **original_validation,
        "length_repair_failed": True,
        "length_repair_reasons": list(revalidation["reasons"]),
    }, usage


def _generate_draft(run_id: str, project_id: str, project: dict, facts: list[dict], inventory: dict,
                    total_usage: LlmUsage, *, allow_llm: bool = True,
                    section_keys: set[str] | None = None) -> tuple[int, LlmUsage]:
    stored_risks = rows("SELECT * FROM risks WHERE project_id=?", (project_id,))
    stored_checks = rows("SELECT * FROM cross_checks WHERE project_id=?", (project_id,))
    with connect() as conn:
        if section_keys is None:
            conn.execute("DELETE FROM report_sections WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM citations WHERE project_id=?", (project_id,))
        elif section_keys:
            placeholders = ",".join("?" for _ in section_keys)
            conn.execute(
                f"DELETE FROM report_sections WHERE project_id=? AND section_key IN ({placeholders})",
                (project_id, *sorted(section_keys)),
            )
    section_count = 0
    draft_usage = LlmUsage()
    templates = _templates(project, facts, stored_checks, stored_risks, inventory)
    if section_keys is not None:
        templates = [item for item in templates if item.key in section_keys]
    for template in templates:
        if _cancelled(run_id):
            break
        chapter_context = build_chapter_context(project_id, template.key, template.content, template.source_ids)
        deterministic_content = chapter_context.get("draft", template.content)
        context_source_ids = [item["source_id"] for item in chapter_context.get("evidence", [])]
        all_citation_ids = _citations(project_id, context_source_ids)
        citation_ids = _limit_chapter_citations(all_citation_ids, chapter_context, template.key)
        evidence_rows = [item for citation_id in all_citation_ids if (item := row("SELECT id,source_id,quote,locator_type,locator FROM citations WHERE id=?", (citation_id,)))]
        content, safe_citations = deterministic_content, citation_ids
        chapter_validation = {"status": "passed", "reasons": []}
        fallback_reason = ""
        repair_attempted = False
        # 生成状态与证据支持状态必须分开：前者说明"模型结果有没有真正进入报告"，
        # 后者由 validate_report_support 事后按证据计算，不得互相覆盖。
        generation_status = "deterministic" if not chapter_uses_llm(template.key) else "skipped"
        if allow_llm and citation_ids and is_configured() and chapter_uses_llm(template.key) and chapter_context.get("llm_applicable", True):
            try:
                result, usage = rewrite_section(
                    template.title, deterministic_content, evidence_rows,
                    LlmCallContext(project_id, run_id, "draft", "report"),
                    chapter_key=template.key,
                    chapter_context=chapter_context,
                    strict_citations=True,
                )
                _add_usage(total_usage, usage)
                _add_usage(draft_usage, usage)
                attribution_restored = _attribution_block_missing(result.content, chapter_context)
                result.content = _preserve_chapter_boundaries(result.content, chapter_context)
                chapter_validation = validate_chapter_output(result.content, result.citation_ids, chapter_context)
                chapter_validation["source_attribution_restored"] = attribution_restored
                if not _report_boundary_safe(result.content):
                    chapter_validation["reasons"].append("章节包含禁止的承保或审批结论")
                    chapter_validation["status"] = "rejected"
                    chapter_validation["length_only_failure"] = False
                if chapter_validation.get("length_only_failure"):
                    # 安全校验全过、仅篇幅超出目标上限 → 最多一次定向压缩（复用同一上下文）
                    repair_attempted = True
                    result, chapter_validation, repair_usage = repair_chapter_length(
                        template.title, deterministic_content, evidence_rows,
                        chapter_key=template.key, chapter_context=chapter_context,
                        call_context=LlmCallContext(project_id, run_id, "draft", "report"),
                        original_result=result, original_validation=chapter_validation,
                    )
                    _add_usage(total_usage, repair_usage)
                    _add_usage(draft_usage, repair_usage)
                safe_citations = _limit_chapter_citations(list(result.citation_ids), chapter_context, template.key)
                content = result.content
                if chapter_validation["status"] != "passed":
                    fallback_reason = "；".join(chapter_validation["reasons"]) or "章节润色包含非法引用、新数字或审批结论"
                    _reject_last_llm_attempt(run_id, "draft", "report_validation", fallback_reason)
                    content, safe_citations = deterministic_content, citation_ids
                    # 保留被拒版本的诊断信息：回退原因必须能定位到具体数字/表达，
                    # 否则界面上只剩一句笼统的"新数字"。
                    rejected_diagnostics = validation_diagnostics(chapter_validation)
                    chapter_validation = {"status": "fallback", "reasons": [fallback_reason], **rejected_diagnostics}
                generation_status = "model_passed" if chapter_validation["status"] == "passed" else "fallback"
            except Exception as exc:
                fallback_reason = f"章节模型调用或结构校验失败：{type(exc).__name__}"
                content, safe_citations = deterministic_content, citation_ids
                chapter_validation = {"status": "fallback", "reasons": [fallback_reason]}
                generation_status = "fallback"
        if _cancelled(run_id):
            break
        # 统一写入入口：正文与来源/状态/篇幅/哈希/冻结披露在同一事务内落库，
        # 避免出现"正文已是模板、元数据仍说模型已采用"这类不一致。
        required_disclosures = [block for block in (
            chapter_context.get("conflict_block"), chapter_context.get("system_table_block"),
            chapter_context.get("source_attribution_block"),
        ) if block]
        write_report_section(
            project_id=project_id, section_key=template.key, title=template.title,
            section_id=str(uuid.uuid4()), content=content,
            citation_ids=safe_citations or citation_ids, allowed_source_ids=context_source_ids,
            content_origin="model" if generation_status == "model_passed" else "deterministic",
            generation_status=generation_status, fallback_reason=fallback_reason,
            fallback_stage="chapter_validation" if fallback_reason else "none",
            required_disclosures=required_disclosures, deterministic_draft=deterministic_content,
            table_records=chapter_context.get("table_records") or [],
            length_policy=chapter_context.get("length_policy") or {},
            diagnostics=validation_diagnostics(chapter_validation),
            prompt_version=chapter_context.get("prompt_version", ""),
            prompt_sha256=chapter_context.get("prompt_sha256", ""),
            input_hash=chapter_context.get("input_hash", ""),
            support_status="pending",
        )
        if repair_attempted:
            execute("UPDATE report_sections SET length_repair_attempted=1 WHERE project_id=? AND section_key=?",
                    (project_id, template.key))
        from .professional import audit_event
        audit_event(
            project_id, "chapter_generate", "report_section", template.key,
            {}, {"validation_status": chapter_validation["status"], "citation_ids": safe_citations or citation_ids,
                 "prompt_version": chapter_context.get("prompt_version", ""),
                 "prompt_sha256": chapter_context.get("prompt_sha256", ""),
                 "input_hash": chapter_context.get("input_hash", ""),
                 "generation_status": generation_status,
                 "model_attempted": generation_status in {"model_passed", "fallback"},
                 "model_output_accepted": generation_status == "model_passed",
                 "fallback_reason_code": fallback_reason_code(fallback_reason),
                 "length_repair_attempted": repair_attempted,
                 "validation_diagnostics": validation_diagnostics(chapter_validation),
                 "fallback_reason": fallback_reason}, "agent",
        )
        if fallback_reason:
            audit_event(project_id, "chapter_fallback", "report_section", template.key,
                        {}, {"status": chapter_validation["status"], "reason": fallback_reason,
                             "fallback_reason_code": fallback_reason_code(fallback_reason),
                             "prompt_version": chapter_context.get("prompt_version", ""),
                             "input_hash": chapter_context.get("input_hash", "")})
        section_count += 1
    _fallback_unsupported_sections(project_id, {item.key: item for item in templates}, section_keys=section_keys)
    return section_count, draft_usage


def _candidate_passes_delivery(project_id: str, section: dict, content: str, citation_ids: list[str],
                               citation_map: dict, source_texts: dict, valid_sources: set,
                               required_disclosures: list[str]) -> bool:
    """候选正文是否通过"全部校验"：引用合法 + 主张有支持 + 强制披露完整（不含软性篇幅下限）。"""
    from .p0 import assess_section_claims

    if not content.strip():
        return False
    if any(item not in citation_map for item in citation_ids):
        return False
    if any(block and block not in content for block in required_disclosures):
        return False
    probe = {**section, "content": content, "citation_ids": json_dump(citation_ids)}
    assessment = assess_section_claims(project_id, probe, citation_map, source_texts, valid_sources)
    return assessment["unsupported_claims"] == 0 and assessment["invalid_citations"] == 0


def _fallback_unsupported_sections(project_id: str, templates: dict[str, SectionTemplate],
                                   *, section_keys: set[str] | None = None) -> int:
    """一次候选回退：用**生成时冻结的确定性草稿**替换未通过支持校验的章节正文。

    与旧实现的三点差别（2026-09-19 交付一致性缺陷）：
    1. 只做一遍，第二轮「20 字占位符降级」已删除——不再把占位符写进报告；
    2. 候选正文取自冻结的 `deterministic_draft`（含冲突块与来源边界块），
       而不是裸 `template.content`，回退不会丢强制披露；
    3. 无论替换还是保留，都经统一写入入口同步来源/状态/阶段/诊断/篇幅/哈希，
       旧实现只改 content 与 support_rate，导致界面显示"模型已采用"而正文是模板。
    """
    from .p0 import _support_context

    quality = validate_report_support(project_id)
    if not quality["unsupported_claims"] and not quality["invalid_citations"]:
        return 0
    citation_map, source_texts, valid_sources = _support_context(project_id)
    unsupported_sections = {
        item["section_id"] for item in rows(
            "SELECT DISTINCT section_id FROM report_claims WHERE project_id=? AND support_status='unsupported'",
            (project_id,),
        )
    }
    sections = rows("SELECT * FROM report_sections WHERE project_id=? ORDER BY rowid", (project_id,))
    if section_keys is not None:
        sections = [item for item in sections if item["section_key"] in section_keys]

    fallback_count = 0
    for section in sections:
        citation_ids = json_load(section["citation_ids"])
        invalid = any(item not in citation_map for item in citation_ids)
        if section["id"] not in unsupported_sections and not invalid:
            continue
        template = templates.get(section["section_key"])
        required_disclosures = json_load(section.get("required_disclosures") or "[]")
        candidate = str(section.get("deterministic_draft") or "")
        for block in required_disclosures:
            if block and block not in candidate:
                candidate = f"{candidate}\n\n{block}".strip() if candidate else ""
        candidate_citations = _limit_chapter_citations(
            _citations(project_id, template.source_ids), chapter_key=section["section_key"],
        ) if template else []
        # 候选引用是刚刚创建的，引用映射在此之前已经取好——不补进来，候选稿会被
        # 误判成"引用非法"而永远回退失败。
        for citation_id in candidate_citations:
            if citation_id not in citation_map:
                fresh = row("SELECT * FROM citations WHERE id=?", (citation_id,))
                if fresh:
                    citation_map[citation_id] = fresh
        # 失败现场：在重建 report_claims 之前留下具体主张与原因码
        unsupported_claims = [
            {"claim_index": item["claim_index"], "claim_text": item["claim_text"],
             "reason_code": "support_unverified", "unsupported_numbers": json_load(item["unsupported_numbers"])}
            for item in rows(
                "SELECT claim_index,claim_text,unsupported_numbers FROM report_claims "
                "WHERE section_id=? AND support_status='unsupported' ORDER BY claim_index LIMIT 20",
                (section["id"],),
            )
        ]
        before = {"content": section["content"], "citation_ids": citation_ids,
                  "content_origin": section.get("content_origin") or "",
                  "generation_status": section.get("generation_status") or "",
                  "content_sha256": section.get("content_sha256") or ""}
        length_policy = {
            "effective_min_chars": section.get("effective_min_chars") or 0,
            "effective_max_chars": section.get("effective_max_chars") or 0,
        }
        if _candidate_passes_delivery(project_id, section, candidate, candidate_citations,
                                      citation_map, source_texts, valid_sources, required_disclosures):
            # 确定性章节自身的回退**不算模型失败**：阶段照记，生成状态保持确定性，
            # 否则采用率的分母会把从未调用模型的章节算进去。
            uses_llm = chapter_uses_llm(section["section_key"])
            write_report_section(
                project_id=project_id, section_key=section["section_key"], content=candidate,
                citation_ids=candidate_citations,
                allowed_source_ids=[citation_map[item]["source_id"] for item in candidate_citations
                                    if item in citation_map] or json_load(section.get("allowed_source_ids") or "[]"),
                content_origin="deterministic",
                generation_status="fallback" if uses_llm else "deterministic",
                fallback_reason=(
                    "模型正文未通过后置证据检查，已回退确定性模板" if uses_llm
                    else "章节正文未通过后置证据检查，已回退确定性模板"
                ),
                fallback_stage="report_support", required_disclosures=required_disclosures,
                length_policy=length_policy, diagnostics=json_load(section.get("validation_diagnostics") or "{}"),
                prompt_version=section.get("prompt_version") or "",
                prompt_sha256=section.get("prompt_sha256") or "",
                input_hash=section.get("input_hash") or "", support_status="pending",
            )
            outcome = "replaced_with_deterministic_draft"
        else:
            # 无法可靠构建合格替代稿：保留可读正文供人工核验，不静默删除、不写占位符
            write_report_section(
                project_id=project_id, section_key=section["section_key"], content=section["content"],
                citation_ids=citation_ids, allowed_source_ids=json_load(section.get("allowed_source_ids") or "[]"),
                content_origin=section.get("content_origin") or "unknown",
                generation_status=section.get("generation_status") or "",
                fallback_reason="部分主张尚未获得证据支持，已保留正文供人工核验",
                fallback_stage="report_support", required_disclosures=required_disclosures,
                length_policy=length_policy, diagnostics=json_load(section.get("validation_diagnostics") or "{}"),
                prompt_version=section.get("prompt_version") or "",
                prompt_sha256=section.get("prompt_sha256") or "",
                input_hash=section.get("input_hash") or "", support_status="warning",
            )
            outcome = "kept_for_human_review"
        audit_event(project_id, "fallback", "report_section", section["id"], before,
                    {"outcome": outcome, "fallback_stage": "report_support",
                     "reason_code": "support_unverified", "unsupported_claims": unsupported_claims,
                     "citation_ids": citation_ids, "content_sha256": content_sha256(section["content"])})
        fallback_count += 1
    validate_report_support(project_id)
    return fallback_count


_FACT_SECTION_KEYS = {
    "bond": {"scope", "bond_overview", "proceeds", "guarantee", "counter_guarantee", "investor_protection"},
    "rating": {"rating", "bond_overview", "risk_matrix"},
    "financial": {"financial", "cashflow", "debt", "risk_matrix", "evidence"},
    "debt": {"debt", "cashflow", "financial", "risk_matrix", "evidence"},
    "governance": {"governance", "legal", "risk_matrix", "evidence"},
    "operations": {"operations", "cashflow", "risk_matrix", "evidence"},
}
_DEFAULT_FACT_SECTIONS = {"scope", "bond_overview", "risk_matrix", "tasks", "monitoring", "evidence"}


def _affected_report_sections(reasons: list[str]) -> set[str]:
    affected: set[str] = set()
    for reason in reasons:
        if reason == "candidate":
            affected.update({"risk_matrix", "tasks", "monitoring", "evidence"})
            continue
        if reason == "task":
            affected.update({"tasks", "monitoring", "evidence", "scope"})
            continue
        if reason.startswith("fact:"):
            definition = FACT_REGISTRY.get(reason.split(":", 1)[1])
            affected.update(_FACT_SECTION_KEYS.get(definition.category if definition else "", _DEFAULT_FACT_SECTIONS))
    return affected or set(_DEFAULT_FACT_SECTIONS)


def _store_review_rule_results(project_id: str, checks: list[RuleResult], facts: list[dict]) -> list[dict]:
    """Persist deterministic rule results used by both a full run and review apply."""
    stored: list[dict] = []
    with connect() as conn:
        conn.execute("DELETE FROM cross_checks WHERE project_id=?", (project_id,))
        for check in checks:
            check_id = str(uuid.uuid4())
            check_facts = [item for item in facts if item.get("id") in check.fact_ids]
            subject_id = check_facts[0].get("subject_id", "") if check_facts else ""
            scope = check_facts[0].get("scope", "unknown") if check_facts else "unknown"
            trace = {"formula": check.formula, "fact_ids": check.fact_ids, "values": check.values, "thresholds": check.thresholds}
            conn.execute(
                "INSERT INTO cross_checks (id,project_id,rule_key,title,severity,status,summary,values_json,source_ids,formula,fact_ids,policy_version,thresholds,result_value,rationale,subject_id,scope,trace_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (check_id, project_id, check.key, check.title, check.severity, check.status, check.summary,
                 json_dump(check.values), json_dump(check.source_ids), check.formula, json_dump(check.fact_ids),
                 check.policy_version, json_dump(check.thresholds), check.result_value, check.rationale,
                 subject_id, scope, json_dump(trace)),
            )
            stored.append({**check.__dict__, "id": check_id, "rule_key": check.key})
    return stored


def _sync_rule_risks(project_id: str, checks: list[RuleResult]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM risks WHERE project_id=? AND source_type='rule'", (project_id,))
        for risk in risks_from_checks(checks):
            conn.execute(
                "INSERT INTO risks (id,project_id,severity,category,title,description,impact,action,source_ids,likelihood,impact_score,source_type,candidate_id,condition_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), project_id, risk["severity"], risk["category"], risk["title"],
                 risk["description"], risk["impact"], risk["action"], json_dump(risk["source_ids"]),
                 risk.get("likelihood", 2), risk.get("impact_score", 2), "rule", "", risk.get("condition_text", "")),
            )


def apply_review_decisions(project_id: str) -> dict:
    """Apply a batch of human decisions without using the model.

    The report update is deterministic, scoped to changed decision types and
    published only when the revision captured at start is still current.
    """
    from .review_lifecycle import mark_report_current, report_state

    project = row("SELECT * FROM projects WHERE id=?", (project_id,))
    if not project:
        raise ValueError("项目不存在")
    active = row("SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running') LIMIT 1", (project_id,))
    if active:
        raise ValueError("Agent 正在运行，暂不能应用人工复核")
    state = report_state(project_id)
    if not state["report_stale"]:
        return {**state, "affected_section_keys": [], "applied": False}
    revision = state["review_revision"]
    reasons = list(state["report_stale_reasons"])

    sync_project_entities(project_id)
    groups = fact_groups(project_id)
    conflicts = [item for item in groups if item["status"] == "conflicted"]
    facts = analysis_fact_rows(project_id)
    inventory = material_inventory(project_id)
    policy = load_policy("bond-v1")
    metrics = compute_and_store_metrics(project_id, float(project["amount"]), policy.version)
    tie_outs = compute_financial_tie_outs(project_id, policy.version)
    # 产品只做债券担保：规则引擎固定为债券规则。
    from .bond_rules import evaluate_bond_rules
    tranches = rows(
        "SELECT tranche_key,term_years,issue_amount,amount_scope,allocation_status FROM bond_tranches WHERE project_id=? AND extraction_method!='legacy_reference_seed'",
        (project_id,),
    )
    checks = evaluate_bond_rules(facts, float(project["amount"]), policy=policy,
                                 material_gaps=inventory["gaps"], conflicts=conflicts, tranches=tranches)
    stored_checks = _store_review_rule_results(project_id, checks, facts)
    _sync_rule_risks(project_id, checks)
    sync_confirmed_candidate_risks(project_id)
    candidates = decode_candidates(rows("SELECT * FROM candidate_findings WHERE project_id=?", (project_id,)))
    generate_review_tasks(project_id, inventory["gaps"], conflicts, stored_checks, candidates, tie_outs)

    has_report = bool(rows("SELECT id FROM report_sections WHERE project_id=? LIMIT 1", (project_id,)))
    affected = _affected_report_sections(reasons)
    targets = affected if has_report else None
    generated, _usage = _generate_draft(
        f"review-apply-{uuid.uuid4()}", project_id, project, facts, inventory, LlmUsage(),
        allow_llm=False, section_keys=targets,
    )
    quality = validate_report_support(project_id)
    build_evidence_graph(project_id)
    if quality["status"] != "passed":
        with connect() as conn:
            current = row("SELECT report_stale_reasons FROM projects WHERE id=?", (project_id,)) or {}
            stale_reasons = json_load(current.get("report_stale_reasons", "[]"))
            if "report_validation" not in stale_reasons:
                stale_reasons.append("report_validation")
            conn.execute(
                "UPDATE projects SET report_stale=1,report_stale_reasons=?,updated_at=? WHERE id=?",
                (json_dump(stale_reasons), now_iso(), project_id),
            )
    else:
        mark_report_current(project_id, revision)
    audit_event(
        project_id, "review_apply", "report", str(revision),
        {"reasons": reasons},
        {"affected_section_keys": sorted(affected), "generated_sections": generated, "metric_count": len(metrics), "quality": quality}, "human",
    )
    return {**report_state(project_id), "affected_section_keys": sorted(affected), "applied": quality["status"] == "passed", "quality": quality}


def _run(run_id: str, project_id: str, resume_node: str = "inventory", source_run_id: str = "") -> None:
    started = time.perf_counter()
    total_usage = LlmUsage()
    current_node, current_progress = "inventory", 8
    quality_warning = ""
    resume_index = NODE_KEYS.index(resume_node) if resume_node in NODE_KEYS else 0

    def reusable(node: str) -> bool:
        if not source_run_id or NODE_KEYS.index(node) >= resume_index:
            return False
        artifact = row("SELECT input_hash,status FROM run_artifacts WHERE run_id=? AND node_key=?", (source_run_id, node))
        return bool(artifact and artifact["status"] in {"completed", "degraded"} and artifact["input_hash"] == _node_input_hash(project_id, node))
    try:
        run = row("SELECT policy_profile FROM runs WHERE id=?", (run_id,)) or {}
        policy = load_policy(run.get("policy_profile") or None)
        save_policy_snapshot(project_id, run_id, policy)
        materialize_snapshot_evidence(project_id)

        inventory = material_inventory(project_id)
        if reusable("inventory"):
            _reuse_node(run_id, project_id, "inventory", 8, source_run_id)
        else:
            _node_start(run_id, project_id, "inventory", 8, "校验材料清单")
            _node_finish(run_id, "inventory", f"{len(inventory['documents'])}份材料，{len(inventory['gaps'])}类缺口")
        if _check_cancel(run_id, project_id, "inventory", 8):
            return

        current_node, current_progress = "extract", 20
        if reusable("extract"):
            _reuse_node(run_id, project_id, current_node, current_progress, source_run_id)
        else:
            _node_start(run_id, project_id, current_node, current_progress, "抽取事实断言")
            try:
                fact_count, usage, extraction_stats = _extract_project_facts(run_id, project_id)
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
            assertion_count = row("SELECT COUNT(*) count FROM fact_assertions WHERE project_id=?", (project_id,))["count"]
            extraction_summary = (
                f"形成{assertion_count}条事实断言、{fact_count}项兼容事实；"
                f"全量{extraction_stats['total_sources']}个来源，模型筛选{extraction_stats['selected_sources']}个，"
                f"完成{extraction_stats['completed_batches']}/{extraction_stats['planned_batches']}批"
            )
            if extraction_stats.get("retrieval_unchecked_fields"):
                extraction_summary += (
                    f"；{extraction_stats['retrieval_unchecked_fields']}项必需字段未纳入补充检索预算，"
                    "需人工核验（不据此认定材料未披露）"
                )
            if extraction_stats.get("retrieval_no_hit_fact_keys"):
                extraction_summary += (
                    f"；{len(extraction_stats['retrieval_no_hit_fact_keys'])}项补充检索未命中，"
                    "需人工核验（不据此认定材料未披露）"
                )
            if extraction_stats["budget_truncated"]:
                extraction_summary += "；模型补充抽取达到节点预算，已保留全量确定性结果"
            _node_finish(
                run_id, current_node, extraction_summary, usage,
                status="degraded" if extraction_stats["budget_truncated"] else "completed",
                artifact_details={
                    "extraction_diagnostics": extraction_stats.get("extraction_diagnostics", extraction_stats.get("section_diagnostics", [])),
                    "retrieval_queries": extraction_stats.get("retrieval_queries", 0),
                    "retrieval_unchecked_fields": extraction_stats.get("retrieval_unchecked_fields", 0),
                    "retrieval_unchecked_fact_keys": extraction_stats.get("retrieval_unchecked_fact_keys", []),
                    "retrieval_no_hit_fact_keys": extraction_stats.get("retrieval_no_hit_fact_keys", []),
                },
            )

        current_node, current_progress = "conflict", 30
        if reusable("conflict"):
            _reuse_node(run_id, project_id, current_node, current_progress, source_run_id)
            groups = fact_groups(project_id)
        else:
            _node_start(run_id, project_id, current_node, current_progress, "识别事实冲突")
            entities = sync_project_entities(project_id)
            groups = fact_groups(project_id)
            conflicts = [item for item in groups if item["status"] == "conflicted"]
            _node_finish(run_id, current_node, f"形成{len(entities)}个主体、{len(groups)}组规范事实，{len(conflicts)}组待解决冲突")
        conflicts = [item for item in groups if item["status"] == "conflicted"]
        facts = analysis_fact_rows(project_id)
        if _check_cancel(run_id, project_id, current_node, current_progress):
            return

        project = row("SELECT * FROM projects WHERE id=?", (project_id,))
        current_node, current_progress = "metrics", 40
        if reusable("metrics"):
            _reuse_node(run_id, project_id, current_node, current_progress, source_run_id)
            metrics = rows("SELECT * FROM financial_metrics WHERE project_id=?", (project_id,))
            tie_outs = rows("SELECT * FROM financial_tie_outs WHERE project_id=?", (project_id,))
        else:
            _node_start(run_id, project_id, current_node, current_progress, "计算专业财务指标")
            metrics = compute_and_store_metrics(project_id, float(project["amount"]), policy.version)
            tie_outs = compute_financial_tie_outs(project_id, policy.version)
            _node_finish(run_id, current_node, f"完成{len(metrics)}项确定性财务指标、{len(tie_outs)}项财务勾稽")

        current_node, current_progress = "cross_check", 54
        if reusable("cross_check"):
            _reuse_node(run_id, project_id, current_node, current_progress, source_run_id)
            checks, stored_checks = _stored_rule_results(project_id)
        else:
            _node_start(run_id, project_id, current_node, current_progress, "执行版本化风控规则")
            from .bond_rules import evaluate_bond_rules
            tranches = rows(
                "SELECT tranche_key,term_years,issue_amount,amount_scope,allocation_status FROM bond_tranches WHERE project_id=? AND extraction_method!='legacy_reference_seed'",
                (project_id,),
            )
            checks = evaluate_bond_rules(
                facts, float(project["amount"]), policy=policy, material_gaps=inventory["gaps"],
                conflicts=conflicts, tranches=tranches,
            )
            stored_checks: list[dict] = []
            with connect() as conn:
                conn.execute("DELETE FROM cross_checks WHERE project_id=?", (project_id,))
                for check in checks:
                    check_id = str(uuid.uuid4())
                    check_facts = [item for item in facts if item.get("id") in check.fact_ids]
                    subject_id = check_facts[0].get("subject_id", "") if check_facts else ""
                    scope = check_facts[0].get("scope", "unknown") if check_facts else "unknown"
                    trace = {"formula": check.formula, "fact_ids": check.fact_ids, "values": check.values, "thresholds": check.thresholds}
                    conn.execute(
                        "INSERT INTO cross_checks (id,project_id,rule_key,title,severity,status,summary,values_json,source_ids,formula,fact_ids,policy_version,thresholds,result_value,rationale,subject_id,scope,trace_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (check_id, project_id, check.key, check.title, check.severity, check.status, check.summary,
                         json_dump(check.values), json_dump(check.source_ids), check.formula, json_dump(check.fact_ids),
                         check.policy_version, json_dump(check.thresholds), check.result_value, check.rationale,
                         subject_id, scope, json_dump(trace)),
                    )
                    stored_checks.append({**check.__dict__, "id": check_id, "rule_key": check.key})
            _node_finish(run_id, current_node, f"完成{len(checks)}项规则，{sum(item.status == 'insufficient' for item in checks)}项材料不足")

        current_node, current_progress = "candidate", 66
        if reusable("candidate"):
            _reuse_node(run_id, project_id, current_node, current_progress, source_run_id)
            candidates = decode_candidates(rows("SELECT * FROM candidate_findings WHERE project_id=?", (project_id,)))
        else:
            _node_start(run_id, project_id, current_node, current_progress, "检索证据并生成候选风险")
            evidence = retrieve_candidate_evidence(project_id)
            try:
                if _check_cancel(run_id, project_id, current_node, current_progress):
                    return
                candidates_raw, usage = identify_candidate_findings(
                    evidence, stored_checks, LlmCallContext(project_id, run_id, "candidate", "candidate"),
                )
                _add_usage(total_usage, usage)
                candidates = replace_candidates(project_id, candidates_raw)
                _node_finish(run_id, current_node, f"生成{len(candidates)}项待人工确认候选风险", usage)
            except Exception as exc:
                candidates = decode_candidates(rows("SELECT * FROM candidate_findings WHERE project_id=?", (project_id,)))
                _node_finish(run_id, current_node, "定性风险识别未完成；确定性规则继续执行", status="degraded", error=str(exc)[:500])
        if _check_cancel(run_id, project_id, current_node, current_progress):
            return

        current_node, current_progress = "tasks", 74
        if reusable("tasks"):
            _reuse_node(run_id, project_id, current_node, current_progress, source_run_id)
            tasks = decode_tasks(rows("SELECT * FROM review_tasks WHERE project_id=?", (project_id,)))
        else:
            _node_start(run_id, project_id, current_node, current_progress, "生成尽调复核任务")
            tasks = generate_review_tasks(project_id, inventory["gaps"], conflicts, stored_checks, candidates, tie_outs)
            _node_finish(run_id, current_node, f"生成{len(tasks)}项尽调任务")

        current_node, current_progress = "risk", 82
        if reusable("risk"):
            _reuse_node(run_id, project_id, current_node, current_progress, source_run_id)
        else:
            _node_start(run_id, project_id, current_node, current_progress, "形成确认风险与担保条件")
            generated_risks = risks_from_checks(checks)
            with connect() as conn:
                conn.execute("DELETE FROM risks WHERE project_id=? AND source_type='rule'", (project_id,))
                for risk in generated_risks:
                    conn.execute(
                        "INSERT INTO risks (id,project_id,severity,category,title,description,impact,action,source_ids,likelihood,impact_score,source_type,candidate_id,condition_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), project_id, risk["severity"], risk["category"], risk["title"], risk["description"],
                         risk["impact"], risk["action"], json_dump(risk["source_ids"]), risk["likelihood"], risk["impact_score"],
                         risk["source_type"], risk["candidate_id"], risk["condition_text"]),
                    )
            sync_confirmed_candidate_risks(project_id)
            risk_count = row("SELECT COUNT(*) count FROM risks WHERE project_id=?", (project_id,))["count"]
            _node_finish(run_id, current_node, f"形成{risk_count}项正式风险及缓释条件")

        current_node, current_progress = "draft", 92
        if reusable("draft"):
            _reuse_node(run_id, project_id, current_node, current_progress, source_run_id)
        else:
            _node_start(run_id, project_id, current_node, current_progress, "生成专业审查报告")
            section_count, draft_usage = _generate_draft(run_id, project_id, project, facts, inventory, total_usage)
            candidate_node = row("SELECT status FROM run_nodes WHERE run_id=? AND node_key='candidate'", (run_id,))
            if candidate_node and candidate_node["status"] == "degraded":
                notice = "> 定性风险识别未完成：本报告仅保留确定性规则结果，须人工补充治理、经营连续性、交易真实性、资金用途、关键人物和抵押可执行性核验。\n\n"
                # 前置提示语改变了正文，必须同步重算 content_sha256：否则库里存着一个
                # 与正文对不上的哈希，交付一致性校验会拿到一条失真的证据。
                # validated_content_sha256 不动，让「校验后被改动」如实暴露出来。
                with connect() as conn:
                    for item in conn.execute(
                        "SELECT id,content FROM report_sections WHERE project_id=? "
                        "AND section_key IN ('executive_summary','evidence')",
                        (project_id,),
                    ).fetchall():
                        conn.execute(
                            "UPDATE report_sections SET content=?,content_sha256=? WHERE id=?",
                            (notice + (item["content"] or ""), content_sha256(notice + (item["content"] or "")), item["id"]),
                        )
            if _check_cancel(run_id, project_id, current_node, current_progress):
                return
            _node_finish(run_id, current_node, f"生成{section_count}个专业章节", draft_usage)

        current_node, current_progress = "verify", 97
        _node_start(run_id, project_id, current_node, current_progress, "校验引用、数字和事实支持")
        invalid = row(
            "SELECT COUNT(*) count FROM report_sections r WHERE r.project_id=? AND EXISTS (SELECT 1 FROM json_each(r.citation_ids) j LEFT JOIN citations c ON c.id=j.value WHERE c.id IS NULL)",
            (project_id,),
        )["count"]
        if invalid:
            raise RuntimeError(f"发现{invalid}个章节包含无效引用")
        quality = validate_report_support(project_id)
        build_evidence_graph(project_id)
        if quality["unsupported_claims"] or quality["invalid_citations"]:
            quality_warning = (
                f"报告校验存在{quality['unsupported_claims']}条无证据声明、"
                f"{quality['invalid_citations']}条无效引用，已保留草稿并等待人工复核"
            )
            _node_finish(run_id, current_node, quality_warning, status="warning", error=quality_warning)
        else:
            _node_finish(run_id, current_node, f"引用与数字校验通过，事实声明支持率{quality['support_rate'] * 100:.1f}%")

        current_node, current_progress = "review", 100
        _node_start(run_id, project_id, current_node, current_progress, "等待人工复核")
        _node_finish(run_id, current_node, "专业工作流完成")
        latency = int((time.perf_counter() - started) * 1000)
        measured_usage = row(
            "SELECT COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens,COALESCE(SUM(cost),0) cost "
            "FROM llm_attempts WHERE run_id=?", (run_id,),
        ) or {"input_tokens": total_usage.input_tokens, "output_tokens": total_usage.output_tokens, "cost": total_usage.cost}
        with connect() as conn:
            conn.execute("UPDATE projects SET status='review',analysis_stale=0,updated_at=? WHERE id=?", (now_iso(), project_id))
            conn.execute(
                "UPDATE runs SET status='completed',current_node='review',review_phase='human_review',progress=100,message=?,finished_at=?,latency_ms=?,input_tokens=?,output_tokens=?,cost=? WHERE id=?",
                (f"等待人工复核；{quality_warning}" if quality_warning else "等待人工复核",
                 now_iso(), latency, measured_usage["input_tokens"], measured_usage["output_tokens"], measured_usage["cost"], run_id),
            )
        # A completed run publishes the report only if evidence validation
        # passed.  Any later human decision increments review_revision and
        # explicitly makes this version stale again.
        if quality["status"] == "passed":
            from .review_lifecycle import mark_report_current
            current_revision = (row("SELECT review_revision FROM projects WHERE id=?", (project_id,)) or {}).get("review_revision", 0)
            mark_report_current(project_id, int(current_revision or 0))
        else:
            with connect() as conn:
                conn.execute(
                    "UPDATE projects SET report_stale=1,report_stale_reasons=?,updated_at=? WHERE id=?",
                    (json_dump(["report_validation"]), now_iso(), project_id),
                )
    except Exception as exc:
        _node_finish(run_id, current_node, "执行失败", status="failed", error=str(exc))
        latency = int((time.perf_counter() - started) * 1000)
        measured_usage = row(
            "SELECT COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens,COALESCE(SUM(cost),0) cost "
            "FROM llm_attempts WHERE run_id=?", (run_id,),
        ) or {"input_tokens": 0, "output_tokens": 0, "cost": 0}
        with connect() as conn:
            conn.execute(
                "UPDATE runs SET status='failed',error=?,message='分析失败',finished_at=?,latency_ms=?,input_tokens=?,output_tokens=?,cost=? WHERE id=?",
                (str(exc), now_iso(), latency, measured_usage["input_tokens"], measured_usage["output_tokens"], measured_usage["cost"], run_id),
            )
            conn.execute("UPDATE projects SET status='failed',updated_at=? WHERE id=?", (now_iso(), project_id))


def start_run(project_id: str, policy_profile: str = "", *, resumed_from_run_id: str = "",
              resume_node: str = "inventory") -> str:
    if resume_node not in NODE_KEYS:
        raise ValueError(f"未知节点：{resume_node}")
    run_id = str(uuid.uuid4())
    with connect() as conn:
        project_row = conn.execute("SELECT project_type,archived_at FROM projects WHERE id=?", (project_id,)).fetchone()
        if project_row and project_row["archived_at"]:
            raise ValueError("项目已归档，请先恢复项目后再启动尽调")
        if not policy_profile and project_row and project_row["project_type"] == "bond_guarantee":
            policy_profile = "bond-v1"
        active = conn.execute("SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running')", (project_id,)).fetchone()
        if active:
            raise ValueError(f"项目已有活动任务：{active['id']}")
        conn.execute(
            "INSERT INTO runs (id,project_id,status,current_node,progress,message,cancel_requested,started_at,finished_at,latency_ms,input_tokens,output_tokens,cost,error,policy_profile,resumed_from_run_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, project_id, "queued", "queued", 0, "任务已进入队列", 0, now_iso(), None, 0, 0, 0, 0.0, "", policy_profile, resumed_from_run_id),
        )
        conn.execute("UPDATE projects SET status='running',updated_at=? WHERE id=?", (now_iso(), project_id))
    threading.Thread(target=_run, args=(run_id, project_id, resume_node, resumed_from_run_id), daemon=True).start()
    return run_id


def retry_run(run_id: str, node_key: str | None = None) -> str:
    previous = row("SELECT project_id,status,policy_profile FROM runs WHERE id=?", (run_id,))
    if not previous:
        raise ValueError("任务不存在")
    selected_node = node_key or (row("SELECT current_node FROM runs WHERE id=?", (run_id,)) or {}).get("current_node") or "inventory"
    if selected_node not in NODE_KEYS:
        raise ValueError("重试节点不存在")
    node = row("SELECT status FROM run_nodes WHERE run_id=? AND node_key=?", (run_id, selected_node))
    allowed_run = previous["status"] in {"failed", "cancelled", "model_required"}
    allowed_node = bool(node and node["status"] in {"failed", "degraded", "cancelled", "model_required"})
    requested_index = NODE_KEYS.index(selected_node)
    changed_boundary = ""
    for boundary in ("extract", "metrics", "risk", "verify"):
        if NODE_KEYS.index(boundary) > requested_index:
            break
        artifact = row("SELECT input_hash FROM run_artifacts WHERE run_id=? AND node_key=?", (run_id, boundary))
        if not artifact or artifact["input_hash"] != _node_input_hash(previous["project_id"], boundary):
            selected_node = boundary
            changed_boundary = boundary
            break
    if not allowed_run and not allowed_node and not changed_boundary:
        raise ValueError("仅失败、降级、取消、等待模型配置或输入已变化的节点可以重试")
    return start_run(previous["project_id"], previous.get("policy_profile", ""),
                     resumed_from_run_id=run_id, resume_node=selected_node)
