"""The data service: the one place the rest of the app asks for bars.

    get_bars(instrument, timeframe, start, end)

returns the bars for that range, downloading only what is missing first. The
rules (plan §4.5):

- **Fetch only missing sessions.** The trading calendar says which sessions the
  range needs; the stores say which are present. Missing sessions are merged
  into a few contiguous ranges, so a year with three holes costs three short
  requests, not a year of them.
- **The past is permanent; the recent past is re-checked.** A stored session
  is complete unless it is one of the last few and ends before the close
  (fetched mid-day, or the day is still trading).
- **Remember sessions with no data.** A weekday the broker has no bars for (a
  holiday the calendar doesn't know, a date before listing) is recorded and not
  requested again. A window that failed is not mistaken for one with no data.
- **Say what a fetch will cost before doing it.** `plan()` returns the missing
  ranges, request count, bar and byte estimates, and a time estimate under the
  broker's rate limits.
- **Optionally download only 1-minute bars** and derive the rest
  (`derive_intraday_from_1m`, off by default; see backend/data/resample.py).

Simulated and imported series are never fetched: they come from the simulator
or a file, and the broker has nothing to add.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import pandas as pd

from backend.app import preferences
from backend.app.context import AppContext
from backend.app.db import get_app_db, now_iso
from backend.core.config import IST
from backend.data import calendar as cal
from backend.data.candles import CandleStore, empty_frame, library_store, normalize
from backend.data.resample import bar_minutes, derive, is_intraday
from backend.data.upstox_client import CHUNK_DAYS, EARLIEST, Timeframe

log = logging.getLogger(__name__)

# Keys the broker serves. Anything else (SIM|..., imported CSV keys) is local only.
_BROKER_KEY = re.compile(r"^(NSE|BSE|MCX|NCD|BCD)_[A-Z]+\|")

# Stored sessions this recent are checked for completeness and re-fetched if short.
RECHECK_SESSIONS = 5
# Missing sessions closer together than this (calendar days) are fetched as one range:
# a few redundant days are cheaper than an extra request.
MERGE_GAP_DAYS = 5
# Default history when nothing is stored and no start is given.
DEFAULT_LOOKBACK_DAYS = 365
# Rough cost model for estimates: per-request latency, and the broker's
# sustained limit of 2,000 requests per 30 minutes.
SECONDS_PER_REQUEST = 0.4
SUSTAINED_REQUESTS_PER_S = 2000 / 1800
BYTES_PER_BAR = 40

Fetcher = Callable[..., pd.DataFrame]
Progress = Callable[[int, int], None]


def is_fetchable(instrument_key: str) -> bool:
    return bool(_BROKER_KEY.match(instrument_key))


def is_mcx(instrument_key: str) -> bool:
    return instrument_key.upper().startswith("MCX")


@dataclass
class DataPlan:
    instrument_key: str
    timeframe: str
    fetch_timeframe: str
    start: date
    end: date
    fetchable: bool
    sessions_needed: int = 0
    sessions_missing: list[date] = field(default_factory=list)
    ranges: list[tuple[date, date]] = field(default_factory=list)
    requests: int = 0
    estimated_bars: int = 0
    estimated_bytes: int = 0
    estimated_seconds: float = 0.0
    note: str = ""

    @property
    def complete(self) -> bool:
        return not self.sessions_missing

    def to_dict(self) -> dict:
        return {
            "instrument_key": self.instrument_key,
            "timeframe": self.timeframe,
            "fetch_timeframe": self.fetch_timeframe,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "fetchable": self.fetchable,
            "complete": self.complete,
            "sessions_needed": self.sessions_needed,
            "sessions_missing": len(self.sessions_missing),
            "ranges": [[a.isoformat(), b.isoformat()] for a, b in self.ranges],
            "requests": self.requests,
            "estimated_bars": self.estimated_bars,
            "estimated_bytes": self.estimated_bytes,
            "estimated_seconds": round(self.estimated_seconds, 1),
            "note": self.note,
        }


@dataclass
class FetchReport:
    fetched_bars: int = 0
    no_data_sessions: list[date] = field(default_factory=list)
    failed_ranges: list[tuple[date, date]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class DataService:
    def __init__(
        self,
        ctx: AppContext | None = None,
        *,
        read_stores: list[CandleStore] | None = None,
        write_store: CandleStore | None = None,
        fetcher: Fetcher | None = None,
        today: Callable[[], date] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.ctx = ctx or AppContext.local()
        if read_stores is None and write_store is None:
            # The library, plus this app session's scratch cache when one is open
            # (downloads then land in scratch unless the trader keeps everything).
            from backend.app.app_session import download_stores

            read_stores, write_store = download_stores(self.ctx)
        self.write_store = write_store or library_store(self.ctx)
        self.read_stores = list(read_stores or [self.write_store])
        if self.write_store not in self.read_stores:
            self.read_stores.append(self.write_store)
        self._fetcher = fetcher
        self._now = now or (lambda: datetime.now(IST))
        self._today = today or (lambda: self._now().date())

    # -- public -----------------------------------------------------------------

    def fetch_timeframe(self, instrument_key: str, timeframe: str) -> str:
        """The timeframe actually downloaded and stored for `timeframe`."""
        if (
            is_fetchable(instrument_key)
            and is_intraday(timeframe)
            and timeframe != "1m"
            and preferences.get("derive_intraday_from_1m", self.ctx)
        ):
            return "1m"
        return timeframe

    def plan(
        self, instrument_key: str, timeframe: str,
        start: date | None = None, end: date | None = None,
    ) -> DataPlan:
        """What get_bars would download for this range, without downloading it."""
        fetch_tf = self.fetch_timeframe(instrument_key, timeframe)
        start, end = self._resolve_range(instrument_key, fetch_tf, start, end)
        plan = DataPlan(instrument_key, timeframe, fetch_tf, start, end,
                        fetchable=is_fetchable(instrument_key))
        if start > end:
            plan.note = "nothing to fetch: the range ends before it starts"
            return plan

        tf = Timeframe.parse(fetch_tf)
        if tf.unit in ("weeks", "months"):
            return self._plan_coarse(plan)

        needed = cal.trading_days(start, end)
        plan.sessions_needed = len(needed)
        frames = [s.read(instrument_key, fetch_tf, start, end, columns=["timestamp"])
                  for s in self.read_stores]
        stamps = pd.concat(frames, ignore_index=True)["timestamp"] if frames else pd.Series([])
        present = _dates(stamps) if len(stamps) else set()
        no_data = self.no_data_sessions(instrument_key, fetch_tf, start, end)

        missing = {d for d in needed if d not in present and d not in no_data}
        missing |= self._incomplete_recent(instrument_key, fetch_tf, needed, present, stamps)
        plan.sessions_missing = sorted(missing)
        if not plan.sessions_missing:
            return plan
        if not plan.fetchable:
            plan.note = ("local-only series (simulated or imported): missing sessions "
                         "can't be downloaded")
            return plan

        plan.ranges = merge_ranges(plan.sessions_missing, MERGE_GAP_DAYS)
        span = CHUNK_DAYS[tf.unit]
        plan.requests = sum(math.ceil(((b - a).days + 1) / span) for a, b in plan.ranges)
        if plan.ranges[-1][1] >= self._today():
            plan.requests += 1  # today's bars come from the intraday endpoint
        plan.estimated_bars = len(plan.sessions_missing) * cal.expected_bars_per_day(
            fetch_tf, mcx=is_mcx(instrument_key))
        plan.estimated_bytes = plan.estimated_bars * BYTES_PER_BAR
        plan.estimated_seconds = max(plan.requests * SECONDS_PER_REQUEST,
                                     max(0, plan.requests - 500) / SUSTAINED_REQUESTS_PER_S)
        return plan

    def ensure(self, plan: DataPlan, on_progress: Progress | None = None) -> FetchReport:
        """Download what `plan` says is missing into the write store."""
        report = FetchReport()
        if plan.complete or not plan.fetchable or not plan.ranges:
            return report

        fetcher = self._fetcher or _default_fetcher
        total = max(plan.requests, 1)
        done = 0
        today = self._today()
        for first, last in plan.ranges:
            span_requests = math.ceil(((last - first).days + 1) / CHUNK_DAYS[
                Timeframe.parse(plan.fetch_timeframe).unit])

            def progress(i: int, n: int, _base=done) -> None:
                if on_progress is not None:
                    on_progress(min(_base + i, total), total)

            try:
                df = fetcher(plan.instrument_key, plan.fetch_timeframe, first, last,
                             include_today=last >= today, on_progress=progress)
            except Exception as exc:
                # A missing token or a dead network: keep what we have and say so.
                report.errors.append(f"{type(exc).__name__}: {exc}")
                report.failed_ranges.append((first, last))
                log.warning("Fetch %s %s %s..%s failed: %s", plan.instrument_key,
                            plan.fetch_timeframe, first, last, exc)
                if _is_fatal(exc):
                    break
                continue
            finally:
                done += span_requests

            failed = list(df.attrs.get("failed_ranges", []))
            report.failed_ranges += failed
            if not df.empty:
                self.write_store.write(plan.instrument_key, plan.fetch_timeframe, df, "upstox")
                report.fetched_bars += len(df)

            got = _dates(df["timestamp"]) if not df.empty else set()
            for session in plan.sessions_missing:
                if not (first <= session <= last) or session in got or session >= today:
                    continue
                if any(a <= session <= b for a, b in failed):
                    continue
                report.no_data_sessions.append(session)

        if report.no_data_sessions:
            self._remember_no_data(plan.instrument_key, plan.fetch_timeframe,
                                   report.no_data_sessions)
        if on_progress is not None:
            on_progress(total, total)
        return report

    def get_bars(
        self,
        instrument_key: str,
        timeframe: str,
        start: date | None = None,
        end: date | None = None,
        *,
        fetch: bool = True,
        on_progress: Progress | None = None,
    ) -> tuple[pd.DataFrame, DataPlan, FetchReport]:
        """Bars for the range, after downloading what's missing (unless fetch=False).

        Returns the bars, the plan that was followed, and what the download did.
        A failed download is not an error here: the bars that are available are
        returned and the report says what went wrong, so the caller can decide.
        """
        plan = self.plan(instrument_key, timeframe, start, end)
        report = self.ensure(plan, on_progress) if fetch else FetchReport()
        df = self.read_stored(instrument_key, plan.fetch_timeframe, plan.start, plan.end)
        if plan.fetch_timeframe != timeframe and not df.empty:
            df = derive(df, timeframe, mcx=is_mcx(instrument_key))
        return df, plan, report

    def read_stored(self, instrument_key: str, timeframe: str,
                    start: date | datetime | None = None,
                    end: date | datetime | None = None) -> pd.DataFrame:
        """What the stores hold for the range, merged, without planning or fetching."""
        frames = [s.read(instrument_key, timeframe, start, end) for s in self.read_stores]
        frames = [f for f in frames if not f.empty]
        return normalize(pd.concat(frames, ignore_index=True)) if frames else empty_frame()

    def write_download(self, instrument_key: str, timeframe: str, df: pd.DataFrame) -> int:
        """Store bars downloaded from the broker where this session keeps downloads."""
        return self.write_store.write(instrument_key, timeframe, df, "upstox")

    def no_data_sessions(self, instrument_key: str, timeframe: str,
                         start: date, end: date) -> set[date]:
        with get_app_db(self.ctx).read() as conn:
            rows = conn.execute(
                "SELECT session_date FROM data_no_data WHERE user_id = ? AND instrument_key = ?"
                " AND timeframe = ? AND session_date BETWEEN ? AND ?",
                (self.ctx.user_id, instrument_key, timeframe, start.isoformat(), end.isoformat()),
            ).fetchall()
        return {date.fromisoformat(r[0]) for r in rows}

    # -- internals --------------------------------------------------------------

    def _resolve_range(self, key: str, timeframe: str, start: date | None,
                       end: date | None) -> tuple[date, date]:
        today = self._today()
        end = min(end or today, today)
        if start is None:
            stored = [s.info(key, timeframe) for s in self.read_stores]
            firsts = [date.fromisoformat(i.first_bar[:10]) for i in stored if i]
            start = min(firsts) if firsts else end - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        if is_fetchable(key):
            start = max(start, EARLIEST[Timeframe.parse(timeframe).unit])
        return start, end

    def _incomplete_recent(self, key: str, timeframe: str, needed: list[date],
                           present: set[date], stamps: pd.Series) -> set[date]:
        """Recent stored sessions that end before the close (or are still trading)."""
        if not needed:
            return set()
        today = self._today()
        recent = [d for d in needed[-RECHECK_SESSIONS:] if d in present]
        if not recent:
            return set()
        tf = Timeframe.parse(timeframe)
        if tf.unit not in ("minutes", "hours"):
            # A daily bar for today is only final after the close.
            if today in recent and self._now().time() < _close(key):
                return {today}
            return set()

        ts = pd.to_datetime(stamps, utc=True).dt.tz_convert(IST)
        last_per_day = ts.groupby(ts.dt.date).max()
        step = timedelta(minutes=bar_minutes(timeframe))
        out = set()
        for day in recent:
            last_bar = last_per_day.get(day)
            if last_bar is None:
                continue
            final_bar_start = datetime.combine(day, _close(key), tzinfo=IST) - step
            if last_bar.to_pydatetime() < final_bar_start:
                out.add(day)
        return out

    def _plan_coarse(self, plan: DataPlan) -> DataPlan:
        """Weekly and monthly bars: fetch the whole range unless it is covered."""
        infos = [i for s in self.read_stores
                 if (i := s.info(plan.instrument_key, plan.fetch_timeframe)) is not None]
        covered = infos and min(date.fromisoformat(i.first_bar[:10]) for i in infos) <= plan.start \
            and max(date.fromisoformat(i.last_bar[:10]) for i in infos) >= plan.end - timedelta(days=31)
        if covered or not plan.fetchable:
            return plan
        plan.sessions_missing = [plan.start, plan.end]
        plan.ranges = [(plan.start, plan.end)]
        plan.requests = 1
        return plan

    def _remember_no_data(self, key: str, timeframe: str, sessions: list[date]) -> None:
        stamp = now_iso()
        with get_app_db(self.ctx).connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO data_no_data (user_id, instrument_key, timeframe,"
                " session_date, checked_at) VALUES (?, ?, ?, ?, ?)",
                [(self.ctx.user_id, key, timeframe, d.isoformat(), stamp) for d in sessions],
            )


def merge_ranges(days: list[date], max_gap_days: int) -> list[tuple[date, date]]:
    """Sorted dates -> (first, last) ranges, joining runs less than `max_gap_days` apart."""
    ranges: list[tuple[date, date]] = []
    for day in sorted(days):
        if ranges and (day - ranges[-1][1]).days <= max_gap_days:
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ranges


def _dates(stamps: pd.Series) -> set[date]:
    return set(pd.to_datetime(stamps, utc=True).dt.tz_convert(IST).dt.date)


def _close(instrument_key: str) -> time:
    return cal.MCX_CLOSE if is_mcx(instrument_key) else cal.MARKET_CLOSE


def _is_fatal(exc: Exception) -> bool:
    """Errors that will fail every remaining request too (no token, no auth)."""
    text = str(exc).lower()
    return any(word in text for word in ("token", "unauthorized", "401", "403", "login"))


def _default_fetcher(instrument_key: str, timeframe: str, first: date, last: date, *,
                     include_today: bool, on_progress: Progress | None) -> pd.DataFrame:
    from backend.data.upstox_client import fetch_candles

    return fetch_candles(instrument_key, timeframe, first, last,
                         include_today=include_today, on_progress=on_progress)
