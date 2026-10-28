"""Paper (and later live) sessions: start, stop, inspect, resume."""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

import pandas as pd
from fastapi import HTTPException
from pydantic import BaseModel, Field

from backend.core.config import IST
from backend.strategy.schema import Strategy

from fastapi import APIRouter

from backend.api.common import (
    _format_validation_error,
    _load_candles,
    _resolve_or_400,
)

log = logging.getLogger(__name__)

router = APIRouter()


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


@router.post("/api/live/start")
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
    source: ReplaySource | RestPollingSource
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


@router.post("/api/live/{session_id}/stop")
def live_stop(session_id: str) -> dict:
    from backend.live.manager import manager

    if not manager.stop(session_id):
        raise HTTPException(404, f"No session {session_id} in this process")
    return {"session_id": session_id, "snapshot": manager.snapshot(session_id)}


@router.get("/api/live/sessions")
def live_sessions() -> dict:
    from backend.live.manager import manager

    return {"active": manager.active(), "history": manager.history()}


@router.get("/api/live/interrupted")
def live_interrupted() -> dict:
    """Sessions cut off by a crash or shutdown today, which the user may resume."""
    from backend.live.manager import manager

    return {"sessions": manager.interrupted()}


@router.get("/api/live/{session_id}")
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
