"""P4.2: walk-forward, neighbourhood robustness, slippage stress and a second instrument."""

from __future__ import annotations

from datetime import date

import pytest

from backend.core.config import settings
from backend.services import validate

SYMBOL, OTHER = "SIM|VS", "SIM|VS2"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from backend.appstate import db
    from backend.data import store
    from backend.data.simulator import generate_ohlcv

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    for key, seed in ((SYMBOL, 21), (OTHER, 22)):
        store.write_candles(key, "1d", generate_ohlcv("NIFTY", date(2023, 1, 2), date(2025, 6, 30), "1d", seed=seed),
                            source="simulated")
    yield
    db.close()
    store.close_db()


def _doc(sweep=None) -> dict:
    from backend.strategy import templates

    doc = templates.load("ema_crossover", symbol=SYMBOL).model_dump(mode="json")
    doc.update({"id": "", "timeframe": "1d", "session": {"start": "09:15", "end": "15:30", "square_off": None}})
    if sweep:
        doc.pop("schema_version")
        doc["sweep"] = sweep
    return doc


def _params(doc, **kw) -> validate.ValidateParams:
    return validate.ValidateParams(strategy=doc, **kw)


def test_a_plain_strategy_gets_folds_slippage_and_a_second_instrument(env):
    from backend.appstate import runs
    from backend.services import backtest, jobs
    from backend.strategy.family import family_key

    messages: list[str] = []
    out = validate.run(_params(_doc(), folds=4, second_symbol=OTHER),
                       on_progress=lambda f, m: messages.append(m))
    wf = out["walk_forward"]
    assert wf["mode"] == "folds" and len(wf["folds"]) == 4
    # The folds tile the base period without gaps or overlaps.
    first, last = date.fromisoformat(out["base"]["first_bar"]), date.fromisoformat(out["base"]["last_bar"])
    assert date.fromisoformat(wf["folds"][0]["start"]) == first and date.fromisoformat(wf["folds"][-1]["end"]) == last
    for a, b in zip(wf["folds"], wf["folds"][1:]):
        assert (date.fromisoformat(b["start"]) - date.fromisoformat(a["end"])).days == 1

    # A fold equals an ordinary backtest of the same dates.
    fold = wf["folds"][1]
    plain = backtest.run(backtest.BacktestParams(strategy=_doc(), include_chart=False,
                                                 start=date.fromisoformat(fold["start"]),
                                                 end=date.fromisoformat(fold["end"])))
    assert plain["metrics"]["net_pnl"] == pytest.approx(fold["metrics"]["net_pnl"])

    levels = out["slippage"]["levels"]
    assert [lv["slippage_bps"] for lv in levels] == [5.0, 10.0, 15.0, 25.0]
    pnl = [lv["metrics"]["net_pnl"] for lv in levels]
    assert pnl == sorted(pnl, reverse=True), "more slippage, less profit"
    assert out["second_instrument"]["symbol"] == OTHER
    assert out["neighbourhood"] is None
    assert out["runs"] == 1 + 4 + 4 + 1 and any("of about 10" in m for m in messages)
    assert any("walk-forward folds" in v for v in out["verdicts"])

    # Checks of a chosen strategy are recorded, but are not trials of the idea.
    family = family_key(_doc())
    assert runs.family_trials(family) == {"runs": 0, "variants": 0}
    assert len(runs.list_runs(limit=100)) == 10
    first_real = jobs.backtest_and_record(backtest.BacktestParams(strategy=_doc(), include_chart=False))
    assert first_real["trials"] == 1 and first_real["deflated"]["trials"] == 1


def test_a_swept_strategy_is_chosen_in_sample_and_judged_out_of_sample(env):
    sweep = [{"path": "/indicators/0/params/period", "values": [5, 9, 13]}]
    out = validate.run(_params(_doc(sweep), folds=3, slippage_multipliers=[1, 3]))
    wf = out["walk_forward"]
    assert wf["mode"] == "optimised" and [r["fold"] for r in wf["folds"]] == [2, 3]
    labels = {f"ema_fast.period={p}" for p in (5, 9, 13)}
    assert all(r["chosen"] in labels for r in wf["folds"])
    # 1 base + 3 variants x 2 in-sample windows + 2 out-of-sample + 3 grid + 2 slippage
    assert out["runs"] == 1 + 6 + 2 + 3 + 2
    nb = out["neighbourhood"]
    assert nb["best"] in labels and nb["neighbours"] in (1, 2)


def test_neighbourhood_tells_a_plateau_from_a_spike():
    axes = [{"path": "/p", "values": [1, 2, 3, 4, 5]}]

    def grid(sharpes):
        return [{"label": f"p={i + 1}", "assignment": {"/p": i + 1}, "metrics": {"sharpe": s}}
                for i, s in enumerate(sharpes)]

    plateau = validate.neighbourhood(grid([1.0, 1.1, 1.2, 1.15, 1.05]), axes)
    spike = validate.neighbourhood(grid([-0.2, 0.1, 2.0, -0.1, 0.0]), axes)
    assert plateau["best"] == "p=3" and plateau["neighbours"] == 2 and plateau["ratio"] > 0.9
    assert plateau["plateau_share"] == 1.0
    assert spike["ratio"] < 0.1 and spike["plateau_share"] == 0.2


def test_breakeven_slippage_is_interpolated():
    class Fake:
        def run_many(self, items):
            return [{"run_id": label, "metrics": {"net_pnl": 1000 - 100 * o["slippage_bps"]}} for _, label, o in items]

    out = validate.slippage_stress(Fake(), {}, 5.0, [1, 2, 3])        # 500, 0, -500
    assert out["breakeven_bps"] == pytest.approx(10.0) and not out["survives_all"]
    lose = validate.slippage_stress(Fake(), {}, 20.0, [1, 2])
    assert lose["loses_at_base"] and lose["breakeven_bps"] is None


def test_validate_api(env):
    from fastapi.testclient import TestClient

    from backend.api.main import app
    from backend.services.jobs import runner

    runner.start()
    try:
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        job = client.post("/api/jobs/validate", json={"strategy": _doc(), "folds": 2, "slippage_multipliers": [1, 2]})
        assert job.status_code == 202
        done = runner.wait(job.json()["id"], timeout=120)
        assert done["status"] == "succeeded", done
        assert set(done["result"]) >= {"base", "walk_forward", "slippage", "verdicts", "notes"}
        bad = _doc()
        bad["entry"]["conditions"][0]["left"] = "nope"
        assert client.post("/api/jobs/validate", json={"strategy": bad}).status_code == 422
        assert client.post("/api/jobs/validate", json={"strategy": _doc(), "folds": 1}).status_code == 422
    finally:
        runner.shutdown(timeout=10)
