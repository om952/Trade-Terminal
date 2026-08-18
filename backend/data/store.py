"""Candle storage: DuckDB for metadata and queries, Parquet for the bars themselves.

Layout:
    data/terminal.duckdb                          instruments, meta, coverage
    data/parquet/candles/{timeframe}/{key}.parquet one file per instrument+timeframe

Why not put candles in DuckDB tables: 1-minute bars across a few hundred
instruments over several years runs to hundreds of millions of rows. Parquet
keeps them columnar, compressed, and independently replaceable, and DuckDB
queries them in place at full speed. A corrupt or stale instrument is one file
to delete, not a surgical DELETE against a giant table.

Writes are idempotent. Re-fetching a range you already have produces a
byte-identical file, which is what makes the Phase 1 reproducibility gate
meaningful.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd

from backend.core.config import IST, settings

log = logging.getLogger(__name__)

CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "oi"]

_db: duckdb.DuckDBPyConnection | None = None
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    instrument_key    VARCHAR PRIMARY KEY,
    trading_symbol    VARCHAR,
    name              VARCHAR,
    exchange          VARCHAR,
    segment           VARCHAR,
    instrument_type   VARCHAR,
    lot_size          INTEGER,
    tick_size         DOUBLE,
    expiry            DATE,
    strike_price      DOUBLE,
    underlying_key    VARCHAR,
    underlying_symbol VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_inst_underlying ON instruments(underlying_symbol, expiry);
CREATE INDEX IF NOT EXISTS idx_inst_symbol     ON instruments(trading_symbol);

CREATE TABLE IF NOT EXISTS coverage (
    instrument_key VARCHAR,
    timeframe      VARCHAR,
    first_bar      TIMESTAMPTZ,
    last_bar       TIMESTAMPTZ,
    bar_count      BIGINT,
    updated_at     TIMESTAMPTZ,
    source         VARCHAR,
    PRIMARY KEY (instrument_key, timeframe)
);

CREATE TABLE IF NOT EXISTS meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR
);
"""


def get_db() -> duckdb.DuckDBPyConnection:
    """Process-wide DuckDB connection, created on first use."""
    global _db
    with _lock:
        if _db is None:
            settings.ensure_dirs()
            try:
                _db = duckdb.connect(str(settings.db_path))
            except duckdb.IOException as exc:
                # DuckDB permits a single writer process. Running the test suite
                # while the API server is up produces a lock error that surfaces
                # as a baffling fixture failure several layers up, so name it here.
                raise RuntimeError(
                    f"Cannot open {settings.db_path}: another process holds it.\n"
                    f"DuckDB allows only one writer at a time — stop the API server "
                    f"before running the tests, or vice versa:\n"
                    f"    pkill -f 'uvicorn backend.api.main'\n"
                    f"Original error: {exc}"
                ) from exc

            _db.execute("SET TimeZone='Asia/Kolkata'")
            for stmt in filter(None, (s.strip() for s in SCHEMA.split(";"))):
                _db.execute(stmt)
            log.debug("DuckDB opened at %s", settings.db_path)
        return _db


def close_db() -> None:
    global _db
    with _lock:
        if _db is not None:
            _db.close()
            _db = None


def _safe(instrument_key: str) -> str:
    """Filesystem-safe filename from an instrument key.

    'NSE_INDEX|Nifty 50' -> 'NSE_INDEX__Nifty_50'
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", instrument_key.replace("|", "__"))


def candle_path(instrument_key: str, timeframe: str) -> Path:
    d = settings.parquet_dir / "candles" / timeframe
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_safe(instrument_key)}.parquet"


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce to the canonical schema, sorted, deduplicated, tz-aware IST.

    Deduplication keeps the LAST occurrence: a re-fetch of an overlapping range
    is treated as a correction, since the exchange occasionally revises bars.
    """
    df = df.copy()

    for col in CANDLE_COLUMNS:
        if col not in df.columns:
            df[col] = 0 if col in ("volume", "oi") else pd.NA
    df = df[CANDLE_COLUMNS]

    ts = pd.to_datetime(df["timestamp"], utc=True, format="mixed")
    df["timestamp"] = ts.dt.tz_convert(IST)

    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    for col in ("volume", "oi"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")

    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.sort_values("timestamp", kind="stable")
    df = df.drop_duplicates(subset="timestamp", keep="last")
    return df.reset_index(drop=True)


def write_candles(
    instrument_key: str,
    timeframe: str,
    df: pd.DataFrame,
    source: str = "upstox",
) -> int:
    """Merge bars into storage. Returns total bar count after the merge.

    Idempotent: writing the same data twice leaves the file unchanged.
    """
    if df.empty:
        return read_coverage(instrument_key, timeframe).get("bar_count", 0)

    incoming = _normalize(df)
    path = candle_path(instrument_key, timeframe)

    if path.exists():
        existing = pd.read_parquet(path)
        existing["timestamp"] = pd.to_datetime(existing["timestamp"], utc=True).dt.tz_convert(IST)
        combined = pd.concat([existing, incoming], ignore_index=True)
        combined = combined.sort_values("timestamp", kind="stable")
        combined = combined.drop_duplicates(subset="timestamp", keep="last").reset_index(drop=True)
    else:
        combined = incoming

    combined.to_parquet(path, index=False, compression="snappy")
    _update_coverage(instrument_key, timeframe, combined, source)
    return len(combined)


def _update_coverage(
    instrument_key: str, timeframe: str, df: pd.DataFrame, source: str
) -> None:
    get_db().execute(
        """INSERT OR REPLACE INTO coverage
           (instrument_key, timeframe, first_bar, last_bar, bar_count, updated_at, source)
           VALUES (?,?,?,?,?,?,?)""",
        [
            instrument_key,
            timeframe,
            df["timestamp"].iloc[0].to_pydatetime(),
            df["timestamp"].iloc[-1].to_pydatetime(),
            len(df),
            datetime.now(IST),
            source,
        ],
    )


def read_candles(
    instrument_key: str,
    timeframe: str,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
) -> pd.DataFrame:
    """Read bars for a range. Returns an empty canonical frame if nothing stored."""
    path = candle_path(instrument_key, timeframe)
    if not path.exists():
        return pd.DataFrame(columns=CANDLE_COLUMNS)

    df = pd.read_parquet(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(IST)

    if start is not None:
        df = df[df["timestamp"] >= _as_ts(start)]
    if end is not None:
        df = df[df["timestamp"] <= _as_ts(end, end_of_day=True)]

    return df.reset_index(drop=True)


def _as_ts(value: date | datetime, end_of_day: bool = False) -> pd.Timestamp:
    if isinstance(value, datetime):
        ts = pd.Timestamp(value)
    else:
        ts = pd.Timestamp(value)
        if end_of_day:
            ts = ts + pd.Timedelta(hours=23, minutes=59, seconds=59)
    return ts.tz_localize(IST) if ts.tz is None else ts.tz_convert(IST)


def read_coverage(instrument_key: str, timeframe: str) -> dict:
    row = get_db().execute(
        """SELECT first_bar, last_bar, bar_count, updated_at, source
           FROM coverage WHERE instrument_key = ? AND timeframe = ?""",
        [instrument_key, timeframe],
    ).fetchone()
    if not row:
        return {}
    return {
        "first_bar": row[0], "last_bar": row[1], "bar_count": row[2],
        "updated_at": row[3], "source": row[4],
    }


def all_coverage() -> pd.DataFrame:
    return get_db().execute(
        "SELECT * FROM coverage ORDER BY instrument_key, timeframe"
    ).df()


def delete_candles(instrument_key: str, timeframe: str) -> None:
    path = candle_path(instrument_key, timeframe)
    path.unlink(missing_ok=True)
    get_db().execute(
        "DELETE FROM coverage WHERE instrument_key = ? AND timeframe = ?",
        [instrument_key, timeframe],
    )


def stored_instruments(timeframe: str | None = None) -> list[tuple[str, str]]:
    q = "SELECT instrument_key, timeframe FROM coverage"
    params: list = []
    if timeframe:
        q += " WHERE timeframe = ?"
        params.append(timeframe)
    return [tuple(r) for r in get_db().execute(q + " ORDER BY 1,2", params).fetchall()]
