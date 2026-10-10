from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from typing import Any

from .database import connect, json_dump, json_load, now_iso, row, rows
from .profile_catalog import RECORD_TYPES, calculate_readiness, effective_status


PROFILE_STATUSES = {"ready", "stale", "conflicted", "incomplete"}
FACT_STATUSES = {"confirmed", "single_source", "stale", "conflicted", "insufficient"}
ENTITY_ROLES = {"issuer", "guarantor", "counter_guarantor"}


def _profile_counts(profile_id: str, version: int) -> dict[str, int]:
    counts = {key: 0 for key in FACT_STATUSES}
    for item in rows(
        "SELECT status,valid_until FROM entity_profile_facts WHERE profile_id=? AND version=?",
        (profile_id, version),
    ):
        counts[effective_status(item)] += 1
    for item in rows(
        "SELECT status,valid_until FROM entity_profile_records WHERE profile_id=? AND version=?",
        (profile_id, version),
    ):
        counts[effective_status(item)] += 1
    return counts


def profile_summary(item: dict[str, Any]) -> dict[str, Any]:
    version = int(item.get("current_version") or 1)
    counts = _profile_counts(item["id"], version)
    summary_sources = rows(
        "SELECT id,locator,quote FROM entity_profile_sources WHERE profile_id=? AND version=?",
        (item["id"], version),
    )
    summary_facts = rows(
        "SELECT fact_key,period,as_of_date,scope,status,valid_until,source_ids FROM entity_profile_facts "
        "WHERE profile_id=? AND version=?",
        (item["id"], version),
    )
    for fact in summary_facts:
        fact["source_ids"] = json_load(fact["source_ids"])
    summary_records = rows(
        "SELECT record_type,period,as_of_date,scope,status,valid_until,source_ids FROM entity_profile_records "
        "WHERE profile_id=? AND version=?",
        (item["id"], version),
    )
    payload: dict[str, Any] = {
        "name": item["name"], "unified_code": item["unified_code"], "facts": summary_facts,
        "sources": summary_sources, **{record_type: [] for record_type in RECORD_TYPES},
    }
    for record in summary_records:
        record["source_ids"] = json_load(record["source_ids"])
        payload.setdefault(record["record_type"], []).append(record)
    derived_completeness, effective_status = _derived_profile_metadata(item["id"], payload)
    return {
        "id": item["id"],
        "name": item["name"],
        "unified_code": item["unified_code"],
        "baseline_date": item.get("baseline_date"),
        "latest_financial_period": item.get("latest_financial_period"),
        "completeness": derived_completeness,
        "status": effective_status,
        "version": version,
        "project_count": row(
            "SELECT COUNT(DISTINCT project_id) count FROM project_profile_roles WHERE profile_id=?",
            (item["id"],),
        )["count"],
        "counts": counts,
    }


def list_profiles(search: str = "", status: str = "") -> list[dict[str, Any]]:
    items = [profile_summary(item) for item in rows("SELECT * FROM entity_profiles ORDER BY name,id")]
    normalized_search = search.strip().casefold()
    if normalized_search:
        items = [
            item for item in items
            if normalized_search in f"{item['name']} {item['unified_code']}".casefold()
        ]
    if status:
        items = [item for item in items if item["status"] == status]
    return items


def get_profile(profile_id: str, version: int | None = None) -> dict[str, Any] | None:
    item = row("SELECT * FROM entity_profiles WHERE id=?", (profile_id,))
    if not item:
        return None
    version = int(version or item["current_version"])
    version_row = row(
        "SELECT data_json,profile_json,content_hash,change_note,created_at,baseline_date FROM entity_profile_versions "
        "WHERE profile_id=? AND version=?",
        (profile_id, version),
    )
    if not version_row:
        return None
    result = profile_summary(item)
    result["version"] = version
    result["counts"] = _profile_counts(profile_id, version)
    result["facts"] = rows(
        "SELECT id,profile_id,version,fact_key,category,value,unit,text_value,period,as_of_date,period_type,"
        "scope,subject_role,status,valid_until,source_ids,raw_value,confidence,currency,reviewer,reviewed_at,record_hash "
        "FROM entity_profile_facts WHERE profile_id=? AND version=? ORDER BY category,fact_key,period",
        (profile_id, version),
    )
    source_by_id = {
        source["id"]: source for source in rows(
            "SELECT id,profile_id,version,filename,kind,locator,page,quote,file_id,locator_type,content_hash "
            "FROM entity_profile_sources "
            "WHERE profile_id=? AND version=? ORDER BY filename,page,id",
            (profile_id, version),
        )
    }
    from .facts import FACT_REGISTRY

    for fact in result["facts"]:
        fact["source_ids"] = json_load(fact["source_ids"])
        definition = FACT_REGISTRY.get(fact["fact_key"])
        fact["name"] = definition.label if definition else fact["fact_key"]
        primary_source = next(
            (source_by_id[source_id] for source_id in fact["source_ids"] if source_id in source_by_id),
            None,
        )
        fact["source_id"] = primary_source["id"] if primary_source else None
        fact["source_label"] = primary_source["filename"] if primary_source else None
        fact["page"] = primary_source.get("page") if primary_source else None
        fact["status"] = effective_status(fact)
    result["sources"] = list(source_by_id.values())
    for record_type in RECORD_TYPES:
        records = rows(
            "SELECT id,profile_id,version,record_type,period,as_of_date,scope,status,valid_until,source_ids,"
            "reviewer,reviewed_at,unit,currency,data_json,record_hash FROM entity_profile_records "
            "WHERE profile_id=? AND version=? AND record_type=? ORDER BY period,as_of_date,id",
            (profile_id, version, record_type),
        )
        for record in records:
            record["source_ids"] = json_load(record["source_ids"])
            record["data"] = json_load(record.pop("data_json"))
            record["status"] = effective_status(record)
        result[record_type] = records
    historical_profile = json_load(version_row.get("profile_json") or "{}")
    result["name"] = historical_profile.get("name") or result["name"]
    result["unified_code"] = historical_profile.get("unified_code") or result["unified_code"]
    result["baseline_date"] = version_row.get("baseline_date")
    result["latest_financial_period"] = historical_profile.get("latest_financial_period") or result["latest_financial_period"]
    result["data"] = json_load(version_row["data_json"])
    result["version_created_at"] = version_row["created_at"]
    result["content_hash"] = version_row.get("content_hash", "")
    result["change_note"] = version_row.get("change_note", "")
    result["completeness"], result["status"] = _derived_profile_metadata(profile_id, result)
    return result


def list_profile_versions(profile_id: str) -> list[dict[str, Any]]:
    profile = row("SELECT * FROM entity_profiles WHERE id=?", (profile_id,))
    if not profile:
        raise LookupError("主体档案不存在")
    versions = rows(
        "SELECT version,baseline_date,content_hash,change_note,created_at,profile_json FROM entity_profile_versions "
        "WHERE profile_id=? ORDER BY version DESC",
        (profile_id,),
    )
    output = []
    for version in versions:
        detail = get_profile(profile_id, int(version["version"]))
        historical = json_load(version.pop("profile_json") or "{}")
        output.append({
            **version,
            "latest_financial_period": historical.get("latest_financial_period"),
            "status": (detail or {}).get("status", "incomplete"),
            "completeness": float((detail or {}).get("completeness") or 0),
            "created_by": "system",
            "change_summary": version.get("change_note", ""),
            "source_count": len((detail or {}).get("sources", [])),
            "fact_count": len((detail or {}).get("facts", [])),
            "current": int(version["version"]) == int(profile.get("current_version") or 0),
        })
    return output


def profile_version_diff(profile_id: str, version: int, compare_to: int | None = None) -> dict[str, Any]:
    current = get_profile(profile_id, version)
    if not current:
        raise LookupError("主体档案版本不存在")
    compare_version = compare_to if compare_to is not None else version - 1
    previous = get_profile(profile_id, compare_version) if compare_version > 0 else None

    def keyed(detail: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        if not detail:
            return {}
        output: dict[str, dict[str, Any]] = {}
        for fact in detail.get("facts", []):
            key = f"fact:{fact.get('fact_key')}:{fact.get('period')}:{fact.get('scope')}"
            output[key] = fact
        for record_type in RECORD_TYPES:
            for record in detail.get(record_type, []):
                data = record.get("data") or {}
                identity = next(
                    (str(data.get(field)) for field in ("name", "counterparty", "instrument_name", "facility_name", "event_type") if data.get(field)),
                    "record",
                )
                key = f"{record_type}:{record.get('period')}:{record.get('as_of_date')}:{record.get('scope')}:{identity}"
                output[key] = record
        return output

    before, after = keyed(previous), keyed(current)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        key for key in set(before) & set(after)
        if before[key].get("record_hash") != after[key].get("record_hash")
    )
    return {
        "profile_id": profile_id, "version": version, "compare_to": compare_version if previous else None,
        "added": [{"key": key, "record": after[key]} for key in added],
        "removed": [{"key": key, "record": before[key]} for key in removed],
        "changed": [{"key": key, "before": before[key], "after": after[key]} for key in changed],
    }


def _payload_hash(payload: dict[str, Any]) -> str:
    content = {
        key: payload.get(key)
        for key in ("name", "unified_code", "baseline_date", "latest_financial_period", "data", "facts", "sources", *RECORD_TYPES)
    }
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _validate_payload(payload: dict[str, Any]) -> None:
    from .facts import FACT_REGISTRY

    source_ids = [str(item.get("id")) for item in payload.get("sources") or [] if item.get("id")]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("主体档案来源 ID 不得重复")
    known_sources = set(source_ids)
    unknown_keys = sorted({item["fact_key"] for item in payload.get("facts") or [] if item["fact_key"] not in FACT_REGISTRY})
    if unknown_keys:
        raise ValueError(f"主体事实键未注册：{','.join(unknown_keys)}")
    for item in [*(payload.get("facts") or []), *(
        record for record_type in RECORD_TYPES for record in (payload.get(record_type) or [])
    )]:
        references = set(item.get("source_ids") or [])
        missing = sorted(references - known_sources)
        if missing:
            raise ValueError(f"主体记录引用了不存在的来源：{','.join(missing)}")


def _derived_profile_metadata(profile_id: str, payload: dict[str, Any]) -> tuple[float, str]:
    preview = {
        "id": profile_id, "name": payload.get("name", ""), "unified_code": payload.get("unified_code", ""),
        "facts": payload.get("facts") or [], "sources": payload.get("sources") or [], "counts": {},
        **{record_type: payload.get(record_type) or [] for record_type in RECORD_TYPES},
    }
    readiness = calculate_readiness(preview)
    statuses = [effective_status(item) for item in preview["facts"]]
    statuses.extend(
        effective_status(item) for record_type in RECORD_TYPES for item in preview[record_type]
    )
    if "conflicted" in statuses:
        status = "conflicted"
    elif "stale" in statuses:
        status = "stale"
    elif readiness["ready_for_agent"] and readiness["completeness"] >= 0.8:
        status = "ready"
    else:
        status = "incomplete"
    return float(readiness["completeness"]), status


def _insert_version(conn, profile_id: str, version: int, payload: dict[str, Any]) -> None:
    now = now_iso()
    content_hash = _payload_hash(payload)
    conn.execute(
        "INSERT INTO entity_profile_versions (id,profile_id,version,baseline_date,data_json,content_hash,profile_json,change_note,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), profile_id, version, payload.get("baseline_date"), json_dump(payload.get("data") or {}),
         content_hash, json_dump({"name": payload.get("name", ""), "unified_code": payload.get("unified_code", ""),
                                  "latest_financial_period": payload.get("latest_financial_period")}),
         payload.get("change_note", ""), now),
    )
    source_ids: set[str] = set()
    source_id_map: dict[str, str] = {}
    file_id_map: dict[str, str] = {}
    for source in payload.get("sources") or []:
        supplied_id = source.get("id")
        source_id = (
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"entity-profile-source:{profile_id}:{version}:{supplied_id}"))
            if supplied_id else str(uuid.uuid4())
        )
        source_ids.add(source_id)
        if supplied_id:
            source_id_map[supplied_id] = source_id
        filename = source["filename"]
        file_id = file_id_map.get(filename)
        if not file_id:
            file_id = str(uuid.uuid4())
            file_id_map[filename] = file_id
            file_hash = source.get("content_hash") or hashlib.sha256(filename.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO entity_profile_files (id,profile_id,version,filename,kind,media_type,sha256,size,parse_status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (file_id, profile_id, version, filename, source.get("kind", "other"), "application/x-entity-profile",
                 file_hash, len(str(source.get("quote", ""))), "parsed", now),
            )
        quote = str(source.get("quote", ""))
        conn.execute(
            "INSERT INTO entity_profile_sources (id,profile_id,version,filename,kind,locator,page,quote,file_id,locator_type,content_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, profile_id, version, source["filename"], source.get("kind", "other"),
             source.get("locator", ""), source.get("page"), quote, file_id,
             source.get("locator_type", "profile_locator"),
             source.get("content_hash") or hashlib.sha256(quote.encode("utf-8")).hexdigest(), now),
        )
    for fact in payload.get("facts") or []:
        references = [source_id_map.get(source_id, source_id) for source_id in fact.get("source_ids", [])]
        references = [source_id for source_id in references if source_id in source_ids]
        conn.execute(
            "INSERT INTO entity_profile_facts (id,profile_id,version,fact_key,category,value,unit,text_value,period,as_of_date,period_type,scope,subject_role,status,valid_until,source_ids,raw_value,confidence,currency,reviewer,reviewed_at,record_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid5(uuid.NAMESPACE_URL, f"entity-profile-fact:{profile_id}:{version}:{fact['id']}")) if fact.get("id") else str(uuid.uuid4()), profile_id, version, fact["fact_key"], fact.get("category", ""),
             fact.get("value"), fact.get("unit", ""), fact.get("text_value", ""), fact.get("period", ""), fact.get("as_of_date"),
             fact.get("period_type", "unknown"), fact.get("scope", "entity"), fact.get("subject_role", "issuer"),
             fact.get("status", "single_source"), fact.get("valid_until"), json_dump(references), fact.get("raw_value", ""),
             fact.get("confidence", 0.8), fact.get("currency", "CNY"), fact.get("reviewer", ""), fact.get("reviewed_at"),
             hashlib.sha256(json.dumps(fact, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest(), now),
        )
    for record_type in RECORD_TYPES:
        for record in payload.get(record_type) or []:
            references = [source_id_map.get(source_id, source_id) for source_id in record.get("source_ids", [])]
            references = [source_id for source_id in references if source_id in source_ids]
            data = record.get("data") or {}
            conn.execute(
                "INSERT INTO entity_profile_records (id,profile_id,version,record_type,period,as_of_date,scope,status,valid_until,source_ids,reviewer,reviewed_at,unit,currency,data_json,record_hash,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid5(uuid.NAMESPACE_URL, f"entity-profile-record:{profile_id}:{version}:{record['id']}")) if record.get("id") else str(uuid.uuid4()), profile_id, version,
                 record_type, record.get("period", ""), record.get("as_of_date"), record.get("scope", "entity"),
                 record.get("status", "single_source"), record.get("valid_until"), json_dump(references),
                 record.get("reviewer", ""), record.get("reviewed_at"), record.get("unit", ""),
                 record.get("currency", "CNY"), json_dump(data),
                 hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest(), now),
            )


def create_profile(payload: dict[str, Any], *, profile_id: str | None = None) -> dict[str, Any]:
    if row("SELECT id FROM entity_profiles WHERE unified_code=?", (payload["unified_code"],)):
        raise ValueError("统一社会信用代码已存在")
    profile_id = profile_id or str(uuid.uuid4())
    _validate_payload(payload)
    completeness, status = _derived_profile_metadata(profile_id, payload)
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO entity_profiles (id,name,unified_code,status,baseline_date,latest_financial_period,completeness,current_version,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (profile_id, payload["name"], payload["unified_code"], status,
             payload.get("baseline_date"), payload.get("latest_financial_period"), completeness, 1, now, now),
        )
        _insert_version(conn, profile_id, 1, payload)
    return get_profile(profile_id) or {}


def update_profile(profile_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_profile(profile_id)
    if not current:
        raise LookupError("主体档案不存在")
    version = int(current["version"]) + 1
    merged = {
        "name": payload.get("name") or current["name"],
        "baseline_date": payload.get("baseline_date", current["baseline_date"]),
        "latest_financial_period": payload.get("latest_financial_period", current["latest_financial_period"]),
        "unified_code": current["unified_code"],
        "facts": payload.get("facts") if payload.get("facts") is not None else current["facts"],
        "sources": payload.get("sources") if payload.get("sources") is not None else current["sources"],
        "data": payload.get("data") if payload.get("data") is not None else current["data"],
        "change_note": payload.get("change_note", "更新主体档案"),
    }
    for record_type in RECORD_TYPES:
        merged[record_type] = payload.get(record_type) if payload.get(record_type) is not None else current.get(record_type, [])
    _validate_payload(merged)
    completeness, status = _derived_profile_metadata(profile_id, merged)
    with connect() as conn:
        _insert_version(conn, profile_id, version, merged)
        conn.execute(
            "UPDATE entity_profiles SET name=?,status=?,baseline_date=?,latest_financial_period=?,completeness=?,current_version=?,updated_at=? WHERE id=?",
            (merged["name"], status, merged["baseline_date"], merged["latest_financial_period"],
             completeness, version, now_iso(), profile_id),
        )
    return get_profile(profile_id) or {}


def profile_readiness(profile_id: str) -> dict[str, Any]:
    profile = get_profile(profile_id)
    if not profile:
        raise LookupError("主体档案不存在")
    result = calculate_readiness(profile)
    result["blocking_reasons"] = [item["reason"] for item in result["hard_vetoes"]]
    result["status"] = profile["status"]
    return result


def bind_project_profiles(
    project_id: str, issuer_profile_id: str, guarantor_profile_id: str | None,
    counter_guarantor_profile_ids: list[str],
) -> None:
    bindings = [(issuer_profile_id, "issuer")]
    if guarantor_profile_id:
        bindings.append((guarantor_profile_id, "guarantor"))
    bindings.extend((profile_id, "counter_guarantor") for profile_id in counter_guarantor_profile_ids)
    seen: set[tuple[str, str]] = set()
    for profile_id, role in bindings:
        if (profile_id, role) in seen:
            continue
        seen.add((profile_id, role))
        if not get_profile(profile_id):
            raise LookupError(f"主体档案不存在：{profile_id}")
    with connect() as conn:
        conn.execute("DELETE FROM project_profile_roles WHERE project_id=?", (project_id,))
        for profile_id, role in bindings:
            conn.execute(
                "INSERT INTO project_profile_roles (id,project_id,profile_id,role,created_at) VALUES (?,?,?,?,?)",
                (str(uuid.uuid4()), project_id, profile_id, role, now_iso()),
            )


def create_project_snapshot(project_id: str) -> dict[str, Any]:
    bindings = rows(
        "SELECT r.role,p.* FROM project_profile_roles r JOIN entity_profiles p ON p.id=r.profile_id "
        "WHERE r.project_id=? ORDER BY CASE r.role WHEN 'issuer' THEN 0 WHEN 'guarantor' THEN 1 ELSE 2 END,p.name",
        (project_id,),
    )
    if not bindings:
        raise ValueError("项目尚未绑定主体档案")
    roles: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    records_payload: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    profile_versions: dict[str, int] = {}
    counts = {key: 0 for key in FACT_STATUSES}
    baseline_dates: list[str] = []
    for binding in bindings:
        profile = get_profile(binding["id"])
        if not profile:
            continue
        profile_versions[profile["id"]] = profile["version"]
        roles.append({
            "role": binding["role"], "profile_id": profile["id"], "name": profile["name"],
            "profile_version": profile["version"], "status": profile["status"],
            "completeness": profile["completeness"], "baseline_date": profile["baseline_date"],
            "latest_financial_period": profile["latest_financial_period"],
        })
        if profile.get("baseline_date"):
            baseline_dates.append(profile["baseline_date"])
        for key, value in profile["counts"].items():
            counts[key] += int(value)
        facts.extend({**item, "profile_id": profile["id"], "role": binding["role"]} for item in profile["facts"])
        for record_type in RECORD_TYPES:
            records_payload.extend(
                {**item, "profile_id": profile["id"], "role": binding["role"], "record_type": record_type}
                for item in profile.get(record_type, [])
            )
        sources.extend({**item, "profile_id": profile["id"], "role": binding["role"]} for item in profile["sources"])
    latest = row("SELECT COALESCE(MAX(version),0) version FROM project_profile_snapshots WHERE project_id=?", (project_id,))
    version = int(latest["version"] or 0) + 1
    payload = {"roles": roles, "facts": facts, "records": records_payload, "sources": sources, "profile_versions": profile_versions, "counts": counts}
    content_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    snapshot_id = str(uuid.uuid4())
    baseline_date = min(baseline_dates) if baseline_dates else None
    created_at = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO project_profile_snapshots (id,project_id,version,baseline_date,roles_json,facts_json,records_json,sources_json,counts_json,profile_versions_json,content_hash,evidence_status,evidence_count,evidence_error,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, project_id, version, baseline_date, json_dump(roles), json_dump(facts), json_dump(records_payload),
             json_dump(sources), json_dump(counts), json_dump(profile_versions), content_hash, "pending", 0, "", created_at),
        )
        conn.execute(
            "UPDATE projects SET profile_snapshot_id=?,analysis_stale=1,updated_at=? WHERE id=?",
            (snapshot_id, created_at, project_id),
        )
    return get_project_snapshot(project_id) or {}


def get_project_snapshot(project_id: str) -> dict[str, Any] | None:
    item = row(
        "SELECT s.* FROM projects p JOIN project_profile_snapshots s ON s.id=p.profile_snapshot_id "
        "WHERE p.id=?",
        (project_id,),
    )
    if not item:
        return None
    return {
        "id": item["id"], "version": item["version"], "baseline_date": item.get("baseline_date"),
        "created_at": item["created_at"], "roles": json_load(item["roles_json"]),
        "counts": json_load(item["counts_json"]), "facts": json_load(item["facts_json"]),
        "records": json_load(item.get("records_json") or "[]"),
        "sources": json_load(item["sources_json"]), "profile_versions": json_load(item["profile_versions_json"]),
        "content_hash": item["content_hash"], "evidence_status": item.get("evidence_status", "pending"),
        "evidence_count": int(item.get("evidence_count") or 0), "evidence_error": item.get("evidence_error", ""),
    }


def snapshot_readiness(project_id: str) -> dict[str, Any]:
    snapshot = get_project_snapshot(project_id)
    if not snapshot:
        return {"present": False, "complete": False, "counts": {}, "blocking_reasons": ["未绑定主体档案"]}
    issuer = next((role for role in snapshot["roles"] if role["role"] == "issuer"), None)
    if not issuer:
        return {
            "present": False, "complete": False, "ready_for_agent": False,
            "counts": snapshot["counts"], "hard_vetoes": [{
                "key": "issuer_profile", "label": "发行人主体档案", "reason": "主体快照缺少发行人",
                "message": "主体快照缺少发行人",
            }],
            "blocking_reasons": ["主体快照缺少发行人"],
            "baseline_date": snapshot["baseline_date"], "version": snapshot["version"],
        }
    issuer_profile_id = issuer.get("profile_id")
    profile = {
        "id": issuer_profile_id,
        "name": issuer.get("name", ""),
        "unified_code": "snapshot-bound",
        "facts": [
            item for item in snapshot.get("facts", [])
            if item.get("role") == "issuer" and item.get("profile_id") == issuer_profile_id
        ],
        "sources": [
            item for item in snapshot.get("sources", [])
            if item.get("role") == "issuer" and item.get("profile_id") == issuer_profile_id
        ],
        "counts": snapshot.get("counts", {}),
        **{record_type: [] for record_type in RECORD_TYPES},
    }
    for record in snapshot.get("records", []):
        if record.get("role") != "issuer" or record.get("profile_id") != issuer_profile_id:
            continue
        record_type = record.get("record_type")
        if record_type in RECORD_TYPES:
            profile[record_type].append(record)
    readiness = calculate_readiness(profile)
    readiness.update({
        "present": True,
        "complete": bool(readiness["ready_for_agent"]),
        "blocking_reasons": [item["reason"] for item in readiness["hard_vetoes"]],
        "baseline_date": snapshot["baseline_date"],
        "version": snapshot["version"],
        "snapshot_id": snapshot["id"],
    })
    return readiness


def project_snapshot_status(project_id: str) -> dict[str, Any]:
    project = row(
        "SELECT profile_snapshot_id,analysis_stale FROM projects WHERE id=?",
        (project_id,),
    )
    if not project or not project.get("profile_snapshot_id"):
        raise LookupError("项目尚未绑定主体档案")
    snapshot = get_project_snapshot(project_id)
    if not snapshot:
        raise LookupError("项目主体快照不存在")
    issuer = next((item for item in snapshot["roles"] if item.get("role") == "issuer"), None)
    snapshot_profile_version = int(issuer.get("profile_version") or 0) if issuer else 0
    current = row(
        "SELECT current_version FROM entity_profiles WHERE id=?",
        (issuer.get("profile_id"),),
    ) if issuer else None
    current_profile_version = int(current.get("current_version") or 0) if current else 0
    has_newer = current_profile_version > snapshot_profile_version
    return {
        "snapshot_id": snapshot["id"],
        "snapshot_version": snapshot["version"],
        "snapshot_profile_version": snapshot_profile_version,
        "current_profile_version": current_profile_version,
        "has_newer_version": has_newer,
        "refresh_required": has_newer,
        "analysis_stale": bool(project.get("analysis_stale")),
        "evidence_status": snapshot.get("evidence_status", "pending"),
        "evidence_count": snapshot.get("evidence_count", 0),
        "evidence_error": snapshot.get("evidence_error", ""),
    }


def project_profile_evidence(project_id: str) -> dict[str, Any]:
    snapshot = get_project_snapshot(project_id)
    if not snapshot:
        raise LookupError("项目尚未绑定主体档案")
    documents = rows(
        "SELECT * FROM project_profile_documents WHERE project_id=? AND snapshot_id=? ORDER BY entity_role,filename,id",
        (project_id, snapshot["id"]),
    )
    sources = rows(
        "SELECT * FROM project_profile_sources WHERE project_id=? AND snapshot_id=? ORDER BY entity_role,document_id,page,id",
        (project_id, snapshot["id"]),
    )
    for source in sources:
        source["bbox"] = json_load(source.pop("bbox_json", "[]"))
    return {
        "project_id": project_id,
        "snapshot_id": snapshot["id"],
        "snapshot_version": snapshot["version"],
        "evidence_status": snapshot.get("evidence_status", "pending"),
        "evidence_count": snapshot.get("evidence_count", 0),
        "evidence_error": snapshot.get("evidence_error", ""),
        "documents": documents,
        "sources": sources,
    }


def _bond_case_profile_seed(case: Any, index: int) -> dict[str, Any]:
    from .facts import FACT_REGISTRY

    profile_id = "profile-demo-huachen-manufacturing" if case.id == "demo-bond-huachen" else f"profile-{case.id}"
    code = f"91310000DEMOBOND{index:02d}X"
    representative = case.controller
    address = "上海市浦东新区金海路1888号" if case.id == "demo-bond-huachen" else f"{('上海市' if index % 2 else '北京市')}{'浦东新区' if index % 2 else '海淀区'}"
    source_prefix = f"profile-source-{case.short}"
    sources = [
        {
            "id": f"{source_prefix}-identity", "filename": f"{case.company}主体身份资料.pdf", "kind": "工商资料",
            "locator": "第1页 工商身份", "page": 1,
            "quote": f"{case.company}，法定代表人{representative}，注册地址{address}，注册资本10,000万元，成立日期2013年6月18日。",
        },
        {
            "id": f"{source_prefix}-ownership", "filename": f"{case.company}股权结构.pdf", "kind": "股权资料",
            "locator": "第1页 股权结构", "page": 1,
            "quote": f"{case.controller}持有发行人{case.controller_share}%股权，为实际控制人。",
        },
        {
            "id": f"{source_prefix}-operations", "filename": f"{case.company}经营资料.pdf", "kind": "经营资料",
            "locator": "第1页 经营及集中度", "page": 1,
            "quote": f"主营业务为{case.main_business}。2025年第一大客户占比{case.top_customer}%，第一大供应商占比{case.top_supplier}%。",
        },
        {
            "id": f"{source_prefix}-legal", "filename": f"{case.company}法律事项筛查.pdf", "kind": "法律资料",
            "locator": "第1页 法律事项筛查", "page": 1,
            "quote": "主体档案已记录法律事项筛查结果；独立法律意见仍应结合本次债券发行另行取得。",
        },
    ]
    facts: list[dict[str, Any]] = [
        {"fact_key": "legal_representative", "category": "主体治理", "text_value": representative, "status": "confirmed", "source_ids": [f"{source_prefix}-identity"]},
        {"fact_key": "registered_address", "category": "主体治理", "text_value": address, "status": "confirmed", "source_ids": [f"{source_prefix}-identity"]},
        {"fact_key": "establishment_date", "category": "主体治理", "text_value": "2013-06-18", "status": "confirmed", "source_ids": [f"{source_prefix}-identity"]},
        {"fact_key": "registered_capital", "category": "主体治理", "value": 10000, "unit": "万元", "status": "confirmed", "source_ids": [f"{source_prefix}-identity"]},
        {"fact_key": "controller", "category": "主体治理", "text_value": case.controller, "status": "confirmed", "source_ids": [f"{source_prefix}-ownership"]},
        {"fact_key": "controller_share", "category": "主体治理", "value": float(case.controller_share), "unit": "%", "status": "confirmed", "source_ids": [f"{source_prefix}-ownership"]},
        {"fact_key": "main_business", "category": "经营", "text_value": case.main_business, "status": "confirmed", "source_ids": [f"{source_prefix}-operations"]},
        {"fact_key": "top_customer_share", "category": "经营", "period": "2025", "value": float(case.top_customer), "unit": "%", "status": "confirmed", "source_ids": [f"{source_prefix}-operations"]},
        {"fact_key": "top_supplier_share", "category": "经营", "period": "2025", "value": float(case.top_supplier), "unit": "%", "status": "confirmed", "source_ids": [f"{source_prefix}-operations"]},
        {"fact_key": "related_parties", "category": "关联方", "text_value": "主要关联方清单已纳入主体档案", "status": "confirmed", "source_ids": [f"{source_prefix}-operations"]},
        {"fact_key": "related_unsupported_amount", "category": "关联方", "period": "2025", "value": float(case.related_unsupported), "unit": "万元", "status": "confirmed", "source_ids": [f"{source_prefix}-operations"]},
        {"fact_key": "external_guarantee", "category": "担保", "period": "2025", "value": 0, "unit": "万元", "scope": "consolidated", "status": "single_source", "source_ids": [f"{source_prefix}-legal"]},
        {"fact_key": "contingent_liabilities", "category": "担保", "period": "2025", "value": 0, "unit": "万元", "scope": "consolidated", "status": "single_source", "source_ids": [f"{source_prefix}-legal"]},
        {"fact_key": "legal_opinion_status", "category": "发行合规", "text_value": "主体法律事项筛查已记录，本次发行法律意见待取得", "status": "single_source", "source_ids": [f"{source_prefix}-legal"]},
    ]
    financial_keys = (
        "cash", "restricted_cash", "accounts_receivable", "other_receivables", "inventory",
        "current_assets", "total_assets", "short_term_debt", "debt_due_12m", "current_liabilities",
        "long_term_debt", "bonds_payable", "interest_bearing_debt", "total_liabilities", "equity",
        "revenue", "cost_of_goods_sold", "finance_expense", "interest_expense", "net_profit",
        "cash_received_from_sales", "operating_cash_flow", "investing_cash_flow", "financing_cash_flow",
    )
    for snapshot in case.financials:
        source_id = f"{source_prefix}-financial-{snapshot.year}"
        sources.append({
            "id": source_id, "filename": f"{case.company}{snapshot.year}年度审计数据包.pdf", "kind": "审计报告",
            "locator": f"第1页 {snapshot.year}年度主要财务报表", "page": 1,
            "quote": "；".join(f"{FACT_REGISTRY[key].label}{getattr(snapshot, key):,.0f}万元" for key in financial_keys) + "。",
        })
        for key in financial_keys:
            facts.append({
                "fact_key": key, "category": "债务" if key in {"short_term_debt", "debt_due_12m", "long_term_debt", "bonds_payable", "interest_bearing_debt"} else "财务",
                "period": str(snapshot.year), "period_type": "duration" if key in {"revenue", "cost_of_goods_sold", "finance_expense", "interest_expense", "net_profit", "cash_received_from_sales", "operating_cash_flow", "investing_cash_flow", "financing_cash_flow"} else "point_in_time",
                "scope": "consolidated", "value": float(getattr(snapshot, key)), "unit": "万元",
                "status": "confirmed", "source_ids": [source_id],
            })
    record = lambda source_id, data, period="2025", scope="entity": {
        "period": period, "scope": scope, "status": "confirmed", "source_ids": [source_id], "data": data,
    }
    return {
        "id": profile_id, "name": case.company, "unified_code": code,
        "baseline_date": "2025-12-31", "latest_financial_period": "2025",
        "data": {"industry": case.industry, "seed_version": "entity-profile-bond-v3.1"},
        "sources": sources, "facts": facts,
        "shareholders": [record(f"{source_prefix}-ownership", {"name": case.controller, "shareholding_ratio": float(case.controller_share)})],
        "financial_periods": [record(f"{source_prefix}-financial-{item.year}", {"name": f"{item.year}年度主要财务报表"}, str(item.year), "consolidated") for item in case.financials],
        "debt_instruments": [record(f"{source_prefix}-financial-2025", {"instrument_name": "2025年末有息债务及到期结构"}, "2025", "consolidated")],
        "credit_facilities": [record(f"{source_prefix}-financial-2025", {"facility_name": "银行授信及债务台账"}, "2025", "consolidated")],
        "concentrations": [record(f"{source_prefix}-operations", {"name": "客户供应商集中度"})],
        "related_transactions": [record(f"{source_prefix}-operations", {"name": "关联交易清单"})],
        "legal_events": [record(f"{source_prefix}-legal", {"event_type": "法律事项筛查", "result": "已记录"})],
        "material_packages": [record(f"{source_prefix}-identity", {"name": "发行人主体档案资料包"})],
        "change_note": "系统演示档案升级：三个完整年度融资担保核心财务数据",
    }


def seed_entity_profiles() -> None:
    huachen_sources = [
        {"id": "profile-source-huachen-identity", "filename": "华辰主体档案资料包.pdf", "kind": "工商资料", "locator": "第1页 工商信息", "page": 1, "quote": "华辰精密制造有限公司法定代表人为陈嘉诚，注册地址为上海市浦东新区金海路1888号，成立于2013年6月18日，注册资本10,000万元。"},
        {"id": "profile-source-huachen-ownership", "filename": "华辰主体档案资料包.pdf", "kind": "股权资料", "locator": "第2页 股权结构", "page": 2, "quote": "陈嘉诚直接及间接持有公司58%股权，为实际控制人。"},
        {"id": "profile-source-huachen-fin-2023", "filename": "华辰主体档案资料包.pdf", "kind": "审计报告", "locator": "第3页 2023年度财务报表", "page": 3, "quote": "2023年资产总额59,040万元、负债总额41,328万元、所有者权益17,712万元、营业收入39,360万元、净利润2,296万元、经营活动现金流量净额4,680万元、有息负债25,420万元。"},
        {"id": "profile-source-huachen-fin-2024", "filename": "华辰主体档案资料包.pdf", "kind": "审计报告", "locator": "第4页 2024年度财务报表", "page": 4, "quote": "2024年资产总额65,520万元、负债总额45,864万元、所有者权益19,656万元、营业收入43,680万元、净利润2,548万元、经营活动现金流量净额5,590万元、有息负债28,210万元。"},
        {"id": "profile-source-huachen-fin-2025", "filename": "华辰主体档案资料包.pdf", "kind": "审计报告", "locator": "第5页 2025年度财务报表", "page": 5, "quote": "2025年资产总额72,000万元、负债总额50,400万元、所有者权益21,600万元、营业收入48,000万元、净利润2,800万元、经营活动现金流量净额6,500万元、有息负债31,000万元。"},
        {"id": "profile-source-huachen-debt", "filename": "华辰主体档案资料包.pdf", "kind": "债务明细", "locator": "第7页 债务及或有事项", "page": 7, "quote": "截至2026年3月末有息负债31,800万元，未来12个月到期债务20,500万元，对外担保及其他或有负债余额均为0万元。"},
        {"id": "profile-source-huachen-operation", "filename": "华辰主体档案资料包.pdf", "kind": "经营资料", "locator": "第8页 经营及关联方", "page": 8, "quote": "公司主营精密汽车结构件、传动部件及新能源车热管理组件；第一大客户占比36%，第一大供应商占比31%，关联方往来中缺少合同支持的金额为1,200万元。"},
        {"id": "profile-source-huachen-legal", "filename": "华辰主体档案资料包.pdf", "kind": "法律资料", "locator": "第9页 法律事项筛查", "page": 9, "quote": "已完成重大诉讼、行政处罚及对外担保筛查，未发现需单独披露的重大法律事件。"},
    ]
    huachen_facts = [
        {"fact_key": "legal_representative", "category": "主体治理", "text_value": "陈嘉诚", "status": "confirmed", "source_ids": ["profile-source-huachen-identity"]},
        {"fact_key": "registered_address", "category": "主体治理", "text_value": "上海市浦东新区金海路1888号", "status": "confirmed", "source_ids": ["profile-source-huachen-identity"]},
        {"fact_key": "establishment_date", "category": "主体治理", "text_value": "2013-06-18", "status": "confirmed", "source_ids": ["profile-source-huachen-identity"]},
        {"fact_key": "registered_capital", "category": "主体治理", "value": 10000, "unit": "万元", "status": "confirmed", "source_ids": ["profile-source-huachen-identity"]},
        {"fact_key": "controller", "category": "主体治理", "text_value": "陈嘉诚", "status": "confirmed", "source_ids": ["profile-source-huachen-ownership"]},
        {"fact_key": "controller_share", "category": "主体治理", "value": 58, "unit": "%", "status": "confirmed", "source_ids": ["profile-source-huachen-ownership"]},
        {"fact_key": "main_business", "category": "经营", "text_value": "精密汽车结构件、传动部件及新能源车热管理组件的研发、生产和销售", "status": "confirmed", "source_ids": ["profile-source-huachen-operation"]},
        {"fact_key": "top_customer_share", "category": "经营", "period": "2025", "value": 36, "unit": "%", "status": "confirmed", "source_ids": ["profile-source-huachen-operation"]},
        {"fact_key": "top_supplier_share", "category": "经营", "period": "2025", "value": 31, "unit": "%", "status": "confirmed", "source_ids": ["profile-source-huachen-operation"]},
        {"fact_key": "related_parties", "category": "关联方", "text_value": "华辰精密制造集团有限公司等", "status": "confirmed", "source_ids": ["profile-source-huachen-operation"]},
        {"fact_key": "related_unsupported_amount", "category": "关联方", "period": "2025", "value": 1200, "unit": "万元", "status": "confirmed", "source_ids": ["profile-source-huachen-operation"]},
        {"fact_key": "debt_due_12m", "category": "债务", "period": "2025", "scope": "consolidated", "value": 20500, "unit": "万元", "status": "confirmed", "source_ids": ["profile-source-huachen-debt"]},
        {"fact_key": "external_guarantee", "category": "担保", "period": "2025", "scope": "consolidated", "value": 0, "unit": "万元", "status": "confirmed", "source_ids": ["profile-source-huachen-debt"]},
        {"fact_key": "contingent_liabilities", "category": "担保", "period": "2025", "scope": "consolidated", "value": 0, "unit": "万元", "status": "confirmed", "source_ids": ["profile-source-huachen-debt"]},
        {"fact_key": "legal_opinion_status", "category": "发行合规", "text_value": "已完成重大法律事项筛查", "status": "confirmed", "source_ids": ["profile-source-huachen-legal"]},
    ]
    financial_values = {
        "2023": (59040, 41328, 17712, 39360, 2296, 4680, 25420),
        "2024": (65520, 45864, 19656, 43680, 2548, 5590, 28210),
        "2025": (72000, 50400, 21600, 48000, 2800, 6500, 31000),
    }
    financial_keys = ("total_assets", "total_liabilities", "equity", "revenue", "net_profit", "operating_cash_flow", "interest_bearing_debt")
    for period, values in financial_values.items():
        source_id = f"profile-source-huachen-fin-{period.lower()}"
        for fact_key, value in zip(financial_keys, values):
            huachen_facts.append({
                "fact_key": fact_key, "category": "财务" if fact_key != "interest_bearing_debt" else "债务",
                "period": period, "scope": "consolidated", "value": value, "unit": "万元",
                "status": "confirmed", "source_ids": [source_id],
            })
    record = lambda source_id, data, period="2025": {
        "period": period, "scope": "entity", "status": "confirmed", "source_ids": [source_id], "data": data,
    }
    seeds = [
        {
            "id": "profile-demo-huachen-manufacturing", "name": "华辰精密制造有限公司", "unified_code": "91310000DEMOHC001X",
            "baseline_date": "2025-12-31", "latest_financial_period": "2025", "status": "ready", "completeness": 0.88,
            "data": {"industry": "精密制造", "controller": "陈嘉诚", "seed_version": "entity-profile-demo-v2"},
            "sources": huachen_sources,
            "facts": huachen_facts,
            "shareholders": [record("profile-source-huachen-ownership", {"name": "陈嘉诚及其控制主体", "shareholding_ratio": 58})],
            "financial_periods": [record(f"profile-source-huachen-fin-{period.lower()}", {"name": f"{period}主要财务报表"}, period) for period in financial_values],
            "debt_instruments": [record("profile-source-huachen-debt", {"instrument_name": "有息债务及未来12个月到期结构"})],
            "credit_facilities": [record("profile-source-huachen-debt", {"facility_name": "银行授信及债务台账"})],
            "concentrations": [record("profile-source-huachen-operation", {"name": "客户供应商集中度"}, "2025")],
            "related_transactions": [record("profile-source-huachen-operation", {"counterparty": "华辰精密制造集团有限公司"}, "2025")],
            "legal_events": [record("profile-source-huachen-legal", {"event_type": "重大法律事项筛查", "result": "未发现需单独披露的重大事项"})],
            "material_packages": [record("profile-source-huachen-identity", {"name": "主体档案资料包"})],
        },
        {
            "id": "profile-demo-huachen-group", "name": "华辰精密制造集团有限公司", "unified_code": "91310000DEMOHC002Q",
            "baseline_date": "2025-12-31", "latest_financial_period": "2025", "status": "incomplete", "completeness": 0.52,
            "data": {"industry": "装备制造"}, "sources": [], "facts": [],
        },
        {
            "id": "profile-demo-guarantor", "name": "中汇融资担保有限公司", "unified_code": "91110000DEMOGT001A",
            "baseline_date": "2025-12-31", "latest_financial_period": "2025", "status": "ready", "completeness": 0.91,
            "data": {"institution_type": "融资担保公司"}, "sources": [], "facts": [],
        },
    ]
    from .bond_documents import CASE_SPECS

    case_seeds = [_bond_case_profile_seed(case, index) for index, case in enumerate(CASE_SPECS, 1)]
    seeds[0] = case_seeds[0]
    seeds.extend(case_seeds[1:])
    for seed in seeds:
        existing = row(
            "SELECT id,current_version FROM entity_profiles WHERE id=? OR unified_code=?",
            (seed["id"], seed["unified_code"]),
        )
        if not existing:
            create_profile(seed, profile_id=seed["id"])
            continue
        current = get_profile(existing["id"])
        current_seed_version = str(((current or {}).get("data") or {}).get("seed_version") or "")
        desired_seed_version = str((seed.get("data") or {}).get("seed_version") or "")
        is_managed_demo = seed["id"].startswith("profile-demo-") or seed["id"].startswith("profile-demo-bond-")
        if desired_seed_version and desired_seed_version != current_seed_version and is_managed_demo:
            update_profile(existing["id"], seed)

    # Evaluation projects use the exact same issuer profile facts as their
    # generated documents. Existing snapshots are refreshed only when the
    # managed fixture version changes; user projects are never touched here.
    from .profile_evidence import materialize_snapshot_evidence

    for case, seed in zip(CASE_SPECS, case_seeds):
        profile = get_profile(seed["id"])
        project = row("SELECT profile_snapshot_id FROM projects WHERE id=?", (case.id,))
        if not profile or not project:
            continue
        binding = row(
            "SELECT profile_id FROM project_profile_roles WHERE project_id=? AND role='issuer'",
            (case.id,),
        )
        snapshot = get_project_snapshot(case.id)
        snapshot_role = next((item for item in (snapshot or {}).get("roles", []) if item.get("role") == "issuer"), None)
        needs_snapshot = (
            not binding or binding.get("profile_id") != profile["id"] or not snapshot_role
            or int(snapshot_role.get("profile_version") or 0) != int(profile["version"])
        )
        if not needs_snapshot:
            continue
        bind_project_profiles(case.id, profile["id"], None, [])
        execute_values = (profile["id"], case.id)
        with connect() as conn:
            conn.execute("UPDATE projects SET issuer_profile_id=? WHERE id=?", execute_values)
        create_project_snapshot(case.id)
        try:
            materialize_snapshot_evidence(case.id)
        except Exception:
            pass
