"""Background jobs: long work off the request thread, with progress and cancel.

A job is a function that takes a `JobHandle` and returns a JSON-able result.
It reports progress through the handle and checks it for cancellation;
cancellation is cooperative, at the points the job chooses (between chunks of
a download, every few hundred bars of a backtest).

Job state lives in the app database, so it can be read from any request and
survives a restart as a record. The work itself runs on a small thread pool in
this process: jobs that were running when the process stopped are marked
failed at the next start rather than left "running" forever.

`JobRunner` is the interface callers use; a hosted version can swap in a task
queue behind the same methods (plan §1, "Scope", rule 4).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from backend.app.context import AppContext
from backend.app.db import get_app_db, now_iso, to_json

log = logging.getLogger(__name__)

TERMINAL = frozenset({"done", "failed", "cancelled"})

# Progress is written to the database at most this often, per job.
PROGRESS_INTERVAL_S = 0.25


class JobCancelled(Exception):
    """Raised inside a job when cancellation has been requested."""


@dataclass(frozen=True)
class JobRecord:
    id: str
    kind: str
    status: str
    progress: float
    message: str
    eta_seconds: float | None
    params: dict[str, Any]
    result: Any
    error: str | None
    cancel_requested: bool
    created_at: str
    started_at: str | None
    finished_at: str | None

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class JobHandle:
    """What a running job sees: progress reporting and the cancel flag."""

    def __init__(self, runner: JobRunner, job_id: str, ctx: AppContext) -> None:
        self._runner = runner
        self.job_id = job_id
        self.ctx = ctx
        self._last_write = 0.0
        self._started = time.monotonic()

    @property
    def cancelled(self) -> bool:
        return self._runner._cancel_flag(self.job_id).is_set()

    def check(self) -> None:
        """Raise JobCancelled if the user asked to stop."""
        if self.cancelled:
            raise JobCancelled(self.job_id)

    def progress(self, fraction: float, message: str = "", *, force: bool = False) -> None:
        """Report progress in [0, 1]. Also a cancellation point."""
        self.check()
        fraction = min(max(fraction, 0.0), 1.0)
        now = time.monotonic()
        if not force and now - self._last_write < PROGRESS_INTERVAL_S:
            return
        self._last_write = now
        elapsed = now - self._started
        eta = elapsed * (1 - fraction) / fraction if fraction > 0.02 else None
        with get_app_db(self.ctx).connect() as conn:
            conn.execute(
                "UPDATE jobs SET progress = ?, message = ?, eta_seconds = ? WHERE id = ?",
                (fraction, message, eta, self.job_id),
            )


JobFn = Callable[[JobHandle], Any]


class JobRunner:
    def __init__(self, max_workers: int = 2) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job")
        self._flags: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # -- submit / cancel --------------------------------------------------------

    def submit(
        self, kind: str, fn: JobFn, *, params: dict[str, Any] | None = None,
        ctx: AppContext | None = None,
    ) -> str:
        ctx = ctx or AppContext.local()
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        with get_app_db(ctx).connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, user_id, kind, status, params_json, created_at)"
                " VALUES (?, ?, ?, 'queued', ?, ?)",
                (job_id, ctx.user_id, kind, to_json(params or {}), now_iso()),
            )
        self._cancel_flag(job_id)
        self._pool.submit(self._execute, job_id, fn, ctx)
        return job_id

    def cancel(self, job_id: str, ctx: AppContext | None = None) -> JobRecord:
        """Ask a job to stop. A queued job never starts; a running one stops at its
        next cancellation point. Finished jobs are left as they are."""
        ctx = ctx or AppContext.local()
        record = self.get(job_id, ctx)
        if record.finished:
            return record
        self._cancel_flag(job_id).set()
        with get_app_db(ctx).connect() as conn:
            conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
        return self.get(job_id, ctx)

    # -- reads ------------------------------------------------------------------

    def get(self, job_id: str, ctx: AppContext | None = None) -> JobRecord:
        ctx = ctx or AppContext.local()
        with get_app_db(ctx).read() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?",
                               (job_id, ctx.user_id)).fetchone()
        if row is None:
            raise KeyError(f"No job {job_id!r}")
        return _job_record(row)

    def list_jobs(self, ctx: AppContext | None = None, active_only: bool = False,
                  limit: int = 50) -> list[JobRecord]:
        ctx = ctx or AppContext.local()
        query = "SELECT * FROM jobs WHERE user_id = ?"
        if active_only:
            query += " AND status IN ('queued', 'running')"
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        with get_app_db(ctx).read() as conn:
            return [_job_record(r) for r in conn.execute(query, (ctx.user_id, limit))]

    def wait(self, job_id: str, ctx: AppContext | None = None, timeout: float = 60.0) -> JobRecord:
        """Block until the job finishes (for CLIs and tests)."""
        deadline = time.monotonic() + timeout
        while True:
            record = self.get(job_id, ctx)
            if record.finished or time.monotonic() > deadline:
                return record
            time.sleep(0.05)

    # -- lifecycle --------------------------------------------------------------

    def recover_interrupted(self, ctx: AppContext | None = None) -> int:
        """Mark jobs left queued or running by a previous process as failed."""
        ctx = ctx or AppContext.local()
        with get_app_db(ctx).connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = 'failed', error = 'interrupted: the app stopped "
                "while this job was running', finished_at = ? "
                "WHERE user_id = ? AND status IN ('queued', 'running')",
                (now_iso(), ctx.user_id),
            )
            return cursor.rowcount

    def shutdown(self) -> None:
        """Cancel everything and stop the pool (API shutdown)."""
        with self._lock:
            for flag in self._flags.values():
                flag.set()
        self._pool.shutdown(wait=True, cancel_futures=True)

    # -- internals --------------------------------------------------------------

    def _cancel_flag(self, job_id: str) -> threading.Event:
        with self._lock:
            return self._flags.setdefault(job_id, threading.Event())

    def _execute(self, job_id: str, fn: JobFn, ctx: AppContext) -> None:
        db = get_app_db(ctx)
        handle = JobHandle(self, job_id, ctx)
        try:
            if handle.cancelled:
                raise JobCancelled(job_id)
            with db.connect() as conn:
                conn.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
                             (now_iso(), job_id))
            result = fn(handle)
            with db.connect() as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'done', progress = 1, eta_seconds = 0, "
                    "result_json = ?, finished_at = ? WHERE id = ?",
                    (to_json(result), now_iso(), job_id),
                )
        except JobCancelled:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'cancelled', finished_at = ?, eta_seconds = NULL "
                    "WHERE id = ?", (now_iso(), job_id))
        except Exception as exc:
            log.exception("Job %s failed", job_id)
            detail = getattr(exc, "detail", None)
            message = detail if isinstance(detail, str) else f"{type(exc).__name__}: {exc}"
            with db.connect() as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                    (message[:4000], now_iso(), job_id))
        finally:
            with self._lock:
                self._flags.pop(job_id, None)


def _job_record(row) -> JobRecord:
    return JobRecord(
        id=row["id"], kind=row["kind"], status=row["status"], progress=row["progress"],
        message=row["message"], eta_seconds=row["eta_seconds"],
        params=json.loads(row["params_json"]),
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        error=row["error"], cancel_requested=bool(row["cancel_requested"]),
        created_at=row["created_at"], started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


_runner: JobRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> JobRunner:
    """The process-wide job runner."""
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = JobRunner()
        return _runner


def shutdown_runner() -> None:
    """Cancel running jobs and stop the pool; the next get_runner() starts afresh."""
    global _runner
    with _runner_lock:
        runner, _runner = _runner, None
    if runner is not None:
        runner.shutdown()
