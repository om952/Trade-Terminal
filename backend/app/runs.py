"""Run history: every backtest and paper session, and the trial counter.

A run records what was run (the strategy's content hash, so it stays traceable
even if the strategy is later edited or deleted), its parameters, the data it
used (a fingerprint), and its headline metrics.

The trial counter answers "how many configurations of this idea have been
tested?", which a deflated Sharpe ratio needs (Phase 6). It can't be
reconstructed after the fact, which is why it is recorded from day one.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from backend.app.context import AppContext
from backend.app.db import get_app_db, now_iso, to_json

RunKind = Literal["backtest", "paper", "replay", "live"]


@dataclass(frozen=True)
class RunRecord:
    id: str
    strategy_id: str | None
    strategy_hash: str
    family_id: str | None
    kind: str
    status: str
    params: dict[str, Any]
    data_fingerprint: dict[str, Any] | None
    metrics: dict[str, Any] | None
    result_path: str | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class RunStore:
    def __init__(self, ctx: AppContext | None = None) -> None:
        self.ctx = ctx or AppContext.local()
        self.db = get_app_db(self.ctx)

    def start(
        self,
        kind: RunKind,
        strategy_hash: str,
        *,
        strategy_id: str | None = None,
        params: dict[str, Any] | None = None,
        result_path: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Record a run as started. Its family comes from the saved strategy, if any."""
        rid = run_id or f"run_{uuid.uuid4().hex[:12]}"
        stamp = now_iso()
        with self.db.connect() as conn:
            family = None
            if strategy_id:
                row = conn.execute(
                    "SELECT family_id FROM strategies WHERE id = ? AND user_id = ?",
                    (strategy_id, self.ctx.user_id),
                ).fetchone()
                if row is None:
                    strategy_id = None  # unknown or deleted: keep the run, unlinked
                else:
                    family = row["family_id"]
            conn.execute(
                "INSERT INTO runs (id, user_id, strategy_id, strategy_hash, family_id, kind, status,"
                " params_json, result_path, created_at, started_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                (rid, self.ctx.user_id, strategy_id, strategy_hash, family, kind,
                 to_json(params or {}), result_path, stamp, stamp),
            )
        return rid

    def finish(
        self,
        run_id: str,
        *,
        metrics: dict[str, Any] | None = None,
        data_fingerprint: dict[str, Any] | None = None,
        result_path: str | None = None,
    ) -> None:
        self._close(run_id, "done", metrics=metrics, data_fingerprint=data_fingerprint,
                    result_path=result_path)

    def fail(self, run_id: str, error: str) -> None:
        self._close(run_id, "failed", error=error[:4000])

    def cancel(self, run_id: str) -> None:
        self._close(run_id, "cancelled")

    def get(self, run_id: str) -> RunRecord:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ? AND user_id = ?",
                               (run_id, self.ctx.user_id)).fetchone()
        if row is None:
            raise KeyError(f"No run {run_id!r}")
        return _run_record(row)

    def list_runs(
        self,
        *,
        strategy_id: str | None = None,
        strategy_hash: str | None = None,
        kind: RunKind | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        query = "SELECT * FROM runs WHERE user_id = ?"
        params: list[Any] = [self.ctx.user_id]
        for column, value in (("strategy_id", strategy_id), ("strategy_hash", strategy_hash),
                              ("kind", kind)):
            if value is not None:
                query += f" AND {column} = ?"
                params.append(value)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self.db.read() as conn:
            return [_run_record(r) for r in conn.execute(query, params)]

    def trials(self, family_id: str) -> dict[str, int]:
        """How often this idea has been tested: completed backtests, and how many
        distinct versions (content hashes) they covered."""
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT count(*) AS runs, count(DISTINCT strategy_hash) AS versions FROM runs"
                " WHERE user_id = ? AND family_id = ? AND kind = 'backtest' AND status = 'done'",
                (self.ctx.user_id, family_id),
            ).fetchone()
        return {"backtests": int(row["runs"]), "distinct_versions": int(row["versions"])}

    def _close(self, run_id: str, status: str, **fields: Any) -> None:
        sets = ["status = ?", "finished_at = ?"]
        values: list[Any] = [status, now_iso()]
        for name, value in fields.items():
            if value is None:
                continue
            column = {"metrics": "metrics_json", "data_fingerprint": "data_fingerprint"}.get(name, name)
            sets.append(f"{column} = ?")
            values.append(to_json(value) if name in ("metrics", "data_fingerprint") else value)
        values += [run_id, self.ctx.user_id]
        with self.db.connect() as conn:
            conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ? AND user_id = ?", values)


def _loads(text: str | None) -> Any:
    return json.loads(text) if text else None


def _run_record(row) -> RunRecord:
    return RunRecord(
        id=row["id"], strategy_id=row["strategy_id"], strategy_hash=row["strategy_hash"],
        family_id=row["family_id"], kind=row["kind"], status=row["status"],
        params=json.loads(row["params_json"]), data_fingerprint=_loads(row["data_fingerprint"]),
        metrics=_loads(row["metrics_json"]), result_path=row["result_path"], error=row["error"],
        created_at=row["created_at"], started_at=row["started_at"], finished_at=row["finished_at"],
    )
