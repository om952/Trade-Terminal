"""P1.2 gate: saved strategies, run history and background jobs."""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.appstate import db, runs
from backend.appstate import strategies as saved
from backend.appstate.jobs import JobContext, JobRunner
from backend.core import logsetup
from backend.core.config import settings
from backend.strategy import templates


@pytest.fixture
def appdb(tmp_path, monkeypatch):
    from backend.data import store

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    yield tmp_path / "app.sqlite"
    db.close()
    store.close_db()


def _doc(name="EMA test", fast=None, symbol="SIM|APPSTATE"):
    document = templates.load("ema_crossover", symbol=symbol).model_dump(mode="json")
    document["name"] = name
    document["timeframe"] = "15m"
    if fast is not None:
        document["indicators"][0]["params"]["period"] = fast
    return document


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def test_fresh_database_is_migrated_and_private(appdb):
    conn = db.connection()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert appdb.stat().st_mode & 0o777 == 0o600


def test_a_newer_database_is_refused(appdb):
    db.connection()
    db.close()
    db._migrated.discard(appdb)
    raw = sqlite3.connect(appdb)
    raw.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    raw.close()
    with pytest.raises(RuntimeError, match="newer than this build"):
        db.connection()
    db.close()


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def test_save_creates_version_one_and_writes_the_id_into_the_document(appdb):
    version, created = saved.save(_doc())
    assert created and version.version == 1
    assert version.strategy_id.startswith("stg_")
    assert version.document["id"] == version.strategy_id


def test_saving_an_identical_document_is_a_no_op(appdb):
    first, _ = saved.save(_doc())
    again, created = saved.save(first.document, strategy_id=first.strategy_id)
    assert not created and again.version == 1


def test_changes_append_versions_and_old_ones_stay_readable(appdb):
    v1, _ = saved.save(_doc(fast=9))
    v2, created = saved.save(_doc(fast=12), strategy_id=v1.strategy_id, note="faster")
    assert created and v2.version == 2 and v2.hash != v1.hash
    v3, _ = saved.save(_doc(name="Renamed", fast=12), strategy_id=v1.strategy_id)
    assert v3.version == 3 and v3.hash == v2.hash, "a rename keeps the content hash"

    history = saved.history(v1.strategy_id)
    assert [v.version for v in history] == [3, 2, 1]
    assert saved.get(v1.strategy_id, 1).document["indicators"][0]["params"]["period"] == 9
    assert saved.find_version_by_hash(v1.strategy_id, v1.hash).version == 1
    listed = saved.list_all()
    assert [(s["id"], s["name"], s["version"]) for s in listed] == [
        (v1.strategy_id, "Renamed", 3)
    ]


def test_archive_hides_and_saving_restores(appdb):
    v1, _ = saved.save(_doc())
    saved.archive(v1.strategy_id)
    assert saved.list_all() == []
    assert saved.list_all(include_archived=True)[0]["archived"]
    restored, created = saved.save(v1.document, strategy_id=v1.strategy_id)
    assert created and restored.version == 2
    assert saved.list_all()[0]["id"] == v1.strategy_id


def test_unknown_ids_and_bad_documents_are_refused(appdb):
    with pytest.raises(saved.StrategyNotFound):
        saved.save(_doc(), strategy_id="stg_nope")
    with pytest.raises(saved.StrategyNotFound):
        saved.get("stg_nope")
    bad = _doc()
    bad["entry"] = {"all": [{"left": "missing_indicator", "op": ">", "right": 1}]}
    with pytest.raises(ValidationError):
        saved.save(bad)


def test_concurrent_saves_lose_no_versions(appdb):
    v1, _ = saved.save(_doc(fast=5))
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def worker(fast):
        try:
            barrier.wait(timeout=10)
            saved.save(_doc(fast=fast), strategy_id=v1.strategy_id)
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(f,)) for f in range(6, 12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert [v.version for v in saved.history(v1.strategy_id)] == list(range(7, 0, -1))


# ---------------------------------------------------------------------------
# API: strategies, runs, jobs
# ---------------------------------------------------------------------------

@pytest.fixture
def api(appdb):
    """The app with its lifespan running (job runner started), on a seeded dataset."""
    from backend.api.main import app
    from backend.data.simulator import generate_ohlcv
    from backend.data.store import write_candles

    frame = generate_ohlcv("RELIANCE", date(2025, 1, 1), date(2025, 12, 31), "15m", seed=21)
    write_candles("SIM|APPSTATE", "15m", frame, source="simulated")

    root = logging.getLogger()
    level = root.level
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield client
    logsetup.reset_logging()
    root.setLevel(level)


def _backtest_body(document):
    return {"strategy": document, "include_chart": False}


def test_strategy_crud_over_http(api):
    created = api.post("/api/strategies", json={"document": _doc(), "origin": "template"})
    assert created.status_code == 201
    sid = created.json()["strategy_id"]

    updated = api.put(f"/api/strategies/{sid}", json={"document": _doc(fast=14), "note": "tweak"})
    assert updated.json()["version"] == 2 and updated.json()["new_version"]

    body = api.get(f"/api/strategies/{sid}").json()
    assert body["version"] == 2 and [v["version"] for v in body["versions"]] == [2, 1]
    assert "document" not in body["versions"][0]
    assert api.get(f"/api/strategies/{sid}?version=1").json()["document"]["indicators"][0][
        "params"]["period"] == 9

    assert api.delete(f"/api/strategies/{sid}").json()["archived"]
    assert api.get("/api/strategies").json()["strategies"] == []
    assert api.get("/api/strategies/stg_nope").status_code == 404
    bad = api.post("/api/strategies", json={"document": {"name": "x"}})
    assert bad.status_code == 422 and bad.json()["detail"]["errors"]


def test_every_backtest_is_recorded_and_reopens_identically(api):
    sid = api.post("/api/strategies", json={"document": _doc()}).json()["strategy_id"]
    doc = api.get(f"/api/strategies/{sid}").json()["document"]

    report = api.post("/api/backtest", json={**_backtest_body(doc), "strategy_id": sid}).json()
    assert report["run_id"].startswith("run_") and report["trials"] == 1

    stored = api.get(f"/api/runs/{report['run_id']}").json()
    assert stored["strategy_id"] == sid
    assert stored["data"]["fingerprint"] == report["data"]["fingerprint"]
    assert stored["result"]["metrics"] == report["metrics"]
    assert stored["result"]["trades"] == report["trades"]

    again = api.post("/api/backtest", json=_backtest_body(doc)).json()
    assert again["trials"] == 2, "same hash, second trial"
    assert [r["id"] for r in api.get(f"/api/runs?strategy_id={sid}").json()["runs"]] == [
        report["run_id"]
    ]
    assert len(api.get(f"/api/runs?strategy_hash={report['strategy_hash']}").json()["runs"]) == 2


def test_gate_job_path_matches_the_synchronous_path(api):
    """The job runner adds a queue, not a second way of computing results."""
    sid = api.post("/api/strategies", json={"document": _doc()}).json()["strategy_id"]
    doc = api.get(f"/api/strategies/{sid}").json()["document"]
    sync = api.post("/api/backtest", json=_backtest_body(doc)).json()

    submitted = api.post(
        "/api/jobs/backtest", json={"strategy_id": sid, "include_chart": False}
    )
    assert submitted.status_code == 202
    from backend.services.jobs import runner

    job = runner.wait(submitted.json()["id"], timeout=60)
    assert job["status"] == "succeeded", job
    assert job["progress"] == 1.0
    assert job["result"]["metrics"] == sync["metrics"]
    assert job["result"]["data"]["fingerprint"] == sync["data"]["fingerprint"]

    run = api.get(f"/api/runs/{job['result']['run_id']}").json()
    assert run["job_id"] == job["id"] and run["strategy_id"] == sid
    assert run["result"]["trades"] == sync["trades"]


def test_backtest_job_errors(api):
    bad = api.post("/api/jobs/backtest", json={"strategy": {"name": "x"}})
    assert bad.status_code == 422
    assert api.post("/api/jobs/backtest", json={}).status_code == 400
    assert api.post("/api/jobs/backtest", json={"strategy_id": "stg_nope"}).status_code == 404
    assert api.get("/api/jobs/job_nope").status_code == 404

    missing = _backtest_body(_doc(symbol="SIM|NOTHERE"))
    from backend.services.jobs import runner

    job = runner.wait(api.post("/api/jobs/backtest", json=missing).json()["id"])
    assert job["status"] == "failed" and "No candles for SIM|NOTHERE" in job["error"]


def _swept_doc():
    document = _doc()
    document.pop("schema_version")
    document.pop("id")
    document["sweep"] = [
        {"path": "/indicators/0/params/period", "values": [5, 9, 13]},
        {"path": "/legs/0/lots", "values": [1, 0]},          # lots 0 cannot run
    ]
    return document


def test_sweep_job_runs_and_records_every_variant(api):
    from backend.services.jobs import runner
    from backend.strategy.family import family_key

    document = _swept_doc()
    sid = api.post("/api/strategies", json={"document": document}).json()["strategy_id"]
    submitted = api.post("/api/jobs/sweep", json={"strategy_id": sid})
    assert submitted.status_code == 202
    job = runner.wait(submitted.json()["id"], timeout=120)
    assert job["status"] == "succeeded", job
    result = job["result"]

    assert len(result["variants"]) == 3
    assert [s["label"] for s in result["skipped"]] == [
        f"ema_fast.period={p}, leg1.lots=0" for p in (5, 9, 13)
    ]
    assert all("lots" in s["reason"] for s in result["skipped"])
    assert "best" not in result and "partly luck" in result["note"]

    # Every variant is a recorded run of the same family, linked to the strategy.
    family = family_key(document)
    assert result["family"] == family
    assert result["family_trials"] == {"runs": 3, "variants": 3}
    for row in result["variants"]:
        run = api.get(f"/api/runs/{row['run_id']}").json()
        assert run["strategy_id"] == sid and run["job_id"] == job["id"]
        assert run["result"]["metrics"]["total_trades"] == row["metrics"]["total_trades"]

    # A variant's run equals a plain backtest of the same concrete document.
    plain = _doc(fast=9)
    plain.pop("id")
    sync = api.post("/api/backtest", json=_backtest_body(plain)).json()
    nine = next(r for r in result["variants"] if r["assignment"]["/indicators/0/params/period"] == 9)
    assert sync["strategy_hash"] == nine["strategy_hash"]
    assert sync["metrics"]["net_pnl"] == nine["metrics"]["net_pnl"]


def test_sweep_job_errors(api):
    plain = api.post("/api/jobs/sweep", json={"strategy": _doc()})
    assert plain.status_code == 400 and "no sweep" in plain.json()["detail"]
    too_big = _swept_doc()
    too_big["sweep"] = [
        {"path": "/indicators/0/params/period", "values": list(range(2, 52))},
        {"path": "/indicators/1/params/period", "values": list(range(20, 25))},
    ]
    assert api.post("/api/jobs/sweep", json={"strategy": too_big}).status_code == 422


def test_sweep_job_can_be_cancelled(api, monkeypatch):
    from backend.services import jobs as job_module
    from backend.services.jobs import runner

    started = threading.Event()
    release = threading.Event()
    real = job_module.backtest_and_record

    def slow(*args, **kwargs):
        started.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(job_module, "backtest_and_record", slow)
    job_id = api.post("/api/jobs/sweep", json={"strategy": _swept_doc()}).json()["id"]
    assert started.wait(10)
    api.post(f"/api/jobs/{job_id}/cancel")
    release.set()
    job = runner.wait(job_id, timeout=60)
    assert job["status"] == "cancelled", job
    # Only the variant in flight when the cancel arrived was recorded.
    assert len(api.get("/api/runs?limit=50").json()["runs"]) == 1


def test_fetch_job_refuses_without_a_broker(api):
    assert api.post("/api/jobs/fetch", json={"symbol": "SIM|APPSTATE"}).status_code == 400
    from backend.services.jobs import runner

    job = runner.wait(api.post("/api/jobs/fetch", json={"symbol": "NSE_INDEX|Nifty 50"}).json()["id"])
    assert job["status"] == "failed" and "not connected" in job["error"]


# ---------------------------------------------------------------------------
# Runner mechanics
# ---------------------------------------------------------------------------

@pytest.fixture
def runner(appdb):
    r = JobRunner(workers=1)
    r.start()
    yield r
    r.shutdown(timeout=5)


def _spin(params, ctx: JobContext):
    """Reports progress until cancelled, or until `release` is set."""
    release: threading.Event = SPIN_EVENTS[params["name"]]
    started: threading.Event = SPIN_STARTED[params["name"]]
    started.set()
    i = 0
    while not release.wait(0.01):
        i += 1
        ctx.progress(min(0.9, i / 1000), f"step {i}")
    return {"steps": i}


SPIN_EVENTS: dict[str, threading.Event] = {}
SPIN_STARTED: dict[str, threading.Event] = {}


def _spin_job(r: JobRunner, name: str) -> str:
    SPIN_EVENTS[name], SPIN_STARTED[name] = threading.Event(), threading.Event()
    return r.submit("spin", {"name": name})["id"]


def test_running_job_reports_progress_and_can_be_cancelled(runner):
    runner.register("spin", _spin)
    job_id = _spin_job(runner, "a")
    assert SPIN_STARTED["a"].wait(5)
    progressed = runner.wait(job_id, timeout=0.8)
    assert progressed["status"] == "running" and progressed["progress"] > 0

    runner.cancel(job_id)
    done = runner.wait(job_id, timeout=5)
    assert done["status"] == "cancelled" and done["finished_at"]


def test_a_queued_job_cancelled_never_runs(runner):
    runner.register("spin", _spin)
    first = _spin_job(runner, "first")
    assert SPIN_STARTED["first"].wait(5)
    second = _spin_job(runner, "second")
    assert runner.get(second)["status"] == "queued"

    assert runner.cancel(second)["status"] == "cancelled"
    SPIN_EVENTS["first"].set()
    assert runner.wait(first)["status"] == "succeeded"
    assert not SPIN_STARTED["second"].wait(0.3), "a cancelled queued job started anyway"


def test_a_failing_job_records_its_error(runner):
    def boom(params, ctx):
        raise RuntimeError("upstream exploded")

    runner.register("boom", boom)
    job = runner.wait(runner.submit("boom", {})["id"])
    assert job["status"] == "failed" and job["error"] == "upstream exploded"


def test_unknown_kinds_are_refused(runner):
    from backend.appstate.jobs import UnknownJobKind

    with pytest.raises(UnknownJobKind):
        runner.submit("nope", {})


def test_jobs_left_by_a_dead_process_are_marked_interrupted(appdb):
    conn = db.connection()
    for status in ("queued", "running", "succeeded"):
        conn.execute(
            "INSERT INTO jobs (id, kind, status, params, created_at) VALUES (?, 'x', ?, '{}', '')",
            [f"job_{status}", status],
        )
    r = JobRunner(workers=1)
    assert r.start() == 2
    statuses = {j["id"]: j["status"] for j in r.list()}
    assert statuses == {
        "job_queued": "interrupted", "job_running": "interrupted", "job_succeeded": "succeeded",
    }
    r.shutdown(timeout=1)


def test_shutdown_cancels_running_jobs(appdb):
    r = JobRunner(workers=1)
    r.start()
    r.register("spin", _spin)
    job_id = _spin_job(r, "shutdown")
    assert SPIN_STARTED["shutdown"].wait(5)
    r.shutdown(timeout=5)
    assert r.get(job_id)["status"] == "cancelled"


def test_backtest_cancel_stops_inside_the_engine(appdb, monkeypatch):
    """A cancel reaches the bar loop, not just the gaps between phases."""
    from backend.data.simulator import generate_ohlcv
    from backend.data.store import write_candles
    from backend.services import jobs as job_services
    from backend.strategy import runner as engine_runner

    frame = generate_ohlcv("RELIANCE", date(2024, 1, 1), date(2025, 12, 31), "5m", seed=3)
    write_candles("SIM|APPSTATE", "5m", frame, source="simulated")
    monkeypatch.setattr(engine_runner, "PROGRESS_EVERY", 50)

    r = JobRunner(workers=1)
    r.start()
    r.register("backtest", job_services.run_backtest_job)
    reached = threading.Event()
    original = JobContext.progress

    def progress(self, fraction, message=""):
        if "Backtesting" in message:
            reached.set()
        return original(self, fraction, message)

    monkeypatch.setattr(JobContext, "progress", progress)
    doc = _doc()
    doc["timeframe"] = "5m"
    job_id = r.submit("backtest", {"strategy": doc, "include_chart": False})["id"]
    assert reached.wait(30), "the engine never reported progress"
    r.cancel(job_id)
    job = r.wait(job_id, timeout=30)
    r.shutdown(timeout=5)
    assert job["status"] == "cancelled"
    assert runs.list_runs() == [], "a cancelled backtest must not be recorded"
