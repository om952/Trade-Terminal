"""App database: migrations, concurrency, integrity, and the single local user."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading

import pytest

from backend.app import preferences
from backend.app.context import LOCAL_USER, AppContext
from backend.app.db import AppDB, NewerDatabaseError, available_migrations, get_app_db, now_iso


@pytest.fixture
def db(tmp_path) -> AppDB:
    return AppDB(tmp_path / "app.sqlite")


def test_migrations_are_numbered_without_gaps():
    versions = [v for v, _ in available_migrations()]
    assert versions == list(range(1, len(versions) + 1))


def test_migrate_creates_schema_in_wal_mode(db):
    assert db.migrate() == len(available_migrations())
    with db.read() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"strategies", "strategy_versions", "runs", "jobs", "app_sessions",
                "preferences", "conversations", "messages"} <= tables
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_every_app_table_has_a_user_id(db):
    db.migrate()
    with db.read() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT IN ('schema_migrations', 'sqlite_sequence')"
        )]
        for table in tables:
            columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert "user_id" in columns, f"{table} has no user_id (plan §1, Scope rule 1)"


def test_migrate_is_idempotent_across_instances(db):
    first = db.migrate()
    again = AppDB(db.path).migrate()
    assert first == again
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == first


def test_newer_database_is_refused(db):
    db.migrate()
    with db.connect() as conn:
        conn.execute("INSERT INTO schema_migrations VALUES (999, '0999_future.sql', ?)", (now_iso(),))
    with pytest.raises(NewerDatabaseError, match="999"):
        AppDB(db.path).migrate()


def test_failed_transaction_rolls_back(db):
    with pytest.raises(RuntimeError):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO preferences (key, value_json, updated_at) VALUES ('k', '1', ?)",
                (now_iso(),),
            )
            raise RuntimeError("boom")
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM preferences").fetchone()[0] == 0


def test_foreign_keys_are_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO strategy_versions (strategy_id, hash, schema_version, spec_json, "
                "source, created_at) VALUES ('missing', 'h', 1, '{}', 'builder', ?)",
                (now_iso(),),
            )


def test_user_id_defaults_to_local(db):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO conversations (id, created_at, updated_at) VALUES ('c1', ?, ?)",
            (now_iso(), now_iso()),
        )
    with db.read() as conn:
        assert conn.execute("SELECT user_id FROM conversations").fetchone()[0] == LOCAL_USER


def test_concurrent_writers_in_threads_all_land(db):
    db.migrate()
    errors: list[Exception] = []

    def write(i: int) -> None:
        try:
            for j in range(20):
                with db.connect() as conn:
                    conn.execute(
                        "INSERT INTO jobs (id, kind, status, created_at) VALUES (?, 'test', 'queued', ?)",
                        (f"t{i}-{j}", now_iso()),
                    )
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 160


def test_two_processes_can_write_at_once(db):
    """The point of leaving DuckDB for app state: the API and a CLI can both write."""
    db.migrate()
    script = (
        "import sys; from pathlib import Path; from backend.app.db import AppDB, now_iso\n"
        "db = AppDB(Path(sys.argv[1]))\n"
        "for j in range(30):\n"
        "    with db.connect() as c:\n"
        "        c.execute(\"INSERT INTO jobs (id, kind, status, created_at) "
        "VALUES (?, 'p', 'queued', ?)\", (sys.argv[2] + str(j), now_iso()))\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(db.path), prefix])
        for prefix in ("a", "b", "c")
    ]
    assert [p.wait(timeout=60) for p in procs] == [0, 0, 0]
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 90


def test_get_app_db_follows_the_local_context(tmp_path, monkeypatch):
    from backend.core.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    assert AppContext.local().data_dir == tmp_path
    assert get_app_db().path == (tmp_path / "app.sqlite").resolve()
    assert get_app_db() is get_app_db()


def test_preferences_defaults_set_and_validation(tmp_path, monkeypatch):
    from backend.core.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    assert preferences.get("data_retention") == "ask"
    assert preferences.get("idle_minutes") == 30
    assert preferences.get("checkpoint_retention_days") == 15

    preferences.set("data_retention", "discard")
    assert preferences.get("data_retention") == "discard"
    preferences.set("data_retention", "keep")
    assert preferences.all_preferences()["data_retention"] == "keep"

    with pytest.raises(ValueError):
        preferences.set("data_retention", "forever")
    with pytest.raises(KeyError):
        preferences.get("data_retension")


def test_preferences_are_per_user(tmp_path):
    alice = AppContext(user_id="alice", data_dir=tmp_path)
    bob = AppContext(user_id="bob", data_dir=tmp_path)
    preferences.set("data_retention", "keep", alice)
    assert preferences.get("data_retention", alice) == "keep"
    assert preferences.get("data_retention", bob) == "ask"
