"""P5.2: the kill switch flattens and stops everything, and nothing new starts until it is released."""

from __future__ import annotations

import re
import threading
import time
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from backend.core.config import Mode, settings
from backend.data.simulator import generate_ohlcv
from backend.live import journal as J
from backend.live import kill_switch
from backend.live.session import SessionConfig
from backend.live.sources import ReplaySource
from backend.strategy import templates

SYMBOL = "SIM|KILL"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from backend.appstate import db
    from backend.data import store

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    yield tmp_path
    db.close()
    store.close_db()


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    return generate_ohlcv("RELIANCE", date(2026, 1, 1), date(2026, 3, 31), "5m", seed=909)


class PausedReplay(ReplaySource):
    """Replays bars up to `hold_after`, then waits there, as a live feed waits for its next bar."""

    def __init__(self, df: pd.DataFrame, symbol: str, hold_after: int) -> None:
        super().__init__(df, symbol)
        self.hold_after = hold_after
        self.reached = threading.Event()

    def bars(self):
        for n, bar in enumerate(super().bars(), start=1):
            yield bar
            if n == self.hold_after:          # the session has processed this bar
                self.reached.set()
                while not self._stopped:
                    time.sleep(0.01)
                return


def _config(strategy) -> SessionConfig:
    return SessionConfig(strategy=strategy, instrument_key=SYMBOL, starting_capital=Decimal("1000000"), lot_size=250,
                         slippage_bps=5.0, max_leverage=Decimal("5"), mode=Mode.PAPER, alerts_enabled=False)


def _holding_point(strategy, df: pd.DataFrame) -> int:
    """How many bars to replay so the session ends them holding an open position."""
    from backend.strategy.risk import RiskGuard
    from backend.strategy.runner import FillModel, run_backtest

    result = run_backtest(strategy, df, starting_capital=Decimal("1000000"), lot_size=250,
                          fill_model=FillModel(slippage_bps=5.0), risk=RiskGuard(rules=strategy.risk), collect_events=False)
    trade = next(t for t in result.trades if t.bars_held >= 3)
    stamps = pd.to_datetime(df["timestamp"])
    entry = int((stamps == pd.Timestamp(trade.entry_time)).to_numpy().nonzero()[0][0])
    return entry + 2                                   # the fill bar and one more, still open


def test_engage_flattens_a_running_paper_session_and_stops_it(env, frame):
    from backend.live.manager import manager

    strategy = templates.load("ema_crossover", symbol=SYMBOL, timeframe="5m")
    source = PausedReplay(frame, SYMBOL, _holding_point(strategy, frame))
    session, started = manager.start(_config(strategy), source=source, session_id="kill-1",
                                     history_frame=frame.iloc[:0])
    assert started and source.reached.wait(30), "the replay never reached the holding point"
    assert session.portfolio.has_open_positions, "the test needs an open position to flatten"

    report = kill_switch.engage("drill")
    assert report["engaged"] and report["reason"] == "drill" and report["complete"]
    [row] = [s for s in report["sessions"] if s["session_id"] == "kill-1"]
    assert row["positions_closed"] == 1 and row["stopped"] and row["still_open"] == []
    assert not manager.is_running("kill-1")
    assert not session.portfolio.has_open_positions

    # Flattened on the session's own thread, before its end record, and the end says why.
    records = list(J.read(settings.journal_dir / "kill-1.jsonl"))
    end = [r for r in records if r.type is J.Event.SESSION_END]
    assert end and end[-1].data["reason"] == "kill_switch"
    last_fill = max(r.seq for r in records if r.type is J.Event.FILL)
    assert last_fill < end[-1].seq
    assert session.portfolio.closed_trades[-1].reason.value == "session_end"

    # Engaging again is safe and keeps the first time.
    again = kill_switch.engage("second press")
    assert again["engaged_at"] == report["engaged_at"] and again["sessions"] == []


def test_nothing_starts_while_engaged_and_everything_can_after_release(env, frame):
    from fastapi.testclient import TestClient

    from backend.api.main import app
    from backend.data import store
    from backend.live.manager import manager

    store.write_candles(SYMBOL, "5m", frame.iloc[:300], source="simulated")
    strategy = templates.load("ema_crossover", symbol=SYMBOL, timeframe="5m")
    portfolio_spec = {"name": "Rotation", "universe": {"symbols": ["SIM|KA", "SIM|KB"]},
                      "indicators": [{"id": "mom", "type": "ROC", "params": {"period": 20}}],
                      "rank_by": "mom", "hold": 1}
    client = TestClient(app, base_url="http://127.0.0.1:8000")
    from backend.services.jobs import runner

    runner.start()
    try:
        _nothing_starts(client, manager, strategy, portfolio_spec, frame)
    finally:
        runner.shutdown(timeout=10)


def _nothing_starts(client, manager, strategy, portfolio_spec, frame) -> None:
    made = client.post("/api/portfolio/sessions", json={"spec": portfolio_spec})
    assert made.status_code == 201, made.text
    portfolio_id = made.json()["id"]

    engaged = client.post("/api/kill-switch/engage", json={"reason": "test"})
    assert engaged.status_code == 200
    assert [p["id"] for p in engaged.json()["portfolios"]] == [portfolio_id]
    assert client.get(f"/api/portfolio/sessions/{portfolio_id}").json()["status"] == "stopped"
    assert client.get("/api/kill-switch").json()["engaged"] is True

    refused = client.post("/api/live/start", json={"strategy": strategy.model_dump(mode="json"), "symbol": SYMBOL,
                                                   "source": "replay"})
    assert refused.status_code == 409 and "kill switch" in refused.json()["detail"]
    assert client.post("/api/portfolio/sessions", json={"spec": portfolio_spec}).status_code == 409
    with pytest.raises(kill_switch.KillSwitchEngaged):
        manager.start(_config(strategy), source=ReplaySource(frame.iloc[:10], SYMBOL), session_id="kill-2",
                      history_frame=frame.iloc[:0])

    released = client.post("/api/kill-switch/release")
    assert released.status_code == 200 and released.json()["engaged"] is False and released.json()["released_at"]
    started = client.post("/api/live/start", json={"strategy": strategy.model_dump(mode="json"), "symbol": SYMBOL,
                                                   "source": "replay"})
    assert started.status_code == 200, started.text
    manager.stop(started.json()["session_id"], "test over")
    assert client.post("/api/portfolio/sessions", json={"spec": portfolio_spec}).status_code == 201


def test_the_switch_survives_a_restart(env):
    from backend.appstate import db

    kill_switch.engage("restart drill")
    db.close()
    state = kill_switch.status()
    assert state["engaged"] and state["reason"] == "restart drill"
    with pytest.raises(kill_switch.KillSwitchEngaged, match="restart drill"):
        kill_switch.require_released()
    kill_switch.release()
    db.close()
    assert kill_switch.status()["engaged"] is False
    kill_switch.require_released()


def test_an_agent_cannot_reach_the_kill_switch():
    from backend.mcpserver.server import ALLOWED

    for method in ("GET", "POST"):
        for path in ("/api/kill-switch", "/api/kill-switch/engage", "/api/kill-switch/release"):
            assert not any(m == method and re.fullmatch(p, path) for m, p in ALLOWED), path


def test_a_session_stopped_by_a_tripped_pretrade_guard_flattens_first(env, frame):
    """P5.2: a runaway stop that trips while a position is open closes it before the session ends."""
    from backend.live.manager import manager

    strategy = templates.load("ema_crossover", symbol=SYMBOL, timeframe="5m")
    source = PausedReplay(frame, SYMBOL, _holding_point(strategy, frame))
    session, started = manager.start(_config(strategy), source=source, session_id="trip-1",
                                     history_frame=frame.iloc[:0])
    assert started and source.reached.wait(30)
    assert session.portfolio.has_open_positions
    manager.stop("trip-1", "pretrade_tripped")          # the reason the session uses when its guard trips
    assert not session.portfolio.has_open_positions, "the open position was flattened"
    records = list(J.read(settings.journal_dir / "trip-1.jsonl"))
    end = [r for r in records if r.type is J.Event.SESSION_END]
    assert end and end[-1].data["reason"] == "pretrade_tripped"
    assert max(r.seq for r in records if r.type is J.Event.FILL) < end[-1].seq
