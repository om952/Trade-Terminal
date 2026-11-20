"""Saved scans with version history, alert snapshots and change events.

A saved scan works like a saved strategy: a stable id (`scn_` plus hex), and a
new version only when the spec changes, so a snapshot can name exactly the
spec it ran. Archiving hides a scan and stops its alerts; nothing is deleted.

Snapshots and events belong to the alert checks (backend/scan/alerts.py).
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any

from backend.appstate.db import connection, transaction
from backend.core.config import IST
from backend.scan.spec import ScanSpec


class ScanNotFound(KeyError):
    pass


def _now() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def _row(conn, scan_id: str):
    row = conn.execute("SELECT * FROM scans WHERE id = ?", [scan_id]).fetchone()
    if row is None:
        raise ScanNotFound(scan_id)
    return row


def _record(row, document: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "version": row["version"],
        "hash": row["current_hash"], "alerts": bool(row["alerts"]),
        "archived": bool(row["archived"]), "last_checked_at": row["last_checked_at"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "document": document,
    }


def save(
    document: dict[str, Any], *, scan_id: str | None = None, alerts: bool | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create or update. Returns (the scan, whether a new version was written).

    Raises pydantic's ValidationError for an invalid spec and ScanNotFound for
    an unknown id. `alerts` None keeps the current setting (off for a new scan).
    """
    spec = ScanSpec.model_validate(document)
    body = spec.model_dump(mode="json")
    text = json.dumps(body, sort_keys=True)
    content_hash = spec.content_hash()
    now = _now()

    with transaction() as conn:
        if scan_id is None:
            scan_id = f"scn_{secrets.token_hex(6)}"
            conn.execute(
                """INSERT INTO scans (id, name, current_hash, version, alerts, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                [scan_id, spec.name, content_hash, 1, int(bool(alerts)), now, now],
            )
            number, created = 1, True
        else:
            row = _row(conn, scan_id)
            changed = row["current_hash"] != content_hash or bool(row["archived"])
            number = row["version"] + 1 if changed else row["version"]
            conn.execute(
                """UPDATE scans SET name = ?, current_hash = ?, version = ?, archived = 0,
                   alerts = COALESCE(?, alerts), updated_at = ? WHERE id = ?""",
                [spec.name, content_hash, number, None if alerts is None else int(alerts),
                 now if changed else row["updated_at"], scan_id],
            )
            created = changed
        if created:
            conn.execute(
                "INSERT INTO scan_versions (scan_id, version, hash, document, created_at) VALUES (?,?,?,?,?)",
                [scan_id, number, content_hash, text, now],
            )
    return get(scan_id), created


def get(scan_id: str, version: int | None = None) -> dict[str, Any]:
    conn = connection()
    row = _row(conn, scan_id)
    v = conn.execute(
        "SELECT document FROM scan_versions WHERE scan_id = ? AND version = ?",
        [scan_id, version or row["version"]],
    ).fetchone()
    if v is None:
        raise ScanNotFound(f"{scan_id} v{version}")
    return _record(row, json.loads(v["document"]))


def list_all(include_archived: bool = False) -> list[dict[str, Any]]:
    rows = connection().execute(
        "SELECT * FROM scans" + ("" if include_archived else " WHERE archived = 0")
        + " ORDER BY updated_at DESC"
    ).fetchall()
    out = []
    for row in rows:
        record = _record(row, {})
        record.pop("document")
        out.append(record)
    return out


def set_alerts(scan_id: str, on: bool) -> dict[str, Any]:
    with transaction() as conn:
        _row(conn, scan_id)
        conn.execute("UPDATE scans SET alerts = ? WHERE id = ?", [int(on), scan_id])
    return get(scan_id)


def archive(scan_id: str) -> None:
    with transaction() as conn:
        _row(conn, scan_id)
        conn.execute("UPDATE scans SET archived = 1, alerts = 0 WHERE id = ?", [scan_id])


def alerting() -> list[str]:
    """Ids of the scans whose alerts are on."""
    rows = connection().execute(
        "SELECT id FROM scans WHERE alerts = 1 AND archived = 0 ORDER BY id"
    ).fetchall()
    return [r["id"] for r in rows]


def mark_checked(scan_ids: list[str], at: str) -> None:
    with transaction() as conn:
        conn.executemany("UPDATE scans SET last_checked_at = ? WHERE id = ?", [(at, i) for i in scan_ids])


# ---------------------------------------------------------------------------
# Snapshots and events
# ---------------------------------------------------------------------------

def latest_snapshot(scan_id: str) -> dict[str, Any] | None:
    row = connection().execute(
        "SELECT * FROM scan_snapshots WHERE scan_id = ? ORDER BY id DESC LIMIT 1", [scan_id]
    ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "version": row["version"], "as_of": row["as_of"],
            "states": json.loads(row["states"]), "job_id": row["job_id"],
            "created_at": row["created_at"]}


def record_snapshot(
    scan_id: str, version: int, as_of: str | None, states: dict[str, Any],
    events: list[dict[str, Any]], job_id: str | None,
) -> int:
    now = _now()
    with transaction() as conn:
        cur = conn.execute(
            """INSERT INTO scan_snapshots (scan_id, version, as_of, states, job_id, created_at)
               VALUES (?,?,?,?,?,?)""",
            [scan_id, version, as_of, json.dumps(states, sort_keys=True), job_id, now],
        )
        snapshot_id = int(cur.lastrowid or 0)
        conn.executemany(
            """INSERT INTO scan_events
               (scan_id, snapshot_id, as_of, symbol, instrument_key, kind, detail, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            [(scan_id, snapshot_id, as_of, e["symbol"], e["key"], e["kind"],
              json.dumps(e.get("detail") or {}, sort_keys=True), now) for e in events],
        )
    return snapshot_id


def events(scan_id: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = connection().execute(
        "SELECT * FROM scan_events WHERE scan_id = ? ORDER BY id DESC LIMIT ?",
        [scan_id, max(1, min(limit, 1000))],
    ).fetchall()
    return [{"id": r["id"], "as_of": r["as_of"], "symbol": r["symbol"], "key": r["instrument_key"],
             "kind": r["kind"], "detail": json.loads(r["detail"]), "created_at": r["created_at"]}
            for r in rows]
