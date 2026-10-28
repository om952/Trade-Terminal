"""Background jobs: progress, cancel, failure, restart recovery, and the API."""

from __future__ import annotations

import json
import threading
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backend.app.context import AppContext
from backend.app.jobs import JobCancelled, JobRunner

SYMBOL = "SIM|JOBTEST"


@pytest.fixture
def ctx(tmp_path) -> AppContext:
    return AppContext(user_id="local", data_dir=tmp_path)


@pytest.fixture
def runner():
    r = JobRunner(max_workers=2)
    yield r
    r.shutdown()


def test_job_runs_reports_progress_and_returns_result(runner, ctx):
    def work(job):
        for i in range(5):
            job.progress(i / 5, f"step {i}", force=True)
        return {"answer": 42}

    job_id = runner.submit("test", work, params={"x": 1}, ctx=ctx)
    record = runner.wait(job_id, ctx)
    assert record.status == "done"
    assert record.result == {"answer": 42}
    assert record.progress == 1 and record.params == {"x": 1}
    assert record.started_at and record.finished_at


def test_running_job_can_be_cancelled(runner, ctx):
    started = threading.Event()

    def work(job):
        started.set()
        while True:
            job.progress(0.5, "spinning")

    job_id = runner.submit("test", work, ctx=ctx)
    assert started.wait(5)
    assert runner.cancel(job_id, ctx).cancel_requested
    assert runner.wait(job_id, ctx).status == "cancelled"


def test_queued_job_cancelled_before_it_starts_never_runs(ctx):
    runner = JobRunner(max_workers=1)
    release = threading.Event()
    ran = threading.Event()
    try:
        blocker = runner.submit("test", lambda job: release.wait(5), ctx=ctx)
        queued = runner.submit("test", lambda job: ran.set(), ctx=ctx)
        runner.cancel(queued, ctx)
        release.set()
        assert runner.wait(blocker, ctx).status == "done"
        assert runner.wait(queued, ctx).status == "cancelled"
        assert not ran.is_set()
    finally:
        runner.shutdown()


def test_failure_is_recorded(runner, ctx):
    def work(job):
        raise ValueError("bad input")

    record = runner.wait(runner.submit("test", work, ctx=ctx), ctx)
    assert record.status == "failed"
    assert record.error == "ValueError: bad input"


def test_cancelling_a_finished_job_changes_nothing(runner, ctx):
    job_id = runner.submit("test", lambda job: "ok", ctx=ctx)
    runner.wait(job_id, ctx)
    assert runner.cancel(job_id, ctx).status == "done"


def test_jobs_left_running_by_a_dead_process_are_marked_failed(ctx):
    from backend.app.db import get_app_db, now_iso

    with get_app_db(ctx).connect() as conn:
        conn.execute("INSERT INTO jobs (id, kind, status, created_at) VALUES ('job_old', 'x', 'running', ?)",
                     (now_iso(),))
    runner = JobRunner()
    try:
        assert runner.recover_interrupted(ctx) == 1
        record = runner.get("job_old", ctx)
        assert record.status == "failed" and "interrupted" in record.error
    finally:
        runner.shutdown()


def test_job_handle_check_raises_when_cancelled(runner, ctx):
    seen = {}
    gate = threading.Event()

    def work(job):
        gate.wait(5)
        try:
            job.check()
        except JobCancelled:
            seen["cancelled"] = True
            raise

    job_id = runner.submit("test", work, ctx=ctx)
    runner.cancel(job_id, ctx)
    gate.set()
    assert runner.wait(job_id, ctx).status == "cancelled"
    assert seen == {"cancelled": True}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client() -> TestClient:
    from backend.api.main import app

    return TestClient(app, base_url="http://localhost")


@pytest.fixture(scope="module")
def dataset() -> str:
    from backend.data.simulator import generate_ohlcv
    from backend.data.store import write_candles

    df = generate_ohlcv("RELIANCE", date(2025, 1, 1), date(2025, 12, 31), "5m", seed=5)
    write_candles(SYMBOL, "5m", df, source="simulated")
    return SYMBOL


def _payload(client) -> dict:
    document = client.get("/api/templates/ema_crossover").json()
    document["timeframe"] = "5m"
    document["instrument"]["symbol"] = SYMBOL
    return {"strategy": document, "lot_size": 250, "include_chart": False}


def test_backtest_job_matches_the_direct_backtest(client, dataset):
    """A job is the same backtest, just off the request thread."""
    direct = client.post("/api/backtest", json=_payload(client)).json()
    job = client.post("/api/jobs/backtest", json=_payload(client))
    assert job.status_code == 202
    job_id = job.json()["job"]["id"]

    from backend.app.jobs import get_runner

    record = get_runner().wait(job_id)
    assert record.status == "done", record.error
    assert record.result["metrics"] == direct["metrics"]
    assert record.result["trades"] == direct["trades"]
    assert record.result["data_fingerprint"] == direct["data_fingerprint"]

    listed = client.get("/api/jobs").json()["jobs"]
    assert job_id in [j["id"] for j in listed]
    assert all(j["result"] is None for j in listed)  # lists stay small


def test_job_events_stream_ends_with_done(client, dataset):
    job_id = client.post("/api/jobs/backtest", json=_payload(client)).json()["job"]["id"]
    events = []
    with client.stream("GET", f"/api/jobs/{job_id}/events") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("event: "):
                events.append(line.removeprefix("event: "))
            elif line.startswith("data: ") and events[-1] == "done":
                final = json.loads(line.removeprefix("data: "))
                break
    assert events[-1] == "done"
    assert final["status"] == "done" and final["result"]["metrics"]


def test_failed_job_reports_the_reason(client):
    payload = _payload(client)
    payload["symbol"] = "SIM|NO_SUCH_DATA"
    job_id = client.post("/api/jobs/backtest", json=payload).json()["job"]["id"]

    from backend.app.jobs import get_runner

    record = get_runner().wait(job_id)
    assert record.status == "failed"
    assert "SIM|NO_SUCH_DATA" in record.error


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/job_nope").status_code == 404
    assert client.post("/api/jobs/job_nope/cancel").status_code == 404
