"""Phase 3 gate: schema validation, condition evaluation, and engine semantics.

The two gate tests are `test_hand_computed_scenario` and `test_no_lookahead_*`.
Everything else guards the invariants those two depend on.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

import pandas as pd
import pytest
from pydantic import ValidationError

from backend.core.config import IST
from backend.core.interfaces import Side
from backend.data.simulator import generate_ohlcv
from backend.strategy import conditions, templates
from backend.strategy.engine import StrategyEngine
from backend.strategy.indicator_set import StreamingIndicatorSet, VectorizedIndicatorSet
from backend.strategy.risk import Block, RiskGuard
from backend.strategy.runner import FillModel, run_backtest
from backend.strategy.schema import Strategy
from backend.strategy.state import Portfolio, Position, TradeReason


def make_frame(closes: list[float], start: str = "2026-02-02 09:15") -> pd.DataFrame:
    """Bars with a controlled close series. OHLC hugs the close so stops are
    predictable — intrabar behaviour is tested separately with explicit ranges."""
    ts = pd.date_range(start, periods=len(closes), freq="5min", tz=IST)
    return pd.DataFrame({
        "timestamp": ts,
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1000] * len(closes),
    })


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def test_all_templates_validate():
    for name in templates.TEMPLATES:
        strategy = templates.load(name)
        assert strategy.id.startswith("stg_")
        assert strategy.legs


def test_json_roundtrip_preserves_hash():
    original = templates.load("ema_crossover")
    restored = Strategy.from_json(original.to_json())
    assert restored.content_hash() == original.content_hash()


def test_hash_ignores_name_and_description():
    """Renaming must not orphan historical results."""
    a = templates.load("ema_crossover")
    b = a.model_copy(update={"name": "Totally Different Name", "description": "x"})
    assert a.content_hash() == b.content_hash()


def test_hash_changes_with_behaviour():
    a = templates.load("ema_crossover")
    payload = a.model_dump(mode="json")
    payload["indicators"][0]["params"]["period"] = 50
    b = Strategy.model_validate(payload)
    assert a.content_hash() != b.content_hash()


def test_unknown_indicator_type_rejected():
    with pytest.raises(ValidationError, match="Unknown indicator"):
        Strategy.model_validate({
            "name": "x", "instrument": {"symbol": "S"},
            "indicators": [{"id": "a", "type": "NOTREAL"}],
            "entry": {"conditions": [{"left": "a", "cmp": ">", "right": 1}]},
            "legs": [{"id": "l", "action": "BUY"}],
        })


def test_unknown_indicator_param_rejected():
    with pytest.raises(ValidationError, match="unknown parameter"):
        Strategy.model_validate({
            "name": "x", "instrument": {"symbol": "S"},
            "indicators": [{"id": "a", "type": "EMA", "params": {"perid": 9}}],
            "entry": {"conditions": [{"left": "a", "cmp": ">", "right": 1}]},
            "legs": [{"id": "l", "action": "BUY"}],
        })


def test_typo_in_condition_reference_rejected():
    """The check that turns a silent no-trade strategy into a load-time error."""
    with pytest.raises(ValidationError, match="unknown name"):
        Strategy.model_validate({
            "name": "x", "instrument": {"symbol": "S"},
            "indicators": [{"id": "ema_fast", "type": "EMA"}],
            "entry": {"conditions": [{"left": "ema_fastt", "cmp": ">", "right": 1}]},
            "legs": [{"id": "l", "action": "BUY"}],
        })


def test_duplicate_indicator_ids_rejected():
    with pytest.raises(ValidationError, match="Duplicate indicator"):
        Strategy.model_validate({
            "name": "x", "instrument": {"symbol": "S"},
            "indicators": [{"id": "a", "type": "EMA"}, {"id": "a", "type": "SMA"}],
            "entry": {"conditions": [{"left": "a", "cmp": ">", "right": 1}]},
            "legs": [{"id": "l", "action": "BUY"}],
        })


def test_atr_ref_must_point_at_atr():
    with pytest.raises(ValidationError, match="must be an ATR"):
        Strategy.model_validate({
            "name": "x", "instrument": {"symbol": "S"},
            "indicators": [{"id": "ema", "type": "EMA"}],
            "entry": {"conditions": [{"left": "close", "cmp": ">", "right": 1}]},
            "legs": [{"id": "l", "action": "BUY",
                      "stop_loss": {"type": "atr_multiple", "value": 2, "ref": "ema"}}],
        })


def test_strategy_with_no_exit_path_rejected():
    with pytest.raises(ValidationError, match="no exit path"):
        Strategy.model_validate({
            "name": "x", "instrument": {"symbol": "S"},
            "entry": {"conditions": [{"left": "close", "cmp": ">", "right": 1}]},
            "legs": [{"id": "l", "action": "BUY"}],
            "session": {"start": "09:20", "end": "15:10", "square_off": None},
        })


def test_option_leg_requires_option_block():
    with pytest.raises(ValidationError, match="no `option` block"):
        Strategy.model_validate({
            "name": "x", "instrument": {"symbol": "S"},
            "entry": {"conditions": [{"left": "close", "cmp": ">", "right": 1}]},
            "legs": [{"id": "l", "action": "BUY", "kind": "option"}],
        })


def test_invalid_strike_rule_rejected():
    with pytest.raises(ValidationError, match="Invalid strike_rule"):
        Strategy.model_validate({
            "name": "x", "instrument": {"symbol": "S"},
            "entry": {"conditions": [{"left": "close", "cmp": ">", "right": 1}]},
            "legs": [{"id": "l", "action": "BUY", "kind": "option",
                      "option": {"right": "CE", "strike_rule": "somewhere near the money"}}],
        })


def test_square_off_must_follow_session_end():
    with pytest.raises(ValidationError, match="square_off"):
        Strategy.model_validate({
            "name": "x", "instrument": {"symbol": "S"},
            "entry": {"conditions": [{"left": "close", "cmp": ">", "right": 1}]},
            "legs": [{"id": "l", "action": "BUY"}],
            "session": {"start": "09:20", "end": "15:10", "square_off": "14:00"},
        })


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

@pytest.fixture
def simple_indicators():
    df = make_frame([100, 101, 102, 103, 104])
    strategy = Strategy.model_validate({
        "name": "t", "instrument": {"symbol": "S"},
        "indicators": [{"id": "sma", "type": "SMA", "params": {"period": 3}}],
        "entry": {"conditions": [{"left": "close", "cmp": ">", "right": "sma"}]},
        "legs": [{"id": "l", "action": "BUY"}],
    })
    return VectorizedIndicatorSet(df, strategy.indicators), strategy


def test_nan_never_evaluates_true(simple_indicators):
    """During warm-up the SMA is NaN; no condition may fire."""
    indicators, strategy = simple_indicators
    indicators.seek(0)
    assert conditions.evaluate(strategy.entry, indicators) is False
    indicators.seek(1)
    assert conditions.evaluate(strategy.entry, indicators) is False


def test_comparison_after_warmup(simple_indicators):
    indicators, strategy = simple_indicators
    indicators.seek(3)  # close=103, sma(3) of 101,102,103 = 102
    assert conditions.evaluate(strategy.entry, indicators) is True


def test_crossover_fires_once():
    """crosses_above is true only on the flip bar, not while merely above."""
    df = make_frame([10, 10, 10, 20, 20, 20])
    strategy = Strategy.model_validate({
        "name": "t", "instrument": {"symbol": "S"},
        "entry": {"conditions": [{"left": "close", "cmp": "crosses_above", "right": 15}]},
        "legs": [{"id": "l", "action": "BUY"}],
    })
    indicators = VectorizedIndicatorSet(df, [])
    fired = []
    for i in range(len(df)):
        indicators.seek(i)
        fired.append(conditions.evaluate(strategy.entry, indicators))
    assert fired == [False, False, False, True, False, False]


def test_crosses_below():
    df = make_frame([20, 20, 10, 10])
    strategy = Strategy.model_validate({
        "name": "t", "instrument": {"symbol": "S"},
        "entry": {"conditions": [{"left": "close", "cmp": "crosses_below", "right": 15}]},
        "legs": [{"id": "l", "action": "BUY"}],
    })
    indicators = VectorizedIndicatorSet(df, [])
    fired = []
    for i in range(len(df)):
        indicators.seek(i)
        fired.append(conditions.evaluate(strategy.entry, indicators))
    assert fired == [False, False, True, False]


def test_and_or_nesting():
    df = make_frame([100, 200, 300])
    strategy = Strategy.model_validate({
        "name": "t", "instrument": {"symbol": "S"},
        "entry": {"op": "AND", "conditions": [
            {"left": "close", "cmp": ">", "right": 50},
            {"op": "OR", "conditions": [
                {"left": "close", "cmp": ">", "right": 250},
                {"left": "close", "cmp": "<", "right": 150},
            ]},
        ]},
        "legs": [{"id": "l", "action": "BUY"}],
    })
    indicators = VectorizedIndicatorSet(df, [])
    got = []
    for i in range(len(df)):
        indicators.seek(i)
        got.append(conditions.evaluate(strategy.entry, indicators))
    assert got == [True, False, True]


def test_inline_indicator_operand():
    df = make_frame([100] * 25)
    df.loc[24, "volume"] = 99999
    strategy = Strategy.model_validate({
        "name": "t", "instrument": {"symbol": "S"},
        "entry": {"conditions": [
            {"left": "volume", "cmp": ">",
             "right": {"type": "SMA", "of": "volume", "period": 20}},
        ]},
        "legs": [{"id": "l", "action": "BUY"}],
    })
    inline = conditions.collect_inline(strategy.entry)
    assert len(inline) == 1
    indicators = VectorizedIndicatorSet(df, [], inline)
    indicators.seek(24)
    assert conditions.evaluate(strategy.entry, indicators) is True


def _inline_doc(right: dict) -> dict:
    return {
        "name": "t", "instrument": {"symbol": "S"},
        "entry": {"conditions": [{"left": "volume", "cmp": ">", "right": right}]},
        "legs": [{"id": "l", "action": "BUY"}],
    }


def test_inline_indicator_rejects_misspelled_param():
    """A typo must fail at load, not as a TypeError when a backtest computes it."""
    with pytest.raises(ValidationError, match="unknown parameter"):
        Strategy.model_validate(_inline_doc({"type": "SMA", "of": "volume", "lenght": 20}))


def test_inline_indicator_rejects_unknown_input():
    with pytest.raises(ValidationError, match="input"):
        Strategy.model_validate(_inline_doc({"type": "SMA", "of": "volumee", "period": 20}))


def test_inline_indicator_accepts_declared_indicator_as_input():
    doc = _inline_doc({"type": "SMA", "of": "rsi", "period": 5})
    doc["indicators"] = [{"id": "rsi", "type": "RSI", "params": {"period": 14}}]
    Strategy.model_validate(doc)


def test_explain_reports_values(simple_indicators):
    indicators, strategy = simple_indicators
    indicators.seek(3)
    report = conditions.explain(strategy.entry, indicators)
    assert report["result"] is True
    leaf = report["conditions"][0]
    assert leaf["left_value"] == 103.0
    assert leaf["right_value"] == 102.0


# ---------------------------------------------------------------------------
# IndicatorSet parity — vectorized vs streaming, at the strategy layer
# ---------------------------------------------------------------------------

def test_indicator_sets_agree():
    """The Phase 2 guarantee must survive the IndicatorSet wrapper."""
    df = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 2, 28), "5m", seed=3)
    strategy = templates.load("ema_crossover")

    vec = VectorizedIndicatorSet(df, strategy.indicators)
    stream = StreamingIndicatorSet(strategy.indicators)

    from backend.indicators.base import bars_from_frame

    for i, bar in enumerate(bars_from_frame(df)):
        vec.seek(i)
        stream.update(bar)
        for ref in ("ema_fast", "ema_slow", "atr"):
            a, b = vec.value(ref), stream.value(ref)
            if a != a and b != b:  # both NaN
                continue
            assert abs(a - b) < 1e-9, f"{ref} diverged at bar {i}: {a} vs {b}"


# ---------------------------------------------------------------------------
# Position arithmetic
# ---------------------------------------------------------------------------

def test_position_average_price_on_add():
    p = Position(symbol="S", leg_id="l")
    p.apply_fill(Side.BUY, 10, Decimal("100"))
    p.apply_fill(Side.BUY, 10, Decimal("110"))
    assert p.quantity == 20
    assert p.average_price == Decimal("105")


def test_position_realizes_on_close():
    p = Position(symbol="S", leg_id="l")
    p.apply_fill(Side.BUY, 10, Decimal("100"))
    realized = p.apply_fill(Side.SELL, 10, Decimal("110"))
    assert realized == Decimal("100")
    assert p.quantity == 0
    assert p.average_price == Decimal("0")


def test_position_partial_close():
    p = Position(symbol="S", leg_id="l")
    p.apply_fill(Side.BUY, 10, Decimal("100"))
    realized = p.apply_fill(Side.SELL, 4, Decimal("110"))
    assert realized == Decimal("40")
    assert p.quantity == 6
    assert p.average_price == Decimal("100")  # unchanged on a reduce


def test_position_reversal_opens_fresh():
    """Reversing must not average across zero — that price never traded."""
    p = Position(symbol="S", leg_id="l")
    p.apply_fill(Side.BUY, 10, Decimal("100"))
    realized = p.apply_fill(Side.SELL, 15, Decimal("110"))
    assert realized == Decimal("100")       # booked on the 10 closed
    assert p.quantity == -5
    assert p.average_price == Decimal("110")


def test_short_position_pnl():
    p = Position(symbol="S", leg_id="l")
    p.apply_fill(Side.SELL, 10, Decimal("100"))
    assert p.is_short
    assert p.unrealized_pnl(Decimal("90")) == Decimal("100")   # price fell, short profits
    assert p.pnl_percent(Decimal("90")) == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# RiskGuard
# ---------------------------------------------------------------------------

def test_daily_loss_limit_halts_and_stays_halted():
    from backend.strategy.schema import RiskRules

    guard = RiskGuard(rules=RiskRules(max_loss_per_day=1000))
    portfolio = Portfolio()
    portfolio.book_realized(Decimal("-1500"))

    assert not guard.may_enter(portfolio, 10)
    assert guard.halted

    # Recovering P&L must not un-halt within the session.
    portfolio.book_realized(Decimal("5000"))
    decision = guard.may_enter(portfolio, 11)
    assert not decision
    assert decision.reason is Block.DAILY_LOSS


def test_account_limit_cannot_be_raised_by_strategy():
    """A permissive strategy limit must not override the tighter account cap."""
    from backend.strategy.schema import RiskRules

    guard = RiskGuard(
        rules=RiskRules(max_loss_per_day=100_000),
        account_max_daily_loss=Decimal("500"),
    )
    portfolio = Portfolio()
    portfolio.book_realized(Decimal("-600"))

    decision = guard.may_enter(portfolio, 1)
    assert not decision
    assert decision.reason is Block.ACCOUNT_HALTED


def test_max_trades_per_day():
    from backend.strategy.schema import RiskRules

    guard = RiskGuard(rules=RiskRules(max_trades_per_day=2))
    portfolio = Portfolio()
    guard.record_entry()
    guard.record_entry()
    decision = guard.may_enter(portfolio, 5)
    assert not decision
    assert decision.reason is Block.MAX_TRADES


def test_re_entry_disabled_blocks_second_entry():
    from backend.strategy.schema import RiskRules

    guard = RiskGuard(rules=RiskRules())
    portfolio = Portfolio()
    guard.record_entry()
    guard.record_exit(bar_index=5)
    decision = guard.may_enter(portfolio, 6)
    assert not decision
    assert decision.reason is Block.RE_ENTRY_EXHAUSTED


def test_re_entry_cooldown():
    from backend.strategy.schema import ReEntryRule, RiskRules

    rules = RiskRules(re_entry=ReEntryRule(enabled=True, max=5, cooldown_bars=3))
    guard = RiskGuard(rules=rules)
    portfolio = Portfolio()
    guard.record_entry()
    guard.record_exit(bar_index=10)

    assert not guard.may_enter(portfolio, 12)   # only 2 bars elapsed
    assert guard.may_enter(portfolio, 13)       # 3 bars elapsed


def test_new_day_resets_halt():
    from backend.strategy.schema import RiskRules

    guard = RiskGuard(rules=RiskRules(max_loss_per_day=100))
    portfolio = Portfolio()
    portfolio.book_realized(Decimal("-200"))
    guard.may_enter(portfolio, 1)
    assert guard.halted

    guard.start_new_day()
    portfolio.start_new_day()
    assert not guard.halted
    assert guard.may_enter(portfolio, 1)


def test_live_requires_account_limits(monkeypatch):
    from backend.core.config import settings

    monkeypatch.setattr(settings, "max_daily_loss", None)
    with pytest.raises(RuntimeError, match="MAX_DAILY_LOSS"):
        RiskGuard.require_account_limits()


# ---------------------------------------------------------------------------
# THE GATE: hand-computed scenario
# ---------------------------------------------------------------------------

def test_hand_computed_scenario():
    """A 20-bar series with a known SMA cross must produce exactly one trade,
    entered and exited at prices we can verify by hand.

    Closes: ten bars at 100, then a step to 110 and back down.
    SMA(3) crosses below close on the step up, and above it on the step down.
    Entry fills at the OPEN of the bar AFTER the signal — that is the property
    under test.
    """
    closes = [100] * 10 + [110] * 5 + [90] * 5
    df = make_frame([float(c) for c in closes])

    strategy = Strategy.model_validate({
        "name": "Hand Check",
        "instrument": {"symbol": "TEST"},
        "timeframe": "5m",
        "session": {"start": "09:15", "end": "15:10", "square_off": None},
        "indicators": [{"id": "sma", "type": "SMA", "params": {"period": 3}}],
        "entry": {"conditions": [{"left": "close", "cmp": "crosses_above", "right": "sma"}]},
        "exit": {"conditions": [{"left": "close", "cmp": "crosses_below", "right": "sma"}]},
        "legs": [{"id": "leg1", "action": "BUY", "kind": "equity", "lots": 1}],
        "risk": {"max_concurrent_positions": 1},
    })

    result = run_backtest(
        strategy, df, lot_size=1,
        fill_model=FillModel(slippage_bps=0, fee_per_order=Decimal("0")),
    )

    assert len(result.trades) == 1, [t.as_dict() for t in result.trades]
    trade = result.trades[0]

    # Bar 10 close=110 vs SMA(3 of 100,100,110)=103.33 -> crosses above.
    # Fill happens at bar 11's OPEN, which make_frame sets equal to its close: 110.
    assert trade.entry_price == Decimal("110")

    # Bar 15 close=90 vs SMA(3 of 110,110,90)=103.33 -> crosses below.
    # Fill at bar 16's open = 90.
    assert trade.exit_price == Decimal("90")
    assert trade.gross_pnl == Decimal("-20")
    assert trade.direction == "LONG"
    assert trade.reason is TradeReason.EXIT_SIGNAL


def test_entry_fills_at_next_bar_not_signal_bar():
    """The single most important execution property in the system."""
    closes = [100, 100, 100, 100, 150, 200, 200]
    df = make_frame([float(c) for c in closes])
    df.loc[5, "open"] = 175.0  # distinct open so we can tell which bar filled

    strategy = Strategy.model_validate({
        "name": "Fill Timing", "instrument": {"symbol": "TEST"},
        "session": {"start": "09:15", "end": "15:10", "square_off": None},
        "entry": {"conditions": [{"left": "close", "cmp": "crosses_above", "right": 120}]},
        "exit": {"conditions": [{"left": "close", "cmp": "<", "right": 0}]},
        "legs": [{"id": "leg1", "action": "BUY", "kind": "equity"}],
    })

    result = run_backtest(
        strategy, df, fill_model=FillModel(slippage_bps=0, fee_per_order=Decimal("0"))
    )
    assert len(result.trades) == 1
    # Signal on bar 4 (close 150 crosses 120). Fill must be bar 5's open = 175,
    # NOT bar 4's close of 150.
    assert result.trades[0].entry_price == Decimal("175")


def test_stop_loss_fills_intrabar():
    """A resting stop triggers inside the bar, at the stop price."""
    df = make_frame([100.0, 100.0, 100.0, 100.0])
    df.loc[3, "low"] = 90.0   # this bar's range reaches down to the stop

    strategy = Strategy.model_validate({
        "name": "Stop Test", "instrument": {"symbol": "TEST"},
        "session": {"start": "09:15", "end": "15:10", "square_off": None},
        "entry": {"conditions": [{"left": "close", "cmp": ">", "right": 50}]},
        "exit": {"conditions": [{"left": "close", "cmp": "<", "right": 0}]},
        "legs": [{"id": "leg1", "action": "BUY", "kind": "equity",
                  "stop_loss": {"type": "percent", "value": 5}}],
    })

    result = run_backtest(
        strategy, df, fill_model=FillModel(slippage_bps=0, fee_per_order=Decimal("0"))
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.reason is TradeReason.STOP_LOSS
    assert trade.exit_price == Decimal("95")  # 100 - 5%


def test_stop_assumed_before_target_when_both_touched():
    """Pessimistic tie-break. Both levels inside one bar must resolve to the stop."""
    df = make_frame([100.0, 100.0, 100.0])
    df.loc[2, "high"] = 120.0   # target at +10 reachable
    df.loc[2, "low"] = 90.0     # stop at -5 also reachable

    strategy = Strategy.model_validate({
        "name": "Tie Break", "instrument": {"symbol": "TEST"},
        "session": {"start": "09:15", "end": "15:10", "square_off": None},
        "entry": {"conditions": [{"left": "close", "cmp": ">", "right": 50}]},
        "exit": {"conditions": [{"left": "close", "cmp": "<", "right": 0}]},
        "legs": [{"id": "leg1", "action": "BUY", "kind": "equity",
                  "stop_loss": {"type": "percent", "value": 5},
                  "target": {"type": "percent", "value": 10}}],
    })

    result = run_backtest(
        strategy, df, fill_model=FillModel(slippage_bps=0, fee_per_order=Decimal("0"))
    )
    assert result.trades[0].reason is TradeReason.STOP_LOSS


def test_square_off_closes_position():
    ts = pd.date_range("2026-02-02 15:00", periods=5, freq="5min", tz=IST)
    df = pd.DataFrame({
        "timestamp": ts, "open": [100.0] * 5, "high": [101.0] * 5,
        "low": [99.0] * 5, "close": [100.0] * 5, "volume": [1000] * 5,
    })

    strategy = Strategy.model_validate({
        "name": "Square Off", "instrument": {"symbol": "TEST"},
        "session": {"start": "09:15", "end": "15:05", "square_off": "15:15"},
        "entry": {"conditions": [{"left": "close", "cmp": ">", "right": 50}]},
        "legs": [{"id": "leg1", "action": "BUY", "kind": "equity"}],
    })

    result = run_backtest(
        strategy, df, fill_model=FillModel(slippage_bps=0, fee_per_order=Decimal("0"))
    )
    assert len(result.trades) == 1
    assert result.trades[0].reason is TradeReason.SQUARE_OFF


def test_slippage_always_hurts():
    """Buy fills worse, sell fills worse. A round trip must lose to slippage."""
    df = make_frame([100.0] * 6)
    strategy = Strategy.model_validate({
        "name": "Slip", "instrument": {"symbol": "TEST"},
        "session": {"start": "09:15", "end": "15:10", "square_off": None},
        "entry": {"conditions": [{"left": "close", "cmp": ">", "right": 50}]},
        "exit": {"conditions": [{"left": "close", "cmp": "<", "right": 0}]},
        "legs": [{"id": "leg1", "action": "BUY", "kind": "equity"}],
    })
    result = run_backtest(
        strategy, df, fill_model=FillModel(slippage_bps=100, fee_per_order=Decimal("0"))
    )
    assert result.trades[0].gross_pnl < 0


def test_positions_flattened_at_end_of_data():
    """An open position at the end would hide its loss from the metrics."""
    df = make_frame([100.0] * 5)
    strategy = Strategy.model_validate({
        "name": "Flatten", "instrument": {"symbol": "TEST"},
        "session": {"start": "09:15", "end": "15:10", "square_off": None},
        "entry": {"conditions": [{"left": "close", "cmp": ">", "right": 50}]},
        "exit": {"conditions": [{"left": "close", "cmp": "<", "right": 0}]},
        "legs": [{"id": "leg1", "action": "BUY", "kind": "equity"}],
    })
    result = run_backtest(strategy, df)
    assert not result.portfolio.has_open_positions
    assert len(result.trades) == 1


# ---------------------------------------------------------------------------
# THE GATE: no lookahead
# ---------------------------------------------------------------------------

def test_no_lookahead_truncation_invariance():
    """Trades taken in the first N bars must not change when later bars exist.

    If appending future data changes a past decision, the engine is reading
    ahead. This is the same property proven for indicators in Phase 2, now
    asserted end-to-end through signals, risk and fills.
    """
    full = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 4, 30), "5m", seed=21)
    cut = 2000
    head = full.iloc[:cut].reset_index(drop=True)

    strategy = templates.load("ema_crossover", symbol="TEST")

    a = run_backtest(strategy, full, collect_events=False)
    b = run_backtest(strategy, head, collect_events=False)

    # Compare trades that opened strictly before the cut in both runs.
    def early(result):
        return [
            (t.entry_time, t.entry_price, t.exit_price, t.reason)
            for t in result.trades
            if t.entry_time < head["timestamp"].iloc[-1]
        ]

    early_a, early_b = early(a), early(b)
    # The final trade in the truncated run may be cut short by end-of-data
    # flattening, so compare everything before it.
    assert early_a[: len(early_b) - 1] == early_b[:-1]


def test_engine_cannot_see_future_bar():
    """Directly: the engine is handed one bar and has no handle on the frame."""
    df = make_frame([100.0] * 10)
    strategy = templates.load("ema_crossover", symbol="TEST")
    engine = StrategyEngine(strategy)
    indicators = engine.prepare_backtest(df)

    from backend.indicators.base import bars_from_frame

    bars = list(bars_from_frame(df))
    indicators.seek(3)
    ts = datetime(2026, 2, 2, 9, 30, tzinfo=IST)
    event = engine.on_bar(3, ts, bars[3], indicators)

    assert event.index == 3
    # Whatever it decided, it can only have used bars 0..3.
    assert indicators.value("close") == 100.0
    assert indicators.previous("close") == 100.0


# ---------------------------------------------------------------------------
# Templates run end to end
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", templates.DIRECTIONAL)
def test_directional_templates_run(name):
    """Every directional template must run without error on real-shaped data."""
    df = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 3, 31), "5m", seed=5)
    strategy = templates.load(name, symbol="TEST")
    result = run_backtest(strategy, df, collect_events=False)

    assert result.strategy_hash == strategy.content_hash()
    assert not result.portfolio.has_open_positions
    for trade in result.trades:
        assert trade.entry_time <= trade.exit_time
        assert trade.quantity > 0


# ---------------------------------------------------------------------------
# Regression: the duplicate-Side-enum bug
# ---------------------------------------------------------------------------

def test_side_enum_has_exactly_one_definition():
    """schema.Side must BE core.Side, not merely equal it.

    Two structurally identical enums compare `==` but not `is`, and
    Position.apply_fill dispatches on `is`. When these diverged, every BUY leg
    opened a SHORT position and every backtest reported the inverse of the truth.
    """
    from backend.core.interfaces import Side as CoreSide
    from backend.strategy.schema import Side as SchemaSide

    assert SchemaSide is CoreSide
    assert SchemaSide.BUY is CoreSide.BUY


def test_buy_leg_opens_long_position():
    """End-to-end direction check — the observable symptom of the enum bug."""
    df = make_frame([100.0, 100.0, 110.0, 110.0])
    strategy = Strategy.model_validate({
        "name": "Direction", "instrument": {"symbol": "TEST"},
        "session": {"start": "09:15", "end": "15:10", "square_off": None},
        "entry": {"conditions": [{"left": "close", "cmp": ">", "right": 50}]},
        "exit": {"conditions": [{"left": "close", "cmp": "<", "right": 0}]},
        "legs": [{"id": "leg1", "action": "BUY", "kind": "equity"}],
    })
    result = run_backtest(
        strategy, df, fill_model=FillModel(slippage_bps=0, fee_per_order=Decimal("0"))
    )
    trade = result.trades[0]
    assert trade.direction == "LONG"
    # Bought at 100, market rose to 110 -> a BUY must profit.
    assert trade.gross_pnl > 0


def test_sell_leg_opens_short_position():
    df = make_frame([100.0, 100.0, 90.0, 90.0])
    strategy = Strategy.model_validate({
        "name": "Direction Short", "instrument": {"symbol": "TEST"},
        "session": {"start": "09:15", "end": "15:10", "square_off": None},
        "entry": {"conditions": [{"left": "close", "cmp": ">", "right": 50}]},
        "exit": {"conditions": [{"left": "close", "cmp": "<", "right": 0}]},
        "legs": [{"id": "leg1", "action": "SELL", "kind": "equity"}],
    })
    result = run_backtest(
        strategy, df, fill_model=FillModel(slippage_bps=0, fee_per_order=Decimal("0"))
    )
    trade = result.trades[0]
    assert trade.direction == "SHORT"
    # Sold at 100, market fell to 90 -> a SELL must profit.
    assert trade.gross_pnl > 0


# ---------------------------------------------------------------------------
# Preflight: data-dependent checks
# ---------------------------------------------------------------------------

def test_preflight_flags_volume_strategy_on_zero_volume_instrument():
    """An index has no volume, so a volume-gated strategy can never fire.

    Without this warning the symptom is an empty trade log, which is
    indistinguishable from "found no setups".
    """
    from backend.strategy.runner import preflight

    df = make_frame([100.0] * 200)
    df["volume"] = 0

    strategy = templates.load("volume_spike_breakout", symbol="TEST")
    warnings = preflight(strategy, df)
    assert any("volume" in w for w in warnings)


def test_preflight_silent_on_healthy_data():
    from backend.strategy.runner import preflight

    df = generate_ohlcv("RELIANCE", date(2026, 1, 1), date(2026, 4, 30), "5m", seed=2)
    strategy = templates.load("ema_crossover", symbol="TEST")
    assert preflight(strategy, df) == []


def test_preflight_flags_thin_data():
    from backend.strategy.runner import preflight

    df = make_frame([100.0] * 20)
    strategy = templates.load("ema_crossover", symbol="TEST")
    assert any("bars supplied" in w for w in preflight(strategy, df))


def test_session_context_populates_pivots():
    """CPR levels must reach the condition evaluator, or CPR strategies are dead."""
    from backend.strategy.runner import _session_context

    df = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 2, 28), "5m", seed=4)
    strategy = templates.load("cpr_breakout", symbol="TEST")
    context = _session_context(strategy, df)

    assert context is not None
    for key in ("pivot", "tc", "bc"):
        assert key in context.columns, f"{key} missing from session context"
    # Later sessions must carry real values, not NaN.
    assert context["pivot"].iloc[-1] == context["pivot"].iloc[-1]


def test_previous_works_on_context_fields():
    """Crossovers against pivot levels need previous() to reach context.

    When context lived in a separate dict, previous() returned NaN for it and
    every `close crosses_above tc` condition was permanently False.
    """
    from backend.strategy.runner import _session_context

    df = generate_ohlcv("RELIANCE", date(2026, 1, 1), date(2026, 2, 28), "5m", seed=4)
    strategy = templates.load("cpr_breakout", symbol="TEST")
    engine = StrategyEngine(strategy)
    indicators = engine.prepare_backtest(df)
    indicators.attach_context(_session_context(strategy, df))

    indicators.seek(len(df) - 1)
    assert indicators.value("tc") == indicators.value("tc")
    assert indicators.previous("tc") == indicators.previous("tc"), "previous(tc) is NaN"


def test_cpr_strategy_trades_on_volume_bearing_data():
    """With real volume present, the CPR template must actually produce trades."""
    df = generate_ohlcv("RELIANCE", date(2026, 1, 1), date(2026, 4, 30), "5m", seed=8)
    strategy = templates.load("cpr_breakout", symbol="TEST")
    result = run_backtest(strategy, df, collect_events=False)
    assert len(result.trades) > 0


# ---------------------------------------------------------------------------
# Regression: session filtering must not apply to daily bars
# ---------------------------------------------------------------------------

def test_daily_bars_ignore_intraday_session_window():
    """A daily bar's time-of-day is 00:00:00.

    Applying an intraday session window (09:15-15:20) to daily bars rejected
    every one of them, so positional strategies fired signals and took zero
    trades — indistinguishable from "found no setups". Session rules are
    intraday concepts and must not gate daily-or-coarser bars.
    """
    closes = [100.0 + i for i in range(40)]
    ts = pd.date_range("2026-01-01", periods=len(closes), freq="D", tz=IST)
    df = pd.DataFrame({
        "timestamp": ts, "open": closes,
        "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
        "close": closes, "volume": [1000] * len(closes),
    })

    strategy = Strategy.model_validate({
        "name": "Daily", "instrument": {"symbol": "TEST"},
        "timeframe": "1d",
        "session": {"start": "09:15", "end": "15:20", "square_off": "15:25"},
        "indicators": [{"id": "sma", "type": "SMA", "params": {"period": 5}}],
        "entry": {"conditions": [{"left": "close", "cmp": ">", "right": "sma"}]},
        "exit": {"conditions": [{"left": "close", "cmp": "<", "right": "sma"}]},
        "legs": [{"id": "leg1", "action": "BUY", "kind": "equity"}],
        "risk": {"re_entry": {"enabled": True, "max": 20, "cooldown_bars": 0}},
    })

    result = run_backtest(strategy, df, collect_events=True,
                          fill_model=FillModel(slippage_bps=0, fee_per_order=Decimal("0")))

    blocked = [e.blocked.value for e in result.events if e.blocked.value != "none"]
    assert "outside_session" not in blocked, "daily bars were rejected by an intraday window"
    assert len(result.trades) > 0


def test_daily_bars_never_square_off():
    """Square-off is an intraday flatten; on daily bars it would close every
    position one bar after entry, capping every trade at a single day."""
    closes = [100.0 + i for i in range(30)]
    ts = pd.date_range("2026-01-01", periods=len(closes), freq="D", tz=IST)
    df = pd.DataFrame({
        "timestamp": ts, "open": closes,
        "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
        "close": closes, "volume": [1000] * len(closes),
    })

    strategy = Strategy.model_validate({
        "name": "Daily Hold", "instrument": {"symbol": "TEST"},
        "timeframe": "1d",
        "session": {"start": "09:15", "end": "15:20", "square_off": "15:25"},
        "entry": {"conditions": [{"left": "close", "cmp": ">", "right": 0}]},
        "exit": {"conditions": [{"left": "close", "cmp": "<", "right": 0}]},
        "legs": [{"id": "leg1", "action": "BUY", "kind": "equity"}],
    })

    result = run_backtest(strategy, df,
                          fill_model=FillModel(slippage_bps=0, fee_per_order=Decimal("0")))
    assert all(t.reason is not TradeReason.SQUARE_OFF for t in result.trades)
    # One position held across the whole series, closed only at end of data.
    assert len(result.trades) == 1
    assert result.trades[0].bars_held > 5


def test_intraday_still_respects_session():
    """The fix must not disable session rules where they belong."""
    ts = pd.date_range("2026-02-02 09:00", periods=12, freq="5min", tz=IST)
    df = pd.DataFrame({
        "timestamp": ts, "open": [100.0] * 12, "high": [101.0] * 12,
        "low": [99.0] * 12, "close": [100.0] * 12, "volume": [1000] * 12,
    })
    strategy = Strategy.model_validate({
        "name": "Intraday", "instrument": {"symbol": "TEST"},
        "timeframe": "5m",
        "session": {"start": "09:30", "end": "15:10", "square_off": "15:15"},
        "entry": {"conditions": [{"left": "close", "cmp": ">", "right": 0}]},
        "exit": {"conditions": [{"left": "close", "cmp": "<", "right": 0}]},
        "legs": [{"id": "leg1", "action": "BUY", "kind": "equity"}],
    })
    result = run_backtest(strategy, df, collect_events=True,
                          fill_model=FillModel(slippage_bps=0, fee_per_order=Decimal("0")))
    blocked = [e.blocked.value for e in result.events if e.blocked.value != "none"]
    assert "outside_session" in blocked, "pre-09:30 bars should still be refused"
