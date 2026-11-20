"""The portfolio engine: one code path for backtest and paper, behind the DataFeed and Broker seam.

It pulls bars from any `DataFeed` (interleaved across the universe, in time
order) and sends orders to any `Broker`. A backtest wires in a historical
feed; paper trading wires in the replay or live sources; the engine cannot
tell which, so the two cannot drift apart (backend/core/interfaces.py).

The day, in order:

1. Day D's bars arrive and update each stock's indicators (streaming, the
   same `StreamingIndicatorSet` live sessions use).
2. At D's close: orders decided at D-1's close fill at D's opens, sells first,
   then buys with the cash there is (scaled down when short); then holdings
   are marked at D's closes; then, on a rebalance day, the universe is ranked
   on information up to D's close and the next day's orders are queued.

So a decision never sees the price it fills at, and a fill never uses a
price from before the decision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from backend.core.interfaces import Bar, Broker, DataFeed, Order, OrderType, Side
from backend.indicators.base import PriceBar
from backend.portfolio.spec import PortfolioSpec
from backend.strategy import conditions
from backend.strategy.conditions import collect_exprs, collect_inline
from backend.strategy.indicator_set import StreamingIndicatorSet
from backend.strategy.state import ClosedTrade, TradeReason

RESIZE_BAND = 0.10        # resize a kept holding only when it is this far off its target


@dataclass
class Holding:
    quantity: int
    avg_price: Decimal
    entry_time: datetime
    entry_fees: Decimal
    days_held: int = 0


@dataclass
class Pending:
    symbol: str
    side: Side
    quantity: int
    reason: TradeReason


@dataclass
class PortfolioEngine:
    spec: PortfolioSpec
    broker: Broker
    starting_capital: Decimal
    symbols: list[str]
    benchmark: str | None = None
    strategy_id: str = "portfolio"

    cash: Decimal = field(init=False)
    holdings: dict[str, Holding] = field(default_factory=dict, init=False)
    pending: list[Pending] = field(default_factory=list, init=False)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list, init=False)
    trades: list[ClosedTrade] = field(default_factory=list, init=False)
    rebalances: list[dict[str, Any]] = field(default_factory=list, init=False)
    notes: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.cash = Decimal(self.starting_capital)
        inline, exprs = self.spec.inline(), self.spec.exprs()
        self._sets = {s: StreamingIndicatorSet(self.spec.indicators, inline, exprs) for s in self.symbols}
        self._regime_set = None
        if self.spec.regime is not None:
            r = self.spec.regime
            self._regime_set = StreamingIndicatorSet(r.indicators, collect_inline(r.condition), collect_exprs(r.condition))
        self._day: date | None = None
        self._previous_day: date | None = None
        self._today: dict[str, Bar] = {}
        self._last_close: dict[str, Decimal] = {}
        self._sequence = 0

    # -- feeding -------------------------------------------------------------

    def run(self, feed: DataFeed) -> PortfolioEngine:
        for bar in feed.stream():
            self.on_bar(bar)
        self.finish()
        return self

    def on_bar(self, bar: Bar) -> None:
        day = bar.timestamp.date()
        if self._day is not None and day != self._day:
            self._close_day()
        self._day = day
        price = PriceBar(float(bar.open), float(bar.high), float(bar.low), float(bar.close), float(bar.volume))
        if bar.symbol in self._sets:
            self._sets[bar.symbol].update(price)
            self._today[bar.symbol] = bar
        if bar.symbol == self.benchmark and self._regime_set is not None:
            self._regime_set.update(price)
            if bar.symbol not in self._sets:
                self._today.setdefault(bar.symbol, bar)

    def finish(self) -> None:
        if self._day is not None and self._today:
            self._close_day()

    # -- the day's close -------------------------------------------------------

    def _close_day(self) -> None:
        day, bars = self._day, self._today
        assert day is not None
        when = next(iter(bars.values())).timestamp
        self._execute(bars, when)
        for symbol, bar in bars.items():
            self._last_close[symbol] = bar.close
        for h in self.holdings.values():
            h.days_held += 1
        self.equity_curve.append((when, self._equity()))
        if self._is_rebalance_day(day):
            self._decide(day)
        self._previous_day, self._today = day, {}

    def _equity(self) -> Decimal:
        return self.cash + sum((Decimal(h.quantity) * self._last_close.get(s, h.avg_price)
                                for s, h in self.holdings.items()), Decimal(0))

    def _is_rebalance_day(self, day: date) -> bool:
        prev = self._previous_day
        if prev is None:
            return False
        if self.spec.rebalance == "monthly":
            return (day.year, day.month) != (prev.year, prev.month)
        return day.isocalendar()[:2] != prev.isocalendar()[:2]

    # -- deciding --------------------------------------------------------------

    def _rank_value(self, symbol: str) -> float | None:
        ind = self._sets[symbol]
        if self.spec.filter is not None and not conditions.evaluate(self.spec.filter, ind):
            return None
        value = conditions._operand_value(self.spec.rank_by, ind)
        return None if math.isnan(value) else value

    def _decide(self, day: date) -> None:
        regime_on = True
        if self._regime_set is not None and self.spec.regime is not None:
            regime_on = conditions.evaluate(self.spec.regime.condition, self._regime_set)
        scored = []
        for symbol in self.symbols:
            if symbol not in self._today:          # no bar today: not tradable tomorrow on today's price
                continue
            value = self._rank_value(symbol)
            if value is not None:
                scored.append((value, symbol))
        scored.sort(key=lambda t: (-t[0] if self.spec.rank_order == "desc" else t[0], t[1]))
        targets = [s for _, s in scored[: self.spec.hold]] if regime_on else []

        equity = self._equity()
        per = equity * Decimal(str(1 - self.spec.cash_buffer)) / Decimal(self.spec.hold)
        orders: list[Pending] = []
        for symbol, h in self.holdings.items():
            if symbol not in targets:
                orders.append(Pending(symbol, Side.SELL, h.quantity, TradeReason.REBALANCE))
        for symbol in targets:
            close = self._last_close[symbol]
            want = int((per / close).to_integral_value(rounding=ROUND_DOWN)) if close > 0 else 0
            have = self.holdings[symbol].quantity if symbol in self.holdings else 0
            if have == 0 and want > 0:
                orders.append(Pending(symbol, Side.BUY, want, TradeReason.ENTRY_SIGNAL))
            elif have and want and abs(want - have) > RESIZE_BAND * want:
                side = Side.BUY if want > have else Side.SELL
                orders.append(Pending(symbol, side, abs(want - have), TradeReason.REBALANCE))
        self.pending = orders
        self.rebalances.append({
            "date": day.isoformat(), "regime_on": regime_on, "eligible": len(scored),
            "targets": targets, "orders": len(orders),
            "top": [{"symbol": s, "value": round(v, 6)} for v, s in scored[: max(self.spec.hold, 5)]],
        })

    # -- filling ---------------------------------------------------------------

    def _tag(self) -> str:
        self._sequence += 1
        return f"{self.strategy_id}-{self._sequence:06d}"

    def _execute(self, bars: dict[str, Bar], when: datetime) -> None:
        if not self.pending:
            return
        waiting: list[Pending] = []
        for order in sorted(self.pending, key=lambda o: o.side is Side.BUY):     # sells first
            bar = bars.get(order.symbol)
            if bar is None:
                waiting.append(order)                # no open today; try again tomorrow
                continue
            quantity = order.quantity
            if order.side is Side.BUY:
                affordable = int((self.cash * Decimal("0.995") / bar.open).to_integral_value(rounding=ROUND_DOWN))
                quantity = min(quantity, max(affordable, 0))
                if quantity <= 0:
                    continue
            fill = self.broker.place(Order(
                client_tag=self._tag(), symbol=order.symbol, side=order.side, quantity=quantity,
                order_type=OrderType.MARKET, price=bar.open, strategy_id=self.strategy_id,
            ))
            if fill is None:
                self.notes.append(f"{when.date()}: {order.side.value} {order.symbol} was rejected by the broker.")
                continue
            if fill.side is Side.BUY:
                self._bought(order.symbol, fill.quantity, fill.price, fill.fees, when)
            else:
                self._sold(order.symbol, fill.quantity, fill.price, fill.fees, when, order.reason)
        self.pending = waiting

    def _bought(self, symbol: str, qty: int, price: Decimal, fees: Decimal, when: datetime) -> None:
        self.cash -= Decimal(qty) * price + fees
        h = self.holdings.get(symbol)
        if h is None:
            self.holdings[symbol] = Holding(qty, price, when, fees)
        else:
            total = h.quantity + qty
            h.avg_price = (h.avg_price * h.quantity + price * qty) / total
            h.quantity, h.entry_fees = total, h.entry_fees + fees

    def _sold(self, symbol: str, qty: int, price: Decimal, fees: Decimal, when: datetime, reason: TradeReason) -> None:
        h = self.holdings[symbol]
        qty = min(qty, h.quantity)
        self.cash += Decimal(qty) * price - fees
        share = Decimal(qty) / Decimal(h.quantity)
        entry_fees = h.entry_fees * share
        self.trades.append(ClosedTrade(
            strategy_id=self.strategy_id, leg_id="holding", symbol=symbol, direction="LONG", quantity=qty,
            entry_time=h.entry_time, entry_price=h.avg_price, exit_time=when, exit_price=price,
            gross_pnl=(price - h.avg_price) * qty, fees=entry_fees + fees, reason=reason, bars_held=h.days_held,
        ))
        h.quantity -= qty
        h.entry_fees -= entry_fees
        if h.quantity == 0:
            del self.holdings[symbol]

    # -- reporting ---------------------------------------------------------------

    def open_holdings(self) -> list[dict[str, Any]]:
        return [{"symbol": s, "quantity": h.quantity, "avg_price": float(h.avg_price),
                 "last_close": float(self._last_close.get(s, h.avg_price)),
                 "since": h.entry_time.date().isoformat()} for s, h in sorted(self.holdings.items())]
