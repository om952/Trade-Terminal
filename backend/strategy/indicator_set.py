"""IndicatorSet — one interface, two engines underneath.

The strategy engine asks for "the value of `ema_fast` on the current bar" and
does not care whether that came from a precomputed column or a streaming object.
That indifference is what keeps the engine identical across backtest, paper and
live.

  * `VectorizedIndicatorSet` precomputes every indicator over the whole frame,
    then serves values by row index. Fast, used for backtests.
  * `StreamingIndicatorSet` holds live indicator objects and advances them one
    bar at a time. Used for paper and live.

Phase 2 proved these two produce identical numbers to 1e-9, so which one is in
play cannot change a strategy's decisions.

Both also expose `previous()`, because crossover detection needs the prior bar's
value — and reconstructing that from raw history at the strategy layer is how
lookahead bugs get introduced.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

from backend.indicators import registry
from backend.indicators.base import PriceBar, source_value
from backend.strategy.schema import InlineIndicator, IndicatorSpec

NAN = float("nan")


def _resolve_key(ref: str) -> tuple[str, str | None]:
    """'macd.signal' -> ('macd', 'signal'); 'ema_fast' -> ('ema_fast', None)."""
    if "." in ref:
        base, _, output = ref.partition(".")
        return base, output
    return ref, None


def inline_key(spec: InlineIndicator) -> str:
    """Deterministic name for an inline indicator, so identical ones share state."""
    params = ",".join(f"{k}={v}" for k, v in sorted(spec.params.items()))
    return f"__inline_{spec.type.lower()}_{spec.of}_{params}".replace(" ", "")


class IndicatorSet(ABC):
    """Values for the current bar, plus the previous bar for crossovers."""

    @abstractmethod
    def value(self, ref: str) -> float:
        """Current-bar value for an indicator reference or price field."""

    @abstractmethod
    def previous(self, ref: str) -> float:
        """Previous-bar value. NaN when there is no previous bar."""

    @abstractmethod
    def bar(self) -> PriceBar:
        """The current bar."""

    def context(self) -> dict[str, float]:
        """Session-scoped values injected by the engine (pivots, VWAP, PnL)."""
        return self._context

    def set_context(self, values: dict[str, float]) -> None:
        """Per-bar context. Merged into the same value space as indicators.

        Context must live alongside indicator values, not beside them, because
        `previous()` has to reach it. Keeping it in a separate dict made
        `previous("tc")` return NaN forever, which silently made every crossover
        against a pivot level impossible — the condition could not fire, and the
        strategy just looked like it never found a setup.
        """
        self._context = values


# ---------------------------------------------------------------------------
# Vectorized
# ---------------------------------------------------------------------------

class VectorizedIndicatorSet(IndicatorSet):
    """Precomputes all indicators, then serves values by row index."""

    def __init__(
        self,
        df: pd.DataFrame,
        specs: list[IndicatorSpec],
        inline: list[InlineIndicator] | None = None,
    ) -> None:
        self._df = df.reset_index(drop=True)
        self._columns: dict[str, pd.Series] = {}
        self._context: dict[str, float] = {}
        self._i = -1

        for field in ("open", "high", "low", "close", "volume"):
            self._columns[field] = self._df[field].astype("float64")
        for derived in ("hl2", "hlc3", "ohlc4"):
            self._columns[derived] = self._compute_derived(derived)

        for spec in specs:
            result = registry.compute(spec.type, self._df, **spec.params)
            if isinstance(result, pd.Series):
                self._columns[spec.id] = result.astype("float64")
            else:
                for col in result.columns:
                    self._columns[f"{spec.id}.{col}"] = result[col].astype("float64")
                # Bare id resolves to the indicator's primary output.
                self._columns[spec.id] = result[result.columns[0]].astype("float64")

        for spec in inline or []:
            key = inline_key(spec)
            if key in self._columns:
                continue
            result = registry.compute(spec.type, self._source_frame(spec.of), **spec.params)
            series = result if isinstance(result, pd.Series) else result[result.columns[0]]
            self._columns[key] = series.astype("float64")

    def _source_frame(self, source: str) -> pd.DataFrame:
        """A frame whose OHLC all carry `source`, for indicators over non-price data.

        `SMA(volume, 20)` means "the SMA of volume", so volume has to arrive
        where the indicator looks for its input. Renaming the column in place
        would leave two columns named 'close' and make every lookup ambiguous,
        so this builds a fresh frame instead.
        """
        if source == "close":
            return self._df

        values = self._columns[source] if source in self._columns else self._df[source]
        values = values.astype("float64")
        return pd.DataFrame(
            {
                "open": values, "high": values, "low": values, "close": values,
                "volume": self._df["volume"].astype("float64"),
            }
        )

    def _compute_derived(self, name: str) -> pd.Series:
        d = self._df
        if name == "hl2":
            return ((d["high"] + d["low"]) / 2).astype("float64")
        if name == "hlc3":
            return ((d["high"] + d["low"] + d["close"]) / 3).astype("float64")
        return ((d["open"] + d["high"] + d["low"] + d["close"]) / 4).astype("float64")

    def attach_context(self, frame: pd.DataFrame) -> None:
        """Register session context as ordinary columns.

        Once attached, `pivot` and `tc` are looked up exactly like `ema_fast`,
        so `previous()` works on them and crossovers against pivot levels behave.
        """
        for column in frame.columns:
            self._columns[column] = frame[column].astype("float64").reset_index(drop=True)

    def seek(self, index: int) -> None:
        self._i = index

    def value(self, ref: str) -> float:
        if ref in self._context:
            return self._context[ref]
        series = self._columns.get(ref)
        if series is None:
            return NAN
        return float(series.iloc[self._i])

    def previous(self, ref: str) -> float:
        if self._i <= 0:
            return NAN
        series = self._columns.get(ref)
        if series is None:
            return NAN
        return float(series.iloc[self._i - 1])

    def bar(self) -> PriceBar:
        row = self._df.iloc[self._i]
        return PriceBar(
            float(row["open"]), float(row["high"]), float(row["low"]),
            float(row["close"]), float(row["volume"]),
        )

    @property
    def length(self) -> int:
        return len(self._df)

    def timestamp(self, index: int | None = None):
        return self._df["timestamp"].iloc[self._i if index is None else index]

    def warmup_bars(self) -> int:
        """Bars before every indicator has a value. Trading before this is noise."""
        longest = 0
        for name, series in self._columns.items():
            if name in ("open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4"):
                continue
            first_valid = series.first_valid_index()
            if first_valid is None:
                return self.length  # never warms up
            longest = max(longest, int(first_valid))
        return longest


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

class StreamingIndicatorSet(IndicatorSet):
    """Holds live indicator objects, advanced one bar at a time."""

    def __init__(
        self,
        specs: list[IndicatorSpec],
        inline: list[InlineIndicator] | None = None,
    ) -> None:
        self._indicators: dict[str, Any] = {}
        self._outputs: dict[str, tuple[str, ...]] = {}
        self._inline_source: dict[str, str] = {}
        self._current: dict[str, float] = {}
        self._prev: dict[str, float] = {}
        self._context: dict[str, float] = {}
        self._bar: PriceBar | None = None
        self._seen = 0

        for spec in specs:
            self._indicators[spec.id] = registry.create_streaming(spec.type, **spec.params)
            self._outputs[spec.id] = registry.get(spec.type).outputs

        for spec in inline or []:
            key = inline_key(spec)
            if key in self._indicators:
                continue
            self._indicators[key] = registry.create_streaming(spec.type, **spec.params)
            self._outputs[key] = registry.get(spec.type).outputs
            self._inline_source[key] = spec.of

    def update(self, bar: PriceBar) -> None:
        self._prev = dict(self._current)
        self._bar = bar
        self._seen += 1

        current: dict[str, float] = {
            "open": bar.open, "high": bar.high, "low": bar.low,
            "close": bar.close, "volume": bar.volume,
            "hl2": bar.hl2, "hlc3": bar.hlc3, "ohlc4": bar.ohlc4,
        }

        for key, indicator in self._indicators.items():
            feed = bar
            source = self._inline_source.get(key)
            if source and source != "close":
                # An inline indicator over volume must see volume as its close.
                value = source_value(bar, source)
                feed = PriceBar(value, value, value, value, bar.volume)

            result = indicator.update(feed)
            outputs = self._outputs.get(key, ())

            if result is None:
                current[key] = NAN
                for out in outputs:
                    current[f"{key}.{out}"] = NAN
            elif isinstance(result, dict):
                for out in outputs:
                    current[f"{key}.{out}"] = float(result.get(out, NAN))
                current[key] = float(result.get(outputs[0], NAN)) if outputs else NAN
            else:
                current[key] = float(result)

        self._current = current

    def set_context(self, values: dict[str, float]) -> None:
        """Merge session context into the current bar's value space.

        This must write into `_current`, not sit beside it, for the same reason
        `VectorizedIndicatorSet.attach_context` exists: the next `update()` copies
        `_current` into `_prev`, and `previous()` reads `_prev`. Context held in a
        separate dict is invisible to `previous()`, so `crosses_above("close",
        "tc")` can never fire — the condition evaluates against NaN forever and
        the strategy merely looks like it found no setups.

        The vectorized path was fixed for this; leaving the streaming path unfixed
        would mean a pivot strategy that back-tests fine takes zero trades live.

        Call this *after* `update()` for the bar.
        """
        self._context = values
        self._current.update(values)

    def value(self, ref: str) -> float:
        return self._current.get(ref, NAN)

    def previous(self, ref: str) -> float:
        if self._seen <= 1:
            return NAN
        return self._prev.get(ref, NAN)

    def bar(self) -> PriceBar:
        if self._bar is None:
            raise RuntimeError("No bar consumed yet")
        return self._bar

    @property
    def ready(self) -> bool:
        """Whether every indicator has a real value yet.

        Context fields are excluded. Pivots are NaN through the first session by
        definition — there is no prior day to derive them from — so counting them
        would keep a strategy "warming up" for a whole day after its indicators
        were long since valid.
        """
        skip = {"open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4"}
        return all(
            not math.isnan(v)
            for k, v in self._current.items()
            if k not in skip and k not in self._context
        )
