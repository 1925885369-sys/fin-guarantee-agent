from __future__ import annotations

import io
import shutil
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from .config import settings
from .database import connect, now_iso, row, rows


def check_rate(client_ip: str, action: str, limit: int, window_seconds: int = 86400) -> None:
    if not settings.public_demo:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
    count = row("SELECT COUNT(*) count FROM rate_events WHERE client_ip=? AND action=? AND created_at>=?",
                (client_ip, action, cutoff))["count"]
    if count >= limit:
        labels = {"project": "创建项目", "run": "运行分析", "upload": "上传文件", "material_qa": "材料问答"}
        raise HTTPException(429, f"公开演示的{labels.get(action, action)}频率已达上限")
    with connect() as conn:
        conn.execute("INSERT INTO rate_events (id,client_ip,action,created_at) VALUES (?,?,?,?)",
                     (str(uuid.uuid4()), client_ip, action, now_iso()))


def validate_magic(filename: str, content: bytes) -> None:
    lower = filename.lower()
    valid = (lower.endswith(".pdf") and content.startswith(b"%PDF")) or (
        lower.endswith((".docx", ".xlsx")) and content.startswith(b"PK")
    )
    if not valid:
        raise HTTPException(400, "文件内容与扩展名不一致")
    if lower.endswith((".docx", ".xlsx")):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                uncompressed = sum(item.file_size for item in entries)
                if len(entries) > settings.max_zip_entries:
                    raise HTTPException(413, "Office 文件内部条目过多")
                if uncompressed > settings.max_zip_uncompressed_mb * 1024 * 1024:
                    raise HTTPException(413, "Office 文件解压后体积超过限制")
        except zipfile.BadZipFile as exc:
            raise HTTPException(400, "Office 文件结构损坏") from exc


def cleanup_expired_projects() -> int:
    if not settings.public_demo:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.retention_hours)
    expired = [item["id"] for item in rows("SELECT id,created_at FROM projects WHERE id NOT LIKE 'demo-%'") if datetime.fromisoformat(item["created_at"]) < cutoff]
    tables = [
        "retrieval_chunks", "retrieval_indexes",
        "project_profile_snapshots", "project_profile_roles",
        "llm_attempts", "run_artifacts", "run_nodes", "policy_snapshots", "audit_events",
        "evidence_edges", "report_claims", "report_sections", "review_tasks", "candidate_findings",
        "report_table_snapshots", "document_table_history", "document_tables", "table_cell_reviews",
        "risks", "cross_checks", "financial_tie_outs", "financial_metrics", "canonical_facts",
        "fact_assertions", "facts", "gold_annotations", "project_entities", "runs", "citations",
        "sources", "documents",
    ]
    upload_root = (settings.resolved_data_dir / "uploads").resolve()
    with connect() as conn:
        for project_id in expired:
            for table in tables:
                conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
            directory = (upload_root / project_id).resolve()
            if directory.parent == upload_root and directory.exists() and not directory.is_symlink():
                shutil.rmtree(directory)
        rate_cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        conn.execute("DELETE FROM rate_events WHERE created_at<?", (rate_cutoff,))
    if expired:
        from .retrieval import delete_project_index

        for project_id in expired:
            delete_project_index(project_id)
    return len(expired)
