"""Indicator foundations: the dual-implementation contract and shared smoothing.

Every indicator in this package exists twice:

  * a **vectorized** function taking a DataFrame and returning a Series/DataFrame,
    used by backtests where the whole history is available at once;
  * a **streaming** class fed one bar at a time via `update()`, used live where
    the future genuinely does not exist yet.

They must agree to 1e-9. `backend/tests/test_indicators.py` asserts exactly that
for every registered indicator, and that test is the reason live trading can be
expected to behave the way the backtest said it would. If you add an indicator,
add it to the registry and the test will pick it up automatically.

Warm-up convention
------------------
Both forms return NaN/None until they have `period` observations, then seed from
the simple average of that first window. This is the TradingView/TA-Lib
convention. The alternative — seeding from the first value alone — makes the
first few hundred bars of a backtest depend on where you happened to start the
data, which quietly changes results when you extend the history.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections import deque
from typing import Iterable, NamedTuple

import numpy as np
import pandas as pd

NAN = float("nan")


class PriceBar(NamedTuple):
    """A bar reduced to the floats indicators actually need.

    Deliberately not `core.interfaces.Bar`: that one carries Decimal prices and a
    timestamp for correctness at the order layer, and converting Decimals inside
    a tight indicator loop is pure overhead.
    """

    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def hl2(self) -> float:
        return (self.high + self.low) / 2.0

    @property
    def hlc3(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def ohlc4(self) -> float:
        return (self.open + self.high + self.low + self.close) / 4.0


def bars_from_frame(df: pd.DataFrame) -> Iterable[PriceBar]:
    """Iterate a candle DataFrame as PriceBars, for streaming replay."""
    cols = ("open", "high", "low", "close", "volume")
    values = df[list(cols)].to_numpy(dtype="float64")
    for row in values:
        yield PriceBar(*row)


def source_series(df: pd.DataFrame, source: str = "close") -> pd.Series:
    """Resolve a named price source to a Series."""
    source = source.lower()
    if source in df.columns:
        return df[source].astype("float64")
    if source == "hl2":
        return (df["high"] + df["low"]) / 2.0
    if source == "hlc3":
        return (df["high"] + df["low"] + df["close"]) / 3.0
    if source == "ohlc4":
        return (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    raise ValueError(f"Unknown price source {source!r}")


def source_value(bar: PriceBar, source: str = "close") -> float:
    source = source.lower()
    if source == "close":
        return bar.close
    if source == "open":
        return bar.open
    if source == "high":
        return bar.high
    if source == "low":
        return bar.low
    if source == "volume":
        return bar.volume
    if source == "hl2":
        return bar.hl2
    if source == "hlc3":
        return bar.hlc3
    if source == "ohlc4":
        return bar.ohlc4
    raise ValueError(f"Unknown price source {source!r}")


# ---------------------------------------------------------------------------
# Shared smoothing primitives
# ---------------------------------------------------------------------------

def seeded_ewm(series: pd.Series, alpha: float, period: int) -> pd.Series:
    """Exponential smoothing seeded from the SMA of the first `period` values.

    Output is NaN until index `period - 1`. Both EMA (alpha = 2/(n+1)) and
    Wilder's smoothing (alpha = 1/n) route through here so their warm-up
    behaviour cannot drift apart.
    """
    s = pd.Series(series, dtype="float64").reset_index(drop=True)
    out = pd.Series(NAN, index=s.index, dtype="float64")
    if len(s) < period:
        return out

    seed = s.iloc[:period].mean()
    tail = s.iloc[period - 1 :].copy()
    tail.iloc[0] = seed
    out.iloc[period - 1 :] = tail.ewm(alpha=alpha, adjust=False).mean().to_numpy()
    return out


def ema_series(series: pd.Series, period: int) -> pd.Series:
    return seeded_ewm(series, 2.0 / (period + 1.0), period)


def wilder_series(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing — used by RSI, ATR and ADX. Equivalent to EMA(2n-1)."""
    return seeded_ewm(series, 1.0 / period, period)


def rolling_std(series: pd.Series, period: int, ddof: int = 0) -> pd.Series:
    """Rolling standard deviation, computed two-pass for exactness.

    `pandas.rolling().std()` uses a single-pass online algorithm that carries a
    running sum of squares. That is fast but loses precision as the series grows:
    on 18k bars of NIFTY it drifts about 7e-8 absolute, which is invisible in the
    band itself but shows up in Bollinger %B, where the band width is the
    denominator.

    Two-pass (compute the mean, then the deviations) is numerically stable and
    matches `RollingWindow.std`, so the vectorized and streaming forms agree.
    Windows are processed in chunks to keep peak memory bounded on long series.
    """
    values = pd.Series(series, dtype="float64").to_numpy()
    n = len(values)
    out = np.full(n, np.nan)
    if n < period or period <= ddof:
        return pd.Series(out, index=pd.Series(series).index, dtype="float64")

    windows = np.lib.stride_tricks.sliding_window_view(values, period)
    chunk = max(1, 8_000_000 // period)  # ~8M floats in flight at a time

    for start in range(0, len(windows), chunk):
        block = windows[start : start + chunk]
        mean = block.mean(axis=1, keepdims=True)
        var = ((block - mean) ** 2).sum(axis=1) / (period - ddof)
        out[start + period - 1 : start + period - 1 + len(block)] = np.sqrt(var)

    return pd.Series(out, index=pd.Series(series).index, dtype="float64")


def true_range(df: pd.DataFrame) -> pd.Series:
    """max(H-L, |H-prevC|, |L-prevC|). First bar falls back to H-L."""
    high, low, close = df["high"].astype("float64"), df["low"].astype("float64"), df["close"].astype("float64")
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr


# ---------------------------------------------------------------------------
# Streaming base classes
# ---------------------------------------------------------------------------

class StreamingIndicator(ABC):
    """One bar in, one value out. Never sees the future.

    `update()` returns None during warm-up. Returning None rather than 0.0 is
    deliberate: a strategy comparing against a warm-up value of zero would fire
    spurious signals on the opening bars of every session.
    """

    @abstractmethod
    def update(self, bar: PriceBar) -> float | dict[str, float] | None:
        """Consume one bar and return the current value, or None if warming up."""

    @property
    @abstractmethod
    def ready(self) -> bool:
        """True once enough bars have been seen to produce a real value."""

    def reset(self) -> None:
        self.__init__(**self._params)  # type: ignore[attr-defined]


class RollingWindow:
    """Fixed-size window over the most recent `period` values.

    Sums are computed with `math.fsum` rather than kept as a running total.
    A running total is O(1) but drifts: each push subtracts the oldest value and
    adds the newest, and the rounding error from those operations accumulates
    without bound. Over ~18k bars that drift reached 3e-9 relative, which is
    invisible in an SMA but becomes visible in Bollinger %B, where it is divided
    by a narrow band width.

    `fsum` is exactly rounded, so the streaming result matches the vectorized one
    bit for bit no matter how long the series runs. At typical periods (5-200)
    the cost is a few microseconds per bar.
    """

    __slots__ = ("period", "_buf")

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._buf: deque[float] = deque(maxlen=period)

    def push(self, value: float) -> None:
        self._buf.append(value)

    @property
    def full(self) -> bool:
        return len(self._buf) == self.period

    @property
    def sum(self) -> float:
        return math.fsum(self._buf)

    @property
    def mean(self) -> float:
        return math.fsum(self._buf) / len(self._buf) if self._buf else NAN

    @property
    def values(self) -> list[float]:
        return list(self._buf)

    def std(self, ddof: int = 0) -> float:
        """Population std by default — matches pandas `.rolling().std(ddof=0)`.

        Bollinger Bands conventionally use the population form; using the sample
        form would widen every band slightly and shift every breakout signal.
        """
        n = len(self._buf)
        if n <= ddof:
            return NAN
        mean = self.mean
        return math.sqrt(math.fsum((v - mean) ** 2 for v in self._buf) / (n - ddof))

    @property
    def max(self) -> float:
        return max(self._buf) if self._buf else NAN

    @property
    def min(self) -> float:
        return min(self._buf) if self._buf else NAN

    def __len__(self) -> int:
        return len(self._buf)


class StreamingEMA:
    """EMA with SMA seeding. Mirrors `ema_series` exactly."""

    __slots__ = ("period", "alpha", "_window", "_value")

    def __init__(self, period: int, alpha: float | None = None) -> None:
        self.period = period
        self.alpha = alpha if alpha is not None else 2.0 / (period + 1.0)
        self._window = RollingWindow(period)
        self._value: float | None = None

    def update(self, value: float) -> float | None:
        if self._value is None:
            self._window.push(value)
            if self._window.full:
                self._value = self._window.mean
            return self._value
        self._value = self.alpha * value + (1.0 - self.alpha) * self._value
        return self._value

    @property
    def value(self) -> float | None:
        return self._value

    @property
    def ready(self) -> bool:
        return self._value is not None


class StreamingWilder(StreamingEMA):
    """Wilder's smoothing — same machinery, alpha = 1/n."""

    def __init__(self, period: int) -> None:
        super().__init__(period, alpha=1.0 / period)


class StreamingTrueRange:
    """True range needs the previous close, so it carries one bar of state."""

    __slots__ = ("_prev_close",)

    def __init__(self) -> None:
        self._prev_close: float | None = None

    def update(self, bar: PriceBar) -> float:
        if self._prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._prev_close = bar.close
        return tr


def crossed_above(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """True on the bar where `fast` crosses from at-or-below to above `slow`."""
    prev_below = fast.shift(1) <= slow.shift(1)
    now_above = fast > slow
    return (prev_below & now_above).fillna(False)


def crossed_below(fast: pd.Series, slow: pd.Series) -> pd.Series:
    prev_above = fast.shift(1) >= slow.shift(1)
    now_below = fast < slow
    return (prev_above & now_below).fillna(False)
