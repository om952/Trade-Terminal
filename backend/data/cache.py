"""Read-through bar cache: the one way the rest of the system asks for candles.

    get_bars("NSE_INDEX|Nifty 50", "15m", date(2025, 1, 1), date(2025, 6, 30))

returns the bars, fetching whatever is missing first. Nobody downloads data by
hand any more, and nothing outside this module decides what to fetch.

Rules (PRODUCTION_PLAN.md P1.1):

* **Two base timeframes.** Intraday data is downloaded only as 1-minute bars and
  daily data only as 1-day bars. 3m, 5m, 15m, 1h and every other intraday
  timeframe are derived locally, bucketed from the session open (09:15 on
  NSE/BSE, 09:00 on MCX). One download serves every timeframe, and switching a
  strategy from 5m to 15m never touches the network. Weekly and monthly bars
  are fetched natively as their own base, because their bucketing rules belong
  to the exchange.
* **Fetch only missing sessions.** A ledger records every session checked per
  instrument and base timeframe. Missing sessions are merged into a few ranges
  (one call covers up to 25 days of 1-minute data), not one call per day.
* **Past bars are permanent.** A session counts as settled once it was checked
  at least `RECHECK_DAYS` after it happened; until then it is re-fetched on
  demand, which picks up late revisions. Sessions that returned nothing (an
  unlisted holiday, before the contract listed, after it expired) are
  remembered, so they are not asked for again.
* **Today is never cached.** The historical endpoint excludes the current
  session, so the cache ends at the last completed one. Live warm-up appends
  today's bars itself (backend/live/feed.py).
* **Every result carries a fingerprint**: source, range and a hash of the bars.
  Two runs with the same fingerprint saw the same data.

Synthetic (`SIM|...`) and imported data are local-only: read as stored, never
fetched. Data stored per timeframe before this module existed is still read
when no 1-minute base covers the range, which is the migration path; run
`python -m backend.data.ingest backfill` to replace it with the base.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import pandas as pd

from backend.core.config import IST
from backend.data import calendar as cal
from backend.data.store import (
    CANDLE_COLUMNS,
    get_db,
    read_candles,
    read_coverage,
    write_candles,
)
from backend.data.upstox_client import CHUNK_DAYS, EARLIEST, FetchResult, Timeframe

log = logging.getLogger(__name__)

MINUTE = Timeframe("minutes", 1)
DAY = Timeframe("days", 1)

# A session checked this many days or more after it happened is settled.
RECHECK_DAYS = 2

# Missing sessions closer together than this are fetched in one range: the
# sessions in between are re-downloaded, which costs less than another call.
MERGE_GAP_DAYS = 10

# How far back a request with no start date reaches.
DEFAULT_LOOKBACK_DAYS = {"minutes": 365, "hours": 365, "days": 5 * 365}
DEFAULT_LOOKBACK_FALLBACK = 10 * 365

# A stored session with fewer bars than this share of a full one is treated as
# partial (typically an intraday fetch taken mid-session) and fetched again.
THIN_SHARE = 0.8

_FETCHABLE = re.compile(r"^(NSE|BSE|MCX|NCD|BCD)_[A-Z]+\|")

Fetcher = Callable[..., FetchResult]
Progress = Callable[[float, str], None]

_ensure_locks: defaultdict[tuple[str, str], threading.Lock] = defaultdict(threading.Lock)
_ensure_locks_guard = threading.Lock()


class DataUnavailable(RuntimeError):
    """No bars could be produced for the request. The message says why."""


@dataclass(frozen=True)
class Bars:
    """Bars plus where they came from."""

    frame: pd.DataFrame
    instrument_key: str
    timeframe: str
    source: str                         # upstox, simulated, csv:..., or "" if empty
    derived_from: str | None            # "1m" when derived, None when read as stored
    start: date | None
    end: date | None
    fingerprint: str
    missing_sessions: list[date] = field(default_factory=list)
    fetched_sessions: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return self.frame.empty

    @property
    def synthetic(self) -> bool:
        return self.source == "simulated" or self.instrument_key.startswith("SIM|")

    def describe(self) -> dict:
        """JSON-safe summary, stored alongside results so a run can be reproduced."""
        first = self.frame["timestamp"].iloc[0].isoformat() if not self.empty else None
        last = self.frame["timestamp"].iloc[-1].isoformat() if not self.empty else None
        return {
            "instrument_key": self.instrument_key,
            "timeframe": self.timeframe,
            "source": self.source,
            "derived_from": self.derived_from,
            "synthetic": self.synthetic,
            "requested_start": self.start.isoformat() if self.start else None,
            "requested_end": self.end.isoformat() if self.end else None,
            "first_bar": first,
            "last_bar": last,
            "bars": len(self.frame),
            "fingerprint": self.fingerprint,
            "missing_sessions": len(self.missing_sessions),
            "fetched_sessions": self.fetched_sessions,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Timeframes
# ---------------------------------------------------------------------------

def base_timeframe(tf: Timeframe) -> Timeframe:
    """The timeframe actually downloaded to serve `tf`."""
    if tf.unit in ("minutes", "hours"):
        return MINUTE
    return tf


def session_open(instrument_key: str) -> time:
    return cal.MCX_OPEN if instrument_key.startswith("MCX_") else cal.MARKET_OPEN


def derive(base: pd.DataFrame, tf: Timeframe, open_at: time = cal.MARKET_OPEN) -> pd.DataFrame:
    """Aggregate finer bars into `tf`, bucketed from the session open.

    `base` is normally 1-minute bars, but any finer series aligned to the same
    open works (5m into 15m, say): every aggregate used is exact under nesting.

    Timestamps mark the start of each bar, as Upstox's do. With a 09:15 open a
    1h series runs 09:15, 10:15, ... 15:15, the last bar holding 15 minutes, and
    a 5m series has 75 bars per full session.
    """
    if tf.unit not in ("minutes", "hours"):
        raise ValueError(f"Only intraday timeframes are derived, not {tf.label}")
    if base.empty:
        return pd.DataFrame(columns=CANDLE_COLUMNS)

    minutes = _minutes(tf)
    if minutes == 1:
        return base[CANDLE_COLUMNS].reset_index(drop=True)

    ts = pd.to_datetime(base["timestamp"], utc=True).dt.tz_convert(IST)
    anchor = ts.dt.normalize() + pd.Timedelta(hours=open_at.hour, minutes=open_at.minute)
    offset = ((ts - anchor) // pd.Timedelta(minutes=1)).astype("int64")
    bucket = anchor + pd.to_timedelta((offset // minutes) * minutes, unit="min")

    frame = base.assign(timestamp=ts, _bucket=bucket.values).sort_values("timestamp")
    grouped = frame.groupby("_bucket", sort=True)
    out = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(),
            "oi": grouped["oi"].last(),
        }
    )
    out.index.name = "timestamp"
    out = out.reset_index()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True).dt.tz_convert(IST)
    out["volume"] = out["volume"].astype("int64")
    out["oi"] = out["oi"].astype("int64")
    return out[CANDLE_COLUMNS]


def _minutes(tf: Timeframe) -> int:
    return tf.interval * (60 if tf.unit == "hours" else 1)


def bars_per_session(tf: Timeframe, instrument_key: str = "") -> int:
    return cal.expected_bars_per_day(tf.label, mcx=instrument_key.startswith("MCX_"))


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def _db():
    # A cursor per call: the shared connection is not safe across threads.
    return get_db().cursor()


def _ledger(instrument_key: str, tf: Timeframe, start: date, end: date) -> dict[date, tuple[int, date]]:
    rows = _db().execute(
        """SELECT day, bars, checked_on FROM session_ledger
           WHERE instrument_key = ? AND timeframe = ? AND day BETWEEN ? AND ?""",
        [instrument_key, tf.label, start, end],
    ).fetchall()
    return {r[0]: (int(r[1]), r[2]) for r in rows}


def _record(instrument_key: str, tf: Timeframe, sessions: dict[date, int], checked_on: date) -> None:
    if not sessions:
        return
    _db().executemany(
        """INSERT OR REPLACE INTO session_ledger
           (instrument_key, timeframe, day, bars, checked_on) VALUES (?,?,?,?,?)""",
        [(instrument_key, tf.label, d, n, checked_on) for d, n in sorted(sessions.items())],
    )


def forget(instrument_key: str, timeframe: str | None = None) -> None:
    """Drop ledger entries so the next request re-checks those sessions."""
    q = "DELETE FROM session_ledger WHERE instrument_key = ?"
    params: list = [instrument_key]
    if timeframe:
        q += " AND timeframe = ?"
        params.append(timeframe)
    _db().execute(q, params)


# ---------------------------------------------------------------------------
# Deciding what to fetch
# ---------------------------------------------------------------------------

def is_fetchable(instrument_key: str) -> bool:
    """True for broker instruments. Synthetic and imported data never are."""
    return bool(_FETCHABLE.match(instrument_key))


def broker_ready() -> tuple[bool, str]:
    """Whether a fetch can be attempted right now, and if not, why."""
    from backend.data.upstox_auth import token_status

    status = token_status()
    return status.valid, status.reason


def last_completed_session(today: date | None = None) -> date:
    """The newest session the historical endpoint can serve: never today."""
    day = (today or datetime.now(IST).date()) - timedelta(days=1)
    for _ in range(15):
        if cal.is_trading_day(day):
            return day
        day -= timedelta(days=1)
    return day


def default_start(tf: Timeframe, end: date) -> date:
    return end - timedelta(days=DEFAULT_LOOKBACK_DAYS.get(tf.unit, DEFAULT_LOOKBACK_FALLBACK))


def warmup_start(tf: Timeframe, instrument_key: str, bars: int, before: date) -> date:
    """A start date far enough back to supply `bars` bars ending before `before`.

    Sessions are converted to calendar days with room for weekends and holidays
    (about 250 sessions a year), plus a margin, so warm-up never runs short.
    """
    if tf.unit in ("weeks", "months"):
        per_bar_days = 7 if tf.unit == "weeks" else 31
        return before - timedelta(days=int(bars * tf.interval * per_bar_days * 1.1) + 31)
    sessions = -(-bars // max(1, bars_per_session(tf, instrument_key)))
    if tf.unit == "days":
        sessions *= tf.interval
    return before - timedelta(days=int(sessions * 365 / 245) + 10)


def _settled(day: date, checked_on: date) -> bool:
    return checked_on >= day + timedelta(days=RECHECK_DAYS)


def missing_sessions(
    instrument_key: str, base: Timeframe, start: date, end: date, today: date | None = None
) -> list[date]:
    """Sessions in [start, end] the cache cannot yet vouch for."""
    today = today or datetime.now(IST).date()
    expected = cal.trading_days(start, end)
    if not expected:
        return []

    ledger = _ledger(instrument_key, base, start, end)

    # Sessions stored before the ledger existed. Counted as settled when they
    # are old enough and complete, so existing history is not downloaded again.
    stored = read_candles(instrument_key, base.label, start, end)
    stored_counts = (
        stored["timestamp"].dt.date.value_counts().to_dict() if not stored.empty else {}
    )
    full = bars_per_session(base, instrument_key)

    out = []
    for day in expected:
        entry = ledger.get(day)
        if entry is not None:
            if _settled(day, entry[1]):
                continue
        elif (
            stored_counts.get(day, 0) >= full * THIN_SHARE
            and today >= day + timedelta(days=RECHECK_DAYS)
        ):
            continue
        out.append(day)
    return out


def _ranges(days: list[date]) -> list[tuple[date, date]]:
    """Merge sorted days into (first, last) ranges, bridging short gaps."""
    out: list[tuple[date, date]] = []
    for day in days:
        if out and (day - out[-1][1]).days <= MERGE_GAP_DAYS:
            out[-1] = (out[-1][0], day)
        else:
            out.append((day, day))
    return out


def _in_failed(day: date, failed: list[tuple[date, date, str]]) -> bool:
    return any(a <= day <= b for a, b, _ in failed)


@dataclass
class EnsureReport:
    requested: int = 0          # sessions that were missing
    fetched: int = 0            # sessions now settled with bars
    empty: int = 0              # sessions confirmed to have no data
    failed: int = 0             # sessions still unknown after a failed call
    calls: int = 0
    notes: list[str] = field(default_factory=list)


def ensure(
    instrument_key: str,
    base: Timeframe,
    start: date,
    end: date,
    *,
    fetcher: Fetcher | None = None,
    on_progress: Progress | None = None,
    today: date | None = None,
) -> EnsureReport:
    """Fetch every missing session of `base` in [start, end] into the store."""
    from backend.data.upstox_client import fetch_candles_report

    fetcher = fetcher or fetch_candles_report
    today = today or datetime.now(IST).date()
    report = EnsureReport()

    end = min(end, last_completed_session(today))
    earliest = EARLIEST.get(base.unit)
    if earliest and start < earliest:
        report.notes.append(
            f"Upstox serves {base.label} bars from {earliest} only; earlier sessions are skipped."
        )
        start = earliest
    if start > end:
        return report

    with _ensure_lock(instrument_key, base.label):
        todo = missing_sessions(instrument_key, base, start, end, today)
        report.requested = len(todo)
        if not todo:
            return report

        ranges = _ranges(todo)
        span = CHUNK_DAYS[base.unit]
        total_calls = sum(-(-((b - a).days + 1) // span) for a, b in ranges)
        log.info(
            "%s %s: fetching %d missing session(s) in %d range(s), about %d call(s)",
            instrument_key, base.label, len(todo), len(ranges), total_calls,
        )
        todo_set = set(todo)

        for a, b in ranges:
            calls_before = report.calls

            def on_chunk(done: int, _total: int, _before: int = calls_before) -> None:
                report.calls = _before + done
                if on_progress:
                    on_progress(
                        min(1.0, report.calls / max(1, total_calls)),
                        f"Downloading {instrument_key} {base.label}: "
                        f"{report.calls}/{total_calls} request(s)",
                    )

            result = fetcher(
                instrument_key, base, a, b, include_today=False, on_chunk=on_chunk
            )
            report.calls = calls_before + max(result.calls, report.calls - calls_before)

            frame = result.frame
            if not frame.empty:
                write_candles(instrument_key, base.label, frame, source="upstox")
                counts = (
                    pd.to_datetime(frame["timestamp"], utc=True)
                    .dt.tz_convert(IST).dt.date.value_counts().to_dict()
                )
            else:
                counts = {}

            checked: dict[date, int] = {}
            for day in cal.trading_days(a, b):
                if day not in todo_set:
                    continue
                if _in_failed(day, result.failed):
                    report.failed += 1
                    continue
                n = int(counts.get(day, 0))
                checked[day] = n
                if n:
                    report.fetched += 1
                else:
                    report.empty += 1
            _record(instrument_key, base, checked, today)

        if report.failed:
            report.notes.append(
                f"{report.failed} session(s) could not be downloaded and will be retried next time."
            )
        if on_progress:
            # The call count is an estimate, so finish explicitly.
            on_progress(
                1.0,
                f"Downloaded {instrument_key} {base.label}: {report.fetched} session(s), "
                f"{report.calls} request(s)",
            )
        return report


def _ensure_lock(instrument_key: str, label: str) -> threading.Lock:
    with _ensure_locks_guard:
        return _ensure_locks[(instrument_key, label)]


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------

def get_bars(
    instrument_key: str,
    timeframe: str | Timeframe,
    start: date | None = None,
    end: date | None = None,
    *,
    fetch: bool = True,
    fetcher: Fetcher | None = None,
    on_progress: Progress | None = None,
    today: date | None = None,
) -> Bars:
    """Bars for a range, fetched and derived as needed.

    `start=None` means the default lookback for a broker instrument, and
    everything stored for a local-only one. `end=None` means the last completed
    session. Raises nothing for missing data: an empty result says why in
    `notes`, and callers decide whether that is an error.
    """
    tf = Timeframe.parse(timeframe) if isinstance(timeframe, str) else timeframe
    base = base_timeframe(tf)
    notes: list[str] = []
    fetched = 0

    use_network = fetch and is_fetchable(instrument_key)
    if use_network:
        ready, reason = broker_ready() if fetcher is None else (True, "")
        if not ready:
            use_network = False
            notes.append(f"Not downloading: {reason}")

    if is_fetchable(instrument_key):
        end = end or last_completed_session(today)
        start = start or default_start(tf, end)

    if use_network:
        report = ensure(
            instrument_key, base, start, end,  # type: ignore[arg-type]
            fetcher=fetcher, on_progress=on_progress, today=today,
        )
        fetched = report.fetched
        notes.extend(report.notes)

    frame, source, derived_from = _read(instrument_key, tf, base, start, end)

    missing: list[date] = []
    if is_fetchable(instrument_key) and start and end:
        missing = _uncovered(instrument_key, base, frame, start, min(end, last_completed_session(today)))
        if missing:
            notes.append(f"{len(missing)} session(s) in the range have no bars.")

    return Bars(
        frame=frame,
        instrument_key=instrument_key,
        timeframe=tf.label,
        source=source,
        derived_from=derived_from,
        start=start,
        end=end,
        fingerprint=fingerprint(instrument_key, tf.label, source, frame),
        missing_sessions=missing,
        fetched_sessions=fetched,
        notes=notes,
    )


def _read(
    instrument_key: str, tf: Timeframe, base: Timeframe, start: date | None, end: date | None
) -> tuple[pd.DataFrame, str, str | None]:
    """Prefer bars derived from the base; fall back to a natively stored series."""
    native = read_candles(instrument_key, tf.label, start, end)
    if base == tf:
        return native, _source(instrument_key, tf.label, native), None

    base_frame = read_candles(instrument_key, base.label, start, end)
    if not base_frame.empty and _sessions(base_frame) >= _sessions(native):
        derived = derive(base_frame, tf, session_open(instrument_key))
        return derived, _source(instrument_key, base.label, base_frame), base.label
    if not native.empty:
        return native, _source(instrument_key, tf.label, native), None

    # Neither the base nor the timeframe itself is stored. A finer series stored
    # before the cache existed (native 5m, say) still derives a coarser one.
    for finer in _finer_stored(instrument_key, tf):
        frame = read_candles(instrument_key, finer.label, start, end)
        if not frame.empty:
            derived = derive(frame, tf, session_open(instrument_key))
            return derived, _source(instrument_key, finer.label, frame), finer.label
    return native, "", None


def _finer_stored(instrument_key: str, tf: Timeframe) -> list[Timeframe]:
    """Stored intraday timeframes that divide `tf` evenly, finest first."""
    from backend.data.store import stored_instruments

    target = _minutes(tf)
    out = []
    for key, label in stored_instruments():
        if key != instrument_key:
            continue
        try:
            other = Timeframe.parse(label)
        except ValueError:
            continue
        if other.unit in ("minutes", "hours") and 1 < _minutes(other) < target:
            if target % _minutes(other) == 0:
                out.append(other)
    return sorted(out, key=_minutes)


def _sessions(frame: pd.DataFrame) -> int:
    return 0 if frame.empty else int(frame["timestamp"].dt.date.nunique())


def _source(instrument_key: str, label: str, frame: pd.DataFrame) -> str:
    if frame.empty:
        return ""
    return read_coverage(instrument_key, label).get("source") or "unknown"


def _uncovered(
    instrument_key: str, base: Timeframe, frame: pd.DataFrame, start: date, end: date
) -> list[date]:
    """Expected sessions with no bars that the ledger has not confirmed empty."""
    if start > end:
        return []
    present = set(frame["timestamp"].dt.date) if not frame.empty else set()
    ledger = _ledger(instrument_key, base, start, end)
    confirmed_empty = {d for d, (n, _) in ledger.items() if n == 0}
    earliest = EARLIEST.get(base.unit) or start
    return [
        d for d in cal.trading_days(max(start, earliest), end)
        if d not in present and d not in confirmed_empty
    ]


def fingerprint(instrument_key: str, timeframe: str, source: str, frame: pd.DataFrame) -> str:
    """Stable hash of exactly the bars a computation saw."""
    digest = hashlib.sha256(f"{instrument_key}|{timeframe}|{source}|{len(frame)}|".encode())
    if not frame.empty:
        values = frame[CANDLE_COLUMNS].copy()
        values["timestamp"] = pd.to_datetime(values["timestamp"], utc=True).astype("int64")
        digest.update(pd.util.hash_pandas_object(values, index=False).values.tobytes())
    return digest.hexdigest()[:16]
