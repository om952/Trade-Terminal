"""P4.2: the final holdout, which each strategy family may test on once."""

from __future__ import annotations

import threading
from datetime import date

import pandas as pd
import pytest

from backend.core.config import settings
from backend.services import holdout

REAL = "NSE_EQ|INE0HOLD01"
HELD = date(2026, 5, 1)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from backend.appstate import db
    from backend.appstate.db import transaction
    from backend.data import store
    from backend.data.simulator import generate_ohlcv

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "holdout_months", 6)
    store.close_db()
    frame = generate_ohlcv("RELIANCE", date(2024, 1, 1), date(2026, 11, 13), "1d", seed=4)
    store.write_candles(REAL, "1d", frame, source="upstox")
    store.write_candles("SIM|HOLD", "1d", frame, source="simulated")
    with transaction() as conn:
        conn.execute("INSERT INTO app_meta (key, value) VALUES ('holdout_start', ?)", [HELD.isoformat()])
    yield
    db.close()
    store.close_db()


def _doc(symbol=REAL, period=10) -> dict:
    from backend.strategy import templates

    doc = templates.load("ema_crossover", symbol=symbol).model_dump(mode="json")
    doc.update({"id": "", "timeframe": "1d", "session": {"start": "09:15", "end": "15:30", "square_off": None}})
    doc["indicators"][0]["params"]["period"] = period
    return doc


def _params(**kw):
    from backend.services import backtest

    return backtest.BacktestParams(strategy=kw.pop("strategy", _doc()), include_chart=False, **kw)


def _last_bar(payload) -> date:
    return pd.Timestamp(payload["data"]["last_bar"]).date()


def test_the_start_is_fixed_once_and_off_means_off(tmp_path, monkeypatch):
    from backend.appstate import db

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "holdout_months", 6)
    try:
        first = holdout.start(today=date(2026, 11, 20))
        assert first == date(2026, 5, 1)
        assert holdout.start(today=date(2027, 3, 1)) == first, "the holdout never moves"
        monkeypatch.setattr(settings, "holdout_months", 0)
        assert holdout.start() is None and holdout.status()["enabled"] is False
    finally:
        db.close()


def test_ordinary_backtests_of_real_data_stop_before_the_holdout(env):
    from backend.services.jobs import backtest_and_record

    out = backtest_and_record(_params())
    assert _last_bar(out) < HELD
    assert out["holdout"]["start"] == "2026-05-01" and not out["holdout"]["final"]
    assert "held out" in out["holdout"]["note"]

    early = backtest_and_record(_params(end=date(2025, 12, 31)))
    assert _last_bar(early) <= date(2025, 12, 31) and early["holdout"]["note"] is None

    with pytest.raises(ValueError, match="held out"):
        backtest_and_record(_params(start=date(2026, 6, 1)))


def test_synthetic_data_has_no_holdout(env):
    from backend.services.jobs import backtest_and_record

    out = backtest_and_record(_params(strategy=_doc("SIM|HOLD")))
    assert _last_bar(out) > HELD and out["holdout"] is None


def test_sweeps_and_the_chat_path_respect_the_holdout(env):
    from backend.services import jobs

    doc = {**_doc(), "sweep": [{"path": "/indicators/0/params/period", "values": [8, 10]}]}
    doc.pop("schema_version")
    result = jobs.sweep_and_record(doc, {"include_chart": False})
    assert pd.Timestamp(result["data"]["last_bar"]).date() < HELD


def test_one_final_test_per_family(env):
    first = holdout.final_test(_params())
    assert pd.Timestamp(first["data"]["first_bar"]).date() >= HELD, "only the held-out sessions"
    assert first["holdout"]["final"] is True
    assert holdout.used(first["family"])["run_id"] == first["run_id"]

    with pytest.raises(holdout.HoldoutUsed) as again:
        holdout.final_test(_params(strategy=_doc(period=12)))      # retuned: same family
    assert again.value.run_id == first["run_id"]

    other = _doc()                                                  # a different idea: an RSI filter
    other["indicators"].append({"id": "rsi", "type": "RSI", "params": {"period": 14}})
    other["entry"]["conditions"].append({"left": "rsi", "cmp": ">", "right": 50})
    assert holdout.final_test(_params(strategy=other))["holdout"]["final"]
    assert holdout.status()["families_tested"] == 2


def test_a_final_test_that_produces_nothing_releases_the_claim(env, monkeypatch):
    from backend.services import jobs

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    from backend.strategy.family import family_key

    real = jobs.backtest_and_record
    monkeypatch.setattr(jobs, "backtest_and_record", boom)
    with pytest.raises(RuntimeError):
        holdout.final_test(_params())
    assert holdout.used(family_key(_doc())) is None, "no result, so the look was not taken"
    # Restore only this function (monkeypatch.undo() would also restore the data
    # directory and point the rest of the test at the real app database).
    monkeypatch.setattr(jobs, "backtest_and_record", real)
    assert holdout.final_test(_params())["holdout"]["final"], "and it can be taken now"


def test_two_final_tests_at_once_get_one_look(env):
    from backend.appstate import db

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait()
        try:
            holdout.final_test(_params())
            outcomes.append("ran")
        except holdout.HoldoutUsed:
            outcomes.append("refused")
        finally:
            db.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(outcomes) == ["ran", "refused"]


def test_final_test_api(env):
    from fastapi.testclient import TestClient

    from backend.api.main import app
    from backend.services.jobs import runner

    runner.start()
    try:
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        assert client.get("/api/holdout").json() == {"enabled": True, "start": "2026-05-01", "months": 6,
                                                      "families_tested": 0}
        assert client.post("/api/jobs/final-test", json={"strategy": _doc("SIM|HOLD")}).status_code == 400
        job = client.post("/api/jobs/final-test", json={"strategy": _doc()})
        assert job.status_code == 202
        done = runner.wait(job.json()["id"], timeout=60)
        assert done["status"] == "succeeded" and done["result"]["holdout"]["final"], done
        again = client.post("/api/jobs/final-test", json={"strategy": _doc(period=14)})
        assert again.status_code == 409 and again.json()["detail"]["run_id"] == done["result"]["run_id"]
        plain = client.post("/api/backtest", json={"strategy": _doc(), "include_chart": False}).json()
        assert plain["holdout"]["note"] and pd.Timestamp(plain["data"]["last_bar"]).date() < HELD
    finally:
        runner.shutdown(timeout=10)
