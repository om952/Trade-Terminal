"""Pivot levels: CPR, Classic, Fibonacci, Camarilla, and previous-period levels.

Pivots differ in shape from every other indicator here: they are computed from
the PREVIOUS session's OHLC and held constant across the current session. That
makes them session-aware rather than window-aware, so they carry their own
interface (`roll_session`) instead of pretending to be rolling indicators.

The lookahead trap this module exists to avoid: computing today's pivot from
today's high and low. Those are not known until the close, so a strategy using
them would be trading on information it could not have had. Every function here
derives levels from the prior completed session only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from backend.indicators.base import PriceBar


@dataclass(frozen=True, slots=True)
class PivotLevels:
    """One session's levels, derived from the previous session's OHLC."""

    pivot: float
    bc: float          # CPR bottom central
    tc: float          # CPR top central
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float
    prev_high: float
    prev_low: float
    prev_close: float

    @property
    def cpr_width(self) -> float:
        """TC - BC, normalized by the pivot.

        A narrow CPR is conventionally read as a coiled market likely to trend;
        a wide one as a range. This is the number strategies actually condition on.
        """
        return abs(self.tc - self.bc) / self.pivot if self.pivot else float("nan")

    def as_dict(self) -> dict[str, float]:
        return {
            "pivot": self.pivot, "bc": self.bc, "tc": self.tc,
            "r1": self.r1, "r2": self.r2, "r3": self.r3,
            "s1": self.s1, "s2": self.s2, "s3": self.s3,
            "cpr_width": self.cpr_width,
            "prev_high": self.prev_high, "prev_low": self.prev_low,
            "prev_close": self.prev_close,
        }


def compute_levels(
    high: float, low: float, close: float, method: str = "cpr"
) -> PivotLevels:
    """Derive levels from one completed session's OHLC.

    `method` selects the R/S ladder; CPR's pivot/BC/TC are always included since
    they cost nothing and Indian intraday strategies lean on them heavily.
    """
    pivot = (high + low + close) / 3.0
    rng = high - low

    # CPR central band. BC/TC are defined by which is larger, not by formula
    # order — swapping them would invert every "price above TC" condition.
    bc = (high + low) / 2.0
    tc = 2.0 * pivot - bc
    if tc < bc:
        tc, bc = bc, tc

    method = method.lower()
    if method in ("cpr", "classic", "standard"):
        r1, s1 = 2.0 * pivot - low, 2.0 * pivot - high
        r2, s2 = pivot + rng, pivot - rng
        r3, s3 = high + 2.0 * (pivot - low), low - 2.0 * (high - pivot)
    elif method == "fibonacci":
        r1, s1 = pivot + 0.382 * rng, pivot - 0.382 * rng
        r2, s2 = pivot + 0.618 * rng, pivot - 0.618 * rng
        r3, s3 = pivot + 1.000 * rng, pivot - 1.000 * rng
    elif method == "camarilla":
        r1, s1 = close + rng * 1.1 / 12.0, close - rng * 1.1 / 12.0
        r2, s2 = close + rng * 1.1 / 6.0, close - rng * 1.1 / 6.0
        r3, s3 = close + rng * 1.1 / 4.0, close - rng * 1.1 / 4.0
    else:
        raise ValueError(f"Unknown pivot method {method!r}")

    return PivotLevels(
        pivot=pivot, bc=bc, tc=tc,
        r1=r1, r2=r2, r3=r3, s1=s1, s2=s2, s3=s3,
        prev_high=high, prev_low=low, prev_close=close,
    )


def session_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse intraday bars into one OHLC row per session."""
    if "timestamp" not in df.columns:
        raise ValueError("Pivots need a 'timestamp' column to identify sessions")

    day = pd.to_datetime(df["timestamp"]).dt.date
    grouped = df.groupby(day, sort=True)
    out = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(),
        }
    )
    out.index.name = "session"
    return out


def pivots(df: pd.DataFrame, method: str = "cpr") -> pd.DataFrame:
    """Per-bar pivot levels derived from the previous session.

    The first session has no prior session, so its levels are NaN. That is
    correct rather than inconvenient — there is genuinely nothing to compute.
    """
    sessions = session_ohlc(df)
    prev = sessions.shift(1)

    rows: dict[date, dict[str, float]] = {}
    nan_row = {
        k: np.nan
        for k in (
            "pivot", "bc", "tc", "r1", "r2", "r3", "s1", "s2", "s3",
            "cpr_width", "prev_high", "prev_low", "prev_close",
        )
    }

    for session, row in prev.iterrows():
        if pd.isna(row["high"]):
            rows[session] = dict(nan_row)
        else:
            rows[session] = compute_levels(
                float(row["high"]), float(row["low"]), float(row["close"]), method
            ).as_dict()

    day = pd.to_datetime(df["timestamp"]).dt.date
    levels = pd.DataFrame([rows[d] for d in day], index=df.index)
    return levels


def previous_period_levels(df: pd.DataFrame, period: str = "day") -> pd.DataFrame:
    """Previous day/week/month high, low and close, carried across the period.

    Useful on its own: prior-day high and low are among the most-watched levels
    in Indian intraday trading.
    """
    if "timestamp" not in df.columns:
        raise ValueError("previous_period_levels needs a 'timestamp' column")

    ts = pd.to_datetime(df["timestamp"])
    if period == "day":
        key = ts.dt.date
    elif period == "week":
        key = ts.dt.to_period("W").astype(str)
    elif period == "month":
        key = ts.dt.to_period("M").astype(str)
    else:
        raise ValueError(f"Unknown period {period!r}; use day, week or month")

    grouped = df.groupby(key, sort=True)
    agg = pd.DataFrame(
        {"high": grouped["high"].max(), "low": grouped["low"].min(), "close": grouped["close"].last()}
    ).shift(1)

    mapped = agg.reindex(key.to_numpy())
    mapped.index = df.index
    return mapped.rename(
        columns={"high": f"prev_{period}_high", "low": f"prev_{period}_low",
                 "close": f"prev_{period}_close"}
    )


@dataclass
class StreamingPivots:
    """Session-aware streaming pivots.

    The engine calls `update` for every bar and `roll_session` when the date
    changes. Levels stay frozen within a session, which is the whole point —
    they must not drift as the current session's range extends.
    """

    method: str = "cpr"
    levels: PivotLevels | None = None
    _cur_high: float = field(default=float("-inf"), init=False)
    _cur_low: float = field(default=float("inf"), init=False)
    _cur_close: float = field(default=float("nan"), init=False)
    _has_session: bool = field(default=False, init=False)

    def update(self, bar: PriceBar) -> dict[str, float] | None:
        """Accumulate the current session's range; return the frozen levels."""
        self._cur_high = max(self._cur_high, bar.high)
        self._cur_low = min(self._cur_low, bar.low)
        self._cur_close = bar.close
        self._has_session = True
        return self.levels.as_dict() if self.levels else None

    def roll_session(self) -> None:
        """Close the current session and recompute levels for the next one."""
        if self._has_session and self._cur_high > float("-inf"):
            self.levels = compute_levels(
                self._cur_high, self._cur_low, self._cur_close, self.method
            )
        self._cur_high = float("-inf")
        self._cur_low = float("inf")
        self._cur_close = float("nan")
        self._has_session = False

    @property
    def ready(self) -> bool:
        return self.levels is not None
