"""P5.2: pre-trade checks in front of every broker, and daily limits marked to market."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from backend.core.config import IST
from backend.core.interfaces import Order, OrderType, Side
from backend.execution.costs import Segment
from backend.execution.pretrade import GuardedBroker, PreTradeLimits, RateLimiter, Tripped
from backend.live.broker import SimulatedBroker
from backend.strategy.runner import FillModel
from backend.strategy.state import Portfolio

D = Decimal
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=IST)
_seq = iter(range(1, 10_000))


def _order(side=Side.BUY, qty=10, price="100.5", ref="100", kind=OrderType.LIMIT, trigger=None, symbol="X") -> Order:
    return Order(client_tag=f"t{next(_seq)}", symbol=symbol, side=side, quantity=qty, order_type=kind,
                 price=D(price) if price is not None else None, trigger_price=D(trigger) if trigger else None,
                 reference_price=D(ref) if ref else None)


def _guard(**limits) -> GuardedBroker:
    broker = SimulatedBroker(FillModel(slippage_bps=5.0, segment=Segment.EQUITY_DELIVERY, fee_per_order=D(0)),
                             clock=lambda: NOW)
    return GuardedBroker(broker, PreTradeLimits(**limits), clock=lambda: NOW)


# -- the checks ------------------------------------------------------------------

def test_market_orders_never_reach_the_broker():
    g = _guard()
    assert g.place(_order(kind=OrderType.MARKET, price="100")) is None
    assert g.place(_order(kind=OrderType.SL_M, price=None, trigger="99")) is None
    assert "market orders are not allowed" in g.rejections[0].reason
    assert g.place(_order()) is not None


def test_the_fat_finger_band():
    g = _guard(price_band_pct=5.0)
    assert g.place(_order(price="106", ref="100")) is None
    assert "price band" in g.rejections[-1].reason
    assert g.place(_order(price="104.9", ref="100")) is not None
    # A stop-limit's limit is checked against its trigger, not the market.
    assert g.place(_order(side=Side.SELL, kind=OrderType.SL, price="90", trigger="100", ref="100"), opening=False) is None
    assert g.place(_order(side=Side.SELL, kind=OrderType.SL, price="99", trigger="100", ref="100"), opening=False) is not None


def test_caps_refuse_orders_that_open_exposure_never_ones_that_reduce_it():
    g = _guard(max_order_quantity=50, max_order_notional=D("10000"))
    assert g.place(_order(qty=60), opening=True) is None
    assert g.place(_order(qty=40, price="300", ref="300"), opening=True) is None          # notional 12,000
    assert "MAX_ORDER_NOTIONAL" in g.rejections[-1].reason
    assert g.place(_order(side=Side.SELL, qty=60, price="299", ref="300"), opening=False) is not None, \
        "a stop or an exit is never refused by a size cap"


def test_freeze_quantity_applies_both_ways():
    g = _guard()
    g.freeze_quantity = lambda symbol: 1800
    assert g.place(_order(qty=1801), opening=False) is None
    assert "slice" in g.rejections[-1].reason
    assert g.place(_order(qty=1800), opening=True) is not None


def test_the_fno_ban_refuses_new_positions_and_fails_closed():
    g = _guard()
    answers = {"BANNED": True, "OK": False, "UNKNOWN": None}
    g.is_banned = lambda symbol: answers[symbol]
    assert g.place(_order(symbol="BANNED"), opening=True) is None and "ban period" in g.rejections[-1].reason
    assert g.place(_order(symbol="UNKNOWN"), opening=True) is None and "could not be read" in g.rejections[-1].reason
    assert g.place(_order(symbol="OK"), opening=True) is not None
    assert g.place(_order(symbol="BANNED", side=Side.SELL, price="99.5"), opening=False) is not None, \
        "closing a position in a banned stock is allowed"


# -- the runaway stop -----------------------------------------------------------------

def test_too_many_orders_in_a_day_trips_and_only_exits_go_out_after():
    g = _guard(max_orders_per_day=3)
    for _ in range(3):
        assert g.place(_order(), opening=True) is not None
    assert g.place(_order(), opening=True) is None
    assert g.tripped and "3 orders today" in g.trip_reason
    assert g.place(_order(side=Side.SELL, price="99.5"), opening=False) is not None
    assert g.status()["tripped"] is True


def test_the_daily_count_runs_by_the_orders_time():
    g = _guard(max_orders_per_day=2)
    for day in range(4):
        when = NOW + timedelta(days=day)
        assert g.place(_order(), opening=True, as_of=when) is not None
        assert g.place(_order(), opening=True, as_of=when) is not None
    assert not g.tripped, "two a day for four days is within two a day"


def test_repeated_rejections_trip():
    class Rejecting(SimulatedBroker):
        def place(self, order):
            return None

    g = GuardedBroker(Rejecting(FillModel()), PreTradeLimits(max_consecutive_rejects=3), clock=lambda: NOW)
    for _ in range(3):
        g.place(_order(), opening=True)
    assert g.tripped and "rejected by the broker" in g.trip_reason
    refusing = _guard(max_consecutive_rejects=2)
    refusing.place(_order(kind=OrderType.MARKET, price="100"))
    refusing.place(_order(kind=OrderType.MARKET, price="100"))
    assert refusing.tripped


def test_the_guard_passes_other_attributes_through():
    g = _guard()
    g.place(_order(), opening=True)
    assert len(g.fills) == 1 and g.reconcile()


# -- the rate limiter -----------------------------------------------------------------

class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(round(s, 6))
        self.t += s


def test_orders_are_spaced_and_never_more_than_five_a_second():
    c = Clock()
    lim = RateLimiter(PreTradeLimits(per_second=5, min_spacing_s=0.15), clock=c.now, sleep=c.sleep)
    sent = []
    for _ in range(20):                       # a burst: a rebalance of 20 orders
        lim.acquire()
        sent.append(c.t)
    assert all(b - a >= 0.15 - 1e-9 for a, b in zip(sent, sent[1:]))
    for i in range(len(sent)):
        assert sum(1 for t in sent if sent[i] <= t < sent[i] + 1.0) <= 5
    assert sent[-1] - sent[0] >= 3.0, "20 orders take at least three seconds"


def test_a_wait_beyond_the_limit_trips_instead_of_queueing():
    c = Clock()
    lim = RateLimiter(PreTradeLimits(per_second=5, per_minute=10, min_spacing_s=0.1, max_wait_s=5), clock=c.now, sleep=c.sleep)
    for _ in range(10):
        lim.acquire()
    with pytest.raises(Tripped, match="too many orders"):
        lim.acquire()


# -- daily limits marked to market ------------------------------------------------------

def _long(portfolio: Portfolio, price: str, qty: int = 100) -> None:
    pos = portfolio.position("leg", "X")
    pos.apply_fill(Side.BUY, qty, D(price), D(0))


def test_an_open_losing_position_trips_the_daily_loss_limit_before_it_closes():
    from backend.strategy.risk import Block, RiskGuard
    from backend.strategy.schema import RiskRules
    from backend.strategy.state import TradeReason

    portfolio = Portfolio(starting_capital=D("100000"))
    portfolio.start_new_day()
    _long(portfolio, "100")
    guard = RiskGuard(rules=RiskRules(max_loss_per_day=1000))
    portfolio.mark_to_market({"X": D("95")})                  # 500 down, nothing realized
    assert guard.must_flatten(portfolio) is None
    portfolio.mark_to_market({"X": D("89")})                  # 1,100 down, still open
    assert portfolio.day_realized_pnl == 0 and portfolio.day_pnl == D("-1100")
    assert guard.must_flatten(portfolio) is TradeReason.RISK_LIMIT
    decision = RiskGuard(rules=RiskRules(max_loss_per_day=1000)).may_enter(portfolio, 5)
    assert not decision and decision.reason is Block.DAILY_LOSS and "marked to market" in decision.detail


def test_the_account_limit_is_marked_to_market_and_counts_fees():
    from backend.strategy.risk import Block, RiskGuard
    from backend.strategy.schema import RiskRules

    portfolio = Portfolio(starting_capital=D("100000"))
    portfolio.start_new_day()
    _long(portfolio, "100")
    portfolio.book_fees(D("120"))
    portfolio.mark_to_market({"X": D("96")})                  # 400 down plus 120 of fees
    guard = RiskGuard(rules=RiskRules(), account_max_daily_loss=D("500"))
    decision = guard.may_enter(portfolio, 1)
    assert not decision and decision.reason is Block.ACCOUNT_HALTED


def test_a_new_day_starts_from_the_last_marked_equity():
    portfolio = Portfolio(starting_capital=D("100000"))
    portfolio.start_new_day()
    _long(portfolio, "100")
    portfolio.mark_to_market({"X": D("110")})                 # up 1,000 by the close
    portfolio.start_new_day()
    assert portfolio.day_start_equity == D("101000")
    portfolio.mark_to_market({"X": D("104")})                 # gaps down overnight
    assert portfolio.day_pnl == D("-600"), "the overnight gap counts against the new day"


def test_a_backtest_flattens_an_open_loser_at_the_daily_limit():
    """Engine level: a position that only falls, with no stop, is closed by the daily loss limit."""
    import pandas as pd

    from backend.strategy.risk import RiskGuard
    from backend.strategy.runner import run_backtest
    from backend.strategy.schema import Strategy
    from backend.strategy.state import TradeReason

    stamps = pd.date_range("2026-01-05 09:15", periods=40, freq="5min", tz=IST)
    closes = [100.0] * 5 + [100 - 0.5 * i for i in range(1, 36)]
    opens = [100.0] + closes[:-1]
    df = pd.DataFrame({"timestamp": stamps, "open": opens, "high": [max(o, c) + 0.05 for o, c in zip(opens, closes)],
                       "low": [min(o, c) - 0.05 for o, c in zip(opens, closes)], "close": closes,
                       "volume": 1000, "oi": 0})
    doc = {
        "name": "Hold and hope", "instrument": {"symbol": "SIM|MTM", "segment": "equity"}, "timeframe": "5m",
        "indicators": [], "entry": {"op": "AND", "conditions": [{"left": "close", "cmp": ">", "right": 0}]},
        "exit": {"op": "AND", "conditions": [{"left": "close", "cmp": "<", "right": 0}]},       # never
        "legs": [{"id": "l1", "kind": "equity", "action": "BUY", "lots": 1}],
        "risk": {"max_loss_per_day": 1000, "max_trades_per_day": 1},
        "session": {"start": "09:15", "end": "15:30", "square_off": None},
    }
    strategy = Strategy.model_validate(doc)
    result = run_backtest(strategy, df, starting_capital=D("1000000"), lot_size=100,
                          fill_model=FillModel(slippage_bps=0, segment=Segment.EQUITY_INTRADAY, fee_per_order=D(0)),
                          risk=RiskGuard(rules=strategy.risk), collect_events=False)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.reason is TradeReason.RISK_LIMIT, "marked to market, the limit closes the open loser"
    assert D("-1300") < trade.net_pnl <= D("-1000"), trade.net_pnl
