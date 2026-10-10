from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import defaultdict
from typing import Any

from .database import connect, json_dump, json_load, now_iso, row, rows
from .facts import FACT_REGISTRY, canonical_key, text_value_token
from .p0 import borrower_entity, default_scope, ensure_entity, period_type


def audit_event(
    project_id: str, action: str, entity_type: str, entity_id: str,
    before: Any = None, after: Any = None, actor: str = "system",
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_events (id,project_id,actor,action,entity_type,entity_id,before_json,after_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), project_id, actor, action, entity_type, entity_id,
             json_dump(before or {}), json_dump(after or {}), now_iso()),
        )


def _assertion_identity(item: dict) -> tuple:
    value = item.get("value")
    normalized_value = round(float(value), 6) if value is not None else None
    text = re.sub(r"\s+", "", str(item.get("text_value") or "")) if value is None else ""
    return normalized_value, item.get("unit", ""), text


def _assertion_basis_hash(items: list[dict]) -> str:
    basis = [
        {
            "id": item["id"],
            "identity": _assertion_identity(item),
            "status": item.get("assertion_status", "unreviewed"),
            "origin": item.get("origin_type", "document"),
            "sources": sorted(json_load(item.get("source_ids", "[]"))),
        }
        for item in sorted(items, key=lambda value: value["id"])
    ]
    return hashlib.sha256(
        json.dumps(basis, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def sync_fact_assertions(project_id: str, extracted: list[dict] | None = None) -> None:
    from .profile_evidence import profile_assertion_rows

    items = list(extracted if extracted is not None else rows("SELECT * FROM facts WHERE project_id=?", (project_id,)))
    items.extend(profile_assertion_rows(project_id))
    borrower = borrower_entity(project_id)
    subject_entities = {borrower["name"]: borrower["id"]}
    for item in items:
        subject = item.get("subject") or borrower["name"]
        if item.get("subject_role") == "issuer":
            subject = borrower["name"]
            item["subject"] = subject
        if subject not in subject_entities:
            subject_entities[subject] = ensure_entity(project_id, subject, "enterprise")["id"]
    with connect() as conn:
        if extracted is not None:
            conn.execute(
                "DELETE FROM fact_assertions WHERE project_id=? AND origin_type IN ('document','legacy_reference_seed') AND extraction_method!='human'",
                (project_id,),
            )
        conn.execute("DELETE FROM fact_assertions WHERE project_id=? AND origin_type='profile_snapshot'", (project_id,))
        for item in items:
            fact_key = item.get("fact_key") or canonical_key(item.get("name", ""))
            if fact_key not in FACT_REGISTRY:
                continue
            definition = FACT_REGISTRY[fact_key]
            source_ids = item.get("source_ids", [])
            if isinstance(source_ids, str):
                source_ids = json_load(source_ids)
            assertion_id = item.get("id") or str(uuid.uuid4())
            method = "human" if item.get("review_status") == "human_verified" else item.get("extraction_method", "legacy")
            subject = item.get("subject") or borrower["name"]
            subject_id = item.get("subject_id") or subject_entities[subject]
            scope = item.get("scope") if item.get("scope") not in {None, "", "unknown"} else default_scope(definition.category)
            item_period_type = item.get("period_type") if item.get("period_type") not in {None, "", "unknown"} else period_type(fact_key, item.get("period", ""))
            origin_type = item.get("origin_type") or "document"
            assertion_status = item.get("assertion_status") or (
                "confirmed" if method == "human" else item.get("review_status", "unreviewed")
            )
            conn.execute(
                "INSERT OR IGNORE INTO fact_assertions (id,project_id,fact_key,subject,subject_id,scope,period,period_type,category,name,value,unit,text_value,raw_value,source_ids,confidence,extraction_method,created_at,origin_type,origin_ref,profile_snapshot_id,assertion_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (assertion_id, project_id, fact_key, subject, subject_id, scope, item.get("period", ""), item_period_type,
                 definition.category, definition.label, item.get("value"), item.get("unit", ""), item.get("text_value", ""),
                 item.get("raw_value", ""), json_dump(source_ids), item.get("confidence", 0.8), method, now_iso(),
                 origin_type, item.get("origin_ref", ""), item.get("profile_snapshot_id", ""), assertion_status),
            )
    rebuild_canonical_facts(project_id)


def rebuild_canonical_facts(project_id: str) -> None:
    assertions = rows(
        "SELECT * FROM fact_assertions WHERE project_id=? ORDER BY fact_key,period,confidence DESC,created_at",
        (project_id,),
    )
    existing = {
        (item["fact_key"], item["subject_id"], item["scope"], item["period"]): item
        for item in rows("SELECT * FROM canonical_facts WHERE project_id=?", (project_id,))
    }
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for assertion in assertions:
        grouped[(assertion["fact_key"], assertion["subject_id"], assertion["scope"], assertion["period"])].append(assertion)
    with connect() as conn:
        stale_ids = [item["id"] for key, item in existing.items() if key not in grouped]
        if stale_ids:
            conn.executemany("DELETE FROM canonical_facts WHERE id=?", ((item_id,) for item_id in stale_ids))
        for key, candidates in grouped.items():
            previous = existing.get(key)
            assertion_ids = [item["id"] for item in candidates]
            basis_hash = _assertion_basis_hash(candidates)
            selected = None
            status = "unreviewed"
            resolution_note = ""
            resolved_by = "system"
            active = [
                item for item in candidates
                if item.get("assertion_status", "unreviewed") not in {"stale", "conflicted", "insufficient"}
            ]
            active_ids = {item["id"] for item in active}
            has_declared_conflict = any(item.get("assertion_status") == "conflicted" for item in candidates)
            resolution_invalidated = bool(
                previous and previous.get("resolved_by") == "human"
                and previous.get("resolution_basis_hash") != basis_hash
            )
            if (
                previous and previous.get("resolved_by") == "human"
                and previous.get("selected_assertion_id") in active_ids
                and previous.get("resolution_basis_hash") == basis_hash
            ):
                selected = next(item for item in candidates if item["id"] == previous["selected_assertion_id"])
                status = "confirmed"
                resolution_note = previous.get("resolution_note", "")
                resolved_by = "human"
            else:
                if not active:
                    status = "insufficient"
                else:
                    # Group assertions by a *semantic* signature. Text facts
                    # compare on a canonical token (so "AAA" == "主体信用等级为AAA"),
                    # numeric facts compare with a small relative tolerance, and
                    # stored values that are not plausible for their field
                    # (extraction garbage such as sentence fragments) are excluded
                    # and can never force or enlarge a conflict.
                    groups = _semantic_conflict_groups(key[0], active)
                    if has_declared_conflict or len(groups) > 1:
                        status = "conflicted"
                    elif len(groups) == 1:
                        members = groups[0][1]
                        selected = max(members, key=lambda item: item["confidence"])
                        if any(item.get("assertion_status") == "confirmed" for item in members):
                            status = "confirmed"
                        elif selected["extraction_method"] == "human":
                            status, resolved_by = "confirmed", "human"
                    else:
                        # Every stored value was rejected as extraction garbage;
                        # keep the group unreviewed instead of inventing a conflict.
                        status = "unreviewed"
            if resolution_invalidated:
                conn.execute(
                    "INSERT INTO audit_events (id,project_id,actor,action,entity_type,entity_id,before_json,after_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()), project_id, "system", "resolution_invalidated", "canonical_fact",
                        previous["id"], json_dump(previous),
                        json_dump({"resolution_basis_hash": basis_hash, "status": status}), now_iso(),
                    ),
                )
            definition = FACT_REGISTRY[key[0]]
            canonical_id = previous["id"] if previous else str(uuid.uuid4())
            values = (
                canonical_id, project_id, key[0], candidates[0]["subject"], key[1], key[2], key[3], candidates[0]["period_type"], definition.category, definition.label,
                status, selected["id"] if selected else None, selected.get("value") if selected else None,
                selected.get("unit", "") if selected else "", selected.get("text_value", "") if selected else "",
                json_dump(assertion_ids), resolution_note, resolved_by, basis_hash, now_iso(),
            )
            conn.execute(
                "INSERT INTO canonical_facts (id,project_id,fact_key,subject,subject_id,scope,period,period_type,category,name,status,selected_assertion_id,value,unit,text_value,assertion_ids,resolution_note,resolved_by,resolution_basis_hash,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(project_id,fact_key,subject_id,scope,period) DO UPDATE SET subject=excluded.subject,period_type=excluded.period_type,category=excluded.category,name=excluded.name,status=excluded.status,selected_assertion_id=excluded.selected_assertion_id,value=excluded.value,unit=excluded.unit,text_value=excluded.text_value,assertion_ids=excluded.assertion_ids,resolution_note=excluded.resolution_note,resolved_by=excluded.resolved_by,resolution_basis_hash=excluded.resolution_basis_hash,updated_at=excluded.updated_at",
                values,
            )


def fact_groups(project_id: str) -> list[dict]:
    assertions = rows("SELECT * FROM fact_assertions WHERE project_id=? ORDER BY confidence DESC", (project_id,))
    by_id = {item["id"]: {**item, "source_ids": json_load(item["source_ids"])} for item in assertions}
    output = []
    for canonical in rows("SELECT * FROM canonical_facts WHERE project_id=? ORDER BY category,name,period", (project_id,)):
        ids = json_load(canonical["assertion_ids"])
        canonical["assertion_ids"] = ids
        canonical["assertions"] = [by_id[item_id] for item_id in ids if item_id in by_id]
        output.append(canonical)
    return output


def canonical_fact_rows(project_id: str, subject_id: str | None = None, scope: str | None = None) -> list[dict]:
    output = []
    query = "SELECT * FROM canonical_facts WHERE project_id=? AND selected_assertion_id IS NOT NULL"
    params: list[str] = [project_id]
    if subject_id:
        query += " AND subject_id=?"
        params.append(subject_id)
    if scope:
        query += " AND scope=?"
        params.append(scope)
    for item in rows(query, params):
        assertion = row(
            "SELECT source_ids,confidence,extraction_method,raw_value,origin_type,origin_ref,profile_snapshot_id,assertion_status "
            "FROM fact_assertions WHERE id=?",
            (item["selected_assertion_id"],),
        ) or {}
        output.append({
            "id": item["id"], "project_id": project_id, "fact_key": item["fact_key"], "category": item["category"],
            "name": item["name"], "subject": item["subject"], "subject_id": item["subject_id"], "scope": item["scope"],
            "period": item["period"], "period_type": item["period_type"], "value": item["value"], "unit": item["unit"],
            "text_value": item["text_value"], "source_ids": json_load(assertion.get("source_ids", "[]")),
            "confidence": assertion.get("confidence", 1.0), "extraction_method": assertion.get("extraction_method", "canonical"),
            "review_status": "human_verified" if item["status"] == "confirmed" else item["status"],
            "raw_value": assertion.get("raw_value", ""),
            "origin_type": assertion.get("origin_type", "document"),
            "origin_ref": assertion.get("origin_ref", ""),
            "profile_snapshot_id": assertion.get("profile_snapshot_id", ""),
            "assertion_status": assertion.get("assertion_status", "unreviewed"),
        })
    return output


def analysis_fact_rows(project_id: str) -> list[dict]:
    borrower = borrower_entity(project_id)
    facts = canonical_fact_rows(project_id, borrower["id"])
    scoped_categories = {"财务", "债务", "税务", "流水"}
    financial_scopes = {
        item["scope"] for item in facts
        if item.get("category") in scoped_categories and item["scope"] in {"standalone", "consolidated"}
    }
    preferred = "consolidated" if "consolidated" in financial_scopes else "standalone" if "standalone" in financial_scopes else "unknown"
    return [
        item for item in facts
        if item.get("category") not in scoped_categories or item["scope"] in {preferred, "entity", "transaction"}
    ]


def resolve_fact_group(
    project_id: str, canonical_id: str, assertion_id: str | None, manual: dict | None, note: str,
) -> dict:
    canonical = row("SELECT * FROM canonical_facts WHERE id=? AND project_id=?", (canonical_id, project_id))
    if not canonical:
        raise ValueError("规范事实不存在")
    before = dict(canonical)
    selected = None
    if assertion_id:
        selected = row("SELECT * FROM fact_assertions WHERE id=? AND project_id=?", (assertion_id, project_id))
        if not selected or assertion_id not in json_load(canonical["assertion_ids"]):
            raise ValueError("断言不属于该事实组")
    elif manual:
        selected = {
            "id": str(uuid.uuid4()), "value": manual.get("value"), "unit": manual.get("unit", ""),
            "text_value": manual.get("text_value", ""), "source_ids": manual.get("source_ids", []),
            "raw_value": manual.get("raw_value", "人工录入"),
        }
        with connect() as conn:
            conn.execute(
                "INSERT INTO fact_assertions (id,project_id,fact_key,subject,subject_id,scope,period,period_type,category,name,value,unit,text_value,raw_value,source_ids,confidence,extraction_method,created_at,origin_type,assertion_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (selected["id"], project_id, canonical["fact_key"], canonical["subject"], canonical["subject_id"], canonical["scope"], manual.get("period", canonical["period"]), canonical["period_type"],
                 canonical["category"], canonical["name"], selected["value"], selected["unit"], selected["text_value"],
                 selected["raw_value"], json_dump(selected["source_ids"]), 1.0, "human", now_iso(), "document", "confirmed"),
            )
        ids = json_load(canonical["assertion_ids"]) + [selected["id"]]
        canonical["assertion_ids"] = json_dump(ids)
    else:
        raise ValueError("必须选择已有断言或提交人工值")
    assertion_ids = json_load(canonical["assertion_ids"])
    placeholders = ",".join("?" for _ in assertion_ids)
    current_assertions = rows(
        f"SELECT * FROM fact_assertions WHERE project_id=? AND id IN ({placeholders})",
        (project_id, *assertion_ids),
    )
    basis_hash = _assertion_basis_hash(current_assertions)
    with connect() as conn:
        conn.execute(
            "UPDATE canonical_facts SET status='confirmed',selected_assertion_id=?,value=?,unit=?,text_value=?,assertion_ids=?,resolution_note=?,resolved_by='human',resolution_basis_hash=?,updated_at=? WHERE id=?",
            (selected["id"], selected.get("value"), selected.get("unit", ""), selected.get("text_value", ""),
             canonical["assertion_ids"], note, basis_hash, now_iso(), canonical_id),
        )
    after = row("SELECT * FROM canonical_facts WHERE id=?", (canonical_id,)) or {}
    audit_event(project_id, "resolve", "canonical_fact", canonical_id, before, after, "human")
    # The canonical value is authoritative immediately, while dependent
    # metrics, rules and prose are refreshed together through the explicit
    # review-apply action.
    from .review_lifecycle import mark_review_changed
    mark_review_changed(project_id, f"fact:{after.get('fact_key', '')}")
    return after


def risk_severity(likelihood: int, impact: int) -> str:
    score = likelihood * impact
    return "high" if score >= 6 else "medium" if score >= 3 else "low"


def numeric_close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=0.001, abs_tol=0.01)


def _assertion_signature(fact_key: str, item: dict) -> tuple | None:
    """Semantic signature used when deciding whether assertions conflict.

    Numeric assertions compare on (unit, value); text assertions compare on a
    canonical token produced by ``text_value_token``.  None means the stored
    text is not a plausible value for ``fact_key`` and must not drive a conflict.
    """
    if item.get("value") is not None:
        return ("num", item.get("unit", ""), float(item["value"]))
    token = text_value_token(fact_key, item.get("text_value"))
    return None if token is None else ("txt", token)


def _signatures_close(left: tuple, right: tuple) -> bool:
    if left[0] != right[0]:
        return False
    if left[0] == "num":
        return left[1] == right[1] and numeric_close(left[2], right[2])
    return left[1] == right[1]


def _semantic_conflict_groups(fact_key: str, items: list[dict]) -> list[tuple[tuple, list]]:
    """Cluster assertions that carry the same canonical value.

    Returns [(signature, [assertions…])] over the assertions whose signature is
    not None (i.e. genuine values); garbage text is dropped before clustering.
    """
    groups: list[tuple[tuple, list]] = []
    for item in items:
        signature = _assertion_signature(fact_key, item)
        if signature is None:
            continue
        for index, (candidate, _members) in enumerate(groups):
            if _signatures_close(candidate, signature):
                groups[index][1].append(item)
                break
        else:
            groups.append((signature, [item]))
    return groups
