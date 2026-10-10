from __future__ import annotations

import hashlib
import json
from typing import Any

from .database import connect, json_dump, json_load, now_iso, row, rows
from .facts import FACT_REGISTRY
from .p0 import default_scope, period_type


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "|".join(str(part or "") for part in parts)
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _snapshot(project_id: str, snapshot_id: str | None = None) -> dict[str, Any] | None:
    if snapshot_id:
        return row(
            "SELECT * FROM project_profile_snapshots WHERE id=? AND project_id=?",
            (snapshot_id, project_id),
        )
    return row(
        "SELECT s.* FROM projects p JOIN project_profile_snapshots s "
        "ON s.id=p.profile_snapshot_id WHERE p.id=?",
        (project_id,),
    )


def _decoded(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json_load(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _reliable_source(source: dict[str, Any]) -> bool:
    quote = str(source.get("quote") or source.get("text") or "").strip()
    locator = str(source.get("locator") or "").strip()
    return bool(quote and (locator or source.get("page") is not None))


def materialize_snapshot_evidence(project_id: str, snapshot_id: str | None = None) -> dict[str, Any]:
    """Project the immutable profile snapshot into the separate evidence tables.

    Projection IDs include the snapshot ID, so rebuilding a snapshot is idempotent
    while historical snapshots remain available for audit. The analysis views select
    only the snapshot currently bound to the project.
    """
    snapshot = _snapshot(project_id, snapshot_id)
    if not snapshot:
        return {
            "project_id": project_id,
            "snapshot_id": snapshot_id or "",
            "status": "missing",
            "document_count": 0,
            "source_count": 0,
        }

    snapshot_id = snapshot["id"]
    sources = _decoded(snapshot.get("sources_json"), [])
    roles = _decoded(snapshot.get("roles_json"), [])
    profile_versions = _decoded(snapshot.get("profile_versions_json"), {})
    role_by_profile = {item.get("profile_id", ""): item.get("role", "issuer") for item in roles}
    name_by_profile = {item.get("profile_id", ""): item.get("name", "") for item in roles}

    reliable_sources = [
        source for source in sources
        if _reliable_source(source)
        and (source.get("role") or role_by_profile.get(str(source.get("profile_id") or ""), "issuer")) == "issuer"
    ]
    document_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for source in reliable_sources:
        profile_id = str(source.get("profile_id") or "")
        origin_document_id = str(
            source.get("file_id") or source.get("origin_document_id") or source.get("filename") or "profile-document"
        )
        document_groups.setdefault((profile_id, origin_document_id), []).append(source)

    try:
        with connect() as conn:
            conn.execute("DELETE FROM project_profile_sources WHERE project_id=? AND snapshot_id=?", (project_id, snapshot_id))
            conn.execute("DELETE FROM project_profile_documents WHERE project_id=? AND snapshot_id=?", (project_id, snapshot_id))
            document_ids: dict[tuple[str, str], str] = {}
            created_at = snapshot.get("created_at") or now_iso()
            for (profile_id, origin_document_id), grouped_sources in document_groups.items():
                sample = grouped_sources[0]
                document_id = _stable_id("profile-doc", project_id, snapshot_id, profile_id, origin_document_id)
                document_ids[(profile_id, origin_document_id)] = document_id
                filename = str(sample.get("filename") or f"{name_by_profile.get(profile_id) or '主体'}档案证据")
                kind = str(sample.get("kind") or "主体档案")
                content = "\n".join(str(item.get("quote") or item.get("text") or "") for item in grouped_sources)
                pages = [int(item["page"]) for item in grouped_sources if item.get("page") is not None]
                profile_version = int(sample.get("version") or profile_versions.get(profile_id) or 0)
                conn.execute(
                    "INSERT INTO project_profile_documents "
                    "(id,project_id,snapshot_id,profile_id,profile_version,entity_role,origin_document_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,warning,created_at,parser,document_role,ocr_status,page_count) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        document_id, project_id, snapshot_id, profile_id, profile_version,
                        sample.get("role") or role_by_profile.get(profile_id, "issuer"), origin_document_id,
                        filename, kind, "application/x-entity-profile",
                        hashlib.sha256(content.encode("utf-8")).hexdigest(), len(content.encode("utf-8")),
                        "parsed", 1.0, len(grouped_sources), "", created_at, "profile_snapshot",
                        "entity_profile", "not_required", max(pages) if pages else 0,
                    ),
                )

            for source in reliable_sources:
                profile_id = str(source.get("profile_id") or "")
                origin_document_id = str(
                    source.get("file_id") or source.get("origin_document_id") or source.get("filename") or "profile-document"
                )
                origin_source_id = str(source.get("id") or source.get("source_id") or "")
                if not origin_source_id:
                    origin_source_id = _stable_id(
                        "profile-origin-source", profile_id, source.get("locator"), source.get("quote") or source.get("text")
                    )
                source_id = _stable_id("profile-source", project_id, snapshot_id, origin_source_id)
                text = str(source.get("quote") or source.get("text") or "").strip()
                locator = str(source.get("locator") or "").strip()
                if not locator and source.get("page") is not None:
                    locator = f"第{source['page']}页"
                profile_version = int(source.get("version") or profile_versions.get(profile_id) or 0)
                conn.execute(
                    "INSERT INTO project_profile_sources "
                    "(id,project_id,snapshot_id,profile_id,profile_version,entity_role,document_id,origin_source_id,locator_type,locator,text,page,bbox_json,extraction_method,content_hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        source_id, project_id, snapshot_id, profile_id, profile_version,
                        source.get("role") or role_by_profile.get(profile_id, "issuer"),
                        document_ids[(profile_id, origin_document_id)], origin_source_id,
                        source.get("locator_type") or ("page" if source.get("page") is not None else "profile_locator"),
                        locator, text, source.get("page"), json_dump(source.get("bbox") or []),
                        "profile_snapshot", hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    ),
                )
            evidence_status = "ready" if reliable_sources else "insufficient"
            conn.execute(
                "UPDATE project_profile_snapshots SET evidence_status=?,evidence_count=?,evidence_error='' WHERE id=?",
                (evidence_status, len(reliable_sources), snapshot_id),
            )
    except Exception as exc:
        with connect() as conn:
            conn.execute(
                "UPDATE project_profile_snapshots SET evidence_status='failed',evidence_error=? WHERE id=?",
                (str(exc)[:500], snapshot_id),
            )
        raise

    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "status": "ready" if reliable_sources else "insufficient",
        "document_count": len(document_groups),
        "source_count": len(reliable_sources),
    }


def profile_assertion_rows(project_id: str) -> list[dict[str, Any]]:
    """Return source-backed facts from the current snapshot as assertion payloads."""
    snapshot = _snapshot(project_id)
    if not snapshot:
        return []
    materialize_snapshot_evidence(project_id, snapshot["id"])
    projected_sources = {
        item["origin_source_id"]: item["id"]
        for item in rows(
            "SELECT id,origin_source_id FROM project_profile_sources WHERE project_id=? AND snapshot_id=?",
            (project_id, snapshot["id"]),
        )
    }
    roles = _decoded(snapshot.get("roles_json"), [])
    role_by_profile = {item.get("profile_id", ""): item for item in roles}
    profile_versions = _decoded(snapshot.get("profile_versions_json"), {})
    output: list[dict[str, Any]] = []

    facts = list(_decoded(snapshot.get("facts_json"), []))
    # Repeatable records are allowed to supply assertions only when their data
    # uses an explicit registered fact key. We deliberately do not infer that
    # an arbitrary debt-record balance is the issuer's total debt, nor sum
    # record rows here; aggregation belongs to deterministic domain code.
    for record in _decoded(snapshot.get("records_json"), []):
        data = _decoded(record.get("data"), {})
        if not isinstance(data, dict):
            continue
        candidates: list[tuple[str, Any]] = []
        explicit_key = str(data.get("fact_key") or "")
        if explicit_key in FACT_REGISTRY:
            candidates.append((explicit_key, data.get("value", data.get("text_value"))))
        nested = data.get("facts")
        if isinstance(nested, dict):
            candidates.extend((str(key), value) for key, value in nested.items() if str(key) in FACT_REGISTRY)
        candidates.extend(
            (str(key), value) for key, value in data.items()
            if str(key) in FACT_REGISTRY and str(key) != explicit_key
        )
        for fact_key, supplied in candidates:
            if supplied is None or isinstance(supplied, bool):
                continue
            definition = FACT_REGISTRY[fact_key]
            value = supplied if isinstance(supplied, (int, float)) else None
            text_value = str(supplied) if definition.unit_kind == "text" and isinstance(supplied, str) else ""
            if value is None and not text_value:
                continue
            record_id = str(record.get("id") or record.get("record_hash") or record.get("record_type") or "record")
            facts.append({
                "id": f"{record_id}:{fact_key}",
                "profile_id": record.get("profile_id", ""),
                "role": record.get("role", "issuer"),
                "fact_key": fact_key,
                "period": record.get("period") or record.get("as_of_date") or "",
                "period_type": data.get("period_type") or "unknown",
                "scope": record.get("scope", "unknown"),
                "value": value,
                "text_value": text_value,
                "unit": data.get("unit") or record.get("unit", ""),
                "status": record.get("status", "single_source"),
                "source_ids": record.get("source_ids") or [],
                "raw_value": data.get("raw_value") or str(supplied),
                "confidence": data.get("confidence", 0.8),
                "version": record.get("version"),
            })

    for fact in facts:
        fact_key = str(fact.get("fact_key") or "")
        if fact_key not in FACT_REGISTRY:
            continue
        source_ids = _decoded(fact.get("source_ids"), [])
        projected_ids = [projected_sources[source_id] for source_id in source_ids if source_id in projected_sources]
        if not projected_ids:
            continue
        definition = FACT_REGISTRY[fact_key]
        profile_id = str(fact.get("profile_id") or "")
        role = str(fact.get("role") or role_by_profile.get(profile_id, {}).get("role") or "issuer")
        if role != "issuer":
            continue
        role_data = role_by_profile.get(profile_id, {})
        subject = str(fact.get("subject") or role_data.get("name") or "")
        scope = str(fact.get("scope") or "unknown")
        if scope == "unknown":
            scope = default_scope(definition.category)
        period = str(fact.get("period") or "")
        status = str(fact.get("status") or "single_source")
        origin_fact_id = str(fact.get("id") or fact.get("record_hash") or fact_key)
        profile_version = int(fact.get("version") or profile_versions.get(profile_id) or 0)
        raw_value = str(fact.get("raw_value") or fact.get("text_value") or "")
        if not raw_value and fact.get("value") is not None:
            raw_value = f"{fact['value']}{fact.get('unit', '')}"
        output.append({
            "id": _stable_id("profile-assertion", project_id, snapshot["id"], profile_id, origin_fact_id),
            "fact_key": fact_key,
            "category": definition.category,
            "name": definition.label,
            "period": period,
            "period_type": fact.get("period_type") or period_type(fact_key, period),
            "value": fact.get("value"),
            "unit": fact.get("unit", ""),
            "text_value": fact.get("text_value", ""),
            "raw_value": raw_value,
            "source_ids": projected_ids,
            "confidence": float(fact.get("confidence") or (1.0 if status == "confirmed" else 0.8)),
            "extraction_method": "profile_snapshot",
            "review_status": "human_verified" if status == "confirmed" else status,
            "assertion_status": status,
            "origin_type": "profile_snapshot",
            "origin_ref": f"{profile_id}:{profile_version}:{origin_fact_id}",
            "profile_snapshot_id": snapshot["id"],
            "profile_id": profile_id,
            "profile_version": profile_version,
            "subject": subject,
            "subject_role": role,
            "scope": scope,
        })
    return output


def profile_evidence_status(project_id: str) -> dict[str, Any]:
    snapshot = _snapshot(project_id)
    if not snapshot:
        return {
            "project_id": project_id, "snapshot_id": "", "status": "missing",
            "document_count": 0, "source_count": 0,
        }
    document_count = row(
        "SELECT COUNT(*) count FROM project_profile_documents WHERE project_id=? AND snapshot_id=?",
        (project_id, snapshot["id"]),
    )["count"]
    source_count = row(
        "SELECT COUNT(*) count FROM project_profile_sources WHERE project_id=? AND snapshot_id=?",
        (project_id, snapshot["id"]),
    )["count"]
    return {
        "project_id": project_id,
        "snapshot_id": snapshot["id"],
        "snapshot_version": snapshot["version"],
        "status": snapshot.get("evidence_status") or "pending",
        "document_count": int(document_count),
        "source_count": int(source_count),
        "error": snapshot.get("evidence_error") or "",
    }


def analysis_document_rows(project_id: str) -> list[dict[str, Any]]:
    return rows("SELECT * FROM analysis_documents WHERE project_id=? ORDER BY created_at,id", (project_id,))


def analysis_source_rows(project_id: str) -> list[dict[str, Any]]:
    return rows("SELECT * FROM analysis_sources WHERE project_id=? ORDER BY document_id,page,id", (project_id,))


def analysis_source(project_id: str, source_id: str) -> dict[str, Any] | None:
    return row(
        "SELECT s.*,d.filename,d.kind,d.document_role FROM analysis_sources s "
        "JOIN analysis_documents d ON d.id=s.document_id AND d.project_id=s.project_id "
        "WHERE s.project_id=? AND s.id=?",
        (project_id, source_id),
    )
