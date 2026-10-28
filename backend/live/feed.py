"""LiveFeed — a DataFeed backed by a live bar source.

This is the class that makes paper trading *be* the backtest with a different
input. It satisfies the same `DataFeed` protocol the historical feed does, so the
engine above it is unchanged and unaware.

The hard part is not `stream()`. It is `warmup()`.

An EMA(200) started from nothing needs 200 bars before it means anything, and
until then the strategy is trading on a number that is merely converging. A
backtest gets this for free — the history is right there. A live session started
at 11:47 does not, and the failure is silent: the strategy trades, the numbers
look plausible, and they are simply not the numbers the backtest computed.

So warmup does three things, in order, and all three are required:

  1. Ensures stored history reaches the last completed session, fetching the gap
     if it does not. Priming indicators across a month-wide hole in the data
     produces values that correspond to no real series.
  2. Appends today's already-elapsed bars, so a session started mid-day resumes
     with the same indicator state it would have had running since the open.
  3. Tells the source how far it has primed, so the first live bar is genuinely
     new rather than one the engine already acted on.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterator

import pandas as pd

from backend.core.config import IST
from backend.core.interfaces import Bar
from backend.data import calendar as cal
from backend.data.upstox_client import Timeframe
from backend.live.sources import BarSource

log = logging.getLogger(__name__)


def _to_bars(df: pd.DataFrame, symbol: str) -> list[Bar]:
    out: list[Bar] = []
    for row in df.itertuples(index=False):
        ts = pd.Timestamp(row.timestamp)
        ts = ts.tz_localize(IST) if ts.tz is None else ts.tz_convert(IST)
        out.append(
            Bar(
                symbol=symbol,
                timestamp=ts.to_pydatetime(),
                open=Decimal(str(row.open)),
                high=Decimal(str(row.high)),
                low=Decimal(str(row.low)),
                close=Decimal(str(row.close)),
                volume=int(row.volume),
                oi=int(getattr(row, "oi", 0) or 0) or None,
            )
        )
    return out


class LiveFeed:
    """DataFeed over a live source, with history-backed indicator priming."""

    def __init__(
        self,
        instrument_key: str,
        timeframe: str | Timeframe,
        source: BarSource,
        *,
        auto_fetch: bool = True,
        include_today: bool = True,
        history_frame: pd.DataFrame | None = None,
        history_before: datetime | None = None,
    ) -> None:
        self.instrument_key = instrument_key
        self.tf = Timeframe.parse(timeframe) if isinstance(timeframe, str) else timeframe
        self.source = source
        self.auto_fetch = auto_fetch
        self.include_today = include_today
        # Explicit history bypasses both the store and the network. Used to
        # rehearse a past session offline, and by the tests, which must be able
        # to exercise the whole live path without a token or a market.
        self._history_frame = history_frame
        # Where the source's own bars begin. Warm-up must stop short of it, or
        # the engine would be primed on bars it is about to be handed again.
        self._history_before = history_before
        self.warmup_frame: pd.DataFrame | None = None
        self.primed_through: datetime | None = None

    # -- DataFeed ------------------------------------------------------------

    def warmup(self, symbol: str, bars: int, through: datetime | None = None) -> list[Bar]:
        """Prime indicators with history ending at `through` (default: the source's start).

        On a resume, `through` is the last bar the journal recorded. Warming up
        to the window start instead would leave the indicators blind to every bar
        the crashed run already processed — the session would carry on from the
        right position with the wrong indicator state, and diverge from an
        uninterrupted run for no visible reason.
        """
        frame = self.history(bars, through=through)
        self.warmup_frame = frame
        if not frame.empty:
            last = pd.Timestamp(frame["timestamp"].iloc[-1])
            last = last.tz_localize(IST) if last.tz is None else last.tz_convert(IST)
            self.primed_through = last.to_pydatetime()
            prime = getattr(self.source, "prime", None)
            if callable(prime):
                prime(self.primed_through)
        return _to_bars(frame, symbol or self.instrument_key)

    def stream(self) -> Iterator[Bar]:
        for closed in self.source.bars():
            yield closed.bar

    # -- history -------------------------------------------------------------

    def history(self, bars: int, through: datetime | None = None) -> pd.DataFrame:
        """The last `bars` closed candles ending at `through`, or at the source's start."""
        from backend.data.store import read_candles

        if self._history_frame is not None:
            frame = self._history_frame.reset_index(drop=True)
            frame = self._clip(frame, through)
            return frame.tail(bars).reset_index(drop=True) if bars > 0 else frame

        stored = read_candles(self.instrument_key, self.tf.label)

        if self.auto_fetch:
            stored = self._ensure_current(stored)

        frames = [stored] if not stored.empty else []

        if self.include_today:
            today_frame = self._today()
            if not today_frame.empty:
                frames.append(today_frame)

        if not frames:
            raise RuntimeError(
                f"No history for {self.instrument_key} at {self.tf.label}. "
                f"Indicators cannot be primed, so the session would trade on "
                f"values the backtest never produced. Fetch it first:\n"
                f"    python -m backend.data.ingest fetch {self.instrument_key} "
                f"-t {self.tf.label}"
            )

        combined = pd.concat(frames, ignore_index=True)
        combined["timestamp"] = pd.to_datetime(combined["timestamp"], utc=True).dt.tz_convert(IST)
        combined = (
            combined.sort_values("timestamp")
            .drop_duplicates("timestamp", keep="last")
            .reset_index(drop=True)
        )
        combined = self._clip(combined, through)
        return combined.tail(bars).reset_index(drop=True) if bars > 0 else combined

    def _clip(self, frame: pd.DataFrame, through: datetime | None) -> pd.DataFrame:
        """Trim history to what the engine has not been handed yet.

        `through` (inclusive) wins when resuming — it is the last bar the journal
        saw. Otherwise stop strictly before the source's first bar, so warm-up and
        the live stream do not overlap.
        """
        if frame.empty:
            return frame
        stamps = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(IST)
        if through is not None:
            return frame[stamps <= pd.Timestamp(through)].reset_index(drop=True)
        if self._history_before is not None:
            return frame[stamps < pd.Timestamp(self._history_before)].reset_index(drop=True)
        return frame

    def _ensure_current(self, stored: pd.DataFrame) -> pd.DataFrame:
        """Fetch forward to the last completed session if storage lags behind.

        Priming an EMA across a gap gives a value that belongs to no real series —
        neither the pre-gap one nor the post-gap one — so a stale store has to be
        repaired before it is used, not merely warned about.
        """
        from backend.data.store import read_candles, write_download
        from backend.data.upstox_client import fetch_candles

        today = datetime.now(IST).date()
        target = self._last_completed_session(today)

        if not stored.empty:
            last = pd.Timestamp(stored["timestamp"].iloc[-1])
            last_day = last.date()
            if last_day >= target:
                return stored
            start = last_day + timedelta(days=1)
        else:
            span = 400 if self.tf.unit in ("days", "weeks", "months") else 60
            start = today - timedelta(days=span)

        log.info(
            "History for %s %s ends %s, need %s — fetching the gap",
            self.instrument_key, self.tf.label,
            stored["timestamp"].iloc[-1].date() if not stored.empty else "never", target,
        )
        try:
            fresh = fetch_candles(
                self.instrument_key, self.tf, start, target, include_today=False
            )
        except Exception as exc:
            log.error("Gap fetch failed: %s", exc)
            return stored

        if fresh.empty:
            return stored

        write_download(self.instrument_key, self.tf.label, fresh)
        return read_candles(self.instrument_key, self.tf.label)

    def _today(self) -> pd.DataFrame:
        """Today's elapsed bars, so a mid-session start is not a cold start."""
        from backend.data.upstox_client import fetch_intraday

        try:
            return fetch_intraday(self.instrument_key, self.tf)
        except Exception as exc:
            log.warning("Could not fetch today's bars for priming: %s", exc)
            return pd.DataFrame()

    @staticmethod
    def _last_completed_session(today: date) -> date:
        day = today - timedelta(days=1)
        for _ in range(15):
            if cal.is_trading_day(day):
                return day
            day -= timedelta(days=1)
        return today - timedelta(days=1)
