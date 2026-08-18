"""SimulatedBroker — fills orders against live prices, using the backtest's cost model.

Paper trading is only worth anything if its fills are computed the same way the
backtest computed them. So this shares `FillModel` with `run_backtest`: identical
slippage, identical brokerage, STT, exchange, SEBI, GST and stamp duty. A paper
session that used a friendlier cost model would report a strategy as profitable
that the backtest already knew was not, which is worse than not paper trading.

Two properties beyond "compute a price":

**Idempotency on `client_tag`.** Submitting the same tag twice returns the first
fill and executes nothing. This is what makes crash recovery safe — on restart
the session replays its journal, and any order already filled must not fill
again. Without it, recovery from a crash between "order sent" and "fill recorded"
doubles the position.

**An independent position ledger.** The broker derives positions by folding its
own fill log, while the engine derives them through `Position.apply_fill`. Two
separate computations of the same quantity, compared by `reconcile()`. They
should never disagree, and the only way to find out that they do is to check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from backend.core.config import IST
from backend.core.interfaces import Fill, Order, OrderStatus, Position, Side
from backend.strategy.runner import FillModel

log = logging.getLogger(__name__)

ZERO = Decimal("0")


@dataclass
class _Book:
    """The broker's own view of one instrument, folded from fills alone."""

    symbol: str
    quantity: int = 0
    average_price: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    last_price: Decimal = ZERO

    def apply(self, side: Side, quantity: int, price: Decimal, fees: Decimal) -> None:
        signed = quantity if side is Side.BUY else -quantity
        self.fees += fees
        self.last_price = price

        if self.quantity == 0:
            self.quantity, self.average_price = signed, price
            return

        if (self.quantity > 0) == (signed > 0):
            cost = self.average_price * Decimal(abs(self.quantity)) + price * Decimal(abs(signed))
            self.quantity += signed
            self.average_price = cost / Decimal(abs(self.quantity))
            return

        closing = min(abs(signed), abs(self.quantity))
        direction = Decimal(1 if self.quantity > 0 else -1)
        self.realized_pnl += (price - self.average_price) * Decimal(closing) * direction
        remaining = abs(signed) - closing
        self.quantity += signed
        if self.quantity == 0:
            self.average_price = ZERO
        elif remaining > 0:
            self.average_price = price


@dataclass
class ReconcileReport:
    agrees: bool
    differences: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"agrees": self.agrees, "differences": self.differences}


class SimulatedBroker:
    """Fills at a supplied reference price. Satisfies the `Broker` protocol.

    `Order.price` carries the reference price for every order here, including
    MARKET ones. In a simulator the price an order fills at is known by the
    caller — it is this bar's open, or a stop level inside this bar — and passing
    it explicitly beats hiding it in broker state that the session has to
    remember to update in the right order. A real broker adapter ignores the
    field for MARKET orders, so nothing above this has to care.
    """

    def __init__(self, fill_model: FillModel, clock=None) -> None:
        self.fill_model = fill_model
        self._clock = clock or (lambda: datetime.now(IST))
        self._fills: dict[str, Fill] = {}
        self._books: dict[str, _Book] = {}
        self._rejected: list[tuple[str, str]] = []
        self._sequence = 0

    # -- Broker protocol -----------------------------------------------------

    def place(self, order: Order) -> Fill | None:
        if order.client_tag in self._fills:
            log.info("Duplicate order %s ignored — already filled", order.client_tag)
            return self._fills[order.client_tag]

        if order.price is None:
            self._rejected.append((order.client_tag, "no reference price"))
            log.error("Order %s has no reference price; rejected", order.client_tag)
            return None

        if order.quantity <= 0:
            self._rejected.append((order.client_tag, "non-positive quantity"))
            return None

        price = self.fill_model.fill_price(order.side, order.price)
        fees = self.fill_model.fees(order.side, order.quantity, price)

        book = self._books.setdefault(order.symbol, _Book(symbol=order.symbol))
        book.apply(order.side, order.quantity, price, fees)

        self._sequence += 1
        fill = Fill(
            client_tag=order.client_tag,
            broker_order_id=f"SIM-{self._sequence:06d}",
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=price,
            timestamp=self._clock(),
            fees=fees,
            status=OrderStatus.FILLED,
        )
        self._fills[order.client_tag] = fill
        return fill

    def cancel(self, client_tag: str) -> bool:
        """Nothing rests here: every order fills or is rejected on submission."""
        return False

    def positions(self) -> list[Position]:
        return [
            Position(
                symbol=b.symbol,
                quantity=b.quantity,
                average_price=b.average_price,
                last_price=b.last_price,
                realized_pnl=b.realized_pnl,
                unrealized_pnl=(
                    (b.last_price - b.average_price) * Decimal(b.quantity)
                    if b.quantity
                    else ZERO
                ),
            )
            for b in self._books.values()
            if b.quantity != 0
        ]

    def reconcile(self) -> bool:
        """Protocol form: internal consistency only. Prefer `reconcile_against`."""
        return all(b.quantity == 0 or b.average_price > 0 for b in self._books.values())

    # -- beyond the protocol -------------------------------------------------

    def reconcile_against(self, portfolio) -> ReconcileReport:
        """Compare the engine's position accounting to the broker's fill ledger.

        The engine folds fills through `Position.apply_fill`; this class folds the
        same fills through `_Book.apply`. Independent paths to the same number, so
        a disagreement is evidence of a real accounting bug rather than a rounding
        artefact — and a session that does not know what it holds must stop.
        """
        differences: list[str] = []

        engine_qty: dict[str, int] = {}
        for position in portfolio.positions.values():
            if position.quantity:
                engine_qty[position.symbol] = engine_qty.get(position.symbol, 0) + position.quantity

        broker_qty = {b.symbol: b.quantity for b in self._books.values() if b.quantity}

        for symbol in set(engine_qty) | set(broker_qty):
            mine, theirs = engine_qty.get(symbol, 0), broker_qty.get(symbol, 0)
            if mine != theirs:
                differences.append(f"{symbol}: engine {mine:+d} vs broker {theirs:+d}")

        return ReconcileReport(agrees=not differences, differences=differences)

    def mark(self, symbol: str, price: Decimal) -> None:
        book = self._books.get(symbol)
        if book is not None:
            book.last_price = price

    def restore(self, client_tags: set[str]) -> None:
        """Re-arm idempotency after a restart.

        Recovery replays intents the journal already recorded as filled. Marking
        those tags as spent means the replay reproduces state without producing
        orders — the difference between recovering a position and doubling it.
        """
        for tag in client_tags:
            self._fills.setdefault(tag, None)  # type: ignore[arg-type]

    @property
    def fills(self) -> list[Fill]:
        return [f for f in self._fills.values() if f is not None]

    @property
    def rejected(self) -> list[tuple[str, str]]:
        return list(self._rejected)
