"""Background jobs: long work off the request thread, with progress and cancel.

A data fetch can take minutes (Upstox allows about 2,000 calls per 30 minutes),
and a multi-year backtest tens of seconds. Running those inside an HTTP request
means no progress, no cancel, and a browser timeout that looks like a failure.
So the API submits a job, returns its id at once, and the UI polls it.

Jobs are persisted in SQLite. The process that runs a job is the only writer of
its row while it runs; the row is the source of truth for every reader. Jobs
still queued or running when the process died are marked "interrupted" at the
next start, never silently re-run: a re-run is the user's decision.

Cancellation is cooperative. A job function receives a `JobContext` and calls
`ctx.progress(...)`, which raises `JobCancelled` once a cancel was requested.
The backtest engine and the cache both report progress, so both stop promptly.

The MCP server (P3.5) and the research agent (P4) submit work through this same
runner, so there is one queue to watch and one place to enforce limits.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from backend.appstate.db import connection, to_json, transaction
from backend.core.config import IST

log = logging.getLogger(__name__)

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})

# Progress is written at most this often, plus on every terminal change.
PROGRESS_INTERVAL_S = 0.5

JobFn = Callable[[dict[str, Any], "JobContext"], dict[str, Any]]


class JobCancelled(Exception):
    """Raised inside a job when a cancel has been requested."""


class JobNotFound(KeyError):
    pass


class UnknownJobKind(ValueError):
    pass


def _now() -> str:
    return datetime.now(IST).isoformat(timespec="milliseconds")


class JobContext:
    """Handed to a running job: report progress, observe cancellation."""

    def __init__(self, job_id: str, cancel: threading.Event) -> None:
        self.job_id = job_id
        self._cancel = cancel
        self._last_write = 0.0
        self._last_fraction = -1.0

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled(self.job_id)

    def progress(self, fraction: float, message: str = "") -> None:
        """Record progress (0..1). Raises JobCancelled if a cancel was requested."""
        self.check()
        fraction = max(0.0, min(1.0, float(fraction)))
        now = time.monotonic()
        if now - self._last_write < PROGRESS_INTERVAL_S and fraction < 1.0:
            return
        # Progress never moves backwards on screen, even if a phase re-estimates.
        fraction = max(fraction, self._last_fraction)
        self._last_write, self._last_fraction = now, fraction
        connection().execute(
            "UPDATE jobs SET progress = ?, message = ? WHERE id = ? AND status = 'running'",
            [fraction, message[:300], self.job_id],
        )


class JobRunner:
    def __init__(self, workers: int = 2) -> None:
        self.workers = workers
        self._kinds: dict[str, JobFn] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None

    # -- setup ---------------------------------------------------------------

    def register(self, kind: str, fn: JobFn) -> None:
        self._kinds[kind] = fn

    @property
    def kinds(self) -> list[str]:
        return sorted(self._kinds)

    def start(self) -> int:
        """Start workers. Returns how many jobs a previous process left unfinished."""
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(self.workers, thread_name_prefix="job")
        with transaction() as conn:
            cur = conn.execute(
                """UPDATE jobs SET status = 'interrupted', finished_at = ?,
                   error = 'The app stopped before this job finished. Run it again if needed.'
                   WHERE status IN ('queued', 'running')""",
                [_now()],
            )
        if cur.rowcount:
            log.warning("%d job(s) from a previous run were interrupted", cur.rowcount)
        return cur.rowcount

    def shutdown(self, timeout: float = 15.0) -> None:
        """Cancel everything and wait (up to `timeout`) for running jobs to stop."""
        with self._lock:
            executor, self._executor = self._executor, None
            for event in self._cancels.values():
                event.set()
        if executor is None:
            return
        done = threading.Event()

        def _wait() -> None:
            executor.shutdown(wait=True, cancel_futures=True)
            done.set()

        threading.Thread(target=_wait, name="job-shutdown", daemon=True).start()
        if not done.wait(timeout):
            log.warning("Jobs still running after %.0fs; leaving them to the exit", timeout)
        # Anything still marked active never got to finish.
        with transaction() as conn:
            conn.execute(
                """UPDATE jobs SET status = 'cancelled', finished_at = ?, error = 'App shut down'
                   WHERE status IN ('queued', 'running')""",
                [_now()],
            )

    # -- submit and control --------------------------------------------------

    def submit(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        fn = self._kinds.get(kind)
        if fn is None:
            raise UnknownJobKind(f"Unknown job kind {kind!r}. Known: {', '.join(self.kinds)}")
        if self._executor is None:
            raise RuntimeError("Job runner is not started")

        job_id = f"job_{secrets.token_hex(6)}"
        with transaction() as conn:
            conn.execute(
                """INSERT INTO jobs (id, kind, status, params, created_at)
                   VALUES (?, ?, 'queued', ?, ?)""",
                [job_id, kind, to_json(params, sort_keys=True), _now()],
            )
        cancel = threading.Event()
        with self._lock:
            self._cancels[job_id] = cancel
            executor = self._executor
        if executor is None:
            raise RuntimeError("Job runner is shutting down")
        executor.submit(self._run, job_id, kind, fn, params, cancel)
        return self.get(job_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] in TERMINAL:
            return job
        with self._lock:
            event = self._cancels.get(job_id)
        if event is not None:
            event.set()
        # A queued job is cancelled now; a running one stops at its next progress call.
        with transaction() as conn:
            conn.execute(
                """UPDATE jobs SET status = 'cancelled', finished_at = ?, error = 'Cancelled'
                   WHERE id = ? AND status = 'queued'""",
                [_now(), job_id],
            )
        return self.get(job_id)

    def _run(
        self, job_id: str, kind: str, fn: JobFn, params: dict[str, Any], cancel: threading.Event
    ) -> None:
        with transaction() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
                [_now(), job_id],
            )
        if cur.rowcount == 0:          # cancelled while queued
            self._forget(job_id)
            return

        ctx = JobContext(job_id, cancel)
        status, result, error = "succeeded", None, None
        try:
            ctx.check()
            result = fn(params, ctx)
        except JobCancelled:
            status, error = "cancelled", "Cancelled"
        except Exception as exc:
            status, error = "failed", _describe(exc)
            log.exception("Job %s (%s) failed", job_id, kind)

        with transaction() as conn:
            conn.execute(
                """UPDATE jobs SET status = ?, result = ?, error = ?, finished_at = ?,
                   progress = CASE WHEN ? = 'succeeded' THEN 1.0 ELSE progress END
                   WHERE id = ?""",
                [status, to_json(result) if result is not None else None,
                 error, _now(), status, job_id],
            )
        log.info("Job %s (%s) %s", job_id, kind, status)
        self._forget(job_id)

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._cancels.pop(job_id, None)

    # -- read ----------------------------------------------------------------

    def get(self, job_id: str) -> dict[str, Any]:
        row = connection().execute("SELECT * FROM jobs WHERE id = ?", [job_id]).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        return _job(row)

    def list(self, limit: int = 50, kind: str | None = None) -> list[dict[str, Any]]:
        q, params = "SELECT * FROM jobs", []
        if kind:
            q += " WHERE kind = ?"
            params.append(kind)
        rows = connection().execute(
            q + " ORDER BY created_at DESC LIMIT ?", [*params, max(1, min(limit, 500))]
        ).fetchall()
        return [_job(r) for r in rows]

    def wait(self, job_id: str, timeout: float = 60.0, poll: float = 0.05) -> dict[str, Any]:
        """Block until a job reaches a terminal state (tests, CLI)."""
        deadline = time.monotonic() + timeout
        while True:
            job = self.get(job_id)
            if job["status"] in TERMINAL or time.monotonic() > deadline:
                return job
            time.sleep(poll)


def _describe(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return "; ".join(f"{e.get('field', '')}: {e.get('message', '')}" for e in errors)[:1000]
    return f"{exc}"[:1000] or type(exc).__name__


def _job(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "progress": round(row["progress"], 4),
        "message": row["message"],
        "params": json.loads(row["params"]),
        "result": json.loads(row["result"]) if row["result"] else None,
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }
