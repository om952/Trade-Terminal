"""The app database: SQLite in WAL mode, migrated forward on first use.

Why SQLite and not the existing DuckDB file: DuckDB allows one writer process,
which is why the API and the data CLIs could never run together. SQLite in WAL
mode lets several processes read while one writes, with short writes queuing
behind a busy timeout. DuckDB stays for analytics over Parquet.

Connections are opened per unit of work (`with db.connect() as conn:`) rather
than shared: opening one costs well under a millisecond, and it sidesteps
SQLite's rules about sharing connections across threads.

Migrations are numbered SQL files in `migrations/`, applied in order and
recorded in `schema_migrations`. A database written by a newer version of the
app is refused rather than opened, because silently running old code against a
newer schema is how data gets corrupted.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from backend.app.context import AppContext
from backend.core.config import IST

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class NewerDatabaseError(RuntimeError):
    """The database was migrated by a newer version of the app."""


def available_migrations() -> list[tuple[int, Path]]:
    found = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise RuntimeError(f"Badly named migration {path.name}: expected NNNN_name.sql")
        found.append((int(match.group(1)), path))
    versions = [v for v, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"Migrations must be numbered 1..N without gaps, got {versions}")
    return found


def now_iso() -> str:
    return datetime.now(IST).isoformat()


class AppDB:
    """One SQLite file. Migrated on first connect in each process."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._migrated = False
        self._lock = threading.Lock()

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A connection for reads. Takes no write lock, so it never waits on a writer."""
        self.migrate()
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection inside one write transaction: committed on success, rolled
        back on error. Taken immediately, so two writers queue instead of deadlocking."""
        self.migrate()
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def migrate(self) -> int:
        """Apply pending migrations. Returns the schema version afterwards."""
        if self._migrated:
            return self.schema_version()
        with self._lock:
            if self._migrated:
                return self.schema_version()
            conn = self._open()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
                )
                applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
                known = available_migrations()
                newest_known = known[-1][0] if known else 0
                if applied and max(applied) > newest_known:
                    raise NewerDatabaseError(
                        f"{self.path} is at schema version {max(applied)}, but this version "
                        f"of Trade Terminal only knows up to {newest_known}. Upgrade the app "
                        f"rather than opening a newer database with older code."
                    )
                for version, path in known:
                    if version in applied:
                        continue
                    log.info("Applying app database migration %s", path.name)
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        # executescript() would commit implicitly; run statements one
                        # by one so the migration and its record land atomically.
                        for statement in _split_sql(path.read_text()):
                            conn.execute(statement)
                        conn.execute(
                            "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                            (version, path.name, now_iso()),
                        )
                        conn.execute("COMMIT")
                    except BaseException:
                        conn.execute("ROLLBACK")
                        raise
            finally:
                conn.close()
            self._migrated = True
        return self.schema_version()

    def schema_version(self) -> int:
        conn = self._open()
        try:
            row = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()


def _split_sql(script: str) -> list[str]:
    """Split a migration into statements. Migrations avoid ';' inside literals and
    triggers, which keeps this honest; a statement that needs one gets its own file."""
    lines = [line for line in script.splitlines() if not line.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


_databases: dict[Path, AppDB] = {}
_databases_lock = threading.Lock()


def get_app_db(ctx: AppContext | None = None) -> AppDB:
    """The app database for `ctx` (the local user by default), one instance per file."""
    path = (ctx or AppContext.local()).app_db_path.resolve()
    with _databases_lock:
        db = _databases.get(path)
        if db is None:
            db = _databases[path] = AppDB(path)
        return db
