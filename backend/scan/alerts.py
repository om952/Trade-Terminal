"""Scan alerts: run a saved scan on a schedule and report what changed.

A check runs the scan in "now" mode and compares each stock with the previous
check's snapshot. Only changes are reported:

* triggered:    the condition holds now and did not at the last check
* back_inside:  it held at the last check and does not now

A stock that could not be scanned this time (no data, an error) is not
reported as a change: missing data is not a market event. The first check of
a scan, or the first after its spec changed, records a baseline and sends one
summary instead of a message per stock.

A check whose latest bar is the same as the previous snapshot's records
nothing: there is no new session to compare. That makes repeated checks
harmless, so the scheduler can catch up after the app was closed without
sending anything twice.

Telegram is optional. Changes are recorded and shown in the app either way,
and an alert failure never fails the check (backend/live/alerts.py swallows
send errors by design).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, time, timedelta
from html import escape
from typing import Any

from backend.core.config import IST, settings
from backend.scan import engine, saved
from backend.scan import service as scan_service

log = logging.getLogger(__name__)

TRIGGERED = "triggered"
BACK_INSIDE = "back_inside"
ACTIVE = {engine.TRIGGERED_TODAY, engine.HOLDING}
SCANNED = {engine.TRIGGERED_TODAY, engine.HOLDING, engine.LAPSED, engine.WAITING}
MAX_LISTED = 20          # stocks named per section of one message


def states(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The part of each row a later check compares against, keyed by instrument."""
    return {
        r["key"] or r["symbol"]: {
            "symbol": r["symbol"], "status": r["status"], "active": r["status"] in ACTIVE,
            "since": r.get("since"), "close": r.get("close"), "distance_pct": r.get("distance_pct"),
        }
        for r in rows
    }


def diff(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Stocks that started or stopped holding between two snapshots."""
    out = []
    for key, now in sorted(after.items(), key=lambda kv: kv[1]["symbol"]):
        if now["status"] not in SCANNED:
            continue
        was = before.get(key)
        was_active = bool(was and was.get("status") in SCANNED and was.get("active"))
        if now["active"] and not was_active:
            kind = TRIGGERED
        elif was_active and not now["active"]:
            kind = BACK_INSIDE
        else:
            continue
        out.append({"key": key, "symbol": now["symbol"], "kind": kind, "detail": {
            "status": now["status"], "since": now.get("since"), "close": now.get("close"),
            "distance_pct": now.get("distance_pct"),
        }})
    return out


def _listed(events: list[dict[str, Any]]) -> str:
    def one(e: dict[str, Any]) -> str:
        d = e["detail"].get("distance_pct")
        gap = "" if d is None else f" ({d:+.1f}%)"
        return f"{escape(e['symbol'])}{gap}"

    names = [one(e) for e in events[:MAX_LISTED]]
    more = len(events) - MAX_LISTED
    return ", ".join(names) + (f" and {more} more" if more > 0 else "")


def message(name: str, as_of: str | None, events: list[dict[str, Any]], *,
            baseline: bool, active: int = 0, scanned: int = 0, side: str | None = None) -> str:
    """The Telegram text for one check. HTML-escaped throughout."""
    head = f"🔎 <b>{escape(name)}</b> · close of {escape(as_of or '?')}"
    if baseline:
        return f"{head}\nWatching {scanned} stocks; {active} meet the condition now. " \
               "You will hear about changes from the next session."
    lines = [head]
    up = [e for e in events if e["kind"] == TRIGGERED]
    down = [e for e in events if e["kind"] == BACK_INSIDE]
    arrow = "▼" if side == "short" else "▲"
    if up:
        lines.append(f"{arrow} Triggered: {_listed(up)}")
    if down:
        lines.append(f"↩︎ Back inside: {_listed(down)}")
    return "\n".join(lines)


def check(
    scan_id: str, *, job_id: str | None = None, on_progress=None, fetch: bool = True,
    send: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Run a saved scan, record a snapshot and its changes, and alert on them."""
    scan = saved.get(scan_id)
    result = scan_service.run(
        scan_service.ScanParams(spec=scan["document"], mode="now", fetch=fetch), on_progress=on_progress,
    )
    current = states(result["rows"])
    scanned = sum(1 for s in current.values() if s["status"] in SCANNED)
    active = sum(1 for s in current.values() if s["active"])
    previous = saved.latest_snapshot(scan_id)
    as_of = result.get("as_of")

    summary: dict[str, Any] = {"scan_id": scan_id, "version": scan["version"], "as_of": as_of,
                               "scanned": scanned, "active": active, "events": [], "sent": False}
    if previous and previous["version"] == scan["version"] and previous["as_of"] == as_of:
        summary["new_session"] = False
        return summary

    baseline = previous is None or previous["version"] != scan["version"]
    events = diff(previous["states"], current) if previous is not None and not baseline else []
    summary.update(new_session=True, baseline=baseline, events=events)
    summary["snapshot_id"] = saved.record_snapshot(
        scan_id, scan["version"], as_of, current, events, job_id,
    )
    if baseline or events:
        text = message(scan["name"], as_of, events, baseline=baseline, active=active,
                       scanned=scanned, side=scan["document"].get("side"))
        summary["message"] = text
        sender = send if send is not None else _telegram()
        summary["sent"] = bool(sender(text))
    return summary


def _telegram() -> Callable[[str], bool]:
    from backend.live.alerts import Alerts

    return Alerts().send


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

def parse_times(text: str) -> list[time]:
    out = []
    for part in text.split(","):
        part = part.strip()
        if part:
            hh, mm = part.split(":")
            out.append(time(int(hh), int(mm)))
    return sorted(out)


def last_slot(now: datetime, times: list[time]) -> datetime | None:
    """The most recent scheduled check time at or before `now` (today or yesterday)."""
    if not times:
        return None
    for day in (now.date(), now.date() - timedelta(days=1)):
        for t in reversed(times):
            slot = datetime.combine(day, t, tzinfo=IST)
            if slot <= now:
                return slot
    return None


def due(now: datetime, times: list[time], last_checked: str | None) -> bool:
    """Whether a check is owed: a slot has passed since the last check.

    Only the latest missed slot counts, so a laptop closed for a week runs one
    check when it opens, not one per missed slot.
    """
    slot = last_slot(now, times)
    if slot is None:
        return False
    return last_checked is None or datetime.fromisoformat(last_checked) < slot


class Scheduler:
    """The morning scheduler: submits a check job for each alerting scan, and a
    step job for each active paper portfolio, when one of their slots has passed."""

    def __init__(self, submit: Callable[[str, dict], Any], interval: float = 60.0,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._submit = submit
        self._interval = interval
        self._clock = clock or (lambda: datetime.now(IST))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> list[str]:
        """Check once; returns the scans a job was submitted for."""
        times = parse_times(settings.scan_alert_times)
        now = self._clock()
        owed = [s for s in saved.list_all() if s["alerts"] and due(now, times, s["last_checked_at"])] \
            if settings.scan_alerts_enabled else []
        for scan in owed:
            self._submit("scan_check", {"scan_id": scan["id"]})
        if owed:
            saved.mark_checked([s["id"] for s in owed], now.isoformat(timespec="seconds"))
        from backend.portfolio import session as portfolios

        steps = portfolios.owed(now, parse_times(settings.portfolio_step_times))
        for session_id in steps:
            self._submit("portfolio_step", {"session_id": session_id})
        if steps:
            portfolios.mark_scheduled(steps, now.isoformat(timespec="seconds"))
        return [s["id"] for s in owed] + steps

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the scheduler must outlive one bad tick
                log.exception("Scan alert scheduler tick failed")

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="scan-alerts", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
