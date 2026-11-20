"""Saved strategies with version history.

A saved strategy has a stable id (`stg_` plus random hex) that is written into
the document, so the document and the record agree on identity. Every save
that changes behaviour (the content hash) or the name or description appends a
version; saving an identical document is a no-op, so the history only holds
real changes. Old versions are never edited or deleted, which is what lets a
past run be traced to exactly the document it ran.

Deleting archives: the strategy disappears from the list, its versions and
runs stay.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from backend.appstate.db import connection, transaction
from backend.core.config import IST
from backend.strategy.schema import Strategy

Origin = Literal["manual", "template", "agent", "import", "research"]


class StrategyNotFound(KeyError):
    pass


@dataclass(frozen=True)
class Version:
    strategy_id: str
    version: int
    hash: str
    name: str
    document: dict[str, Any]
    origin: str
    note: str
    created_at: str

    def as_dict(self, include_document: bool = True) -> dict[str, Any]:
        out = {
            "strategy_id": self.strategy_id, "version": self.version, "hash": self.hash,
            "name": self.name, "origin": self.origin, "note": self.note,
            "created_at": self.created_at,
        }
        if include_document:
            out["document"] = self.document
        return out


def _now() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def new_id() -> str:
    return f"stg_{secrets.token_hex(6)}"


def save(
    document: dict[str, Any] | Strategy,
    *,
    strategy_id: str | None = None,
    origin: Origin = "manual",
    note: str = "",
) -> tuple[Version, bool]:
    """Create or update. Returns (current version, whether a new version was written).

    `strategy_id` None creates a new strategy. Raises pydantic's ValidationError
    for an invalid document and StrategyNotFound for an unknown id.
    """
    strategy = document if isinstance(document, Strategy) else Strategy.model_validate(document)
    sid = strategy_id or new_id()
    payload = strategy.model_dump(mode="json")
    payload["id"] = sid
    strategy = Strategy.model_validate(payload)
    content_hash = strategy.content_hash()
    text = json.dumps(payload, sort_keys=True)

    with transaction() as conn:
        row = conn.execute(
            "SELECT version, archived FROM strategies WHERE id = ?", [sid]
        ).fetchone()
        now = _now()

        if row is None:
            if strategy_id is not None:
                raise StrategyNotFound(strategy_id)
            number = 1
            conn.execute(
                """INSERT INTO strategies (id, name, current_hash, version, created_at, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                [sid, strategy.name, content_hash, number, now, now],
            )
        else:
            latest = conn.execute(
                "SELECT document FROM strategy_versions WHERE strategy_id = ? AND version = ?",
                [sid, row["version"]],
            ).fetchone()
            if latest is not None and latest["document"] == text and not row["archived"]:
                return _version(conn, sid, row["version"]), False
            number = row["version"] + 1
            conn.execute(
                """UPDATE strategies SET name = ?, current_hash = ?, version = ?,
                   archived = 0, updated_at = ? WHERE id = ?""",
                [strategy.name, content_hash, number, now, sid],
            )

        conn.execute(
            """INSERT INTO strategy_versions
               (strategy_id, version, hash, name, document, origin, note, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            [sid, number, content_hash, strategy.name, text, origin, note, now],
        )
        return _version(conn, sid, number), True


def _version(conn, strategy_id: str, number: int) -> Version:
    row = conn.execute(
        "SELECT * FROM strategy_versions WHERE strategy_id = ? AND version = ?",
        [strategy_id, number],
    ).fetchone()
    if row is None:
        raise StrategyNotFound(f"{strategy_id} v{number}")
    return Version(
        strategy_id=row["strategy_id"], version=row["version"], hash=row["hash"],
        name=row["name"], document=json.loads(row["document"]), origin=row["origin"],
        note=row["note"], created_at=row["created_at"],
    )


def get(strategy_id: str, version: int | None = None) -> Version:
    conn = connection()
    if version is None:
        row = conn.execute("SELECT version FROM strategies WHERE id = ?", [strategy_id]).fetchone()
        if row is None:
            raise StrategyNotFound(strategy_id)
        version = row["version"]
    return _version(conn, strategy_id, version)


def history(strategy_id: str) -> list[Version]:
    conn = connection()
    if conn.execute("SELECT 1 FROM strategies WHERE id = ?", [strategy_id]).fetchone() is None:
        raise StrategyNotFound(strategy_id)
    rows = conn.execute(
        "SELECT version FROM strategy_versions WHERE strategy_id = ? ORDER BY version DESC",
        [strategy_id],
    ).fetchall()
    return [_version(conn, strategy_id, r["version"]) for r in rows]


def list_all(include_archived: bool = False) -> list[dict[str, Any]]:
    rows = connection().execute(
        f"""SELECT s.*, (SELECT count(*) FROM runs r WHERE r.strategy_id = s.id) AS runs
            FROM strategies s {'' if include_archived else 'WHERE archived = 0'}
            ORDER BY updated_at DESC"""
    ).fetchall()
    return [
        {
            "id": r["id"], "name": r["name"], "hash": r["current_hash"],
            "version": r["version"], "archived": bool(r["archived"]), "runs": r["runs"],
            "created_at": r["created_at"], "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def archive(strategy_id: str) -> None:
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE strategies SET archived = 1, updated_at = ? WHERE id = ?",
            [_now(), strategy_id],
        )
        if cur.rowcount == 0:
            raise StrategyNotFound(strategy_id)


def find_version_by_hash(strategy_id: str, content_hash: str) -> Version | None:
    row = connection().execute(
        """SELECT version FROM strategy_versions WHERE strategy_id = ? AND hash = ?
           ORDER BY version DESC LIMIT 1""",
        [strategy_id, content_hash],
    ).fetchone()
    return None if row is None else get(strategy_id, row["version"])
