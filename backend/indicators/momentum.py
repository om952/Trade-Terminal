"""Momentum indicators: RSI, Stochastic, StochRSI, CCI, Williams %R, ROC, MFI."""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.indicators.base import (
    NAN,
    PriceBar,
    RollingWindow,
    StreamingIndicator,
    StreamingWilder,
    source_series,
    source_value,
    wilder_series,
)


# ---------------------------------------------------------------------------
# RSI (Wilder)
# ---------------------------------------------------------------------------

def rsi(df: pd.DataFrame, period: int = 14, source: str = "close") -> pd.Series:
    price = source_series(df, source)
    delta = price.diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)

    # The first row has no change; Wilder's seed averages the first `period`
    # changes, so drop the NaN row before smoothing and realign after.
    avg_gain = wilder_series(gains.iloc[1:].reset_index(drop=True), period)
    avg_loss = wilder_series(losses.iloc[1:].reset_index(drop=True), period)

    out = pd.Series(NAN, index=price.index, dtype="float64")
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    value = 100.0 - (100.0 / (1.0 + rs))
    value = value.where(avg_loss.to_numpy() != 0.0, 100.0)
    value[avg_gain.isna()] = np.nan

    out.iloc[1:] = value.to_numpy()
    return out


class RSI(StreamingIndicator):
    def __init__(self, period: int = 14, source: str = "close") -> None:
        self._params = {"period": period, "source": source}
        self.source = source
        self._gain = StreamingWilder(period)
        self._loss = StreamingWilder(period)
        self._prev: float | None = None

    def update(self, bar: PriceBar) -> float | None:
        price = source_value(bar, self.source)
        if self._prev is None:
            self._prev = price
            return None

        delta = price - self._prev
        self._prev = price

        avg_gain = self._gain.update(max(delta, 0.0))
        avg_loss = self._loss.update(max(-delta, 0.0))
        if avg_gain is None or avg_loss is None:
            return None
        if avg_loss == 0.0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    @property
    def ready(self) -> bool:
        return self._gain.ready


# ---------------------------------------------------------------------------
# Stochastic Oscillator
# ---------------------------------------------------------------------------

def stochastic(
    df: pd.DataFrame, k_period: int = 14, k_smooth: int = 3, d_period: int = 3
) -> pd.DataFrame:
    """Slow stochastic. Returns: k, d."""
    high, low, close = df["high"].astype("float64"), df["low"].astype("float64"), df["close"].astype("float64")
    highest = high.rolling(k_period).max()
    lowest = low.rolling(k_period).min()

    span = (highest - lowest).replace(0.0, np.nan)
    raw_k = 100.0 * (close - lowest) / span
    # A flat window means no range; convention is a neutral 50 rather than NaN.
    raw_k = raw_k.where((highest - lowest) != 0.0, 50.0)
    raw_k[highest.isna()] = np.nan

    k = raw_k.rolling(k_smooth).mean()
    return pd.DataFrame({"k": k, "d": k.rolling(d_period).mean()})


class Stochastic(StreamingIndicator):
    def __init__(self, k_period: int = 14, k_smooth: int = 3, d_period: int = 3) -> None:
        self._params = {"k_period": k_period, "k_smooth": k_smooth, "d_period": d_period}
        self._highs = RollingWindow(k_period)
        self._lows = RollingWindow(k_period)
        self._k = RollingWindow(k_smooth)
        self._d = RollingWindow(d_period)

    def update(self, bar: PriceBar) -> dict[str, float] | None:
        self._highs.push(bar.high)
        self._lows.push(bar.low)
        if not self._highs.full:
            return None

        hi, lo = self._highs.max, self._lows.min
        raw_k = 50.0 if hi == lo else 100.0 * (bar.close - lo) / (hi - lo)

        self._k.push(raw_k)
        if not self._k.full:
            return None

        k_val = self._k.mean
        self._d.push(k_val)
        return {"k": k_val, "d": self._d.mean if self._d.full else NAN}

    @property
    def ready(self) -> bool:
        return self._d.full


# ---------------------------------------------------------------------------
# Stochastic RSI
# ---------------------------------------------------------------------------

def stoch_rsi(
    df: pd.DataFrame, rsi_period: int = 14, stoch_period: int = 14,
    k_smooth: int = 3, d_period: int = 3, source: str = "close",
) -> pd.DataFrame:
    """Stochastic applied to RSI rather than price. Returns: k, d."""
    r = rsi(df, rsi_period, source)
    valid = r.dropna()

    k = pd.Series(NAN, index=r.index, dtype="float64")
    d = pd.Series(NAN, index=r.index, dtype="float64")
    if len(valid) < stoch_period:
        return pd.DataFrame({"k": k, "d": d})

    lowest = valid.rolling(stoch_period).min()
    highest = valid.rolling(stoch_period).max()
    span = highest - lowest
    raw = 100.0 * (valid - lowest) / span.replace(0.0, np.nan)
    raw = raw.where(span != 0.0, 50.0)
    raw[highest.isna()] = np.nan

    k.loc[valid.index] = raw.rolling(k_smooth).mean().to_numpy()
    d.loc[valid.index] = raw.rolling(k_smooth).mean().rolling(d_period).mean().to_numpy()
    return pd.DataFrame({"k": k, "d": d})


class StochRSI(StreamingIndicator):
    def __init__(
        self, rsi_period: int = 14, stoch_period: int = 14,
        k_smooth: int = 3, d_period: int = 3, source: str = "close",
    ) -> None:
        self._params = {
            "rsi_period": rsi_period, "stoch_period": stoch_period,
            "k_smooth": k_smooth, "d_period": d_period, "source": source,
        }
        self._rsi = RSI(rsi_period, source)
        self._window = RollingWindow(stoch_period)
        self._k = RollingWindow(k_smooth)
        self._d = RollingWindow(d_period)

    def update(self, bar: PriceBar) -> dict[str, float] | None:
        r = self._rsi.update(bar)
        if r is None:
            return None

        self._window.push(r)
        if not self._window.full:
            return None

        hi, lo = self._window.max, self._window.min
        raw = 50.0 if hi == lo else 100.0 * (r - lo) / (hi - lo)

        self._k.push(raw)
        if not self._k.full:
            return None

        k_val = self._k.mean
        self._d.push(k_val)
        return {"k": k_val, "d": self._d.mean if self._d.full else NAN}

    @property
    def ready(self) -> bool:
        return self._d.full


# ---------------------------------------------------------------------------
# CCI
# ---------------------------------------------------------------------------

def cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Commodity Channel Index. Uses mean absolute deviation, not std."""
    tp = (df["high"] + df["low"] + df["close"]).astype("float64") / 3.0
    sma_tp = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda w: float(np.abs(w - w.mean()).mean()), raw=True)
    return (tp - sma_tp) / (0.015 * mad.replace(0.0, np.nan))


class CCI(StreamingIndicator):
    def __init__(self, period: int = 20) -> None:
        self._params = {"period": period}
        self._window = RollingWindow(period)

    def update(self, bar: PriceBar) -> float | None:
        tp = bar.hlc3
        self._window.push(tp)
        if not self._window.full:
            return None

        values = np.array(self._window.values)
        mean = values.mean()
        mad = float(np.abs(values - mean).mean())
        if mad == 0.0:
            return NAN
        return (tp - mean) / (0.015 * mad)

    @property
    def ready(self) -> bool:
        return self._window.full


# ---------------------------------------------------------------------------
# Williams %R
# ---------------------------------------------------------------------------

def williams_r(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"].astype("float64"), df["low"].astype("float64"), df["close"].astype("float64")
    highest = high.rolling(period).max()
    lowest = low.rolling(period).min()
    span = (highest - lowest).replace(0.0, np.nan)
    out = -100.0 * (highest - close) / span
    out = out.where((highest - lowest) != 0.0, -50.0)
    out[highest.isna()] = np.nan
    return out


class WilliamsR(StreamingIndicator):
    def __init__(self, period: int = 14) -> None:
        self._params = {"period": period}
        self._highs = RollingWindow(period)
        self._lows = RollingWindow(period)

    def update(self, bar: PriceBar) -> float | None:
        self._highs.push(bar.high)
        self._lows.push(bar.low)
        if not self._highs.full:
            return None
        hi, lo = self._highs.max, self._lows.min
        if hi == lo:
            return -50.0
        return -100.0 * (hi - bar.close) / (hi - lo)

    @property
    def ready(self) -> bool:
        return self._highs.full


# ---------------------------------------------------------------------------
# Rate of Change
# ---------------------------------------------------------------------------

def roc(df: pd.DataFrame, period: int = 12, source: str = "close") -> pd.Series:
    price = source_series(df, source)
    return 100.0 * (price - price.shift(period)) / price.shift(period)


class ROC(StreamingIndicator):
    def __init__(self, period: int = 12, source: str = "close") -> None:
        self._params = {"period": period, "source": source}
        self.source = source
        # period + 1 values so the oldest in the window is exactly `period` back.
        self._window = RollingWindow(period + 1)

    def update(self, bar: PriceBar) -> float | None:
        self._window.push(source_value(bar, self.source))
        if not self._window.full:
            return None
        values = self._window.values
        old = values[0]
        return NAN if old == 0 else 100.0 * (values[-1] - old) / old

    @property
    def ready(self) -> bool:
        return self._window.full


# ---------------------------------------------------------------------------
# Money Flow Index
# ---------------------------------------------------------------------------

def mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Volume-weighted RSI. Uses simple (not Wilder) averaging, per convention."""
    tp = (df["high"] + df["low"] + df["close"]).astype("float64") / 3.0
    raw_flow = tp * df["volume"].astype("float64")
    delta = tp.diff()

    pos = raw_flow.where(delta > 0, 0.0)
    neg = raw_flow.where(delta < 0, 0.0)
    pos.iloc[0] = neg.iloc[0] = 0.0

    pos_sum = pos.rolling(period).sum()
    neg_sum = neg.rolling(period).sum()

    out = 100.0 - (100.0 / (1.0 + pos_sum / neg_sum.replace(0.0, np.nan)))
    out = out.where(neg_sum != 0.0, 100.0)
    out.iloc[:period] = np.nan
    return out


class MFI(StreamingIndicator):
    def __init__(self, period: int = 14) -> None:
        self._params = {"period": period}
        self._pos = RollingWindow(period)
        self._neg = RollingWindow(period)
        self._prev_tp: float | None = None
        self._seen = 0

    def update(self, bar: PriceBar) -> float | None:
        tp = bar.hlc3
        flow = tp * bar.volume
        self._seen += 1

        if self._prev_tp is None:
            self._pos.push(0.0)
            self._neg.push(0.0)
        elif tp > self._prev_tp:
            self._pos.push(flow)
            self._neg.push(0.0)
        elif tp < self._prev_tp:
            self._pos.push(0.0)
            self._neg.push(flow)
        else:
            self._pos.push(0.0)
            self._neg.push(0.0)

        self._prev_tp = tp
        if not self._pos.full or self._seen <= self._pos.period:
            return None

        pos_sum = sum(self._pos.values)
        neg_sum = sum(self._neg.values)
        if neg_sum == 0.0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + pos_sum / neg_sum))

    @property
    def ready(self) -> bool:
        return self._pos.full and self._seen > self._pos.period
