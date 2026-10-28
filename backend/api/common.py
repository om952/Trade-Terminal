"""Helpers shared by the API routers: candle loading, symbol resolution, payload
shaping and validation-error formatting. No trading logic lives here."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

import pandas as pd
from fastapi import HTTPException

from backend.app.context import AppContext
from backend.core.config import IST
from backend.data import instruments as inst
from backend.data.service import DataService

log = logging.getLogger(__name__)


# Chart payloads are capped so a multi-year 1-minute backtest does not try to
# ship a million candles to a browser that can only render a few thousand.
MAX_CHART_BARS = 4000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _downsample(df: pd.DataFrame, limit: int = MAX_CHART_BARS) -> pd.DataFrame:
    """Keep the most recent `limit` bars.

    Truncating the tail rather than thinning the series: a thinned candle chart
    shows highs and lows that never happened, and traders read those wicks.
    """
    return df.tail(limit) if len(df) > limit else df


def _epoch(ts) -> int:
    value = pd.Timestamp(ts)
    if value.tz is None:
        value = value.tz_localize(IST)
    return int(value.timestamp())


def _candles_payload(df: pd.DataFrame) -> list[dict]:
    return [
        {
            "time": _epoch(row.timestamp),
            "open": float(row.open), "high": float(row.high),
            "low": float(row.low), "close": float(row.close),
            "volume": int(row.volume),
        }
        for row in df.itertuples(index=False)
    ]


def _load_bars(
    symbol: str,
    timeframe: str,
    start: date | None,
    end: date | None,
    *,
    ctx: AppContext | None = None,
    fetch: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Bars for a range through the data service, downloading what's missing when
    `fetch` is on. Returns the bars and notes worth showing the user (a partial
    download, sessions that couldn't be fetched). 404 if there are no bars at all."""
    key = symbol if "|" in symbol else _resolve_or_400(symbol)
    service = DataService(ctx)
    df, plan, report = service.get_bars(key, timeframe, start, end, fetch=fetch,
                                        on_progress=on_progress)
    notes: list[str] = []
    if report.fetched_bars:
        notes.append(f"Downloaded {report.fetched_bars:,} {plan.fetch_timeframe} bars for {key}.")
    if report.errors:
        notes.append(f"Download incomplete: {report.errors[0]}")
    elif report.failed_ranges:
        spans = ", ".join(f"{a}..{b}" for a, b in report.failed_ranges[:3])
        notes.append(f"Some windows could not be downloaded ({spans}); results cover the rest.")
    if df.empty:
        if report.errors:
            detail = f"No candles for {key} {timeframe}, and the download failed: {report.errors[0]}"
        elif not plan.fetchable:
            detail = (f"No stored candles for {key} {timeframe}. It is a local-only series: "
                      f"generate simulated data or import a CSV.")
        else:
            detail = f"No candles for {key} {timeframe} in {plan.start}..{plan.end}."
        raise HTTPException(404, detail)
    return df, notes


def _load_candles(symbol: str, timeframe: str, start: date | None, end: date | None) -> pd.DataFrame:
    """Stored bars only (no download), for charts and replays."""
    df, _ = _load_bars(symbol, timeframe, start, end, fetch=False)
    return df


def _resolve_or_400(symbol: str) -> str:
    try:
        return inst.resolve(symbol)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc


def _format_validation_error(exc: Exception) -> list[dict]:
    """Turn a pydantic error into something a form can point at a field with."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return [{"field": "", "message": str(exc)}]

    out = []
    for err in errors():
        location = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
        message = err.get("msg", "")
        out.append({"field": location, "message": message.replace("Value error, ", "")})
    return out
