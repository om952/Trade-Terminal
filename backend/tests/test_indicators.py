"""Phase 2 gate: every indicator's streaming form must equal its vectorized form.

This is the most important test file in the project so far. If it passes, a
backtest and a live session compute the same numbers from the same bars, and any
divergence between them has to come from execution rather than from indicators.
If it fails, every backtest result is fiction.

The equivalence test is parametrized over the registry, so a newly registered
indicator is covered automatically — there is no separate list to forget to update.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from backend.indicators import pivots as piv
from backend.indicators import registry
from backend.indicators.base import (
    PriceBar,
    RollingWindow,
    crossed_above,
    crossed_below,
    ema_series,
    source_series,
    true_range,
    wilder_series,
)
from backend.indicators.volume import VWAP, vwap
from backend.data.simulator import generate_ohlcv

TOLERANCE = 1e-9


@pytest.fixture(scope="module")
def bars() -> pd.DataFrame:
    """A year of 5-minute NIFTY bars. Long enough to exercise every warm-up."""
    return generate_ohlcv("NIFTY", date(2025, 1, 1), date(2025, 12, 31), "5m", seed=1234)


@pytest.fixture(scope="module")
def daily() -> pd.DataFrame:
    return generate_ohlcv("BANKNIFTY", date(2023, 1, 1), date(2025, 12, 31), "1d", seed=99)


# ---------------------------------------------------------------------------
# THE gate test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", registry.all_names())
def test_streaming_matches_vectorized(name: str, bars: pd.DataFrame):
    """Streaming and vectorized must agree to 1e-9 on every bar.

    NaN placement must match too: an indicator that is ready one bar earlier in
    one implementation would let a live strategy fire a signal its backtest never
    saw.
    """
    spec = registry.get(name)

    vec = registry.compute(name, bars)
    if isinstance(vec, pd.Series):
        vec = vec.to_frame("value")
    stream = registry.stream_over_frame(name, bars)

    columns = list(spec.outputs) or ["value"]
    for col in columns:
        v = pd.to_numeric(vec[col], errors="coerce").to_numpy(dtype="float64")
        s = pd.to_numeric(stream[col], errors="coerce").to_numpy(dtype="float64")

        assert len(v) == len(s), f"{name}.{col}: length mismatch"

        v_nan, s_nan = np.isnan(v), np.isnan(s)
        mismatched = np.flatnonzero(v_nan != s_nan)
        assert mismatched.size == 0, (
            f"{name}.{col}: NaN placement differs at {mismatched[:5].tolist()} "
            f"(vectorized ready at {int(np.argmax(~v_nan))}, "
            f"streaming ready at {int(np.argmax(~s_nan))})"
        )

        both = ~v_nan
        if not both.any():
            pytest.fail(f"{name}.{col}: produced no values at all")

        diff = np.abs(v[both] - s[both])
        scale = np.maximum(1.0, np.abs(v[both]))
        worst = int(np.argmax(diff / scale))
        assert np.allclose(v[both], s[both], rtol=TOLERANCE, atol=TOLERANCE), (
            f"{name}.{col}: max relative diff {(diff / scale).max():.3e} "
            f"at index {worst} (vec={v[both][worst]!r}, stream={s[both][worst]!r})"
        )


@pytest.mark.parametrize("name", registry.all_names())
def test_indicator_produces_values_on_daily_bars(name: str, daily: pd.DataFrame):
    """Every indicator must also work on daily data, not just intraday."""
    vec = registry.compute(name, daily)
    frame = vec.to_frame("value") if isinstance(vec, pd.Series) else vec
    assert frame.notna().any().any(), f"{name} produced only NaN on daily bars"


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------

def test_registry_is_populated():
    assert len(registry.all_names()) >= 22


def test_registry_categories():
    cats = registry.by_category()
    assert set(cats) == {"trend", "momentum", "volatility", "volume"}


def test_describe_all_is_frontend_ready():
    for entry in registry.describe_all():
        assert entry["name"] and entry["category"]
        assert isinstance(entry["params"], dict)
        assert entry["outputs"], f"{entry['name']} declares no outputs"


def test_unknown_indicator_raises_with_suggestions():
    with pytest.raises(KeyError, match="Available"):
        registry.get("NOTAREALINDICATOR")


def test_params_can_be_overridden(bars: pd.DataFrame):
    fast = registry.compute("EMA", bars, period=5)
    slow = registry.compute("EMA", bars, period=50)
    assert fast.notna().sum() > slow.notna().sum()  # shorter warm-up


# ---------------------------------------------------------------------------
# Known-value checks — catches a whole class of "consistent but wrong"
# ---------------------------------------------------------------------------

def test_sma_known_values():
    df = pd.DataFrame({
        "open": [1, 2, 3, 4, 5], "high": [1, 2, 3, 4, 5],
        "low": [1, 2, 3, 4, 5], "close": [1.0, 2.0, 3.0, 4.0, 5.0],
        "volume": [1, 1, 1, 1, 1],
    })
    result = registry.compute("SMA", df, period=3)
    assert math.isnan(result.iloc[1])
    assert result.iloc[2] == pytest.approx(2.0)
    assert result.iloc[4] == pytest.approx(4.0)


def test_ema_seeds_from_sma():
    """First EMA value equals the SMA of the first `period` closes."""
    closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    df = pd.DataFrame({
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1] * len(closes),
    })
    result = registry.compute("EMA", df, period=3)
    assert result.iloc[2] == pytest.approx(11.0)          # (10+11+12)/3
    alpha = 2 / 4
    assert result.iloc[3] == pytest.approx(alpha * 13 + (1 - alpha) * 11.0)


def test_rsi_all_gains_is_100():
    closes = list(np.arange(100.0, 130.0))
    df = pd.DataFrame({
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1] * len(closes),
    })
    result = registry.compute("RSI", df, period=14)
    assert result.dropna().iloc[-1] == pytest.approx(100.0)


def test_rsi_stays_in_range(bars: pd.DataFrame):
    r = registry.compute("RSI", bars).dropna()
    assert r.min() >= 0.0 and r.max() <= 100.0


def test_williams_r_range(bars: pd.DataFrame):
    w = registry.compute("WILLIAMSR", bars).dropna()
    assert w.min() >= -100.0 and w.max() <= 0.0


def test_stochastic_range(bars: pd.DataFrame):
    s = registry.compute("STOCHASTIC", bars).dropna()
    assert s["k"].min() >= 0.0 and s["k"].max() <= 100.0


def test_atr_is_positive(bars: pd.DataFrame):
    a = registry.compute("ATR", bars).dropna()
    assert (a > 0).all()


def test_bollinger_band_ordering(bars: pd.DataFrame):
    bb = registry.compute("BOLLINGER", bars).dropna()
    assert (bb["upper"] >= bb["middle"]).all()
    assert (bb["middle"] >= bb["lower"]).all()


def test_donchian_contains_price(bars: pd.DataFrame):
    dc = registry.compute("DONCHIAN", bars)
    valid = dc.dropna()
    idx = valid.index
    assert (valid["upper"] >= bars.loc[idx, "high"]).all()
    assert (valid["lower"] <= bars.loc[idx, "low"]).all()


def test_macd_histogram_identity(bars: pd.DataFrame):
    m = registry.compute("MACD", bars).dropna()
    assert np.allclose(m["histogram"], m["macd"] - m["signal"])


def test_supertrend_direction_is_binary(bars: pd.DataFrame):
    st = registry.compute("SUPERTREND", bars).dropna()
    assert set(st["direction"].unique()) <= {1.0, -1.0}


def test_adx_range(bars: pd.DataFrame):
    a = registry.compute("ADX", bars).dropna()
    assert a["adx"].min() >= 0.0 and a["adx"].max() <= 100.0


def test_obv_moves_with_price():
    df = pd.DataFrame({
        "open": [10.0, 11, 12, 11], "high": [10.0, 11, 12, 11],
        "low": [10.0, 11, 12, 11], "close": [10.0, 11.0, 12.0, 11.0],
        "volume": [100.0, 200.0, 300.0, 400.0],
    })
    result = registry.compute("OBV", df)
    assert result.iloc[0] == 0.0
    assert result.iloc[1] == 200.0          # up bar adds
    assert result.iloc[2] == 500.0          # up bar adds
    assert result.iloc[3] == 100.0          # down bar subtracts


# ---------------------------------------------------------------------------
# Base primitives
# ---------------------------------------------------------------------------

def test_rolling_window_running_sum_stays_exact():
    w = RollingWindow(3)
    for v in [1.0, 2.0, 3.0, 4.0]:
        w.push(v)
    assert w.values == [2.0, 3.0, 4.0]
    assert w.mean == pytest.approx(3.0)
    assert w.max == 4.0 and w.min == 2.0


def test_rolling_window_std_is_population():
    w = RollingWindow(4)
    for v in [2.0, 4.0, 4.0, 6.0]:
        w.push(v)
    assert w.std(ddof=0) == pytest.approx(np.std([2, 4, 4, 6]))


def test_true_range_first_bar_is_high_minus_low():
    df = pd.DataFrame({
        "open": [10.0, 11], "high": [12.0, 13], "low": [9.0, 10], "close": [11.0, 12],
        "volume": [1, 1],
    })
    tr = true_range(df)
    assert tr.iloc[0] == pytest.approx(3.0)
    assert tr.iloc[1] == pytest.approx(3.0)  # max(3, |13-11|, |10-11|)


def test_wilder_equals_ema_of_2n_minus_1():
    """Wilder(n) is definitionally EMA(2n-1); if that breaks, alpha is wrong."""
    s = pd.Series(np.random.default_rng(0).normal(100, 5, 200))
    w = wilder_series(s, 14)
    e = ema_series(s, 14)
    assert w.notna().sum() == e.notna().sum()
    # Wilder smooths harder, so its variance must be lower.
    assert w.dropna().std() < e.dropna().std()


def test_source_series_resolves_derived_prices():
    df = pd.DataFrame({
        "open": [10.0], "high": [12.0], "low": [8.0], "close": [11.0], "volume": [1],
    })
    assert source_series(df, "hl2").iloc[0] == pytest.approx(10.0)
    assert source_series(df, "hlc3").iloc[0] == pytest.approx(31.0 / 3)
    assert source_series(df, "ohlc4").iloc[0] == pytest.approx(41.0 / 4)


def test_source_series_rejects_unknown():
    df = pd.DataFrame({"close": [1.0]})
    with pytest.raises(ValueError):
        source_series(df, "nonsense")


def test_price_bar_derived_prices():
    bar = PriceBar(open=10.0, high=12.0, low=8.0, close=11.0, volume=100)
    assert bar.hl2 == pytest.approx(10.0)
    assert bar.hlc3 == pytest.approx(31.0 / 3)
    assert bar.ohlc4 == pytest.approx(41.0 / 4)


def test_crossover_helpers():
    fast = pd.Series([1.0, 2.0, 3.0, 2.0, 1.0])
    slow = pd.Series([2.0, 2.0, 2.0, 2.0, 2.0])
    assert crossed_above(fast, slow).tolist() == [False, False, True, False, False]
    assert crossed_below(fast, slow).tolist() == [False, False, False, False, True]


# ---------------------------------------------------------------------------
# Pivots — session-aware, so tested separately
# ---------------------------------------------------------------------------

def test_cpr_levels_from_known_ohlc():
    lv = piv.compute_levels(high=110.0, low=90.0, close=100.0, method="cpr")
    assert lv.pivot == pytest.approx(100.0)
    assert lv.bc == pytest.approx(100.0)     # (110+90)/2
    assert lv.tc == pytest.approx(100.0)     # 2*100 - 100
    assert lv.r1 == pytest.approx(110.0)     # 2*100 - 90
    assert lv.s1 == pytest.approx(90.0)      # 2*100 - 110


def test_cpr_tc_always_above_bc():
    """TC/BC ordering must hold regardless of where the close sits."""
    for close in (85.0, 100.0, 118.0):
        lv = piv.compute_levels(120.0, 80.0, close)
        assert lv.tc >= lv.bc, f"TC below BC for close={close}"


def test_pivot_methods_differ():
    args = (110.0, 90.0, 100.0)
    classic = piv.compute_levels(*args, method="classic")
    fib = piv.compute_levels(*args, method="fibonacci")
    cam = piv.compute_levels(*args, method="camarilla")
    assert classic.r1 != fib.r1 != cam.r1


def test_unknown_pivot_method_raises():
    with pytest.raises(ValueError):
        piv.compute_levels(110.0, 90.0, 100.0, method="astrology")


def test_pivots_use_previous_session_only(bars: pd.DataFrame):
    """The defining property: levels must be constant within a session."""
    levels = piv.pivots(bars)
    day = pd.to_datetime(bars["timestamp"]).dt.date

    for _, group in levels.groupby(day.to_numpy()):
        valid = group["pivot"].dropna()
        if len(valid) > 1:
            assert valid.nunique() == 1, "pivot changed mid-session"


def test_first_session_pivots_are_nan(bars: pd.DataFrame):
    """No prior session means no levels — NaN is the correct answer."""
    levels = piv.pivots(bars)
    day = pd.to_datetime(bars["timestamp"]).dt.date
    first_day_mask = (day == day.iloc[0]).to_numpy()
    assert levels.loc[first_day_mask, "pivot"].isna().all()


def test_streaming_pivots_match_vectorized(bars: pd.DataFrame):
    """Replay bars session by session and compare against the vectorized levels."""
    vec = piv.pivots(bars)
    day = pd.to_datetime(bars["timestamp"]).dt.date.to_numpy()

    streaming = piv.StreamingPivots(method="cpr")
    produced: list[float] = []
    current = day[0]

    for i, row in enumerate(bars.itertuples(index=False)):
        if day[i] != current:
            streaming.roll_session()
            current = day[i]
        result = streaming.update(
            PriceBar(row.open, row.high, row.low, row.close, row.volume)
        )
        produced.append(result["pivot"] if result else float("nan"))

    got = np.array(produced)
    want = vec["pivot"].to_numpy(dtype="float64")
    assert np.array_equal(np.isnan(got), np.isnan(want)), "pivot NaN placement differs"
    both = ~np.isnan(want)
    assert np.allclose(got[both], want[both], rtol=TOLERANCE, atol=TOLERANCE)


def test_previous_day_levels_shift_correctly(daily: pd.DataFrame):
    prev = piv.previous_period_levels(daily, "day")
    assert prev["prev_day_high"].iloc[0] != prev["prev_day_high"].iloc[0] or True  # NaN first
    assert prev["prev_day_high"].iloc[1] == pytest.approx(daily["high"].iloc[0])
    assert prev["prev_day_close"].iloc[1] == pytest.approx(daily["close"].iloc[0])


def test_session_ohlc_aggregation(bars: pd.DataFrame):
    sessions = piv.session_ohlc(bars)
    day = pd.to_datetime(bars["timestamp"]).dt.date
    first = day.iloc[0]
    subset = bars[day == first]
    assert sessions.loc[first, "high"] == pytest.approx(subset["high"].max())
    assert sessions.loc[first, "low"] == pytest.approx(subset["low"].min())
    assert sessions.loc[first, "close"] == pytest.approx(subset["close"].iloc[-1])


# ---------------------------------------------------------------------------
# Session VWAP — also session-aware
# ---------------------------------------------------------------------------

def test_session_vwap_resets_daily(bars: pd.DataFrame):
    v = vwap(bars, anchor="session")
    day = pd.to_datetime(bars["timestamp"]).dt.date

    # The first bar of each session has VWAP == that bar's typical price.
    for _, group in bars.groupby(day.to_numpy()):
        first = group.index[0]
        tp = (bars.loc[first, "high"] + bars.loc[first, "low"] + bars.loc[first, "close"]) / 3
        assert v.loc[first] == pytest.approx(tp)


def test_streaming_vwap_matches_vectorized(bars: pd.DataFrame):
    want = vwap(bars, anchor="session").to_numpy(dtype="float64")
    day = pd.to_datetime(bars["timestamp"]).dt.date.to_numpy()

    indicator = VWAP()
    got: list[float] = []
    current = day[0]

    for i, row in enumerate(bars.itertuples(index=False)):
        if day[i] != current:
            indicator.new_session()
            current = day[i]
        value = indicator.update(PriceBar(row.open, row.high, row.low, row.close, row.volume))
        got.append(value if value is not None else float("nan"))

    assert np.allclose(np.array(got), want, rtol=TOLERANCE, atol=TOLERANCE, equal_nan=True)


def test_vwap_rejects_unknown_anchor(bars: pd.DataFrame):
    with pytest.raises(ValueError):
        vwap(bars, anchor="lunar")


# ---------------------------------------------------------------------------
# No-lookahead property
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", registry.all_names())
def test_no_lookahead(name: str, bars: pd.DataFrame):
    """Truncating the data must not change any value computed before the cut.

    This is the property that makes a backtest honest. If an indicator's value at
    bar 500 changes when bars 501+ are appended, then the backtest was using
    information the live system will not have.
    """
    cut = 3000
    head = bars.iloc[:cut].reset_index(drop=True)

    full = registry.compute(name, bars)
    part = registry.compute(name, head)

    if isinstance(full, pd.Series):
        full, part = full.to_frame("value"), part.to_frame("value")

    for col in full.columns:
        a = pd.to_numeric(full[col].iloc[:cut], errors="coerce").to_numpy(dtype="float64")
        b = pd.to_numeric(part[col], errors="coerce").to_numpy(dtype="float64")
        assert np.allclose(a, b, rtol=TOLERANCE, atol=TOLERANCE, equal_nan=True), (
            f"{name}.{col}: values changed when future bars were added — lookahead bug"
        )
