"""Market data: stored datasets, candles, downloads and simulated data."""

from __future__ import annotations

import logging
from datetime import date

from fastapi import HTTPException
from pydantic import BaseModel, Field

from backend.data.calendar import find_gaps
from backend.data.store import all_coverage, read_candles, write_candles

from fastapi import APIRouter

from backend.api.common import (
    MAX_CHART_BARS,
    _candles_payload,
    _downsample,
    _load_candles,
    _resolve_or_400,
)

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/datasets")
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

@router.get("/api/candles")
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


@router.post("/api/data/fetch")
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


@router.post("/api/data/simulate")
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


@router.get("/api/simulator/profiles")
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
