"""Review-to-report revision lifecycle for bond due-diligence projects.

Human review updates source-of-truth decisions immediately, but report content
is refreshed explicitly as one bounded, auditable operation.  This prevents a
partially refreshed report from looking current after several independent
decisions have been made.
"""
from __future__ import annotations

from .database import connect, json_dump, json_load, now_iso, row


def report_state(project_id: str) -> dict:
    project = row(
        "SELECT review_revision,report_revision,report_reviewed_revision,report_reviewed_at,"
        "report_stale,report_stale_reasons,analysis_stale FROM projects WHERE id=?",
        (project_id,),
    )
    if not project:
        raise ValueError("项目不存在")
    reasons = json_load(project.get("report_stale_reasons", "[]"))
    report_stale = bool(project.get("report_stale")) or bool(project.get("analysis_stale"))
    has_report = bool(row("SELECT 1 FROM report_sections WHERE project_id=? LIMIT 1", (project_id,)))
    return {
        "review_revision": int(project.get("review_revision") or 0),
        "report_revision": int(project.get("report_revision") or 0),
        "report_reviewed_revision": int(project.get("report_reviewed_revision") or 0),
        "report_reviewed_at": project.get("report_reviewed_at"),
        "report_stale": report_stale,
        "report_stale_reasons": reasons,
        "report_state": (
            "missing" if not has_report
            else "draft_stale" if report_stale
            else "final_ready" if int(project.get("report_reviewed_revision") or 0) == int(project.get("report_revision") or 0)
            and int(project.get("report_revision") or 0) > 0
            else "draft_current"
        ),
    }


def mark_review_changed(project_id: str, reason: str) -> dict:
    """Record a decision that requires deterministic report refresh.

    Reasons are deliberately compact and de-duplicated.  They are presentation
    metadata and also drive the targeted chapter selection in the apply step.
    """
    with connect() as conn:
        project = conn.execute(
            "SELECT review_revision,report_stale_reasons FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        if not project:
            raise ValueError("项目不存在")
        reasons = json_load(project["report_stale_reasons"] or "[]")
        if reason and reason not in reasons:
            reasons.append(reason)
        conn.execute(
            "UPDATE projects SET review_revision=?,report_stale=1,report_stale_reasons=?,updated_at=? WHERE id=?",
            (int(project["review_revision"] or 0) + 1, json_dump(reasons), now_iso(), project_id),
        )
    return report_state(project_id)


def mark_report_current(project_id: str, revision: int) -> dict:
    """Publish a fully validated refresh only if it still matches the decisions."""
    with connect() as conn:
        project = conn.execute("SELECT review_revision FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise ValueError("项目不存在")
        if int(project["review_revision"] or 0) != int(revision):
            return report_state(project_id)
        conn.execute(
            "UPDATE projects SET report_revision=?,report_stale=0,report_stale_reasons='[]',updated_at=? WHERE id=?",
            (revision, now_iso(), project_id),
        )
    return report_state(project_id)


def mark_report_reviewed(project_id: str) -> dict:
    with connect() as conn:
        project = conn.execute(
            "SELECT review_revision,report_revision,report_stale,analysis_stale FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        if not project:
            raise ValueError("项目不存在")
        if project["report_stale"] or project["analysis_stale"] or project["review_revision"] != project["report_revision"]:
            raise ValueError("报告尚未应用最新复核决议")
        conn.execute(
            "UPDATE projects SET report_reviewed_revision=?,report_reviewed_at=?,updated_at=? WHERE id=?",
            (project["report_revision"], now_iso(), now_iso(), project_id),
        )
    return report_state(project_id)
