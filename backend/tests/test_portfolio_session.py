"""P4.1: paper portfolio sessions, stepped once a day, with a paper record that is never rewritten."""

from __future__ import annotations

import math
from datetime import date, datetime, time
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from backend.core.config import IST, settings
from backend.execution.costs import Segment
from backend.live.broker import SimulatedBroker
from backend.portfolio.engine import PortfolioEngine
from backend.portfolio.feeds import HistoricalUniverseFeed, ReplayUniverseFeed
from backend.portfolio.spec import PortfolioSpec
from backend.strategy.runner import FillModel

DAYS = 260


def _frame(slopes: list[float], seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    stamps = pd.bdate_range(date(2025, 1, 1), periods=DAYS)
    seg = np.repeat(np.array(slopes, dtype=float), math.ceil(DAYS / len(slopes)))[:DAYS]
    close = np.exp(np.log(100.0) + np.cumsum(seg) + rng.normal(0, 0.006, DAYS))
    open_ = np.concatenate([[close[0]], close[:-1]]) * (1 + rng.normal(0, 0.002, DAYS))
    ts = [pd.Timestamp(d).tz_localize(IST) + pd.Timedelta(hours=9, minutes=15) for d in stamps]
    return pd.DataFrame({"timestamp": ts, "open": open_.round(2), "high": (np.maximum(open_, close) * 1.004).round(2),
                         "low": (np.minimum(open_, close) * 0.996).round(2), "close": close.round(2),
                         "volume": 100_000, "oi": 0})


FRAMES = {
    "SIM|PA": _frame([0.004, -0.002], 1),
    "SIM|PB": _frame([0.001, 0.003], 2),
    "SIM|PC": _frame([-0.001, 0.002], 3),
    "SIM|PD": _frame([0.002, -0.003], 4),
}
STAMPS = FRAMES["SIM|PA"]["timestamp"].dt.date.tolist()
START = STAMPS[120]                      # 120 sessions of warm-up before it


def _spec(**changes) -> dict:
    body = {
        "name": "Rotation", "universe": {"symbols": list(FRAMES)},
        "indicators": [{"id": "mom", "type": "ROC", "params": {"period": 20}},
                       {"id": "sma50", "type": "SMA", "params": {"period": 50}}],
        "filter": {"op": "AND", "conditions": [{"left": "close", "cmp": ">", "right": "sma50"}]},
        "rank_by": "mom", "hold": 2, "rebalance": "weekly", "cash_buffer": 0.02,
    }
    body.update(changes)
    return PortfolioSpec.model_validate(body).model_dump(mode="json")


def _after(day: date, hour: int = 9) -> datetime:
    """A step's clock: the morning after `day` (when its daily bar is served)."""
    return datetime.combine(day, time(hour, 0), tzinfo=IST) + pd.Timedelta(days=1)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from backend.appstate import db
    from backend.data import store

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    yield store
    db.close()
    store.close_db()


def _store(store, upto: date | None = None, frames=FRAMES) -> None:
    for key, frame in frames.items():
        part = frame if upto is None else frame[frame["timestamp"].dt.date <= upto]
        store.write_candles(key, "1d", part, source="simulated")


def _create(**kw):
    from backend.portfolio import session

    return session.create(_spec(**kw.pop("spec", {})), today=START, **kw)


def _direct(spec: dict, feed_type=HistoricalUniverseFeed) -> PortfolioEngine:
    broker = SimulatedBroker(FillModel(slippage_bps=5.0, segment=Segment.EQUITY_DELIVERY))
    engine = PortfolioEngine(PortfolioSpec.model_validate(spec), broker, Decimal("1000000"), sorted(FRAMES),
                             trade_from=START)
    return engine.run(feed_type(FRAMES))


def test_trade_from_warms_up_then_invests_at_the_first_close_on_both_feeds():
    e = _direct(_spec())
    assert e.equity_curve[0][0].date() == START, "no equity before the start"
    first = e.rebalances[0]
    assert first["date"] == START.isoformat() and first["targets"], "invested from the first close, warmed up"
    assert all(f["date"] > START.isoformat() for f in e.fills), "fills only at later opens"
    replayed = _direct(_spec(), ReplayUniverseFeed)
    assert replayed.fills == e.fills and replayed.equity_curve == e.equity_curve


def test_stepping_day_by_day_records_exactly_what_one_replay_gives(env):
    from backend.portfolio import session

    created = _create()
    assert created["status"] == "active" and created["members"] == 4
    sent: list[str] = []
    # Before the start's close has a bar: nothing to record yet.
    _store(env, upto=STAMPS[119])
    first = session.step(created["id"], fetch=False, now=_after(STAMPS[119]), send=sent.append)
    assert first["new_days"] == 0 and first["waiting"] and not sent

    for i in range(120, 160, 7):                      # a step every week or so, catching up each time
        _store(env, upto=STAMPS[i])
        session.step(created["id"], fetch=False, now=_after(STAMPS[i]), send=sent.append)
    _store(env)
    last = session.step(created["id"], fetch=False, now=_after(STAMPS[-1]), send=sent.append)
    again = session.step(created["id"], fetch=False, now=_after(STAMPS[-1]), send=sent.append)
    assert again["new_days"] == 0 and again["message"] is None, "a repeated step records and sends nothing"

    days = session.get(created["id"])["days"]
    direct = _direct(_spec())
    assert [d["day"] for d in days] == [t.date().isoformat() for t, _ in direct.equity_curve]
    assert [d["equity"] for d in days] == [round(float(v), 2) for _, v in direct.equity_curve]
    recorded_fills = [{"date": d["day"], **f} for d in days for f in d["fills"]]
    assert recorded_fills == direct.fills
    state = session.get(created["id"])["state"]
    assert state["as_of"] == STAMPS[-1].isoformat() and last["equity"] == state["equity"]
    assert state["net_pnl"] == pytest.approx(state["realized_pnl"] + state["unrealized_pnl"], abs=0.02)
    assert any("Orders for the next open" in m for m in sent) and any("Filled at the open" in m for m in sent)


def test_a_bar_dated_today_waits_for_the_close(env):
    from backend.portfolio import session

    created = _create()
    _store(env, upto=STAMPS[125])
    morning = datetime.combine(STAMPS[125], time(11, 0), tzinfo=IST)
    out = session.step(created["id"], fetch=False, now=morning, send=lambda _: True)
    assert out["as_of"] == STAMPS[124].isoformat()
    evening = datetime.combine(STAMPS[125], time(16, 30), tzinfo=IST)
    assert session.step(created["id"], fetch=False, now=evening, send=lambda _: True)["as_of"] == STAMPS[125].isoformat()


def test_a_changed_price_history_halts_instead_of_rewriting_the_record(env):
    from backend.portfolio import session

    created = _create()
    _store(env, upto=STAMPS[150])
    session.step(created["id"], fetch=False, now=_after(STAMPS[150]), send=lambda _: True)
    before = session.get(created["id"])["days"]

    revised = {k: f.copy() for k, f in FRAMES.items()}
    row = revised["SIM|PA"].index[revised["SIM|PA"]["timestamp"].dt.date == STAMPS[140]][0]
    for col in ("open", "high", "low", "close"):
        revised["SIM|PA"].loc[row, col] = round(revised["SIM|PA"].loc[row, col] * 1.08, 2)
    _store(env, upto=STAMPS[155], frames=revised)
    sent: list[str] = []
    out = session.step(created["id"], fetch=False, now=_after(STAMPS[155]), send=sent.append)
    assert out["status"] == "halted" and STAMPS[140].isoformat() in out["reason"]
    after = session.get(created["id"])
    assert after["status"] == "halted" and after["days"] == before, "the paper record is untouched"
    assert sent and "Halted" in sent[0]
    assert session.step(created["id"], fetch=False, now=_after(STAMPS[156]))["skipped"]


def test_live_is_refused_and_a_stopped_session_does_not_step(env):
    from backend.portfolio import session

    with pytest.raises(ValueError, match="Phase 5"):
        _create(mode="live")
    created = _create()
    _store(env)
    stopped = session.stop(created["id"])
    assert stopped["status"] == "stopped" and stopped["stopped_at"]
    assert session.step(created["id"], fetch=False, now=_after(STAMPS[-1]))["skipped"]
    with pytest.raises(session.NotFound):
        session.get("pfs_nope")


def test_the_morning_scheduler_steps_active_sessions_once_per_slot(env, monkeypatch):
    from backend.portfolio import session
    from backend.scan.alerts import Scheduler

    monkeypatch.setattr(settings, "portfolio_step_times", "08:50")
    active, stopped = _create(), _create()
    session.stop(stopped["id"])
    submitted: list[tuple[str, dict]] = []
    clock = [datetime(2026, 11, 23, 8, 40, tzinfo=IST)]
    scheduler = Scheduler(lambda kind, params: submitted.append((kind, params)), clock=lambda: clock[0])
    scheduler.tick()
    assert ("portfolio_step", {"session_id": active["id"]}) in submitted, "the slot before (yesterday's) was owed"
    submitted.clear()
    scheduler.tick()
    assert not submitted
    clock[0] = datetime(2026, 11, 23, 8, 55, tzinfo=IST)
    scheduler.tick()
    assert submitted == [("portfolio_step", {"session_id": active["id"]})]


def test_the_frozen_members_and_the_api(env):
    from fastapi.testclient import TestClient

    from backend.api.main import app
    from backend.services.jobs import runner

    yesterday = datetime.now(IST).date() - pd.Timedelta(days=1)
    recent = pd.bdate_range(end=yesterday, periods=DAYS)
    for key, frame in FRAMES.items():
        moved = frame.copy()
        moved["timestamp"] = [pd.Timestamp(d).tz_localize(IST) + pd.Timedelta(hours=9, minutes=15) for d in recent]
        env.write_candles(key, "1d", moved, source="simulated")
    runner.start()
    try:
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        assert client.post("/api/portfolio/sessions", json={"spec": _spec(), "mode": "live"}).status_code == 400
        assert client.post("/api/portfolio/sessions", json={"spec": {**_spec(), "rank_by": "nope"}}).status_code == 422
        made = client.post("/api/portfolio/sessions", json={"spec": _spec(), "starting_capital": 500000})
        assert made.status_code == 201, made.text
        body = made.json()
        done = runner.wait(body["job"]["id"], timeout=120)
        assert done["status"] == "succeeded", done
        # Created today: the stored bars all end before it, so the session waits for its first close.
        assert done["result"]["waiting"] is True
        listed = client.get("/api/portfolio/sessions").json()
        assert listed["sessions"][0]["id"] == body["id"] and listed["step_times"] == settings.portfolio_step_times
        assert client.get(f"/api/portfolio/sessions/{body['id']}").json()["starting_capital"] == 500000
        stepped = client.post(f"/api/portfolio/sessions/{body['id']}/step")
        assert stepped.status_code == 202 and runner.wait(stepped.json()["id"], timeout=120)["status"] == "succeeded"
        assert client.post(f"/api/portfolio/sessions/{body['id']}/stop").json()["status"] == "stopped"
        assert client.get("/api/portfolio/sessions/pfs_nope").status_code == 404
    finally:
        runner.shutdown(timeout=10)
