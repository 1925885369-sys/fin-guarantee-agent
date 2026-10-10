from __future__ import annotations

from collections import defaultdict
from datetime import date
import re
from typing import Any


RECORD_TYPES = (
    "shareholders",
    "financial_periods",
    "debt_instruments",
    "credit_facilities",
    "concentrations",
    "related_transactions",
    "legal_events",
    "material_packages",
)

MODULES: dict[str, dict[str, Any]] = {
    "identity": {"label": "工商身份", "weight": 0.10, "fact_keys": {"legal_representative", "registered_address", "registered_capital", "establishment_date"}},
    "ownership": {"label": "股权与控制", "weight": 0.15, "fact_keys": {"controller", "controller_share"}, "record_types": {"shareholders"}},
    "financial": {"label": "财务数据", "weight": 0.30, "fact_keys": {"total_assets", "total_liabilities", "equity", "revenue", "net_profit", "operating_cash_flow"}, "record_types": {"financial_periods"}},
    "debt": {"label": "债务与或有事项", "weight": 0.20, "fact_keys": {"interest_bearing_debt", "debt_due_12m", "external_guarantee", "contingent_liabilities"}, "record_types": {"debt_instruments", "credit_facilities"}},
    "operations": {"label": "主营经营", "weight": 0.10, "fact_keys": {"main_business"}},
    "concentration": {"label": "客户供应商集中度", "weight": 0.05, "fact_keys": {"top_customer_share", "top_supplier_share"}, "record_types": {"concentrations"}},
    "related_parties": {"label": "关联方与关联交易", "weight": 0.05, "fact_keys": {"related_parties", "related_unsupported_amount"}, "record_types": {"related_transactions"}},
    "legal": {"label": "法律与合规", "weight": 0.05, "fact_keys": {"legal_opinion_status", "contingent_liabilities"}, "record_types": {"legal_events"}},
}

HARD_VETO_LABELS = {
    "legal_identity": "法律身份",
    "control": "股权与控制",
    "financial_history": "财务历史",
    "debt_maturity": "债务到期结构",
    "legal_screening": "法律与合规筛查",
}

USABLE_STATUSES = {"confirmed", "single_source"}
BLOCKED_STATUSES = {"stale", "conflicted", "insufficient"}
ANNUAL_PERIOD_RE = re.compile(r"^(?:19|20)\d{2}(?:年|年度)?$")


def annual_period(value: Any) -> str | None:
    """Return the four-digit year only for a complete fiscal-year label."""
    normalized = str(value or "").strip()
    if not ANNUAL_PERIOD_RE.fullmatch(normalized):
        return None
    return normalized[:4]


def effective_status(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "single_source")
    valid_until = item.get("valid_until")
    if valid_until and str(valid_until) < date.today().isoformat() and status not in {"conflicted", "insufficient"}:
        return "stale"
    return status


def _usable(items: list[dict[str, Any]], reliable_source_ids: set[str]) -> list[dict[str, Any]]:
    return [
        item for item in items
        if effective_status(item) in USABLE_STATUSES
        and reliable_source_ids.intersection(item.get("source_ids") or [])
    ]


def calculate_readiness(profile: dict[str, Any]) -> dict[str, Any]:
    facts = profile.get("facts", [])
    records = [item for record_type in RECORD_TYPES for item in profile.get(record_type, [])]
    reliable_source_ids = {
        str(item.get("id")) for item in profile.get("sources", [])
        if item.get("id") and str(item.get("locator") or "").strip() and str(item.get("quote") or "").strip()
    }
    usable_facts = _usable(facts, reliable_source_ids)
    usable_records = _usable(records, reliable_source_ids)
    facts_by_key = defaultdict(list)
    records_by_type = defaultdict(list)
    for item in usable_facts:
        facts_by_key[item.get("fact_key", "")].append(item)
    for item in usable_records:
        records_by_type[item.get("record_type", "")].append(item)

    module_scores = []
    completeness = 0.0
    for key, config in MODULES.items():
        required_fact_keys = set(config.get("fact_keys", set()))
        present_fact_keys = required_fact_keys & set(facts_by_key)
        record_types = set(config.get("record_types", set()))
        record_credit = 1 if record_types & set(records_by_type) else 0
        denominator = len(required_fact_keys) + (1 if record_types else 0)
        score = (len(present_fact_keys) + record_credit) / denominator if denominator else 1.0
        weighted = score * float(config["weight"])
        completeness += weighted
        missing = sorted(required_fact_keys - present_fact_keys)
        if record_types and not record_credit:
            missing.extend(sorted(record_types))
        module_status = "ready" if not missing else "blocking" if score == 0 else "warning"
        module_scores.append({
            "key": key, "label": config["label"], "weight": config["weight"],
            "score": round(score, 4), "weighted_score": round(weighted, 4), "missing": missing,
            "status": module_status,
        })

    periods_by_key = {
        key: {year for item in items if (year := annual_period(item.get("period"))) is not None}
        for key, items in facts_by_key.items()
    }
    core_financial = {
        "total_assets", "total_liabilities", "equity",
        "revenue", "net_profit", "operating_cash_flow",
    }
    periods_by_scope: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for key in core_financial:
        for item in facts_by_key.get(key, []):
            year = annual_period(item.get("period"))
            if year:
                periods_by_scope[str(item.get("scope") or "unknown")][key].add(year)
    complete_financial_scopes = {
        scope: set.intersection(*(keys.get(key, set()) for key in core_financial))
        for scope, keys in periods_by_scope.items()
        if core_financial <= set(keys)
    }
    hard_vetoes: list[dict[str, str]] = []
    if not profile.get("name") or not profile.get("unified_code") or not {"legal_representative", "establishment_date"} <= set(facts_by_key):
        hard_vetoes.append({"key": "legal_identity", "message": "法律身份资料不完整或缺少有效来源"})
    if "controller" not in facts_by_key and not records_by_type.get("shareholders"):
        hard_vetoes.append({"key": "control", "message": "实际控制关系尚未形成可引用结论"})
    if not any(len(periods) >= 3 for periods in complete_financial_scopes.values()):
        hard_vetoes.append({"key": "financial_history", "message": "缺少同一口径的最近三个完整年度核心财务数据"})
    if not ({"interest_bearing_debt", "debt_due_12m"} <= set(facts_by_key) or records_by_type.get("debt_instruments")):
        hard_vetoes.append({"key": "debt_maturity", "message": "缺少当前债务及到期结构"})
    if not records_by_type.get("legal_events"):
        hard_vetoes.append({"key": "legal_screening", "message": "缺少重大诉讼和合规筛查记录"})

    blocked = [item for item in facts + records if effective_status(item) in BLOCKED_STATUSES]
    hard_veto_keys = {item["key"] for item in hard_vetoes}
    for item in hard_vetoes:
        item["label"] = HARD_VETO_LABELS.get(item["key"], item["key"])
        item["reason"] = item["message"]
    requirements = []
    for item in module_scores:
        if not item["missing"]:
            continue
        blocking = item["key"] in hard_veto_keys or item["status"] == "blocking"
        requirements.append({
            "key": item["key"],
            "label": item["label"],
            "category": item["key"],
            "status": "blocking" if blocking else "warning",
            "blocking_level": "blocking" if blocking else "non_blocking",
            "required_fact_keys": sorted(MODULES[item["key"]].get("fact_keys", set())),
            "missing_fact_keys": list(item["missing"]),
            "stale_fact_keys": [],
            "conflicted_fact_keys": [],
            "source_ids": [],
            "impact": "阻止启动 Agent" if blocking else "降低主体档案完整度",
            # Compatibility for clients using the original compact shape.
            "missing": list(item["missing"]),
        })
    notices = []
    single_source = sum(effective_status(item) == "single_source" for item in facts + records)
    if single_source:
        notices.append(f"{single_source}项事实仅有单一来源，尚未独立验证")
    if blocked:
        notices.append(f"{len(blocked)}项记录已过期、冲突或材料不足")
    ready_for_agent = not hard_vetoes
    return {
        "profile_id": profile["id"],
        "ready_for_project": True,
        "ready_for_agent": ready_for_agent,
        "complete_due_diligence_ready": ready_for_agent and completeness >= 0.8,
        "completeness": round(completeness, 4),
        "module_scores": module_scores,
        "hard_vetoes": hard_vetoes,
        "requirements": requirements,
        "notices": notices,
        "counts": profile.get("counts", {}),
    }
