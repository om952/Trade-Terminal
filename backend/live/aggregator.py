"""Tick aggregation: the exchange streams trades, the engine consumes candles.

Upstox's market-data socket delivers ticks, not bars. The strategy engine was
built against bars and proven correct against bars, so the only honest way to run
it live is to build bars here — identically shaped and identically aligned to the
ones the backtest read out of Parquet — and hand those over.

Three things this has to get right, each of which is silent when wrong:

1. **Bucket alignment.** Upstox's stored 5-minute candles start at 09:15, not at
   09:00. Aligning live buckets to the wall clock instead of the session open
   yields bars offset three minutes from every bar the backtest ever saw, so
   indicators warm on different windows and live diverges from backtest for
   reasons that are near-impossible to find after the fact.

2. **Closing on time, not on the next tick.** A bar is complete when its window
   elapses, not when the next trade prints. Waiting for a tick means an illiquid
   instrument's 15:20 bar closes at 15:24 — after the square-off it was supposed
   to trigger.

3. **Volume.** Ticks carry *cumulative* day volume, so a bar's volume is a
   difference between consecutive bar ends, not a sum of the field. Summing it
   directly inflates every bar by roughly the whole day's turnover.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from backend.core.interfaces import Bar
from backend.data.upstox_client import Timeframe

log = logging.getLogger(__name__)

NSE_OPEN = time(9, 15)
NSE_CLOSE = time(15, 30)


@dataclass(frozen=True, slots=True)
class Tick:
    """One trade print off the feed.

    `cum_volume` is the exchange's running day total, which is what the socket
    actually sends. `ltq` (last traded quantity) is kept as a fallback for feeds
    that omit the cumulative field.
    """

    symbol: str
    timestamp: datetime          # exchange time, tz-aware IST
    ltp: float
    ltq: int = 0
    cum_volume: int | None = None
    oi: int | None = None


@dataclass(frozen=True, slots=True)
class ClosedBar:
    """A completed bar plus how it was built.

    `gap_filled` matters: it means no trade printed in that window and the bar
    was synthesized from the previous close. A strategy acting on a run of those
    is reacting to a stall in the feed, not to the market.
    """

    bar: Bar
    tick_count: int
    gap_filled: bool = False


@dataclass(slots=True)
class _Building:
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    ticks: int = 0
    ltq_sum: int = 0
    cum_volume: int | None = None
    oi: int | None = None


def bucket_start(
    ts: datetime,
    tf: Timeframe,
    session_start: time = NSE_OPEN,
    session_end: time = NSE_CLOSE,
) -> datetime | None:
    """The opening timestamp of the bar this tick belongs to.

    Returns None for ticks before the session opens — the pre-open auction is a
    different mechanism, and folding its prints into the 09:15 candle makes the
    first bar of every day disagree with stored history.

    Ticks at or after the close are clamped into the final bar rather than
    opening a stub that can never complete: the closing-auction print is part of
    the closing candle.
    """
    if tf.unit in ("days", "weeks", "months"):
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)

    if tf.unit not in ("minutes", "hours"):
        raise ValueError(f"Cannot aggregate live bars at unit {tf.unit!r}")

    step = _step(tf)
    anchor = ts.replace(
        hour=session_start.hour, minute=session_start.minute, second=0, microsecond=0
    )
    close_at = ts.replace(
        hour=session_end.hour, minute=session_end.minute, second=0, microsecond=0
    )

    if ts < anchor:
        return None

    if ts >= close_at:
        # Clamp to the last bucket that opens strictly before the close.
        elapsed = (close_at - anchor).total_seconds() - 1
    else:
        elapsed = (ts - anchor).total_seconds()

    buckets = int(elapsed // step.total_seconds())
    return anchor + buckets * step


def _step(tf: Timeframe) -> timedelta:
    if tf.unit == "minutes":
        return timedelta(minutes=tf.interval)
    if tf.unit == "hours":
        return timedelta(hours=tf.interval)
    return timedelta(days=tf.interval)


class BarAggregator:
    """Folds a tick stream into closed bars for one instrument.

    Bars leave here through exactly two doors: a tick that belongs to a later
    bucket (`on_tick`), or the clock passing the bucket's end (`flush_due`).
    Both are required. Ticks alone cannot close the last bar of a quiet stretch;
    the clock alone cannot know a bar's close price.
    """

    def __init__(
        self,
        symbol: str,
        timeframe: str | Timeframe,
        *,
        session_start: time = NSE_OPEN,
        session_end: time = NSE_CLOSE,
        fill_gaps: bool = True,
        grace_seconds: float = 1.0,
    ) -> None:
        self.symbol = symbol
        self.tf = Timeframe.parse(timeframe) if isinstance(timeframe, str) else timeframe
        self.session_start = session_start
        self.session_end = session_end
        self.fill_gaps = fill_gaps
        self.grace = timedelta(seconds=grace_seconds)
        self.step = _step(self.tf)

        self._open: _Building | None = None
        self._next_start: datetime | None = None   # bucket we expect to build next
        self._last_close: Decimal | None = None
        self._cum_at_last_bar: int = 0
        self._day: datetime | None = None
        self.gap_filled_count = 0
        self.rejected_ticks = 0

    # -- ingestion -----------------------------------------------------------

    def on_tick(self, tick: Tick) -> list[ClosedBar]:
        """Absorb a tick. Returns any bars this tick completed."""
        start = bucket_start(tick.timestamp, self.tf, self.session_start, self.session_end)
        if start is None:
            self.rejected_ticks += 1
            return []

        self._roll_day_if_needed(start)

        if self._open is None:
            self._begin(start, tick)
            return []

        if start < self._open.start:
            # Out-of-order print from a reconnect replay. Folding it into the
            # current bar would put a stale price in the wrong candle, so drop it
            # and say so — silent reordering is worse than a visible gap.
            self.rejected_ticks += 1
            log.warning(
                "%s: out-of-order tick %s lands before open bar %s, dropped",
                self.symbol, tick.timestamp, self._open.start,
            )
            return []

        if start == self._open.start:
            self._absorb(tick)
            return []

        closed = [self._finish()]
        closed.extend(self._synthesize_up_to(start))
        self._begin(start, tick)
        return closed

    def flush_due(self, now: datetime) -> list[ClosedBar]:
        """Close whatever the clock says is finished. Call this on a timer.

        Without this, a bar's close waits on the next trade, which in a thin
        instrument can be minutes — long enough to miss a square-off.
        """
        out: list[ClosedBar] = []

        if self._open is not None and now >= self._open.start + self.step + self.grace:
            out.append(self._finish())

        if self.fill_gaps:
            out.extend(self._synthesize_up_to(self._elapsed_bucket(now)))

        return out

    def force_close(self) -> ClosedBar | None:
        """Close the in-progress bar unconditionally. For session end / shutdown."""
        return self._finish() if self._open is not None else None

    # -- internals -----------------------------------------------------------

    def _roll_day_if_needed(self, start: datetime) -> None:
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        if self._day == day:
            return
        if self._day is not None:
            # New session: cumulative volume restarts at zero on the exchange,
            # and yesterday's close must not seed today's gap fills.
            self._open = None
            self._next_start = None
            self._last_close = None
        self._day = day
        self._cum_at_last_bar = 0

    def _begin(self, start: datetime, tick: Tick) -> None:
        price = Decimal(str(tick.ltp))
        self._open = _Building(
            start=start, open=price, high=price, low=price, close=price,
            ticks=1, ltq_sum=tick.ltq, cum_volume=tick.cum_volume, oi=tick.oi,
        )

    def _absorb(self, tick: Tick) -> None:
        assert self._open is not None
        price = Decimal(str(tick.ltp))
        b = self._open
        b.close = price
        if price > b.high:
            b.high = price
        if price < b.low:
            b.low = price
        b.ticks += 1
        b.ltq_sum += tick.ltq
        if tick.cum_volume is not None:
            b.cum_volume = tick.cum_volume
        if tick.oi is not None:
            b.oi = tick.oi

    def _finish(self) -> ClosedBar:
        assert self._open is not None
        b = self._open

        if b.cum_volume is not None:
            # Cumulative counters only ever rise; a fall means the feed reset
            # (reconnect, or a session boundary we did not see). Trust the raw
            # value in that case rather than emitting a negative volume.
            volume = b.cum_volume - self._cum_at_last_bar
            if volume < 0:
                volume = b.cum_volume
            self._cum_at_last_bar = b.cum_volume
        else:
            volume = b.ltq_sum

        bar = Bar(
            symbol=self.symbol,
            timestamp=b.start,
            open=b.open, high=b.high, low=b.low, close=b.close,
            volume=int(volume),
            oi=b.oi,
        )
        self._open = None
        self._next_start = b.start + self.step
        self._last_close = b.close
        return ClosedBar(bar=bar, tick_count=b.ticks, gap_filled=False)

    def _elapsed_bucket(self, now: datetime) -> datetime | None:
        """Start of the bucket currently in progress at `now`, grace applied."""
        return bucket_start(now - self.grace, self.tf, self.session_start, self.session_end)

    def _synthesize_up_to(self, target: datetime | None) -> list[ClosedBar]:
        """Emit flat bars for windows in which nothing traded.

        A strategy on a 5-minute chart expects a bar every five minutes. If the
        instrument simply did not trade, the alternatives are to emit nothing —
        and let the engine stall past its own square-off — or to emit a bar with
        no range and no volume. The second is chosen, and flagged, because a
        stalled engine holding a live position is the worse failure.

        These are counted and logged: a run of them means the instrument is too
        thin for this timeframe, and no backtest on it should be believed.
        """
        if not self.fill_gaps or target is None or self._next_start is None:
            return []
        if self._last_close is None:
            return []

        out: list[ClosedBar] = []
        session_close = target.replace(
            hour=self.session_end.hour, minute=self.session_end.minute,
            second=0, microsecond=0,
        )

        while self._next_start < target and self._next_start < session_close:
            price = self._last_close
            out.append(
                ClosedBar(
                    bar=Bar(
                        symbol=self.symbol, timestamp=self._next_start,
                        open=price, high=price, low=price, close=price,
                        volume=0, oi=None,
                    ),
                    tick_count=0,
                    gap_filled=True,
                )
            )
            self._next_start += self.step
            self.gap_filled_count += 1

        if out:
            log.warning(
                "%s: synthesized %d empty %s bar(s) — no trades printed",
                self.symbol, len(out), self.tf.label,
            )
        return out
