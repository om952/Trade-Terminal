"""Where live bars come from. Three implementations behind one protocol.

The engine wants closed bars. There is more than one honest way to produce them,
and they trade off differently:

  * `RestPollingSource` — polls Upstox's intraday-candle endpoint. The bars are
    built by Upstox, from the same pipeline that produced the historical bars the
    backtest read, so a paper session and a backtest of the same day see byte-
    identical candles. Latency is bounded by the poll interval. This is the
    default for paper trading, and the reason is correctness, not simplicity:
    aggregating ticks ourselves introduces a second bar-construction implementation,
    and a second implementation is a second set of bugs that only show up as an
    unexplained gap between backtest and live.

  * `TickAggregatingSource` — the WebSocket, folded into bars by `BarAggregator`.
    Lower latency, needed for Phase 7 where a stop must trigger inside a bar
    rather than at its close. Carries the divergence risk described above, which
    is why it is not the paper-trading default.

  * `ReplaySource` — stored candles, replayed at a chosen speed. Runs the whole
    live path with no network and no market, which is what makes the recovery and
    determinism gates testable at all.

All three yield `ClosedBar`, and a bar is emitted only once its window has fully
elapsed. That last rule is load-bearing: Upstox's intraday response includes the
*current, still-forming* candle, and handing that to the engine would let a
strategy decide on a close price that has not happened yet — the same lookahead
the backtest is carefully built to make impossible.
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Iterator, Protocol

import pandas as pd

from backend.core.config import IST
from backend.core.interfaces import Bar
from backend.data.upstox_client import Timeframe
from backend.live.aggregator import NSE_CLOSE, NSE_OPEN, BarAggregator, ClosedBar, _step

log = logging.getLogger(__name__)


@dataclass
class FeedStatus:
    connected: bool = False
    bars_emitted: int = 0
    last_bar_at: datetime | None = None
    last_poll_at: datetime | None = None
    reconnects: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "connected": self.connected,
            "bars_emitted": self.bars_emitted,
            "last_bar_at": self.last_bar_at.isoformat() if self.last_bar_at else None,
            "last_poll_at": self.last_poll_at.isoformat() if self.last_poll_at else None,
            "reconnects": self.reconnects,
            "errors": self.errors[-5:],
        }


class BarSource(Protocol):
    """Yields closed bars until the session ends."""

    status: FeedStatus

    def bars(self) -> Iterator[ClosedBar]: ...
    def stop(self) -> None: ...


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

class ReplaySource:
    """Replays stored candles through the live path.

    `speed` is a multiplier on real time: 0 runs flat out (tests), 1 runs at the
    speed the day actually happened, 60 compresses a session into six minutes.
    The engine cannot tell the difference — it sees the same bars in the same
    order either way, which is the point.
    """

    def __init__(self, df: pd.DataFrame, symbol: str, speed: float = 0.0) -> None:
        self._df = df.reset_index(drop=True)
        self.symbol = symbol
        self.speed = speed
        self.status = FeedStatus(connected=True)
        self._stopped = False
        self._skip_through: datetime | None = None

    def prime(self, through: datetime | None) -> None:
        """Skip bars up to and including `through`.

        A live feed resumes correctly on its own — the market only moves forward,
        so a restarted session simply receives the next bar. A replay has no such
        arrow of time: without this it would start the window over and re-trade
        every bar the journal already recorded, doubling the session's history.
        """
        if through is None:
            return
        if self._skip_through is None or through > self._skip_through:
            self._skip_through = through

    def bars(self) -> Iterator[ClosedBar]:
        previous: datetime | None = None
        for row in self._df.itertuples(index=False):
            if self._stopped:
                return

            ts = pd.Timestamp(row.timestamp)
            ts = ts.tz_localize(IST) if ts.tz is None else ts.tz_convert(IST)
            ts = ts.to_pydatetime()

            if self._skip_through is not None and ts <= self._skip_through:
                continue

            if self.speed > 0 and previous is not None:
                _time.sleep((ts - previous).total_seconds() / self.speed)
            previous = ts

            bar = Bar(
                symbol=self.symbol,
                timestamp=ts,
                open=Decimal(str(row.open)),
                high=Decimal(str(row.high)),
                low=Decimal(str(row.low)),
                close=Decimal(str(row.close)),
                volume=int(row.volume),
                oi=int(getattr(row, "oi", 0) or 0) or None,
            )
            self.status.bars_emitted += 1
            self.status.last_bar_at = ts
            yield ClosedBar(bar=bar, tick_count=1)

        self.status.connected = False

    def stop(self) -> None:
        self._stopped = True


# ---------------------------------------------------------------------------
# REST polling
# ---------------------------------------------------------------------------

class RestPollingSource:
    """Polls Upstox intraday candles and emits each bar once it has closed.

    Gap recovery is inherent rather than bolted on: every poll returns the whole
    session so far, so a dropped connection heals on the next successful call
    with no special path — and no risk that the special path is the one nobody
    tested. A sixty-second outage costs sixty seconds of latency, not sixty
    seconds of missing bars.
    """

    def __init__(
        self,
        instrument_key: str,
        timeframe: str | Timeframe,
        *,
        poll_seconds: float = 5.0,
        session_end: time = NSE_CLOSE,
        publish_lag: float = 2.0,
        run_until_session_end: bool = True,
        clock=None,
    ) -> None:
        self.instrument_key = instrument_key
        self.tf = Timeframe.parse(timeframe) if isinstance(timeframe, str) else timeframe
        self.poll_seconds = poll_seconds
        self.session_end = session_end
        # Upstox publishes a candle a moment after its window shuts. Emitting at
        # the exact boundary races that and yields a bar missing its last prints.
        self.publish_lag = timedelta(seconds=publish_lag)
        self.run_until_session_end = run_until_session_end
        self.step = _step(self.tf)
        self.status = FeedStatus()
        self._clock = clock or (lambda: datetime.now(IST))
        self._emitted_through: datetime | None = None
        self._stopped = False

    def prime(self, through: datetime | None) -> None:
        """Suppress bars up to and including `through`.

        Set from what warmup already fed the indicators, so resuming mid-session
        does not replay bars the engine has already acted on.
        """
        self._emitted_through = through

    def bars(self) -> Iterator[ClosedBar]:
        from backend.data.upstox_client import fetch_intraday

        while not self._stopped:
            now = self._clock()
            try:
                df = fetch_intraday(self.instrument_key, self.tf)
                self.status.connected = True
            except Exception as exc:  # network, auth, 5xx — all retryable
                self.status.connected = False
                self.status.reconnects += 1
                self.status.errors.append(f"{now:%H:%M:%S} poll failed: {exc}")
                log.warning("Poll failed (%s); retrying in %.0fs", exc, self.poll_seconds)
                if self._sleep(self.poll_seconds):
                    return
                continue

            self.status.last_poll_at = now
            yield from self._emit_closed(df, now)

            if self.run_until_session_end and self._session_over(now):
                log.info("Session end reached; polling stops")
                self.status.connected = False
                return

            if self._sleep(self.poll_seconds):
                return

    def _emit_closed(self, df: pd.DataFrame, now: datetime) -> Iterator[ClosedBar]:
        if df.empty:
            return

        for row in df.sort_values("timestamp").itertuples(index=False):
            ts = pd.Timestamp(row.timestamp)
            ts = ts.tz_localize(IST) if ts.tz is None else ts.tz_convert(IST)
            ts = ts.to_pydatetime()

            if self._emitted_through is not None and ts <= self._emitted_through:
                continue
            # The window must be fully behind us. Upstox returns the forming
            # candle too, and acting on it is acting on a close that has not
            # happened.
            if ts + self.step + self.publish_lag > now:
                continue

            bar = Bar(
                symbol=self.instrument_key,
                timestamp=ts,
                open=Decimal(str(row.open)),
                high=Decimal(str(row.high)),
                low=Decimal(str(row.low)),
                close=Decimal(str(row.close)),
                volume=int(row.volume),
                oi=int(getattr(row, "oi", 0) or 0) or None,
            )
            self._emitted_through = ts
            self.status.bars_emitted += 1
            self.status.last_bar_at = ts
            yield ClosedBar(bar=bar, tick_count=1)

    def _session_over(self, now: datetime) -> bool:
        close_at = now.replace(
            hour=self.session_end.hour, minute=self.session_end.minute,
            second=0, microsecond=0,
        )
        return now >= close_at + self.step + self.publish_lag

    def _sleep(self, seconds: float) -> bool:
        """Sleep in slices so `stop()` is honoured promptly. True if stopped."""
        deadline = _time.monotonic() + seconds
        while _time.monotonic() < deadline:
            if self._stopped:
                return True
            _time.sleep(min(0.25, deadline - _time.monotonic()))
        return self._stopped

    def stop(self) -> None:
        self._stopped = True


# ---------------------------------------------------------------------------
# WebSocket ticks -> bars
# ---------------------------------------------------------------------------

class TickAggregatingSource:
    """Folds a live tick stream into bars.

    Ticks arrive on a background thread and land in a queue; bars are produced on
    the caller's thread. The trading loop therefore stays single-threaded, which
    is deliberate — a decision loop that can be re-entered from a socket callback
    is a decision loop that can act on half-applied state.
    """

    def __init__(
        self,
        tick_stream,                       # UpstoxTickStream or any .ticks() iterable
        symbol: str,
        timeframe: str | Timeframe,
        *,
        session_start: time = NSE_OPEN,
        session_end: time = NSE_CLOSE,
        fill_gaps: bool = True,
        clock=None,
    ) -> None:
        self.stream = tick_stream
        self.aggregator = BarAggregator(
            symbol, timeframe,
            session_start=session_start, session_end=session_end, fill_gaps=fill_gaps,
        )
        self.status = FeedStatus()
        self._clock = clock or (lambda: datetime.now(IST))
        self._stopped = False

    def bars(self) -> Iterator[ClosedBar]:
        import queue as _queue

        inbox: _queue.Queue = _queue.Queue()
        self.stream.start(inbox)
        self.status.connected = True

        try:
            while not self._stopped:
                try:
                    tick = inbox.get(timeout=0.5)
                except _queue.Empty:
                    tick = None

                closed: list[ClosedBar] = []
                if tick is None:
                    # No tick — the clock still has to be able to close a bar.
                    closed = self.aggregator.flush_due(self._clock())
                elif isinstance(tick, Exception):
                    self.status.connected = False
                    self.status.reconnects += 1
                    self.status.errors.append(str(tick))
                    log.warning("Feed error: %s", tick)
                else:
                    closed = self.aggregator.on_tick(tick)
                    self.status.connected = True

                for bar in closed:
                    self.status.bars_emitted += 1
                    self.status.last_bar_at = bar.bar.timestamp
                    yield bar
        finally:
            self.stream.stop()
            final = self.aggregator.force_close()
            if final is not None:
                yield final
            self.status.connected = False

    def stop(self) -> None:
        self._stopped = True
