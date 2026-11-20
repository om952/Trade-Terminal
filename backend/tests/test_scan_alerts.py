"""P3.1: saved scans, alert checks that report only changes, and their schedule."""

from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd
import pytest

from backend.core.config import IST, settings
from backend.scan import alerts, saved

START = date(2025, 1, 1)


def _frame(closes: list[float], start: date = START) -> pd.DataFrame:
    days = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({
        "timestamp": [pd.Timestamp(d).tz_localize(IST) + pd.Timedelta(hours=9, minutes=15) for d in days],
        "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
        "close": closes, "volume": [1000] * len(closes), "oi": [0] * len(closes),
    })


@pytest.fixture
def env(tmp_path, monkeypatch):
    from backend.appstate import db
    from backend.data import store

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    yield
    db.close()
    store.close_db()


def _write(key: str, closes: list[float]) -> None:
    from backend.data import store

    store.write_candles(key, "1d", _frame(closes), source="simulated")


def _spec(threshold: float = 100, name: str = "Above 100", symbols=("SIM|A", "SIM|B", "SIM|C", "SIM|D")) -> dict:
    return {
        "name": name, "side": "long", "lookback": 5,
        "universe": {"symbols": list(symbols)},
        "condition": {"op": "AND", "conditions": [{"left": "close", "cmp": ">", "right": threshold}]},
    }


class Outbox(list):
    def __call__(self, text: str) -> bool:
        self.append(text)
        return True


# ---------------------------------------------------------------------------
# Saved scans
# ---------------------------------------------------------------------------

def test_saving_versions_only_real_changes(env):
    scan, created = saved.save(_spec())
    assert created and scan["version"] == 1 and scan["id"].startswith("scn_") and not scan["alerts"]

    again, created = saved.save(_spec(), scan_id=scan["id"])
    assert not created and again["version"] == 1

    on, created = saved.save(_spec(), scan_id=scan["id"], alerts=True)
    assert not created and on["alerts"], "turning alerts on is not a new version"

    changed, created = saved.save(_spec(threshold=150), scan_id=scan["id"])
    assert created and changed["version"] == 2 and changed["alerts"], "alerts stay on across versions"
    assert saved.get(scan["id"], version=1)["document"]["condition"]["conditions"][0]["right"] == 100

    saved.archive(scan["id"])
    assert saved.list_all() == [] and saved.alerting() == []
    assert saved.list_all(include_archived=True)[0]["alerts"] is False

    with pytest.raises(saved.ScanNotFound):
        saved.save(_spec(), scan_id="scn_missing")
    with pytest.raises(Exception, match="unknown"):
        saved.save({**_spec(), "condition": {"op": "AND", "conditions": [{"left": "x", "cmp": ">", "right": 1}]}})


# ---------------------------------------------------------------------------
# Changes between checks
# ---------------------------------------------------------------------------

def test_diff_reports_only_starts_and_stops():
    before = {
        "a": {"symbol": "A", "status": "waiting", "active": False},
        "b": {"symbol": "B", "status": "holding", "active": True},
        "c": {"symbol": "C", "status": "holding", "active": True},
        "d": {"symbol": "D", "status": "holding", "active": True},
    }
    after = {
        "a": {"symbol": "A", "status": "triggered_today", "active": True},
        "b": {"symbol": "B", "status": "lapsed", "active": False},
        "c": {"symbol": "C", "status": "holding", "active": True},
        "d": {"symbol": "D", "status": "no_data", "active": False},   # missing data is not an event
        "e": {"symbol": "E", "status": "holding", "active": True},    # new to the universe
    }
    events = alerts.diff(before, after)
    assert [(e["symbol"], e["kind"]) for e in events] == [
        ("A", "triggered"), ("B", "back_inside"), ("E", "triggered"),
    ]


def test_checks_baseline_then_report_changes_once(env):
    _write("SIM|A", [90.0] * 30)     # below the line
    _write("SIM|B", [110.0] * 30)    # above
    _write("SIM|C", [120.0] * 30)    # above
    scan, _ = saved.save(_spec(), alerts=True)
    outbox = Outbox()

    first = alerts.check(scan["id"], fetch=False, send=outbox)
    assert first["baseline"] and first["events"] == [] and first["sent"]
    assert first["scanned"] == 3 and first["active"] == 2
    assert len(outbox) == 1 and "Watching 3 stocks; 2 meet the condition" in outbox[0]

    # Same latest bar: nothing new to compare, nothing recorded, nothing sent.
    repeat = alerts.check(scan["id"], fetch=False, send=outbox)
    assert repeat["new_session"] is False and len(outbox) == 1

    # A new session: A crosses up, B falls back, C stays above, D still has no data.
    _write("SIM|A", [90.0] * 30 + [105.0])
    _write("SIM|B", [110.0] * 30 + [95.0])
    _write("SIM|C", [120.0] * 30 + [125.0])
    nxt = alerts.check(scan["id"], fetch=False, send=outbox)
    assert not nxt["baseline"]
    assert [(e["symbol"], e["kind"]) for e in nxt["events"]] == [("SIM|A", "triggered"), ("SIM|B", "back_inside")]
    text = outbox[-1]
    assert "Triggered: SIM|A (+5.0%)" in text and "Back inside: SIM|B" in text and "SIM|C" not in text

    history = saved.events(scan["id"])
    assert [(e["symbol"], e["kind"]) for e in history] == [("SIM|B", "back_inside"), ("SIM|A", "triggered")]
    assert history[0]["as_of"] == nxt["as_of"]

    # A changed spec starts a new baseline, even on the same bar.
    saved.save(_spec(threshold=100.5), scan_id=scan["id"])
    rebased = alerts.check(scan["id"], fetch=False, send=outbox)
    assert rebased["baseline"] and rebased["events"] == []


def test_messages_escape_names_and_cap_long_lists():
    events = [{"symbol": f"S{i}", "kind": "triggered", "detail": {"distance_pct": 1.0}} for i in range(25)]
    text = alerts.message("Close < 20-day low", "2026-11-19", events, baseline=False, side="short")
    assert "Close &lt; 20-day low" in text and "▼ Triggered" in text
    assert "S19 (+1.0%)" in text and "S20" not in text and "and 5 more" in text


def test_no_telegram_still_records_changes(env, monkeypatch):
    _write("SIM|A", [90.0] * 30)
    scan, _ = saved.save(_spec(symbols=("SIM|A",)), alerts=True)
    monkeypatch.setattr(alerts, "_telegram", lambda: (lambda text: False))
    result = alerts.check(scan["id"], fetch=False)
    assert result["baseline"] and result["sent"] is False and saved.latest_snapshot(scan["id"])


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

def _at(day: int, hh: int, mm: int) -> datetime:
    return datetime(2026, 11, day, hh, mm, tzinfo=IST)


def test_due_after_each_slot_and_only_once_after_a_gap():
    times = [time(8, 45)]
    assert alerts.due(_at(19, 9, 0), times, None)
    assert not alerts.due(_at(19, 9, 0), times, _at(19, 8, 50).isoformat())
    assert not alerts.due(_at(20, 8, 40), times, _at(19, 8, 50).isoformat()), "before today's slot"
    assert alerts.due(_at(20, 8, 46), times, _at(19, 8, 50).isoformat())
    # Closed for a week: one check owed, and none once it has run.
    assert alerts.due(_at(26, 12, 0), times, _at(19, 8, 50).isoformat())
    assert not alerts.due(_at(26, 12, 0), times, _at(26, 12, 0).isoformat())
    assert alerts.parse_times("18:00, 08:45") == [time(8, 45), time(18, 0)]
    assert alerts.last_slot(_at(20, 12, 0), [time(8, 45), time(18, 0)]) == _at(20, 8, 45)
    assert alerts.last_slot(_at(20, 7, 0), [time(8, 45), time(18, 0)]) == _at(19, 18, 0)


def test_scheduler_submits_alerting_scans_once_per_slot(env, monkeypatch):
    monkeypatch.setattr(settings, "scan_alert_times", "08:45")
    on, _ = saved.save(_spec(name="on"), alerts=True)
    saved.save(_spec(name="off"))
    submitted: list[tuple[str, dict]] = []
    clock = {"now": _at(19, 9, 0)}
    scheduler = alerts.Scheduler(lambda kind, params: submitted.append((kind, params)),
                                 clock=lambda: clock["now"])

    assert scheduler.tick() == [on["id"]]
    assert submitted == [("scan_check", {"scan_id": on["id"]})]
    assert scheduler.tick() == [], "already checked for this slot"
    clock["now"] = _at(20, 8, 50)
    assert scheduler.tick() == [on["id"]] and len(submitted) == 2


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_saved_scan_api(env):
    from fastapi.testclient import TestClient

    from backend.api.main import app
    from backend.services.jobs import runner

    _write("SIM|A", [90.0] * 30 + [105.0])
    runner.start()
    try:
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        created = client.post("/api/scans", json={"document": _spec(symbols=("SIM|A",)), "alerts": True})
        assert created.status_code == 201
        scan_id = created.json()["id"]
        listed = client.get("/api/scans").json()
        assert [s["id"] for s in listed["scans"]] == [scan_id]
        assert listed["alert_times"] == settings.scan_alert_times and listed["telegram"] is False

        updated = client.put(f"/api/scans/{scan_id}", json={"document": _spec(threshold=101, symbols=("SIM|A",))})
        assert updated.json()["new_version"] and updated.json()["version"] == 2

        job = client.post(f"/api/scans/{scan_id}/check")
        assert job.status_code == 202
        done = runner.wait(job.json()["id"], timeout=60)
        assert done["status"] == "succeeded" and done["result"]["baseline"], done

        detail = client.get(f"/api/scans/{scan_id}").json()
        assert detail["last_snapshot"]["version"] == 2 and detail["events"] == []
        assert client.get("/api/scans/scn_nope").status_code == 404
        assert client.post("/api/scans", json={"document": {"name": "x"}}).status_code == 422
        assert client.delete(f"/api/scans/{scan_id}").json()["archived"]
        assert client.get("/api/scans").json()["scans"] == []
    finally:
        runner.shutdown(timeout=10)
