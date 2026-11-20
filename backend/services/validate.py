"""Checks of a fixed strategy: walk-forward, neighbourhood, slippage stress, a second instrument (P4.2).

The deflated Sharpe ratio asks whether a result survives the search that found
it; these ask whether it survives being looked at differently:

* walk-forward: does it work in each part of history, not just on average?
  For a strategy with a sweep, the best variant is chosen on the folds before
  each test fold and judged on that fold only (anchored walk-forward), so the
  out-of-sample figures carry no hindsight.
* neighbourhood (swept strategies): do the values next to the best one also
  work, or is the best a lone spike in a grid of noise?
* slippage stress: how much worse can fills get before the edge is gone?
* a second instrument: does the idea hold on something it was not tuned on?

Every run here is recorded with kind "validation". They are checks of a
strategy already chosen, not candidates, so they do not count as trials of
the family (backend/appstate/runs.py counts kind "backtest" only): checking an
idea must not make it look worse. Like every backtest, they stop before the
final holdout.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from backend.services import backtest as backtest_service

KIND = "validation"
MAX_WFO_VARIANTS = 50          # variants x folds runs; beyond this, walk-forward runs the base only
SUMMARY = ("net_pnl", "total_return_pct", "sharpe", "max_drawdown_pct", "win_rate_pct", "total_trades")


class ValidateParams(BaseModel):
    strategy: dict[str, Any]
    symbol: str | None = None
    start: date | None = None
    end: date | None = None
    starting_capital: float = Field(default=1_000_000, gt=0)
    lot_size: int = Field(default=1, ge=1, le=10_000)
    slippage_bps: float | None = None
    max_leverage: float = Field(default=5.0, gt=0, le=50)
    folds: int = Field(default=4, ge=2, le=12)
    slippage_multipliers: list[float] = Field(default_factory=lambda: [1.0, 2.0, 3.0, 5.0], min_length=2, max_length=8)
    second_symbol: str | None = None


@dataclass(frozen=True)
class Candidate:
    """One concrete variant of a swept strategy."""

    label: str
    assignment: dict[str, Any]
    document: dict[str, Any]


def _metrics(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: (payload.get("metrics") or {}).get(k) for k in SUMMARY}


class _Runner:
    """Runs and records validation backtests with shared settings, reporting progress."""

    def __init__(self, params: ValidateParams, strategy_id: str | None, job_id: str | None,
                 total: int, on_progress) -> None:
        self.params, self.strategy_id, self.job_id = params, strategy_id, job_id
        self.total, self.done, self.on_progress = max(1, total), 0, on_progress

    def run(self, document: dict[str, Any], label: str, **overrides: Any) -> dict[str, Any]:
        from backend.services.jobs import backtest_and_record

        if self.on_progress is not None:
            self.on_progress(min(0.99, self.done / self.total), f"{self.done + 1} of about {self.total}: {label}")
        base = self.params.model_dump(exclude={"strategy", "folds", "slippage_multipliers", "second_symbol"})
        run_params = backtest_service.BacktestParams.model_validate(
            {**base, **overrides, "strategy": document, "include_chart": False})
        payload = backtest_and_record(run_params, strategy_id=self.strategy_id, job_id=self.job_id, kind=KIND)
        self.done += 1
        return payload


def _folds(first: date, last: date, n: int) -> list[tuple[date, date]]:
    """n consecutive date ranges covering [first, last]."""
    span = (last - first).days + 1
    edges = [first + timedelta(days=round(span * i / n)) for i in range(n + 1)]
    return [(edges[i], edges[i + 1] - timedelta(days=1)) for i in range(n)]


def _sharpe(payload: dict[str, Any]) -> float:
    s = (payload.get("metrics") or {}).get("sharpe")
    return float(s) if s is not None and math.isfinite(s) else float("-inf")


def walk_forward(runner: _Runner, document: dict[str, Any], variants: list[Candidate],
                 folds: list[tuple[date, date]]) -> dict[str, Any]:
    if not variants:
        rows = []
        for i, (a, b) in enumerate(folds):
            p = runner.run(document, f"walk-forward fold {i + 1}", start=a, end=b)
            rows.append({"fold": i + 1, "start": a.isoformat(), "end": b.isoformat(), "run_id": p["run_id"],
                         "metrics": _metrics(p)})
        pnl = [r["metrics"]["net_pnl"] or 0 for r in rows]
        return {"mode": "folds", "folds": rows, "profitable_folds": sum(1 for x in pnl if x > 0),
                "worst_fold_pnl": min(pnl), "best_fold_pnl": max(pnl)}

    # Anchored walk-forward optimisation: choose on folds[0..i-1], test on fold i.
    rows = []
    for i in range(1, len(folds)):
        is_start, is_end = folds[0][0], folds[i - 1][1]
        scored = []
        for v in variants:
            p = runner.run(v.document, f"fold {i + 1} in-sample: {v.label}",
                           start=is_start, end=is_end)
            scored.append((_sharpe(p), v, p))
        best_sharpe, best, _ = max(scored, key=lambda t: t[0])
        a, b = folds[i]
        oos = runner.run(best.document, f"fold {i + 1} out-of-sample: {best.label}",
                         start=a, end=b)
        rows.append({"fold": i + 1, "start": a.isoformat(), "end": b.isoformat(), "chosen": best.label,
                     "in_sample_sharpe": None if math.isinf(best_sharpe) else best_sharpe,
                     "run_id": oos["run_id"], "metrics": _metrics(oos)})
    is_s = [r["in_sample_sharpe"] for r in rows if r["in_sample_sharpe"] is not None]
    oos_s = [r["metrics"]["sharpe"] for r in rows if r["metrics"]["sharpe"] is not None]
    efficiency = (statistics.mean(oos_s) / statistics.mean(is_s)
                  if is_s and oos_s and statistics.mean(is_s) > 0 else None)
    pnl = [r["metrics"]["net_pnl"] or 0 for r in rows]
    return {"mode": "optimised", "folds": rows, "profitable_folds": sum(1 for x in pnl if x > 0),
            "chosen_changes": len({r["chosen"] for r in rows}), "efficiency": efficiency,
            "worst_fold_pnl": min(pnl) if pnl else None, "best_fold_pnl": max(pnl) if pnl else None}


def neighbourhood(grid: list[dict[str, Any]], axes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """How the best variant's neighbours (one step along one axis) fared."""
    scored = [(r, r["metrics"].get("sharpe")) for r in grid]
    scored = [(r, s) for r, s in scored if s is not None and math.isfinite(s)]
    if len(scored) < 2:
        return None
    best, best_s = max(scored, key=lambda t: t[1])
    index = {a["path"]: {v: i for i, v in enumerate(a["values"])} for a in axes}

    def steps(r):
        return tuple(index[p].get(r["assignment"][p]) for p in index)

    me = steps(best)
    near = [s for r, s in scored if r is not best and sum(abs(a - b) for a, b in zip(steps(r), me)) == 1]
    plateau = sum(1 for _, s in scored if best_s > 0 and s >= 0.5 * best_s) / len(scored)
    median_near = statistics.median(near) if near else None
    return {
        "best": best["label"], "best_sharpe": best_s, "neighbours": len(near),
        "neighbour_median_sharpe": median_near,
        "neighbour_min_sharpe": min(near) if near else None,
        "ratio": (median_near / best_s) if near and best_s > 0 else None,
        "plateau_share": plateau,
    }


def slippage_stress(runner: _Runner, document: dict[str, Any], base_bps: float, multipliers: list[float]) -> dict[str, Any]:
    rows = []
    for m in sorted(set(multipliers)):
        bps = round(base_bps * m, 4)
        p = runner.run(document, f"slippage {bps:g} bps", slippage_bps=bps)
        rows.append({"multiplier": m, "slippage_bps": bps, "run_id": p["run_id"], "metrics": _metrics(p)})
    # Where net P&L crosses zero, by linear interpolation between levels.
    breakeven = None
    for lo, hi in zip(rows, rows[1:]):
        a, b = lo["metrics"]["net_pnl"] or 0, hi["metrics"]["net_pnl"] or 0
        if a > 0 >= b:
            breakeven = lo["slippage_bps"] + (hi["slippage_bps"] - lo["slippage_bps"]) * a / (a - b)
            break
    first_pnl = rows[0]["metrics"]["net_pnl"] or 0
    return {"base_bps": base_bps, "levels": rows,
            "breakeven_bps": round(breakeven, 2) if breakeven is not None else None,
            "survives_all": all((r["metrics"]["net_pnl"] or 0) > 0 for r in rows),
            "loses_at_base": first_pnl <= 0}


def _verdicts(out: dict[str, Any]) -> list[str]:
    lines = []
    wf = out["walk_forward"]
    n = len(wf["folds"])
    lines.append(f"Profitable in {wf['profitable_folds']} of {n} walk-forward folds.")
    if wf.get("efficiency") is not None:
        lines.append(f"Out-of-sample Sharpe was {wf['efficiency'] * 100:.0f}% of in-sample "
                     "(walk-forward efficiency; under 50% suggests the choice is fitting noise).")
    nb = out.get("neighbourhood")
    if nb and nb.get("ratio") is not None:
        lines.append(f"Neighbours of the best variant reach {nb['ratio'] * 100:.0f}% of its Sharpe; "
                     f"{nb['plateau_share'] * 100:.0f}% of the grid is within half of it.")
    sl = out["slippage"]
    if sl["loses_at_base"]:
        lines.append("Loses money at the base slippage already.")
    elif sl["survives_all"]:
        lines.append(f"Still profitable at {sl['levels'][-1]['slippage_bps']:g} bps of slippage.")
    elif sl["breakeven_bps"] is not None:
        lines.append(f"Profit is gone at about {sl['breakeven_bps']:g} bps of slippage (base {sl['base_bps']:g}).")
    second = out.get("second_instrument")
    if second:
        a, b = out["base"]["metrics"].get("sharpe"), second["metrics"].get("sharpe")
        held = a is not None and b is not None and b > 0
        lines.append(f"On {second['symbol']}: Sharpe {b if b is not None else '–'} "
                     f"({'holds' if held else 'does not hold'}).")
    return lines


def run(params: ValidateParams, *, strategy_id: str | None = None, job_id: str | None = None,
        on_progress=None) -> dict[str, Any]:
    from backend.strategy import sweep as sweep_module

    strategy = backtest_service.validate(params.strategy)
    variants = [
        Candidate(v.label, v.assignment, v.strategy.model_dump(mode="json"))
        for v in (sweep_module.variants(strategy) if strategy.sweep else [])
        if v.strategy is not None
    ]
    base_doc = variants[0].document if variants else strategy.model_dump(mode="json")
    optimise = 0 < len(variants) <= MAX_WFO_VARIANTS
    total = (1 + params.folds * (len(variants) if optimise else 1) + len(params.slippage_multipliers)
             + len(variants) + (1 if params.second_symbol else 0))
    runner = _Runner(params, strategy_id, job_id, total, on_progress)

    base = runner.run(base_doc, "the full period")
    first = pd.Timestamp(base["data"]["first_bar"]).date()
    last = pd.Timestamp(base["data"]["last_bar"]).date()
    out: dict[str, Any] = {"base": {"run_id": base["run_id"], "metrics": _metrics(base),
                                    "first_bar": first.isoformat(), "last_bar": last.isoformat(),
                                    "holdout": base.get("holdout")}}
    notes = []
    folds = _folds(first, last, params.folds)
    out["walk_forward"] = walk_forward(runner, base_doc, variants if optimise else [], folds)
    if variants and not optimise:
        notes.append(f"The sweep has {len(variants)} variants; walk-forward optimisation runs at most "
                     f"{MAX_WFO_VARIANTS}, so each fold ran the first variant instead.")
    notes.append("Each fold warms its indicators up inside the fold, so its first bars take no trades.")

    out["neighbourhood"] = None
    if variants:
        grid = []
        for v in variants:
            p = runner.run(v.document, f"grid: {v.label}")
            grid.append({"label": v.label, "assignment": v.assignment, "run_id": p["run_id"], "metrics": _metrics(p)})
        axes = [{"path": a.path, "values": a.values} for a in strategy.sweep]
        out["neighbourhood"] = neighbourhood(grid, axes)

    base_bps = params.slippage_bps if params.slippage_bps is not None else strategy.costs.slippage_bps
    out["slippage"] = slippage_stress(runner, base_doc, float(base_bps), params.slippage_multipliers)

    out["second_instrument"] = None
    if params.second_symbol:
        p = runner.run(base_doc, f"on {params.second_symbol}", symbol=params.second_symbol)
        out["second_instrument"] = {"symbol": params.second_symbol, "run_id": p["run_id"], "metrics": _metrics(p),
                                    "synthetic_data": p.get("synthetic_data")}

    out["verdicts"] = _verdicts(out)
    out["notes"] = notes + [
        "These runs check a strategy already chosen, so they do not count as trials of the idea.",
    ]
    if on_progress is not None:
        on_progress(1.0, f"Validation complete: {runner.done} runs")
    out["runs"] = runner.done
    return out
