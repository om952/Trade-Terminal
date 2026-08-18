"""Trend indicators: SMA, EMA, WMA, DEMA, MACD, Supertrend, ADX/DMI, PSAR."""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.indicators.base import (
    NAN,
    PriceBar,
    RollingWindow,
    StreamingEMA,
    StreamingIndicator,
    StreamingTrueRange,
    StreamingWilder,
    ema_series,
    source_series,
    source_value,
    true_range,
    wilder_series,
)

# ---------------------------------------------------------------------------
# SMA
# ---------------------------------------------------------------------------

def sma(df: pd.DataFrame, period: int = 20, source: str = "close") -> pd.Series:
    return source_series(df, source).rolling(period).mean()


class SMA(StreamingIndicator):
    def __init__(self, period: int = 20, source: str = "close") -> None:
        self._params = {"period": period, "source": source}
        self.source = source
        self._window = RollingWindow(period)

    def update(self, bar: PriceBar) -> float | None:
        self._window.push(source_value(bar, self.source))
        return self._window.mean if self._window.full else None

    @property
    def ready(self) -> bool:
        return self._window.full


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

def ema(df: pd.DataFrame, period: int = 20, source: str = "close") -> pd.Series:
    return ema_series(source_series(df, source), period)


class EMA(StreamingIndicator):
    def __init__(self, period: int = 20, source: str = "close") -> None:
        self._params = {"period": period, "source": source}
        self.source = source
        self._ema = StreamingEMA(period)

    def update(self, bar: PriceBar) -> float | None:
        return self._ema.update(source_value(bar, self.source))

    @property
    def ready(self) -> bool:
        return self._ema.ready


# ---------------------------------------------------------------------------
# WMA — linearly weighted, most recent bar carries the most weight
# ---------------------------------------------------------------------------

def wma(df: pd.DataFrame, period: int = 20, source: str = "close") -> pd.Series:
    weights = np.arange(1, period + 1, dtype="float64")
    denom = weights.sum()
    return source_series(df, source).rolling(period).apply(
        lambda w: float(np.dot(w, weights) / denom), raw=True
    )


class WMA(StreamingIndicator):
    def __init__(self, period: int = 20, source: str = "close") -> None:
        self._params = {"period": period, "source": source}
        self.source = source
        self._window = RollingWindow(period)
        self._weights = np.arange(1, period + 1, dtype="float64")
        self._denom = self._weights.sum()

    def update(self, bar: PriceBar) -> float | None:
        self._window.push(source_value(bar, self.source))
        if not self._window.full:
            return None
        return float(np.dot(np.array(self._window.values), self._weights) / self._denom)

    @property
    def ready(self) -> bool:
        return self._window.full


# ---------------------------------------------------------------------------
# DEMA — double EMA, less lag than a plain EMA of the same period
# ---------------------------------------------------------------------------

def dema(df: pd.DataFrame, period: int = 20, source: str = "close") -> pd.Series:
    e1 = ema_series(source_series(df, source), period)
    e2 = ema_series(e1.dropna(), period)
    e2 = e2.reindex(range(len(e1))).shift(int(e1.isna().sum()))
    e2.index = e1.index
    return 2 * e1 - e2


class DEMA(StreamingIndicator):
    def __init__(self, period: int = 20, source: str = "close") -> None:
        self._params = {"period": period, "source": source}
        self.source = source
        self._e1 = StreamingEMA(period)
        self._e2 = StreamingEMA(period)

    def update(self, bar: PriceBar) -> float | None:
        v1 = self._e1.update(source_value(bar, self.source))
        if v1 is None:
            return None
        v2 = self._e2.update(v1)
        return None if v2 is None else 2 * v1 - v2

    @property
    def ready(self) -> bool:
        return self._e2.ready


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

def macd(
    df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9, source: str = "close"
) -> pd.DataFrame:
    """Returns columns: macd, signal, histogram.

    The signal line is an EMA of the MACD line, which only exists once the slow
    EMA does. Seeding it from the MACD's first valid value (rather than from
    index 0, where the MACD is NaN) is what keeps this matching the streaming
    form and TradingView.
    """
    price = source_series(df, source)
    macd_line = ema_series(price, fast) - ema_series(price, slow)

    valid = macd_line.dropna()
    signal_line = pd.Series(NAN, index=macd_line.index, dtype="float64")
    if len(valid) >= signal:
        signal_line.loc[valid.index] = ema_series(valid.reset_index(drop=True), signal).to_numpy()

    return pd.DataFrame(
        {"macd": macd_line, "signal": signal_line, "histogram": macd_line - signal_line}
    )


class MACD(StreamingIndicator):
    def __init__(
        self, fast: int = 12, slow: int = 26, signal: int = 9, source: str = "close"
    ) -> None:
        self._params = {"fast": fast, "slow": slow, "signal": signal, "source": source}
        self.source = source
        self._fast = StreamingEMA(fast)
        self._slow = StreamingEMA(slow)
        self._signal = StreamingEMA(signal)

    def update(self, bar: PriceBar) -> dict[str, float] | None:
        price = source_value(bar, self.source)
        f = self._fast.update(price)
        s = self._slow.update(price)
        if f is None or s is None:
            return None

        macd_line = f - s
        sig = self._signal.update(macd_line)
        if sig is None:
            return {"macd": macd_line, "signal": NAN, "histogram": NAN}
        return {"macd": macd_line, "signal": sig, "histogram": macd_line - sig}

    @property
    def ready(self) -> bool:
        return self._signal.ready


# ---------------------------------------------------------------------------
# ADX / DMI
# ---------------------------------------------------------------------------

def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Average Directional Index with +DI / -DI. Returns: adx, plus_di, minus_di."""
    high, low = df["high"].astype("float64"), df["low"].astype("float64")
    up_move, down_move = high.diff(), -low.diff()

    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    plus_dm.iloc[0] = minus_dm.iloc[0] = 0.0

    atr_ = wilder_series(true_range(df), period)
    plus_di = 100.0 * wilder_series(plus_dm, period) / atr_
    minus_di = 100.0 * wilder_series(minus_dm, period) / atr_

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)

    valid = dx.dropna()
    adx_line = pd.Series(NAN, index=dx.index, dtype="float64")
    if len(valid) >= period:
        adx_line.loc[valid.index] = wilder_series(valid.reset_index(drop=True), period).to_numpy()

    return pd.DataFrame({"adx": adx_line, "plus_di": plus_di, "minus_di": minus_di})


class ADX(StreamingIndicator):
    def __init__(self, period: int = 14) -> None:
        self._params = {"period": period}
        self.period = period
        self._tr = StreamingTrueRange()
        self._atr = StreamingWilder(period)
        self._plus = StreamingWilder(period)
        self._minus = StreamingWilder(period)
        self._adx = StreamingWilder(period)
        self._prev: PriceBar | None = None

    def update(self, bar: PriceBar) -> dict[str, float] | None:
        tr = self._tr.update(bar)

        if self._prev is None:
            plus_dm = minus_dm = 0.0
        else:
            up = bar.high - self._prev.high
            down = self._prev.low - bar.low
            plus_dm = up if (up > down and up > 0) else 0.0
            minus_dm = down if (down > up and down > 0) else 0.0
        self._prev = bar

        atr_v = self._atr.update(tr)
        plus_v = self._plus.update(plus_dm)
        minus_v = self._minus.update(minus_dm)
        if atr_v is None or plus_v is None or minus_v is None or atr_v == 0:
            return None

        plus_di = 100.0 * plus_v / atr_v
        minus_di = 100.0 * minus_v / atr_v
        total = plus_di + minus_di
        if total == 0:
            return {"adx": NAN, "plus_di": plus_di, "minus_di": minus_di}

        dx = 100.0 * abs(plus_di - minus_di) / total
        adx_v = self._adx.update(dx)
        return {
            "adx": adx_v if adx_v is not None else NAN,
            "plus_di": plus_di,
            "minus_di": minus_di,
        }

    @property
    def ready(self) -> bool:
        return self._adx.ready


# ---------------------------------------------------------------------------
# Supertrend
# ---------------------------------------------------------------------------

def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """Returns: supertrend, direction (+1 long, -1 short), upper, lower.

    The band-ratcheting rule is what makes this path-dependent: bands only move
    in the favourable direction while the trend holds, so this cannot be
    expressed as a pure rolling window. The loop below is the definition.
    """
    atr_ = wilder_series(true_range(df), period)
    hl2 = (df["high"].astype("float64") + df["low"].astype("float64")) / 2.0

    basic_upper = hl2 + multiplier * atr_
    basic_lower = hl2 - multiplier * atr_
    close = df["close"].astype("float64").to_numpy()

    n = len(df)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    trend = np.full(n, np.nan)
    direction = np.full(n, np.nan)

    bu, bl = basic_upper.to_numpy(), basic_lower.to_numpy()
    start = int(atr_.isna().sum())
    if start >= n:
        return pd.DataFrame(
            {"supertrend": trend, "direction": direction, "upper": upper, "lower": lower},
            index=df.index,
        )

    upper[start], lower[start] = bu[start], bl[start]
    direction[start] = 1.0
    trend[start] = lower[start]

    for i in range(start + 1, n):
        upper[i] = bu[i] if (bu[i] < upper[i - 1] or close[i - 1] > upper[i - 1]) else upper[i - 1]
        lower[i] = bl[i] if (bl[i] > lower[i - 1] or close[i - 1] < lower[i - 1]) else lower[i - 1]

        if direction[i - 1] == 1.0:
            direction[i] = -1.0 if close[i] < lower[i] else 1.0
        else:
            direction[i] = 1.0 if close[i] > upper[i] else -1.0

        trend[i] = lower[i] if direction[i] == 1.0 else upper[i]

    return pd.DataFrame(
        {"supertrend": trend, "direction": direction, "upper": upper, "lower": lower},
        index=df.index,
    )


class Supertrend(StreamingIndicator):
    def __init__(self, period: int = 10, multiplier: float = 3.0) -> None:
        self._params = {"period": period, "multiplier": multiplier}
        self.multiplier = multiplier
        self._tr = StreamingTrueRange()
        self._atr = StreamingWilder(period)
        self._upper: float | None = None
        self._lower: float | None = None
        self._direction = 1.0
        self._prev_close: float | None = None

    def update(self, bar: PriceBar) -> dict[str, float] | None:
        atr_v = self._atr.update(self._tr.update(bar))
        if atr_v is None:
            self._prev_close = bar.close
            return None

        hl2 = bar.hl2
        bu = hl2 + self.multiplier * atr_v
        bl = hl2 - self.multiplier * atr_v

        if self._upper is None:  # first bar with a valid ATR
            self._upper, self._lower = bu, bl
            self._direction = 1.0
            self._prev_close = bar.close
            return {
                "supertrend": self._lower, "direction": 1.0,
                "upper": self._upper, "lower": self._lower,
            }

        prev_close = self._prev_close if self._prev_close is not None else bar.close
        self._upper = bu if (bu < self._upper or prev_close > self._upper) else self._upper
        self._lower = bl if (bl > self._lower or prev_close < self._lower) else self._lower

        if self._direction == 1.0:
            self._direction = -1.0 if bar.close < self._lower else 1.0
        else:
            self._direction = 1.0 if bar.close > self._upper else -1.0

        self._prev_close = bar.close
        line = self._lower if self._direction == 1.0 else self._upper
        return {
            "supertrend": line, "direction": self._direction,
            "upper": self._upper, "lower": self._lower,
        }

    @property
    def ready(self) -> bool:
        return self._upper is not None


# ---------------------------------------------------------------------------
# Parabolic SAR
# ---------------------------------------------------------------------------

def psar(
    df: pd.DataFrame, step: float = 0.02, max_step: float = 0.2
) -> pd.DataFrame:
    """Parabolic SAR. Returns: psar, direction (+1 long, -1 short).

    Wilder's original formulation, including the rule that SAR may not penetrate
    the prior two bars' range.
    """
    high = df["high"].astype("float64").to_numpy()
    low = df["low"].astype("float64").to_numpy()
    n = len(df)

    out = np.full(n, np.nan)
    direction = np.full(n, np.nan)
    if n < 2:
        return pd.DataFrame({"psar": out, "direction": direction}, index=df.index)

    bull = True
    af = step
    ep = high[0]
    sar = low[0]
    out[0], direction[0] = sar, 1.0

    for i in range(1, n):
        prev_sar = sar
        sar = prev_sar + af * (ep - prev_sar)

        if bull:
            sar = min(sar, low[i - 1], low[i - 2] if i >= 2 else low[i - 1])
            if low[i] < sar:  # flip to bearish
                bull, sar, ep, af = False, ep, low[i], step
            elif high[i] > ep:
                ep, af = high[i], min(af + step, max_step)
        else:
            sar = max(sar, high[i - 1], high[i - 2] if i >= 2 else high[i - 1])
            if high[i] > sar:  # flip to bullish
                bull, sar, ep, af = True, ep, high[i], step
            elif low[i] < ep:
                ep, af = low[i], min(af + step, max_step)

        out[i], direction[i] = sar, (1.0 if bull else -1.0)

    return pd.DataFrame({"psar": out, "direction": direction}, index=df.index)


class PSAR(StreamingIndicator):
    def __init__(self, step: float = 0.02, max_step: float = 0.2) -> None:
        self._params = {"step": step, "max_step": max_step}
        self.step, self.max_step = step, max_step
        self._bull = True
        self._af = step
        self._ep: float | None = None
        self._sar: float | None = None
        self._h1: float | None = None
        self._h2: float | None = None
        self._l1: float | None = None
        self._l2: float | None = None

    def update(self, bar: PriceBar) -> dict[str, float] | None:
        if self._sar is None:
            self._sar, self._ep = bar.low, bar.high
            self._h1, self._l1 = bar.high, bar.low
            return {"psar": self._sar, "direction": 1.0}

        sar = self._sar + self._af * (self._ep - self._sar)
        l1 = self._l1 if self._l1 is not None else bar.low
        l2 = self._l2 if self._l2 is not None else l1
        h1 = self._h1 if self._h1 is not None else bar.high
        h2 = self._h2 if self._h2 is not None else h1

        if self._bull:
            sar = min(sar, l1, l2)
            if bar.low < sar:
                self._bull, sar, self._ep, self._af = False, self._ep, bar.low, self.step
            elif bar.high > self._ep:
                self._ep, self._af = bar.high, min(self._af + self.step, self.max_step)
        else:
            sar = max(sar, h1, h2)
            if bar.high > sar:
                self._bull, sar, self._ep, self._af = True, self._ep, bar.high, self.step
            elif bar.low < self._ep:
                self._ep, self._af = bar.low, min(self._af + self.step, self.max_step)

        self._sar = sar
        self._h2, self._h1 = self._h1, bar.high
        self._l2, self._l1 = self._l1, bar.low
        return {"psar": sar, "direction": 1.0 if self._bull else -1.0}

    @property
    def ready(self) -> bool:
        return self._sar is not None
