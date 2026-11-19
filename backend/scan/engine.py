"""Evaluating a scan on one instrument's bars.

Two questions, both answered from the same per-bar evaluation the backtest
uses (`conditions.evaluate` over a `VectorizedIndicatorSet`):

* now:     is the condition true on the latest bar, since when, and when did
           it last hold? This is the watchlist view.
* history: every time the condition started to hold, and what the price did
           over the next 1, 5 and 20 bars, next to what it did over the same
           horizons on every bar (the baseline). A signal is only interesting
           if its forward returns differ from the baseline's.

Forward returns are measured from the next bar's open, because the signal is
only known once the signal bar has closed. They carry no costs.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from backend.scan.spec import HORIZONS, ScanSpec
from backend.strategy import conditions
from backend.strategy.indicator_set import VectorizedIndicatorSet
from backend.strategy.runner import context_frame
from backend.strategy.schema import Comparator

# Status of an instrument on its latest bar.
TRIGGERED_TODAY = "triggered_today"   # became true on the latest bar
HOLDING = "holding"                   # true now, and became true earlier
LAPSED = "lapsed"                     # false now, true within the lookback
WAITING = "waiting"                   # not true anywhere in the lookback
NOT_ENOUGH_DATA = "not_enough_data"   # indicators never warm up on these bars

_ABOVE = (Comparator.GT, Comparator.GTE, Comparator.CROSSES_ABOVE)
_BELOW = (Comparator.LT, Comparator.LTE, Comparator.CROSSES_BELOW)


def prepare(spec: ScanSpec, frame: pd.DataFrame) -> tuple[VectorizedIndicatorSet, int]:
    """Indicators and context over the frame, and the first bar where all are valid."""
    indicators = VectorizedIndicatorSet(frame, spec.indicators, spec.inline(), spec.exprs())
    context = context_frame(spec.condition.referenced_ids(), frame)
    if context is not None:
        indicators.attach_context(context)
    return indicators, indicators.warmup_bars()


def fired(spec: ScanSpec, indicators: VectorizedIndicatorSet, start: int, end: int) -> np.ndarray:
    """Whether the condition holds on each bar in [start, end)."""
    out = np.zeros(max(0, end - start), dtype=bool)
    for n, i in enumerate(range(start, end)):
        indicators.seek(i)
        out[n] = conditions.evaluate(spec.condition, indicators)
    return out


def _day(frame: pd.DataFrame, i: int) -> str:
    return pd.Timestamp(frame["timestamp"].iloc[i]).date().isoformat()


def _close(frame: pd.DataFrame, i: int) -> float:
    return float(frame["close"].iloc[i])


def distance_pct(spec: ScanSpec, indicators: VectorizedIndicatorSet) -> float | None:
    """How far the latest bar is past (+) or short of (-) the line, in percent.

    Defined only when the condition is one comparison against a level: for
    "close > upper" it is (close - upper) / upper. A tree of several
    comparisons has no single line to measure against.
    """
    condition = spec.single_comparison()
    if condition is None or condition.cmp not in _ABOVE + _BELOW:
        return None
    left = conditions._operand_value(condition.left, indicators)
    right = conditions._operand_value(condition.right, indicators)
    if math.isnan(left) or math.isnan(right) or right == 0:
        return None
    gap = (left - right) / abs(right) * 100
    return round(gap if condition.cmp in _ABOVE else -gap, 4)


def scan_now(spec: ScanSpec, frame: pd.DataFrame) -> dict[str, Any]:
    """The latest bar's status, and the recent history behind it."""
    n = len(frame)
    if n == 0:
        return {"status": NOT_ENOUGH_DATA, "bars": 0}
    indicators, warmup = prepare(spec, frame)
    if warmup >= n:
        return {"status": NOT_ENOUGH_DATA, "bars": n, "warmup_bars": warmup}

    start = max(warmup, n - spec.lookback)
    window = fired(spec, indicators, start, n)
    last = n - 1
    indicators.seek(last)
    row: dict[str, Any] = {
        "as_of": _day(frame, last),
        "close": _close(frame, last),
        "bars": n,
        "active": bool(window[-1]),
        "distance_pct": distance_pct(spec, indicators),
        "checks": conditions.explain(spec.condition, indicators),
        "since": None, "since_close": None, "since_capped": False,
        "last_fired": None, "last_fired_close": None, "bars_since": None,
    }

    if window[-1]:
        # Walk back to where the current run of true bars began.
        k = len(window) - 1
        while k > 0 and window[k - 1]:
            k -= 1
        first = start + k
        row.update(
            status=TRIGGERED_TODAY if first == last else HOLDING,
            since=_day(frame, first), since_close=_close(frame, first),
            since_capped=(k == 0 and start > warmup),   # the run began before the lookback
            last_fired=row["as_of"], last_fired_close=row["close"], bars_since=0,
        )
        return row

    hits = np.flatnonzero(window)
    if hits.size:
        i = start + int(hits[-1])
        row.update(status=LAPSED, last_fired=_day(frame, i), last_fired_close=_close(frame, i),
                   bars_since=last - i)
    else:
        row["status"] = WAITING
    return row


def forward_returns(frame: pd.DataFrame, i: int, horizons=HORIZONS) -> dict[str, float | None]:
    """Percent return from the open after bar i to the close h bars after it."""
    n = len(frame)
    out: dict[str, float | None] = {}
    if i + 1 >= n:
        return {str(h): None for h in horizons}
    entry = float(frame["open"].iloc[i + 1])
    for h in horizons:
        j = i + h
        out[str(h)] = None if j >= n or entry <= 0 else round((_close(frame, j) / entry - 1) * 100, 6)
    return out


def scan_history(spec: ScanSpec, frame: pd.DataFrame, horizons=HORIZONS) -> dict[str, Any]:
    """Every onset of the condition after warm-up, with forward returns and a baseline.

    An onset is a bar where the condition holds and did not on the bar before,
    so a level held for 30 days is one event, not 30.
    """
    n = len(frame)
    indicators, warmup = prepare(spec, frame) if n else (None, 0)
    if n == 0 or warmup >= n - 1:
        return {"status": NOT_ENOUGH_DATA, "bars": n, "events": [], "baseline": {}}

    held = fired(spec, indicators, warmup, n)   # type: ignore[arg-type]
    onset = held & ~np.concatenate([[False], held[:-1]])
    events = []
    for k in np.flatnonzero(onset):
        i = warmup + int(k)
        events.append({"date": _day(frame, i), "close": _close(frame, i),
                       "forward": forward_returns(frame, i, horizons)})

    # Baseline: the same forward return from every bar after warm-up.
    opens = frame["open"].to_numpy(dtype="float64")
    closes = frame["close"].to_numpy(dtype="float64")
    baseline: dict[str, list[float]] = {}
    for h in horizons:
        idx = np.arange(warmup, n - h)
        entry = opens[idx + 1]
        ok = entry > 0
        baseline[str(h)] = list((closes[idx + h][ok] / entry[ok] - 1) * 100)
    return {
        "status": "ok", "bars": n, "warmup_bars": warmup,
        "first_bar": _day(frame, warmup), "last_bar": _day(frame, n - 1),
        "events": events, "baseline": baseline,
    }


def summarize(returns: list[float]) -> dict[str, float | int | None]:
    """Count, mean, median and share above zero of a list of percent returns."""
    if not returns:
        return {"count": 0, "mean": None, "median": None, "hit_rate": None}
    values = np.asarray(returns, dtype="float64")
    return {
        "count": int(values.size),
        "mean": round(float(values.mean()), 4),
        "median": round(float(np.median(values)), 4),
        "hit_rate": round(float((values > 0).mean() * 100), 2),
    }
