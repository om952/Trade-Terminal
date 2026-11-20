"""Backtest as a service: one function behind every way of running one.

The HTTP endpoint, the background job runner and the agent's backtest tool all
call `run`. That is what keeps the one-engine guarantee true above the engine:
if each caller assembled its own fill model, risk guard and metrics, they would
drift, and the agent's numbers would stop matching the UI's.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from backend.analytics import metrics as M
from backend.core.config import IST
from backend.data import cache
from backend.data import instruments as inst
from backend.execution.costs import Segment, breakeven_move, round_trip_cost
from backend.indicators import registry
from backend.strategy.runner import FillModel, preflight, run_backtest
from backend.strategy.schema import Strategy

Progress = Callable[[float, str], None]

# Share of a job's progress bar given to loading data; the rest is the engine.
DATA_SHARE = 0.4


class InvalidStrategy(ValueError):
    """The document does not validate. `errors` points at the fields."""

    def __init__(self, errors: list[dict]) -> None:
        super().__init__("; ".join(f"{e['field']}: {e['message']}" for e in errors))
        self.errors = errors


class InvalidSymbol(ValueError):
    pass


class DataUnavailable(LookupError):
    """No bars for the request. The message says why and what to do."""


class BacktestParams(BaseModel):
    strategy: dict[str, Any]
    symbol: str | None = None          # overrides the document's instrument
    start: date | None = None
    end: date | None = None
    starting_capital: float = Field(default=1_000_000, gt=0)
    lot_size: int = Field(default=1, ge=1, le=10_000)
    slippage_bps: float | None = None
    max_leverage: float = Field(default=5.0, gt=0, le=50)
    include_chart: bool = True



def validate(document: dict[str, Any]) -> Strategy:
    try:
        return Strategy.model_validate(document)
    except Exception as exc:
        raise InvalidStrategy(format_validation_error(exc)) from exc


def resolve(symbol: str) -> str:
    if "|" in symbol:
        return symbol
    try:
        return inst.resolve(symbol)
    except KeyError as exc:
        raise InvalidSymbol(str(exc)) from exc


def load_bars(
    symbol: str,
    timeframe: str,
    start: date | None,
    end: date | None,
    on_progress: Progress | None = None,
) -> cache.Bars:
    """Bars through the read-through cache: missing sessions are downloaded first."""
    key = resolve(symbol)
    bars = cache.get_bars(key, timeframe, start, end, on_progress=on_progress)
    if bars.empty:
        if key.startswith("SIM|"):
            hint = "Generate synthetic data first (POST /api/data/simulate)."
        elif cache.is_fetchable(key):
            hint = " ".join(bars.notes) or "Upstox returned no bars for that range."
        else:
            hint = "Import it first (python -m backend.data.ingest import-csv)."
        raise DataUnavailable(f"No candles for {key} {timeframe}. {hint}")
    return bars


def run(req: BacktestParams, *, on_progress: Progress | None = None) -> dict[str, Any]:
    """Validate, load data, run the engine and shape the report.

    `on_progress(fraction, message)` may raise to cancel; the exception
    propagates unchanged.
    """
    strategy = validate(req.strategy)
    symbol = req.symbol or strategy.instrument.symbol
    # Real market data stops before the final holdout (backend/services/holdout.py),
    # except in the one final test a strategy family is allowed.
    from backend.services import holdout

    first, last, held = holdout.bounds(resolve(symbol), req.start, req.end)
    bars = load_bars(
        symbol, strategy.timeframe, first, last,
        on_progress=_scaled(on_progress, 0.0, DATA_SHARE),
    )
    df = bars.frame

    warnings = preflight(strategy, df)
    capital = Decimal(str(req.starting_capital))

    from backend.execution.costs import segment_for
    from backend.strategy.risk import RiskGuard

    segment = segment_for(
        strategy.instrument.segment, strategy.is_intraday, strategy.legs[0].kind.value
    )
    fill_model = FillModel(
        slippage_bps=req.slippage_bps
        if req.slippage_bps is not None
        else strategy.costs.slippage_bps,
        segment=segment,
    )
    guard = RiskGuard(rules=strategy.risk, max_leverage=Decimal(str(req.max_leverage)))

    result = run_backtest(
        strategy, df,
        starting_capital=capital,
        lot_size=req.lot_size,
        fill_model=fill_model,
        risk=guard,
        collect_events=req.include_chart,
        on_progress=_bar_progress(on_progress, DATA_SHARE, 0.95),
    )

    stats = M.compute_metrics(result.equity_curve, result.trades, capital, strategy.timeframe)
    split = None
    if len(result.equity_curve) > 50:
        try:
            sp = M.split_metrics(result.equity_curve, result.trades, capital, strategy.timeframe)
            split = {
                "split_date": sp.split_date.isoformat(),
                "in_sample": sp.in_sample.as_dict(),
                "out_of_sample": sp.out_of_sample.as_dict(),
                "degradation_pct": round(sp.degradation_pct, 2),
                "verdict": sp.verdict,
            }
        except ValueError:
            split = None

    payload: dict[str, Any] = {
        "strategy_id": result.strategy_id,
        "strategy_hash": result.strategy_hash,
        "symbol": symbol,
        "timeframe": strategy.timeframe,
        "segment": segment.value,
        "synthetic_data": bars.synthetic,
        "data": bars.describe(),
        "warnings": warnings,
        "metrics": stats.as_dict(),
        "split": split,
        "trades": [t.as_dict() for t in result.trades],
        "equity_curve": equity_payload(result.equity_curve),
        "costs": cost_context(segment, req.lot_size, df),
        "blocked_reasons": blocked_summary(result),
        "holdout": held,
    }

    if req.include_chart:
        payload["chart"] = chart_payload(strategy, df, result)

    if on_progress:
        on_progress(1.0, "Backtest complete")
    return payload


def _scaled(on_progress: Progress | None, lo: float, hi: float) -> Progress | None:
    if on_progress is None:
        return None
    return lambda f, m: on_progress(lo + (hi - lo) * f, m)


def _bar_progress(on_progress: Progress | None, lo: float, hi: float):
    if on_progress is None:
        return None
    return lambda done, total: on_progress(
        lo + (hi - lo) * done / max(1, total), f"Backtesting: bar {done:,} of {total:,}"
    )


def format_validation_error(exc: Exception) -> list[dict]:
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



# Chart payloads are capped so a multi-year 1-minute backtest does not try to
# ship a million candles to a browser that can only render a few thousand.
MAX_CHART_BARS = 4000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def downsample(df: pd.DataFrame, limit: int = MAX_CHART_BARS) -> pd.DataFrame:
    """Keep the most recent `limit` bars.

    Truncating the tail rather than thinning the series: a thinned candle chart
    shows highs and lows that never happened, and traders read those wicks.
    """
    return df.tail(limit) if len(df) > limit else df


def epoch(ts) -> int:
    value = pd.Timestamp(ts)
    if value.tz is None:
        value = value.tz_localize(IST)
    return int(value.timestamp())


def candles_payload(df: pd.DataFrame) -> list[dict]:
    return [
        {
            "time": epoch(row.timestamp),
            "open": float(row.open), "high": float(row.high),
            "low": float(row.low), "close": float(row.close),
            "volume": int(row.volume),
        }
        for row in df.itertuples(index=False)
    ]


def equity_payload(curve: list[tuple[datetime, Decimal]]) -> list[dict]:
    """Equity and drawdown, downsampled for the browser."""
    if not curve:
        return []

    # Ceiling division: floor division rounds the step down, which lets the
    # result overshoot the cap (27,525 bars // 4,000 gives step 6 and 4,588
    # points). The cap exists to bound the payload, so it has to hold.
    step = max(1, -(-len(curve) // MAX_CHART_BARS))
    sampled = curve[::step]

    peak = float("-inf")
    out = []
    for ts, value in sampled:
        equity = float(value)
        peak = max(peak, equity)
        drawdown = (equity - peak) / peak * 100.0 if peak > 0 else 0.0
        out.append({"time": epoch(ts), "equity": round(equity, 2), "drawdown": round(drawdown, 3)})
    return out


def chart_payload(strategy: Strategy, df: pd.DataFrame, result) -> dict:
    """Candles, indicator overlays and trade markers for the chart tab."""
    from backend.strategy import conditions as C
    from backend.strategy.indicator_set import VectorizedIndicatorSet

    view = downsample(df)
    first_time = epoch(view["timestamp"].iloc[0])

    inline = C.collect_inline(strategy.entry) + C.collect_inline(strategy.exit)
    indicators = VectorizedIndicatorSet(df, strategy.indicators, inline)

    # Only overlay indicators that share the price scale; oscillators like RSI
    # would compress the candles into a flat line if drawn on the same axis.
    overlays: list[dict] = []
    panes: list[dict] = []
    price_like = {"SMA", "EMA", "WMA", "DEMA", "SUPERTREND", "BOLLINGER", "KELTNER", "DONCHIAN", "PSAR"}

    tail = len(df) - len(view)
    for spec in strategy.indicators:
        outputs = registry.get(spec.type).outputs or ("",)
        for output in outputs:
            ref = f"{spec.id}.{output}" if output else spec.id
            series = indicators._columns.get(ref)
            if series is None:
                continue
            points = [
                {"time": epoch(t), "value": None if v != v else round(float(v), 4)}
                for t, v in zip(view["timestamp"], series.iloc[tail:])
            ]
            points = [p for p in points if p["value"] is not None]
            entry = {"id": ref, "type": spec.type, "output": output or "value", "data": points}
            (overlays if spec.type.upper() in price_like else panes).append(entry)

    markers = []
    for trade in result.trades:
        if epoch(trade.entry_time) < first_time:
            continue
        markers.append({
            "time": epoch(trade.entry_time),
            "position": "belowBar" if trade.direction == "LONG" else "aboveBar",
            "color": "#0B6B4F" if trade.direction == "LONG" else "#A32218",
            "shape": "arrowUp" if trade.direction == "LONG" else "arrowDown",
            "text": f"{trade.direction} {trade.quantity}",
        })
        markers.append({
            "time": epoch(trade.exit_time),
            "position": "aboveBar" if trade.direction == "LONG" else "belowBar",
            "color": "#0B6B4F" if trade.is_win else "#A32218",
            "shape": "circle",
            "text": f"{trade.reason.value} {float(trade.net_pnl):+,.0f}",
        })

    return {
        "candles": candles_payload(view),
        "overlays": overlays,
        "panes": panes,
        "markers": sorted(markers, key=lambda m: m["time"]),
        "truncated": len(df) > len(view),
        "total_bars": len(df),
    }


def cost_context(segment: Segment, lot_size: int, df: pd.DataFrame) -> dict:
    """What a round trip costs at this instrument's price — the number that
    tells you whether the timeframe is even viable."""
    price = Decimal(str(float(df["close"].iloc[-1])))
    quantity = max(1, lot_size)
    rt = round_trip_cost(segment, quantity, price)
    return {
        "segment": segment.value,
        "reference_price": float(price),
        "quantity": quantity,
        "round_trip": rt.as_dict(),
        "breakeven_move": float(breakeven_move(segment, quantity, price)),
        "breakeven_pct": round(float(breakeven_move(segment, quantity, price) / price * 100), 4),
    }


def blocked_summary(result) -> dict[str, int]:
    """How often each risk rule refused an entry.

    Surfacing this is what turns "why did it only take 3 trades?" from a mystery
    into an answer.
    """
    counts: dict[str, int] = {}
    for event in result.events:
        if event.blocked and event.blocked.value != "none":
            counts[event.blocked.value] = counts.get(event.blocked.value, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


