"""Application state in SQLite: strategies, their versions, runs and jobs.

Why SQLite and not the DuckDB file the market data uses: app state is many
small transactional writes from several threads (a job updating progress, the
UI saving a strategy, a run being recorded), which is SQLite's home ground.
DuckDB allows one writer process and is built for scans over Parquet; it stays
the analytics store. The two never share a transaction, and nothing here needs
one with market data.

Settings: WAL, so readers never block the writer; a busy timeout, so two
writers queue rather than fail; foreign keys on. One connection per thread,
because sqlite3 connections must not be shared across threads.

Schema changes are numbered migrations applied in order, tracked with
`PRAGMA user_version`. Never edit a migration that has shipped; add a new one.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from backend.core.config import settings

log = logging.getLogger(__name__)

def _add_run_families(conn: sqlite3.Connection) -> None:
    """3: every run gets its strategy family (backend/strategy/family.py),
    including the runs recorded before families existed."""
    from backend.strategy.family import family_key

    conn.execute("ALTER TABLE runs ADD COLUMN family TEXT")
    rows = conn.execute("SELECT id, document FROM runs").fetchall()
    for row in rows:
        try:
            key = family_key(json.loads(row[1]))
        except Exception:                     # a document this build cannot read
            key = None
        conn.execute("UPDATE runs SET family = ? WHERE id = ?", [key, row[0]])
    conn.execute("CREATE INDEX ix_runs_family ON runs(family, created_at)")


Migration = str | Callable[[sqlite3.Connection], None]

MIGRATIONS: list[Migration] = [
    # 1: strategies with version history, runs, jobs.
    """
    CREATE TABLE strategies (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        current_hash TEXT NOT NULL,
        version      INTEGER NOT NULL,
        archived     INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    );

    CREATE TABLE strategy_versions (
        strategy_id TEXT NOT NULL REFERENCES strategies(id),
        version     INTEGER NOT NULL,
        hash        TEXT NOT NULL,
        name        TEXT NOT NULL,
        document    TEXT NOT NULL,
        origin      TEXT NOT NULL,
        note        TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        PRIMARY KEY (strategy_id, version)
    );
    CREATE INDEX ix_versions_hash ON strategy_versions(strategy_id, hash);

    CREATE TABLE jobs (
        id          TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,
        status      TEXT NOT NULL,
        progress    REAL NOT NULL DEFAULT 0,
        message     TEXT NOT NULL DEFAULT '',
        params      TEXT NOT NULL,
        result      TEXT,
        error       TEXT,
        created_at  TEXT NOT NULL,
        started_at  TEXT,
        finished_at TEXT
    );
    CREATE INDEX ix_jobs_created ON jobs(created_at);

    CREATE TABLE runs (
        id            TEXT PRIMARY KEY,
        kind          TEXT NOT NULL,
        strategy_id   TEXT REFERENCES strategies(id),
        strategy_hash TEXT NOT NULL,
        strategy_name TEXT NOT NULL,
        document      TEXT NOT NULL,
        params        TEXT NOT NULL,
        data          TEXT NOT NULL,
        metrics       TEXT NOT NULL,
        result        BLOB NOT NULL,
        job_id        TEXT,
        created_at    TEXT NOT NULL
    );
    CREATE INDEX ix_runs_strategy ON runs(strategy_id, created_at);
    CREATE INDEX ix_runs_hash ON runs(strategy_hash);
    """,
    # 2: builder conversations. The graph's own state lives in its checkpoint
    # database; this row is the index the UI lists and the turn lock.
    """
    CREATE TABLE conversations (
        id          TEXT PRIMARY KEY,
        title       TEXT NOT NULL,
        strategy_id TEXT REFERENCES strategies(id),
        job_id      TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE INDEX ix_conversations_updated ON conversations(updated_at);
    """,
    _add_run_families,
    # 4: saved scans, versioned like strategies; each alert check's snapshot
    # of every stock's state, and the changes found between snapshots.
    """
    CREATE TABLE scans (
        id              TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        current_hash    TEXT NOT NULL,
        version         INTEGER NOT NULL,
        alerts          INTEGER NOT NULL DEFAULT 0,
        archived        INTEGER NOT NULL DEFAULT 0,
        last_checked_at TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );

    CREATE TABLE scan_versions (
        scan_id    TEXT NOT NULL REFERENCES scans(id),
        version    INTEGER NOT NULL,
        hash       TEXT NOT NULL,
        document   TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (scan_id, version)
    );

    CREATE TABLE scan_snapshots (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id    TEXT NOT NULL REFERENCES scans(id),
        version    INTEGER NOT NULL,
        as_of      TEXT,
        states     TEXT NOT NULL,
        job_id     TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX ix_scan_snapshots ON scan_snapshots(scan_id, id);

    CREATE TABLE scan_events (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id        TEXT NOT NULL REFERENCES scans(id),
        snapshot_id    INTEGER NOT NULL REFERENCES scan_snapshots(id),
        as_of          TEXT,
        symbol         TEXT NOT NULL,
        instrument_key TEXT NOT NULL,
        kind           TEXT NOT NULL,
        detail         TEXT NOT NULL,
        created_at     TEXT NOT NULL
    );
    CREATE INDEX ix_scan_events ON scan_events(scan_id, id);
    """,
    # 5: the final holdout (P4.2). app_meta holds values fixed once, such as the
    # holdout's start; holdout_tests is the one look each strategy family gets.
    """
    CREATE TABLE app_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE holdout_tests (
        family        TEXT PRIMARY KEY,
        holdout_start TEXT NOT NULL,
        run_id        TEXT REFERENCES runs(id),
        strategy_hash TEXT,
        created_at    TEXT NOT NULL
    );
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)

_local = threading.local()
_migrate_lock = threading.Lock()
_migrated: set[Path] = set()


def db_path() -> Path:
    return settings.app_db_path


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def connection() -> sqlite3.Connection:
    """This thread's connection, migrated to the current schema."""
    path = db_path()
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != path:
        if conn is not None:
            conn.close()
        conn = _open(path)
        _local.conn, _local.path = conn, path
        _migrate(conn, path)
    return conn


def _migrate(conn: sqlite3.Connection, path: Path) -> None:
    with _migrate_lock:
        if path in _migrated:
            return
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"{path} is at schema version {current}, newer than this build "
                f"understands ({SCHEMA_VERSION}). Upgrade the app rather than "
                f"running an older version against newer data."
            )
        for number in range(current + 1, SCHEMA_VERSION + 1):
            log.info("Migrating app database to schema version %d", number)
            _apply(conn, number, MIGRATIONS[number - 1])
        try:
            path.chmod(0o600)
        except OSError:
            pass
        _migrated.add(path)


def _apply(conn: sqlite3.Connection, number: int, step: Migration) -> None:
    """One migration as one transaction: it lands whole or not at all."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if callable(step):
            step(conn)
        else:
            # execute() takes one statement; split the script ourselves rather
            # than use executescript(), which commits whatever came before.
            for statement in filter(None, (s.strip() for s in step.split(";"))):
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {number}")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def to_json(value: Any, **kwargs: Any) -> str:
    """Strict JSON for anything persisted: non-finite floats become null.

    The same rule the HTTP layer applies, so a stored run reads back exactly as
    the response that produced it. `allow_nan=False` makes any miss an error
    instead of writing non-standard `Infinity` into the database.
    """
    return json.dumps(_finite(value), allow_nan=False, default=str, **kwargs)


def _finite(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolled back on any exception."""
    conn = connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def close() -> None:
    """Close this thread's connection (tests, shutdown)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.path = None
