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
    "write_candles", "write_download",
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


def _service():
    from backend.data.service import DataService

    _ensure_migrated()
    return DataService()


def write_candles(
    instrument_key: str,
    timeframe: str,
    df: pd.DataFrame,
    source: str = "upstox",
) -> int:
    """Merge bars into the kept library (simulated data, imports, CLI downloads).
    Returns total bar count after the merge. Idempotent.

    Bars downloaded by the running app belong in `write_download` instead, which
    honours the app session's keep/discard choice.
    """
    return _library().write(instrument_key, timeframe, df, source)


def write_download(instrument_key: str, timeframe: str, df: pd.DataFrame) -> int:
    """Store broker bars where the current app session keeps downloads."""
    return _service().write_download(instrument_key, timeframe, df)


def read_candles(
    instrument_key: str,
    timeframe: str,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
) -> pd.DataFrame:
    """Bars for a range from the library and this app session's cache. Empty
    canonical frame if nothing is stored. Never downloads."""
    return _service().read_stored(instrument_key, timeframe, start, end)


def _merged_series(timeframe: str | None = None, instrument_key: str | None = None):
    """Series across the library and the session cache, one entry per series."""
    merged: dict[tuple[str, str], dict] = {}
    for store in _service().read_stores:
        for info in store.all_series(timeframe):
            if instrument_key is not None and info.instrument_key != instrument_key:
                continue
            key = (info.instrument_key, info.timeframe)
            entry = merged.setdefault(key, {
                "instrument_key": info.instrument_key, "timeframe": info.timeframe,
                "first_bar": info.first_bar, "last_bar": info.last_bar, "bar_count": 0,
                "updated_at": info.updated_at, "source": info.source,
                "in_library": False, "in_session": False,
            })
            # The cache only ever holds sessions the library lacks, so counts add up.
            entry["bar_count"] += info.rows
            entry["first_bar"] = min(entry["first_bar"], info.first_bar)
            entry["last_bar"] = max(entry["last_bar"], info.last_bar)
            entry["updated_at"] = max(entry["updated_at"], info.updated_at)
            entry["in_library" if store.name == "library" else "in_session"] = True
    return [merged[k] for k in sorted(merged)]


def _timestamps(entry: dict) -> dict:
    return {**entry,
            "first_bar": pd.Timestamp(entry["first_bar"]).tz_convert(IST),
            "last_bar": pd.Timestamp(entry["last_bar"]).tz_convert(IST),
            "updated_at": pd.Timestamp(entry["updated_at"])}


def read_coverage(instrument_key: str, timeframe: str) -> dict:
    found = _merged_series(timeframe, instrument_key)
    return _timestamps(found[0]) if found else {}


def all_coverage() -> pd.DataFrame:
    columns = ["instrument_key", "timeframe", "first_bar", "last_bar", "bar_count",
               "updated_at", "source", "in_library", "in_session"]
    return pd.DataFrame([_timestamps(e) for e in _merged_series()], columns=columns)


def delete_candles(instrument_key: str, timeframe: str) -> None:
    _library().delete(instrument_key, timeframe)


def stored_instruments(timeframe: str | None = None) -> list[tuple[str, str]]:
    return [(e["instrument_key"], e["timeframe"]) for e in _merged_series(timeframe)]


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
