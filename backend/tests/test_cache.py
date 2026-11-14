"""P1.1 gate: the read-through bar cache.

A fake broker serves a fixed simulated 1-minute series and counts calls, so the
tests can assert not just that the right bars come back but that the cache
asked for exactly what it lacked.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, time

import pandas as pd
import pytest

from backend.core.config import IST
from backend.data import cache
from backend.data.calendar import trading_days
from backend.data.simulator import generate_ohlcv
from backend.data.upstox_client import FetchResult, Timeframe

KEY = "NSE_INDEX|Nifty 50"
TODAY = date(2025, 3, 3)          # a Monday; the last completed session is Fri 28 Feb


@pytest.fixture
def store(tmp_path, monkeypatch):
    from backend.core.config import settings
    from backend.data import calendar as cal
    from backend.data import store as st

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(cal, "_derived_cache", set())
    st.close_db()
    yield st
    st.close_db()


class FakeBroker:
    """Serves slices of one fixed 1-minute series, like the real endpoint would."""

    def __init__(self, start=date(2025, 1, 1), end=date(2025, 2, 28), no_data=(), fail=()):
        self.series = generate_ohlcv("NIFTY", start, end, "1m", seed=11)
        self.no_data = set(no_data)          # sessions the exchange has nothing for
        self.fail = set(fail)                # sessions whose window errors out
        self.calls: list[tuple[date, date]] = []
        self.lock = threading.Lock()

    def __call__(self, key, tf, a, b, include_today=False, on_chunk=None):
        assert tf == cache.MINUTE and not include_today
        with self.lock:
            self.calls.append((a, b))
        if on_chunk:
            on_chunk(1, 1)
        days = self.series["timestamp"].dt.date
        frame = self.series[(days >= a) & (days <= b) & ~days.isin(self.no_data)]
        failed = [(d, d, "HTTP 500") for d in sorted(self.fail) if a <= d <= b]
        if failed:
            frame = frame[~frame["timestamp"].dt.date.isin(self.fail)]
        return FetchResult(frame.reset_index(drop=True), failed, 1)


def _get(broker, start, end, tf="5m", **kw):
    return cache.get_bars(KEY, tf, start, end, fetcher=broker, today=TODAY, **kw)


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------

def _minutes(day: date, start: str, closes: list[float], volume: int = 10) -> pd.DataFrame:
    first = pd.Timestamp(f"{day} {start}", tz=IST)
    rows = []
    for i, c in enumerate(closes):
        rows.append({
            "timestamp": first + pd.Timedelta(minutes=i),
            "open": c - 0.5, "high": c + 1, "low": c - 1, "close": c,
            "volume": volume, "oi": i,
        })
    return pd.DataFrame(rows)


def test_derive_aggregates_ohlcv_into_buckets_from_the_open():
    base = _minutes(date(2025, 1, 2), "09:15", [100, 101, 99, 102, 103, 104, 98])
    out = cache.derive(base, Timeframe.parse("5m"))

    assert list(out["timestamp"].dt.strftime("%H:%M")) == ["09:15", "09:20"]
    first = out.iloc[0]
    assert first["open"] == 99.5            # first bar's open
    assert first["high"] == 104             # max of 101,102,100,103,104
    assert first["low"] == 98               # min of 99,100,98,101,102
    assert first["close"] == 103            # fifth bar's close
    assert first["volume"] == 50
    assert first["oi"] == 4                 # last bar's open interest
    assert out.iloc[1]["close"] == 98 and out.iloc[1]["volume"] == 20


def test_derive_hourly_matches_the_exchange_grid():
    """09:15, 10:15 ... 15:15, the last bar holding 15 minutes."""
    day = generate_ohlcv("NIFTY", date(2025, 1, 2), date(2025, 1, 2), "1m", seed=3)
    out = cache.derive(day, Timeframe.parse("1h"))
    stamps = list(out["timestamp"].dt.strftime("%H:%M"))
    assert stamps == ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]
    last_bucket = day[day["timestamp"].dt.time >= time(15, 15)]
    assert len(last_bucket) == 15
    assert out.iloc[-1]["close"] == last_bucket["close"].iloc[-1]


@pytest.mark.parametrize("label, per_day", [("3m", 125), ("5m", 75), ("15m", 25), ("75m", 5)])
def test_derive_gives_full_sessions(label, per_day):
    days = generate_ohlcv("NIFTY", date(2025, 1, 1), date(2025, 1, 3), "1m", seed=5)
    out = cache.derive(days, Timeframe.parse(label))
    assert out["timestamp"].dt.date.value_counts().eq(per_day).all()
    assert (out["high"] >= out[["open", "close"]].max(axis=1)).all()
    assert (out["low"] <= out[["open", "close"]].min(axis=1)).all()
    assert out["volume"].sum() == days["volume"].sum()


def test_derive_uses_the_mcx_open():
    base = _minutes(date(2025, 1, 2), "09:00", list(range(100, 115)))
    out = cache.derive(base, Timeframe.parse("15m"), cache.session_open("MCX_FO|12345"))
    assert list(out["timestamp"].dt.strftime("%H:%M")) == ["09:00"]


def test_derive_refuses_daily():
    with pytest.raises(ValueError):
        cache.derive(pd.DataFrame(), Timeframe.parse("1d"))


# ---------------------------------------------------------------------------
# Fetching only what is missing
# ---------------------------------------------------------------------------

def test_first_request_fetches_and_derives(store):
    broker = FakeBroker()
    bars = _get(broker, date(2025, 1, 6), date(2025, 1, 31))

    sessions = trading_days(date(2025, 1, 6), date(2025, 1, 31))
    assert len(broker.calls) == 1, "contiguous missing sessions should be one range"
    assert len(bars.frame) == 75 * len(sessions)
    assert bars.derived_from == "1m" and bars.source == "upstox"
    assert bars.fetched_sessions == len(sessions)
    assert not bars.missing_sessions


def test_second_request_does_not_touch_the_network(store):
    broker = FakeBroker()
    first = _get(broker, date(2025, 1, 6), date(2025, 1, 31))
    calls = len(broker.calls)

    again = _get(broker, date(2025, 1, 6), date(2025, 1, 31))
    other_tf = _get(broker, date(2025, 1, 6), date(2025, 1, 31), tf="15m")

    assert len(broker.calls) == calls
    assert again.fingerprint == first.fingerprint
    assert len(other_tf.frame) == 25 * len(trading_days(date(2025, 1, 6), date(2025, 1, 31)))


def test_extending_the_range_fetches_only_the_new_sessions(store):
    broker = FakeBroker()
    _get(broker, date(2025, 1, 13), date(2025, 1, 24))
    broker.calls.clear()

    _get(broker, date(2025, 1, 6), date(2025, 2, 7))
    fetched = {d for a, b in broker.calls for d in trading_days(a, b)}
    assert fetched.isdisjoint(trading_days(date(2025, 1, 13), date(2025, 1, 24)))
    assert len(broker.calls) == 2


def test_close_gaps_are_bridged_into_one_call(store):
    broker = FakeBroker()
    _get(broker, date(2025, 1, 15), date(2025, 1, 15))   # one stored day in the middle
    broker.calls.clear()
    _get(broker, date(2025, 1, 13), date(2025, 1, 17))
    assert len(broker.calls) == 1


def test_sessions_with_no_data_are_remembered(store):
    """A day the exchange had nothing for is asked about once, not on every request."""
    holiday = date(2025, 1, 21)                         # a Tuesday unknown to the calendar
    broker = FakeBroker(no_data={holiday})
    first = _get(broker, date(2025, 1, 20), date(2025, 1, 24))
    assert holiday not in set(first.frame["timestamp"].dt.date)
    assert not first.missing_sessions, "a confirmed-empty session is not 'missing'"

    broker.calls.clear()
    _get(broker, date(2025, 1, 20), date(2025, 1, 24))
    assert broker.calls == []


def test_failed_windows_are_retried_not_remembered_as_empty(store):
    bad = date(2025, 1, 22)
    broker = FakeBroker(fail={bad})
    first = _get(broker, date(2025, 1, 20), date(2025, 1, 24))
    assert bad in first.missing_sessions
    assert any("retried" in n for n in first.notes)

    broker.fail.clear()
    broker.calls.clear()
    second = _get(broker, date(2025, 1, 20), date(2025, 1, 24))
    assert broker.calls == [(bad, bad)]
    assert bad in set(second.frame["timestamp"].dt.date)


def test_recent_sessions_are_rechecked_until_settled(store):
    broker = FakeBroker()
    friday = date(2025, 2, 28)
    _get(broker, date(2025, 2, 24), friday)               # checked on Mon 3 Mar: 3 days later
    broker.calls.clear()
    _get(broker, date(2025, 2, 24), friday)
    assert broker.calls == [], "checked 3 days after the session: settled"

    # Checked the very next day, the same session is not yet settled.
    cache.forget(KEY)
    store.delete_candles(KEY, "1m")
    cache.get_bars(KEY, "5m", date(2025, 2, 24), friday, fetcher=broker, today=date(2025, 3, 1))
    broker.calls.clear()
    cache.get_bars(KEY, "5m", date(2025, 2, 24), friday, fetcher=broker, today=date(2025, 3, 1))
    assert broker.calls == [(friday, friday)]


def test_today_is_never_fetched_from_history(store):
    broker = FakeBroker()
    bars = _get(broker, date(2025, 2, 24), TODAY)
    assert max(b for _, b in broker.calls) < TODAY
    assert bars.frame["timestamp"].iloc[-1].date() == date(2025, 2, 28)


def test_one_minute_history_starts_in_2022(store):
    broker = FakeBroker()
    bars = _get(broker, date(2021, 12, 27), date(2022, 1, 5))
    assert min(a for a, _ in broker.calls) >= date(2022, 1, 1)
    assert any("2022-01-01" in n for n in bars.notes)


def test_concurrent_requests_fetch_once(store):
    broker = FakeBroker()
    barrier = threading.Barrier(4)
    results = []

    def worker(tf):
        barrier.wait(timeout=10)
        results.append(_get(broker, date(2025, 1, 6), date(2025, 1, 31), tf=tf))

    threads = [threading.Thread(target=worker, args=(tf,)) for tf in ("5m", "15m", "5m", "1h")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(results) == 4
    assert len(broker.calls) == 1


def test_progress_is_reported(store):
    broker = FakeBroker()
    seen: list[tuple[float, str]] = []
    _get(broker, date(2025, 1, 6), date(2025, 1, 31), on_progress=lambda f, m: seen.append((f, m)))
    assert seen and seen[-1][0] == 1.0 and "Nifty 50" in seen[-1][1]


# ---------------------------------------------------------------------------
# Local-only data, legacy files, and no broker
# ---------------------------------------------------------------------------

def test_synthetic_data_is_never_fetched(store):
    broker = FakeBroker()
    frame = generate_ohlcv("NIFTY", date(2025, 1, 1), date(2025, 1, 31), "5m", seed=1)
    store.write_candles("SIM|NIFTY", "5m", frame, source="simulated")

    bars = cache.get_bars("SIM|NIFTY", "5m", fetcher=broker, today=TODAY)
    assert broker.calls == []
    assert len(bars.frame) == len(frame)
    assert bars.synthetic and bars.derived_from is None


def test_legacy_native_files_are_still_read(store, monkeypatch):
    """Migration: data stored per timeframe before the cache existed keeps working."""
    monkeypatch.setattr(cache, "broker_ready", lambda: (False, "no cached token"))
    legacy = generate_ohlcv("NIFTY", date(2025, 1, 6), date(2025, 1, 10), "5m", seed=2)
    store.write_candles(KEY, "5m", legacy, source="upstox")

    bars = cache.get_bars(KEY, "5m", date(2025, 1, 6), date(2025, 1, 10), today=TODAY)
    assert len(bars.frame) == len(legacy)
    assert bars.derived_from is None
    assert any("no cached token" in n for n in bars.notes)


def test_the_base_wins_over_a_legacy_file_once_it_covers_the_range(store):
    legacy = generate_ohlcv("NIFTY", date(2025, 1, 6), date(2025, 1, 10), "5m", seed=2)
    store.write_candles(KEY, "5m", legacy, source="upstox")
    bars = _get(FakeBroker(), date(2025, 1, 6), date(2025, 1, 10))
    assert bars.derived_from == "1m"


def test_without_a_token_nothing_is_fetched_and_the_reason_is_given(store, monkeypatch):
    monkeypatch.setattr(cache, "broker_ready", lambda: (False, "token expired"))
    bars = cache.get_bars(KEY, "5m", date(2025, 1, 6), date(2025, 1, 10), today=TODAY)
    assert bars.empty
    assert any("token expired" in n for n in bars.notes)
    assert bars.missing_sessions == trading_days(date(2025, 1, 6), date(2025, 1, 10))


def test_daily_bars_use_their_own_base(store):
    calls = []

    def daily(key, tf, a, b, include_today=False, on_chunk=None):
        calls.append(tf)
        frame = generate_ohlcv("NIFTY", a, b, "1d", seed=4)
        return FetchResult(frame, [], 1)

    bars = cache.get_bars(KEY, "1d", date(2025, 1, 6), date(2025, 1, 31), fetcher=daily, today=TODAY)
    assert calls == [cache.DAY]
    assert bars.derived_from is None and len(bars.frame) == len(
        trading_days(date(2025, 1, 6), date(2025, 1, 31))
    )


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

def test_fingerprint_tracks_the_data():
    frame = generate_ohlcv("NIFTY", date(2025, 1, 6), date(2025, 1, 10), "5m", seed=2)
    a = cache.fingerprint(KEY, "5m", "upstox", frame)
    assert a == cache.fingerprint(KEY, "5m", "upstox", frame.copy())

    nudged = frame.copy()
    nudged.loc[10, "close"] += 0.05
    assert cache.fingerprint(KEY, "5m", "upstox", nudged) != a
    assert cache.fingerprint(KEY, "15m", "upstox", frame) != a


def test_describe_is_json_safe(store):
    import json

    bars = _get(FakeBroker(), date(2025, 1, 6), date(2025, 1, 10))
    payload = bars.describe()
    json.dumps(payload)
    assert payload["bars"] == len(bars.frame)
    assert payload["first_bar"].startswith("2025-01-06T09:15")
    assert datetime.fromisoformat(payload["last_bar"]).time() == time(15, 25)


# ---------------------------------------------------------------------------
# End to end: no download step
# ---------------------------------------------------------------------------

def test_backtest_on_an_empty_store_downloads_what_it_needs(store, monkeypatch):
    """The M1 promise: pick an instrument, run a backtest, nothing to fetch by hand."""
    from fastapi.testclient import TestClient

    from backend.api.main import app
    from backend.data import upstox_client
    from backend.strategy import templates

    broker = FakeBroker()
    monkeypatch.setattr(cache, "broker_ready", lambda: (True, ""))
    monkeypatch.setattr(upstox_client, "fetch_candles_report", broker)

    strategy = templates.load("ema_crossover", symbol=KEY).model_dump(mode="json")
    strategy["timeframe"] = "15m"
    client = TestClient(app, base_url="http://127.0.0.1:8000")
    body = {"strategy": strategy, "start": "2025-01-06", "end": "2025-02-28", "include_chart": False}

    first = client.post("/api/backtest", json=body)
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["derived_from"] == "1m" and data["source"] == "upstox"
    assert data["fetched_sessions"] == len(trading_days(date(2025, 1, 6), date(2025, 2, 28)))
    assert len(broker.calls) == 1          # one contiguous range (chunked inside the real client)

    calls = len(broker.calls)
    second = client.post("/api/backtest", json=body)
    assert len(broker.calls) == calls, "the rerun downloaded again"
    assert second.json()["data"]["fingerprint"] == data["fingerprint"]
    assert second.json()["metrics"] == first.json()["metrics"]


def test_a_coarser_timeframe_derives_from_a_stored_finer_one(store, monkeypatch):
    """Legacy native 5m still serves 15m: the aggregation is exact under nesting."""
    monkeypatch.setattr(cache, "broker_ready", lambda: (False, "no cached token"))
    minute = generate_ohlcv("NIFTY", date(2025, 1, 6), date(2025, 1, 10), "1m", seed=8)
    five = cache.derive(minute, Timeframe.parse("5m"))
    store.write_candles(KEY, "5m", five, source="upstox")

    bars = cache.get_bars(KEY, "15m", date(2025, 1, 6), date(2025, 1, 10), today=TODAY)
    assert bars.derived_from == "5m"
    expected = cache.derive(minute, Timeframe.parse("15m"))
    pd.testing.assert_frame_equal(bars.frame.reset_index(drop=True), expected, check_dtype=False)

    # 10m does not nest in 15m, so a stored 10m series must not be used for it.
    ten = cache.derive(minute, Timeframe.parse("10m"))
    store.write_candles("NSE_EQ|TENONLY", "10m", ten, source="upstox")
    assert cache.get_bars("NSE_EQ|TENONLY", "15m", date(2025, 1, 6), date(2025, 1, 10),
                          today=TODAY).empty
    assert len(cache.get_bars("NSE_EQ|TENONLY", "30m", date(2025, 1, 6), date(2025, 1, 10),
                              today=TODAY).frame) == 13 * 5
