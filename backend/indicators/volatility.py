"""Volatility indicators: ATR, Bollinger Bands, Keltner, Donchian, historical vol."""

from __future__ import annotations

import math

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
    rolling_std,
    source_value,
    true_range,
    wilder_series,
)

TRADING_DAYS_PER_YEAR = 252


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return wilder_series(true_range(df), period)


class ATR(StreamingIndicator):
    def __init__(self, period: int = 14) -> None:
        self._params = {"period": period}
        self._tr = StreamingTrueRange()
        self._smooth = StreamingWilder(period)

    def update(self, bar: PriceBar) -> float | None:
        return self._smooth.update(self._tr.update(bar))

    @property
    def ready(self) -> bool:
        return self._smooth.ready


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

def bollinger(
    df: pd.DataFrame, period: int = 20, std_dev: float = 2.0, source: str = "close"
) -> pd.DataFrame:
    """Returns: middle, upper, lower, bandwidth, percent_b.

    Population standard deviation (ddof=0), which is the conventional form. Using
    the sample form would widen every band and shift every breakout signal.
    """
    price = source_series(df, source)
    middle = price.rolling(period).mean()
    sd = rolling_std(price, period, ddof=0)

    upper = middle + std_dev * sd
    lower = middle - std_dev * sd
    span = (upper - lower).replace(0.0, np.nan)

    return pd.DataFrame(
        {
            "middle": middle,
            "upper": upper,
            "lower": lower,
            "bandwidth": span / middle.replace(0.0, np.nan),
            "percent_b": (price - lower) / span,
        }
    )


class Bollinger(StreamingIndicator):
    def __init__(self, period: int = 20, std_dev: float = 2.0, source: str = "close") -> None:
        self._params = {"period": period, "std_dev": std_dev, "source": source}
        self.std_dev, self.source = std_dev, source
        self._window = RollingWindow(period)

    def update(self, bar: PriceBar) -> dict[str, float] | None:
        price = source_value(bar, self.source)
        self._window.push(price)
        if not self._window.full:
            return None

        middle = self._window.mean
        sd = self._window.std(ddof=0)
        upper = middle + self.std_dev * sd
        lower = middle - self.std_dev * sd
        span = upper - lower

        return {
            "middle": middle,
            "upper": upper,
            "lower": lower,
            "bandwidth": (span / middle) if middle else NAN,
            "percent_b": ((price - lower) / span) if span else NAN,
        }

    @property
    def ready(self) -> bool:
        return self._window.full


# ---------------------------------------------------------------------------
# Keltner Channels
# ---------------------------------------------------------------------------

def keltner(
    df: pd.DataFrame, period: int = 20, atr_period: int = 10, multiplier: float = 2.0
) -> pd.DataFrame:
    """EMA centre with ATR-scaled bands. Returns: middle, upper, lower."""
    middle = ema_series(source_series(df, "close"), period)
    band = multiplier * wilder_series(true_range(df), atr_period)
    return pd.DataFrame({"middle": middle, "upper": middle + band, "lower": middle - band})


class Keltner(StreamingIndicator):
    def __init__(self, period: int = 20, atr_period: int = 10, multiplier: float = 2.0) -> None:
        self._params = {"period": period, "atr_period": atr_period, "multiplier": multiplier}
        self.multiplier = multiplier
        self._ema = StreamingEMA(period)
        self._tr = StreamingTrueRange()
        self._atr = StreamingWilder(atr_period)

    def update(self, bar: PriceBar) -> dict[str, float] | None:
        middle = self._ema.update(bar.close)
        atr_v = self._atr.update(self._tr.update(bar))
        if middle is None or atr_v is None:
            return None
        band = self.multiplier * atr_v
        return {"middle": middle, "upper": middle + band, "lower": middle - band}

    @property
    def ready(self) -> bool:
        return self._ema.ready and self._atr.ready


# ---------------------------------------------------------------------------
# Donchian Channels
# ---------------------------------------------------------------------------

def donchian(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """Returns: upper, lower, middle.

    Includes the current bar, matching the common charting default. A breakout
    strategy usually wants the channel EXCLUDING the current bar — otherwise
    price can never exceed its own high — so shift by one at the strategy level.
    """
    upper = df["high"].astype("float64").rolling(period).max()
    lower = df["low"].astype("float64").rolling(period).min()
    return pd.DataFrame({"upper": upper, "lower": lower, "middle": (upper + lower) / 2.0})


class Donchian(StreamingIndicator):
    def __init__(self, period: int = 20) -> None:
        self._params = {"period": period}
        self._highs = RollingWindow(period)
        self._lows = RollingWindow(period)

    def update(self, bar: PriceBar) -> dict[str, float] | None:
        self._highs.push(bar.high)
        self._lows.push(bar.low)
        if not self._highs.full:
            return None
        upper, lower = self._highs.max, self._lows.min
        return {"upper": upper, "lower": lower, "middle": (upper + lower) / 2.0}

    @property
    def ready(self) -> bool:
        return self._highs.full


# ---------------------------------------------------------------------------
# Historical volatility
# ---------------------------------------------------------------------------

def historical_volatility(
    df: pd.DataFrame, period: int = 20, bars_per_year: int = TRADING_DAYS_PER_YEAR,
    source: str = "close",
) -> pd.Series:
    """Annualized stdev of log returns, as a percentage.

    `bars_per_year` must match the timeframe: 252 for daily, 252*75 for 5-minute.
    Leaving it at the daily default on intraday data understates volatility by
    an order of magnitude and makes every option look cheap.
    """
    price = source_series(df, source)
    log_ret = np.log(price / price.shift(1))
    return rolling_std(log_ret, period, ddof=1) * math.sqrt(bars_per_year) * 100.0


class HistoricalVolatility(StreamingIndicator):
    def __init__(
        self, period: int = 20, bars_per_year: int = TRADING_DAYS_PER_YEAR,
        source: str = "close",
    ) -> None:
        self._params = {"period": period, "bars_per_year": bars_per_year, "source": source}
        self.source = source
        self.scale = math.sqrt(bars_per_year) * 100.0
        self._window = RollingWindow(period)
        self._prev: float | None = None

    def update(self, bar: PriceBar) -> float | None:
        price = source_value(bar, self.source)
        if self._prev is None:
            self._prev = price
            return None

        self._window.push(math.log(price / self._prev))
        self._prev = price
        if not self._window.full:
            return None
        return self._window.std(ddof=1) * self.scale

    @property
    def ready(self) -> bool:
        return self._window.full
