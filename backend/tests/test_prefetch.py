"""P3.3: planning and running a rate-limited, resumable download for a universe."""

from __future__ import annotations

import threading
from datetime import date

import pytest

from backend.appstate.jobs import JobCancelled
from backend.core.config import settings
from backend.data import prefetch
from backend.data.upstox_client import CHUNK_DAYS, FetchResult

TODAY = date(2025, 3, 3)
KEYS = ["NSE_EQ|A", "NSE_EQ|B", "NSE_EQ|C"]


class Broker:
    """Serves 1-minute or daily bars per key, splitting a range into requests like Upstox."""

    def __init__(self, fail_keys=()):
        from backend.data.simulator import generate_ohlcv

        self.minute = generate_ohlcv("NIFTY", date(2025, 1, 1), date(2025, 2, 28), "1m", seed=5)
        self.daily = generate_ohlcv("NIFTY", date(2024, 1, 1), date(2025, 2, 28), "1d", seed=5)
        self.fail_keys = set(fail_keys)
        self.calls: list[tuple[str, date, date]] = []
        self.lock = threading.Lock()

    def __call__(self, key, tf, a, b, include_today=False, on_chunk=None):
        if key in self.fail_keys:
            raise RuntimeError("HTTP 500 from the broker")
        span = CHUNK_DAYS[tf.unit]
        chunks = -(-((b - a).days + 1) // span)
        for i in range(chunks):
            with self.lock:
                self.calls.append((key, a, b))
            if on_chunk:
                on_chunk(i + 1, chunks)
        series = self.minute if tf.unit == "minutes" else self.daily
        days = series["timestamp"].dt.date
        return FetchResult(frame=series[(days >= a) & (days <= b)].reset_index(drop=True), calls=chunks)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from backend.appstate import db
    from backend.data import store

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    yield
    db.close()
    store.close_db()


def _params(timeframe="1m", symbols=KEYS, start=date(2025, 1, 1), end=date(2025, 2, 28)):
    return prefetch.PrefetchParams.model_validate(
        {"universe": {"symbols": list(symbols)}, "timeframe": timeframe, "start": start, "end": end}
    )


def test_estimates_follow_the_binding_limit():
    assert prefetch.estimate_seconds(0) == 0
    assert prefetch.estimate_seconds(40) == pytest.approx(40 * prefetch.CALL_LATENCY)
    assert prefetch.binding_limit(40) is None, "40 requests: the network sets the pace"
    # 3,000 requests: 1,900 per 30 minutes binds, so one full window of waiting.
    assert prefetch.estimate_seconds(3000) == 1800
    assert prefetch.binding_limit(3000) == "1900 requests per 30 min"
    # The plan's own figure: about 8,800 one-minute requests is over two hours.
    assert prefetch.estimate_seconds(8800) >= 2 * 3600


def test_plan_counts_missing_sessions_and_requests(env):
    the_plan = prefetch.plan(_params(symbols=[*KEYS, "SIM|LOCAL"]), today=TODAY)
    rows = {r["key"]: r for r in the_plan["rows"]}
    assert rows["SIM|LOCAL"]["skip"].startswith("Not a broker instrument")
    # Two months of 1-minute data is 3 requests of at most 25 days each.
    assert all(rows[k]["calls"] == 3 and rows[k]["missing_sessions"] > 30 for k in KEYS)
    assert the_plan["calls"] == 9 and the_plan["to_download"] == 3 and the_plan["skipped"] == 1
    assert the_plan["eta_seconds"] == round(prefetch.estimate_seconds(9))

    daily = prefetch.plan(_params(timeframe="1d", start=date(2024, 1, 1)), today=TODAY)
    assert daily["calls"] == 3, "a year of daily bars is one request per stock"


def test_a_run_downloads_everything_and_leaves_nothing_to_plan(env):
    broker = Broker()
    messages: list[str] = []
    result = prefetch.run(_params(), fetcher=broker, today=TODAY,
                          on_progress=lambda f, m: messages.append(m))
    assert result["instruments"] == 3 and result["errors"] == []
    assert result["sessions_fetched"] > 90 and len(broker.calls) == 9
    assert any("left" in m and "/9 requests" in m for m in messages)
    again = prefetch.plan(_params(), today=TODAY)
    assert again["calls"] == 0 and again["up_to_date"] == 3


def test_a_cancelled_run_resumes_where_it_stopped(env):
    broker = Broker()
    seen = {"n": 0}

    def cancel_midway(fraction, message):
        seen["n"] += 1
        if "2 of 3" in message:            # stop once the second stock has begun
            raise JobCancelled("job")

    with pytest.raises(JobCancelled):
        prefetch.run(_params(), fetcher=broker, today=TODAY, on_progress=cancel_midway)
    first = {k for k, _, _ in broker.calls}
    assert "NSE_EQ|A" in first and "NSE_EQ|C" not in first

    # The rerun plans only what is missing: A is done, so it is not asked for again.
    left = prefetch.plan(_params(), today=TODAY)
    assert {r["key"] for r in left["rows"] if r["calls"]} == {"NSE_EQ|B", "NSE_EQ|C"}
    broker.calls.clear()
    prefetch.run(_params(), fetcher=broker, today=TODAY)
    assert "NSE_EQ|A" not in {k for k, _, _ in broker.calls}
    assert prefetch.plan(_params(), today=TODAY)["calls"] == 0


def test_one_failing_instrument_does_not_stop_the_rest(env):
    result = prefetch.run(_params(), fetcher=Broker(fail_keys={"NSE_EQ|B"}), today=TODAY)
    assert [e["key"] for e in result["errors"]] == ["NSE_EQ|B"] and "HTTP 500" in result["errors"][0]["error"]
    left = prefetch.plan(_params(), today=TODAY)
    assert [r["key"] for r in left["rows"] if r["calls"]] == ["NSE_EQ|B"]


def test_without_a_broker_a_run_refuses_clearly(env):
    with pytest.raises(RuntimeError, match="Upstox is not connected"):
        prefetch.run(_params())


def test_prefetch_api(env):
    from fastapi.testclient import TestClient

    from backend.api.main import app
    from backend.services.jobs import runner

    runner.start()
    try:
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        body = {"universe": {"symbols": KEYS}, "timeframe": "1d", "start": "2024-06-03", "end": "2024-12-31"}
        the_plan = client.post("/api/data/prefetch/plan", json=body).json()
        assert the_plan["calls"] == 3 and the_plan["broker_ready"] is False and the_plan["broker_reason"]

        refused = client.post("/api/jobs/prefetch", json=body)
        assert refused.status_code == 400 and "not connected" in refused.json()["detail"]
        assert client.post("/api/data/prefetch/plan", json={**body, "universe": {}}).status_code == 422

        # A stopped download can be run again with the same parameters.
        stopped = runner.wait(runner.submit("prefetch", {**body, "universe": {"symbols": KEYS}})["id"], timeout=30)
        assert stopped["status"] == "failed"
        resumed = client.post(f"/api/jobs/{stopped['id']}/resume")
        assert resumed.status_code == 202 and resumed.json()["id"] != stopped["id"]
        assert resumed.json()["params"] == stopped["params"]

        scan = runner.wait(runner.submit("scan", {"spec": {"name": "x", "universe": {"symbols": ["SIM|X"]},
                           "condition": {"op": "AND", "conditions": [{"left": "close", "cmp": ">", "right": 0}]}},
                           "fetch": False})["id"], timeout=30)
        assert client.post(f"/api/jobs/{scan['id']}/resume").status_code == 400
    finally:
        runner.shutdown(timeout=10)
