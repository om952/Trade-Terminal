"""Partitioned candle store, 1-minute derivation, the data service, and migration."""

from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, time

import pandas as pd
import pytest

from backend.app import preferences
from backend.app.context import AppContext
from backend.core.config import IST
from backend.data import calendar as cal
from backend.data.candles import CandleStore
from backend.data.resample import compare, derive
from backend.data.service import DataService, merge_ranges
from backend.data.simulator import generate_ohlcv

KEY = "NSE_INDEX|Test Index"


@pytest.fixture
def ctx(tmp_path, monkeypatch) -> AppContext:
    from backend.core.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return AppContext(user_id="local", data_dir=tmp_path)


@pytest.fixture
def store(ctx) -> CandleStore:
    return CandleStore(ctx.data_dir / "store" / "candles", "library", ctx)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def test_store_partitions_by_month_and_reads_across_them(store):
    df = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 3, 31), "1d", seed=1)
    assert store.write(KEY, "1d", df, "upstox") == len(df)
    assert store.months(KEY, "1d") == ["2026-01", "2026-02", "2026-03"]

    feb = store.read(KEY, "1d", date(2026, 2, 1), date(2026, 2, 28))
    assert set(feb["timestamp"].dt.month) == {2}
    assert len(store.read(KEY, "1d")) == len(df)

    info = store.info(KEY, "1d")
    assert info.rows == len(df) and info.months == 3 and info.source == "upstox"
    assert info.first_bar.startswith("2026-01") and info.last_bar.startswith("2026-03")
    assert store.size_bytes() == info.bytes > 0


def test_store_merge_dedupes_with_newer_bar_winning(store):
    df = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 1, 31), "1d", seed=1)
    store.write(KEY, "1d", df, "upstox")
    revised = df.copy()
    revised["close"] += 1.0
    assert store.write(KEY, "1d", revised, "upstox") == len(df)
    assert store.read(KEY, "1d")["close"].tolist() == pytest.approx(revised["close"].tolist())


def test_store_delete_and_delete_all(store):
    df = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 2, 28), "1d", seed=1)
    store.write(KEY, "1d", df, "upstox")
    store.write("NSE_EQ|OTHER", "1d", df, "upstox")
    store.delete(KEY, "1d")
    assert store.info(KEY, "1d") is None and store.read(KEY, "1d").empty
    assert [s.instrument_key for s in store.all_series()] == ["NSE_EQ|OTHER"]
    store.delete_all()
    assert store.all_series() == [] and not store.root.exists()


def test_two_processes_merging_one_series_lose_nothing(store, ctx):
    """The API and a data CLI may write the same series at once."""
    script = (
        "import sys; from datetime import date; from pathlib import Path\n"
        "from backend.app.context import AppContext\n"
        "from backend.data.candles import CandleStore\n"
        "from backend.data.simulator import generate_ohlcv\n"
        "ctx = AppContext('local', Path(sys.argv[1]))\n"
        "store = CandleStore(ctx.data_dir / 'store' / 'candles', 'library', ctx)\n"
        "m = int(sys.argv[2])\n"
        "for day in range(1, 28, 3):\n"
        "    df = generate_ohlcv('NIFTY', date(2026, m, day), date(2026, m, day + 2), '1d', seed=day)\n"
        "    store.write('NSE_INDEX|Test Index', '1d', df, 'upstox')\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", script, str(ctx.data_dir), "3"]) for _ in range(3)]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0]
    stored = store.read(KEY, "1d")
    assert stored["timestamp"].is_unique
    expected = len(cal.trading_days(date(2026, 3, 1), date(2026, 3, 29)))
    assert len(stored) == expected
    assert store.info(KEY, "1d").rows == expected


# ---------------------------------------------------------------------------
# Derivation from 1-minute bars
# ---------------------------------------------------------------------------

def _one_minute_day(day: date, minutes: int = 375, start: time = time(9, 15)) -> pd.DataFrame:
    stamps = pd.date_range(datetime.combine(day, start), periods=minutes, freq="1min", tz=IST)
    n = len(stamps)
    return pd.DataFrame({
        "timestamp": stamps,
        "open": [100.0 + i for i in range(n)],
        "high": [101.0 + i for i in range(n)],
        "low": [99.0 + i for i in range(n)],
        "close": [100.5 + i for i in range(n)],
        "volume": [10] * n,
        "oi": list(range(n)),
    })


def test_derive_five_minute_bars_from_the_open():
    day = date(2026, 1, 5)
    bars = derive(_one_minute_day(day), "5m")
    assert len(bars) == 75
    first = bars.iloc[0]
    assert first["timestamp"] == pd.Timestamp(datetime.combine(day, time(9, 15)), tz=IST)
    assert (first["open"], first["high"], first["low"], first["close"]) == (100.0, 105.0, 99.0, 104.5)
    assert first["volume"] == 50 and first["oi"] == 4
    assert bars.iloc[-1]["timestamp"].time() == time(15, 25)


def test_derive_hourly_bars_anchor_at_9_15_and_end_short():
    bars = derive(_one_minute_day(date(2026, 1, 5)), "1h")
    assert [t.time() for t in bars["timestamp"]] == [
        time(9, 15), time(10, 15), time(11, 15), time(12, 15), time(13, 15), time(14, 15), time(15, 15)]
    assert bars.iloc[-1]["volume"] == 15 * 10  # 15:15 to 15:30 is a 15-minute bar


def test_derive_mcx_anchors_at_9_00():
    bars = derive(_one_minute_day(date(2026, 1, 5), minutes=60, start=time(9, 0)), "15m", mcx=True)
    assert [t.time() for t in bars["timestamp"]] == [time(9, 0), time(9, 15), time(9, 30), time(9, 45)]


def test_derive_keeps_days_separate():
    two_days = pd.concat([_one_minute_day(date(2026, 1, 5)), _one_minute_day(date(2026, 1, 6))])
    bars = derive(two_days, "15m")
    assert len(bars) == 50
    assert bars["timestamp"].dt.date.nunique() == 2


def test_compare_reports_identical_and_differences():
    native = derive(_one_minute_day(date(2026, 1, 5)), "5m")
    assert compare(native, native)["identical"]

    off = native.copy()
    off.loc[3, "close"] += 0.05
    off = off.drop(index=10)
    report = compare(native, off)
    assert not report["identical"]
    assert report["mismatched_bars"] == 1
    assert report["only_in_derived"] == 1 and report["only_in_native"] == 0
    assert report["examples"][0]["field"] == "close"


# ---------------------------------------------------------------------------
# Data service
# ---------------------------------------------------------------------------

class FakeBroker:
    """Serves simulated bars for whatever range is asked, minus chosen sessions."""

    def __init__(self, no_data: set[date] = frozenset(), fail: set[date] = frozenset(),
                 error: Exception | None = None) -> None:
        self.calls: list[tuple[str, date, date]] = []
        self.no_data, self.fail, self.error = set(no_data), set(fail), error

    def __call__(self, key, timeframe, first, last, *, include_today, on_progress):
        self.calls.append((timeframe, first, last))
        if self.error:
            raise self.error
        if on_progress:
            on_progress(0, 1)
        days = [d for d in cal.trading_days(first, last) if d not in self.no_data | self.fail]
        frames = [generate_ohlcv("NIFTY", d, d, timeframe, seed=d.toordinal()) for d in days]
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
        df.attrs["failed_ranges"] = [(d, d) for d in sorted(self.fail)]
        return df


TODAY = date(2026, 3, 13)  # a Friday


def _service(ctx, broker, now=None):
    now = now or datetime.combine(TODAY, time(18, 0), tzinfo=IST)  # after the close
    return DataService(ctx, fetcher=broker, now=lambda: now)


def test_merge_ranges():
    days = [date(2026, 1, d) for d in (1, 2, 5, 6, 20, 21)]
    assert merge_ranges(days, 5) == [(date(2026, 1, 1), date(2026, 1, 6)),
                                     (date(2026, 1, 20), date(2026, 1, 21))]


def test_plan_for_an_empty_store_wants_every_session(ctx):
    plan = _service(ctx, FakeBroker()).plan(KEY, "5m", date(2026, 2, 2), date(2026, 2, 27))
    needed = cal.trading_days(date(2026, 2, 2), date(2026, 2, 27))
    assert plan.fetchable and plan.sessions_needed == len(needed)
    assert plan.sessions_missing == needed
    assert plan.ranges == [(needed[0], needed[-1])]
    assert plan.requests == 2  # 26 calendar days at 25 days per minute-data request
    assert plan.estimated_bars == len(needed) * 75
    assert plan.estimated_seconds > 0


def test_get_bars_downloads_once_then_serves_from_the_store(ctx):
    broker = FakeBroker()
    service = _service(ctx, broker)
    df, plan, report = service.get_bars(KEY, "5m", date(2026, 2, 2), date(2026, 2, 27))
    assert len(broker.calls) == 1 and report.fetched_bars == len(df) > 0
    assert set(df["timestamp"].dt.date) == set(plan.sessions_missing)

    again, plan2, report2 = service.get_bars(KEY, "5m", date(2026, 2, 2), date(2026, 2, 27))
    assert plan2.complete and report2.fetched_bars == 0
    assert len(broker.calls) == 1  # no second download
    assert len(again) == len(df)


def test_only_the_hole_is_fetched(ctx, store):
    days = cal.trading_days(date(2026, 1, 1), date(2026, 2, 27))
    hole = [d for d in days if date(2026, 1, 19) <= d <= date(2026, 1, 23)]
    for d in days:
        if d not in hole:
            store.write(KEY, "5m", generate_ohlcv("NIFTY", d, d, "5m", seed=1), "upstox")
    broker = FakeBroker()
    _service(ctx, broker).get_bars(KEY, "5m", date(2026, 1, 1), date(2026, 2, 27))
    assert broker.calls == [("5m", hole[0], hole[-1])]


def test_sessions_without_data_are_remembered_and_not_asked_again(ctx):
    holiday = date(2026, 2, 16)  # a weekday the static calendar treats as trading
    assert holiday in cal.trading_days(holiday, holiday)
    broker = FakeBroker(no_data={holiday})
    service = _service(ctx, broker)
    _, _, report = service.get_bars(KEY, "5m", date(2026, 2, 9), date(2026, 2, 20))
    assert report.no_data_sessions == [holiday]
    assert service.plan(KEY, "5m", date(2026, 2, 9), date(2026, 2, 20)).complete


def test_failed_window_is_not_remembered_as_no_data(ctx):
    bad = date(2026, 2, 18)
    broker = FakeBroker(fail={bad})
    service = _service(ctx, broker)
    _, _, report = service.get_bars(KEY, "5m", date(2026, 2, 9), date(2026, 2, 20))
    assert bad not in report.no_data_sessions
    assert service.plan(KEY, "5m", date(2026, 2, 9), date(2026, 2, 20)).sessions_missing == [bad]


def test_a_short_recent_session_is_fetched_again(ctx, store):
    days = cal.trading_days(date(2026, 3, 2), TODAY)
    for d in days:
        bars = generate_ohlcv("NIFTY", d, d, "5m", seed=2)
        if d == days[-2]:
            bars = bars[bars["timestamp"].dt.time < time(12, 0)]  # stored mid-day
        store.write(KEY, "5m", bars, "upstox")
    plan = _service(ctx, FakeBroker()).plan(KEY, "5m", date(2026, 3, 2), TODAY)
    assert plan.sessions_missing == [days[-2]]


def test_today_is_never_remembered_as_no_data(ctx):
    during_market = datetime.combine(TODAY, time(11, 0), tzinfo=IST)
    broker = FakeBroker(no_data={TODAY})
    _, _, report = _service(ctx, broker, now=during_market).get_bars(
        KEY, "5m", date(2026, 3, 9), TODAY)
    assert TODAY not in report.no_data_sessions


def test_local_only_series_are_never_fetched(ctx):
    broker = FakeBroker()
    plan = _service(ctx, broker).plan("SIM|LOCAL", "5m", date(2026, 2, 2), date(2026, 2, 27))
    assert not plan.fetchable and plan.ranges == [] and "local-only" in plan.note
    _service(ctx, broker).get_bars("SIM|LOCAL", "5m", date(2026, 2, 2), date(2026, 2, 27))
    assert broker.calls == []


def test_a_missing_token_returns_what_is_stored_and_says_why(ctx, store):
    d = date(2026, 2, 2)
    store.write(KEY, "5m", generate_ohlcv("NIFTY", d, d, "5m", seed=3), "upstox")
    broker = FakeBroker(error=RuntimeError("No valid Upstox access token"))
    df, _, report = _service(ctx, broker).get_bars(KEY, "5m", d, date(2026, 2, 27))
    assert len(df) == 75
    assert report.errors and "token" in report.errors[0]


def test_derivation_downloads_only_one_minute_bars(ctx):
    preferences.set("derive_intraday_from_1m", True, ctx)
    broker = FakeBroker()
    service = _service(ctx, broker)
    assert service.fetch_timeframe(KEY, "15m") == "1m"
    assert service.fetch_timeframe("SIM|LOCAL", "15m") == "15m"  # local series as stored
    assert service.fetch_timeframe(KEY, "1d") == "1d"

    d = date(2026, 2, 2)
    df, plan, _ = service.get_bars(KEY, "15m", d, d)
    assert plan.fetch_timeframe == "1m" and {c[0] for c in broker.calls} == {"1m"}
    assert len(df) == 25  # 375 one-minute bars -> 25 fifteen-minute bars
    assert df["timestamp"].iloc[0].time() == time(9, 15)


def test_progress_is_reported(ctx):
    seen = []
    _service(ctx, FakeBroker()).get_bars(KEY, "5m", date(2026, 2, 2), date(2026, 2, 27),
                                         on_progress=lambda d, t: seen.append((d, t)))
    assert seen and seen[-1][0] == seen[-1][1]


# ---------------------------------------------------------------------------
# Migration from the single-file layout
# ---------------------------------------------------------------------------

def test_legacy_candle_files_move_into_the_partitioned_store(ctx):
    from backend.data import store as legacy

    legacy.close_db()
    df = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 2, 28), "5m", seed=9)
    old = ctx.data_dir / "parquet" / "candles" / "5m" / "NSE_INDEX__Test_Index.parquet"
    old.parent.mkdir(parents=True)
    df.to_parquet(old, index=False)
    legacy.get_db().execute(
        "INSERT INTO coverage VALUES (?, '5m', now(), now(), ?, now(), 'upstox')", [KEY, len(df)])

    try:
        assert legacy.migrate_legacy_candles() == 1
        assert not old.exists()
        assert (ctx.data_dir / "parquet" / "legacy-migrated" / "5m" / old.name).exists()
        back = legacy.read_candles(KEY, "5m")
        assert len(back) == len(df)
        assert back["close"].tolist() == pytest.approx(df["close"].tolist())
        assert legacy.migrate_legacy_candles() == 0  # idempotent
    finally:
        legacy.close_db()


def test_data_plan_endpoint(ctx):
    from fastapi.testclient import TestClient

    from backend.api.main import app

    client = TestClient(app, base_url="http://localhost")
    body = client.get("/api/data/plan", params={"symbol": "SIM|NOPE", "timeframe": "5m",
                                                "start": "2026-02-02", "end": "2026-02-27"}).json()
    assert body["plan"]["fetchable"] is False
    assert body["plan"]["sessions_missing"] == body["plan"]["sessions_needed"] > 0
