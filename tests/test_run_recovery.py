from __future__ import annotations

import uuid

from backend.app.database import connect, now_iso, recover_interrupted_runs, row, rows


def test_recover_interrupted_runs_preserves_history_and_releases_project():
    # Normalize residue left by previous process terminations before creating
    # the isolated rows whose exact recovery count is asserted below.
    recover_interrupted_runs()
    project_id = f"test-recovery-{uuid.uuid4()}"
    active_run_ids = [f"test-run-{uuid.uuid4()}", f"test-run-{uuid.uuid4()}"]
    completed_run_id = f"test-run-{uuid.uuid4()}"
    now = now_iso()
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO projects "
                "(id,access_token,company_name,amount,term_months,purpose,status,analysis_stale,created_at,updated_at,consent_at,project_type) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, "token", "恢复测试企业", 1000, 36, "债券发行", "running", 0, now, now, now, "bond_guarantee"),
            )
            for index, (run_id, status) in enumerate(zip(active_run_ids, ("queued", "running"))):
                conn.execute(
                    "INSERT INTO runs "
                    "(id,project_id,status,current_node,progress,message,cancel_requested,started_at,error) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (run_id, project_id, status, "extract", index * 20, "运行中", 0, now, ""),
                )
                conn.execute(
                    "INSERT INTO run_nodes "
                    "(id,run_id,project_id,node_key,status,progress,attempt,started_at,error) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"node-{run_id}", run_id, project_id, "extract", status, index * 20, 1, now, ""),
                )
            conn.execute(
                "INSERT INTO runs "
                "(id,project_id,status,current_node,progress,message,cancel_requested,started_at,finished_at,error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (completed_run_id, project_id, "completed", "review", 100, "已完成", 0, now, now, ""),
            )

        assert recover_interrupted_runs() == 2
        recovered = rows(
            "SELECT id,status,message,error,finished_at FROM runs WHERE id IN (?,?) ORDER BY id",
            active_run_ids,
        )
        assert len(recovered) == 2
        assert all(item["status"] == "failed" for item in recovered)
        assert all("服务重启" in item["error"] and item["finished_at"] for item in recovered)
        recovered_nodes = rows(
            "SELECT status,error,finished_at FROM run_nodes WHERE run_id IN (?,?) ORDER BY run_id",
            active_run_ids,
        )
        assert all(item["status"] == "failed" for item in recovered_nodes)
        assert all("服务重启" in item["error"] and item["finished_at"] for item in recovered_nodes)
        assert row("SELECT status,finished_at FROM runs WHERE id=?", (completed_run_id,))["status"] == "completed"
        project = row("SELECT status,analysis_stale FROM projects WHERE id=?", (project_id,))
        assert project == {"status": "failed", "analysis_stale": 1}
        assert recover_interrupted_runs() == 0
    finally:
        with connect() as conn:
            conn.execute("DELETE FROM run_nodes WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM runs WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
