"""P4.3: the research graph, and the Phase 4 gate.

Gate: run the research graph on pure-noise simulated data. After deflation it
must report zero survivors. The control alongside it: on data with a strong,
persistent drift, a strategy that rides it must survive, so a judge that
rejects everything could not pass this file.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from backend.core.config import settings
from backend.data.simulator import InstrumentProfile
from backend.llm.fake import FakeLLM

NOISE = InstrumentProfile("NOISE", 1_000, 0.18, annual_drift=0.0, jump_intensity=0.0)
TREND = InstrumentProfile("TREND", 1_000, 0.08, annual_drift=0.45, jump_intensity=0.0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from backend.agent import service as agent
    from backend.appstate import db
    from backend.data import store
    from backend.research import service as research
    from backend.services.jobs import runner

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    fake = FakeLLM()
    agent.set_llm_factory(lambda: fake)
    runner.start()
    yield fake, research, runner
    runner.shutdown(timeout=30)
    agent.set_llm_factory(None)
    db.close()
    store.close_db()


def _data(key: str, profile: InstrumentProfile, seed: int) -> None:
    from backend.data import store
    from backend.data.simulator import generate_ohlcv

    store.write_candles(key, "1d", generate_ohlcv(profile, date(2022, 1, 3), date(2025, 6, 30), "1d", seed=seed),
                        source="simulated")


def _base(symbol: str) -> dict:
    from backend.strategy import templates

    doc = templates.load("ema_crossover", symbol=symbol).model_dump(mode="json")
    doc.update({"id": "", "name": "EMA cross", "timeframe": "1d",
                "session": {"start": "09:15", "end": "15:30", "square_off": None}})
    return doc


def _op(op: str, path: str, value=None) -> dict:
    return {"op": op, "path": path, "value_json": "" if op == "remove" else json.dumps(value)}


def _variant(hypothesis: str, *ops) -> dict:
    return {"hypothesis": hypothesis, "change_summary": hypothesis, "patch": list(ops)}


IND = "/indicators/-"
ENTRY = "/entry/conditions/-"

STRUCTURAL = [
    _variant("RSI filter", _op("add", IND, {"id": "rsi", "type": "RSI", "params": {"period": 14}}),
             _op("add", ENTRY, {"left": "rsi", "cmp": ">", "right": 50})),
    _variant("Trend filter", _op("add", IND, {"id": "sma200", "type": "SMA", "params": {"period": 200}}),
             _op("add", ENTRY, {"left": "close", "cmp": ">", "right": "sma200"})),
    _variant("Volume confirmation", _op("add", ENTRY, {"left": "volume", "cmp": ">",
                                                       "right": {"type": "SMA", "of": "volume", "period": 20}})),
    _variant("ADX trend strength", _op("add", IND, {"id": "adx", "type": "ADX", "params": {"period": 14}}),
             _op("add", ENTRY, {"left": "adx", "cmp": ">", "right": 20})),
    _variant("MACD confirmation", _op("add", IND, {"id": "macd", "type": "MACD", "params": {}}),
             _op("add", ENTRY, {"left": "macd.histogram", "cmp": ">", "right": 0})),
    _variant("Short instead", _op("replace", "/legs/0/action", "SELL")),
    _variant("Rising slow average", _op("add", ENTRY, {"left": "ema_slow", "cmp": "rising", "right": 0})),
    _variant("Breakout entry", _op("replace", "/entry/conditions/0",
                                   {"left": "close", "cmp": ">", "right": {"fn": "quantile", "of": "high",
                                                                          "window": 20, "q": 1}})),
]


def plan(*variants, notes="") -> dict:
    return {"variants": list(variants), "notes": notes}


def _settle(research, runner, session_id: str) -> dict:
    view = research.view(session_id)
    if view["job"]:
        runner.wait(view["job"]["id"], timeout=300)
    return research.view(session_id)


def _start(research, runner, symbol: str, goal="Find a more robust version") -> dict:
    view = research.start(research.StartRequest(strategy=_base(symbol), goal=goal))
    return _settle(research, runner, view["id"])


# ---------------------------------------------------------------------------
# The screen and the approval gate
# ---------------------------------------------------------------------------

def test_the_screen_rejects_non_structural_ideas_and_nothing_runs_before_approval(env):
    from backend.appstate import runs

    fake, research, runner = env
    _data("SIM|NOISE", NOISE, 1)
    fake.script += [plan(
        STRUCTURAL[0],
        _variant("Faster EMA", _op("replace", "/indicators/0/params/period", 5)),       # numbers only
        _variant("RSI filter again", *STRUCTURAL[0]["patch"]),                          # same idea twice
        _variant("Broken", _op("add", ENTRY, {"left": "nope", "cmp": ">", "right": 1})),  # invalid
        STRUCTURAL[1],
    )]
    view = _start(research, runner, "SIM|NOISE")
    assert view["phase"] == "awaiting_plan_approval", view
    status = {p["hypothesis"]: (p["status"], p.get("reason", "")) for p in view["proposals"]}
    assert status["RSI filter"][0] == "accepted" and status["Trend filter"][0] == "accepted"
    assert "only changes numbers" in status["Faster EMA"][1]
    assert "same idea as another variant" in status["RSI filter again"][1]
    assert "not a valid strategy" in status["Broken"][1]
    est = view["estimate"]
    assert est["candidates"] == 3 and est["backtests_min"] == 3 and est["backtests_max"] == 3 * 8
    assert est["model_calls_used"] == 1 and est["model_calls_to_come"] == 0
    assert runs.list_runs() == [], "nothing is backtested before approval"
    assert view["regime"].endswith("volatility")
    # The pasted goal stays out of the system prompt.
    assert "Find a more robust version" not in fake.calls[0]["system"]


def test_rejecting_the_plan_runs_nothing(env):
    from backend.appstate import runs

    fake, research, runner = env
    _data("SIM|NOISE", NOISE, 2)
    fake.script += [plan(STRUCTURAL[0])]
    view = _start(research, runner, "SIM|NOISE")
    research.reply(view["id"], {"action": "reject"})
    assert _settle(research, runner, view["id"])["phase"] == "rejected"
    assert runs.list_runs() == []


# ---------------------------------------------------------------------------
# The gate: pure noise gives zero survivors
# ---------------------------------------------------------------------------

# Costs are made negligible (500 units a lot, no slippage) so noise results fall on
# both sides of zero and the best of the search is positive by luck. These seeds
# are ones where that best candidate looks real on its own (PSR above 95%);
# measured over 20 seeds, 4 did, and none cleared the deflated Sharpe bar.
@pytest.mark.parametrize("seed", [112, 117, 118])
def test_gate_pure_noise_has_no_survivors(env, seed):
    from backend.analytics.validation import DSR_THRESHOLD
    from backend.appstate import runs

    fake, research, runner = env
    symbol = f"SIM|NOISE{seed}"
    _data(symbol, NOISE, seed)
    fake.script += [plan(*STRUCTURAL)]
    view = research.start(research.StartRequest(strategy=_base(symbol), goal="filters", lot_size=500, slippage_bps=0))
    view = _settle(research, runner, view["id"])
    assert sum(p["status"] == "accepted" for p in view["proposals"]) == len(STRUCTURAL)

    research.reply(view["id"], {"action": "approve"})
    view = _settle(research, runner, view["id"])
    assert view["phase"] == "awaiting_choice", view
    ranking = view["ranking"]
    assert len(ranking) == len(STRUCTURAL) + 1
    best = max(ranking, key=lambda r: r["metrics"]["sharpe"])
    assert best["metrics"]["sharpe"] > 0.7 and best["deflated"]["psr"] > 0.95, "on its own, the best looks real"
    assert all((r.get("dsr") or 0) < DSR_THRESHOLD for r in ranking), "after the search, nothing clears the bar"
    assert [r for r in ranking if r["outcome"] == "survived"] == []
    assert all(r["deflated"]["trials"] == len(ranking) for r in ranking)
    backtests = [r for r in runs.list_runs(limit=500) if r["kind"] == "backtest"]
    assert len(backtests) == len(ranking), "every candidate is a recorded trial"

    research.reply(view["id"], {"keep": []})
    assert _settle(research, runner, view["id"])["phase"] == "done"


# ---------------------------------------------------------------------------
# The control: a real drift is found
# ---------------------------------------------------------------------------

def test_control_a_real_drift_survives_and_can_be_kept(env):
    from backend.appstate import strategies

    fake, research, runner = env
    _data("SIM|TREND", TREND, 7)
    stay_long = _variant(
        "Stay long: the drift is the edge",
        _op("replace", "/entry", {"op": "AND", "conditions": [{"left": "close", "cmp": ">", "right": 0}]}),
        _op("replace", "/exit", None),
        _op("replace", "/legs/0/stop_loss", {"type": "percent", "value": 25}),
        _op("replace", "/legs/0/target", None),
        _op("replace", "/risk/max_trades_per_day", 1),
    )
    fake.script += [plan(stay_long, STRUCTURAL[0])]
    view = _start(research, runner, "SIM|TREND")
    accepted = [p for p in view["proposals"] if p["status"] == "accepted"]
    assert len(accepted) == 2, view["proposals"]
    research.reply(view["id"], {"action": "approve"})
    view = _settle(research, runner, view["id"])
    survivors = [r for r in view["ranking"] if r["outcome"] == "survived"]
    assert [r["hypothesis"] for r in survivors][:1] == ["Stay long: the drift is the edge"], view["ranking"]
    assert survivors[0]["checks"]["profitable_folds"] >= 3 and survivors[0]["checks"]["survives_slippage"]

    research.reply(view["id"], {"keep": [survivors[0]["id"]]})
    view = _settle(research, runner, view["id"])
    assert view["phase"] == "done" and len(view["kept"]) == 1
    saved = strategies.get(view["kept"][0]["strategy_id"])
    assert saved.origin == "research" and "drift" in saved.note


# ---------------------------------------------------------------------------
# Memory across sessions
# ---------------------------------------------------------------------------

def test_memory_drops_failed_ideas_and_counts_earlier_tries(env):
    from backend.research import memory

    fake, research, runner = env
    _data("SIM|NOISE", NOISE, 21)
    fake.script += [plan(*STRUCTURAL[:3])]
    first = _start(research, runner, "SIM|NOISE")
    research.reply(first["id"], {"action": "approve"})
    first = _settle(research, runner, first["id"])
    research.reply(first["id"], {"keep": []})
    _settle(research, runner, first["id"])
    assert len(memory.failures("SIM|NOISE")) == 4

    fake.script += [plan(STRUCTURAL[0], STRUCTURAL[3])]
    second = _start(research, runner, "SIM|NOISE")
    status = {p["hypothesis"]: p for p in second["proposals"]}
    assert status["RSI filter"]["status"] == "rejected" and "failed here before" in status["RSI filter"]["reason"]
    assert status["ADX trend strength"]["status"] == "accepted"
    sent = fake.calls[-1]["messages"][0]["content"]
    assert "Past failures on this instrument" in sent and "RSI filter" in sent

    research.reply(second["id"], {"action": "approve"})
    second = _settle(research, runner, second["id"])
    trials = {r["deflated"]["trials"] for r in second["ranking"] if r.get("deflated")}
    assert trials == {2 + 4}, "the earlier session's four tries count in this search"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_research_api(env):
    from fastapi.testclient import TestClient

    from backend.api.main import app

    fake, research, runner = env
    _data("SIM|NOISE", NOISE, 3)
    fake.script += [plan(STRUCTURAL[0])]
    client = TestClient(app, base_url="http://127.0.0.1:8000")
    started = client.post("/api/research", json={"strategy": _base("SIM|NOISE"), "goal": "filters"})
    assert started.status_code == 202
    sid = started.json()["id"]
    view = _settle(research, runner, sid)
    assert view["phase"] == "awaiting_plan_approval"
    assert client.post(f"/api/research/{sid}/reply", json={"action": "maybe"}).status_code == 400
    assert client.get("/api/research/res_missing").status_code == 404
    assert [s["id"] for s in client.get("/api/research").json()["sessions"]] == [sid]
    assert client.post("/api/research", json={"goal": "x"}).status_code == 400
