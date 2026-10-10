from __future__ import annotations

import uuid

from backend.app.database import connect, init_db, json_dump, json_load, now_iso, row, rows
from backend.app.p0 import build_evidence_graph
from backend.app.professional import analysis_fact_rows, fact_groups, resolve_fact_group, sync_fact_assertions
from backend.app.profile_evidence import analysis_source_rows, materialize_snapshot_evidence
from backend.app.workflow import _citations, _node_input_hash, invalidate_project_analysis


def _insert_project_and_snapshot(*, profile_value: float, profile_status: str = "confirmed") -> tuple[str, str]:
    project_id = f"profile-agent-{uuid.uuid4()}"
    snapshot_id = f"snapshot-{uuid.uuid4()}"
    profile_id = f"profile-{uuid.uuid4()}"
    created_at = now_iso()
    roles = [{
        "role": "issuer", "profile_id": profile_id, "name": "Profile Agent Issuer",
        "profile_version": 1, "status": "ready", "completeness": 1.0,
    }]
    facts = [{
        "id": "profile-assets", "profile_id": profile_id, "role": "issuer",
        "fact_key": "total_assets", "period": "2025", "scope": "consolidated",
        "value": profile_value, "unit": "CNY10k", "status": profile_status,
        "source_ids": ["profile-assets-source"],
    }]
    sources = [{
        "id": "profile-assets-source", "profile_id": profile_id, "role": "issuer",
        "filename": "profile-financial.xlsx", "kind": "financial_statement",
        "locator_type": "cell", "locator": "Assets!B2", "quote": f"total assets {profile_value}",
    }]
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,consent_at,project_type,profile_snapshot_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, "token", "Profile Agent Issuer", 20000, 36, "bond", "draft", created_at,
             created_at, created_at, "bond_guarantee", snapshot_id),
        )
        conn.execute(
            "INSERT INTO project_profile_snapshots "
            "(id,project_id,version,baseline_date,roles_json,facts_json,sources_json,counts_json,profile_versions_json,content_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, project_id, 1, "2025-12-31", json_dump(roles), json_dump(facts), json_dump(sources),
             json_dump({profile_status: 1}), json_dump({profile_id: 1}), f"hash-{profile_value}", created_at),
        )
    return project_id, snapshot_id


def _add_document_source(project_id: str) -> str:
    document_id = f"document-{uuid.uuid4()}"
    source_id = f"source-{uuid.uuid4()}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (document_id, project_id, "prospectus.pdf", "prospectus", "application/pdf", document_id,
             100, "parsed", 1.0, 1, now_iso()),
        )
        conn.execute(
            "INSERT INTO sources (id,project_id,document_id,locator_type,locator,text,page,content_hash) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (source_id, project_id, document_id, "page", "p.10", "total assets 1100", 10, source_id),
        )
    return source_id


def _switch_snapshot(project_id: str, previous_snapshot: str, value: float) -> str:
    current = row("SELECT * FROM project_profile_snapshots WHERE id=?", (previous_snapshot,))
    facts = json_load(current["facts_json"])
    facts[0] = {**facts[0], "value": value, "raw_value": str(value)}
    sources = json_load(current["sources_json"])
    sources[0] = {**sources[0], "quote": f"total assets {value}"}
    snapshot_id = f"snapshot-{uuid.uuid4()}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO project_profile_snapshots "
            "(id,project_id,version,baseline_date,roles_json,facts_json,sources_json,counts_json,profile_versions_json,content_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, project_id, int(current["version"]) + 1, current["baseline_date"], current["roles_json"],
             json_dump(facts), json_dump(sources), current["counts_json"], current["profile_versions_json"],
             f"hash-{value}-{snapshot_id}", now_iso()),
        )
        conn.execute("UPDATE projects SET profile_snapshot_id=?,analysis_stale=1 WHERE id=?", (snapshot_id, project_id))
    return snapshot_id


def _cleanup(project_id: str) -> None:
    with connect() as conn:
        for table in (
            "audit_events", "report_claims", "report_sections", "citations", "evidence_edges", "financial_metrics",
            "canonical_facts", "fact_assertions", "facts", "project_profile_sources",
            "project_profile_documents", "project_profile_snapshots", "project_entities", "documents", "sources",
        ):
            conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def test_profile_and_document_assertions_conflict_and_resolution_expires_on_refresh() -> None:
    init_db()
    project_id, snapshot_id = _insert_project_and_snapshot(profile_value=1000)
    try:
        document_source = _add_document_source(project_id)
        document_fact = {
            "id": f"fact-{uuid.uuid4()}", "fact_key": "total_assets", "period": "2025",
            "scope": "consolidated", "value": 1100, "unit": "CNY10k", "raw_value": "1100",
            "source_ids": [document_source], "extraction_method": "deterministic",
        }
        sync_fact_assertions(project_id, [document_fact])
        group = fact_groups(project_id)[0]
        assert group["status"] == "conflicted"
        assert {item["origin_type"] for item in group["assertions"]} == {"document", "profile_snapshot"}

        document_assertion = next(item for item in group["assertions"] if item["origin_type"] == "document")
        resolved = resolve_fact_group(project_id, group["id"], document_assertion["id"], None, "verified")
        assert resolved["status"] == "confirmed"
        old_basis = resolved["resolution_basis_hash"]
        old_hash = _node_input_hash(project_id, "extract")

        new_snapshot = _switch_snapshot(project_id, snapshot_id, 1200)
        materialize_snapshot_evidence(project_id, new_snapshot)
        sync_fact_assertions(project_id, [document_fact])
        refreshed = fact_groups(project_id)[0]
        assert refreshed["status"] == "conflicted"
        assert refreshed["resolved_by"] == "system"
        assert refreshed["resolution_note"] == ""
        assert refreshed["resolution_basis_hash"] != old_basis
        assert _node_input_hash(project_id, "extract") != old_hash
        assert row(
            "SELECT id FROM audit_events WHERE project_id=? AND action='resolution_invalidated'",
            (project_id,),
        ) is not None
    finally:
        _cleanup(project_id)


def test_stale_profile_assertion_is_visible_but_excluded_from_calculation_and_citations_work() -> None:
    init_db()
    project_id, _ = _insert_project_and_snapshot(profile_value=900, profile_status="stale")
    try:
        sync_fact_assertions(project_id, [])
        group = fact_groups(project_id)[0]
        assert group["status"] == "insufficient"
        assert group["selected_assertion_id"] is None
        assert group["assertions"][0]["assertion_status"] == "stale"
        assert analysis_fact_rows(project_id) == []

        source_id = analysis_source_rows(project_id)[0]["id"]
        citation_ids = _citations(project_id, [source_id])
        citation = row("SELECT * FROM citations WHERE id=?", (citation_ids[0],))
        assert "1" in citation["label"]
        assert citation["source_id"] == source_id

        graph = build_evidence_graph(project_id)
        assert graph["edge_summary"]
        assert row(
            "SELECT id FROM evidence_edges WHERE project_id=? AND from_type='source' AND from_id=? "
            "AND relation='supports' AND to_type='assertion'",
            (project_id, source_id),
        ) is not None
    finally:
        _cleanup(project_id)


def test_analysis_invalidation_preserves_project_intake_transaction_subjects() -> None:
    init_db()
    project_id, _ = _insert_project_and_snapshot(profile_value=900)
    now = now_iso()
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO project_entities "
                "(id,project_id,entity_type,name,is_borrower,status,attributes_json,created_at,updated_at) "
                "VALUES (?,?,?,?,0,'unreviewed',?,?,?)",
                (
                    f"guarantor-{uuid.uuid4()}", project_id, "guarantor", "Project Guarantor",
                    json_dump({"role": "guarantor", "source": "project_intake"}), now, now,
                ),
            )
            conn.execute(
                "INSERT INTO project_entities "
                "(id,project_id,entity_type,name,is_borrower,status,attributes_json,created_at,updated_at) "
                "VALUES (?,?,?,?,0,'unreviewed',?,?,?)",
                (
                    f"derived-{uuid.uuid4()}", project_id, "related_enterprise", "Derived Entity",
                    json_dump({"relation": "related_party"}), now, now,
                ),
            )

        invalidate_project_analysis(project_id)

        remaining = rows(
            "SELECT entity_type,name,attributes_json FROM project_entities WHERE project_id=?",
            (project_id,),
        )
        assert [(item["entity_type"], item["name"]) for item in remaining] == [
            ("guarantor", "Project Guarantor"),
        ]
        assert json_load(remaining[0]["attributes_json"])["source"] == "project_intake"
    finally:
        _cleanup(project_id)
