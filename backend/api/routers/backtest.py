"""Backtests and cost previews."""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

import pandas as pd
from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.analytics import metrics as M
from backend.api.deps import current_context
from backend.app.context import AppContext
from backend.app.runs import RunStore
from backend.data.fingerprint import fingerprint
from backend.execution.costs import Segment, breakeven_move, round_trip_cost
from backend.indicators import registry
from backend.strategy.runner import FillModel, preflight, run_backtest
from backend.strategy.schema import Strategy

from fastapi import APIRouter

from backend.api.common import (
    MAX_CHART_BARS,
    _candles_payload,
    _downsample,
    _epoch,
    _format_validation_error,
    _load_candles,
)

log = logging.getLogger(__name__)

router = APIRouter()


class BacktestRequest(BaseModel):
    strategy: dict[str, Any]
    symbol: str | None = None          # overrides the document's instrument
    start: date | None = None
    end: date | None = None
    starting_capital: float = Field(default=1_000_000, gt=0)
    lot_size: int = Field(default=1, ge=1, le=10_000)
    slippage_bps: float | None = None
    max_leverage: float = Field(default=5.0, gt=0, le=50)
    include_chart: bool = True
    # The saved strategy this document is a version of, if any. Links the run
    # into that strategy's history and its family's trial count.
    library_id: str | None = None


@router.post("/api/backtest")
def backtest(req: BacktestRequest, ctx: AppContext = Depends(current_context)) -> dict:
    try:
        strategy = Strategy.model_validate(req.strategy)
    except Exception as exc:
        raise HTTPException(422, {"errors": _format_validation_error(exc)}) from exc

    symbol = req.symbol or strategy.instrument.symbol
    df = _load_candles(symbol, strategy.timeframe, req.start, req.end)

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

    runs = RunStore(ctx)
    data_print = fingerprint(df, symbol, strategy.timeframe)
    run_id = runs.start(
        "backtest", strategy.content_hash(), strategy_id=req.library_id,
        params=req.model_dump(mode="json", exclude={"strategy", "include_chart", "library_id"})
        | {"symbol": symbol},
    )
    try:
        result = run_backtest(
            strategy, df,
            starting_capital=capital,
            lot_size=req.lot_size,
            fill_model=fill_model,
            risk=guard,
            collect_events=req.include_chart,
        )
        stats = M.compute_metrics(result.equity_curve, result.trades, capital, strategy.timeframe)
    except Exception as exc:
        runs.fail(run_id, f"{type(exc).__name__}: {exc}")
        raise
    runs.finish(run_id, metrics=stats.as_dict(), data_fingerprint=data_print)
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
        "run_id": run_id,
        "data_fingerprint": data_print,
        "strategy_id": result.strategy_id,
        "strategy_hash": result.strategy_hash,
        "symbol": symbol,
        "timeframe": strategy.timeframe,
        "segment": segment.value,
        "synthetic_data": symbol.startswith("SIM|"),
        "warnings": warnings,
        "metrics": stats.as_dict(),
        "split": split,
        "trades": [t.as_dict() for t in result.trades],
        "equity_curve": _equity_payload(result.equity_curve),
        "costs": _cost_context(segment, req.lot_size, df),
        "blocked_reasons": _blocked_summary(result),
    }

    if req.include_chart:
        payload["chart"] = _chart_payload(strategy, df, result)

    return payload


def _equity_payload(curve: list[tuple[datetime, Decimal]]) -> list[dict]:
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
        out.append({"time": _epoch(ts), "equity": round(equity, 2), "drawdown": round(drawdown, 3)})
    return out


def _chart_payload(strategy: Strategy, df: pd.DataFrame, result) -> dict:
    """Candles, indicator overlays and trade markers for the chart tab."""
    from backend.strategy import conditions as C
    from backend.strategy.indicator_set import VectorizedIndicatorSet

    view = _downsample(df)
    first_time = _epoch(view["timestamp"].iloc[0])

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
                {"time": _epoch(t), "value": None if v != v else round(float(v), 4)}
                for t, v in zip(view["timestamp"], series.iloc[tail:])
            ]
            points = [p for p in points if p["value"] is not None]
            entry = {"id": ref, "type": spec.type, "output": output or "value", "data": points}
            (overlays if spec.type.upper() in price_like else panes).append(entry)

    markers = []
    for trade in result.trades:
        if _epoch(trade.entry_time) < first_time:
            continue
        markers.append({
            "time": _epoch(trade.entry_time),
            "position": "belowBar" if trade.direction == "LONG" else "aboveBar",
            "color": "#0B6B4F" if trade.direction == "LONG" else "#A32218",
            "shape": "arrowUp" if trade.direction == "LONG" else "arrowDown",
            "text": f"{trade.direction} {trade.quantity}",
        })
        markers.append({
            "time": _epoch(trade.exit_time),
            "position": "aboveBar" if trade.direction == "LONG" else "belowBar",
            "color": "#0B6B4F" if trade.is_win else "#A32218",
            "shape": "circle",
            "text": f"{trade.reason.value} {float(trade.net_pnl):+,.0f}",
        })

    return {
        "candles": _candles_payload(view),
        "overlays": overlays,
        "panes": panes,
        "markers": sorted(markers, key=lambda m: m["time"]),
        "truncated": len(df) > len(view),
        "total_bars": len(df),
    }


def _cost_context(segment: Segment, lot_size: int, df: pd.DataFrame) -> dict:
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


def _blocked_summary(result) -> dict[str, int]:
    """How often each risk rule refused an entry.

    Surfacing this is what turns "why did it only take 3 trades?" from a mystery
    into an answer.
    """
    counts: dict[str, int] = {}
    for event in result.events:
        if event.blocked and event.blocked.value != "none":
            counts[event.blocked.value] = counts.get(event.blocked.value, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


@router.get("/api/costs/preview")
def cost_preview(
    segment: Literal[
        "equity_delivery", "equity_intraday", "futures", "options",
        "commodity_futures", "commodity_options",
    ],
    quantity: int = Query(gt=0),
    price: float = Query(gt=0),
) -> dict:
    """Cost calculator for the builder's sidebar."""
    seg = Segment(segment)
    rt = round_trip_cost(seg, quantity, Decimal(str(price)))
    return {
        "round_trip": rt.as_dict(),
        "breakeven_move": float(breakeven_move(seg, quantity, Decimal(str(price)))),
    }
