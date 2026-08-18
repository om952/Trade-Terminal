"""StrategyEngine — the event loop. Mode-agnostic by construction.

The engine consumes one bar at a time and emits orders. It never knows whether
those bars came from a Parquet file or a WebSocket, or whether the orders go to a
simulator or to Upstox. That indifference is the whole reliability argument:
backtest, paper and live run *this* code, differing only in what is wired in.

Bar-close semantics
-------------------
Signals are evaluated on the CLOSE of a bar. Orders fill at the NEXT bar. The
engine physically cannot act on the bar it is evaluating, because the fill price
comes from a bar it has not been given yet.

This costs one bar of latency versus the naive approach, and that cost is real —
but the naive approach is not a faster strategy, it is a strategy that trades on
information it did not have. Every backtest that fills at the signal bar's close
is reporting returns nobody could have earned.

Intrabar exits are the one exception. A stop loss is a resting order at the
exchange, so it can trigger inside a bar. The engine checks the bar's high/low
against stop and target levels, and when both are touched in one bar it assumes
the stop hit first. That is pessimistic and occasionally wrong, but the
alternative — assuming the target — makes every backtest optimistic in exactly
the situation where the truth matters most.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Iterator

import pandas as pd

from backend.core.config import IST
from backend.core.interfaces import Side
from backend.indicators.base import PriceBar
from backend.strategy import conditions
from backend.strategy.indicator_set import (
    IndicatorSet,
    StreamingIndicatorSet,
    VectorizedIndicatorSet,
)
from backend.strategy.risk import Block, RiskGuard
from backend.strategy.schema import ExitType, Leg, Strategy
from backend.strategy.state import ClosedTrade, Portfolio, Position, TradeReason

log = logging.getLogger(__name__)

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """What the engine wants done. A broker turns this into a fill.

    `same_bar` separates the two genuinely different order types:

      * False — a decision made from the closing state of this bar. It cannot
        fill until the next bar exists. Signal entries and signal exits.
      * True  — a resting order already at the exchange when the bar traded, so
        it fills inside it at `limit_price`. Stops, targets, square-off.

    Collapsing these two into one rule is the most common way a backtest becomes
    dishonest: fill everything same-bar and entries become clairvoyant; fill
    everything next-bar and stops stop working.
    """

    leg_id: str
    symbol: str
    side: Side
    quantity: int
    reason: TradeReason
    bar_index: int
    timestamp: datetime
    limit_price: Decimal | None = None   # None means market
    same_bar: bool = False


@dataclass(frozen=True, slots=True)
class BarEvent:
    """Everything that happened on one bar. Journaled for replay."""

    index: int
    timestamp: datetime
    bar: PriceBar
    entry_signal: bool
    exit_signal: bool
    intents: tuple[OrderIntent, ...]
    blocked: Block = Block.NONE
    note: str = ""


class Fill(dict):
    """A filled order, as reported back by a broker. Loose by design so both the
    simulator and a real broker adapter can report the same shape."""


@dataclass
class EngineResult:
    strategy_id: str
    strategy_hash: str
    portfolio: Portfolio
    events: list[BarEvent] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    # Set when the strategy has option legs: which contracts were traded and how
    # they were priced. Results are meaningless without it, so it travels with them.
    option_book: object | None = None

    @property
    def trades(self) -> list[ClosedTrade]:
        return self.portfolio.closed_trades


class StrategyEngine:
    """Drives one strategy over a stream of bars."""

    def __init__(
        self,
        strategy: Strategy,
        portfolio: Portfolio | None = None,
        risk: RiskGuard | None = None,
        lot_size: int = 1,
    ) -> None:
        self.strategy = strategy
        self.portfolio = portfolio or Portfolio()
        self.risk = risk or RiskGuard.from_settings(strategy.risk)
        self.lot_size = lot_size

        self._inline = conditions.collect_inline(strategy.entry) + conditions.collect_inline(
            strategy.exit
        )
        self._current_day: date | None = None
        self._bar_index = -1
        self._pending: list[OrderIntent] = []
        self._warmup = 0

    # -- helpers -------------------------------------------------------------

    @property
    def symbol(self) -> str:
        return self.strategy.instrument.symbol

    def _quantity(self, leg: Leg) -> int:
        return leg.lots * self.lot_size

    @property
    def _is_intraday_timeframe(self) -> bool:
        """Whether bars are finer than one day.

        Session windows and square-off only mean something intraday. A daily bar
        carries a time-of-day of 00:00:00, so applying a 09:15-15:20 window to it
        rejects every bar and the strategy silently takes zero trades while its
        signals fire normally — which reads as "found no setups" rather than as
        a configuration error.
        """
        from backend.data.upstox_client import Timeframe

        return Timeframe.parse(self.strategy.timeframe).unit in ("minutes", "hours")

    def _in_session(self, ts: datetime) -> bool:
        if not self._is_intraday_timeframe:
            return True  # the bar *is* the session
        t = ts.timetz().replace(tzinfo=None)
        return self.strategy.session.start <= t <= self.strategy.session.end

    def _past_square_off(self, ts: datetime) -> bool:
        so = self.strategy.session.square_off
        if so is None or not self._is_intraday_timeframe:
            return False
        return ts.timetz().replace(tzinfo=None) >= so

    # -- lifecycle -----------------------------------------------------------

    def _roll_day_if_needed(self, ts: datetime) -> bool:
        day = ts.date()
        if self._current_day == day:
            return False
        self._current_day = day
        self.portfolio.start_new_day()
        self.risk.start_new_day()
        return True

    # -- exits ---------------------------------------------------------------

    def _set_exit_levels(self, position: Position, leg: Leg, indicators: IndicatorSet) -> None:
        """Compute stop/target once at entry, from the entry bar's values."""
        entry = position.average_price
        direction = Decimal(position.direction)

        position.stop_price = self._level(leg.stop_loss, entry, direction, indicators, adverse=True)
        position.target_price = self._level(leg.target, entry, direction, indicators, adverse=False)
        position.trail_anchor = entry
        position.trail_armed = False

    def _level(
        self,
        rule,
        entry: Decimal,
        direction: Decimal,
        indicators: IndicatorSet,
        adverse: bool,
    ) -> Decimal | None:
        if rule is None:
            return None

        if rule.type is ExitType.PERCENT:
            offset = entry * Decimal(str(rule.value)) / Decimal(100)
        elif rule.type is ExitType.POINTS:
            offset = Decimal(str(rule.value))
        else:  # atr_multiple
            atr = indicators.value(rule.ref)
            if atr != atr:  # NaN
                return None
            offset = Decimal(str(atr)) * Decimal(str(rule.value))

        sign = -direction if adverse else direction
        return entry + sign * offset

    def _check_intrabar_exit(
        self, position: Position, bar: PriceBar
    ) -> tuple[TradeReason, Decimal] | None:
        """Did a resting stop or target trigger inside this bar?

        When both levels sit inside the bar's range we cannot know from OHLC
        alone which came first, so we assume the stop. Pessimistic on purpose.
        """
        if not position.is_open:
            return None

        high, low = Decimal(str(bar.high)), Decimal(str(bar.low))
        long_ = position.is_long

        stop_hit = position.stop_price is not None and (
            low <= position.stop_price if long_ else high >= position.stop_price
        )
        target_hit = position.target_price is not None and (
            high >= position.target_price if long_ else low <= position.target_price
        )

        if stop_hit:
            return TradeReason.STOP_LOSS, position.stop_price  # type: ignore[return-value]
        if target_hit:
            return TradeReason.TARGET, position.target_price  # type: ignore[return-value]
        return None

    def _update_trail(self, position: Position, leg: Leg, bar: PriceBar) -> None:
        """Ratchet the trailing stop. Only ever moves in the favourable direction."""
        rule = leg.trail
        if rule is None or not position.is_open:
            return

        long_ = position.is_long
        extreme = Decimal(str(bar.high if long_ else bar.low))

        if position.trail_anchor is None:
            position.trail_anchor = extreme
        elif (long_ and extreme > position.trail_anchor) or (
            not long_ and extreme < position.trail_anchor
        ):
            position.trail_anchor = extreme

        entry = position.average_price
        if entry == 0:
            return

        move_pct = abs(position.trail_anchor - entry) / entry * Decimal(100)
        favourable = (position.trail_anchor > entry) if long_ else (position.trail_anchor < entry)
        if not (favourable and move_pct >= Decimal(str(rule.activate_at))):
            return

        position.trail_armed = True

        if rule.type is ExitType.PERCENT:
            offset = position.trail_anchor * Decimal(str(rule.value)) / Decimal(100)
        elif rule.type is ExitType.POINTS:
            offset = Decimal(str(rule.value))
        else:
            return  # atr trail is set at entry only

        candidate = position.trail_anchor - offset if long_ else position.trail_anchor + offset

        if position.stop_price is None:
            position.stop_price = candidate
        elif (long_ and candidate > position.stop_price) or (
            not long_ and candidate < position.stop_price
        ):
            position.stop_price = candidate

    # -- the loop ------------------------------------------------------------

    def on_bar(
        self,
        index: int,
        ts: datetime,
        bar: PriceBar,
        indicators: IndicatorSet,
        exec_bars: dict[str, PriceBar] | None = None,
    ) -> BarEvent:
        """Process one closed bar and return the intents it produced.

        Order matters here:
          1. roll the day if needed
          2. resolve exits (intrabar stops/targets, then signals)
          3. apply square-off
          4. consider a new entry

        Exits before entries means a stop-out and a fresh entry can happen on the
        same bar, which is what a real trader would do — but the entry is still
        filled on the NEXT bar.

        `exec_bars` separates *where the signal comes from* from *what the position
        is actually in*. An option strategy reads its EMA and RSI off the index but
        holds a contract worth a few hundred rupees, so its stop, target and trail
        have to be measured against the premium. Passing the index bar there would
        compare a 30%-of-premium stop against a 55,000-point instrument and never
        trigger. Legs without an entry fall back to `bar`, so equity and futures
        strategies are unaffected.
        """
        exec_bars = exec_bars or {}
        self._bar_index = index
        self._roll_day_if_needed(ts)

        intents: list[OrderIntent] = []
        blocked = Block.NONE
        note = ""

        exit_signal = (
            conditions.evaluate(self.strategy.exit, indicators) if self.strategy.exit else False
        )
        entry_signal = conditions.evaluate(self.strategy.entry, indicators)

        # --- forced flatten from risk limits --------------------------------
        forced = self.risk.must_flatten(self.portfolio)

        for leg in self.strategy.legs:
            position = self.portfolio.positions.get(leg.id)
            if position is None or not position.is_open:
                continue

            # The bar this position lives in: its own contract for an option
            # leg, the strategy's instrument for everything else.
            market = exec_bars.get(leg.id, bar)
            self._update_trail(position, leg, market)

            reason: TradeReason | None = None
            fill_price: Decimal | None = None
            same_bar = False

            if forced is not None:
                reason = forced
            elif self._past_square_off(ts):
                # Square-off is a standing instruction, so it executes on this
                # bar rather than waiting for one that may never come.
                reason = TradeReason.SQUARE_OFF
                fill_price = Decimal(str(market.close))
                same_bar = True
            else:
                hit = self._check_intrabar_exit(position, market)
                if hit:
                    reason, fill_price = hit
                    same_bar = True
                elif exit_signal:
                    reason = TradeReason.EXIT_SIGNAL

            if reason is not None:
                intents.append(
                    OrderIntent(
                        leg_id=leg.id,
                        symbol=position.symbol,
                        side=Side.SELL if position.is_long else Side.BUY,
                        quantity=abs(position.quantity),
                        reason=reason,
                        bar_index=index,
                        timestamp=ts,
                        limit_price=fill_price,
                        same_bar=same_bar,
                    )
                )

        # --- entries ---------------------------------------------------------
        if entry_signal and not intents:
            if index < self._warmup:
                blocked = Block.WARMING_UP
                note = f"bar {index} < warmup {self._warmup}"
            elif not self._in_session(ts):
                blocked = Block.OUTSIDE_SESSION
                note = f"{ts:%H:%M} outside {self.strategy.session.start}-{self.strategy.session.end}"
            elif self._past_square_off(ts):
                blocked = Block.OUTSIDE_SESSION
                note = "past square-off"
            else:
                price = Decimal(str(bar.close))
                notional = sum(
                    (price * Decimal(self._quantity(leg)) for leg in self.strategy.legs),
                    ZERO,
                )
                equity = self.portfolio.equity({self.symbol: price})
                decision = self.risk.may_enter(self.portfolio, index, notional, equity)
                if not decision:
                    blocked, note = decision.reason, decision.detail
                else:
                    for leg in self.strategy.legs:
                        position = self.portfolio.positions.get(leg.id)
                        if position and position.is_open:
                            continue
                        intents.append(
                            OrderIntent(
                                leg_id=leg.id,
                                symbol=self.symbol,
                                side=leg.action,
                                quantity=self._quantity(leg),
                                reason=TradeReason.ENTRY_SIGNAL,
                                bar_index=index,
                                timestamp=ts,
                            )
                        )

        return BarEvent(
            index=index,
            timestamp=ts,
            bar=bar,
            entry_signal=entry_signal,
            exit_signal=exit_signal,
            intents=tuple(intents),
            blocked=blocked,
            note=note,
        )

    def apply_fill(
        self,
        intent: OrderIntent,
        price: Decimal,
        fees: Decimal,
        ts: datetime,
        bar_index: int,
        indicators: IndicatorSet | None = None,
    ) -> ClosedTrade | None:
        """Book a fill against the portfolio. Returns a ClosedTrade if it closed one."""
        leg = next(l for l in self.strategy.legs if l.id == intent.leg_id)
        position = self.portfolio.position(intent.leg_id, intent.symbol, self.lot_size)

        was_open = position.is_open
        entry_time = position.opened_at
        entry_price = position.average_price
        entry_bar = position.entry_bar_index
        entry_fees = position.entry_fees
        direction = "LONG" if position.is_long else "SHORT"

        realized = position.apply_fill(intent.side, intent.quantity, price, fees)
        self.portfolio.book_fees(fees)

        if not was_open and position.is_open:
            position.opened_at = ts
            position.entry_bar_index = bar_index
            position.entry_fees = fees
            if indicators is not None:
                self._set_exit_levels(position, leg, indicators)
            self.risk.record_entry()
            self.portfolio.day_entry_count += 1
            return None

        if was_open and not position.is_open:
            self.portfolio.book_realized(realized)
            self.risk.record_exit(bar_index)
            # Round-trip cost: entry charges plus this exit's charges. Reporting
            # only the exit side would halve the apparent cost of every trade.
            round_trip_fees = entry_fees + fees
            trade = ClosedTrade(
                strategy_id=self.strategy.id,
                leg_id=intent.leg_id,
                symbol=intent.symbol,
                direction=direction,
                quantity=intent.quantity,
                entry_time=entry_time or ts,
                entry_price=entry_price,
                exit_time=ts,
                exit_price=price,
                gross_pnl=realized,
                fees=round_trip_fees,
                reason=intent.reason,
                bars_held=bar_index - entry_bar,
            )
            self.portfolio.record_trade(trade)
            return trade

        if realized:
            self.portfolio.book_realized(realized)
        return None

    # -- convenience: run over a DataFrame ------------------------------------

    def prepare_backtest(self, df: pd.DataFrame) -> VectorizedIndicatorSet:
        indicators = VectorizedIndicatorSet(df, self.strategy.indicators, self._inline)
        self._warmup = indicators.warmup_bars()
        return indicators

    def prepare_live(self) -> StreamingIndicatorSet:
        return StreamingIndicatorSet(self.strategy.indicators, self._inline)


def bars_with_timestamps(df: pd.DataFrame) -> Iterator[tuple[int, datetime, PriceBar]]:
    """Iterate a candle frame as (index, timestamp, PriceBar)."""
    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(IST)
    values = df[["open", "high", "low", "close", "volume"]].to_numpy(dtype="float64")
    for i, (t, row) in enumerate(zip(ts, values)):
        yield i, t.to_pydatetime(), PriceBar(*row)
