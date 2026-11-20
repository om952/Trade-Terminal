"""Research sessions: start one, answer its pauses, read it.

A turn (from the start or a reply to the next pause) runs as a background job,
like the strategy builder's, because evaluating a plan is many backtests. The
graph state is checkpointed in its own SQLite file, so a restart resumes at
the same pause.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from datetime import date, datetime
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from backend.appstate.db import connection, transaction
from backend.appstate.jobs import TERMINAL, JobContext
from backend.core.config import IST, settings
from backend.research import graph as research_graph
from backend.research import memory
from backend.services import jobs as job_services

CHECKPOINT_FILE = "research_checkpoints.sqlite"

_graph = None
_graph_path = None
_lock = threading.Lock()


class SessionNotFound(KeyError):
    pass


class InvalidReply(ValueError):
    pass


class TurnInProgress(RuntimeError):
    pass


class StartRequest(BaseModel):
    strategy: dict[str, Any] | None = None
    strategy_id: str | None = None
    version: int | None = None
    goal: str = Field(default="", max_length=5000)
    symbol: str | None = None
    start: date | None = None
    end: date | None = None
    starting_capital: float = Field(default=1_000_000, gt=0)
    lot_size: int = Field(default=1, ge=1, le=10_000)
    slippage_bps: float | None = None


def _llm():
    from backend.agent import service as agent_service

    return agent_service._llm_factory()


def graph():
    global _graph, _graph_path
    path = settings.data_dir / CHECKPOINT_FILE
    with _lock:
        if _graph is None or _graph_path != path:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            path.chmod(0o600)
            _graph = research_graph.build(_llm, checkpointer=SqliteSaver(conn))
            _graph_path = path
    return _graph


def _config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


def _now() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def start(req: StartRequest) -> dict[str, Any]:
    from backend.services import backtest, holdout
    from backend.services.jobs import resolve_document
    from backend.strategy.family import family_key

    if (req.strategy is None) == (req.strategy_id is None):
        raise InvalidReply("Give either a strategy document or a strategy_id.")
    document, _ = resolve_document(job_services.BacktestJob.model_validate(
        {"strategy": req.strategy, "strategy_id": req.strategy_id, "version": req.version}))
    strategy = backtest.validate(document)
    base = strategy.model_dump(mode="json")
    key = backtest.resolve(req.symbol or strategy.instrument.symbol)
    first, last, _ = holdout.bounds(key, req.start, req.end)
    regime = memory.regime(backtest.load_bars(key, strategy.timeframe, first, last).frame)

    session_id = f"res_{secrets.token_hex(6)}"
    now = _now()
    with transaction() as conn:
        conn.execute(
            """INSERT INTO research_sessions (id, title, base_family, instrument, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            [session_id, (req.goal or strategy.name)[:80], family_key(base), key, now, now],
        )
    run_settings = {k: v for k, v in req.model_dump(mode="json").items()
                    if k in ("start", "end", "starting_capital", "lot_size", "slippage_bps") and v is not None}
    run_settings["symbol"] = key
    initial = {
        "session_id": session_id, "goal": req.goal, "base": base, "base_family": family_key(base),
        "instrument": key, "settings": run_settings, "regime": regime, "phase": "planning",
        "evaluations": [], "events": [research_graph._event("started", f"Researching {strategy.name} on {key} ({regime})")],
    }
    _submit(session_id, {"input": initial})
    return view(session_id)


def reply(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    _require_idle(session_id)
    pending = _pending(session_id)
    if pending is None:
        raise InvalidReply("Nothing is waiting for a reply in this session.")
    if pending.get("type") == "plan" and payload.get("action") not in ("approve", "reject"):
        raise InvalidReply("Reply with action approve (optionally drop: [ids]) or reject.")
    if pending.get("type") == "survivors" and not isinstance(payload.get("keep"), list):
        raise InvalidReply("Reply with keep: [ids] (an empty list keeps none).")
    _submit(session_id, {"resume": payload})
    return view(session_id)


def _submit(session_id: str, params: dict[str, Any]) -> None:
    job = job_services.runner.submit("research_turn", {"session_id": session_id, **params})
    with transaction() as conn:
        conn.execute("UPDATE research_sessions SET job_id = ?, updated_at = ? WHERE id = ?",
                     [job["id"], _now(), session_id])


def _row(session_id: str):
    row = connection().execute("SELECT * FROM research_sessions WHERE id = ?", [session_id]).fetchone()
    if row is None:
        raise SessionNotFound(session_id)
    return row


def _job(row) -> dict[str, Any] | None:
    from backend.appstate.jobs import JobNotFound

    if not row["job_id"]:
        return None
    try:
        return job_services.runner.get(row["job_id"])
    except JobNotFound:
        return None


def _require_idle(session_id: str) -> None:
    job = _job(_row(session_id))
    if job is not None and job["status"] not in TERMINAL:
        raise TurnInProgress("The previous turn is still running.")


def _pending(session_id: str) -> dict[str, Any] | None:
    state = graph().get_state(_config(session_id))
    for task in state.tasks:
        for item in task.interrupts:
            return item.value
    return None


def run_turn(params: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    session_id = params["session_id"]
    command: Any = params["input"] if "input" in params else Command(resume=params["resume"])
    labels = {"plan": "Planning variants", "screen": "Checking the variants", "approve": "Waiting",
              "evaluate": "Backtesting a candidate", "judge": "Judging the results", "choose": "Saving"}
    steps = 0
    for update in graph().stream(command, _config(session_id), stream_mode="updates"):
        for node in update:
            if not node.startswith("__"):
                steps += 1
                ctx.progress(min(0.95, 1 - 0.85 ** steps), labels.get(node, node))
    with transaction() as conn:
        conn.execute("UPDATE research_sessions SET updated_at = ? WHERE id = ?", [_now(), session_id])
    return {"session_id": session_id, "phase": view(session_id)["phase"]}


job_services.runner.register("research_turn", run_turn)


_PAUSE = {"plan": "awaiting_plan_approval", "survivors": "awaiting_choice"}


def view(session_id: str) -> dict[str, Any]:
    row = _row(session_id)
    state = graph().get_state(_config(session_id))
    values = state.values or {}
    pending = _pending(session_id)
    job = _job(row)
    if job is not None and job["status"] not in TERMINAL:
        phase = "working"
    elif pending is not None:
        phase = _PAUSE.get(str(pending.get("type")), "waiting")
    elif values.get("phase") in ("failed", "rejected", "done"):
        phase = values["phase"]
    elif state.next:
        phase = "interrupted"
    else:
        phase = values.get("phase") or "idle"
    return {
        "id": row["id"], "title": row["title"], "instrument": row["instrument"], "base_family": row["base_family"],
        "created_at": row["created_at"], "updated_at": row["updated_at"], "phase": phase, "pending": pending,
        "goal": values.get("goal"), "regime": values.get("regime"), "proposals": values.get("proposals") or [],
        "estimate": values.get("estimate"), "ranking": values.get("ranking") or [], "kept": values.get("kept") or [],
        "events": values.get("events") or [], "usage": values.get("usage") or {}, "error": values.get("error"),
        "job": None if job is None else {k: job[k] for k in ("id", "status", "progress", "message", "error")},
    }


def list_sessions(limit: int = 50) -> list[dict[str, Any]]:
    rows = connection().execute(
        "SELECT id, title, instrument, created_at, updated_at FROM research_sessions ORDER BY updated_at DESC LIMIT ?",
        [max(1, min(limit, 200))],
    ).fetchall()
    return [dict(r) for r in rows]
