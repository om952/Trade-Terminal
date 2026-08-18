"""Volume indicators: volume SMA, OBV, VWAP (session-anchored and rolling)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.indicators.base import (
    NAN,
    PriceBar,
    RollingWindow,
    StreamingIndicator,
)


# ---------------------------------------------------------------------------
# Volume moving average
# ---------------------------------------------------------------------------

def volume_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    return df["volume"].astype("float64").rolling(period).mean()


class VolumeSMA(StreamingIndicator):
    def __init__(self, period: int = 20) -> None:
        self._params = {"period": period}
        self._window = RollingWindow(period)

    def update(self, bar: PriceBar) -> float | None:
        self._window.push(bar.volume)
        return self._window.mean if self._window.full else None

    @property
    def ready(self) -> bool:
        return self._window.full


# ---------------------------------------------------------------------------
# Volume ratio — how unusual is this bar's volume
# ---------------------------------------------------------------------------

def volume_ratio(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Current volume divided by its rolling average. 2.0 means a volume spike."""
    vol = df["volume"].astype("float64")
    return vol / vol.rolling(period).mean().replace(0.0, np.nan)


class VolumeRatio(StreamingIndicator):
    def __init__(self, period: int = 20) -> None:
        self._params = {"period": period}
        self._window = RollingWindow(period)

    def update(self, bar: PriceBar) -> float | None:
        self._window.push(bar.volume)
        if not self._window.full:
            return None
        avg = self._window.mean
        return NAN if avg == 0 else bar.volume / avg

    @property
    def ready(self) -> bool:
        return self._window.full


# ---------------------------------------------------------------------------
# On-Balance Volume
# ---------------------------------------------------------------------------

def obv(df: pd.DataFrame) -> pd.Series:
    close = df["close"].astype("float64")
    vol = df["volume"].astype("float64")
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * vol).cumsum()


class OBV(StreamingIndicator):
    def __init__(self) -> None:
        self._params = {}
        self._value = 0.0
        self._prev: float | None = None

    def update(self, bar: PriceBar) -> float:
        if self._prev is not None:
            if bar.close > self._prev:
                self._value += bar.volume
            elif bar.close < self._prev:
                self._value -= bar.volume
        self._prev = bar.close
        return self._value

    @property
    def ready(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------

def vwap(df: pd.DataFrame, anchor: str = "session") -> pd.Series:
    """Volume-weighted average price.

    `anchor="session"` resets each trading day, which is what every Indian
    intraday trader means by VWAP. A cumulative-since-inception VWAP would drift
    uselessly far from price within weeks.
    """
    tp = (df["high"] + df["low"] + df["close"]).astype("float64") / 3.0
    vol = df["volume"].astype("float64")
    pv = tp * vol

    if anchor == "session":
        if "timestamp" not in df.columns:
            raise ValueError("session-anchored VWAP needs a 'timestamp' column")
        day = pd.to_datetime(df["timestamp"]).dt.date
        cum_pv = pv.groupby(day).cumsum()
        cum_vol = vol.groupby(day).cumsum()
    elif anchor == "cumulative":
        cum_pv, cum_vol = pv.cumsum(), vol.cumsum()
    else:
        raise ValueError(f"Unknown VWAP anchor {anchor!r}; use 'session' or 'cumulative'")

    return cum_pv / cum_vol.replace(0.0, np.nan)


class VWAP(StreamingIndicator):
    """Session-anchored VWAP.

    Call `new_session()` at each day boundary. The engine does this from the bar
    timestamp; the indicator itself deliberately holds no clock, so it stays
    testable without one.
    """

    def __init__(self) -> None:
        self._params = {}
        self._cum_pv = 0.0
        self._cum_vol = 0.0

    def new_session(self) -> None:
        self._cum_pv = 0.0
        self._cum_vol = 0.0

    def update(self, bar: PriceBar) -> float | None:
        self._cum_pv += bar.hlc3 * bar.volume
        self._cum_vol += bar.volume
        return None if self._cum_vol == 0 else self._cum_pv / self._cum_vol

    @property
    def ready(self) -> bool:
        return self._cum_vol > 0


def rolling_vwap(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """VWAP over a fixed lookback rather than a session."""
    tp = (df["high"] + df["low"] + df["close"]).astype("float64") / 3.0
    vol = df["volume"].astype("float64")
    pv_sum = (tp * vol).rolling(period).sum()
    vol_sum = vol.rolling(period).sum()
    return pv_sum / vol_sum.replace(0.0, np.nan)


class RollingVWAP(StreamingIndicator):
    def __init__(self, period: int = 20) -> None:
        self._params = {"period": period}
        self._pv = RollingWindow(period)
        self._vol = RollingWindow(period)

    def update(self, bar: PriceBar) -> float | None:
        self._pv.push(bar.hlc3 * bar.volume)
        self._vol.push(bar.volume)
        if not self._pv.full:
            return None
        vol_sum = sum(self._vol.values)
        return NAN if vol_sum == 0 else sum(self._pv.values) / vol_sum

    @property
    def ready(self) -> bool:
        return self._pv.full
