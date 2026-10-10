from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient

from backend.app.database import connect, init_db, json_dump, now_iso, row, rows
from backend.app.facts import material_inventory
from backend.app.profile_evidence import (
    analysis_source_rows,
    materialize_snapshot_evidence,
    profile_assertion_rows,
    profile_evidence_status,
)
from backend.app.main import app


def _project_with_snapshot(*, value: float = 1000, source_locator: str = "Sheet1!B2") -> tuple[str, str]:
    project_id = f"profile-evidence-{uuid.uuid4()}"
    snapshot_id = f"snapshot-{uuid.uuid4()}"
    profile_id = f"profile-{uuid.uuid4()}"
    created_at = now_iso()
    roles = [{
        "role": "issuer", "profile_id": profile_id, "name": "Profile Evidence Issuer",
        "profile_version": 1, "status": "ready", "completeness": 1.0,
    }]
    sources = [{
        "id": "origin-source-assets", "profile_id": profile_id, "role": "issuer",
        "filename": "issuer-financial.xlsx", "kind": "financial_statement",
        "locator_type": "cell", "locator": source_locator, "page": None,
        "quote": f"total assets {value}",
    }]
    facts = [{
        "id": "origin-fact-assets", "profile_id": profile_id, "role": "issuer",
        "fact_key": "total_assets", "period": "2025", "scope": "consolidated",
        "value": value, "unit": "CNY10k", "status": "confirmed",
        "source_ids": ["origin-source-assets"],
    }]
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,consent_at,project_type,profile_snapshot_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, "token", "Profile Evidence Issuer", 20000, 36, "bond", "draft", created_at,
             created_at, created_at, "bond_guarantee", snapshot_id),
        )
        conn.execute(
            "INSERT INTO project_profile_snapshots "
            "(id,project_id,version,baseline_date,roles_json,facts_json,sources_json,counts_json,profile_versions_json,content_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, project_id, 1, "2025-12-31", json_dump(roles), json_dump(facts),
             json_dump(sources), json_dump({"confirmed": 1}), json_dump({profile_id: 1}),
             f"hash-{snapshot_id}", created_at),
        )
    return project_id, snapshot_id


def _cleanup(project_id: str) -> None:
    with connect() as conn:
        for table in (
            "citations", "report_claims", "evidence_edges", "canonical_facts", "fact_assertions",
            "facts", "project_profile_sources", "project_profile_documents", "project_profile_snapshots",
            "project_entities", "documents", "sources",
        ):
            conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def test_snapshot_evidence_projection_is_idempotent_and_not_material_inventory() -> None:
    init_db()
    project_id, snapshot_id = _project_with_snapshot()
    try:
        first = materialize_snapshot_evidence(project_id)
        first_ids = [item["id"] for item in analysis_source_rows(project_id)]
        second = materialize_snapshot_evidence(project_id)
        assert first == second
        assert first["status"] == "ready"
        assert len(first_ids) == 1
        assert [item["id"] for item in analysis_source_rows(project_id)] == first_ids
        assert row(
            "SELECT COUNT(*) count FROM project_profile_sources WHERE project_id=? AND snapshot_id=?",
            (project_id, snapshot_id),
        )["count"] == 1

        assertion = profile_assertion_rows(project_id)[0]
        assert assertion["origin_type"] == "profile_snapshot"
        assert assertion["profile_snapshot_id"] == snapshot_id
        assert assertion["source_ids"] == first_ids
        assert profile_evidence_status(project_id)["source_count"] == 1

        inventory = material_inventory(project_id)
        assert inventory["documents"] == []
        assert inventory["readiness"] == "empty"
    finally:
        _cleanup(project_id)


def test_analysis_views_expose_only_current_snapshot_and_unlocated_facts_are_not_citable() -> None:
    init_db()
    project_id, first_snapshot = _project_with_snapshot(value=1000)
    try:
        materialize_snapshot_evidence(project_id)
        first_source_id = analysis_source_rows(project_id)[0]["id"]
        second_snapshot = f"snapshot-{uuid.uuid4()}"
        current = row("SELECT * FROM project_profile_snapshots WHERE id=?", (first_snapshot,))
        profile_id = next(iter(json.loads(current["profile_versions_json"])))
        facts = json_dump([{
            "id": "unlocated-fact", "profile_id": profile_id,
            "role": "issuer", "fact_key": "total_assets", "period": "2025", "scope": "consolidated",
            "value": 1200, "unit": "CNY10k", "status": "confirmed", "source_ids": ["unlocated-source"],
        }])
        sources = json_dump([{
            "id": "unlocated-source", "profile_id": profile_id,
            "role": "issuer", "filename": "unlocated.xlsx", "kind": "financial_statement",
            "locator": "", "quote": "total assets 1200",
        }])
        with connect() as conn:
            conn.execute(
                "INSERT INTO project_profile_snapshots "
                "(id,project_id,version,baseline_date,roles_json,facts_json,sources_json,counts_json,profile_versions_json,content_hash,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (second_snapshot, project_id, 2, current["baseline_date"], current["roles_json"], facts, sources,
                 current["counts_json"], current["profile_versions_json"], f"hash-{second_snapshot}", now_iso()),
            )
            conn.execute("UPDATE projects SET profile_snapshot_id=? WHERE id=?", (second_snapshot, project_id))
        status = materialize_snapshot_evidence(project_id)
        assert status["status"] == "insufficient"
        assert analysis_source_rows(project_id) == []
        assert profile_assertion_rows(project_id) == []
        assert row("SELECT id FROM project_profile_sources WHERE id=?", (first_source_id,)) is not None
        assert rows("SELECT id FROM analysis_sources WHERE project_id=?", (project_id,)) == []
    finally:
        _cleanup(project_id)


def test_legacy_guarantor_profile_data_does_not_enter_issuer_analysis() -> None:
    init_db()
    project_id, snapshot_id = _project_with_snapshot(value=1000)
    try:
        snapshot = row("SELECT * FROM project_profile_snapshots WHERE id=?", (snapshot_id,))
        roles = json.loads(snapshot["roles_json"])
        facts = json.loads(snapshot["facts_json"])
        sources = json.loads(snapshot["sources_json"])
        roles.append({
            "role": "guarantor", "profile_id": "legacy-guarantor", "name": "Legacy Guarantor",
            "profile_version": 1, "status": "ready", "completeness": 1.0,
        })
        sources.append({
            "id": "guarantor-source", "profile_id": "legacy-guarantor", "role": "guarantor",
            "filename": "guarantor.xlsx", "kind": "financial_statement", "locator": "B2",
            "quote": "total assets 9999",
        })
        facts.append({
            "id": "guarantor-assets", "profile_id": "legacy-guarantor", "role": "guarantor",
            "fact_key": "total_assets", "period": "2025", "scope": "consolidated", "value": 9999,
            "unit": "CNY10k", "status": "confirmed", "source_ids": ["guarantor-source"],
        })
        with connect() as conn:
            conn.execute(
                "UPDATE project_profile_snapshots SET roles_json=?,facts_json=?,sources_json=? WHERE id=?",
                (json_dump(roles), json_dump(facts), json_dump(sources), snapshot_id),
            )
        materialize_snapshot_evidence(project_id)
        projected = analysis_source_rows(project_id)
        assertions = profile_assertion_rows(project_id)
        assert len(projected) == 1 and projected[0]["entity_role"] == "issuer"
        assert len(assertions) == 1 and assertions[0]["subject_role"] == "issuer"
        assert assertions[0]["value"] == 1000
    finally:
        _cleanup(project_id)


def test_citation_api_exposes_profile_snapshot_provenance() -> None:
    init_db()
    project_id, snapshot_id = _project_with_snapshot()
    try:
        materialize_snapshot_evidence(project_id)
        projected = analysis_source_rows(project_id)[0]
        with TestClient(app) as client:
            response = client.get(
                f"/api/citations/c-{projected['id']}", headers={"X-Project-Token": "token"},
            )
        assert response.status_code == 200, response.text
        citation = response.json()
        assert citation["origin_type"] == "profile_snapshot"
        assert citation["snapshot_id"] == snapshot_id
        assert citation["profile_id"] == projected["profile_id"]
        assert citation["profile_version"] == 1
        assert citation["entity_role"] == "issuer"
        assert citation["label"] == "issuer-financial.xlsx"
        assert citation["locator"] == "Sheet1!B2"
        assert citation["quote"] == "total assets 1000"
        assert citation["bbox"] == []
    finally:
        _cleanup(project_id)


def test_explicit_registered_record_fields_become_assertions_without_inference() -> None:
    init_db()
    project_id, snapshot_id = _project_with_snapshot()
    try:
        snapshot = row("SELECT records_json FROM project_profile_snapshots WHERE id=?", (snapshot_id,))
        assert snapshot is not None
        records_payload = [{
            "id": "record-debt-summary",
            "record_type": "debt_instruments",
            "profile_id": row(
                "SELECT json_extract(roles_json,'$[0].profile_id') profile_id "
                "FROM project_profile_snapshots WHERE id=?", (snapshot_id,),
            )["profile_id"],
            "role": "issuer",
            "period": "2025-12-31",
            "scope": "consolidated",
            "status": "confirmed",
            "source_ids": ["origin-source-assets"],
            "unit": "CNY10k",
            "data": {
                "interest_bearing_debt": 600,
                "ordinary_balance": 999,
            },
        }]
        with connect() as conn:
            conn.execute(
                "UPDATE project_profile_snapshots SET records_json=? WHERE id=?",
                (json_dump(records_payload), snapshot_id),
            )

        assertions = profile_assertion_rows(project_id)
        record_assertions = [item for item in assertions if item["fact_key"] == "interest_bearing_debt"]
        assert len(record_assertions) == 1
        assert record_assertions[0]["value"] == 600
        assert record_assertions[0]["source_ids"]
        assert all(item["fact_key"] != "ordinary_balance" for item in assertions)
    finally:
        _cleanup(project_id)


def test_incomplete_frozen_profile_prevents_full_and_core_report_readiness() -> None:
    init_db()
    project_id, _ = _project_with_snapshot()
    document_rows = [
        ("prospectus", "募集说明书", "prospectus"),
        ("rating", "信用评级报告", "rating_report"),
    ]
    try:
        with connect() as conn:
            for suffix, kind, role in document_rows:
                document_id = f"{project_id}-{suffix}"
                conn.execute(
                    "INSERT INTO documents "
                    "(id,project_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,created_at,document_role) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (document_id, project_id, f"{kind}.pdf", kind, "application/pdf", document_id,
                     100, "parsed", 1.0, 0, now_iso(), role),
                )

        inventory = material_inventory(project_id)
        assert inventory["core_materials_ready"] is True
        assert inventory["profile_snapshot"]["ready_for_agent"] is False
        assert inventory["core_pre_review_ready"] is False
        assert inventory["complete_due_diligence_ready"] is False
        assert inventory["analysis_mode"] == "insufficient"
    finally:
        _cleanup(project_id)
