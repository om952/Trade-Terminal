"""The orders Trade Terminal sends, and how a simulated bar fills them (P5.0).

NSE rejects algorithmic market orders, and every order sent through a client
API is an algorithmic order (NSE/INVG/73992 para 8.1.12; NSE/INVG/69255 para
2.8; docs/REGULATORY_CHECK.md section 6). So live trading sends only:

* **Marketable limit orders** for entries, signal exits, forced exits and
  square-off: a limit `limit_band_bps` away from the price when the order goes
  out (the next bar's open for an order decided at a close, the bar's close for
  square-off). The fill is that price plus slippage, never beyond the limit.
* **Stop-limit (SL) orders** for stop-losses and trailing stops: triggered at
  the stop, with a limit `stop_limit_band_bps` beyond it. A bar that gaps
  through the stop but stays inside the limit fills at its open, not at the
  stop. A bar that gaps past the limit does not fill; the position is still
  open, and the stop is chased with a marketable limit at the next bar's open.
* **Limit orders** resting at a target. A limit never fills worse than its
  price, and touching the price is not enough to be filled from the back of the
  queue, so a target fills only when the bar trades through it (at the target,
  or at the open when the bar opens beyond it).

Backtest, paper trading and the portfolio engine all settle orders here, so
the three cannot model live orders differently. `FillModel.orders = "market"`
keeps the previous behaviour (fill at the reference price plus slippage, stops
at their level even on a gap) for measuring the difference only.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING

from backend.core.interfaces import OrderType, Side
from backend.strategy.state import TradeReason

if TYPE_CHECKING:
    from backend.indicators.base import PriceBar
    from backend.strategy.engine import OrderIntent
    from backend.strategy.runner import FillModel

STOP_REASONS = {TradeReason.STOP_LOSS, TradeReason.TRAILING_STOP}
BPS = Decimal(10_000)


def _band(bps: float) -> Decimal:
    return Decimal(str(bps)) / BPS


def marketable_limit(side: Side, reference: Decimal, band_bps: float) -> Decimal:
    """The limit for an order meant to fill now: `band_bps` worse than `reference`."""
    band = reference * _band(band_bps)
    return reference + band if side is Side.BUY else reference - band


def stop_limit(side: Side, trigger: Decimal, band_bps: float) -> Decimal:
    """A stop-limit's limit: `band_bps` beyond the trigger, in the direction of the exit."""
    return marketable_limit(side, trigger, band_bps)


def capped(side: Side, price: Decimal, limit: Decimal | None) -> Decimal:
    """A fill price never beyond the order's limit."""
    if limit is None:
        return price
    return min(price, limit) if side is Side.BUY else max(price, limit)


@dataclass(frozen=True, slots=True)
class Order:
    """One order to settle against a bar: what goes to the broker, and the price it meets.

    `reference` is the market price the order meets in a simulation; the fill
    is `reference` plus slippage, capped at `limit`.
    """

    order_type: OrderType
    reference: Decimal
    limit: Decimal | None
    trigger: Decimal | None = None


def fill_price(fills: FillModel, side: Side, order: Order) -> Decimal:
    return capped(side, fills.fill_price(side, order.reference), order.limit)


def at_open(fills: FillModel, side: Side, bar_open: Decimal) -> Order:
    """An order decided at the previous close, sent as the bar opens."""
    if fills.orders == "market":
        return Order(OrderType.MARKET, bar_open, None)
    return Order(OrderType.LIMIT, bar_open, marketable_limit(side, bar_open, fills.limit_band_bps))


def settle_resting(fills: FillModel, intent: OrderIntent, bar: PriceBar) -> Order | OrderIntent | None:
    """A resting order the engine says this bar reached.

    Returns the `Order` that fills on this bar; or a next-bar intent, for a
    stop that gapped past its limit and is chased at the next open; or None,
    for a target touched but not traded through, which stays resting.
    `intent.limit_price` is the level the engine computed: the stop trigger,
    the target, or the close for square-off.
    """
    assert intent.limit_price is not None
    level = intent.limit_price
    side = intent.side
    o, h, low = Decimal(str(bar.open)), Decimal(str(bar.high)), Decimal(str(bar.low))

    if fills.orders == "market":
        order_type = OrderType.SL_M if intent.reason in STOP_REASONS else OrderType.MARKET
        return Order(order_type, level, None, level if order_type is OrderType.SL_M else None)

    if intent.reason in STOP_REASONS:
        limit = stop_limit(side, level, fills.stop_limit_band_bps)
        selling = side is Side.SELL                     # a long's stop sells below the market
        gapped = o <= level if selling else o >= level
        if not gapped:                                  # triggered inside the bar, at the stop
            return Order(OrderType.SL, level, limit, level)
        inside = o >= limit if selling else o <= limit
        if inside:                                      # gapped through the stop, within the limit
            return Order(OrderType.SL, o, limit, level)
        reached = h >= limit if selling else low <= limit
        if reached:                                     # gapped past the limit, then came back to it
            return Order(OrderType.SL, limit, limit, level)
        # Gapped past the limit and stayed there: unfilled. Cancel and exit at the next open.
        return replace(intent, same_bar=False, limit_price=None)

    if intent.reason is TradeReason.TARGET:
        selling = side is Side.SELL
        if (o > level) if selling else (o < level):    # opened beyond the target: filled at the open
            return Order(OrderType.LIMIT, o, level)
        through = h > level if selling else low < level
        if through:
            return Order(OrderType.LIMIT, level, level)
        return None                              # touched, not traded through: still resting

    # Square-off and anything else that executes on this bar: a marketable limit at the level.
    return Order(OrderType.LIMIT, level, marketable_limit(side, level, fills.limit_band_bps))
