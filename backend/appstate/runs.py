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
    from backend.strategy.family import family_key

    run_id = new_id()
    family = family_key(document)
    blob = gzip.compress(to_json(payload).encode(), compresslevel=6)
    with transaction() as conn:
        conn.execute(
            """INSERT INTO runs (id, kind, strategy_id, strategy_hash, strategy_name, document,
                                 params, data, metrics, result, job_id, created_at, family)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                run_id, kind, strategy_id, payload["strategy_hash"],
                document.get("name", ""), to_json(document, sort_keys=True),
                to_json(params, sort_keys=True),
                to_json(payload.get("data", {}), sort_keys=True),
                to_json(payload.get("metrics", {})),
                blob, job_id, datetime.now(IST).isoformat(timespec="seconds"), family,
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
        "family": row["family"],
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
    strategy_id: str | None = None, strategy_hash: str | None = None, limit: int = 50,
    family: str | None = None,
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if family:
        clauses.append("family = ?")
        params.append(family)
    if strategy_id:
        clauses.append("strategy_id = ?")
        params.append(strategy_id)
    if strategy_hash:
        clauses.append("strategy_hash = ?")
        params.append(strategy_hash)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = connection().execute(
        f"""SELECT id, kind, strategy_id, strategy_hash, family, strategy_name, params, data,
                   metrics, job_id, created_at
            FROM runs {where} ORDER BY created_at DESC, rowid DESC LIMIT ?""",
        [*params, max(1, min(limit, 500))],
    ).fetchall()
    return [_summary(r) for r in rows]


# Trials count kind "backtest" runs only: runs and sweep variants are what a
# trader chooses between. Validation runs (walk-forward folds, slippage
# stress, a second instrument) check a strategy already chosen.


def trial_count(strategy_hash: str) -> int:
    """How many backtests this exact strategy has had."""
    return connection().execute(
        "SELECT count(*) FROM runs WHERE strategy_hash = ? AND kind = 'backtest'", [strategy_hash]
    ).fetchone()[0]


def family_trials(family: str) -> dict[str, int]:
    """Trials in a family: every backtest, and how many distinct variants they covered.

    `runs` is what a multiple-testing correction needs as the number of trials;
    `variants` (distinct strategy hashes) says how much of that was retuning
    rather than re-running the same thing.
    """
    row = connection().execute(
        "SELECT count(*), count(DISTINCT strategy_hash) FROM runs WHERE family = ? AND kind = 'backtest'",
        [family],
    ).fetchone()
    return {"runs": row[0], "variants": row[1]}


def family_experiments(family: str) -> list[float | None]:
    """Annualized Sharpe of each distinct experiment in a family, latest run of each.

    An experiment is a (strategy version, data set) pair: rerunning the identical
    test adds nothing, but a retuned number or another instrument or period is a
    new try. Its count is the N the deflated Sharpe ratio corrects for.
    """
    rows = connection().execute(
        """SELECT json_extract(metrics, '$.sharpe') AS sharpe, max(created_at)
           FROM runs WHERE family = ? AND kind = 'backtest'
           GROUP BY strategy_hash, json_extract(data, '$.fingerprint')""",
        [family],
    ).fetchall()
    return [r["sharpe"] for r in rows]


def families(limit: int = 50) -> list[dict[str, Any]]:
    """Families by recent activity, with their trial counts and best Sharpe so far."""
    rows = connection().execute(
        """SELECT family, count(*) AS runs, count(DISTINCT strategy_hash) AS variants,
                  max(created_at) AS last_run, min(created_at) AS first_run,
                  max(json_extract(metrics, '$.sharpe')) AS best_sharpe
           FROM runs WHERE family IS NOT NULL
           GROUP BY family ORDER BY last_run DESC LIMIT ?""",
        [max(1, min(limit, 500))],
    ).fetchall()
    return [dict(r) for r in rows]
