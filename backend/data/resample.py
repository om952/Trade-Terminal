"""Build intraday bars of any size from 1-minute bars.

Bars are bucketed from the session open (09:15 for NSE and BSE, 09:00 for MCX),
the way the exchange's own candles are: the first 5-minute bar of a day is
09:15 to 09:20, the first hourly bar 09:15 to 10:15, and the last bar of the day
is whatever is left before the close. Each bar is stamped with its start time.

    open = first open, high = max, low = min, close = last close,
    volume = sum, oi = last

Whether bars built this way match the broker's native candles exactly is a
fact about the broker's data, not something this module can promise. Check it
on real data with `python -m backend.data.ingest verify-derivation`, and only
then turn on `derive_intraday_from_1m`.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from backend.core.config import IST
from backend.data.calendar import MARKET_OPEN, MCX_OPEN
from backend.data.upstox_client import Timeframe


def bar_minutes(timeframe: str) -> int:
    tf = Timeframe.parse(timeframe)
    if tf.unit == "minutes":
        return tf.interval
    if tf.unit == "hours":
        return tf.interval * 60
    raise ValueError(f"{timeframe} is not an intraday timeframe")


def is_intraday(timeframe: str) -> bool:
    return Timeframe.parse(timeframe).unit in ("minutes", "hours")


def derive(df_1m: pd.DataFrame, timeframe: str, mcx: bool = False) -> pd.DataFrame:
    """Aggregate canonical 1-minute candles into `timeframe` candles."""
    minutes = bar_minutes(timeframe)
    if df_1m.empty or minutes == 1:
        return df_1m.copy().reset_index(drop=True)

    df = df_1m.sort_values("timestamp", kind="stable").reset_index(drop=True)
    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(IST)
    open_time = MCX_OPEN if mcx else MARKET_OPEN

    day = ts.dt.normalize()
    session_open = day + pd.Timedelta(hours=open_time.hour, minutes=open_time.minute)
    offset = (ts - session_open) // pd.Timedelta(minutes=1)
    # Bars before the open (rare pre-open prints) fall into the first bucket.
    bucket = session_open + pd.to_timedelta((offset.clip(lower=0) // minutes) * minutes, unit="m")

    grouped = df.assign(_bucket=bucket).groupby("_bucket", sort=True)
    out = pd.DataFrame({
        "timestamp": grouped["timestamp"].size().index,
        "open": grouped["open"].first().to_numpy(),
        "high": grouped["high"].max().to_numpy(),
        "low": grouped["low"].min().to_numpy(),
        "close": grouped["close"].last().to_numpy(),
        "volume": grouped["volume"].sum().to_numpy(),
        "oi": grouped["oi"].last().to_numpy() if "oi" in df.columns else 0,
    })
    out["volume"] = out["volume"].astype("int64")
    out["oi"] = pd.Series(out["oi"]).fillna(0).astype("int64")
    return out.reset_index(drop=True)


def compare(derived: pd.DataFrame, native: pd.DataFrame, tolerance: float = 1e-6) -> dict:
    """How derived bars differ from the broker's native bars over their common span.

    Returns counts of bars only in one side and of bars whose OHLC or volume
    differ, plus a few examples. A clean result is the evidence needed before
    trusting derived bars for backtests that must match paper trading.
    """
    def keyed(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True).dt.tz_convert(IST)
        return out.set_index("timestamp")

    a, b = keyed(derived), keyed(native)
    if not a.empty and not b.empty:
        start, end = max(a.index.min(), b.index.min()), min(a.index.max(), b.index.max())
        a, b = a[(a.index >= start) & (a.index <= end)], b[(b.index >= start) & (b.index <= end)]

    only_derived = a.index.difference(b.index)
    only_native = b.index.difference(a.index)
    common = a.index.intersection(b.index)

    bad = pd.Series(False, index=common)
    examples: list[tuple[datetime, str, float, float]] = []
    for column in ("open", "high", "low", "close", "volume"):
        diff = (a.loc[common, column].astype("float64") - b.loc[common, column].astype("float64")).abs()
        over = diff > tolerance
        bad |= over
        for ts in diff[over].index[: max(0, 10 - len(examples))]:
            examples.append((ts.to_pydatetime(), column, float(a.at[ts, column]), float(b.at[ts, column])))

    mismatched = int(bad.sum())
    return {
        "bars_compared": int(len(common)),
        "only_in_derived": int(len(only_derived)),
        "only_in_native": int(len(only_native)),
        "mismatched_bars": mismatched,
        "examples": [
            {"timestamp": t.isoformat(), "field": f, "derived": d, "native": n}
            for t, f, d, n in examples
        ],
        "identical": not only_derived.size and not only_native.size and mismatched == 0,
    }
