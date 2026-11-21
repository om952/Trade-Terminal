"""The account kill switch: flatten everything, and keep anything new from starting (P5.2).

Engaging it, from the app's header or `POST /api/kill-switch/engage`:

1. records the switch as engaged first (in `app_meta`, so it survives a
   restart), which makes `require_released()` refuse every new paper session
   and paper portfolio from that moment;
2. stops every running paper session with reason "kill_switch". The session
   closes its open positions at the last price on its own thread, before it
   writes its end record (`PaperSession._finish`), so the flatten cannot race
   the trading loop or land in a journal that has already closed;
3. stops every active paper portfolio session;
4. sends one alert.

Engaging again flattens again and keeps the first `engaged_at`: pressing it
twice is always safe. Only a person releases it (`POST /api/kill-switch/release`);
the MCP server cannot reach either route.

Live orders (Phase 5) will add cancelling open broker orders and exiting broker
positions here; paper sessions are all there is to flatten today.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from backend.appstate.db import connection, transaction
from backend.core.config import IST

log = logging.getLogger(__name__)

KEY = "kill_switch"
REASON = "kill_switch"          # the stop reason a flattened session records


class KillSwitchEngaged(ValueError):
    """Raised by anything that would start trading while the switch is engaged."""


def _now() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def status() -> dict[str, Any]:
    row = connection().execute("SELECT value FROM app_meta WHERE key = ?", [KEY]).fetchone()
    base = {"engaged": False, "engaged_at": None, "reason": None, "released_at": None}
    return {**base, **json.loads(row["value"])} if row is not None else base


def _save(state: dict[str, Any]) -> None:
    with transaction() as conn:
        conn.execute("INSERT INTO app_meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", [KEY, json.dumps(state)])


def require_released() -> None:
    state = status()
    if state["engaged"]:
        raise KillSwitchEngaged(
            f"The kill switch was engaged at {state['engaged_at']}"
            f"{' (' + state['reason'] + ')' if state.get('reason') else ''}: no new session can start "
            "until it is released."
        )


def engage(reason: str = "") -> dict[str, Any]:
    """Flatten and stop every paper session and portfolio. Safe to call again."""
    from backend.live.manager import manager
    from backend.portfolio import session as portfolios

    before = status()
    state = {
        "engaged": True,
        "engaged_at": before["engaged_at"] if before["engaged"] else _now(),
        "reason": (reason or before.get("reason") or "") if before["engaged"] else reason,
        "released_at": None,
    }
    _save(state)          # first, so nothing new can start while the rest runs
    log.warning("Kill switch engaged (%s)", reason or "no reason given")

    flattened: list[dict[str, Any]] = []
    for row in manager.active():
        if not row["running"]:
            continue
        sid = row["session_id"]
        session = manager.get(sid)
        if session is None:
            continue
        open_before = [p.leg_id for p in session.portfolio.open_positions]
        manager.stop(sid, REASON)          # joins the session thread, which flattens as it ends
        still_open = [p.leg_id for p in session.portfolio.open_positions]
        flattened.append({
            "session_id": sid, "strategy": row["strategy"], "symbol": row["symbol"],
            "positions_closed": len(open_before) - len(still_open), "still_open": still_open,
            "stopped": not manager.is_running(sid),
        })

    stopped_portfolios: list[dict[str, Any]] = []
    for p in portfolios.list_all():
        if p["status"] == portfolios.ACTIVE:
            portfolios.stop(p["id"], reason=f"kill switch{': ' + reason if reason else ''}")
            stopped_portfolios.append({"id": p["id"], "name": p["name"]})

    problems = [f for f in flattened if f["still_open"] or not f["stopped"]]
    report = {**state, "sessions": flattened, "portfolios": stopped_portfolios, "at": _now(),
              "complete": not problems}
    _alert(report)
    return report


def release() -> dict[str, Any]:
    state = status()
    if state["engaged"]:
        state = {**state, "engaged": False, "released_at": _now()}
        _save(state)
        log.warning("Kill switch released")
    return state


def _alert(report: dict[str, Any]) -> None:
    from backend.live.alerts import Alerts

    closed = sum(s["positions_closed"] for s in report["sessions"])
    lines = [f"🛑 Kill switch engaged{': ' + report['reason'] if report.get('reason') else ''}",
             f"{len(report['sessions'])} paper session(s) stopped, {closed} position(s) closed, "
             f"{len(report['portfolios'])} paper portfolio(s) stopped.",
             "No new session can start until it is released."]
    if not report["complete"]:
        lines.append("⚠️ Not everything closed: check the app.")
    Alerts().send("\n".join(lines))
