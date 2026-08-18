"""Indicator registry — the bridge between a strategy JSON document and code.

A strategy says:

    {"id": "ema_fast", "type": "EMA", "params": {"period": 9, "source": "close"}}

and the registry turns "EMA" into either the vectorized function (backtest) or
the streaming class (live). Because both come from the same entry, a strategy
cannot accidentally run one implementation in one mode and a different one in
another.

The registry is also what the frontend reads to build its indicator palette:
`describe_all()` returns every indicator with its parameters, defaults and
output columns, so the UI never hardcodes a list that can drift from reality.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from backend.indicators import momentum, trend, volatility, volume
from backend.indicators.base import PriceBar, StreamingIndicator, bars_from_frame

Category = str


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    name: str
    category: Category
    vectorized: Callable[..., pd.Series | pd.DataFrame]
    streaming: type
    params: dict[str, Any]
    outputs: tuple[str, ...]          # () means a single unnamed Series
    description: str
    needs_volume: bool = False

    @property
    def is_multi_output(self) -> bool:
        return bool(self.outputs)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "params": self.params,
            "outputs": list(self.outputs) or ["value"],
            "description": self.description,
            "needs_volume": self.needs_volume,
        }


_REGISTRY: dict[str, IndicatorSpec] = {}


def register(spec: IndicatorSpec) -> None:
    key = spec.name.upper()
    if key in _REGISTRY:
        raise ValueError(f"Indicator {spec.name} already registered")
    _REGISTRY[key] = spec


def get(name: str) -> IndicatorSpec:
    key = name.upper()
    if key not in _REGISTRY:
        raise KeyError(
            f"Unknown indicator {name!r}. Available: {', '.join(sorted(_REGISTRY))}"
        )
    return _REGISTRY[key]


def all_names() -> list[str]:
    return sorted(_REGISTRY)


def describe_all() -> list[dict[str, Any]]:
    """Everything the frontend needs to render an indicator palette."""
    return [spec.describe() for spec in sorted(_REGISTRY.values(), key=lambda s: (s.category, s.name))]


def by_category() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for spec in _REGISTRY.values():
        out.setdefault(spec.category, []).append(spec.name)
    return {k: sorted(v) for k, v in sorted(out.items())}


def compute(name: str, df: pd.DataFrame, **params) -> pd.Series | pd.DataFrame:
    """Vectorized path — used by backtests."""
    spec = get(name)
    merged = {**spec.params, **params}
    return spec.vectorized(df, **merged)


def create_streaming(name: str, **params) -> StreamingIndicator:
    """Streaming path — used by paper and live."""
    spec = get(name)
    merged = {**spec.params, **params}
    return spec.streaming(**merged)


def stream_over_frame(name: str, df: pd.DataFrame, **params) -> pd.DataFrame:
    """Replay a DataFrame bar-by-bar through the streaming implementation.

    This exists for the equivalence test: it produces exactly what a live session
    would have produced, in a form directly comparable to the vectorized output.
    """
    spec = get(name)
    indicator = create_streaming(name, **params)
    columns = spec.outputs or ("value",)

    rows: list[dict[str, float]] = []
    for bar in bars_from_frame(df):
        result = indicator.update(bar)
        if result is None:
            rows.append({c: float("nan") for c in columns})
        elif isinstance(result, dict):
            rows.append({c: result.get(c, float("nan")) for c in columns})
        else:
            rows.append({columns[0]: float(result)})

    return pd.DataFrame(rows, index=df.index)


# ---------------------------------------------------------------------------
# Registrations
# ---------------------------------------------------------------------------

def _register_all() -> None:
    # --- Trend -------------------------------------------------------------
    register(IndicatorSpec(
        "SMA", "trend", trend.sma, trend.SMA,
        {"period": 20, "source": "close"}, (),
        "Simple moving average",
    ))
    register(IndicatorSpec(
        "EMA", "trend", trend.ema, trend.EMA,
        {"period": 20, "source": "close"}, (),
        "Exponential moving average, SMA-seeded",
    ))
    register(IndicatorSpec(
        "WMA", "trend", trend.wma, trend.WMA,
        {"period": 20, "source": "close"}, (),
        "Linearly weighted moving average",
    ))
    register(IndicatorSpec(
        "DEMA", "trend", trend.dema, trend.DEMA,
        {"period": 20, "source": "close"}, (),
        "Double exponential moving average, less lag than EMA",
    ))
    register(IndicatorSpec(
        "MACD", "trend", trend.macd, trend.MACD,
        {"fast": 12, "slow": 26, "signal": 9, "source": "close"},
        ("macd", "signal", "histogram"),
        "Moving average convergence divergence",
    ))
    register(IndicatorSpec(
        "ADX", "trend", trend.adx, trend.ADX,
        {"period": 14}, ("adx", "plus_di", "minus_di"),
        "Average directional index with +DI/-DI",
    ))
    register(IndicatorSpec(
        "SUPERTREND", "trend", trend.supertrend, trend.Supertrend,
        {"period": 10, "multiplier": 3.0},
        ("supertrend", "direction", "upper", "lower"),
        "ATR-banded trend follower; direction +1 long, -1 short",
    ))
    register(IndicatorSpec(
        "PSAR", "trend", trend.psar, trend.PSAR,
        {"step": 0.02, "max_step": 0.2}, ("psar", "direction"),
        "Parabolic stop and reverse",
    ))

    # --- Momentum ----------------------------------------------------------
    register(IndicatorSpec(
        "RSI", "momentum", momentum.rsi, momentum.RSI,
        {"period": 14, "source": "close"}, (),
        "Relative strength index, Wilder-smoothed",
    ))
    register(IndicatorSpec(
        "STOCHASTIC", "momentum", momentum.stochastic, momentum.Stochastic,
        {"k_period": 14, "k_smooth": 3, "d_period": 3}, ("k", "d"),
        "Slow stochastic oscillator",
    ))
    register(IndicatorSpec(
        "STOCHRSI", "momentum", momentum.stoch_rsi, momentum.StochRSI,
        {"rsi_period": 14, "stoch_period": 14, "k_smooth": 3, "d_period": 3, "source": "close"},
        ("k", "d"),
        "Stochastic applied to RSI",
    ))
    register(IndicatorSpec(
        "CCI", "momentum", momentum.cci, momentum.CCI,
        {"period": 20}, (),
        "Commodity channel index",
    ))
    register(IndicatorSpec(
        "WILLIAMSR", "momentum", momentum.williams_r, momentum.WilliamsR,
        {"period": 14}, (),
        "Williams %R, ranges -100 to 0",
    ))
    register(IndicatorSpec(
        "ROC", "momentum", momentum.roc, momentum.ROC,
        {"period": 12, "source": "close"}, (),
        "Rate of change, percent",
    ))
    register(IndicatorSpec(
        "MFI", "momentum", momentum.mfi, momentum.MFI,
        {"period": 14}, (),
        "Money flow index, volume-weighted RSI",
        needs_volume=True,
    ))

    # --- Volatility --------------------------------------------------------
    register(IndicatorSpec(
        "ATR", "volatility", volatility.atr, volatility.ATR,
        {"period": 14}, (),
        "Average true range, Wilder-smoothed",
    ))
    register(IndicatorSpec(
        "BOLLINGER", "volatility", volatility.bollinger, volatility.Bollinger,
        {"period": 20, "std_dev": 2.0, "source": "close"},
        ("middle", "upper", "lower", "bandwidth", "percent_b"),
        "Bollinger bands, population stdev",
    ))
    register(IndicatorSpec(
        "KELTNER", "volatility", volatility.keltner, volatility.Keltner,
        {"period": 20, "atr_period": 10, "multiplier": 2.0},
        ("middle", "upper", "lower"),
        "Keltner channels, EMA centre with ATR bands",
    ))
    register(IndicatorSpec(
        "DONCHIAN", "volatility", volatility.donchian, volatility.Donchian,
        {"period": 20}, ("upper", "lower", "middle"),
        "Donchian channels; shift by one bar for breakout logic",
    ))
    register(IndicatorSpec(
        "HISTVOL", "volatility", volatility.historical_volatility,
        volatility.HistoricalVolatility,
        {"period": 20, "bars_per_year": 252, "source": "close"}, (),
        "Annualized stdev of log returns, percent; set bars_per_year per timeframe",
    ))

    # --- Volume ------------------------------------------------------------
    register(IndicatorSpec(
        "VOLUMESMA", "volume", volume.volume_sma, volume.VolumeSMA,
        {"period": 20}, (),
        "Simple moving average of volume",
        needs_volume=True,
    ))
    register(IndicatorSpec(
        "VOLUMERATIO", "volume", volume.volume_ratio, volume.VolumeRatio,
        {"period": 20}, (),
        "Volume divided by its rolling average; >2 is a spike",
        needs_volume=True,
    ))
    register(IndicatorSpec(
        "OBV", "volume", volume.obv, volume.OBV,
        {}, (),
        "On-balance volume",
        needs_volume=True,
    ))
    register(IndicatorSpec(
        "ROLLINGVWAP", "volume", volume.rolling_vwap, volume.RollingVWAP,
        {"period": 20}, (),
        "Volume-weighted average price over a fixed lookback",
        needs_volume=True,
    ))


_register_all()
