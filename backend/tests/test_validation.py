"""P4.2: probabilistic and deflated Sharpe ratios, and the trial count behind them."""

from __future__ import annotations

import math
import random
from datetime import date

import numpy as np
import pytest

from backend.analytics import validation as v


def test_psr_matches_the_closed_form():
    # Normal returns (skew 0, kurtosis 3): PSR = Phi(SR * sqrt(T - 1) / sqrt(1 + SR^2 / 2)).
    sr = v.per_day(1.0)
    assert v.probabilistic_sharpe(sr, 0.0, 252, 0.0, 3.0) == pytest.approx(
        v._N.cdf(sr * math.sqrt(251) / math.sqrt(1 + sr * sr / 2)), rel=1e-12)
    assert v.probabilistic_sharpe(sr, sr, 252, 0.0, 3.0) == pytest.approx(0.5)
    # More days, more confidence; fat tails and negative skew, less.
    assert v.probabilistic_sharpe(sr, 0, 1000, 0, 3) > v.probabilistic_sharpe(sr, 0, 252, 0, 3)
    assert v.probabilistic_sharpe(sr, 0, 252, -1, 9) < v.probabilistic_sharpe(sr, 0, 252, 0, 3)
    assert v.probabilistic_sharpe(sr, 0, 2, 0, 3) is None


def test_expected_maximum_matches_simulation():
    """The best of N noise Sharpe ratios, by formula and by simulation."""
    rng = np.random.default_rng(3)
    days, trials = 252, 50
    best = [np.max(rng.normal(0, 1, (trials, days)).mean(axis=1) / 1.0) for _ in range(400)]
    simulated = float(np.mean(best))
    assert v.expected_max_sharpe(trials, 1 / days) == pytest.approx(simulated, rel=0.06)
    assert v.expected_max_sharpe(1, 1 / days) == 0.0


def test_the_best_of_many_noise_strategies_does_not_survive_deflation():
    """The multiple-testing concern, in miniature: 200 worthless strategies, keep the best."""
    rng = random.Random(11)
    days, sharpes, best = 500, [], None
    for _ in range(200):
        returns = [rng.gauss(0.0, 0.01) for _ in range(days)]
        mean = sum(returns) / days
        std = math.sqrt(sum((r - mean) ** 2 for r in returns) / (days - 1))
        annual = mean / std * math.sqrt(252)
        sharpes.append(annual)
        if best is None or annual > best["sharpe"]:
            best = {"sharpe": annual, "return_days": days, "return_skew": 0.0, "return_kurtosis": 3.0}
    assert best is not None
    alone = v.deflate(best, [best["sharpe"]], 1)
    searched = v.deflate(best, sharpes, len(sharpes))
    assert alone.psr is not None and alone.psr > 0.95, "on its own, the best noise strategy looks real"
    assert searched.dsr is not None and searched.dsr < v.DSR_THRESHOLD, "after the search, it does not"
    assert searched.verdict != "likely real after the search"
    assert searched.benchmark_sharpe > 1.5


def test_a_strong_edge_survives_a_small_search():
    rng = random.Random(5)
    returns = [rng.gauss(0.002, 0.01) for _ in range(750)]      # annual Sharpe about 3
    mean = sum(returns) / len(returns)
    std = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
    m = {"sharpe": mean / std * math.sqrt(252), "return_days": 750, "return_skew": 0.0, "return_kurtosis": 3.0}
    out = v.deflate(m, [m["sharpe"], 0.2, -0.3, 0.5], 4)
    assert out.dsr is not None and out.dsr > v.DSR_THRESHOLD and out.verdict == "likely real after the search"


def test_runs_recorded_before_daily_moments_are_not_judged():
    out = v.deflate({"sharpe": 2.0}, [2.0], 1).as_dict()
    assert out["psr"] is None and out["dsr"] is None and out["verdict"] == "too little data to judge"


# ---------------------------------------------------------------------------
# Trial counting through real backtests
# ---------------------------------------------------------------------------

@pytest.fixture
def recorded(tmp_path, monkeypatch):
    from backend.appstate import db
    from backend.core.config import settings
    from backend.data import store
    from backend.data.simulator import generate_ohlcv

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    store.write_candles("SIM|VAL", "1d", generate_ohlcv("NIFTY", date(2023, 1, 1), date(2025, 6, 30), "1d", seed=2),
                        source="simulated")
    yield
    db.close()
    store.close_db()


def _run(period=10, start=None):
    from backend.services import backtest, jobs
    from backend.strategy import templates

    doc = templates.load("ema_crossover", symbol="SIM|VAL").model_dump(mode="json")
    doc.update({"id": "", "timeframe": "1d", "session": {"start": "09:15", "end": "15:30", "square_off": None}})
    doc["indicators"][0]["params"]["period"] = period
    return jobs.backtest_and_record(backtest.BacktestParams(strategy=doc, include_chart=False, start=start))


def test_trials_count_distinct_experiments_not_reruns(recorded):
    first = _run()
    assert first["deflated"]["trials"] == 1 and first["deflated"]["days"] > 300
    assert _run()["deflated"]["trials"] == 1, "the identical test again is not a new try"
    assert _run(period=12)["deflated"]["trials"] == 2, "a retuned number is"
    assert _run(start=date(2024, 1, 1))["deflated"]["trials"] == 3, "so is another period"
    assert first["metrics"]["return_days"] == first["deflated"]["days"]
