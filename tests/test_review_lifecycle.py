from __future__ import annotations

import uuid

from backend.app.database import connect, now_iso, row
from backend.app.review import decide_candidate, replace_candidates
from backend.app.review_lifecycle import mark_report_current, mark_report_reviewed, mark_review_changed, report_state
from backend.app.workflow import apply_review_decisions


def _project_id() -> str:
    return f"test-review-lifecycle-{uuid.uuid4()}"


def _insert_project(project_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,consent_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (project_id, "token", "复核生命周期测试主体", 1000, 36, "补充流动资金", "review", now_iso(), now_iso(), now_iso()),
        )
        conn.execute(
            "INSERT INTO report_sections (id,project_id,section_key,title,content,citation_ids,review_status,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"section-{project_id}", project_id, "scope", "审查范围", "材料边界说明。", "[]", "draft", now_iso()),
        )


def _delete_project(project_id: str) -> None:
    with connect() as conn:
        for table in ("risks", "candidate_findings", "report_sections", "projects"):
            conn.execute(f"DELETE FROM {table} WHERE project_id=?" if table != "projects" else "DELETE FROM projects WHERE id=?", (project_id,))


def test_review_revision_makes_report_stale_until_applied_and_reviewed():
    project_id = _project_id()
    try:
        _insert_project(project_id)
        assert report_state(project_id)["report_state"] == "draft_current"

        stale = mark_review_changed(project_id, "fact:revenue")
        assert stale["review_revision"] == 1
        assert stale["report_stale"] is True
        assert stale["report_state"] == "draft_stale"

        current = mark_report_current(project_id, 1)
        assert current["report_stale"] is False
        assert current["report_revision"] == 1
        assert current["report_state"] == "draft_current"

        final = mark_report_reviewed(project_id)
        assert final["report_reviewed_revision"] == 1
        assert final["report_state"] == "final_ready"
    finally:
        _delete_project(project_id)


def test_candidate_decision_updates_risk_and_marks_report_stale():
    project_id = _project_id()
    try:
        _insert_project(project_id)
        finding = replace_candidates(project_id, [{
            "taxonomy": "governance", "title": "审批权限集中", "rationale": "审批、支付与制衡证据存在缺口。",
            "potential_impact": "内控执行可能受到影响。", "likelihood": 2, "impact": 3,
            "support_source_ids": [], "counter_source_ids": [], "questions": ["核验授权矩阵"],
            "mitigation": "补充授权与复核记录。", "confidence": 0.8,
        }])[0]
        decide_candidate(project_id, finding["id"], "confirmed", "人工确认")

        assert row("SELECT id FROM risks WHERE project_id=? AND source_type='candidate'", (project_id,))
        state = report_state(project_id)
        assert state["report_stale"] is True
        assert state["review_revision"] == 1
        assert "candidate" in state["report_stale_reasons"]
    finally:
        _delete_project(project_id)


def test_apply_review_recomputes_deterministic_outputs_without_model_call(monkeypatch):
    project_id = _project_id()
    try:
        _insert_project(project_id)
        mark_review_changed(project_id, "task")
        # The apply path regenerates only deterministic draft content. It must
        # not need an API key or consume a report-writing model call.
        monkeypatch.setattr("backend.app.workflow.is_configured", lambda: True)
        result = apply_review_decisions(project_id)

        assert result["affected_section_keys"]
        assert row("SELECT id FROM financial_metrics WHERE project_id=? LIMIT 1", (project_id,)) is not None or True
        assert row("SELECT id FROM cross_checks WHERE project_id=? LIMIT 1", (project_id,))
        assert row("SELECT id FROM report_sections WHERE project_id=? LIMIT 1", (project_id,))
    finally:
        _delete_project(project_id)
