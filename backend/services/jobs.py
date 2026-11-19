"""The job kinds the app runs in the background, and the runner that runs them.

Each job function takes JSON-safe params and a JobContext, and returns a
JSON-safe result. They call the same services the synchronous endpoints call.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field

from backend.appstate import runs
from backend.appstate import strategies as saved
from backend.appstate.db import to_json
from backend.appstate.jobs import JobContext, JobRunner
from backend.data import cache
from backend.services import backtest as backtest_service


class BacktestJob(backtest_service.BacktestParams):
    """A backtest, given as a document or as a saved strategy (and version)."""

    strategy: dict[str, Any] | None = None   # type: ignore[assignment]
    strategy_id: str | None = None
    version: int | None = None


class FetchJob(BaseModel):
    symbol: str
    timeframe: str = "5m"
    from_date: date | None = None
    to_date: date | None = None
    days: int = Field(default=365, ge=1, le=3650)


def resolve_document(job: BacktestJob) -> tuple[dict[str, Any], str | None]:
    """The document to run and the saved strategy it belongs to, if any."""
    if job.strategy is not None:
        return job.strategy, job.strategy_id
    if job.strategy_id is None:
        raise ValueError("Give either a strategy document or a strategy_id")
    return saved.get(job.strategy_id, job.version).document, job.strategy_id


def backtest_and_record(
    params: backtest_service.BacktestParams,
    *,
    strategy_id: str | None = None,
    job_id: str | None = None,
    on_progress=None,
) -> dict[str, Any]:
    """Run a backtest and record it in the run history. Returns the report with run_id."""
    payload = backtest_service.run(params, on_progress=on_progress)
    document = backtest_service.validate(params.strategy).model_dump(mode="json")
    run_params = params.model_dump(mode="json", exclude={"strategy"})
    run_id = runs.record(
        kind="backtest", document=document, params=run_params, payload=payload,
        strategy_id=strategy_id, job_id=job_id,
    )
    from backend.strategy.family import family_key

    payload["run_id"] = run_id
    payload["trials"] = runs.trial_count(payload["strategy_hash"])
    payload["family"] = family_key(document)
    payload["family_trials"] = runs.family_trials(payload["family"])
    return json.loads(to_json(payload))


def run_backtest_job(params: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    job = BacktestJob.model_validate(params)
    document, strategy_id = resolve_document(job)
    run_params = backtest_service.BacktestParams.model_validate(
        {**job.model_dump(exclude={"strategy_id", "version"}), "strategy": document}
    )
    payload = backtest_and_record(
        run_params, strategy_id=strategy_id, job_id=ctx.job_id, on_progress=ctx.progress
    )
    # The full report lives with the run; the job result points at it.
    return {
        "run_id": payload["run_id"],
        "strategy_hash": payload["strategy_hash"],
        "metrics": payload["metrics"],
        "data": payload["data"],
        "warnings": payload["warnings"],
        "trials": payload["trials"],
    }


SWEEP_METRICS = (
    "total_trades", "net_pnl", "total_return_pct", "sharpe", "max_drawdown_pct",
    "win_rate_pct", "profit_factor",
)


SWEEP_NOTE = (
    "Every variant is recorded as a trial of this strategy's family. "
    "The best row of a grid is partly luck; the more rows, the more so."
)


def sweep_and_record(
    document: dict[str, Any],
    params: dict[str, Any],
    *,
    strategy_id: str | None = None,
    job_id: str | None = None,
    on_progress=None,
) -> dict[str, Any]:
    """Run every variant of a document's sweep, recording each as a run.

    `params` are backtest settings (symbol, capital, dates...) without the
    strategy. The job and the chat builder both call this, so a sweep from
    either one runs the same variants with the same settings.

    No "best" variant is named. Picking the top row of a grid is exactly the
    selection the family trial count exists to penalize, so the result reports
    every row in grid order and the count, and leaves the choice (and its
    caveat) visible.
    """
    from backend.strategy import sweep
    from backend.strategy.family import family_key

    strategy = backtest_service.validate(document)
    if not strategy.sweep:
        raise ValueError("This strategy has no sweep: add one to vary its parameters.")

    grid = sweep.variants(strategy)
    base = {k: v for k, v in params.items() if k not in ("strategy", "strategy_id", "version")}
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    first: dict[str, Any] | None = None
    for n, variant in enumerate(grid):
        if on_progress is not None:
            on_progress(n / len(grid), f"Variant {n + 1} of {len(grid)}: {variant.label}")
        if variant.strategy is None:
            skipped.append({"label": variant.label, "assignment": variant.assignment,
                            "reason": variant.error})
            continue
        run_params = backtest_service.BacktestParams.model_validate({
            **base, "include_chart": False,
            "strategy": variant.strategy.model_dump(mode="json"),
        })
        payload = backtest_and_record(run_params, strategy_id=strategy_id, job_id=job_id)
        first = first or payload
        rows.append({
            "label": variant.label,
            "assignment": variant.assignment,
            "run_id": payload["run_id"],
            "strategy_hash": payload["strategy_hash"],
            "metrics": {k: payload["metrics"].get(k) for k in SWEEP_METRICS},
        })

    family = family_key(strategy)
    if on_progress is not None:
        on_progress(1.0, f"Sweep complete: {len(rows)} variants run")
    return json.loads(to_json({
        "family": family,
        "family_trials": runs.family_trials(family),
        "axes": [
            {"path": axis.path, "label": sweep.path_label(axis.path, document),
             "values": axis.values}
            for axis in strategy.sweep
        ],
        "symbol": first["symbol"] if first else None,
        "timeframe": first["timeframe"] if first else strategy.timeframe,
        "synthetic_data": first["synthetic_data"] if first else None,
        "data": first["data"] if first else None,
        "variants": rows,
        "skipped": skipped,
        "note": SWEEP_NOTE,
    }))


def run_sweep_job(params: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    job = BacktestJob.model_validate(params)
    document, strategy_id = resolve_document(job)
    return sweep_and_record(
        document, job.model_dump(mode="json"), strategy_id=strategy_id,
        job_id=ctx.job_id, on_progress=ctx.progress,
    )


def run_fetch_job(params: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    job = FetchJob.model_validate(params)
    key = backtest_service.resolve(job.symbol)
    if not cache.is_fetchable(key):
        raise ValueError(f"{key} is not a broker instrument; nothing to download.")
    ready, reason = cache.broker_ready()
    if not ready:
        raise RuntimeError(f"Upstox is not connected: {reason}")
    start = job.from_date or (date.today() - timedelta(days=job.days))
    bars = cache.get_bars(key, job.timeframe, start, job.to_date, on_progress=ctx.progress)
    return bars.describe()


runner = JobRunner(workers=2)
runner.register("backtest", run_backtest_job)
runner.register("fetch", run_fetch_job)
runner.register("sweep", run_sweep_job)
