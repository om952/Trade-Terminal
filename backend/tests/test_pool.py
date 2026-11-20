"""P4.4: backtests in worker processes give the same reports, recorded the same way."""

from __future__ import annotations

import random
from datetime import date

import pytest

from backend.core.config import settings
from backend.services import backtest, pool

SYMBOL, OTHER = "SIM|POOL", "SIM|POOL2"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from backend.appstate import db
    from backend.data import store
    from backend.data.simulator import generate_ohlcv

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    for key, seed in ((SYMBOL, 31), (OTHER, 32)):
        store.write_candles(key, "1d", generate_ohlcv("NIFTY", date(2022, 1, 3), date(2025, 6, 30), "1d", seed=seed),
                            source="simulated")
    yield
    db.close()
    store.close_db()


def _doc(fast: int = 9, slow: int = 21, symbol: str = SYMBOL) -> dict:
    from backend.strategy import templates

    doc = templates.load("ema_crossover", symbol=symbol).model_dump(mode="json")
    doc.update({"id": "", "timeframe": "1d", "session": {"start": "09:15", "end": "15:30", "square_off": None}})
    for ind in doc["indicators"]:
        if ind["id"] == "ema_fast":
            ind["params"]["period"] = fast
        if ind["id"] == "ema_slow":
            ind["params"]["period"] = slow
    return doc


def _requests() -> list[backtest.BacktestParams]:
    docs = [_doc(f, s) for f in (5, 9, 13) for s in (21, 34)] + [_doc(symbol=OTHER)]
    reqs = [backtest.BacktestParams(strategy=d, include_chart=False) for d in docs]
    reqs.append(backtest.BacktestParams(strategy=_doc(), slippage_bps=25, start=date(2023, 1, 1), include_chart=False))
    return reqs


def _strip(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k != "seed"}


def test_workers_give_the_same_reports_as_the_app_process(env):
    reqs = _requests()
    alone = [backtest.run(r) for r in reqs]
    inline = pool.backtest_many(reqs, size=1)
    spread = pool.backtest_many(reqs, size=3)
    assert all(isinstance(p, dict) for p in spread), spread
    assert len({p["strategy_hash"] for p in alone}) == len(reqs) - 1     # one document runs twice, differently
    for a, b, c in zip(alone, inline, spread, strict=True):
        assert _strip(b) == a
        assert c == b, "a worker process gives exactly the report the app's process gives"
    assert any(p["metrics"]["total_trades"] for p in spread)


def test_order_and_worker_count_do_not_change_a_report_or_its_seed(env):
    reqs = _requests()
    reference = pool.backtest_many(reqs, size=2)
    order = list(range(len(reqs)))
    random.Random(7).shuffle(order)
    shuffled = pool.backtest_many([reqs[i] for i in order], size=4)
    for position, i in enumerate(order):
        assert shuffled[position] == reference[i]
    seeds = [p["seed"] for p in reference]
    assert len(set(seeds)) == len(seeds), "each request has its own seed"
    assert seeds[0] == pool.seed_for(backtest.prepare(reqs[0]))


def test_one_failing_request_does_not_stop_the_batch(env):
    reqs = [backtest.BacktestParams(strategy=_doc(), include_chart=False),
            backtest.BacktestParams(strategy=_doc(symbol="SIM|NOTHERE"), include_chart=False),
            backtest.BacktestParams(strategy=_doc(5, 34), include_chart=False)]
    out = pool.backtest_many(reqs, size=2)
    assert isinstance(out[0], dict) and isinstance(out[2], dict)
    assert isinstance(out[1], backtest.DataUnavailable)


def test_a_worker_cannot_open_the_stores(env):
    from backend.data import store

    with pytest.raises(RuntimeError, match="must not open"):
        pool._executor(2).submit(store.get_db).result(timeout=60)


def test_batches_are_recorded_in_order_as_if_run_one_at_a_time(env, tmp_path, monkeypatch):
    """Trial counts and deflated Sharpe ratios match a one-at-a-time sweep exactly."""
    from backend.appstate import db
    from backend.services import jobs

    reqs = _requests()[:6]          # one family, six experiments
    batch = jobs.backtests_and_record(reqs)
    one_by_one_dir = tmp_path / "serial"
    one_by_one_dir.mkdir()
    db.close()
    monkeypatch.setattr(db, "db_path", lambda: one_by_one_dir / "app.sqlite")
    serial = [jobs.backtest_and_record(r) for r in reqs]
    for b, s in zip(batch, serial, strict=True):
        assert isinstance(b, dict)
        assert b["family_trials"] == s["family_trials"] and b["deflated"] == s["deflated"]
        assert b["metrics"] == s["metrics"]
    assert [b["deflated"]["trials"] for b in batch] == [1, 2, 3, 4, 5, 6]     # type: ignore[index]


@pytest.mark.parametrize("size", [1, 3])
def test_a_cancel_keeps_what_finished_and_records_it(env, monkeypatch, size):
    from backend.appstate import runs
    from backend.services import jobs

    monkeypatch.setattr(settings, "backtest_workers", size)

    class Stop(Exception):
        pass

    def progress(fraction: float, message: str) -> None:
        if message.startswith(("2 of", "Backtest 3")):          # after the second finishes
            raise Stop

    reqs = _requests()[:5]
    with pytest.raises(Stop) as caught:
        jobs.backtests_and_record(reqs, on_progress=progress)
    partial = caught.value.partial               # type: ignore[attr-defined]
    finished = [p for p in partial if p is not None]
    assert 1 <= len(finished) < len(reqs)
    assert len(runs.list_runs()) == len(finished), "the finished backtests were recorded"


def test_a_sweep_through_the_pool_matches_one_worker(env, monkeypatch):
    from backend.services import jobs

    document = _doc()
    document.pop("schema_version", None)
    document["sweep"] = [{"path": "/indicators/0/params/period", "values": [5, 9, 13]}]
    params = {"include_chart": False}
    spread = jobs.sweep_and_record(document, params)
    monkeypatch.setattr(settings, "backtest_workers", 1)
    inline = jobs.sweep_and_record(document, params)
    assert [r["metrics"] for r in spread["variants"]] == [r["metrics"] for r in inline["variants"]]
    assert [r["label"] for r in spread["variants"]] == [r["label"] for r in inline["variants"]]


def test_a_dead_worker_does_not_lose_the_batch_or_break_the_next(env):
    import os

    broken = pool._executor(2)
    from concurrent.futures.process import BrokenProcessPool

    with pytest.raises(BrokenProcessPool):
        broken.submit(os._exit, 1).result(timeout=60)        # a worker dies, as on a kill or out of memory
    reqs = _requests()[:3]
    out = pool.backtest_many(reqs, size=2)
    assert all(isinstance(p, dict) for p in out), out
    assert out == pool.backtest_many(reqs, size=1)
    assert pool._pool is not broken, "the next batch got a new pool"
    assert pool.backtest_many(reqs, size=2) == out
