from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from backend.app.database import connect, json_dump, now_iso, row
from backend.app.entity_profiles import get_project_snapshot
from backend.app.main import app


def _cleanup(project_id: str = "", profile_id: str = "") -> None:
    with connect() as conn:
        if project_id:
            for table in (
                "project_profile_sources", "project_profile_documents", "project_profile_snapshots",
                "project_profile_roles", "review_tasks", "candidate_findings", "project_entities",
                "canonical_facts", "documents", "sources", "projects",
            ):
                conn.execute(f"DELETE FROM {table} WHERE {'id' if table == 'projects' else 'project_id'}=?", (project_id,))
        if profile_id:
            for table in (
                "entity_profile_records", "entity_profile_facts", "entity_profile_sources",
                "entity_profile_files", "entity_profile_versions",
            ):
                conn.execute(f"DELETE FROM {table} WHERE profile_id=?", (profile_id,))
            conn.execute("DELETE FROM entity_profiles WHERE id=?", (profile_id,))


def _profile_payload(code: str) -> dict:
    return {
        "name": "测试发行人有限公司",
        "unified_code": code,
        "baseline_date": "2025-12-31",
        "latest_financial_period": "2025",
        "status": "ready",
        "completeness": 0.86,
        "sources": [{
            "id": "source-original", "filename": "主体档案.pdf", "kind": "主体档案",
            "locator": "第1页", "page": 1, "quote": "资产总额为1000万元。",
        }],
        "facts": [{
            "fact_key": "total_assets", "category": "财务", "value": 1000, "unit": "万元",
            "period": "2025", "scope": "consolidated", "status": "confirmed",
            "source_ids": ["source-original"],
        }],
        "data": {"industry": "制造业"},
    }


def test_seeded_demo_profile_exposes_identity_and_three_year_financials():
    with TestClient(app) as client:
        response = client.get("/api/entity-profiles/profile-demo-huachen-manufacturing")
        assert response.status_code == 200
        detail = response.json()
        facts = detail["facts"]
        by_key = {}
        for fact in facts:
            by_key.setdefault(fact["fact_key"], []).append(fact)
        assert detail["name"] == "华辰精密制造有限公司"
        assert by_key["legal_representative"][0]["text_value"] == "陈嘉诚"
        assert by_key["registered_address"][0]["text_value"] == "上海市浦东新区金海路1888号"
        three_year_keys = (
            "cash", "restricted_cash", "accounts_receivable", "other_receivables", "inventory",
            "current_assets", "total_assets", "short_term_debt", "debt_due_12m",
            "current_liabilities", "long_term_debt", "bonds_payable", "interest_bearing_debt",
            "total_liabilities", "equity", "revenue", "cost_of_goods_sold", "finance_expense",
            "interest_expense", "net_profit", "cash_received_from_sales", "operating_cash_flow",
            "investing_cash_flow", "financing_cash_flow",
        )
        for fact_key in three_year_keys:
            annual = {item["period"]: item for item in by_key[fact_key] if item["period"] in {"2023", "2024", "2025"}}
            assert set(annual) == {"2023", "2024", "2025"}
            assert all(item["source_ids"] for item in annual.values())
        assert not any("Q" in str(item.get("period") or "") for item in facts)
        for period in ("2023", "2024", "2025"):
            assets = next(item["value"] for item in by_key["total_assets"] if item["period"] == period)
            liabilities = next(item["value"] for item in by_key["total_liabilities"] if item["period"] == period)
            equity = next(item["value"] for item in by_key["equity"] if item["period"] == period)
            assert assets == liabilities + equity


def test_profile_version_and_project_snapshot_are_immutable():
    suffix = uuid.uuid4().hex[:8].upper()
    profile_id = ""
    project_id = ""
    with TestClient(app) as client:
        created_profile = client.post("/api/entity-profiles", json=_profile_payload(f"91310000TEST{suffix}"))
        assert created_profile.status_code == 201
        profile_id = created_profile.json()["id"]
        assert created_profile.json()["completeness"] < 0.1
        assert created_profile.json()["status"] == "incomplete"
        assert created_profile.json()["facts"][0]["name"]
        assert created_profile.json()["facts"][0]["source_label"] == "主体档案.pdf"
        assert created_profile.json()["facts"][0]["page"] == 1
        searched = client.get("/api/entity-profiles", params={"search": suffix, "status": "incomplete"})
        assert searched.status_code == 200
        assert [item["id"] for item in searched.json()] == [profile_id]
        assert client.get("/api/entity-profiles", params={"status": "unknown"}).status_code == 400

        created_project = client.post("/api/projects", json={
            "company_name": "该名称应被主体档案覆盖", "amount": 20000, "term_months": 36,
            "purpose": "偿还有息债务", "bond_name": "测试公司债券", "data_consent": True,
            "project_type": "bond_guarantee", "issuer_profile_id": profile_id,
            "guarantor_profile_id": "profile-demo-guarantor", "counter_guarantor_profile_ids": [],
            "counter_guarantee_status": "not_applicable", "analysis_target": "full_due_diligence",
        })
        assert created_project.status_code == 201
        project_id = created_project.json()["id"]
        headers = {"X-Project-Token": created_project.json()["accessToken"]}
        project = client.get(f"/api/projects/{project_id}", headers=headers).json()
        assert project["company_name"] == "测试发行人有限公司"
        assert project["profile_snapshot_id"]
        blocked_export = client.get(f"/api/projects/{project_id}/export?format=docx", headers=headers)
        assert blocked_export.status_code == 409
        assert "暂不能导出 Word" in blocked_export.json()["detail"]

        snapshot_v1 = client.get(f"/api/projects/{project_id}/profile-snapshot", headers=headers).json()
        assert snapshot_v1["version"] == 1
        assert snapshot_v1["facts"][0]["value"] == 1000
        source_id = created_profile.json()["facts"][0]["source_ids"][0]
        patched = client.patch(f"/api/entity-profiles/{profile_id}", json={
            "baseline_date": "2026-03-31", "facts": [{
                "fact_key": "total_assets", "category": "财务", "value": 1200, "unit": "万元",
                "period": "2025", "scope": "consolidated", "status": "confirmed", "source_ids": [source_id],
            }],
        })
        assert patched.status_code == 200 and patched.json()["version"] == 2
        versions = client.get(f"/api/entity-profiles/{profile_id}/versions")
        assert versions.status_code == 200 and [item["version"] for item in versions.json()] == [2, 1]
        assert versions.json()[0]["current"] is True
        assert versions.json()[0]["fact_count"] == 1
        assert versions.json()[0]["source_count"] == 1
        assert versions.json()[0]["change_summary"]
        assert versions.json()[1]["current"] is False
        version_detail = client.get(f"/api/entity-profiles/{profile_id}/versions/2")
        assert version_detail.status_code == 200 and version_detail.json()["diff"]["compare_to"] == 1
        assert client.get(f"/api/projects/{project_id}/profile-snapshot", headers=headers).json()["facts"][0]["value"] == 1000
        before_refresh = client.get(f"/api/projects/{project_id}/profile-snapshot/status", headers=headers)
        assert before_refresh.json()["has_newer_version"] is True
        assert before_refresh.json()["refresh_required"] is True

        refreshed = client.post(f"/api/projects/{project_id}/profile-snapshot/refresh", headers=headers)
        assert refreshed.status_code == 200
        assert refreshed.json()["version"] == 2
        assert refreshed.json()["facts"][0]["value"] == 1200
        snapshot_status = client.get(f"/api/projects/{project_id}/profile-snapshot/status", headers=headers)
        assert snapshot_status.status_code == 200
        assert snapshot_status.json()["current_profile_version"] == 2
        assert snapshot_status.json()["evidence_status"] == "ready"
        assert snapshot_status.json()["evidence_count"] >= 1
        assert client.get(f"/api/projects/{project_id}", headers=headers).json()["analysis_stale"] is True
        stale_export = client.get(f"/api/projects/{project_id}/export?format=docx", headers=headers)
        assert stale_export.status_code == 409
        assert "Word" in stale_export.json()["detail"]

    _cleanup(project_id, profile_id)


def test_review_summary_counts_unique_root_issues_only():
    project_id = f"test-review-summary-{uuid.uuid4()}"
    now = now_iso()
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,consent_at,project_type) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, "token", "汇总测试企业", 1000, 36, "债券发行", "review", now, now, now, "bond_guarantee"),
            )
            conn.execute(
                "INSERT INTO canonical_facts (id,project_id,fact_key,subject,subject_id,scope,period,period_type,category,name,status,assertion_ids,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("conflict-1", project_id, "revenue", "汇总测试企业", "issuer", "consolidated", "2025", "duration", "财务", "营业收入", "conflicted", "[]", now),
            )
            conn.execute(
                "INSERT INTO candidate_findings (id,project_id,taxonomy,title,rationale,potential_impact,likelihood,impact,severity,support_source_ids,counter_source_ids,questions,mitigation,status,confidence,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("candidate-1", project_id, "governance", "治理风险", "原因", "影响", 2, 2, "medium", "[]", "[]", "[]", "措施", "proposed", 0.8, now, now),
            )
            task_rows = [
                ("legacy-conflict", "fact_conflict", "冲突重复项", "conflict:conflict-1"),
                ("legacy-candidate", "candidate_finding", "候选重复项", "candidate:candidate-1"),
                ("package-1", "material_gap", "补充债务材料", "material:debt"),
                ("package-duplicate", "insufficient_rule", "补充债务依据", "material:debt"),
            ]
            for task_id, task_type, title, issue_key in task_rows:
                conn.execute(
                    "INSERT INTO review_tasks (id,project_id,task_type,priority,title,description,status,related_ids,required_evidence,created_at,updated_at,issue_key,issue_category,affected_fact_ids,affected_rule_ids,blocking_level,source_requirement) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, project_id, task_type, "medium", title, title, "open", "[]", "", now, now,
                     issue_key, "test", "[]", "[]", "non_blocking", ""),
                )
        with TestClient(app) as client:
            response = client.get(
                f"/api/projects/{project_id}/review-summary", headers={"X-Project-Token": "token"},
            )
            assert response.status_code == 200
            payload = response.json()
            assert {key: payload[key] for key in (
                "total_unique_issues", "conflict_count", "candidate_count", "action_package_count", "information_notice_count",
            )} == {
                "total_unique_issues": 3,
                "conflict_count": 1,
                "candidate_count": 1,
                "action_package_count": 1,
                "information_notice_count": 0,
            }
            assert payload["report_state"] == "missing"
            assert payload["report_stale"] is False
    finally:
        _cleanup(project_id)


def test_legacy_project_payload_and_snapshot_authorization_remain_compatible():
    project_id = ""
    with TestClient(app) as client:
        created = client.post("/api/projects", json={
            "company_name": "旧请求兼容企业", "amount": 500, "term_months": 12,
            "purpose": "补充流动资金", "data_consent": True,
        })
        assert created.status_code == 201
        project_id = created.json()["id"]
        assert client.get(f"/api/projects/{project_id}/profile-snapshot").status_code == 403
        response = client.get(
            f"/api/projects/{project_id}/profile-snapshot",
            headers={"X-Project-Token": created.json()["accessToken"]},
        )
        assert response.status_code == 404
    _cleanup(project_id)
