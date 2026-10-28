"""Storage front door: DuckDB for the instrument master, candles via backend.data.candles.

Layout:
    data/terminal.duckdb                       instrument master and meta
    data/store/candles/{tf}/{key}/{YYYY-MM}.parquet   candles (see candles.py)
    data/app.sqlite                            which candles are where (data_partitions)

The candle functions here (`read_candles`, `write_candles`, ...) keep the
signatures the rest of the code base has always used and delegate to the kept
library (`backend.data.candles.library_store`). New code should use the data
service (`backend.data.service.DataService`), which also downloads what is
missing.

Candle files used to live at data/parquet/candles/{tf}/{key}.parquet with their
coverage in DuckDB. `migrate_legacy_candles()` moves them into the new layout
once, on first use; the old files are kept under data/parquet/legacy-migrated/
until you delete them.
"""

from __future__ import annotations

import logging
import shutil
import threading
from datetime import date, datetime

import duckdb
import pandas as pd

from backend.core.config import IST, settings
from backend.data.candles import CANDLE_COLUMNS, library_store, safe_name

log = logging.getLogger(__name__)

__all__ = [
    "CANDLE_COLUMNS", "all_coverage", "close_db", "delete_candles", "get_db",
    "migrate_legacy_candles", "read_candles", "read_coverage", "stored_instruments",
    "write_candles",
]

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
                # DuckDB permits a single writer process. Running a data CLI
                # while the API server is up produces a lock error that surfaces
                # several layers up, so name it here.
                raise RuntimeError(
                    f"Cannot open {settings.db_path}: another process holds it.\n"
                    f"DuckDB allows only one writer at a time — stop the API server "
                    f"before running a data command, or vice versa:\n"
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



# ---------------------------------------------------------------------------
# Candles (delegating to the library store)
# ---------------------------------------------------------------------------

_migrated: set[str] = set()
_migrate_lock = threading.Lock()


def _library():
    _ensure_migrated()
    return library_store()


def write_candles(
    instrument_key: str,
    timeframe: str,
    df: pd.DataFrame,
    source: str = "upstox",
) -> int:
    """Merge bars into the kept library. Returns total bar count after the merge.

    Idempotent: writing the same data twice leaves the series unchanged.
    """
    return _library().write(instrument_key, timeframe, df, source)


def read_candles(
    instrument_key: str,
    timeframe: str,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
) -> pd.DataFrame:
    """Read bars for a range. Returns an empty canonical frame if nothing stored."""
    return _library().read(instrument_key, timeframe, start, end)


def read_coverage(instrument_key: str, timeframe: str) -> dict:
    info = _library().info(instrument_key, timeframe)
    if info is None:
        return {}
    return {
        "first_bar": pd.Timestamp(info.first_bar).tz_convert(IST),
        "last_bar": pd.Timestamp(info.last_bar).tz_convert(IST),
        "bar_count": info.rows,
        "updated_at": pd.Timestamp(info.updated_at),
        "source": info.source,
    }


def all_coverage() -> pd.DataFrame:
    columns = ["instrument_key", "timeframe", "first_bar", "last_bar", "bar_count",
               "updated_at", "source"]
    rows = [
        (i.instrument_key, i.timeframe, pd.Timestamp(i.first_bar).tz_convert(IST),
         pd.Timestamp(i.last_bar).tz_convert(IST), i.rows, pd.Timestamp(i.updated_at), i.source)
        for i in _library().all_series()
    ]
    return pd.DataFrame(rows, columns=columns)


def delete_candles(instrument_key: str, timeframe: str) -> None:
    _library().delete(instrument_key, timeframe)


def stored_instruments(timeframe: str | None = None) -> list[tuple[str, str]]:
    return [(i.instrument_key, i.timeframe) for i in _library().all_series(timeframe)]


# ---------------------------------------------------------------------------
# One-time migration from the single-file layout
# ---------------------------------------------------------------------------

def _ensure_migrated() -> None:
    key = str(settings.data_dir.resolve())
    if key in _migrated:
        return
    with _migrate_lock:
        if key not in _migrated:
            migrate_legacy_candles()
            _migrated.add(key)


def migrate_legacy_candles() -> int:
    """Move data/parquet/candles/{tf}/{key}.parquet into the partitioned store.

    Instrument keys come from the old DuckDB coverage table (file names are a
    lossy encoding of them). Each file is copied into the new store and then
    moved aside to data/parquet/legacy-migrated/, never deleted, so the
    migration can be checked and re-done. Returns how many series were moved.
    """
    legacy_root = settings.parquet_dir / "candles"
    if not legacy_root.exists() or not any(legacy_root.rglob("*.parquet")):
        return 0

    try:
        rows = get_db().execute(
            "SELECT instrument_key, timeframe, source FROM coverage"
        ).fetchall() if _has_table("coverage") else []
    except duckdb.Error as exc:
        log.warning("Could not read legacy coverage, skipping candle migration: %s", exc)
        return 0

    library = library_store()
    aside = settings.parquet_dir / "legacy-migrated"
    moved = 0
    for instrument_key, timeframe, source in rows:
        path = legacy_root / timeframe / f"{safe_name(instrument_key)}.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        library.write(instrument_key, timeframe, df, source or "upstox")
        target = aside / timeframe / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), target)
        moved += 1
        log.info("Migrated %s %s (%d bars) into the partitioned store", instrument_key,
                 timeframe, len(df))

    leftovers = [p for p in legacy_root.rglob("*.parquet")]
    if leftovers:
        log.warning("%d legacy candle file(s) had no coverage record and were left in %s",
                    len(leftovers), legacy_root)
    return moved


def _has_table(name: str) -> bool:
    row = get_db().execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()
    return bool(row and row[0])
