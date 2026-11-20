"""Downloading history for many instruments within Upstox's rate limits (P3.3).

A universe of 200 stocks is a few hundred requests of daily bars, but
thousands of 1-minute ones, and Upstox allows 1,900 requests per 30 minutes.
So a prefetch is planned before it runs: for each instrument, the sessions the
cache cannot yet vouch for, the requests they need, and how long the rate
limits make that take. The plan is what the UI shows before anything starts.

Running a prefetch is `cache.ensure` per instrument, which records every
session it settles in the ledger. That is what makes a prefetch resumable: run
the same one again after a cancel, a crash or a closed laptop, and the plan
only contains what is still missing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel

from backend.appstate.jobs import JobCancelled
from backend.data import cache
from backend.data.upstox_client import CHUNK_DAYS, EARLIEST, LIMITS, Timeframe
from backend.scan.spec import Universe

Progress = Callable[[float, str], None]

# Seconds per request when the limits are not what binds (network and server).
CALL_LATENCY = 0.35


class PrefetchParams(BaseModel):
    universe: Universe
    # The two bases the cache downloads; every other timeframe is derived locally.
    timeframe: Literal["1d", "1m"] = "1d"
    start: date | None = None
    end: date | None = None


def estimate_seconds(calls: int, limits=LIMITS, latency: float = CALL_LATENCY) -> float:
    """Lower bound on how long `calls` requests take under the sliding-window limits.

    Each window lets `max_calls` through per `window` seconds, so n requests need
    at least floor((n - 1) / max_calls) full windows of waiting; requests are
    also sequential, so never faster than n x latency.
    """
    if calls <= 0:
        return 0.0
    waits = max(((calls - 1) // max_calls) * window for max_calls, window in limits)
    return float(max(waits, calls * latency))


def binding_limit(calls: int, limits=LIMITS) -> str | None:
    """The rate limit that sets the pace, in words; None when none forces a wait."""
    waits = [(((calls - 1) // max_calls) * window, max_calls, window) for max_calls, window in limits]
    wait, max_calls, window = max(waits)
    if calls <= 0 or wait <= calls * CALL_LATENCY:
        return None                    # the network, not a limit, sets the pace
    unit = f"{int(window // 60)} min" if window >= 60 else f"{int(window)} s"
    return f"{max_calls} requests per {unit}"


def _span(base: Timeframe, start: date | None, end: date | None, today: date | None) -> tuple[date, date]:
    end = min(end or cache.last_completed_session(today), cache.last_completed_session(today))
    start = start or cache.default_start(base, end)
    return max(start, EARLIEST.get(base.unit, start)), end


def plan(params: PrefetchParams, *, today: date | None = None) -> dict[str, Any]:
    """What a prefetch would download: per instrument, and in total with a time estimate."""
    from backend.scan.service import resolve_universe

    base = Timeframe.parse(params.timeframe)
    start, end = _span(base, params.start, params.end, today)
    span = CHUNK_DAYS[base.unit]
    rows: list[dict[str, Any]] = []
    for item in resolve_universe(params.universe):
        key, symbol = item["key"], item["symbol"]
        if not key:
            rows.append({"key": key, "symbol": symbol, "skip": f"Unknown instrument {symbol!r}"})
            continue
        if not cache.is_fetchable(key):
            rows.append({"key": key, "symbol": symbol, "skip": "Not a broker instrument; nothing to download."})
            continue
        missing = cache.missing_sessions(key, base, start, end, today) if start <= end else []
        ranges = cache._ranges(missing)
        calls = sum(-(-((b - a).days + 1) // span) for a, b in ranges)
        rows.append({"key": key, "symbol": symbol, "missing_sessions": len(missing), "calls": calls})

    calls = sum(r.get("calls", 0) for r in rows)
    return {
        "timeframe": base.label, "start": start.isoformat(), "end": end.isoformat(),
        "instruments": len(rows),
        "to_download": sum(1 for r in rows if r.get("calls")),
        "up_to_date": sum(1 for r in rows if "calls" in r and not r["calls"]),
        "skipped": sum(1 for r in rows if "skip" in r),
        "missing_sessions": sum(r.get("missing_sessions", 0) for r in rows),
        "calls": calls,
        "eta_seconds": round(estimate_seconds(calls)),
        "binding_limit": binding_limit(calls),
        "rows": rows,
    }


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    return f"{minutes} min" if minutes < 90 else f"{minutes // 60}h {minutes % 60:02d}m"


def run(
    params: PrefetchParams, *, on_progress: Progress | None = None,
    fetcher=None, today: date | None = None,
) -> dict[str, Any]:
    """Download what the plan says is missing, instrument by instrument.

    One instrument failing does not stop the rest; its error is in the result.
    `on_progress` may raise to cancel (the job runner's cancel), and is called
    between requests, so a cancel takes effect within one request.
    """
    if fetcher is None:
        ready, reason = cache.broker_ready()
        if not ready:
            raise RuntimeError(f"Upstox is not connected: {reason}")

    the_plan = plan(params, today=today)
    base = Timeframe.parse(params.timeframe)
    start, end = date.fromisoformat(the_plan["start"]), date.fromisoformat(the_plan["end"])
    total = max(1, the_plan["calls"])
    todo = [r for r in the_plan["rows"] if r.get("calls")]
    done_calls = 0
    began = time.monotonic()
    results = []

    def remaining_text(done: int) -> str:
        left = total - done
        planned = estimate_seconds(left)
        elapsed = time.monotonic() - began
        # After a few requests, the measured pace is the better guide when slower.
        measured = elapsed / done * left if done >= 20 else 0.0
        return f"about {_duration(max(planned, measured))} left" if left > 0 else "finishing"

    for n, row in enumerate(todo, start=1):
        before = done_calls

        def progress(fraction: float, _message: str, _before: int = before, _row=row, _n=n) -> None:
            calls_here = min(_row["calls"], round(fraction * _row["calls"]))
            done = _before + calls_here
            if on_progress is not None:
                on_progress(
                    min(0.999, done / total),
                    f"{_n} of {len(todo)}: {_row['symbol']} · {done}/{total} requests · {remaining_text(done)}",
                )

        try:
            report = cache.ensure(row["key"], base, start, end, fetcher=fetcher,
                                  on_progress=progress, today=today)
            results.append({"key": row["key"], "symbol": row["symbol"], "fetched": report.fetched,
                            "empty": report.empty, "failed": report.failed, "calls": report.calls,
                            "notes": report.notes})
            done_calls = before + max(report.calls, row["calls"])
        except JobCancelled:
            raise                      # a cancel stops the prefetch; the ledger keeps what is done
        except Exception as exc:  # noqa: BLE001 - one instrument must not end the prefetch
            results.append({"key": row["key"], "symbol": row["symbol"], "error": str(exc)})
            done_calls = before + row["calls"]

    if on_progress is not None:
        on_progress(1.0, f"Prefetch complete: {len(todo)} instrument(s), {done_calls} request(s)")
    return {
        "timeframe": the_plan["timeframe"], "start": the_plan["start"], "end": the_plan["end"],
        "planned_calls": the_plan["calls"], "instruments": len(todo),
        "up_to_date": the_plan["up_to_date"], "skipped": the_plan["skipped"],
        "sessions_fetched": sum(r.get("fetched", 0) for r in results),
        "sessions_failed": sum(r.get("failed", 0) for r in results),
        "errors": [r for r in results if "error" in r],
        "elapsed_seconds": round(time.monotonic() - began, 1),
        "results": results,
    }
