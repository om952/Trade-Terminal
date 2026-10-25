"""SessionManager — owns running paper sessions so the API can stay stateless.

One thread per session. The trading loop itself is single-threaded and blocking,
which is what makes it easy to reason about; this class exists so an HTTP request
can start one, look at it, and stop it without ever entering that loop.

The only shared state is the session's snapshot, which `PaperSession` guards with
its own lock. Nothing here reaches into engine or portfolio state directly — an
HTTP handler that could mutate a position mid-bar is a race waiting to happen.

Duplicate protection is by session id, which is derived from strategy hash +
instrument + day. Starting the same strategy on the same instrument twice in one
day returns the already-running session instead of a second one that would trade
the same signals again in ignorance of the first.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from typing import Any

from backend.core.config import IST
from backend.live import journal as J
from backend.live.session import PaperSession, SessionConfig, make_session_id

log = logging.getLogger(__name__)


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, PaperSession] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    # -- lifecycle -----------------------------------------------------------

    def start(
        self,
        config: SessionConfig,
        source=None,
        session_id: str | None = None,
        history_frame=None,
        history_before=None,
    ) -> tuple[PaperSession, bool]:
        """Start a session. Returns (session, started_now)."""
        sid = session_id or make_session_id(config.strategy, config.instrument_key)

        with self._lock:
            existing = self._sessions.get(sid)
            if existing is not None and self.is_running(sid):
                log.info("Session %s already running", sid)
                return existing, False

            session = PaperSession.create(
                config, source=source, session_id=sid,
                history_frame=history_frame, history_before=history_before
            )
            self._sessions[sid] = session

            thread = threading.Thread(
                target=self._run, args=(sid, session), name=f"session-{sid}", daemon=True
            )
            self._threads[sid] = thread
            thread.start()

        return session, True

    def _run(self, sid: str, session: PaperSession) -> None:
        try:
            session.run()
        except Exception:
            # `PaperSession.run` has already journaled, alerted and marked the
            # session failed. Re-raising off a daemon thread would only kill the
            # thread silently, so log and let the snapshot carry the bad news.
            log.exception("Session %s ended in error", sid)

    def stop(self, session_id: str, reason: str = "manual") -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return False
        session.stop(reason)
        thread = self._threads.get(session_id)
        if thread is not None:
            thread.join(timeout=15.0)
        return True

    def stop_all(self, reason: str = "shutdown") -> None:
        for sid in list(self._sessions):
            self.stop(sid, reason)

    # -- inspection ----------------------------------------------------------

    def get(self, session_id: str) -> PaperSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def is_running(self, session_id: str) -> bool:
        thread = self._threads.get(session_id)
        return bool(thread and thread.is_alive())

    def snapshot(self, session_id: str, include_bars: bool = False) -> dict[str, Any] | None:
        session = self.get(session_id)
        if session is None:
            return None
        payload = session.snapshot(include_bars=include_bars)
        payload["running"] = self.is_running(session_id)
        return payload

    def active(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(self._sessions)
        out = []
        for sid in ids:
            snapshot = self.snapshot(sid)
            if snapshot is None:
                continue
            out.append(
                {
                    "session_id": sid,
                    "running": snapshot["running"],
                    "state": snapshot["state"],
                    "strategy": snapshot["strategy"]["name"],
                    "symbol": snapshot["symbol"],
                    "bars_seen": snapshot["bars_seen"],
                    "trades": len(snapshot["trades"]),
                    "net_pnl": snapshot["net_pnl"],
                    "started_at": snapshot["started_at"],
                }
            )
        return out

    def interrupted(self, day: date | None = None) -> list[dict[str, Any]]:
        """Today's live-feed sessions that were cut off rather than stopped.

        A crash leaves no end record; an app shutdown or Ctrl-C leaves one that
        still counts as resumable. Either way the session is not running in this
        process, and starting the same strategy on the same instrument today
        lands on its journal and resumes it. Replays are not offered: re-running
        one is a fresh experiment, not a position to look after.

        This only lists them. Resuming is always the user's decision.
        """
        prefix = f"{(day or datetime.now(IST).date()):%Y%m%d}-"
        out = []
        for sid in J.sessions():
            if not sid.startswith(prefix) or self.is_running(sid):
                continue
            state = J.recover(sid)
            if not state.resumable or state.source_kind != "live":
                continue
            out.append(
                {
                    "session_id": sid,
                    "strategy_name": state.strategy.get("name", "?"),
                    "symbol": state.symbol,
                    "timeframe": state.timeframe,
                    "cause": "app shutdown" if state.ended else "crash",
                    "bars_seen": state.bars_seen,
                    "trades": len(state.trades),
                    "open_legs": state.open_legs,
                    "last_bar_at": state.last_bar_time.isoformat() if state.last_bar_time else None,
                    # Everything the start endpoint needs to land on this journal.
                    "resume": {
                        "strategy": state.strategy,
                        "symbol": state.symbol,
                        "starting_capital": float(state.starting_capital),
                        "lot_size": state.lot_size,
                        "source": "live",
                    },
                }
            )
        return out

    def history(self, limit: int = 25) -> list[dict[str, Any]]:
        """Past sessions read off disk, including ones this process never ran."""
        out = []
        for sid in J.sessions()[:limit]:
            if self.is_running(sid):
                continue
            state = J.recover(sid)
            if not state.started:
                continue
            out.append(
                {
                    "session_id": sid,
                    "running": False,
                    "state": "interrupted" if state.resumable else "ended",
                    "strategy": state.strategy.get("name", "?"),
                    "symbol": state.symbol,
                    "timeframe": state.timeframe,
                    "bars_seen": state.bars_seen,
                    "trades": len(state.trades),
                    "open_legs": state.open_legs,
                    "last_bar_at": state.last_bar_time.isoformat()
                    if state.last_bar_time else None,
                    "errors": state.errors[-3:],
                }
            )
        return out


manager = SessionManager()
