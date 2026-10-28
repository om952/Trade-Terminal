"""App sessions and the session data cache (plan §4.1 to §4.3).

An app session runs from when the app starts until the trader quits, or until
no UI has been connected for `idle_minutes` with no paper session running.
Market data downloaded during the session goes into a scratch store,
data/scratch/<session id>/, shared by every strategy and conversation in that
session. When the session ends, the scratch store is:

- **kept**: merged into the local library and deleted;
- **discarded**: deleted outright (rm -rf, so "the data is gone" is checkable);
- **pending**: left in place until the trader decides, when the preference is
  "ask" and nobody could be asked (an idle timeout, a shutdown from the
  terminal, a crash). Pending sessions are offered at the next start.

With the preference set to "keep", downloads go straight into the library and
there is nothing to promote at the end.

Promotion is resumable: each series is merged into the library (atomically,
see backend/data/candles.py) and only then removed from scratch, so an
interrupted promotion leaves the remaining series in scratch, still pending.

Command-line tools never start an app session: their downloads are deliberate,
and go to the library.
"""

from __future__ import annotations

import logging
import shutil
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Literal

from backend.app import preferences
from backend.app.context import AppContext
from backend.app.db import get_app_db
from backend.core.config import IST
from backend.data.candles import CandleStore, library_store

log = logging.getLogger(__name__)

Decision = Literal["keep", "discard"]
EndCause = Literal["quit", "idle", "shutdown", "crash"]


def scratch_store(session_id: str, ctx: AppContext) -> CandleStore:
    return CandleStore(ctx.data_dir / "scratch" / session_id, f"scratch:{session_id}", ctx)


class AppSessions:
    """The app sessions of one user. The API process holds one of these."""

    def __init__(self, ctx: AppContext | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        self.ctx = ctx or AppContext.local()
        self.current_id: str | None = None
        self._now = now or (lambda: datetime.now(IST))
        self._lock = threading.RLock()

    # -- lifecycle --------------------------------------------------------------

    def begin(self) -> str:
        """Start a new app session (after settling any a previous process left open)."""
        with self._lock:
            if self.current_id is not None:
                return self.current_id
            self.recover_leftovers()
            session_id = f"as_{self._now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
            stamp = self._stamp()
            with get_app_db(self.ctx).connect() as conn:
                conn.execute(
                    "INSERT INTO app_sessions (id, user_id, started_at, last_seen_at)"
                    " VALUES (?, ?, ?, ?)", (session_id, self.ctx.user_id, stamp, stamp))
            self.current_id = session_id
            log.info("App session %s started", session_id)
            return session_id

    def heartbeat(self) -> str:
        """The UI is connected. Starts a new session if the last one went idle."""
        with self._lock:
            session_id = self.current_id or self.begin()
            with get_app_db(self.ctx).connect() as conn:
                conn.execute("UPDATE app_sessions SET last_seen_at = ? WHERE id = ?",
                             (self._stamp(), session_id))
            return session_id

    def end(self, cause: EndCause, decision: Decision | None = None,
            remember: bool = False) -> dict[str, Any]:
        """End the current session and settle its data. Returns what happened."""
        with self._lock:
            session_id = self.current_id
            if session_id is None:
                return {"session_id": None, "outcome": None}
            self.current_id = None
            if remember and decision is not None:
                preferences.set("data_retention", decision, self.ctx)
            outcome = self._settle(session_id, decision)
            with get_app_db(self.ctx).connect() as conn:
                conn.execute(
                    "UPDATE app_sessions SET ended_at = ?, end_cause = ? WHERE id = ?",
                    (self._stamp(), cause, session_id))
            log.info("App session %s ended (%s): data %s", session_id, cause, outcome)
            return {"session_id": session_id, "outcome": outcome}

    def check_idle(self, paper_running: bool) -> bool:
        """End the session if no UI has been seen for `idle_minutes` and no paper
        session is running. Returns True if it ended."""
        with self._lock:
            if self.current_id is None or paper_running:
                return False
            minutes = preferences.get("idle_minutes", self.ctx)
            with get_app_db(self.ctx).read() as conn:
                row = conn.execute("SELECT last_seen_at FROM app_sessions WHERE id = ?",
                                   (self.current_id,)).fetchone()
            last_seen = datetime.fromisoformat(row["last_seen_at"])
            if self._now() - last_seen < timedelta(minutes=minutes):
                return False
            self.end("idle")
            return True

    def recover_leftovers(self) -> list[dict[str, Any]]:
        """Sessions a previous process never ended (it crashed): mark them, and
        settle their data by the remembered preference, or leave it pending."""
        with get_app_db(self.ctx).read() as conn:
            open_rows = conn.execute(
                "SELECT id FROM app_sessions WHERE user_id = ? AND ended_at IS NULL",
                (self.ctx.user_id,)).fetchall()
        settled = []
        for row in open_rows:
            if row["id"] == self.current_id:
                continue
            outcome = self._settle(row["id"], None)
            with get_app_db(self.ctx).connect() as conn:
                conn.execute("UPDATE app_sessions SET ended_at = ?, end_cause = 'crash' WHERE id = ?",
                             (self._stamp(), row["id"]))
            settled.append({"session_id": row["id"], "outcome": outcome})
        return settled

    # -- pending decisions ------------------------------------------------------

    def pending(self) -> list[dict[str, Any]]:
        """Ended sessions whose downloaded data is waiting for keep or discard."""
        with get_app_db(self.ctx).read() as conn:
            rows = conn.execute(
                "SELECT id, started_at, ended_at, end_cause FROM app_sessions"
                " WHERE user_id = ? AND outcome = 'pending' ORDER BY started_at",
                (self.ctx.user_id,)).fetchall()
        return [{**dict(r), **self._contents(r["id"])} for r in rows]

    def resolve(self, session_id: str, decision: Decision, remember: bool = False) -> dict[str, Any]:
        with get_app_db(self.ctx).read() as conn:
            row = conn.execute("SELECT outcome FROM app_sessions WHERE id = ? AND user_id = ?",
                               (session_id, self.ctx.user_id)).fetchone()
        if row is None:
            raise KeyError(f"No app session {session_id!r}")
        if row["outcome"] != "pending":
            return {"session_id": session_id, "outcome": row["outcome"]}
        if remember:
            preferences.set("data_retention", decision, self.ctx)
        return {"session_id": session_id, "outcome": self._settle(session_id, decision)}

    # -- stores -----------------------------------------------------------------

    def stores(self) -> tuple[list[CandleStore], CandleStore]:
        """(stores to read, store to write) for downloads right now."""
        library = library_store(self.ctx)
        with self._lock:
            session_id = self.current_id
        if session_id is None:
            return [library], library
        scratch = scratch_store(session_id, self.ctx)
        if preferences.get("data_retention", self.ctx) == "keep":
            return [library, scratch], library
        return [library, scratch], scratch

    def info(self) -> dict[str, Any]:
        """The current session, for the status bar and the quit dialog."""
        with self._lock:
            session_id = self.current_id
        out: dict[str, Any] = {
            "session_id": session_id,
            "data_retention": preferences.get("data_retention", self.ctx),
            "idle_minutes": preferences.get("idle_minutes", self.ctx),
            "pending": len(self.pending()),
        }
        if session_id is not None:
            out.update(self._contents(session_id))
        return out

    # -- internals --------------------------------------------------------------

    def _contents(self, session_id: str) -> dict[str, Any]:
        store = scratch_store(session_id, self.ctx)
        series = store.all_series()
        return {
            "scratch_bytes": sum(s.bytes for s in series),
            "series": [
                {"instrument_key": s.instrument_key, "timeframe": s.timeframe, "bars": s.rows,
                 "first_bar": s.first_bar, "last_bar": s.last_bar, "bytes": s.bytes}
                for s in series
            ],
        }

    def _settle(self, session_id: str, decision: Decision | None) -> str:
        """Apply keep/discard (or the preference) to a session's scratch data."""
        store = scratch_store(session_id, self.ctx)
        series = store.all_series()
        size = sum(s.bytes for s in series)
        choice = decision or preferences.get("data_retention", self.ctx)
        if not series:
            shutil.rmtree(store.root, ignore_errors=True)
            outcome = "discarded"
        elif choice == "keep":
            self._promote(store)
            outcome = "kept"
        elif choice == "discard":
            store.delete_all()
            outcome = "discarded"
        else:
            outcome = "pending"
        with get_app_db(self.ctx).connect() as conn:
            conn.execute("UPDATE app_sessions SET outcome = ?, scratch_bytes = ? WHERE id = ?",
                         (outcome, size, session_id))
        return outcome

    def _promote(self, store: CandleStore) -> None:
        library = library_store(self.ctx)
        for series in store.all_series():
            df = store.read(series.instrument_key, series.timeframe)
            library.write(series.instrument_key, series.timeframe, df, series.source)
            store.delete(series.instrument_key, series.timeframe)  # only after the merge landed
        store.delete_all()

    def _stamp(self) -> str:
        return self._now().isoformat()


_sessions: dict[tuple[str, str], AppSessions] = {}
_sessions_lock = threading.Lock()


def get_app_sessions(ctx: AppContext | None = None) -> AppSessions:
    ctx = ctx or AppContext.local()
    key = (ctx.user_id, str(ctx.data_dir))
    with _sessions_lock:
        if key not in _sessions:
            _sessions[key] = AppSessions(ctx)
        return _sessions[key]


def download_stores(ctx: AppContext | None = None) -> tuple[list[CandleStore], CandleStore]:
    """Where downloads are read from and written to right now: the library plus the
    current app session's scratch store when there is one."""
    return get_app_sessions(ctx).stores()

