"""Import candles from CSV — yfinance, nsepython, TradingView exports, or hand-built files.

Column names vary wildly between sources, so this normalizes rather than
demanding an exact format. What it will not do is guess silently: if a required
column is missing or timestamps are ambiguous, it raises with the columns it
actually found, because a mis-parsed date column produces a backtest that looks
fine and is wrong.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from backend.core.config import IST
from backend.data.store import write_candles

log = logging.getLogger(__name__)

# Lowercased source column -> canonical name.
ALIASES = {
    "date": "timestamp", "datetime": "timestamp", "time": "timestamp",
    "timestamp": "timestamp", "date_time": "timestamp",
    "open": "open", "o": "open",
    "high": "high", "h": "high",
    "low": "low", "l": "low",
    "close": "close", "c": "close", "adj close": "close", "adj_close": "close",
    "last": "close", "ltp": "close", "price": "close",
    "volume": "volume", "v": "volume", "vol": "volume", "qty": "volume",
    "oi": "oi", "open interest": "oi", "open_interest": "oi",
}

REQUIRED = ("timestamp", "open", "high", "low", "close")


def normalize_frame(df: pd.DataFrame, tz_naive_is_ist: bool = True) -> pd.DataFrame:
    """Rename columns to canonical names and coerce dtypes."""
    renamed = {}
    for col in df.columns:
        key = str(col).strip().lower()
        if key in ALIASES:
            renamed[col] = ALIASES[key]
    df = df.rename(columns=renamed)

    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(
            f"CSV missing required column(s): {missing}. "
            f"Found columns: {list(df.columns)}. "
            f"Recognized aliases: {sorted(set(ALIASES))}"
        )

    if "volume" not in df.columns:
        df["volume"] = 0
    if "oi" not in df.columns:
        df["oi"] = 0

    ts = pd.to_datetime(df["timestamp"], format="mixed", errors="coerce")
    if ts.isna().any():
        bad = df.loc[ts.isna(), "timestamp"].head(3).tolist()
        raise ValueError(f"Unparseable timestamps, e.g. {bad}")

    # A naive timestamp from an Indian data source is IST. Assuming UTC would
    # shift every bar by 5.5 hours and quietly move trades into the wrong session.
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(IST) if tz_naive_is_ist else ts.dt.tz_localize("UTC")
    df["timestamp"] = ts.dt.tz_convert(IST)

    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("volume", "oi"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")

    before = len(df)
    df = df.dropna(subset=["open", "high", "low", "close"])
    if len(df) < before:
        log.warning("Dropped %d rows with unparseable OHLC values", before - len(df))

    return df[["timestamp", "open", "high", "low", "close", "volume", "oi"]].reset_index(drop=True)


def validate_ohlc(df: pd.DataFrame) -> list[str]:
    """Sanity checks. Returns a list of problems — empty means clean.

    These catch the failure modes that make a backtest silently optimistic:
    a high below the close means a limit order would fill at a price that never
    traded.
    """
    problems: list[str] = []

    bad_high = df[df["high"] < df[["open", "close"]].max(axis=1)]
    if len(bad_high):
        problems.append(f"{len(bad_high)} bars where high < max(open, close)")

    bad_low = df[df["low"] > df[["open", "close"]].min(axis=1)]
    if len(bad_low):
        problems.append(f"{len(bad_low)} bars where low > min(open, close)")

    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        problems.append("non-positive prices present")

    dupes = df["timestamp"].duplicated().sum()
    if dupes:
        problems.append(f"{dupes} duplicate timestamps")

    if not df["timestamp"].is_monotonic_increasing:
        problems.append("timestamps not sorted ascending")

    return problems


def import_csv(
    path: str | Path,
    instrument_key: str,
    timeframe: str,
    tz_naive_is_ist: bool = True,
    strict: bool = True,
) -> int:
    """Load a CSV into the candle store. Returns total bars after the merge.

    `strict=True` refuses to import data that fails OHLC validation. Override it
    only when you know why the data looks the way it does.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    raw = pd.read_csv(path)
    log.info("Read %s: %d rows, columns %s", path.name, len(raw), list(raw.columns))

    df = normalize_frame(raw, tz_naive_is_ist=tz_naive_is_ist)
    df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)

    problems = validate_ohlc(df)
    if problems:
        message = f"OHLC validation failed for {path.name}: " + "; ".join(problems)
        if strict:
            raise ValueError(message + "  (pass strict=False to import anyway)")
        log.warning(message)

    total = write_candles(instrument_key, timeframe, df, source=f"csv:{path.name}")
    log.info(
        "Imported %d bars (%s..%s) into %s %s — %d total",
        len(df), df["timestamp"].iloc[0].date(), df["timestamp"].iloc[-1].date(),
        instrument_key, timeframe, total,
    )
    return total


def export_csv(instrument_key: str, timeframe: str, path: str | Path) -> int:
    """Dump stored candles back out, for inspection or sharing."""
    from backend.data.store import read_candles

    df = read_candles(instrument_key, timeframe)
    if df.empty:
        raise ValueError(f"No stored data for {instrument_key} {timeframe}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    log.info("Exported %d bars to %s", len(df), path)
    return len(df)
