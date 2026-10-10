from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from backend.app import database


def test_connection_commits_successful_writes_and_rolls_back_errors(tmp_path, monkeypatch):
    path = tmp_path / "agent.db"
    monkeypatch.setattr(database, "settings", SimpleNamespace(database_path=path))
    with database.connect() as conn:
        conn.execute("CREATE TABLE write_probe (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO write_probe VALUES (1)")

    with pytest.raises(sqlite3.IntegrityError):
        with database.connect() as conn:
            conn.execute("INSERT INTO write_probe VALUES (2)")
            conn.execute("INSERT INTO write_probe VALUES (1)")

    with database.connect() as conn:
        assert [item[0] for item in conn.execute("SELECT id FROM write_probe")] == [1]


def test_readonly_error_logs_database_path_and_sqlite_code(tmp_path, monkeypatch, caplog):
    path = tmp_path / "agent.db"
    monkeypatch.setattr(database, "settings", SimpleNamespace(database_path=path))

    with pytest.raises(sqlite3.OperationalError) as caught:
        with database.connect() as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("CREATE TABLE write_probe (id INTEGER)")

    assert caught.value.sqlite_errorname == "SQLITE_READONLY"
    assert str(path) in caplog.text
    assert "code=8 name=SQLITE_READONLY" in caplog.text
    assert "CREATE TABLE write_probe" in caplog.text
    assert "attempt to write a readonly database" in caplog.text
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        conn.execute("SELECT 1")
