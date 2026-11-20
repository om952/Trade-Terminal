"""Running a portfolio backtest: load the universe, run the engine, measure, record.

Every way a portfolio is evaluated goes through the engine in engine.py with
the simulated broker paper trading uses; only the feed differs. Results are
recorded as runs of kind "portfolio" with the portfolio's own family, so they
count as trials and carry a deflated Sharpe like any backtest. Real market data
stops before the final holdout, as every backtest does.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from backend.appstate.db import to_json
from backend.portfolio.spec import PortfolioSpec

Progress = Any


class PortfolioParams(BaseModel):
    spec: dict[str, Any]
    start: date | None = None
    end: date | None = None
    starting_capital: float = Field(default=1_000_000, gt=0)
    slippage_bps: float = Field(default=5.0, ge=0, le=500)
    fetch: bool = True


class InvalidPortfolio(ValueError):
    def __init__(self, errors: list[dict]) -> None:
        super().__init__("; ".join(f"{e['field']}: {e['message']}" for e in errors))
        self.errors = errors


def validate(document: dict[str, Any]) -> PortfolioSpec:
    from backend.services.backtest import format_validation_error

    try:
        return PortfolioSpec.model_validate(document)
    except Exception as exc:
        raise InvalidPortfolio(format_validation_error(exc)) from exc


def load(spec: PortfolioSpec, params: PortfolioParams, on_progress=None) -> tuple[dict[str, pd.DataFrame], list[dict], str | None]:
    """Daily bars per instrument (cut before the holdout), what could not be loaded, and the benchmark key."""
    from backend.data import cache
    from backend.scan.service import resolve_universe
    from backend.services import backtest, holdout

    universe = resolve_universe(spec.universe)
    benchmark = backtest.resolve(spec.regime.symbol) if spec.regime else None
    keys = [u["key"] for u in universe if u["key"]]
    if benchmark and benchmark not in keys:
        keys.append(benchmark)
    frames: dict[str, pd.DataFrame] = {}
    missing = [{"symbol": u["symbol"], "reason": "unknown instrument"} for u in universe if not u["key"]]
    for n, key in enumerate(keys):
        if on_progress is not None:
            on_progress(0.5 * n / max(1, len(keys)), f"Loading {n + 1} of {len(keys)}: {key}")
        first, last, _ = holdout.bounds(key, params.start, params.end)
        bars = cache.get_bars(key, "1d", first, last, fetch=params.fetch)
        if bars.empty:
            missing.append({"symbol": key, "reason": " ".join(bars.notes) or "no daily bars"})
        else:
            frames[key] = bars.frame
    return frames, missing, benchmark


def run(params: PortfolioParams, *, on_progress=None, job_id: str | None = None, feed: str = "historical",
        record: bool = True) -> dict[str, Any]:
    from backend.analytics.metrics import compute_metrics
    from backend.execution.costs import Segment
    from backend.live.broker import SimulatedBroker
    from backend.portfolio.engine import PortfolioEngine
    from backend.portfolio.feeds import HistoricalUniverseFeed, ReplayUniverseFeed
    from backend.services.backtest import equity_payload
    from backend.strategy.runner import FillModel

    spec = validate(params.spec)
    frames, missing, benchmark = load(spec, params, on_progress)
    members = _universe_keys(spec)
    tradable = [k for k in frames if k in members]     # the benchmark trades only if it is also a member
    if not tradable:
        raise ValueError("No instrument in the universe has daily bars to backtest.")
    if spec.regime and benchmark not in frames:
        raise ValueError(f"The regime benchmark {spec.regime.symbol} has no daily bars.")

    if on_progress is not None:
        on_progress(0.55, f"Running the portfolio over {len(tradable)} instruments")
    broker = SimulatedBroker(FillModel(slippage_bps=params.slippage_bps, segment=Segment.EQUITY_DELIVERY))
    engine = PortfolioEngine(spec, broker, Decimal(str(params.starting_capital)), sorted(tradable), benchmark)
    engine.run((ReplayUniverseFeed if feed == "replay" else HistoricalUniverseFeed)(frames))

    metrics = compute_metrics(engine.equity_curve, engine.trades, Decimal(str(params.starting_capital)), "1d")
    first = min(f["timestamp"].iloc[0] for f in frames.values())
    last = max(f["timestamp"].iloc[-1] for f in frames.values())
    synthetic = all(k.startswith("SIM|") for k in frames)
    notes = list(engine.notes)
    if spec.universe.preset == "fno_stocks":
        notes.append("The F&O list is today's: stocks that left it are missing from history, which flatters results.")
    if synthetic:
        notes.append("Synthetic data: this result says nothing about real markets.")
    payload: dict[str, Any] = {
        "kind": "portfolio", "name": spec.name, "strategy_hash": spec.content_hash(), "family": spec.family(),
        "synthetic_data": synthetic,
        "data": {"instruments": len(tradable), "benchmark": benchmark, "first_bar": pd.Timestamp(first).isoformat(),
                 "last_bar": pd.Timestamp(last).isoformat(), "missing": missing,
                 "fingerprint": _fingerprint(frames)},
        "metrics": metrics.as_dict(),
        "trades": [t.as_dict() for t in engine.trades],
        "equity_curve": equity_payload(engine.equity_curve),
        "rebalances": engine.rebalances,
        "holdings": engine.open_holdings(),
        "notes": notes,
    }
    if record:
        payload.update(_record(spec, params, payload, job_id))
    if on_progress is not None:
        on_progress(1.0, "Portfolio backtest complete")
    return json.loads(to_json(payload))


def _universe_keys(spec: PortfolioSpec) -> set[str]:
    from backend.scan.service import resolve_universe

    return {u["key"] for u in resolve_universe(spec.universe) if u["key"]}


def _fingerprint(frames: dict[str, pd.DataFrame]) -> str:
    import hashlib

    h = hashlib.sha256()
    for key in sorted(frames):
        f = frames[key]
        h.update(f"{key}:{len(f)}:{f['timestamp'].iloc[0]}:{f['timestamp'].iloc[-1]}:{float(f['close'].sum()):.4f}".encode())
    return h.hexdigest()[:16]


def _record(spec: PortfolioSpec, params: PortfolioParams, payload: dict[str, Any], job_id: str | None) -> dict[str, Any]:
    from backend.appstate import runs
    from backend.services.jobs import deflated_sharpe

    document = {**spec.model_dump(mode="json")}
    run_params = params.model_dump(mode="json", exclude={"spec"})
    run_id = runs.record(kind="portfolio", document=document, params=run_params, payload=payload,
                         job_id=job_id, family=spec.family())
    return {"run_id": run_id, "family_trials": runs.family_trials(spec.family()),
            "deflated": deflated_sharpe(payload["metrics"], spec.family())}
