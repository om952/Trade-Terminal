"""Phase 1 gate tests. None of these need network access or a broker token.

The properties asserted here are the ones later phases silently depend on:
storage is idempotent, OHLC invariants hold, timestamps stay in IST, gaps are
detected rather than ignored, and the simulator is reproducible.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from backend.core.config import IST
from backend.data.calendar import find_gaps, is_trading_day, trading_days
from backend.data.csv_import import normalize_frame, validate_ohlc
from backend.data.simulator import (
    PROFILES,
    black_scholes,
    build_option_series,
    generate_ohlcv,
    implied_volatility,
    weekly_expiries,
)
from backend.data.upstox_client import Timeframe


# --------------------------------------------------------------------------
# Timeframe parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,unit,interval,label",
    [
        ("1m", "minutes", 1, "1m"),
        ("5m", "minutes", 5, "5m"),
        ("15m", "minutes", 15, "15m"),
        ("1h", "hours", 1, "1h"),
        ("1d", "days", 1, "1d"),
        ("1w", "weeks", 1, "1w"),
    ],
)
def test_timeframe_parse(text, unit, interval, label):
    tf = Timeframe.parse(text)
    assert (tf.unit, tf.interval, tf.label) == (unit, interval, label)


def test_timeframe_rejects_garbage():
    with pytest.raises(ValueError):
        Timeframe.parse("5x")


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------

def test_weekends_are_not_trading_days():
    assert not is_trading_day(date(2026, 8, 15))  # Saturday
    assert not is_trading_day(date(2026, 8, 16))  # Sunday


def test_republic_day_is_a_holiday():
    assert not is_trading_day(date(2026, 1, 26))


def test_trading_days_excludes_weekends_and_holidays():
    days = trading_days(date(2026, 1, 19), date(2026, 1, 30))
    assert date(2026, 1, 26) not in days       # Republic Day
    assert date(2026, 1, 24) not in days       # Saturday
    assert date(2026, 1, 20) in days
    assert all(d.weekday() < 5 for d in days)


# --------------------------------------------------------------------------
# Simulator
# --------------------------------------------------------------------------

def test_simulator_is_reproducible():
    """Same seed must give byte-identical output, or regression tests are worthless."""
    a = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 2, 1), "5m", seed=7)
    b = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 2, 1), "5m", seed=7)
    pd.testing.assert_frame_equal(a, b)


def test_simulator_different_seeds_differ():
    a = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 2, 1), "5m", seed=1)
    b = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 2, 1), "5m", seed=2)
    assert not a["close"].equals(b["close"])


def test_simulator_ohlc_invariants():
    """high >= max(open, close) and low <= min(open, close), always."""
    df = generate_ohlcv("BANKNIFTY", date(2026, 1, 1), date(2026, 3, 1), "5m", seed=3)
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1)).all()
    assert (df["high"] >= df["low"]).all()
    assert (df[["open", "high", "low", "close"]] > 0).all().all()


def test_simulator_timestamps_are_ist_and_sorted():
    df = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 2, 1), "5m", seed=5)
    assert str(df["timestamp"].dt.tz) == "Asia/Kolkata"
    assert df["timestamp"].is_monotonic_increasing
    assert not df["timestamp"].duplicated().any()


def test_simulator_only_generates_session_bars():
    """No bars on weekends, holidays, or outside market hours."""
    df = generate_ohlcv("NIFTY", date(2026, 1, 19), date(2026, 1, 31), "5m", seed=11)
    days = set(df["timestamp"].dt.date)
    assert date(2026, 1, 26) not in days                 # Republic Day
    assert all(is_trading_day(d) for d in days)
    assert df["timestamp"].dt.hour.min() >= 9


def test_simulator_volatility_multiplier_raises_realized_vol():
    calm = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 6, 1), "5m", seed=9)
    wild = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 6, 1), "5m", seed=9,
                          vol_multiplier=3.0)
    assert wild["close"].pct_change().std() > 2 * calm["close"].pct_change().std()


def test_all_profiles_generate():
    for name in PROFILES:
        df = generate_ohlcv(name, date(2026, 1, 1), date(2026, 1, 31), "1d", seed=1)
        assert len(df) > 15, f"{name} produced too few bars"
        assert (df["close"] > 0).all(), f"{name} produced non-positive prices"


# --------------------------------------------------------------------------
# Black-Scholes
# --------------------------------------------------------------------------

def test_put_call_parity():
    """C - P == S - K*exp(-rT). If this fails the pricer is wrong, full stop."""
    s, k, t, v, r = 24800.0, 24800.0, 30 / 365, 0.15, 0.065
    call = black_scholes(s, k, t, v, r, "CE").price
    put = black_scholes(s, k, t, v, r, "PE").price
    assert call - put == pytest.approx(s - k * math.exp(-r * t), abs=0.01)


def test_option_intrinsic_at_expiry():
    itm = black_scholes(25000, 24800, 0.0, 0.15, right="CE")
    assert itm.price == pytest.approx(200.0)
    otm = black_scholes(24600, 24800, 0.0, 0.15, right="CE")
    assert otm.price == pytest.approx(0.0)


def test_greeks_have_expected_signs():
    call = black_scholes(24800, 24800, 30 / 365, 0.15, right="CE")
    put = black_scholes(24800, 24800, 30 / 365, 0.15, right="PE")

    assert 0 < call.delta < 1
    assert -1 < put.delta < 0
    assert call.gamma > 0 and put.gamma > 0     # long options are long gamma
    assert call.theta < 0 and put.theta < 0     # long options bleed time value
    assert call.vega > 0 and put.vega > 0


def test_atm_call_delta_near_half():
    g = black_scholes(24800, 24800, 7 / 365, 0.13)
    assert 0.50 < g.delta < 0.58  # slightly above 0.5 from positive carry


def test_implied_vol_roundtrip():
    """Price at a known vol, solve for it, and get the same number back."""
    for vol in (0.08, 0.15, 0.30, 0.60):
        price = black_scholes(24800, 25000, 21 / 365, vol).price
        recovered = implied_volatility(price, 24800, 25000, 21 / 365)
        assert recovered == pytest.approx(vol, abs=1e-3)


def test_option_series_matches_underlying_length():
    und = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 1, 31), "1d", seed=4)
    opt = build_option_series(und, 24800, date(2026, 2, 26), "CE", 0.13)
    assert len(opt) == len(und)
    assert (opt["high"] >= opt["low"]).all()
    assert (opt["close"] >= 0).all()


def test_weekly_expiries_roll_off_holidays():
    exp = weekly_expiries(date(2026, 1, 1), date(2026, 3, 31))
    assert all(is_trading_day(d) for d in exp)
    assert len(exp) > 8


# --------------------------------------------------------------------------
# CSV normalization
# --------------------------------------------------------------------------

def test_csv_alias_mapping():
    raw = pd.DataFrame({
        "Date": ["2026-01-01 09:15:00", "2026-01-01 09:20:00"],
        "Open": [100.0, 101.0], "High": [102.0, 103.0],
        "Low": [99.0, 100.5], "Adj Close": [101.0, 102.5],
        "Volume": [1000, 1200],
    })
    df = normalize_frame(raw)
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume", "oi"]
    assert df["close"].iloc[0] == 101.0
    assert str(df["timestamp"].dt.tz) == "Asia/Kolkata"


def test_csv_missing_column_raises_with_context():
    raw = pd.DataFrame({"Date": ["2026-01-01"], "Open": [100.0]})
    with pytest.raises(ValueError, match="missing required column"):
        normalize_frame(raw)


def test_csv_naive_timestamps_treated_as_ist():
    """A naive Indian timestamp must not be read as UTC — that shifts 5.5 hours."""
    raw = pd.DataFrame({
        "date": ["2026-01-01 09:15:00"],
        "open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5], "volume": [10],
    })
    df = normalize_frame(raw, tz_naive_is_ist=True)
    assert df["timestamp"].iloc[0].hour == 9
    assert df["timestamp"].iloc[0].minute == 15


def test_validate_ohlc_catches_impossible_bars():
    bad = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-01-01 09:15", "2026-01-01 09:20"]).tz_localize(IST),
        "open": [100.0, 100.0], "high": [99.0, 105.0],   # first high < open
        "low": [98.0, 99.0], "close": [100.0, 104.0],
        "volume": [1, 1], "oi": [0, 0],
    })
    problems = validate_ohlc(bad)
    assert any("high <" in p for p in problems)


def test_validate_ohlc_passes_clean_data():
    df = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 1, 31), "1d", seed=6)
    assert validate_ohlc(df) == []


# --------------------------------------------------------------------------
# Gap detection
# --------------------------------------------------------------------------

def test_gap_detection_finds_removed_session():
    df = generate_ohlcv("NIFTY", date(2026, 2, 2), date(2026, 2, 27), "1d", seed=8)
    missing_day = df["timestamp"].dt.date.iloc[5]
    holed = df[df["timestamp"].dt.date != missing_day]

    report = find_gaps(holed, "TEST", "1d")
    assert missing_day in report.missing_sessions
    assert not report.is_clean


def test_gap_detection_clean_on_complete_data():
    df = generate_ohlcv("NIFTY", date(2026, 2, 2), date(2026, 2, 27), "1d", seed=8)
    report = find_gaps(df, "TEST", "1d")
    assert report.is_clean
    assert report.coverage_pct == 100.0


def test_gap_detection_flags_thin_sessions():
    """A day with only a handful of 5m bars is a partial fetch, not a session."""
    df = generate_ohlcv("NIFTY", date(2026, 2, 2), date(2026, 2, 13), "5m", seed=8)
    target = df["timestamp"].dt.date.iloc[0]
    mask = df["timestamp"].dt.date == target
    truncated = pd.concat([df[mask].head(5), df[~mask]])

    report = find_gaps(truncated, "TEST", "5m")
    assert any(d == target for d, _, _ in report.thin_sessions)


# --------------------------------------------------------------------------
# Store round-trip (uses a temp data dir via monkeypatch)
# --------------------------------------------------------------------------

@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    from backend.core.config import settings as s
    import backend.data.store as store

    monkeypatch.setattr(s, "data_dir", tmp_path)
    store.close_db()
    yield store
    store.close_db()


def test_store_roundtrip(temp_store):
    df = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 1, 31), "1d", seed=2)
    temp_store.write_candles("TEST|X", "1d", df)

    back = temp_store.read_candles("TEST|X", "1d")
    assert len(back) == len(df)
    assert back["close"].tolist() == pytest.approx(df["close"].tolist())
    assert str(back["timestamp"].dt.tz) == "Asia/Kolkata"


def test_store_write_is_idempotent(temp_store):
    """Writing the same data twice must not duplicate rows."""
    df = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 1, 31), "1d", seed=2)
    first = temp_store.write_candles("TEST|Y", "1d", df)
    second = temp_store.write_candles("TEST|Y", "1d", df)
    assert first == second == len(df)


def test_store_merge_extends_range(temp_store):
    jan = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 1, 31), "1d", seed=2)
    feb = generate_ohlcv("NIFTY", date(2026, 2, 1), date(2026, 2, 28), "1d", seed=3)

    temp_store.write_candles("TEST|Z", "1d", jan)
    total = temp_store.write_candles("TEST|Z", "1d", feb)

    assert total == len(jan) + len(feb)
    back = temp_store.read_candles("TEST|Z", "1d")
    assert back["timestamp"].is_monotonic_increasing


def test_store_overlapping_write_takes_latest(temp_store):
    """A re-fetch of an overlapping range is a correction, not a duplicate."""
    df = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 1, 31), "1d", seed=2)
    temp_store.write_candles("TEST|W", "1d", df)

    revised = df.copy()
    revised["close"] = revised["close"] + 100.0
    temp_store.write_candles("TEST|W", "1d", revised)

    back = temp_store.read_candles("TEST|W", "1d")
    assert len(back) == len(df)
    assert back["close"].iloc[0] == pytest.approx(df["close"].iloc[0] + 100.0)


def test_store_date_range_filter(temp_store):
    df = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 3, 31), "1d", seed=2)
    temp_store.write_candles("TEST|R", "1d", df)

    subset = temp_store.read_candles("TEST|R", "1d", start=date(2026, 2, 1), end=date(2026, 2, 28))
    assert all(d.month == 2 for d in subset["timestamp"].dt.date)
    assert len(subset) < len(df)


def test_store_unknown_instrument_returns_empty(temp_store):
    assert temp_store.read_candles("NOPE|NOPE", "1d").empty


def test_store_crash_mid_write_keeps_old_file(temp_store, monkeypatch):
    """A write that dies halfway must leave the previous file intact and no debris."""
    jan = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 1, 31), "1d", seed=2)
    feb = generate_ohlcv("NIFTY", date(2026, 2, 1), date(2026, 2, 28), "1d", seed=3)
    temp_store.write_candles("TEST|CRASH", "1d", jan)
    path = temp_store.candle_path("TEST|CRASH", "1d")
    before = path.read_bytes()

    real_to_parquet = pd.DataFrame.to_parquet

    def dying_to_parquet(self, target, *args, **kwargs):
        real_to_parquet(self.iloc[:1], target, *args, **kwargs)  # partial file on disk
        raise OSError("simulated crash mid-write")

    with monkeypatch.context() as m:
        m.setattr(pd.DataFrame, "to_parquet", dying_to_parquet)
        with pytest.raises(OSError, match="simulated crash"):
            temp_store.write_candles("TEST|CRASH", "1d", feb)

    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []
    assert len(temp_store.read_candles("TEST|CRASH", "1d")) == len(jan)


def test_store_concurrent_writes_lose_nothing(temp_store):
    """Threads merging different months into one file must not drop each other's bars."""
    from concurrent.futures import ThreadPoolExecutor

    months = [
        generate_ohlcv("NIFTY", date(2026, m, 1), date(2026, m, 28), "1d", seed=m)
        for m in range(1, 9)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda df: temp_store.write_candles("TEST|CONC", "1d", df), months))

    back = temp_store.read_candles("TEST|CONC", "1d")
    assert len(back) == sum(len(m) for m in months)
    assert temp_store.read_coverage("TEST|CONC", "1d")["bar_count"] == len(back)
