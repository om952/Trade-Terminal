"""Run history: every backtest that completes is recorded.

A run stores what it needs to be reproduced and compared: the exact document,
the parameters, and the data fingerprint from the cache. Two runs with the same
strategy hash, parameters and fingerprint must produce the same result, which
is a property the test suite can check and a user can rely on.

The full report (trades, equity curve, chart) is stored gzipped. It is a few
hundred kilobytes at most, and keeping it means a past result opens instantly
and exactly as it was, even after the data or the engine moved on.

Every backtest of a strategy family is also a trial for the multiple-testing
correction planned in P2.8 and P4.2; recording all of them now is cheap, and the
count cannot be reconstructed later.
"""

from __future__ import annotations

import gzip
import json
import secrets
from datetime import datetime
from typing import Any

from backend.appstate.db import connection, to_json, transaction
from backend.core.config import IST


class RunNotFound(KeyError):
    pass


def new_id() -> str:
    return f"run_{secrets.token_hex(6)}"


def record(
    *,
    kind: str,
    document: dict[str, Any],
    params: dict[str, Any],
    payload: dict[str, Any],
    strategy_id: str | None = None,
    job_id: str | None = None,
) -> str:
    """Store a finished run and return its id."""
    run_id = new_id()
    blob = gzip.compress(to_json(payload).encode(), compresslevel=6)
    with transaction() as conn:
        conn.execute(
            """INSERT INTO runs (id, kind, strategy_id, strategy_hash, strategy_name, document,
                                 params, data, metrics, result, job_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                run_id, kind, strategy_id, payload["strategy_hash"],
                document.get("name", ""), to_json(document, sort_keys=True),
                to_json(params, sort_keys=True),
                to_json(payload.get("data", {}), sort_keys=True),
                to_json(payload.get("metrics", {})),
                blob, job_id, datetime.now(IST).isoformat(timespec="seconds"),
            ],
        )
    return run_id


def _summary(row) -> dict[str, Any]:
    data = json.loads(row["data"])
    return {
        "id": row["id"],
        "kind": row["kind"],
        "strategy_id": row["strategy_id"],
        "strategy_hash": row["strategy_hash"],
        "strategy_name": row["strategy_name"],
        "params": json.loads(row["params"]),
        "data": data,
        "metrics": json.loads(row["metrics"]),
        "job_id": row["job_id"],
        "created_at": row["created_at"],
    }


def get(run_id: str, include_result: bool = True) -> dict[str, Any]:
    row = connection().execute("SELECT * FROM runs WHERE id = ?", [run_id]).fetchone()
    if row is None:
        raise RunNotFound(run_id)
    out = _summary(row)
    out["document"] = json.loads(row["document"])
    if include_result:
        out["result"] = json.loads(gzip.decompress(row["result"]))
    return out


def list_runs(
    strategy_id: str | None = None, strategy_hash: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if strategy_id:
        clauses.append("strategy_id = ?")
        params.append(strategy_id)
    if strategy_hash:
        clauses.append("strategy_hash = ?")
        params.append(strategy_hash)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = connection().execute(
        f"""SELECT id, kind, strategy_id, strategy_hash, strategy_name, params, data,
                   metrics, job_id, created_at
            FROM runs {where} ORDER BY created_at DESC, rowid DESC LIMIT ?""",
        [*params, max(1, min(limit, 500))],
    ).fetchall()
    return [_summary(r) for r in rows]


def trial_count(strategy_hash: str) -> int:
    """How many backtests this exact strategy has had."""
    return connection().execute(
        "SELECT count(*) FROM runs WHERE strategy_hash = ?", [strategy_hash]
    ).fetchone()[0]
