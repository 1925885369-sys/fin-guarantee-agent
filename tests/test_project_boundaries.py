from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from backend.app.database import connect, now_iso, row
from backend.app.main import app


def _project_payload(**overrides):
    payload = {
        "company_name": "产品边界测试有限公司",
        "amount": 20000,
        "term_months": 36,
        "purpose": "债券发行融资担保完整尽调",
        "bond_name": "产品边界测试债券",
        "data_consent": True,
    }
    payload.update(overrides)
    return payload


def _cleanup(project_id: str) -> None:
    if not project_id:
        return
    with connect() as conn:
        for table in (
            "run_nodes", "runs", "sources", "documents", "project_entities",
            "project_profile_sources", "project_profile_documents", "project_profile_snapshots",
            "project_profile_roles",
        ):
            conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def test_project_create_accepts_only_bond_guarantee_and_defaults_to_bond():
    project_id = ""
    try:
        with TestClient(app) as client:
            rejected = client.post("/api/projects", json=_project_payload(project_type="loan_guarantee"))
            assert rejected.status_code == 422
            created = client.post("/api/projects", json=_project_payload())
            assert created.status_code == 201, created.text
            project_id = created.json()["id"]
            assert created.json()["projectType"] == "bond_guarantee"
            assert row("SELECT project_type FROM projects WHERE id=?", (project_id,))["project_type"] == "bond_guarantee"
    finally:
        _cleanup(project_id)


def test_project_lists_hide_legacy_loan_records_for_every_data_class():
    loan_id = f"legacy-loan-{uuid.uuid4()}"
    now = now_iso()
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO projects "
                "(id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,consent_at,project_type,data_class) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (loan_id, "legacy-token", "历史贷款兼容企业", 1000, 12, "历史迁移读取", "ready", now, now, now,
                 "loan_guarantee", "user_project"),
            )
        with TestClient(app) as client:
            assert loan_id not in {item["id"] for item in client.get("/api/projects").json()}
            assert loan_id not in {item["id"] for item in client.get("/api/projects?data_class=user_project").json()}
            legacy = client.get(f"/api/projects/{loan_id}", headers={"X-Project-Token": "legacy-token"})
            assert legacy.status_code == 200
            assert legacy.json()["project_type"] == "loan_guarantee"
    finally:
        _cleanup(loan_id)


def test_bond_run_and_report_require_both_core_documents(monkeypatch):
    project_id = ""
    started: list[tuple[str, str]] = []

    def fake_start_run(target_project_id: str, policy_profile: str = "") -> str:
        started.append((target_project_id, policy_profile))
        return "mock-run-id"

    monkeypatch.setattr("backend.app.main.start_run", fake_start_run)
    try:
        with TestClient(app) as client:
            created = client.post("/api/projects", json=_project_payload())
            assert created.status_code == 201, created.text
            project_id = created.json()["id"]
            headers = {"X-Project-Token": created.json()["accessToken"]}
            blocked = client.post(
                f"/api/projects/{project_id}/runs", json={"focus": "债券尽调"}, headers=headers,
            )
            assert blocked.status_code == 409
            assert blocked.json()["detail"]["code"] == "core_materials_not_ready"
            assert {item["group"] for item in blocked.json()["detail"]["blocking_items"]} == {
                "募集说明书", "信用评级报告",
            }
            now = now_iso()
            with connect() as conn:
                for document_id, filename, kind, role in (
                    ("boundary-prospectus", "募集说明书.pdf", "募集说明书", "prospectus"),
                    ("boundary-rating", "信用评级报告.pdf", "信用评级报告", "rating_report"),
                ):
                    conn.execute(
                        "INSERT INTO documents "
                        "(id,project_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,warning,created_at,parser,document_role,ocr_status,page_count) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (document_id, project_id, filename, kind, "application/pdf", document_id, 10, "parsed", 1.0,
                         1, "", now, "native", role, "not_required", 1),
                    )
            run = client.post(f"/api/projects/{project_id}/runs", json={"focus": "债券尽调"}, headers=headers)
            report = client.post(
                f"/api/projects/{project_id}/report/generate", json={"focus": "报告生成"}, headers=headers,
            )
            assert run.status_code == 202 and run.json()["id"] == "mock-run-id"
            assert report.status_code == 202 and report.json()["id"] == "mock-run-id"
            assert started == [(project_id, ""), (project_id, "")]
    finally:
        _cleanup(project_id)
