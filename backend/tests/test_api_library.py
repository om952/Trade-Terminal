"""Library and run-history endpoints, and runs recorded by backtests and sessions."""

from __future__ import annotations

import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.data.simulator import generate_ohlcv
from backend.data.store import write_candles

SYMBOL = "SIM|LIBTEST"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app, base_url="http://localhost")


@pytest.fixture(scope="module")
def dataset() -> str:
    df = generate_ohlcv("RELIANCE", date(2025, 6, 1), date(2025, 12, 31), "5m", seed=77)
    write_candles(SYMBOL, "5m", df, source="simulated")
    return SYMBOL


def _template(client, name="ema_crossover") -> dict:
    document = client.get(f"/api/templates/{name}").json()
    document["timeframe"] = "5m"
    document["instrument"]["symbol"] = SYMBOL
    return document


def test_templates_appear_as_read_only_entries(client):
    listed = client.get("/api/strategies").json()["strategies"]
    ids = {s["id"] for s in listed}
    assert "tpl_ema_crossover" in ids
    template = next(s for s in listed if s["id"] == "tpl_ema_crossover")
    assert template["read_only"] is True

    response = client.post("/api/strategies/tpl_ema_crossover/versions",
                           json={"document": _template(client)})
    assert response.status_code == 409
    assert "Fork it" in response.json()["detail"]


def test_create_version_rename_archive_delete(client):
    created = client.post("/api/strategies", json={"document": _template(client), "name": "API EMA"})
    assert created.status_code == 201
    sid = created.json()["strategy"]["id"]

    detail = client.get(f"/api/strategies/{sid}").json()
    assert detail["strategy"]["name"] == "API EMA"
    assert detail["head"]["document"]["name"] == "API EMA"
    assert detail["trials"] == {"backtests": 0, "distinct_versions": 0}

    edited = _template(client)
    edited["indicators"][0]["params"]["period"] = 12
    saved = client.post(f"/api/strategies/{sid}/versions", json={"document": edited, "note": "faster"})
    assert saved.status_code == 201
    assert saved.json()["strategy"]["version_count"] == 2

    versions = client.get(f"/api/strategies/{sid}/versions").json()["versions"]
    assert len(versions) == 2 and "document" not in versions[0]
    one = client.get(f"/api/strategies/{sid}/versions/{versions[1]['hash']}").json()["version"]
    assert one["document"]["indicators"][0]["params"]["period"] != 12

    renamed = client.patch(f"/api/strategies/{sid}", json={"name": "Renamed EMA"}).json()
    assert renamed["strategy"]["name"] == "Renamed EMA"
    assert renamed["strategy"]["version_count"] == 2  # a rename is not a new version

    client.patch(f"/api/strategies/{sid}", json={"archived": True})
    assert sid not in {s["id"] for s in client.get("/api/strategies").json()["strategies"]}

    assert client.delete(f"/api/strategies/{sid}").status_code == 204
    assert client.get(f"/api/strategies/{sid}").status_code == 404


def test_invalid_document_is_rejected_with_field_errors(client):
    response = client.post("/api/strategies", json={"document": {"name": "x"}})
    assert response.status_code == 422
    assert response.json()["detail"]["errors"]


def test_backtest_records_a_run_with_fingerprint_and_counts_a_trial(client, dataset):
    sid = client.post("/api/strategies", json={"document": _template(client)}).json()["strategy"]["id"]
    body = client.post("/api/backtest", json={
        "strategy": _template(client), "library_id": sid, "lot_size": 250, "include_chart": False,
    }).json()
    run_id = body["run_id"]
    assert body["data_fingerprint"]["rows"] > 0

    run = client.get(f"/api/runs/{run_id}").json()["run"]
    assert run["status"] == "done" and run["kind"] == "backtest"
    assert run["strategy_id"] == sid
    assert run["metrics"]["total_trades"] == body["metrics"]["total_trades"]
    assert run["data_fingerprint"] == body["data_fingerprint"]
    assert run["params"]["lot_size"] == 250

    assert client.get(f"/api/strategies/{sid}").json()["trials"]["backtests"] == 1
    assert run_id in [r["id"] for r in client.get(f"/api/runs?library_id={sid}").json()["runs"]]


def test_same_data_gives_same_fingerprint(client, dataset):
    payload = {"strategy": _template(client), "lot_size": 250, "include_chart": False}
    a = client.post("/api/backtest", json=payload).json()["data_fingerprint"]
    b = client.post("/api/backtest", json=payload).json()["data_fingerprint"]
    assert a == b


def test_replay_session_is_recorded_as_a_run(client, dataset):
    document = _template(client)
    document["name"] = "run history replay"
    started = client.post("/api/live/start", json={
        "strategy": document, "symbol": SYMBOL, "source": "replay",
        "replay_start": "2025-09-01", "replay_end": "2025-10-31", "replay_speed": 0,
        "alerts": False, "lot_size": 250,
    }).json()
    run_id = started["run_id"]
    assert run_id

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()["run"]
        if run["status"] != "running":
            break
        time.sleep(0.1)
    assert run["status"] == "done", run
    assert run["kind"] == "replay"
    assert run["metrics"]["session_id"] == started["session_id"]
    assert run["metrics"]["bars_seen"] > 0
    assert run["result_path"].endswith(".jsonl")


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/run_nope").status_code == 404
