from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import settings


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, access_token TEXT NOT NULL, company_name TEXT NOT NULL,
  amount REAL NOT NULL, term_months INTEGER NOT NULL, purpose TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  consent_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, filename TEXT NOT NULL,
  kind TEXT NOT NULL, media_type TEXT NOT NULL, sha256 TEXT NOT NULL,
  size INTEGER NOT NULL, parse_status TEXT NOT NULL, quality REAL NOT NULL,
  locator_count INTEGER NOT NULL, warning TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, document_id TEXT NOT NULL,
  locator_type TEXT NOT NULL, locator TEXT NOT NULL, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, category TEXT NOT NULL,
  name TEXT NOT NULL, period TEXT NOT NULL, value REAL, unit TEXT NOT NULL,
  text_value TEXT NOT NULL, source_ids TEXT NOT NULL, confidence REAL NOT NULL,
  fact_key TEXT NOT NULL DEFAULT '', raw_value TEXT NOT NULL DEFAULT '',
  extraction_method TEXT NOT NULL DEFAULT 'seed', review_status TEXT NOT NULL DEFAULT 'unreviewed'
);
CREATE TABLE IF NOT EXISTS cross_checks (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, rule_key TEXT NOT NULL,
  title TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL,
  summary TEXT NOT NULL, values_json TEXT NOT NULL, source_ids TEXT NOT NULL,
  formula TEXT NOT NULL DEFAULT '', fact_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS risks (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, severity TEXT NOT NULL,
  category TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
  impact TEXT NOT NULL, action TEXT NOT NULL, source_ids TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_sections (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, section_key TEXT NOT NULL,
  title TEXT NOT NULL, content TEXT NOT NULL, citation_ids TEXT NOT NULL,
  review_status TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(project_id, section_key)
);
CREATE TABLE IF NOT EXISTS citations (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_id TEXT NOT NULL,
  document_id TEXT NOT NULL, label TEXT NOT NULL, quote TEXT NOT NULL,
  locator_type TEXT NOT NULL, locator TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL,
  current_node TEXT NOT NULL, progress INTEGER NOT NULL, message TEXT NOT NULL,
  cancel_requested INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL,
  finished_at TEXT, latency_ms INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS run_nodes (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, project_id TEXT NOT NULL,
  node_key TEXT NOT NULL, status TEXT NOT NULL, progress INTEGER NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0, started_at TEXT, finished_at TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', output_summary TEXT NOT NULL DEFAULT '',
  UNIQUE(run_id, node_key)
);
CREATE TABLE IF NOT EXISTS rate_events (
  id TEXT PRIMARY KEY, client_ip TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_project ON sources(project_id);
CREATE INDEX IF NOT EXISTS idx_rate_events_lookup ON rate_events(client_ip, action, created_at);
"""


MIGRATIONS = {
    "projects": {"consent_at": "TEXT NOT NULL DEFAULT ''"},
    "facts": {
        "fact_key": "TEXT NOT NULL DEFAULT ''",
        "raw_value": "TEXT NOT NULL DEFAULT ''",
        "extraction_method": "TEXT NOT NULL DEFAULT 'seed'",
        "review_status": "TEXT NOT NULL DEFAULT 'unreviewed'",
    },
    "cross_checks": {
        "formula": "TEXT NOT NULL DEFAULT ''",
        "fact_ids": "TEXT NOT NULL DEFAULT '[]'",
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    conn = sqlite3.connect(str(settings.database_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {item[1] for item in conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_project_key ON facts(project_id, fact_key, period)")


def rows(query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]


def row(query: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    result = rows(query, params)
    return result[0] if result else None


def execute(query: str, params: Iterable[Any] = ()) -> None:
    with connect() as conn:
        conn.execute(query, tuple(params))


def executemany(query: str, values: Iterable[Iterable[Any]]) -> None:
    with connect() as conn:
        conn.executemany(query, values)


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def json_load(value: str) -> Any:
    return json.loads(value) if value else []
