"""一次性清理脚本：删除 user_project / loan_guarantee 项目。

用法：
    python scripts/cleanup_loan_projects.py backup   # 仅备份
    python scripts/cleanup_loan_projects.py apply    # 备份 + 事务删除 + 清理文件
    python scripts/cleanup_loan_projects.py verify   # 只读校验

设计要点：
- 目标集合 = data_class='user_project' AND project_type='loan_guarantee'。
- 执行前重新计数并检查活动任务（runs.status in queued/running）；数量不是 110 或存在活动任务则中止。
- 所有删除在一个事务内完成，任一失败整体回滚。
- 文件删除前用 resolve() 校验路径位于当前数据目录内。
- 不修改数据库结构，不新增 API。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = (ROOT / ".data").resolve()
DB_PATH = DATA_DIR / "agent.db"
UPLOADS_DIR = DATA_DIR / "uploads"
RETRIEVAL_DIR = DATA_DIR / "retrieval"

EXPECTED_COUNT = 110

# 含 project_id 列、需要按目标项目清理的表（来自 PRAGMA table_info 全量扫描）。
PROJECT_ID_TABLES = [
    "audit_events", "bond_tranches", "candidate_findings", "canonical_facts", "citations",
    "cross_checks", "document_sections", "document_table_history", "document_tables", "documents",
    "evidence_edges", "fact_assertions", "facts", "financial_metrics", "financial_tie_outs",
    "gold_annotations", "llm_attempts", "policy_snapshots", "project_entities",
    "project_profile_documents", "project_profile_roles", "project_profile_snapshots",
    "project_profile_sources", "report_claims", "report_prompt_overrides", "report_sections",
    "report_table_snapshots", "retrieval_chunks", "retrieval_indexes", "review_tasks", "risks",
    "run_artifacts", "run_nodes", "runs", "sources", "table_cell_reviews",
]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def target_ids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT id FROM projects WHERE data_class='user_project' AND project_type='loan_guarantee' ORDER BY id"
    )]


def active_runs(conn: sqlite3.Connection, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    return [dict(r) for r in conn.execute(
        f"SELECT id,project_id,status,current_node FROM runs WHERE project_id IN ({ph}) "
        "AND status IN ('queued','running')", ids
    )]


def guard(conn: sqlite3.Connection, ids: list[str]) -> None:
    if len(ids) != EXPECTED_COUNT:
        raise SystemExit(f"目标数量异常：期望 {EXPECTED_COUNT}，实际 {len(ids)}，已停止。")
    active = active_runs(conn, ids)
    if active:
        raise SystemExit(f"存在活动任务 {len(active)} 条，已停止：{active[:5]}")
    print(f"[guard] 目标数量 {len(ids)}，无活动任务，继续。")


def _safe_under(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise SystemExit(f"路径越界，拒绝操作：{resolved}")
    return resolved


def do_backup() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = _safe_under(DATA_DIR / "backups" / f"loan-project-cleanup-{stamp}", DATA_DIR / "backups")
    backup_root.mkdir(parents=True, exist_ok=False)

    conn = connect()
    ids = target_ids(conn)
    guard(conn, ids)
    projects = [dict(r) for r in conn.execute(
        "SELECT id,company_name,status,created_at,access_token FROM projects WHERE data_class='user_project' "
        "AND project_type='loan_guarantee' ORDER BY id"
    )]

    # 1) 数据库：用 SQLite 备份 API 落一份一致快照（WAL 模式下也安全）。
    dst = sqlite3.connect(str(backup_root / "agent.db"))
    try:
        conn.backup(dst)
        dst.commit()
    finally:
        dst.close()
    print(f"[backup] 数据库 -> {backup_root / 'agent.db'}")

    # 2) 目标项目上传文件
    upload_dirs = [UPLOADS_DIR / pid for pid in ids if (UPLOADS_DIR / pid).is_dir()]
    backup_uploads = backup_root / "uploads"
    backup_uploads.mkdir(parents=True, exist_ok=True)
    file_count = 0
    for directory in upload_dirs:
        _safe_under(directory, UPLOADS_DIR)
        target = backup_uploads / directory.name
        shutil.copytree(directory, target)
        file_count += sum(1 for _ in target.rglob("*") if _.is_file())
    print(f"[backup] 上传目录 {len(upload_dirs)} 个，文件 {file_count} 个 -> {backup_uploads}")

    # 3) 检索文件（如有）
    retrieval_dirs = []
    import hashlib
    for pid in ids:
        key32 = hashlib.sha256(pid.encode("utf-8")).hexdigest()[:32]
        key64 = hashlib.sha256(pid.encode("utf-8")).hexdigest()
        for key in (key32, key64):
            candidate = RETRIEVAL_DIR / key
            if candidate.is_dir():
                retrieval_dirs.append(candidate)
    backup_retrieval = backup_root / "retrieval"
    if retrieval_dirs:
        backup_retrieval.mkdir(parents=True, exist_ok=True)
        for directory in retrieval_dirs:
            _safe_under(directory, RETRIEVAL_DIR)
            shutil.copytree(directory, backup_retrieval / directory.name)
    print(f"[backup] 检索目录 {len(retrieval_dirs)} 个")

    # 4) manifest
    row_counts = {t: conn.execute(
        f"SELECT COUNT(*) FROM {t} WHERE project_id IN ({','.join('?' * len(ids))})", ids
    ).fetchone()[0] for t in PROJECT_ID_TABLES}
    manifest = {
        "created_at": datetime.now().isoformat(),
        "expected_count": EXPECTED_COUNT,
        "target_count": len(ids),
        "target_ids": ids,
        "projects": projects,
        "upload_dirs": sorted(d.name for d in upload_dirs),
        "upload_file_count": file_count,
        "retrieval_dirs": sorted(d.name for d in retrieval_dirs),
        "project_id_row_counts": row_counts,
        "project_id_rows_total": sum(row_counts.values()),
    }
    (backup_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[backup] manifest -> {backup_root / 'manifest.json'}")

    # 5) 校验备份可读取
    with sqlite3.connect(str(backup_root / "agent.db")) as check:
        n = check.execute(
            "SELECT COUNT(*) FROM projects WHERE data_class='user_project' AND project_type='loan_guarantee'"
        ).fetchone()[0]
    assert n == EXPECTED_COUNT, f"备份库目标数量异常：{n}"
    print(f"[backup] 校验通过：备份库含 {n} 个目标项目。")
    conn.close()
    return backup_root


def do_apply(reuse_backup: Path | None = None) -> None:
    backup_root = reuse_backup if reuse_backup is not None else do_backup()

    conn = connect()
    ids = target_ids(conn)
    guard(conn, ids)
    ph = ",".join("?" * len(ids))
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in PROJECT_ID_TABLES:
            cur = conn.execute(f"DELETE FROM {table} WHERE project_id IN ({ph})", ids)
            if cur.rowcount:
                print(f"  - {table}: 删除 {cur.rowcount} 行")
        cur = conn.execute(f"DELETE FROM projects WHERE id IN ({ph})", ids)
        print(f"  - projects: 删除 {cur.rowcount} 行")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.close()
        raise
    conn.close()
    print(f"[apply] 数据库事务已提交。备份位于 {backup_root}")

    # 文件清理
    removed = 0
    for pid in ids:
        directory = UPLOADS_DIR / pid
        if directory.is_dir():
            _safe_under(directory, UPLOADS_DIR)
            shutil.rmtree(directory)
            removed += 1
    print(f"[apply] 删除上传目录 {removed} 个")

    import hashlib
    r_removed = 0
    for pid in ids:
        key32 = hashlib.sha256(pid.encode("utf-8")).hexdigest()[:32]
        key64 = hashlib.sha256(pid.encode("utf-8")).hexdigest()
        for key in (key32, key64):
            candidate = RETRIEVAL_DIR / key
            if candidate.is_dir():
                _safe_under(candidate, RETRIEVAL_DIR)
                shutil.rmtree(candidate)
                r_removed += 1
    print(f"[apply] 删除检索目录 {r_removed} 个")


def _ids_from_manifest(backup_dir: Path) -> list[str]:
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    return list(manifest["target_ids"])


def do_finalize(backup_dir: Path) -> None:
    """当目标项目已被删除（目标数量为 0）时，按备份清单收尾。

    以备份时冻结的目标 ID 为准，在一个事务内清扫任何残留行，并清理残留文件，
    使最终状态与「事务删除 + 文件清理」一致。
    """
    ids = _ids_from_manifest(backup_dir)
    ph = ",".join("?" * len(ids))
    conn = connect()
    alive = conn.execute(f"SELECT COUNT(*) FROM projects WHERE id IN ({ph})", ids).fetchone()[0]
    if alive:
        conn.close()
        raise SystemExit(f"备份清单中的 {alive} 个项目仍存在，请改用具事务语义的 apply 模式。")
    residual = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE project_id IN ({ph})", ids).fetchone()[0]
                for t in PROJECT_ID_TABLES}
    residual = {t: n for t, n in residual.items() if n}
    print(f"[finalize] 备份清单 {len(ids)} 个项目；执行前残留：{residual or 'none'}")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in PROJECT_ID_TABLES:
            cur = conn.execute(f"DELETE FROM {table} WHERE project_id IN ({ph})", ids)
            if cur.rowcount:
                print(f"  - {table}: 清扫 {cur.rowcount} 行")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.close()
        raise
    conn.close()
    print("[finalize] 残留清扫事务已提交。")

    removed = 0
    for pid in ids:
        directory = UPLOADS_DIR / pid
        if directory.is_dir():
            _safe_under(directory, UPLOADS_DIR)
            shutil.rmtree(directory)
            removed += 1
    print(f"[finalize] 删除残留上传目录 {removed} 个")

    import hashlib
    r_removed = 0
    for pid in ids:
        key32 = hashlib.sha256(pid.encode("utf-8")).hexdigest()[:32]
        key64 = hashlib.sha256(pid.encode("utf-8")).hexdigest()
        for key in (key32, key64):
            candidate = RETRIEVAL_DIR / key
            if candidate.is_dir():
                _safe_under(candidate, RETRIEVAL_DIR)
                shutil.rmtree(candidate)
                r_removed += 1
    print(f"[finalize] 删除残留检索目录 {r_removed} 个")
    do_verify(ids)


def do_verify(ids: list[str] | None = None) -> None:
    conn = connect()
    q = lambda sql, *p: conn.execute(sql, p).fetchone()[0]
    print("target loan user projects:", q("SELECT COUNT(*) FROM projects WHERE data_class='user_project' AND project_type='loan_guarantee'"))
    print("bond user projects:      ", q("SELECT COUNT(*) FROM projects WHERE data_class='user_project' AND project_type='bond_guarantee'"))
    print("fictional_demo projects: ", q("SELECT COUNT(*) FROM projects WHERE data_class='fictional_demo'"))
    print("real_reference projects: ", q("SELECT COUNT(*) FROM projects WHERE data_class='real_reference'"))
    print("entity_profiles:         ", q("SELECT COUNT(*) FROM entity_profiles"))
    print("total projects:          ", q("SELECT COUNT(*) FROM projects"))
    if ids is None:
        ids = target_ids(conn)
    print("checked target ids:      ", len(ids))
    residual = {}
    if ids:
        ph = ",".join("?" * len(ids))
        for t in PROJECT_ID_TABLES:
            n = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE project_id IN ({ph})", ids).fetchone()[0]
            if n:
                residual[t] = n
    print("residual rows in project_id tables:", residual or "none")
    conn.close()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if mode == "backup":
        do_backup()
    elif mode == "apply":
        reuse = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
        do_apply(reuse)
    elif mode == "finalize":
        do_finalize(Path(sys.argv[2]).resolve())
    elif mode == "verify":
        ids = _ids_from_manifest(Path(sys.argv[2]).resolve()) if len(sys.argv) > 2 else None
        do_verify(ids)
    else:
        raise SystemExit(f"未知模式：{mode}")
