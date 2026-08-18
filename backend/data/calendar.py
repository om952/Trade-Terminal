"""Trading calendar and gap detection.

Gap detection is the whole point of this module. A backtest run over data with
silent holes produces confident, wrong numbers — the equity curve simply skips
the days that would have hurt. So before any dataset is trusted we check it
against the sessions that should exist.

Holidays are derived from the data itself where possible (a day on which no
instrument traded was a holiday) and cross-checked against a static list, since
NSE publishes holidays as a PDF rather than an API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pandas as pd

from backend.core.config import IST

log = logging.getLogger(__name__)

# NSE/BSE equity and F&O session, IST.
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# MCX runs an evening session.
MCX_OPEN = time(9, 0)
MCX_CLOSE = time(23, 30)

# NSE trading holidays. Update annually — NSE publishes these as a circular,
# not an endpoint, so there is nothing to sync against.
NSE_HOLIDAYS: dict[int, list[str]] = {
    2022: ["01-26", "03-01", "03-18", "04-14", "04-15", "05-03", "08-09", "08-15",
           "08-31", "10-05", "10-24", "10-26", "11-08"],
    2023: ["01-26", "03-07", "03-30", "04-04", "04-07", "04-14", "05-01", "06-29",
           "08-15", "09-19", "10-02", "10-24", "11-14", "11-27", "12-25"],
    2024: ["01-22", "01-26", "03-08", "03-25", "03-29", "04-11", "04-17", "05-01",
           "05-20", "06-17", "07-17", "08-15", "10-02", "11-01", "11-15", "12-25"],
    2025: ["02-26", "03-14", "03-31", "04-10", "04-14", "04-18", "05-01", "08-15",
           "08-27", "10-02", "10-21", "10-22", "11-05", "12-25"],
    2026: ["01-26", "03-04", "03-21", "03-31", "04-01", "04-03", "04-14", "05-01",
           "08-15", "09-14", "10-02", "10-20", "11-11", "12-25"],
}


def holidays(year: int) -> set[date]:
    out = set()
    for md in NSE_HOLIDAYS.get(year, []):
        month, day = md.split("-")
        out.add(date(year, int(month), int(day)))
    return out


# --------------------------------------------------------------------------
# Data-derived calendar
# --------------------------------------------------------------------------
# The static list above will always drift — NSE announces closures like the
# 2024-11-20 election holiday by circular, with no feed to sync from. But we
# have a better authority already: real index history. If NIFTY did not print a
# daily bar on a weekday, the market was shut. That makes the calendar
# self-maintaining, and it corrects itself every time history is refreshed.

_ANCHOR = "NSE_INDEX|Nifty 50"
_derived_cache: set[date] | None = None


def derived_sessions(refresh: bool = False) -> set[date]:
    """Actual trading days observed in stored NIFTY daily history.

    Returns an empty set when no history is stored, which callers treat as
    "fall back to the static holiday list".
    """
    global _derived_cache
    if _derived_cache is not None and not refresh:
        return _derived_cache

    try:
        from backend.data.store import read_candles

        df = read_candles(_ANCHOR, "1d")
        _derived_cache = set() if df.empty else set(df["timestamp"].dt.date)
    except Exception as exc:  # storage not initialized yet
        log.debug("Could not derive calendar from stored data: %s", exc)
        _derived_cache = set()

    return _derived_cache


def derived_range() -> tuple[date, date] | None:
    """The span the derived calendar can speak to. None if no data."""
    sessions = derived_sessions()
    return (min(sessions), max(sessions)) if sessions else None


def is_trading_day(day: date, use_derived: bool = True) -> bool:
    """True if the market traded on `day`.

    Prefers observed history where it covers the date, since that is ground
    truth. Outside that span it falls back to weekday plus the static list.
    """
    if day.weekday() >= 5:  # Sat/Sun
        return False

    if use_derived:
        span = derived_range()
        if span and span[0] <= day <= span[1]:
            return day in derived_sessions()

    return day not in holidays(day.year)


def trading_days(start: date, end: date, use_derived: bool = True) -> list[date]:
    days, cursor = [], start
    while cursor <= end:
        if is_trading_day(cursor, use_derived=use_derived):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def unlisted_holidays(start: date, end: date) -> list[date]:
    """Weekdays the market was shut that the static list does not know about.

    Run this after refreshing index history to find entries worth adding to
    NSE_HOLIDAYS, so the fallback path improves over time too.
    """
    sessions = derived_sessions()
    if not sessions:
        return []

    out, cursor = [], max(start, min(sessions))
    last = min(end, max(sessions))
    while cursor <= last:
        if cursor.weekday() < 5 and cursor not in sessions and cursor not in holidays(cursor.year):
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def expected_bars_per_day(timeframe: str, mcx: bool = False) -> int:
    """How many bars a complete session should contain.

    Daily and above are one bar per session by definition.
    """
    from backend.data.upstox_client import Timeframe

    tf = Timeframe.parse(timeframe)
    if tf.unit in ("days", "weeks", "months"):
        return 1

    open_t, close_t = (MCX_OPEN, MCX_CLOSE) if mcx else (MARKET_OPEN, MARKET_CLOSE)
    minutes = (
        datetime.combine(date.today(), close_t) - datetime.combine(date.today(), open_t)
    ).seconds // 60

    per_bar = tf.interval * {"minutes": 1, "hours": 60}[tf.unit]
    return max(1, minutes // per_bar)


@dataclass(frozen=True, slots=True)
class GapReport:
    instrument_key: str
    timeframe: str
    first_bar: date | None
    last_bar: date | None
    total_bars: int
    expected_sessions: int
    present_sessions: int
    missing_sessions: list[date]
    thin_sessions: list[tuple[date, int, int]]  # (day, actual, expected)

    @property
    def is_clean(self) -> bool:
        return not self.missing_sessions and not self.thin_sessions

    @property
    def coverage_pct(self) -> float:
        if self.expected_sessions == 0:
            return 0.0
        return 100.0 * self.present_sessions / self.expected_sessions

    def summary(self) -> str:
        status = "CLEAN" if self.is_clean else "GAPS"
        return (
            f"[{status}] {self.instrument_key} {self.timeframe}: "
            f"{self.total_bars:,} bars, {self.present_sessions}/{self.expected_sessions} "
            f"sessions ({self.coverage_pct:.1f}%), "
            f"{len(self.missing_sessions)} missing, {len(self.thin_sessions)} thin"
        )


def find_gaps(
    df: pd.DataFrame,
    instrument_key: str,
    timeframe: str,
    start: date | None = None,
    end: date | None = None,
    mcx: bool = False,
    thin_threshold: float = 0.8,
) -> GapReport:
    """Compare stored bars against the sessions that should exist.

    A "thin" session has bars but fewer than `thin_threshold` of the expected
    count — usually a partial fetch or a genuine half-day. Both are worth
    surfacing, because silently trading on a half-populated day is how a
    backtest and live diverge without anyone noticing.
    """
    if df.empty:
        return GapReport(instrument_key, timeframe, None, None, 0, 0, 0, [], [])

    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(IST)
    days = ts.dt.date
    present = set(days)

    first, last = min(present), max(present)
    start = start or first
    end = end or last

    expected = [d for d in trading_days(start, end)]
    missing = sorted(set(expected) - present)

    thin: list[tuple[date, int, int]] = []
    exp_per_day = expected_bars_per_day(timeframe, mcx=mcx)
    if exp_per_day > 1:
        counts = days.value_counts()
        for day in sorted(present):
            actual = int(counts[day])
            if actual < exp_per_day * thin_threshold:
                thin.append((day, actual, exp_per_day))

    return GapReport(
        instrument_key=instrument_key,
        timeframe=timeframe,
        first_bar=first,
        last_bar=last,
        total_bars=len(df),
        expected_sessions=len(expected),
        present_sessions=len(present & set(expected)),
        missing_sessions=missing,
        thin_sessions=thin,
    )
