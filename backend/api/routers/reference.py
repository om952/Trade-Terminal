"""Reference data: health, status, indicators, templates and instrument search."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Query

from backend.core.config import settings
from backend.data import instruments as inst
from backend.data.store import all_coverage
from backend.indicators import registry
from backend.strategy import templates

from fastapi import APIRouter

from backend import __version__

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/api/status")
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


@router.get("/api/indicators")
def list_indicators() -> dict:
    """The indicator palette. The UI builds its menu from this, so a newly
    registered indicator appears without a frontend change."""
    return {"indicators": registry.describe_all(), "categories": registry.by_category()}


@router.get("/api/templates")
def list_templates() -> dict:
    return {"templates": templates.list_templates()}


@router.get("/api/templates/{key}")
def get_template(key: str, symbol: str | None = None) -> dict:
    try:
        strategy = templates.load(key, **({"symbol": symbol} if symbol else {}))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return strategy.model_dump(mode="json")


@router.get("/api/instruments/search")
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


@router.get("/api/instruments/aliases")
def instrument_aliases() -> dict:
    """Friendly index names, for the picker's quick-select row."""
    return {"aliases": inst.INDICES}
