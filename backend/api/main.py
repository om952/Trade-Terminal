"""FastAPI application — the frontend's only door into the engine.

Design rule: this layer contains no trading logic. It validates input, calls the
same functions the CLI calls, and shapes the result for the UI. If a calculation
lives here, it can diverge from what a backtest or a live session does, and the
one-engine guarantee is gone.

Run:
    .venv/bin/uvicorn backend.api.main:app --reload --host 127.0.0.1 --port 8000

Keep the host at 127.0.0.1. The API has no authentication; it relies on being
reachable only from this machine (see backend/api/security.py).
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.agent import service as agent
from backend.api.security import DEV_ORIGINS, LocalOnlyMiddleware, parse_extra_hosts
from backend.appstate import runs as run_history
from backend.appstate import strategies as saved_strategies
from backend.core.config import IST, settings
from backend.core.logsetup import configure_logging
from backend.core.version import __version__
from backend.data import cache
from backend.data import instruments as inst
from backend.data.store import all_coverage, write_candles
from backend.execution.costs import Segment, breakeven_move, round_trip_cost
from backend.indicators import registry
from backend.services import backtest as backtest_service
from backend.services import jobs as job_services
from backend.services.backtest import (
    MAX_CHART_BARS,
    candles_payload,
    downsample,
    format_validation_error,
)
from backend.services.jobs import runner as job_runner
from backend.strategy import templates
from backend.strategy.schema import Strategy

log = logging.getLogger(__name__)



def _report_interrupted_sessions() -> None:
    """Log sessions whose journal has no end record, i.e. the process died mid-run.

    Reported, never resumed: restarting a session is the user's decision (the
    Live tab lists these as "interrupted"). Runs off the startup path because
    reading every journal can take seconds.
    """
    try:
        from backend.live.manager import manager

        interrupted = [s for s in manager.history() if s["state"] == "interrupted"]
    except Exception:
        log.exception("Could not check for interrupted sessions")
        return

    for s in interrupted:
        log.warning(
            "Interrupted session %s (%s on %s): %d bar(s), %d trade(s), open legs: %s. "
            "Not resumed automatically; restart it from the Live tab if you want it back.",
            s["session_id"], s["strategy"], s["symbol"], s["bars_seen"], s["trades"],
            ", ".join(map(str, s["open_legs"])) or "none",
        )
    if not interrupted:
        log.info("No interrupted sessions found")


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    log.info(
        "Trade Terminal API %s starting (mode=%s, data dir %s)",
        __version__, settings.mode.value, settings.data_dir,
    )
    threading.Thread(
        target=_report_interrupted_sessions, name="interrupted-sessions", daemon=True
    ).start()
    job_runner.start()

    yield

    await run_in_threadpool(job_runner.shutdown)

    # Stop sessions before the process goes away, so each journal gets its end
    # record instead of looking like a crash on the next start.
    from backend.data.store import close_db
    from backend.live.manager import manager

    running = [s["session_id"] for s in manager.active() if s["running"]]
    if running:
        log.info("Shutdown: stopping %d running session(s): %s", len(running), ", ".join(running))
    await run_in_threadpool(manager.stop_all, "shutdown")
    close_db()
    log.info("Trade Terminal API stopped")


app = FastAPI(
    title="Trade Terminal",
    description="Visual strategy definition, backtesting, paper trading and live execution.",
    version=__version__,
    lifespan=lifespan,
)

# Middleware runs outermost-last-added. CORS is added last so it wraps the
# local-only check: a refused request still carries CORS headers, and the dev
# UI sees a readable 403 rather than an opaque network error.
app.add_middleware(
    LocalOnlyMiddleware,
    extra_hosts=parse_extra_hosts(settings.api_allowed_hosts),
)

# The Vite dev server runs on a different port; in production the frontend is
# served as static files from this same origin and CORS becomes irrelevant.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(DEV_ORIGINS),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _load_bars(
    symbol: str, timeframe: str, start: date | None, end: date | None
) -> cache.Bars:
    with _service_errors():
        return backtest_service.load_bars(symbol, timeframe, start, end)


def _load_candles(symbol: str, timeframe: str, start: date | None, end: date | None) -> pd.DataFrame:
    return _load_bars(symbol, timeframe, start, end).frame


@contextmanager
def _service_errors():
    """Map service-layer errors to HTTP statuses, in one place."""
    from pydantic import ValidationError

    from backend.appstate.jobs import JobNotFound, UnknownJobKind
    from backend.appstate.runs import RunNotFound
    from backend.appstate.strategies import StrategyNotFound

    try:
        yield
    except HTTPException:
        raise
    except (StrategyNotFound, RunNotFound, JobNotFound) as exc:
        raise HTTPException(404, f"Not found: {exc.args[0] if exc.args else exc}") from exc
    except UnknownJobKind as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(422, {"errors": format_validation_error(exc)}) from exc
    except backtest_service.InvalidStrategy as exc:
        raise HTTPException(422, {"errors": exc.errors}) from exc
    except backtest_service.InvalidSymbol as exc:
        raise HTTPException(400, str(exc)) from exc
    except backtest_service.DataUnavailable as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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
    bars = _load_bars(symbol, timeframe, start, end)
    df = bars.frame
    total = len(df)
    df = downsample(df, limit)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "total_bars": total,
        "returned": len(df),
        "data": bars.describe(),
        "candles": candles_payload(df),
    }


class FetchRequest(BaseModel):
    symbol: str
    timeframe: str = "5m"
    from_date: date | None = None
    days: int = Field(default=365, ge=1, le=3650)


@app.post("/api/data/fetch")
def fetch_data(req: FetchRequest) -> dict:
    """Fill the cache for a range. Intraday timeframes download 1-minute bars once
    and derive the rest, so a later request at another timeframe is instant."""
    from datetime import timedelta

    key = _resolve_or_400(req.symbol)
    if not cache.is_fetchable(key):
        raise HTTPException(400, f"{key} is not a broker instrument; nothing to download.")
    ready, reason = cache.broker_ready()
    if not ready:
        raise HTTPException(409, f"Upstox is not connected: {reason}")

    start = req.from_date or (date.today() - timedelta(days=req.days))
    try:
        bars = cache.get_bars(key, req.timeframe, start)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc

    if bars.empty:
        raise HTTPException(404, f"No data for {key} {req.timeframe}. {' '.join(bars.notes)}")

    expected = len(bars.frame["timestamp"].dt.date.unique()) + len(bars.missing_sessions)
    present = expected - len(bars.missing_sessions)
    return {
        "instrument_key": key,
        "fetched": bars.fetched_sessions,
        "total_bars": len(bars.frame),
        "coverage_pct": round(100.0 * present / expected, 2) if expected else 0.0,
        "missing_sessions": len(bars.missing_sessions),
        "clean": not bars.missing_sessions,
        "data": bars.describe(),
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
        return {"valid": False, "errors": format_validation_error(exc)}

    return {
        "valid": True,
        "id": strategy.id,
        "hash": strategy.content_hash(),
        "summary": strategy.summary(),
        "is_intraday": strategy.is_intraday,
        "has_options": strategy.has_options,
        "document": strategy.model_dump(mode="json"),
    }


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

class BacktestRequest(backtest_service.BacktestParams):
    strategy_id: str | None = None      # file the run under a saved strategy


@app.post("/api/backtest")
def backtest(req: BacktestRequest) -> dict:
    """Run a backtest in the request and record it. Long runs: POST /api/jobs/backtest."""
    params = backtest_service.BacktestParams.model_validate(
        req.model_dump(exclude={"strategy_id"})
    )
    with _service_errors():
        return job_services.backtest_and_record(params, strategy_id=req.strategy_id)


# ---------------------------------------------------------------------------
# Saved strategies, runs and jobs
# ---------------------------------------------------------------------------

class SaveStrategyRequest(BaseModel):
    document: dict[str, Any]
    origin: Literal["manual", "template", "agent", "import"] = "manual"
    note: str = Field(default="", max_length=500)


def _saved(version: saved_strategies.Version, created: bool) -> dict:
    return {**version.as_dict(), "new_version": created}


@app.get("/api/strategies")
def strategies_list(include_archived: bool = False) -> dict:
    return {"strategies": saved_strategies.list_all(include_archived)}


@app.post("/api/strategies", status_code=201)
def strategies_create(req: SaveStrategyRequest) -> dict:
    with _service_errors():
        version, created = saved_strategies.save(req.document, origin=req.origin, note=req.note)
    return _saved(version, created)


@app.get("/api/strategies/{strategy_id}")
def strategies_get(strategy_id: str, version: int | None = None) -> dict:
    with _service_errors():
        current = saved_strategies.get(strategy_id, version)
        versions = saved_strategies.history(strategy_id)
    return {
        **current.as_dict(),
        "versions": [v.as_dict(include_document=False) for v in versions],
    }


@app.put("/api/strategies/{strategy_id}")
def strategies_update(strategy_id: str, req: SaveStrategyRequest) -> dict:
    """Save a new version. Saving an unchanged document returns the current one."""
    with _service_errors():
        version, created = saved_strategies.save(
            req.document, strategy_id=strategy_id, origin=req.origin, note=req.note
        )
    return _saved(version, created)


@app.delete("/api/strategies/{strategy_id}")
def strategies_archive(strategy_id: str) -> dict:
    """Archive: hidden from the list, versions and runs kept. Saving again restores it."""
    with _service_errors():
        saved_strategies.archive(strategy_id)
    return {"id": strategy_id, "archived": True}


@app.get("/api/runs")
def runs_list(
    strategy_id: str | None = None, strategy_hash: str | None = None, limit: int = 50
) -> dict:
    return {"runs": run_history.list_runs(strategy_id, strategy_hash, limit)}


@app.get("/api/runs/{run_id}")
def runs_get(run_id: str, include_result: bool = True) -> dict:
    with _service_errors():
        return run_history.get(run_id, include_result)


@app.post("/api/jobs/backtest", status_code=202)
def jobs_backtest(req: job_services.BacktestJob) -> dict:
    """Queue a backtest. Poll GET /api/jobs/{id}; the result names the recorded run."""
    with _service_errors():
        document, _ = job_services.resolve_document(req)
        backtest_service.validate(document)          # reject a bad document now, not later
        return job_runner.submit("backtest", req.model_dump(mode="json"))


@app.post("/api/jobs/fetch", status_code=202)
def jobs_fetch(req: job_services.FetchJob) -> dict:
    """Queue a download into the cache, with progress."""
    with _service_errors():
        key = backtest_service.resolve(req.symbol)
        if not cache.is_fetchable(key):
            raise HTTPException(400, f"{key} is not a broker instrument; nothing to download.")
        return job_runner.submit("fetch", req.model_dump(mode="json"))


@app.get("/api/jobs")
def jobs_list(limit: int = 50, kind: str | None = None) -> dict:
    return {"jobs": job_runner.list(limit, kind)}


@app.get("/api/jobs/{job_id}")
def jobs_get(job_id: str) -> dict:
    with _service_errors():
        return job_runner.get(job_id)


@app.post("/api/jobs/{job_id}/cancel")
def jobs_cancel(job_id: str) -> dict:
    with _service_errors():
        return job_runner.cancel(job_id)


# ---------------------------------------------------------------------------
# Strategy builder conversations
# ---------------------------------------------------------------------------

class BuilderBacktestSettings(BaseModel):
    start: date | None = None
    end: date | None = None
    starting_capital: float | None = Field(default=None, gt=0)
    lot_size: int | None = Field(default=None, ge=1, le=10_000)
    symbol: str | None = None


class StartConversationRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    backtest: BuilderBacktestSettings = Field(default_factory=BuilderBacktestSettings)


@contextmanager
def _agent_errors():
    from backend.agent.service import ConversationNotFound, InvalidReply, TurnInProgress

    try:
        yield
    except ConversationNotFound as exc:
        raise HTTPException(404, f"No conversation {exc.args[0]}") from exc
    except TurnInProgress as exc:
        raise HTTPException(409, str(exc)) from exc
    except InvalidReply as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/agent/conversations")
def agent_list(limit: int = 50) -> dict:
    return {"conversations": agent.list_conversations(limit)}


@app.post("/api/agent/conversations", status_code=202)
def agent_start(req: StartConversationRequest) -> dict:
    """Describe a strategy. The turn runs in the background; poll the conversation."""
    with _agent_errors():
        return agent.start(req.text, req.backtest.model_dump(mode="json", exclude_none=True))


@app.get("/api/agent/conversations/{conversation_id}")
def agent_view(conversation_id: str) -> dict:
    with _agent_errors():
        return agent.view(conversation_id)


@app.post("/api/agent/conversations/{conversation_id}/reply", status_code=202)
def agent_reply(conversation_id: str, payload: dict[str, Any]) -> dict:
    """Answer the pending pause: questions, approval, or a follow-up change."""
    with _agent_errors():
        return agent.reply(conversation_id, payload)


@app.post("/api/agent/conversations/{conversation_id}/retry", status_code=202)
def agent_retry(conversation_id: str) -> dict:
    with _agent_errors():
        return agent.retry(conversation_id)


@app.post("/api/agent/conversations/{conversation_id}/cancel")
def agent_cancel(conversation_id: str) -> dict:
    with _agent_errors():
        current = agent.view(conversation_id)
        if current["job"]:
            job_runner.cancel(current["job"]["id"])
        return agent.view(conversation_id)


# ---------------------------------------------------------------------------
# LLM settings
# ---------------------------------------------------------------------------

class LLMKeyRequest(BaseModel):
    provider: Literal["anthropic"] = "anthropic"
    api_key: str = Field(min_length=20, max_length=500)


@app.get("/api/llm/status")
def llm_status() -> dict:
    from backend.llm import factory

    return factory.status()


@app.put("/api/llm/key")
def llm_set_key(req: LLMKeyRequest) -> dict:
    """Store the key in the OS keychain. It is never returned or logged."""
    from backend.llm import factory, keys

    try:
        keys.set_key(req.provider, req.api_key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return factory.status()


@app.delete("/api/llm/key")
def llm_delete_key(provider: Literal["anthropic"] = "anthropic") -> dict:
    from backend.llm import factory, keys

    keys.delete_key(provider)
    return factory.status()


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
        raise HTTPException(422, {"errors": format_validation_error(exc)}) from exc

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
        from backend.data.upstox_client import Timeframe

        warm_from = (
            cache.warmup_start(
                Timeframe.parse(strategy.timeframe), symbol, req.warmup_bars, req.replay_start
            )
            if req.replay_start is not None else None
        )
        history = _load_candles(symbol, strategy.timeframe, warm_from, req.replay_end)
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
        "state": "ended" if state.ended else "interrupted",
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
