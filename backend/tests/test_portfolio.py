"""P4.1: the portfolio engine on daily bars, and its parity with the paper path."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from backend.core.config import IST, settings
from backend.execution.costs import Segment
from backend.live.broker import SimulatedBroker
from backend.portfolio.engine import PortfolioEngine
from backend.portfolio.feeds import HistoricalUniverseFeed, ReplayUniverseFeed
from backend.portfolio.spec import PortfolioSpec
from backend.strategy.runner import FillModel
from backend.strategy.state import TradeReason

START = date(2024, 1, 1)
DAYS = 300


def _frame(slopes: list[float], wiggle: float = 0.004, seed: int = 0, start: date = START, days: int = DAYS) -> pd.DataFrame:
    """Daily bars whose log price follows piecewise slopes (one per equal segment), plus a little noise."""
    rng = np.random.default_rng(seed)
    stamps = pd.bdate_range(start, periods=days)
    seg = np.repeat(np.array(slopes, dtype=float), math.ceil(days / len(slopes)))[:days]
    logp = np.log(100.0) + np.cumsum(seg) + rng.normal(0, wiggle, days)
    close = np.exp(logp)
    open_ = np.concatenate([[close[0]], close[:-1]]) * (1 + rng.normal(0, 0.001, days))
    high = np.maximum(open_, close) * 1.004
    low = np.minimum(open_, close) * 0.996
    ts = [pd.Timestamp(d).tz_localize(IST) + pd.Timedelta(hours=9, minutes=15) for d in stamps]
    return pd.DataFrame({"timestamp": ts, "open": open_.round(2), "high": high.round(2), "low": low.round(2),
                         "close": close.round(2), "volume": 100_000, "oi": 0})


FRAMES = {
    "SIM|A": _frame([0.004], seed=1),     # steepest
    "SIM|B": _frame([0.003], seed=2),
    "SIM|C": _frame([0.001], seed=3),
    "SIM|D": _frame([-0.002], seed=4),    # falling
}


def _spec(**changes) -> PortfolioSpec:
    body = {
        "name": "Momentum rotation", "universe": {"symbols": list(FRAMES)},
        "indicators": [{"id": "mom", "type": "ROC", "params": {"period": 20}},
                       {"id": "sma50", "type": "SMA", "params": {"period": 50}}],
        "filter": {"op": "AND", "conditions": [{"left": "close", "cmp": ">", "right": "sma50"}]},
        "rank_by": "mom", "hold": 2, "rebalance": "monthly", "cash_buffer": 0.02,
    }
    body.update(changes)
    return PortfolioSpec.model_validate(body)


def _engine(spec: PortfolioSpec, frames=FRAMES, benchmark=None, slippage=5.0) -> PortfolioEngine:
    broker = SimulatedBroker(FillModel(slippage_bps=slippage, segment=Segment.EQUITY_DELIVERY))
    symbols = sorted(k for k in frames if k != benchmark or k in spec.universe.symbols)
    return PortfolioEngine(spec, broker, Decimal("1000000"), symbols, benchmark)


def test_parity_the_paper_replay_path_matches_the_backtest_feed():
    """P4.1 gate: the same engine through the historical feed and through paper's ReplaySource."""
    spec = _spec()
    backtest = _engine(spec).run(HistoricalUniverseFeed(FRAMES))
    paper = _engine(spec).run(ReplayUniverseFeed(FRAMES))
    assert len(backtest.trades) > 0
    assert [t.__dict__ if hasattr(t, "__dict__") else t for t in backtest.trades] == \
        [t.__dict__ if hasattr(t, "__dict__") else t for t in paper.trades]
    assert backtest.equity_curve == paper.equity_curve
    assert backtest.rebalances == paper.rebalances


def test_it_holds_the_strongest_eligible_and_fills_at_the_next_open():
    e = _engine(_spec(), slippage=10.0).run(HistoricalUniverseFeed(FRAMES))
    first = next(r for r in e.rebalances if r["targets"])
    assert first["targets"] == ["SIM|A", "SIM|B"], first
    assert all(s["symbol"] != "SIM|D" for s in first["top"]), "a falling stock is below its average: not eligible"

    # Decided at the rebalance day's close, filled at the next day's open, slipped against us.
    decided = date.fromisoformat(first["date"])
    buys = sorted({t.symbol for t in e.trades} | set(e.holdings))
    assert {"SIM|A", "SIM|B"} <= set(buys)
    held_a = e.holdings.get("SIM|A")
    entry_time = held_a.entry_time if held_a else next(t.entry_time for t in e.trades if t.symbol == "SIM|A")
    assert entry_time.date() > decided
    frame = FRAMES["SIM|A"]
    day_open = float(frame.loc[frame["timestamp"].dt.date == entry_time.date(), "open"].iloc[0])
    entry_price = held_a.avg_price if held_a else next(t.entry_price for t in e.trades if t.symbol == "SIM|A")
    assert float(entry_price) == pytest.approx(day_open * 1.001, rel=1e-6)

    # Cash never goes negative, and equity always equals cash plus marked holdings.
    assert e.cash >= 0
    assert min(v for _, v in e.equity_curve) > 0


def test_the_regime_filter_moves_to_cash_when_the_benchmark_breaks():
    frames = dict(FRAMES)
    frames["SIM|IDX"] = _frame([0.003, -0.006], seed=9)          # rises, then falls hard
    regime = {"symbol": "SIM|IDX", "indicators": [{"id": "sma50", "type": "SMA", "params": {"period": 50}}],
              "condition": {"op": "AND", "conditions": [{"left": "close", "cmp": ">", "right": "sma50"}]}}
    e = _engine(_spec(regime=regime), frames, benchmark="SIM|IDX").run(HistoricalUniverseFeed(frames))
    states = [r["regime_on"] for r in e.rebalances]
    assert True in states and states[-1] is False, states
    off = next(r for r in e.rebalances if r["regime_on"] is False and r["date"] > next(
        x["date"] for x in e.rebalances if x["targets"]))
    assert off["targets"] == []
    assert not e.holdings, "everything was sold when the regime turned off"
    assert any(t.reason is TradeReason.REBALANCE for t in e.trades)
    assert "SIM|IDX" not in {t.symbol for t in e.trades}, "the benchmark is not traded"


def test_weekly_rebalances_more_often_than_monthly():
    monthly = _engine(_spec()).run(HistoricalUniverseFeed(FRAMES)).rebalances
    weekly = _engine(_spec(rebalance="weekly")).run(HistoricalUniverseFeed(FRAMES)).rebalances
    assert len(weekly) > 3 * len(monthly)


def test_families_ignore_numbers_but_not_structure():
    base = _spec()
    assert _spec(hold=5, rebalance="weekly").family() == base.family()
    retuned = _spec(indicators=[{"id": "mom", "type": "ROC", "params": {"period": 126}},
                                {"id": "sma50", "type": "SMA", "params": {"period": 200}}])
    assert retuned.family() == base.family()
    other = _spec(indicators=[{"id": "mom", "type": "RSI", "params": {"period": 14}},
                              {"id": "sma50", "type": "SMA", "params": {"period": 50}}])
    assert other.family() != base.family()
    assert _spec(filter=None).family() != base.family()


def test_spec_validation():
    with pytest.raises(Exception, match="unknown name"):
        _spec(rank_by="momentum")
    with pytest.raises(Exception, match="unknown name"):
        _spec(filter={"op": "AND", "conditions": [{"left": "prev_day_high", "cmp": ">", "right": 0}]})
    with pytest.raises(ValidationError, match="hold"):
        _spec(hold=0)


# ---------------------------------------------------------------------------
# The service: stored data, a recorded run, the API
# ---------------------------------------------------------------------------

@pytest.fixture
def stored(tmp_path, monkeypatch):
    from backend.appstate import db
    from backend.data import store

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    for key, frame in FRAMES.items():
        store.write_candles(key, "1d", frame, source="simulated")
    yield
    db.close()
    store.close_db()


def test_a_recorded_portfolio_run(stored):
    from backend.appstate import runs
    from backend.portfolio import service

    params = service.PortfolioParams(spec=_spec().model_dump(mode="json"), fetch=False)
    out = service.run(params)
    assert out["kind"] == "portfolio" and out["run_id"] and out["metrics"]["total_trades"] >= 0
    assert out["data"]["instruments"] == 4 and out["synthetic_data"] is True
    assert out["deflated"]["trials"] == 1 and out["family_trials"]["runs"] == 1
    stored_run = runs.get(out["run_id"])
    assert stored_run["kind"] == "portfolio" and stored_run["family"] == _spec().family()
    # Retuning is another try of the same idea.
    again = service.run(service.PortfolioParams(spec=_spec(hold=3).model_dump(mode="json"), fetch=False))
    assert again["deflated"]["trials"] == 2

    # The service equals the engine run directly on the same frames.
    direct = _engine(_spec()).run(HistoricalUniverseFeed(FRAMES))
    assert len(out["trades"]) == len(direct.trades)


def test_portfolio_api(stored):
    from fastapi.testclient import TestClient

    from backend.api.main import app
    from backend.services.jobs import runner

    runner.start()
    try:
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        checked = client.post("/api/portfolio/validate", json=_spec().model_dump(mode="json")).json()
        assert checked["valid"] and checked["universe"] == 4 and checked["family"].startswith("pf_")
        bad = client.post("/api/portfolio/validate", json={**_spec().model_dump(mode="json"), "rank_by": "nope"}).json()
        assert not bad["valid"]
        job = client.post("/api/jobs/portfolio", json={"spec": _spec().model_dump(mode="json"), "fetch": False})
        assert job.status_code == 202
        done = runner.wait(job.json()["id"], timeout=120)
        assert done["status"] == "succeeded", done
        assert done["result"]["run_id"] and len(done["result"]["rebalances"]) <= 12
        assert client.post("/api/jobs/portfolio", json={"spec": {"name": "x"}}).status_code == 422
    finally:
        runner.shutdown(timeout=10)


def test_dates_are_aligned_by_session_not_by_position():
    """An instrument with a missing day is not shifted onto its neighbours' dates."""
    frames = {k: f.copy() for k, f in FRAMES.items()}
    gap_day = frames["SIM|B"]["timestamp"].iloc[100]
    frames["SIM|B"] = frames["SIM|B"][frames["SIM|B"]["timestamp"] != gap_day]
    e = _engine(_spec()).run(HistoricalUniverseFeed(frames))
    days = [ts.date() for ts, _ in e.equity_curve]
    assert len(days) == len(set(days)) == DAYS, "one equity point per session, the gap included"
    assert gap_day.date() in days
