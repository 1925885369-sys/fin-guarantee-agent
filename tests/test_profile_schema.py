from __future__ import annotations

import uuid

from backend.app.database import connect, init_db, json_dump, now_iso, row, rows


def test_profile_contract_v1_schema_and_migration_are_present():
    init_db()
    assert row("SELECT name FROM schema_migrations WHERE version=5")["name"] == "profile_contract_v1"
    expected_tables = {
        "entity_profile_files", "entity_profile_records", "entity_profile_imports",
        "entity_profile_import_files", "project_profile_documents", "project_profile_sources",
    }
    actual_tables = {
        item["name"] for item in rows("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert expected_tables <= actual_tables
    actual_views = {item["name"] for item in rows("SELECT name FROM sqlite_master WHERE type='view'")}
    assert {"analysis_documents", "analysis_sources"} <= actual_views
    assertion_columns = {item["name"] for item in rows("PRAGMA table_info(fact_assertions)")}
    assert {"origin_type", "origin_ref", "profile_snapshot_id", "assertion_status"} <= assertion_columns
    canonical_columns = {item["name"] for item in rows("PRAGMA table_info(canonical_facts)")}
    assert "resolution_basis_hash" in canonical_columns


def test_analysis_views_expose_only_current_profile_snapshot():
    project_id = f"schema-profile-{uuid.uuid4()}"
    snapshot_old = str(uuid.uuid4())
    snapshot_current = str(uuid.uuid4())
    now = now_iso()
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,profile_snapshot_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (project_id, "token", "视图测试企业", 1000, 36, "债券发行", "draft", now, now, snapshot_current),
            )
            for version, snapshot_id in enumerate((snapshot_old, snapshot_current), start=1):
                conn.execute(
                    "INSERT INTO project_profile_snapshots (id,project_id,version,roles_json,facts_json,records_json,sources_json,counts_json,profile_versions_json,content_hash,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (snapshot_id, project_id, version, "[]", "[]", "[]", "[]", "{}", "{}", f"hash-{version}", now),
                )
                document_id = f"profile-document-{version}-{project_id}"
                source_id = f"profile-source-{version}-{project_id}"
                conn.execute(
                    "INSERT INTO project_profile_documents (id,project_id,snapshot_id,profile_id,profile_version,entity_role,origin_document_id,filename,sha256,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (document_id, project_id, snapshot_id, "profile", version, "issuer", "origin", "主体档案.pdf", f"sha-{version}", now),
                )
                conn.execute(
                    "INSERT INTO project_profile_sources (id,project_id,snapshot_id,profile_id,profile_version,entity_role,document_id,origin_source_id,locator,text,content_hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (source_id, project_id, snapshot_id, "profile", version, "issuer", document_id,
                     f"origin-{version}", "第1页", f"版本{version}", f"content-{version}"),
                )
        assert [item["snapshot_id"] for item in rows(
            "SELECT snapshot_id FROM analysis_documents WHERE project_id=?", (project_id,),
        )] == [snapshot_current]
        assert [item["text"] for item in rows(
            "SELECT text FROM analysis_sources WHERE project_id=?", (project_id,),
        )] == ["版本2"]
    finally:
        with connect() as conn:
            conn.execute("DELETE FROM project_profile_sources WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM project_profile_documents WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM project_profile_snapshots WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
