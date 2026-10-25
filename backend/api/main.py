"""FastAPI application — the frontend's only door into the engine.

Design rule: this layer contains no trading logic. It validates input, calls the
same functions the CLI calls, and shapes the result for the UI. If a calculation
lives here, it can diverge from what a backtest or a live session does, and the
one-engine guarantee is gone.

Run (local only; requests with a non-local Host or Origin are rejected):
    .venv/bin/uvicorn backend.api.main:app --reload --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.analytics import metrics as M
from backend.api.security import LocalOnlyMiddleware
from backend.core.config import IST, settings
from backend.core.log_setup import configure_logging
from backend.data import instruments as inst
from backend.data.calendar import find_gaps
from backend.data.store import all_coverage, read_candles, read_coverage, write_candles
from backend.execution.costs import Segment, breakeven_move, round_trip_cost
from backend.indicators import registry
from backend.strategy import templates
from backend.strategy.runner import FillModel, preflight, run_backtest
from backend.strategy.schema import Strategy

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    from backend.live.manager import manager

    interrupted = manager.interrupted()
    if interrupted:
        # Never resumed automatically: the user may have closed the position by
        # hand, or may not want the session back at all.
        log.warning(
            "%d paper session(s) were interrupted today and can be resumed from the "
            "Live tab: %s",
            len(interrupted), ", ".join(s["session_id"] for s in interrupted),
        )
    log.info("Trade Terminal API started (data dir %s)", settings.data_dir)
    yield
    log.info("Shutting down: stopping running sessions")
    manager.stop_all("shutdown")


app = FastAPI(
    lifespan=lifespan,
    title="Trade Terminal",
    description="Visual strategy definition, backtesting, paper trading and live execution.",
    version="0.5.0",
)

# The Vite dev server runs on a different port; in production the frontend is
# served as static files from this same origin and CORS becomes irrelevant.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Added last so it runs first: nothing, not even CORS, sees a foreign request.
app.add_middleware(LocalOnlyMiddleware)

# Chart payloads are capped so a multi-year 1-minute backtest does not try to
# ship a million candles to a browser that can only render a few thousand.
MAX_CHART_BARS = 4000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _downsample(df: pd.DataFrame, limit: int = MAX_CHART_BARS) -> pd.DataFrame:
    """Keep the most recent `limit` bars.

    Truncating the tail rather than thinning the series: a thinned candle chart
    shows highs and lows that never happened, and traders read those wicks.
    """
    return df.tail(limit) if len(df) > limit else df


def _epoch(ts) -> int:
    value = pd.Timestamp(ts)
    if value.tz is None:
        value = value.tz_localize(IST)
    return int(value.timestamp())


def _candles_payload(df: pd.DataFrame) -> list[dict]:
    return [
        {
            "time": _epoch(row.timestamp),
            "open": float(row.open), "high": float(row.high),
            "low": float(row.low), "close": float(row.close),
            "volume": int(row.volume),
        }
        for row in df.itertuples(index=False)
    ]


def _load_candles(symbol: str, timeframe: str, start: date | None, end: date | None) -> pd.DataFrame:
    key = symbol if "|" in symbol else _resolve_or_400(symbol)
    df = read_candles(key, timeframe, start, end)
    if df.empty:
        raise HTTPException(
            404,
            f"No stored candles for {key} {timeframe}. "
            f"Fetch them first (POST /api/data/fetch) or generate synthetic data.",
        )
    return df


def _resolve_or_400(symbol: str) -> str:
    try:
        return inst.resolve(symbol)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# Status and reference data
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": app.version}


@app.get("/api/status")
def status() -> dict:
    """Auth, instrument master and stored-data status for the header bar."""
    from backend.data.upstox_auth import token_status

    ts = token_status()
    try:
        instrument_count = inst.count()
    except Exception:
        instrument_count = 0

    coverage = all_coverage()
    return {
        "auth": {
            "valid": ts.valid,
            "reason": ts.reason,
            "expires_at": ts.expires_at.isoformat() if ts.expires_at else None,
            "hours_remaining": round(ts.seconds_remaining / 3600, 1),
        },
        "instruments": instrument_count,
        "datasets": len(coverage),
        "mode": settings.mode.value,
        "risk_configured": settings.max_daily_loss is not None,
    }


@app.get("/api/indicators")
def list_indicators() -> dict:
    """The indicator palette. The UI builds its menu from this, so a newly
    registered indicator appears without a frontend change."""
    return {"indicators": registry.describe_all(), "categories": registry.by_category()}


@app.get("/api/templates")
def list_templates() -> dict:
    return {"templates": templates.list_templates()}


@app.get("/api/templates/{key}")
def get_template(key: str, symbol: str | None = None) -> dict:
    try:
        strategy = templates.load(key, **({"symbol": symbol} if symbol else {}))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return strategy.model_dump(mode="json")


@app.get("/api/instruments/search")
def search_instruments(q: str = Query(min_length=1), limit: int = 20) -> dict:
    try:
        found = inst.search(q, limit)
    except Exception as exc:
        raise HTTPException(503, f"Instrument master not synced: {exc}") from exc

    return {
        "results": [
            {
                "instrument_key": i.instrument_key,
                "trading_symbol": i.trading_symbol,
                "name": i.name,
                "exchange": i.exchange,
                "segment": i.segment,
                "instrument_type": i.instrument_type,
                "lot_size": i.lot_size,
                "expiry": i.expiry.isoformat() if i.expiry else None,
                "strike_price": i.strike_price,
            }
            for i in found
        ]
    }


@app.get("/api/instruments/aliases")
def instrument_aliases() -> dict:
    """Friendly index names, for the picker's quick-select row."""
    return {"aliases": inst.INDICES}


@app.get("/api/datasets")
def datasets() -> dict:
    """Everything with stored candles — what the user can actually backtest."""
    coverage = all_coverage()
    if coverage.empty:
        return {"datasets": []}

    rows = []
    for _, r in coverage.iterrows():
        rows.append({
            "instrument_key": r.instrument_key,
            "timeframe": r.timeframe,
            "bars": int(r.bar_count),
            "first_bar": str(r.first_bar)[:19],
            "last_bar": str(r.last_bar)[:19],
            "source": r.source,
            "synthetic": r.source == "simulated",
        })
    return {"datasets": rows}


# ---------------------------------------------------------------------------
# Candles
# ---------------------------------------------------------------------------

@app.get("/api/candles")
def candles(
    symbol: str,
    timeframe: str = "5m",
    start: date | None = None,
    end: date | None = None,
    limit: int = MAX_CHART_BARS,
) -> dict:
    df = _load_candles(symbol, timeframe, start, end)
    total = len(df)
    df = _downsample(df, limit)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "total_bars": total,
        "returned": len(df),
        "candles": _candles_payload(df),
    }


class FetchRequest(BaseModel):
    symbol: str
    timeframe: str = "5m"
    from_date: date | None = None
    days: int = Field(default=365, ge=1, le=3650)


@app.post("/api/data/fetch")
def fetch_data(req: FetchRequest) -> dict:
    """Pull history from Upstox into the local store."""
    from datetime import timedelta

    from backend.data.upstox_client import fetch_candles

    key = _resolve_or_400(req.symbol)
    start = req.from_date or (date.today() - timedelta(days=req.days))

    try:
        df = fetch_candles(key, req.timeframe, start)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc

    if df.empty:
        raise HTTPException(404, f"Upstox returned no data for {key} {req.timeframe}")

    total = write_candles(key, req.timeframe, df, source="upstox")
    report = find_gaps(read_candles(key, req.timeframe), key, req.timeframe)
    return {
        "instrument_key": key,
        "fetched": len(df),
        "total_bars": total,
        "coverage_pct": round(report.coverage_pct, 2),
        "missing_sessions": len(report.missing_sessions),
        "clean": report.is_clean,
    }


class SimulateRequest(BaseModel):
    profile: str
    timeframe: str = "5m"
    days: int = Field(default=365, ge=1, le=3650)
    seed: int = 42
    vol_multiplier: float = Field(default=1.0, gt=0, le=10)


@app.post("/api/data/simulate")
def simulate_data(req: SimulateRequest) -> dict:
    """Generate synthetic candles. Namespaced SIM| so it can never be confused
    with real history."""
    from datetime import timedelta

    from backend.data.simulator import PROFILES, generate_ohlcv

    if req.profile.upper() not in PROFILES:
        raise HTTPException(400, f"Unknown profile. Available: {sorted(PROFILES)}")

    end = date.today()
    df = generate_ohlcv(
        req.profile.upper(), end - timedelta(days=req.days), end,
        req.timeframe, seed=req.seed, vol_multiplier=req.vol_multiplier,
    )
    key = f"SIM|{req.profile.upper()}"
    total = write_candles(key, req.timeframe, df, source="simulated")
    return {
        "instrument_key": key, "bars": len(df), "total_bars": total, "synthetic": True,
    }


@app.get("/api/simulator/profiles")
def simulator_profiles() -> dict:
    from backend.data.simulator import PROFILES

    return {
        "profiles": [
            {
                "name": p.name, "spot": p.spot, "annual_vol": p.annual_vol,
                "lot_size": p.lot_size, "is_mcx": p.is_mcx,
            }
            for p in PROFILES.values()
        ]
    }


# ---------------------------------------------------------------------------
# Strategy validation
# ---------------------------------------------------------------------------

@app.post("/api/strategies/validate")
def validate_strategy(document: dict[str, Any]) -> dict:
    """Validate a strategy document and return its hash.

    The builder calls this on every edit, so a typo in an indicator reference
    surfaces immediately rather than as an empty trade log later.
    """
    try:
        strategy = Strategy.model_validate(document)
    except Exception as exc:
        return {"valid": False, "errors": _format_validation_error(exc)}

    return {
        "valid": True,
        "id": strategy.id,
        "hash": strategy.content_hash(),
        "summary": strategy.summary(),
        "is_intraday": strategy.is_intraday,
        "has_options": strategy.has_options,
        "document": strategy.model_dump(mode="json"),
    }


def _format_validation_error(exc: Exception) -> list[dict]:
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


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

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


@app.post("/api/backtest")
def backtest(req: BacktestRequest) -> dict:
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

    result = run_backtest(
        strategy, df,
        starting_capital=capital,
        lot_size=req.lot_size,
        fill_model=fill_model,
        risk=guard,
        collect_events=req.include_chart,
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
    from backend.strategy.indicator_set import VectorizedIndicatorSet
    from backend.strategy import conditions as C

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


@app.get("/api/costs/preview")
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


# ---------------------------------------------------------------------------
# Live / paper sessions
# ---------------------------------------------------------------------------

class LiveStartRequest(BaseModel):
    strategy: dict[str, Any]
    symbol: str | None = None
    starting_capital: float = Field(default=1_000_000, gt=0)
    lot_size: int = Field(default=1, ge=1, le=10_000)
    slippage_bps: float | None = None
    max_leverage: float = Field(default=5.0, gt=0, le=50)
    warmup_bars: int = Field(default=400, ge=50, le=5000)
    alerts: bool = True

    # "live" polls Upstox during market hours. "replay" runs the identical path
    # over stored candles, which is how you rehearse a strategy at 2am — and the
    # only way to exercise the live loop end to end outside a session.
    source: Literal["live", "replay"] = "live"
    replay_start: date | None = None
    replay_end: date | None = None
    replay_speed: float = Field(default=0.0, ge=0, le=3600)
    poll_seconds: float = Field(default=5.0, ge=1.0, le=60.0)


@app.post("/api/live/start")
def live_start(req: LiveStartRequest) -> dict:
    from backend.core.config import Mode
    from backend.live.manager import manager
    from backend.live.session import SessionConfig
    from backend.live.sources import ReplaySource, RestPollingSource

    try:
        strategy = Strategy.model_validate(req.strategy)
    except Exception as exc:
        raise HTTPException(422, {"errors": _format_validation_error(exc)}) from exc

    symbol = _resolve_or_400(req.symbol or strategy.instrument.symbol)

    history: pd.DataFrame | None = None
    history_before: datetime | None = None

    if req.source == "replay":
        # The feed gets the whole stored frame and the window boundary, rather
        # than a pre-sliced warm-up: on a resume it has to be able to reach past
        # that boundary, up to the last bar the journal recorded.
        #
        # Warming up on the newest bars in the store instead — which is what a
        # live session correctly does — would evaluate the opening bars of an old
        # window against indicator values drawn from months later.
        history = _load_candles(symbol, strategy.timeframe, None, req.replay_end)
        if req.replay_start is not None:
            history_before = pd.Timestamp(req.replay_start).tz_localize(IST).to_pydatetime()
            df = history[history["timestamp"] >= history_before].reset_index(drop=True)
        else:
            df = history
        if df.empty:
            raise HTTPException(404, f"No stored candles for {symbol} in that replay window")
        source = ReplaySource(df, symbol, speed=req.replay_speed)
    else:
        from backend.data.upstox_auth import token_status

        status = token_status()
        if not status.valid:
            # Better to refuse at the door than to start a session that will fail
            # on its first poll and look like a dead feed.
            raise HTTPException(400, f"Upstox token is not usable: {status.reason}")
        source = RestPollingSource(symbol, strategy.timeframe, poll_seconds=req.poll_seconds)

    config = SessionConfig(
        strategy=strategy,
        instrument_key=symbol,
        starting_capital=Decimal(str(req.starting_capital)),
        lot_size=req.lot_size,
        slippage_bps=(
            req.slippage_bps if req.slippage_bps is not None else strategy.costs.slippage_bps
        ),
        max_leverage=Decimal(str(req.max_leverage)),
        warmup_bars=req.warmup_bars,
        mode=Mode.PAPER,
        alerts_enabled=req.alerts,
        source_kind=req.source,
    )

    session, started = manager.start(
        config, source=source, history_frame=history, history_before=history_before
    )
    return {
        "session_id": session.journal.session_id,
        "started": started,
        "already_running": not started,
        "source": req.source,
        "snapshot": manager.snapshot(session.journal.session_id) or {},
    }


@app.post("/api/live/{session_id}/stop")
def live_stop(session_id: str) -> dict:
    from backend.live.manager import manager

    if not manager.stop(session_id):
        raise HTTPException(404, f"No session {session_id} in this process")
    return {"session_id": session_id, "snapshot": manager.snapshot(session_id)}


@app.get("/api/live/sessions")
def live_sessions() -> dict:
    from backend.live.manager import manager

    return {"active": manager.active(), "history": manager.history()}


@app.get("/api/live/interrupted")
def live_interrupted() -> dict:
    """Sessions cut off by a crash or shutdown today, which the user may resume."""
    from backend.live.manager import manager

    return {"sessions": manager.interrupted()}


@app.get("/api/live/{session_id}")
def live_snapshot(session_id: str, include_bars: bool = True) -> dict:
    """Poll target for the dashboard."""
    from backend.live.manager import manager

    snapshot = manager.snapshot(session_id, include_bars=include_bars)
    if snapshot is not None:
        return snapshot

    # Not in this process — reconstruct what the journal knows. This is what
    # makes a session survive an API restart as something you can still read.
    from backend.live import journal as J

    state = J.recover(session_id)
    if not state.started:
        raise HTTPException(404, f"No session {session_id}")
    return {
        "session_id": session_id,
        "state": "interrupted" if state.resumable else "ended",
        "running": False,
        "from_journal": True,
        "strategy": {"name": state.strategy.get("name", "?"), "hash": state.strategy_hash},
        "symbol": state.symbol,
        "timeframe": state.timeframe,
        "bars_seen": state.bars_seen,
        "trades": state.trades,
        "open_positions": [
            state.positions[leg] for leg in state.open_legs
        ],
        "errors": state.errors,
    }
