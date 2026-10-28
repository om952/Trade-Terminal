"""Helpers shared by the API routers: candle loading, symbol resolution, payload
shaping and validation-error formatting. No trading logic lives here."""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
from fastapi import HTTPException

from backend.core.config import IST
from backend.data import instruments as inst
from backend.data.store import read_candles

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


def _load_candles(symbol: str, timeframe: str, start: date | None, end: date | None) -> pd.DataFrame:
    key = symbol if "|" in symbol else _resolve_or_400(symbol)
    df = read_candles(key, timeframe, start, end)
    if df.empty:
        raise HTTPException(
            404,
            f"No stored candles for {key} {timeframe}. "
            f"Fetch them first (POST /api/data/fetch) or generate synthetic data.",
        )
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
