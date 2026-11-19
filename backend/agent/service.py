"""Builder conversations: start one, reply to a pause, read it, retry a cut-off turn.

Each turn (from one user input to the next pause or the end) runs as a
background job, because a turn can take minutes: a model call with thinking,
then possibly a data download and a backtest. The UI polls the conversation.

One turn at a time per conversation: a reply while a turn is running is
refused, rather than queued, so two turns can never interleave on one graph.

Graph state is checkpointed in its own SQLite file (LangGraph's tables), next
to the app database. A turn cut off by a crash or a cancel leaves the graph at
its last completed node; `retry` continues from there.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from backend.agent import graph as builder
from backend.appstate import strategies as saved
from backend.appstate.db import connection, transaction
from backend.appstate.jobs import TERMINAL, JobContext
from backend.core.config import IST, settings
from backend.llm.base import LLMClient
from backend.services import backtest as backtest_service
from backend.services import jobs as job_services

CHECKPOINT_FILE = "agent_checkpoints.sqlite"
MAX_INPUT_CHARS = 20_000

NODE_LABELS = {
    "compile": "Reading the strategy", "clarify": "Noting your answers",
    "validate": "Checking the document", "repair": "Fixing validation problems",
    "readback": "Preparing the read-back", "save": "Saving the version",
    "backtest": "Running the backtest", "explain": "Explaining the results",
    "followup": "Done", "refine": "Applying the change", "give_up": "Stopping",
}


class ConversationNotFound(KeyError):
    pass


class TurnInProgress(RuntimeError):
    pass


class InvalidReply(ValueError):
    pass


def _default_llm() -> LLMClient:
    from backend.llm.factory import get_client

    return get_client()


_llm_factory: Callable[[], LLMClient] = _default_llm
_graph = None
_graph_path = None
_graph_lock = threading.Lock()


def set_llm_factory(factory: Callable[[], LLMClient] | None) -> None:
    """Swap the model (tests use a scripted fake). None restores the default."""
    global _llm_factory
    _llm_factory = factory or _default_llm


def _save(document: dict[str, Any], strategy_id: str | None) -> tuple[str, int]:
    version, _ = saved.save(document, strategy_id=strategy_id, origin="agent")
    return version.strategy_id, version.version


def _run_backtest(document: dict[str, Any], params: dict[str, Any], strategy_id: str | None) -> dict:
    request = backtest_service.BacktestParams.model_validate(
        builder.default_backtest_params(document, params)
    )
    return job_services.backtest_and_record(request, strategy_id=strategy_id)


# The running turn's progress hook, so a sweep inside a turn reports each
# variant (and honours a cancel between variants) like the sweep job does.
_turn_progress: ContextVar[Callable[[float, str], None] | None] = ContextVar(
    "turn_progress", default=None
)


def _run_sweep(document: dict[str, Any], params: dict[str, Any], strategy_id: str | None) -> dict:
    settings_only = builder.default_backtest_params(document, params)
    settings_only.pop("strategy", None)
    return job_services.sweep_and_record(
        document, settings_only, strategy_id=strategy_id, on_progress=_turn_progress.get(),
    )


def graph():
    """The compiled graph, bound to the current data directory's checkpoint file."""
    global _graph, _graph_path
    path = settings.data_dir / CHECKPOINT_FILE
    with _graph_lock:
        if _graph is None or _graph_path != path:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            path.chmod(0o600)
            _graph = builder.build(
                lambda: _llm_factory(), _run_backtest, _save,
                checkpointer=SqliteSaver(conn), run_sweep=_run_sweep,
            )
            _graph_path = path
    return _graph


def _config(conversation_id: str) -> dict:
    return {"configurable": {"thread_id": conversation_id}}


def _now() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def start(text: str, backtest: dict[str, Any] | None = None) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise InvalidReply("Describe the strategy first.")
    if len(text) > MAX_INPUT_CHARS:
        raise InvalidReply(f"That is longer than {MAX_INPUT_CHARS:,} characters; trim it.")

    conversation_id = f"conv_{secrets.token_hex(6)}"
    title = text.splitlines()[0][:80]
    now = _now()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?,?,?,?)",
            [conversation_id, title, now, now],
        )
    initial = {
        "dialogue": [{"role": "user", "content": text}],
        "transcript": [builder._event("user", "text", text)],
        "phase": "compiling", "mode": "compile", "questions_asked": 0, "repairs": 0,
        "usage": builder._fresh_usage(), "backtest": backtest or {},
        "document": None, "strategy_id": None, "run": None,
    }
    _submit(conversation_id, {"input": initial})
    return view(conversation_id)


def reply(conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    _require_idle(conversation_id)
    pending = _pending(conversation_id)
    if pending is None:
        raise InvalidReply("Nothing is waiting for a reply in this conversation.")
    _check_reply(pending, payload)
    _submit(conversation_id, {"resume": payload})
    return view(conversation_id)


def retry(conversation_id: str) -> dict[str, Any]:
    """Continue a turn that was cut off (crash, cancel, failed job)."""
    _require_idle(conversation_id)
    state = graph().get_state(_config(conversation_id))
    if _pending(conversation_id) is not None or not state.next:
        raise InvalidReply("There is no unfinished turn to continue.")
    _submit(conversation_id, {"continue": True})
    return view(conversation_id)


def _check_reply(pending: dict[str, Any], payload: dict[str, Any]) -> None:
    kind = pending.get("type")
    if kind == "questions":
        answers = payload.get("answers")
        if isinstance(answers, list) and answers and all(isinstance(a, str) and a.strip() for a in answers):
            return
        if isinstance(payload.get("text"), str) and payload["text"].strip():
            return
        raise InvalidReply("Answer the questions (answers: [...]) or reply in text.")
    if kind == "approval":
        action = payload.get("action")
        if action in ("approve", "reject"):
            return
        if action == "change" and str(payload.get("text", "")).strip():
            return
        if action == "edit" and isinstance(payload.get("document"), dict):
            return
        raise InvalidReply("Reply with action approve, reject, change (with text) or edit (with document).")
    if kind == "followup":
        text = str(payload.get("text", "")).strip()
        if text and len(text) <= MAX_INPUT_CHARS:
            return
        raise InvalidReply("Describe the change you want.")
    raise InvalidReply(f"Unknown pause type {kind!r}.")


def _submit(conversation_id: str, params: dict[str, Any]) -> None:
    job = job_services.runner.submit("agent_turn", {"conversation_id": conversation_id, **params})
    with transaction() as conn:
        conn.execute(
            "UPDATE conversations SET job_id = ?, updated_at = ? WHERE id = ?",
            [job["id"], _now(), conversation_id],
        )


def _row(conversation_id: str):
    row = connection().execute(
        "SELECT * FROM conversations WHERE id = ?", [conversation_id]
    ).fetchone()
    if row is None:
        raise ConversationNotFound(conversation_id)
    return row


def _job(row) -> dict[str, Any] | None:
    if not row["job_id"]:
        return None
    from backend.appstate.jobs import JobNotFound

    try:
        return job_services.runner.get(row["job_id"])
    except JobNotFound:
        return None


def _require_idle(conversation_id: str) -> None:
    job = _job(_row(conversation_id))
    if job is not None and job["status"] not in TERMINAL:
        raise TurnInProgress("The previous turn is still running.")


def _pending(conversation_id: str) -> dict[str, Any] | None:
    state = graph().get_state(_config(conversation_id))
    for task in state.tasks:
        for item in task.interrupts:
            return item.value
    return None


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

def run_turn(params: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    conversation_id = params["conversation_id"]
    if "input" in params:
        command: Any = params["input"]
    elif "resume" in params:
        command = Command(resume=params["resume"])
    else:
        command = None                     # continue from the last checkpoint

    steps = 0
    token = _turn_progress.set(ctx.progress)
    try:
        for update in graph().stream(command, _config(conversation_id), stream_mode="updates"):
            for node in update:
                if node.startswith("__"):
                    continue
                steps += 1
                ctx.progress(1 - 0.6 ** steps, NODE_LABELS.get(node, node))
    finally:
        _turn_progress.reset(token)

    values = graph().get_state(_config(conversation_id)).values
    with transaction() as conn:
        conn.execute(
            "UPDATE conversations SET strategy_id = COALESCE(?, strategy_id), updated_at = ? WHERE id = ?",
            [values.get("strategy_id"), _now(), conversation_id],
        )
    return {"conversation_id": conversation_id, "phase": view(conversation_id)["phase"]}


job_services.runner.register("agent_turn", run_turn)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

_PAUSE_PHASE = {
    "questions": "awaiting_answers", "approval": "awaiting_approval", "followup": "awaiting_followup",
}


def view(conversation_id: str) -> dict[str, Any]:
    row = _row(conversation_id)
    state = graph().get_state(_config(conversation_id))
    values = state.values or {}
    pending = _pending(conversation_id)
    job = _job(row)

    if job is not None and job["status"] not in TERMINAL:
        phase = "working"
    elif pending is not None:
        phase = _PAUSE_PHASE.get(str(pending.get("type")), "waiting")
    elif values.get("phase") in ("failed", "unsupported", "rejected"):
        phase = values["phase"]
    elif state.next:
        phase = "interrupted"              # a turn stopped part-way; retry continues it
    else:
        phase = values.get("phase") or "idle"

    from backend.llm.budget import Budget

    limits = Budget.from_settings()
    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "strategy_id": values.get("strategy_id") or row["strategy_id"],
        "version": values.get("version"),
        "phase": phase,
        "pending": pending,
        "transcript": values.get("transcript") or [],
        "document": values.get("document"),
        "assumptions": values.get("assumptions") or [],
        "run": values.get("run"),
        "sweep": values.get("sweep"),
        "error": values.get("error"),
        "usage": {
            **(values.get("usage") or {}),
            "limits": {"steps": limits.max_steps, "tokens": limits.max_tokens,
                       "seconds": limits.max_seconds},
        },
        "job": None if job is None else {
            k: job[k] for k in ("id", "status", "progress", "message", "error")
        },
        "can_retry": phase == "interrupted",
    }


def list_conversations(limit: int = 50) -> list[dict[str, Any]]:
    rows = connection().execute(
        "SELECT id, title, strategy_id, created_at, updated_at FROM conversations "
        "ORDER BY updated_at DESC LIMIT ?", [max(1, min(limit, 200))],
    ).fetchall()
    return [dict(r) for r in rows]
