"""Option legs must trade the contract, not the index the signal came from.

The bug these exist to prevent was silent and total: a leg declaring
`kind: option` filled at the underlying's price, so "sell BANKNIFTY ATM PE" was
booked as shorting 55,000 points of index instead of collecting ~300 of premium.
Every number downstream — P&L, fees, drawdown, the equity curve — was wrong by
roughly two orders of magnitude, and nothing looked broken because they were all
still plausible prices.
"""

from __future__ import annotations

import warnings
from datetime import date
from decimal import Decimal

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

from backend.data.simulator import generate_ohlcv
from backend.execution.costs import Segment
from backend.strategy.option_book import OptionBook, _underlying_name
from backend.strategy.options import strike_step
from backend.strategy.risk import RiskGuard
from backend.strategy.runner import FillModel, run_backtest
from backend.strategy.schema import OptionRight, Strategy

SYMBOL = "NSE_INDEX|Nifty Bank"


@pytest.fixture(scope="module")
def frame():
    return generate_ohlcv("BANKNIFTY", date(2026, 1, 1), date(2026, 4, 30), "5m", seed=31)


def _doc(right="PE", action="SELL", stop=None, target=None, always=True):
    leg = {
        "id": "leg1", "action": action, "kind": "option", "lots": 1,
        "option": {"right": right, "strike_rule": "ATM", "expiry": "current_monthly"},
    }
    if stop:
        leg["stop_loss"] = {"type": "percent", "value": stop}
    if target:
        leg["target"] = {"type": "percent", "value": target}
    entry = (
        {"op": "AND", "conditions": [{"left": "ema9", "cmp": "crosses_above", "right": "sma50"}]}
        if not always
        else {"op": "AND", "conditions": [{"left": "close", "cmp": ">", "right": 1}]}
    )
    return Strategy.model_validate({
        "name": "opt", "instrument": {"symbol": SYMBOL, "segment": "index"},
        "timeframe": "5m",
        "session": {"start": "09:20", "end": "15:05", "square_off": "15:10"},
        "indicators": [
            {"id": "ema9", "type": "EMA", "params": {"period": 9}},
            {"id": "sma50", "type": "SMA", "params": {"period": 50}},
        ],
        "entry": entry,
        "exit": {"op": "OR", "conditions": [
            {"left": "ema9", "cmp": "crosses_below", "right": "sma50"}]},
        "legs": [leg],
        "risk": {"max_concurrent_positions": 1,
                 "re_entry": {"enabled": True, "max": 20, "cooldown_bars": 1}},
    })


def _run(strategy, frame, iv=0.0):
    return run_backtest(
        strategy, frame, starting_capital=Decimal("500000"), lot_size=30,
        fill_model=FillModel(slippage_bps=5.0, segment=Segment.OPTIONS),
        risk=RiskGuard(rules=strategy.risk, max_leverage=Decimal("5")),
        collect_events=False, iv_premium=iv,
    )


class TestResolution:
    def test_underlying_name_maps_the_upstox_key(self):
        assert _underlying_name("NSE_INDEX|Nifty Bank") == "BANKNIFTY"
        assert _underlying_name("NSE_INDEX|Nifty 50") == "NIFTY"

    def test_banknifty_uses_its_own_strike_grid(self):
        """'NIFTY' is a substring of 'BANKNIFTY'; the 50-point step is not."""
        assert strike_step("BANKNIFTY") == 100
        assert strike_step("NIFTY") == 50

    def test_strikes_land_on_the_exchange_grid(self, frame):
        book = OptionBook(_doc(), frame)
        leg = book.option_legs[0]
        for index in (200, 900, 2000):
            book.release("leg1")
            option = book.open(leg, index)
            assert option.strike % 100 == 0, option.label
            spot = float(frame["close"].iloc[index])
            assert abs(option.strike - spot) <= 100


class TestPricing:
    def test_fills_are_premiums_not_the_index(self, frame):
        """The whole point. A premium is hundreds; the index is tens of thousands."""
        result = _run(_doc(), frame)
        assert result.trades
        for trade in result.trades:
            assert 1 < float(trade.entry_price) < 5000, (
                f"{trade.entry_price} is not a premium — this is the index price bug"
            )
            # An option finishing out of the money settles at zero, which is a
            # real outcome rather than a pricing failure; the index bug would show
            # up as tens of thousands, not as zero.
            assert 0 <= float(trade.exit_price) < 5000

    def test_a_live_option_never_prices_below_the_minimum_tick(self, frame):
        """Quoting 0.02 lets a backtest buy back a short for free."""
        book = OptionBook(_doc(), frame)
        leg = book.option_legs[0]
        option = book.open(leg, 100)

        far = type(option)(
            underlying=option.underlying, right=OptionRight.PE,
            strike=option.strike - 8000,          # deep out of the money
            expiry=option.expiry, synthetic=True,
        )
        bar = book.price_bar(far, 100)
        assert bar.low >= 0.05
        assert bar.close >= 0.05

    def test_the_trade_log_names_the_contract(self, frame):
        result = _run(_doc(), frame)
        symbol = result.trades[0].symbol
        assert "BANKNIFTY" in symbol and "PE" in symbol
        assert symbol != SYMBOL, "position still keyed to the index, so equity marks wrong"

    def test_a_puts_high_comes_from_the_underlyings_low(self, frame):
        """Getting this inversion wrong stops out every put on its winning move."""
        put = OptionBook(_doc(right="PE"), frame)
        call = OptionBook(_doc(right="CE"), frame)
        index = 500

        p = put.price_bar(put.open(put.option_legs[0], index), index)
        c = call.price_bar(call.open(call.option_legs[0], index), index)

        assert p.high >= p.close >= p.low
        assert c.high >= c.close >= c.low

        row = frame.iloc[index]
        if float(row["high"]) > float(row["low"]):
            # Same underlying bar, opposite responses.
            assert p.high > p.low and c.high > c.low

    def test_a_higher_iv_makes_options_dearer(self, frame):
        book_low = OptionBook(_doc(), frame, iv_premium=0.0)
        book_high = OptionBook(_doc(), frame, iv_premium=0.06)
        leg = book_low.option_legs[0]
        cheap = book_low.price_bar(book_low.open(leg, 400), 400)
        dear = book_high.price_bar(book_high.open(leg, 400), 400)
        assert dear.close > cheap.close

    def test_raising_iv_helps_the_seller_and_hurts_the_buyer(self, frame):
        """The direction that makes an IV assumption matter for a buy/sell comparison."""
        sell = _run(_doc(right="PE", action="SELL"), frame, iv=0.0).portfolio.realized_pnl
        sell_rich = _run(_doc(right="PE", action="SELL"), frame, iv=0.06).portfolio.realized_pnl
        buy = _run(_doc(right="CE", action="BUY"), frame, iv=0.0).portfolio.realized_pnl
        buy_rich = _run(_doc(right="CE", action="BUY"), frame, iv=0.06).portfolio.realized_pnl

        assert sell_rich > sell, "selling richer options should collect more"
        assert buy_rich < buy, "buying dearer options should cost more"


class TestPositionManagement:
    def test_stops_are_measured_in_premium_not_index_points(self, frame):
        """A 25% stop on a 300-point premium is 75 points, not 25% of BANKNIFTY.

        Comparing a premium-derived stop against the index bar would put the level
        thousands of points away and it would never trigger.
        """
        result = _run(_doc(stop=25, target=40), frame)
        reasons = {t.reason.value for t in result.trades}
        assert reasons & {"stop_loss", "target"}, (
            f"no premium-based exit ever fired: {reasons}"
        )

        for trade in result.trades:
            if trade.reason.value == "stop_loss":
                # Short put: a stop means the premium ROSE against us.
                assert trade.exit_price > trade.entry_price
                assert trade.gross_pnl < 0

    def test_each_entry_resolves_a_fresh_contract(self, frame):
        """Holding the old strike would trade one chosen for a spot long gone."""
        result = _run(_doc(), frame)
        book = result.option_book
        assert len(book.contracts) == len(result.trades)
        assert len({c["contract"] for c in book.contracts}) > 1

        for contract, trade in zip(book.contracts, result.trades):
            assert abs(contract["strike"] - contract["spot_at_entry"]) <= 100

    def test_no_position_is_left_open(self, frame):
        result = _run(_doc(), frame)
        assert not result.portfolio.has_open_positions

    def test_equity_curve_is_the_strategy_not_the_index(self, frame):
        """Marking an open option at the index would draw BANKNIFTY, not the P&L."""
        result = _run(_doc(), frame)
        values = [float(v) for _, v in result.equity_curve]
        assert max(values) < 5_000_000, "equity swings like an index — marking is wrong"
        assert min(values) > 0


class TestRisingFalling:
    def test_rising_and_falling_read_the_previous_bar(self):
        from backend.indicators.base import PriceBar
        from backend.strategy.conditions import evaluate_condition
        from backend.strategy.indicator_set import StreamingIndicatorSet
        from backend.strategy.schema import Condition

        indicators = StreamingIndicatorSet([], [])
        up = Condition.model_validate({"left": "close", "cmp": "rising", "right": 0})
        down = Condition.model_validate({"left": "close", "cmp": "falling", "right": 0})

        indicators.update(PriceBar(100.0, 101.0, 99.0, 100.0, 10))
        assert evaluate_condition(up, indicators) is False   # no previous bar yet

        indicators.update(PriceBar(100.0, 103.0, 100.0, 102.0, 10))
        assert evaluate_condition(up, indicators) is True
        assert evaluate_condition(down, indicators) is False

        indicators.update(PriceBar(102.0, 102.0, 98.0, 99.0, 10))
        assert evaluate_condition(up, indicators) is False
        assert evaluate_condition(down, indicators) is True

    def test_rising_matches_between_streaming_and_vectorized(self, frame):
        """Same guarantee every other comparator has: live must equal backtest."""
        from backend.indicators.base import PriceBar
        from backend.strategy.conditions import evaluate_condition
        from backend.strategy.indicator_set import (
            StreamingIndicatorSet,
            VectorizedIndicatorSet,
        )
        from backend.strategy.schema import Condition, IndicatorSpec

        specs = [IndicatorSpec.model_validate(
            {"id": "rsi", "type": "RSI", "params": {"period": 14}}
        )]
        condition = Condition.model_validate({"left": "rsi", "cmp": "rising", "right": 0})

        sample = frame.head(400).reset_index(drop=True)
        vectorized = VectorizedIndicatorSet(sample, specs, [])
        streaming = StreamingIndicatorSet(specs, [])

        for i, row in enumerate(sample.itertuples(index=False)):
            vectorized.seek(i)
            streaming.update(PriceBar(row.open, row.high, row.low, row.close, row.volume))
            assert evaluate_condition(condition, vectorized) == evaluate_condition(
                condition, streaming
            ), f"rising disagreed at bar {i}"
