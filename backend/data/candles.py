"""Candle storage: Parquet files partitioned by instrument, timeframe and month.

Layout under a store's root:

    {root}/{timeframe}/{instrument}/{YYYY-MM}.parquet

Month partitions keep each file small and independently replaceable, let a read
touch only the months it needs, and scale to the thousands of option contracts
real premium data may bring (one series per contract). What is stored where is
indexed in the app database (`data_partitions`), so listing coverage never has
to open a file.

Writes merge into the existing month, deduplicating by timestamp with the newer
bar winning (a re-fetch is treated as a correction). Each month is written to a
temporary file and swapped in atomically, under a lock that holds across
processes, so a crash can't truncate a file and two writers can't lose each
other's bars.

A `CandleStore` is one root: the kept library, or (from P1.4) one app session's
scratch cache. The data service reads across both.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from backend.app.context import AppContext
from backend.app.db import get_app_db, now_iso
from backend.core.config import IST
from backend.core.filelock import file_lock

log = logging.getLogger(__name__)

CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "oi"]

_MONTH_FILE = re.compile(r"^\d{4}-\d{2}\.parquet$")


def safe_name(instrument_key: str) -> str:
    """Filesystem-safe name from an instrument key: 'NSE_INDEX|Nifty 50' -> 'NSE_INDEX__Nifty_50'."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", instrument_key.replace("|", "__"))


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=CANDLE_COLUMNS)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce to the canonical schema, sorted, deduplicated, tz-aware IST.

    Deduplication keeps the LAST occurrence: a re-fetch of an overlapping range
    is treated as a correction, since the exchange occasionally revises bars.
    """
    df = df.copy()
    # Metadata riding on the frame (such as the fetcher's failed windows) is not
    # candle data, and pandas would try to serialize it into the Parquet file.
    df.attrs = {}
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


def atomic_write_parquet(df: pd.DataFrame, path: Path) -> None:
    """Write `df` to `path` so that readers only ever see the old or the new file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    try:
        df.to_parquet(tmp, index=False, compression="snappy")
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        # Make the rename itself durable, not just the file contents.
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _as_ts(value: date | datetime, end_of_day: bool = False) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if not isinstance(value, datetime) and end_of_day:
        ts = ts + pd.Timedelta(hours=23, minutes=59, seconds=59)
    return ts.tz_localize(IST) if ts.tz is None else ts.tz_convert(IST)


def _month_of(value: date | datetime) -> str:
    return f"{value.year:04d}-{value.month:02d}"


@dataclass(frozen=True)
class SeriesInfo:
    """What one store holds for one instrument and timeframe."""

    store: str
    instrument_key: str
    timeframe: str
    rows: int
    first_bar: str
    last_bar: str
    bytes: int
    source: str
    months: int
    updated_at: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class CandleStore:
    def __init__(self, root: Path, name: str = "library", ctx: AppContext | None = None) -> None:
        self.root = root
        self.name = name
        self.ctx = ctx or AppContext.local()

    # -- paths ------------------------------------------------------------------

    def series_dir(self, instrument_key: str, timeframe: str) -> Path:
        return self.root / timeframe / safe_name(instrument_key)

    def partition_path(self, instrument_key: str, timeframe: str, month: str) -> Path:
        return self.series_dir(instrument_key, timeframe) / f"{month}.parquet"

    def _lock_path(self, instrument_key: str, timeframe: str) -> Path:
        return self.root / ".locks" / f"{timeframe}__{safe_name(instrument_key)}.lock"

    # -- writes -----------------------------------------------------------------

    def write(self, instrument_key: str, timeframe: str, df: pd.DataFrame, source: str) -> int:
        """Merge bars into the series. Returns the series' total bar count afterwards."""
        if df.empty:
            info = self.info(instrument_key, timeframe)
            return info.rows if info else 0

        incoming = normalize(df)
        months = incoming["timestamp"].dt.strftime("%Y-%m")
        with file_lock(self._lock_path(instrument_key, timeframe)):
            for month, part in incoming.groupby(months, sort=True):
                self._merge_month(instrument_key, timeframe, str(month), part, source)
        info = self.info(instrument_key, timeframe)
        return info.rows if info else 0

    def _merge_month(self, key: str, timeframe: str, month: str, part: pd.DataFrame,
                     source: str) -> None:
        path = self.partition_path(key, timeframe, month)
        if path.exists():
            existing = pd.read_parquet(path)
            existing["timestamp"] = pd.to_datetime(existing["timestamp"], utc=True).dt.tz_convert(IST)
            merged = pd.concat([existing, part], ignore_index=True)
            merged = merged.sort_values("timestamp", kind="stable")
            merged = merged.drop_duplicates(subset="timestamp", keep="last").reset_index(drop=True)
        else:
            merged = part.reset_index(drop=True)

        atomic_write_parquet(merged, path)
        with get_app_db(self.ctx).connect() as conn:
            conn.execute(
                "INSERT INTO data_partitions (user_id, store, instrument_key, timeframe, month, rows,"
                " first_bar, last_bar, bytes, source, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (user_id, store, instrument_key, timeframe, month) DO UPDATE SET"
                " rows = excluded.rows, first_bar = excluded.first_bar,"
                " last_bar = excluded.last_bar, bytes = excluded.bytes,"
                " source = excluded.source, updated_at = excluded.updated_at",
                (self.ctx.user_id, self.name, key, timeframe, month, len(merged),
                 merged["timestamp"].iloc[0].isoformat(), merged["timestamp"].iloc[-1].isoformat(),
                 path.stat().st_size, source, now_iso()),
            )

    def delete(self, instrument_key: str, timeframe: str) -> None:
        with file_lock(self._lock_path(instrument_key, timeframe)):
            shutil.rmtree(self.series_dir(instrument_key, timeframe), ignore_errors=True)
            with get_app_db(self.ctx).connect() as conn:
                conn.execute(
                    "DELETE FROM data_partitions WHERE user_id = ? AND store = ?"
                    " AND instrument_key = ? AND timeframe = ?",
                    (self.ctx.user_id, self.name, instrument_key, timeframe),
                )

    def delete_all(self) -> None:
        """Remove every file and index row of this store."""
        shutil.rmtree(self.root, ignore_errors=True)
        with get_app_db(self.ctx).connect() as conn:
            conn.execute("DELETE FROM data_partitions WHERE user_id = ? AND store = ?",
                         (self.ctx.user_id, self.name))

    # -- reads ------------------------------------------------------------------

    def months(self, instrument_key: str, timeframe: str) -> list[str]:
        directory = self.series_dir(instrument_key, timeframe)
        if not directory.exists():
            return []
        return sorted(p.stem for p in directory.iterdir() if _MONTH_FILE.match(p.name))

    def read(
        self,
        instrument_key: str,
        timeframe: str,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
        columns: list[str] | None = None,
    ) -> pd.DataFrame:
        """Bars in [start, end] (dates are whole days). Empty canonical frame if none."""
        wanted = self.months(instrument_key, timeframe)
        if start is not None:
            wanted = [m for m in wanted if m >= _month_of(start)]
        if end is not None:
            wanted = [m for m in wanted if m <= _month_of(end)]
        if not wanted:
            return empty_frame() if columns is None else empty_frame()[columns]

        frames = [
            pd.read_parquet(self.partition_path(instrument_key, timeframe, m), columns=columns)
            for m in wanted
        ]
        df = pd.concat(frames, ignore_index=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(IST)
        if start is not None:
            df = df[df["timestamp"] >= _as_ts(start)]
        if end is not None:
            df = df[df["timestamp"] <= _as_ts(end, end_of_day=True)]
        return df.sort_values("timestamp", kind="stable").reset_index(drop=True)

    def info(self, instrument_key: str, timeframe: str) -> SeriesInfo | None:
        found = self._series(instrument_key=instrument_key, timeframe=timeframe)
        return found[0] if found else None

    def all_series(self, timeframe: str | None = None) -> list[SeriesInfo]:
        return self._series(timeframe=timeframe)

    def size_bytes(self) -> int:
        with get_app_db(self.ctx).read() as conn:
            row = conn.execute(
                "SELECT coalesce(sum(bytes), 0) FROM data_partitions WHERE user_id = ? AND store = ?",
                (self.ctx.user_id, self.name),
            ).fetchone()
        return int(row[0])

    def _series(self, instrument_key: str | None = None, timeframe: str | None = None) -> list[SeriesInfo]:
        query = (
            "SELECT instrument_key, timeframe, sum(rows) AS rows, min(first_bar) AS first_bar,"
            " max(last_bar) AS last_bar, sum(bytes) AS bytes, count(*) AS months,"
            " max(updated_at) AS updated_at,"
            # The source of the most recently written month speaks for the series.
            " (SELECT p2.source FROM data_partitions p2 WHERE p2.user_id = p.user_id"
            "   AND p2.store = p.store AND p2.instrument_key = p.instrument_key"
            "   AND p2.timeframe = p.timeframe ORDER BY p2.updated_at DESC LIMIT 1) AS source"
            " FROM data_partitions p WHERE user_id = ? AND store = ?"
        )
        params: list = [self.ctx.user_id, self.name]
        if instrument_key is not None:
            query += " AND instrument_key = ?"
            params.append(instrument_key)
        if timeframe is not None:
            query += " AND timeframe = ?"
            params.append(timeframe)
        query += " GROUP BY instrument_key, timeframe ORDER BY instrument_key, timeframe"
        with get_app_db(self.ctx).read() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            SeriesInfo(store=self.name, instrument_key=r["instrument_key"],
                       timeframe=r["timeframe"], rows=int(r["rows"]), first_bar=r["first_bar"],
                       last_bar=r["last_bar"], bytes=int(r["bytes"]), source=r["source"],
                       months=int(r["months"]), updated_at=r["updated_at"])
            for r in rows
        ]


def library_store(ctx: AppContext | None = None) -> CandleStore:
    """The data kept on this machine."""
    ctx = ctx or AppContext.local()
    return CandleStore(ctx.data_dir / "store" / "candles", "library", ctx)


def session_dates(frames: Iterable[pd.DataFrame]) -> set[date]:
    """Trading dates present in candle frames (IST)."""
    out: set[date] = set()
    for df in frames:
        if not df.empty:
            out |= set(df["timestamp"].dt.date)
    return out
