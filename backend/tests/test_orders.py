"""P5.0: the orders live trading sends, settled against a bar (backend/execution/orders.py)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from backend.core.config import IST
from backend.core.interfaces import Order, OrderType, Side
from backend.execution import orders
from backend.execution.costs import Segment
from backend.indicators.base import PriceBar
from backend.live.broker import SimulatedBroker
from backend.strategy.engine import OrderIntent
from backend.strategy.runner import FillModel
from backend.strategy.state import TradeReason

D = Decimal
NOW = datetime(2026, 1, 5, 9, 15, tzinfo=IST)
FILLS = FillModel(slippage_bps=10.0, segment=Segment.EQUITY_DELIVERY, fee_per_order=D(0))     # band 100 bps


def _intent(side: Side, reason: TradeReason, level: str) -> OrderIntent:
    return OrderIntent(leg_id="l", symbol="X", side=side, quantity=10, reason=reason, bar_index=3,
                       timestamp=NOW, limit_price=D(level), same_bar=True)


def _bar(o, h, low, c) -> PriceBar:
    return PriceBar(o, h, low, c, 1000)


# -- a long position's stop (a SELL stop-limit) --------------------------------

def test_a_stop_triggered_inside_the_bar_fills_at_the_stop_less_slippage():
    order = orders.settle_resting(FILLS, _intent(Side.SELL, TradeReason.STOP_LOSS, "100"), _bar(102, 103, 98, 99))
    assert isinstance(order, orders.Order) and order.order_type is OrderType.SL and order.trigger == D(100)
    assert order.limit == D("99.00")
    assert orders.fill_price(FILLS, Side.SELL, order) == D("99.9")


def test_a_stop_gapped_through_within_its_limit_fills_at_the_open_not_the_stop():
    order = orders.settle_resting(FILLS, _intent(Side.SELL, TradeReason.STOP_LOSS, "100"), _bar(99.5, 100.5, 98, 99))
    assert isinstance(order, orders.Order) and order.reference == D("99.5")
    assert orders.fill_price(FILLS, Side.SELL, order) == D("99.40050")


def test_a_stop_gapped_past_its_limit_fills_at_the_limit_if_the_bar_comes_back():
    order = orders.settle_resting(FILLS, _intent(Side.SELL, TradeReason.STOP_LOSS, "100"), _bar(97, 99.2, 96, 98))
    assert isinstance(order, orders.Order) and order.reference == D("99.00")
    assert orders.fill_price(FILLS, Side.SELL, order) == D("99.00"), "never worse than the limit"


def test_a_stop_gapped_past_its_limit_and_staying_there_is_unfilled_and_chased():
    intent = _intent(Side.SELL, TradeReason.STOP_LOSS, "100")
    chased = orders.settle_resting(FILLS, intent, _bar(96, 98, 95, 97))
    assert isinstance(chased, OrderIntent)
    assert chased.same_bar is False and chased.limit_price is None and chased.reason is TradeReason.STOP_LOSS


def test_a_short_positions_stop_mirrors():
    order = orders.settle_resting(FILLS, _intent(Side.BUY, TradeReason.STOP_LOSS, "100"), _bar(100.5, 101, 99, 100.8))
    assert isinstance(order, orders.Order) and order.reference == D("100.5") and order.limit == D("101.00")
    assert isinstance(orders.settle_resting(FILLS, _intent(Side.BUY, TradeReason.STOP_LOSS, "100"), _bar(104, 105, 103, 104)), OrderIntent)


# -- targets (resting limits) --------------------------------------------------

def test_a_target_fills_at_the_target_only_when_traded_through():
    order = orders.settle_resting(FILLS, _intent(Side.SELL, TradeReason.TARGET, "110"), _bar(108, 111, 107, 109))
    assert isinstance(order, orders.Order) and orders.fill_price(FILLS, Side.SELL, order) == D(110), "a limit never fills worse"
    assert orders.settle_resting(FILLS, _intent(Side.SELL, TradeReason.TARGET, "110"), _bar(108, 110, 107, 109)) is None


def test_a_target_opened_beyond_fills_at_the_open():
    order = orders.settle_resting(FILLS, _intent(Side.SELL, TradeReason.TARGET, "110"), _bar(112, 113, 111, 112))
    assert isinstance(order, orders.Order) and order.reference == D(112)


# -- marketable limits ------------------------------------------------------------

def test_an_order_at_the_open_is_a_marketable_limit_and_slippage_never_passes_it():
    order = orders.at_open(FILLS, Side.BUY, D(200))
    assert order.order_type is OrderType.LIMIT and order.limit == D("202.00")
    assert orders.fill_price(FILLS, Side.BUY, order) == D("200.2")
    wide = FillModel(slippage_bps=300.0, segment=Segment.EQUITY_DELIVERY, fee_per_order=D(0))
    assert orders.fill_price(wide, Side.BUY, orders.at_open(wide, Side.BUY, D(200))) == D("202.00")


def test_square_off_is_a_marketable_limit_at_the_close():
    order = orders.settle_resting(FILLS, _intent(Side.SELL, TradeReason.SQUARE_OFF, "150"), _bar(149, 151, 148, 150))
    assert isinstance(order, orders.Order) and order.order_type is OrderType.LIMIT and order.limit == D("148.50")


def test_the_old_market_model_is_kept_only_for_measuring():
    old = FillModel(slippage_bps=10.0, segment=Segment.EQUITY_DELIVERY, fee_per_order=D(0), orders="market")
    assert orders.at_open(old, Side.BUY, D(200)).order_type is OrderType.MARKET
    order = orders.settle_resting(old, _intent(Side.SELL, TradeReason.STOP_LOSS, "100"), _bar(96, 98, 95, 97))
    assert isinstance(order, orders.Order) and order.order_type is OrderType.SL_M and order.reference == D(100), \
        "the old model filled a gapped stop at its level: the bias this change removes"


# -- the simulated broker and the portfolio engine ---------------------------------

def test_the_simulated_broker_caps_limit_and_stop_limit_fills():
    broker = SimulatedBroker(FILLS, clock=lambda: NOW)
    buy = broker.place(Order(client_tag="a", symbol="X", side=Side.BUY, quantity=5, order_type=OrderType.LIMIT,
                             price=D("100.05"), reference_price=D(100)))
    assert buy is not None and buy.price == D("100.05"), "slippage of 10 bps capped at a 5 bp limit"
    sell = broker.place(Order(client_tag="b", symbol="X", side=Side.SELL, quantity=5, order_type=OrderType.SL,
                              price=D(99), trigger_price=D(100), reference_price=D(100)))
    assert sell is not None and sell.price == D("99.9")


def test_the_portfolio_engine_sends_marketable_limit_orders():
    from test_portfolio import FRAMES, _spec

    from backend.portfolio.engine import PortfolioEngine
    from backend.portfolio.feeds import HistoricalUniverseFeed

    sent: list[Order] = []

    class Recording(SimulatedBroker):
        def place(self, order):
            sent.append(order)
            return super().place(order)

    broker = Recording(FillModel(slippage_bps=5.0, segment=Segment.EQUITY_DELIVERY))
    PortfolioEngine(_spec(), broker, D("1000000"), sorted(FRAMES)).run(HistoricalUniverseFeed(FRAMES))
    assert sent and {o.order_type for o in sent} == {OrderType.LIMIT}
    for o in sent:
        assert o.reference_price is not None
        band = o.reference_price * D("0.01")
        assert o.price == (o.reference_price + band if o.side is Side.BUY else o.reference_price - band)


@pytest.mark.parametrize("reason", [TradeReason.STOP_LOSS, TradeReason.TARGET, TradeReason.SQUARE_OFF])
def test_no_order_settled_here_is_a_market_order(reason):
    for bar in (_bar(102, 103, 98, 99), _bar(96, 98, 95, 97), _bar(112, 113, 111, 112)):
        detail = orders.settle_resting(FILLS, _intent(Side.SELL, reason, "100" if reason is not TradeReason.TARGET else "110"), bar)
        if isinstance(detail, orders.Order):
            assert detail.order_type in (OrderType.LIMIT, OrderType.SL) and detail.limit is not None
