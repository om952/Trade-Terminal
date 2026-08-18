"""Position and portfolio state.

Money is `Decimal` here, not float. Indicators can be float — an EMA that is off
by 1e-15 is irrelevant. A P&L that is off by 1e-15 accumulates across thousands
of fills and eventually disagrees with the broker's contract note, at which point
reconciliation fails and you cannot tell a real break from a rounding artefact.

Positions are signed: negative quantity is short. Averaging, reducing and
reversing all flow through `apply_fill`, which is the only place quantity or
average price changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from backend.core.interfaces import Side

ZERO = Decimal("0")


class TradeReason(str, Enum):
    """Why a position was opened or closed. Drives the trade log and analytics."""

    ENTRY_SIGNAL = "entry_signal"
    EXIT_SIGNAL = "exit_signal"
    STOP_LOSS = "stop_loss"
    TARGET = "target"
    TRAILING_STOP = "trailing_stop"
    SQUARE_OFF = "square_off"
    RISK_LIMIT = "risk_limit"
    SESSION_END = "session_end"
    MANUAL = "manual"


@dataclass(slots=True)
class Position:
    """A live position in one instrument."""

    symbol: str
    leg_id: str
    quantity: int = 0                       # signed; negative is short
    average_price: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    fees_paid: Decimal = ZERO
    opened_at: datetime | None = None
    entry_bar_index: int = 0
    lot_size: int = 1

    # Charges incurred opening this position, held so the closing ClosedTrade can
    # report the full round-trip cost. Without this the trade log shows only the
    # exit-side fee and every trade looks about half as expensive as it was.
    entry_fees: Decimal = ZERO

    # Exit tracking
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    trail_anchor: Decimal | None = None     # best price seen since entry
    trail_armed: bool = False

    @property
    def is_open(self) -> bool:
        return self.quantity != 0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def direction(self) -> int:
        return 1 if self.quantity > 0 else (-1 if self.quantity < 0 else 0)

    def unrealized_pnl(self, last_price: Decimal) -> Decimal:
        if self.quantity == 0:
            return ZERO
        return (last_price - self.average_price) * Decimal(self.quantity)

    def total_pnl(self, last_price: Decimal) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl(last_price) - self.fees_paid

    def pnl_percent(self, last_price: Decimal) -> float:
        """Return on the position's own notional, signed for direction."""
        if self.quantity == 0 or self.average_price == 0:
            return 0.0
        move = (last_price - self.average_price) / self.average_price
        return float(move * 100) * (1 if self.quantity > 0 else -1)

    def apply_fill(
        self, side: Side, quantity: int, price: Decimal, fees: Decimal = ZERO
    ) -> Decimal:
        """Apply a fill. Returns realized P&L booked by this fill.

        Handles the three cases that differ materially:
          * adding to a position   -> weighted-average the entry price
          * reducing a position    -> book P&L on the closed portion only
          * reversing through zero -> book the full close, then open fresh at
            this price. Averaging across a reversal would produce an entry price
            that never traded.
        """
        signed = quantity if side is Side.BUY else -quantity
        self.fees_paid += fees

        if self.quantity == 0:
            self.quantity = signed
            self.average_price = price
            return ZERO

        same_direction = (self.quantity > 0) == (signed > 0)

        if same_direction:
            total_cost = self.average_price * Decimal(abs(self.quantity)) + price * Decimal(abs(signed))
            self.quantity += signed
            self.average_price = total_cost / Decimal(abs(self.quantity))
            return ZERO

        closing = min(abs(signed), abs(self.quantity))
        direction = Decimal(1 if self.quantity > 0 else -1)
        realized = (price - self.average_price) * Decimal(closing) * direction
        self.realized_pnl += realized

        remaining = abs(signed) - closing
        self.quantity += signed

        if self.quantity == 0:
            self.average_price = ZERO
            self.stop_price = self.target_price = self.trail_anchor = None
            self.trail_armed = False
        elif remaining > 0:
            self.average_price = price  # reversed: fresh position at this price

        return realized


@dataclass(slots=True)
class ClosedTrade:
    """A completed round trip, for the trade log and metrics."""

    strategy_id: str
    leg_id: str
    symbol: str
    direction: str                # LONG | SHORT
    quantity: int
    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime
    exit_price: Decimal
    gross_pnl: Decimal
    fees: Decimal
    reason: TradeReason
    bars_held: int

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.fees

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0

    @property
    def return_pct(self) -> float:
        notional = self.entry_price * Decimal(abs(self.quantity))
        return float(self.net_pnl / notional * 100) if notional else 0.0

    def as_dict(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "leg_id": self.leg_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "quantity": self.quantity,
            "entry_time": self.entry_time.isoformat(),
            "entry_price": float(self.entry_price),
            "exit_time": self.exit_time.isoformat(),
            "exit_price": float(self.exit_price),
            "gross_pnl": float(self.gross_pnl),
            "fees": float(self.fees),
            "net_pnl": float(self.net_pnl),
            "return_pct": round(self.return_pct, 4),
            "reason": self.reason.value,
            "bars_held": self.bars_held,
        }


@dataclass
class Portfolio:
    """All positions for one strategy run, plus the day's running totals."""

    starting_capital: Decimal = Decimal("100000")
    positions: dict[str, Position] = field(default_factory=dict)
    closed_trades: list[ClosedTrade] = field(default_factory=list)

    realized_pnl: Decimal = ZERO
    fees_paid: Decimal = ZERO

    # Reset by the engine at each session boundary.
    day_realized_pnl: Decimal = ZERO
    day_trade_count: int = 0
    day_entry_count: int = 0

    def position(self, leg_id: str, symbol: str, lot_size: int = 1) -> Position:
        if leg_id not in self.positions:
            self.positions[leg_id] = Position(symbol=symbol, leg_id=leg_id, lot_size=lot_size)
        return self.positions[leg_id]

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    @property
    def has_open_positions(self) -> bool:
        return any(p.is_open for p in self.positions.values())

    def unrealized_pnl(self, prices: dict[str, Decimal]) -> Decimal:
        total = ZERO
        for pos in self.positions.values():
            if pos.is_open and pos.symbol in prices:
                total += pos.unrealized_pnl(prices[pos.symbol])
        return total

    def equity(self, prices: dict[str, Decimal]) -> Decimal:
        return self.starting_capital + self.realized_pnl - self.fees_paid + self.unrealized_pnl(prices)

    def total_pnl(self, prices: dict[str, Decimal]) -> Decimal:
        return self.realized_pnl - self.fees_paid + self.unrealized_pnl(prices)

    def book_realized(self, amount: Decimal) -> None:
        self.realized_pnl += amount
        self.day_realized_pnl += amount

    def book_fees(self, amount: Decimal) -> None:
        self.fees_paid += amount

    def start_new_day(self) -> None:
        self.day_realized_pnl = ZERO
        self.day_trade_count = 0
        self.day_entry_count = 0

    def record_trade(self, trade: ClosedTrade) -> None:
        self.closed_trades.append(trade)
        self.day_trade_count += 1
