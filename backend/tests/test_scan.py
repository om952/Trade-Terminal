"""P3.1: scans across a universe, checked against independent pandas calculations.

The gate for Phase 3 asks for scans hand-verified against an independent
calculation. Each check below recomputes the answer with plain pandas, never
through the indicator registry or the condition evaluator, and compares.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from backend.core.config import settings
from backend.scan import engine
from backend.scan import service as scan
from backend.scan.spec import ScanSpec

SYMBOLS = ["SIM|SCAN_A", "SIM|SCAN_B", "SIM|SCAN_C", "SIM|SCAN_D"]


@pytest.fixture
def data(tmp_path, monkeypatch):
    from backend.appstate import db
    from backend.data import store
    from backend.data.simulator import generate_ohlcv

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    frames = {}
    for seed, key in enumerate(SYMBOLS, start=11):
        frame = generate_ohlcv("RELIANCE", date(2024, 1, 1), date(2025, 9, 30), "1d", seed=seed)
        store.write_candles(key, "1d", frame, source="simulated")
        frames[key] = frame.reset_index(drop=True)
    short = generate_ohlcv("RELIANCE", date(2025, 9, 1), date(2025, 9, 20), "1d", seed=3)
    store.write_candles("SIM|SCAN_SHORT", "1d", short, source="simulated")
    yield frames
    db.close()
    store.close_db()


def _spec(condition, indicators=(), **extra) -> dict:
    return {
        "name": "test scan",
        "universe": {"symbols": extra.pop("symbols", SYMBOLS)},
        "indicators": list(indicators),
        "condition": {"op": "AND", "conditions": [condition]},
        **extra,
    }


def _run(document, mode="now", **params):
    return scan.run(scan.ScanParams(spec=document, mode=mode, fetch=False, **params))


# ---------------------------------------------------------------------------
# Check 1: close above its 50-day SMA (now)
# ---------------------------------------------------------------------------

def _expected_now(held: pd.Series, frame: pd.DataFrame, lookback: int, warmup: int) -> dict:
    """Status from a boolean series, computed independently of engine.scan_now."""
    n = len(frame)
    start = max(warmup, n - lookback)
    window = held.iloc[start:].to_numpy()
    day = lambda i: frame["timestamp"].iloc[i].date().isoformat()  # noqa: E731
    if window[-1]:
        k = len(window) - 1
        while k > 0 and window[k - 1]:
            k -= 1
        first = start + k
        return {"status": "triggered_today" if first == n - 1 else "holding", "since": day(first)}
    hits = np.flatnonzero(window)
    if hits.size:
        return {"status": "lapsed", "last_fired": day(start + int(hits[-1]))}
    return {"status": "waiting"}


def test_close_above_sma_matches_pandas(data):
    document = _spec({"left": "close", "cmp": ">", "right": "sma50"},
                     [{"id": "sma50", "type": "SMA", "params": {"period": 50}}], lookback=60)
    result = _run(document)
    rows = {r["key"]: r for r in result["rows"]}
    assert result["counts"].get("error", 0) == 0

    for key, frame in data.items():
        sma = frame["close"].rolling(50).mean()
        held = frame["close"] > sma
        expected = _expected_now(held, frame, 60, warmup=49)
        row = rows[key]
        assert row["status"] == expected["status"], key
        for field in ("since", "last_fired"):
            if field in expected:
                assert row[field] == expected[field], (key, field)
        gap = (frame["close"].iloc[-1] - sma.iloc[-1]) / sma.iloc[-1] * 100
        assert row["distance_pct"] == pytest.approx(gap, abs=1e-3), key
        assert row["active"] == bool(held.iloc[-1])
        assert row["as_of"] == "2025-09-30" and not row["stale"]


# ---------------------------------------------------------------------------
# Check 2: close crosses above the prior day's high (now, context path)
# ---------------------------------------------------------------------------

def test_cross_of_prior_day_high_matches_pandas(data):
    document = _spec({"left": "close", "cmp": "crosses_above", "right": "prev_day_high"},
                     lookback=120)
    rows = {r["key"]: r for r in _run(document)["rows"]}
    for key, frame in data.items():
        prev_high = frame["high"].shift(1)
        above = frame["close"] > prev_high
        crossed = above & ~(frame["close"].shift(1) > prev_high.shift(1)).fillna(True)
        # The first bars have no prior-day level, or no prior bar to cross from.
        crossed.iloc[:2] = False
        expected = _expected_now(crossed, frame, 120, warmup=0)
        row = rows[key]
        assert row["status"] == expected["status"], key
        assert row.get("last_fired") == expected.get("last_fired", row.get("last_fired")), key
        # A cross holds for one bar, so "since" is always the cross day itself.
        if row["status"] == "triggered_today":
            assert row["since"] == row["as_of"]


# ---------------------------------------------------------------------------
# Check 3: close below 97% of its 20-day SMA (history, forward returns, baseline)
# ---------------------------------------------------------------------------

def test_history_events_and_forward_returns_match_pandas(data):
    document = _spec(
        {"left": "close", "cmp": "<", "right": {"fn": "mul", "args": ["sma20", 0.97]}},
        [{"id": "sma20", "type": "SMA", "params": {"period": 20}}],
    )
    result = _run(document, mode="history")
    assert result["mode"] == "history"
    pooled = {h: [] for h in ("1", "5", "20")}
    baseline = {h: [] for h in ("1", "5", "20")}

    for key, frame in data.items():
        close, open_ = frame["close"], frame["open"]
        held = (close < close.rolling(20).mean() * 0.97).to_numpy().copy()
        held[:19] = False
        onset = held & ~np.concatenate([[False], held[:-1]])
        row = next(r for r in result["rows"] if r["key"] == key)
        dates = [frame["timestamp"].iloc[i].date().isoformat() for i in np.flatnonzero(onset)]
        assert [e["date"] for e in row["events"]] == dates, key

        n = len(frame)
        for i in np.flatnonzero(onset):
            event = next(e for e in row["events"] if e["date"] == frame["timestamp"].iloc[i].date().isoformat())
            for h in (1, 5, 20):
                if i + h < n:
                    expected = (close.iloc[i + h] / open_.iloc[i + 1] - 1) * 100
                    assert event["forward"][str(h)] == pytest.approx(expected, abs=1e-4)
                    pooled[str(h)].append(expected)
                else:
                    assert event["forward"][str(h)] is None
        for h in (1, 5, 20):
            for i in range(19, n - h):
                baseline[str(h)].append((close.iloc[i + h] / open_.iloc[i + 1] - 1) * 100)

    for h in ("1", "5", "20"):
        signal, base = result["summary"][h]["signal"], result["summary"][h]["baseline"]
        assert signal["count"] == len(pooled[h]) and signal["count"] > 0
        assert signal["mean"] == pytest.approx(np.mean(pooled[h]), abs=1e-3)
        assert base["count"] == len(baseline[h])
        assert base["mean"] == pytest.approx(np.mean(baseline[h]), abs=1e-3)
        assert base["hit_rate"] == pytest.approx(np.mean(np.array(baseline[h]) > 0) * 100, abs=0.01)
    assert any("baseline" in note for note in result["notes"])


# ---------------------------------------------------------------------------
# Coverage: every instrument is reported, with the reason when it was not scanned
# ---------------------------------------------------------------------------

def test_every_instrument_is_reported_with_a_reason(data):
    document = _spec({"left": "close", "cmp": ">", "right": "sma50"},
                     [{"id": "sma50", "type": "SMA", "params": {"period": 50}}],
                     symbols=[*SYMBOLS, "SIM|SCAN_SHORT", "SIM|NOTHING_STORED"])
    result = _run(document)
    rows = {r["key"]: r for r in result["rows"]}
    assert result["universe"] == 6 and len(rows) == 6
    assert rows["SIM|SCAN_SHORT"]["status"] == "not_enough_data"
    assert rows["SIM|NOTHING_STORED"]["status"] == "no_data" and rows["SIM|NOTHING_STORED"]["reason"]
    assert sum(result["counts"].values()) == 6
    order = [r["status"] for r in result["rows"]]
    assert order == sorted(order, key=lambda s: scan._ORDER[s]), "what just happened comes first"
    assert any("synthetic" in n for n in result["notes"])


def test_stale_instruments_are_flagged(data):
    from backend.data import store
    from backend.data.simulator import generate_ohlcv

    old = generate_ohlcv("RELIANCE", date(2024, 1, 1), date(2025, 8, 29), "1d", seed=5)
    store.write_candles("SIM|SCAN_OLD", "1d", old, source="simulated")
    document = _spec({"left": "close", "cmp": ">", "right": 0}, symbols=["SIM|SCAN_A", "SIM|SCAN_OLD"])
    rows = {r["key"]: r for r in _run(document)["rows"]}
    assert rows["SIM|SCAN_OLD"]["stale"] and not rows["SIM|SCAN_A"]["stale"]


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------

def test_spec_rejects_what_cannot_be_scanned():
    base = _spec({"left": "close", "cmp": ">", "right": "sma50"},
                 [{"id": "sma50", "type": "SMA", "params": {"period": 50}}])
    ScanSpec.model_validate(base)

    for bad, needle in (
        ({**base, "condition": {"op": "AND", "conditions": [{"left": "close", "cmp": ">", "right": "ema9"}]}}, "ema9"),
        ({**base, "condition": {"op": "AND", "conditions": [{"left": "close", "cmp": ">", "right": "vwap"}]}}, "vwap"),
        ({**base, "condition": {"op": "AND", "conditions": [{"left": "bars_since_entry", "cmp": ">", "right": 1}]}}, "bars_since_entry"),
        ({**base, "universe": {"symbols": ["A", "A"]}}, "twice"),
        ({**base, "universe": {"symbols": ["A"], "preset": "fno_stocks"}}, "either"),
        ({**base, "universe": {}}, "either"),
        ({**base, "timeframe": "5m"}, "timeframe"),
    ):
        with pytest.raises(Exception) as err:
            ScanSpec.model_validate(bad)
        assert needle in str(err.value), (needle, str(err.value))


def test_distance_is_only_defined_for_one_comparison(data):
    frame = data[SYMBOLS[0]]
    spec = ScanSpec.model_validate({
        "name": "two", "universe": {"symbols": ["X"]},
        "condition": {"op": "AND", "conditions": [
            {"left": "close", "cmp": ">", "right": 0}, {"left": "volume", "cmp": ">", "right": 0}]},
    })
    assert engine.scan_now(spec, frame)["distance_pct"] is None
    below = ScanSpec.model_validate({
        "name": "below", "universe": {"symbols": ["X"]},
        "condition": {"op": "AND", "conditions": [{"left": "close", "cmp": "<", "right": 100000}]},
    })
    # Short of a "below" line is measured the other way round: positive means past it.
    expected = (100000 - frame["close"].iloc[-1]) / 100000 * 100
    assert engine.scan_now(below, frame)["distance_pct"] == pytest.approx(expected, abs=1e-3)


# ---------------------------------------------------------------------------
# The F&O preset, the job and the API
# ---------------------------------------------------------------------------

def _seed_master():
    from backend.data.store import get_db

    db = get_db()
    rows = [
        ("NSE_EQ|INE001", "AAA", "EQ", "NSE_EQ", None, None),
        ("NSE_EQ|INE002", "BBB", "EQ", "NSE_EQ", None, None),
        ("NSE_FO|1", "AAA FUT", "FUT", "NSE_FO", "NSE_EQ|INE001", "AAA"),
        ("NSE_FO|2", "AAA FUT 2", "FUT", "NSE_FO", "NSE_EQ|INE001", "AAA"),
        ("NSE_FO|3", "BBB FUT", "FUT", "NSE_FO", "NSE_EQ|INE002", "BBB"),
        ("NSE_FO|4", "NIFTY FUT", "FUT", "NSE_FO", "NSE_INDEX|Nifty 50", "NIFTY"),
    ]
    for key, sym, kind, seg, under, under_sym in rows:
        db.execute(
            "INSERT INTO instruments VALUES (?, ?, ?, 'NSE', ?, ?, 1, 0.05, NULL, NULL, ?, ?)",
            [key, sym, sym, seg, kind, under, under_sym],
        )


def test_fno_preset_lists_stock_underlyings_only(data):
    from backend.scan.spec import Universe

    with pytest.raises(ValueError, match="instrument master"):
        scan.resolve_universe(Universe(preset="fno_stocks"))
    _seed_master()
    assert scan.resolve_universe(Universe(preset="fno_stocks")) == [
        {"key": "NSE_EQ|INE001", "symbol": "AAA"}, {"key": "NSE_EQ|INE002", "symbol": "BBB"},
    ]


def test_scan_job_through_the_api(data):
    from fastapi.testclient import TestClient

    from backend.api.main import app
    from backend.services.jobs import runner

    runner.start()
    try:
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        document = _spec({"left": "close", "cmp": ">", "right": "sma50"},
                         [{"id": "sma50", "type": "SMA", "params": {"period": 50}}])
        checked = client.post("/api/scans/validate", json=document).json()
        assert checked["valid"] and checked["universe"] == 4

        bad = client.post("/api/scans/validate", json={**document, "indicators": []}).json()
        assert not bad["valid"] and "sma50" in bad["errors"][0]["message"]
        assert client.post("/api/jobs/scan", json={"spec": {**document, "indicators": []}}).status_code == 422
        no_master = client.post("/api/jobs/scan", json={"spec": {**document, "universe": {"preset": "fno_stocks"}}})
        assert no_master.status_code == 400 and "instrument master" in no_master.json()["detail"]

        job = client.post("/api/jobs/scan", json={"spec": document, "fetch": False})
        assert job.status_code == 202
        done = runner.wait(job.json()["id"], timeout=60)
        assert done["status"] == "succeeded", done
        assert done["result"]["universe"] == 4 and done["result"]["mode"] == "now"
        assert done["result"] == _run(document), "the job and a direct run agree"
    finally:
        runner.shutdown(timeout=10)


def test_a_line_never_reached_is_waiting_with_the_distance_to_go(data):
    document = _spec({"left": "close", "cmp": ">", "right": 1_000_000}, lookback=250)
    for row in _run(document)["rows"]:
        assert row["status"] == "waiting" and row["last_fired"] is None
        assert row["distance_pct"] < -99, "short of the line reads as negative"
