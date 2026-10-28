"""Phase 4 gate: cost model exactness, metrics correctness, option resolution.

The gate test is `test_gate_costs_reconcile_to_the_rupee` — a strategy engineered
to have zero gross edge must lose exactly the sum of its charges, hand-computed
independently. If that reconciles, the cost model and the P&L accounting agree,
and every other number in the metrics panel rests on solid ground.
"""

from __future__ import annotations

import math

from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from backend.core.config import IST
from backend.core.interfaces import Side
from backend.analytics import metrics as M
from backend.data.simulator import generate_ohlcv
from backend.execution.costs import (
    SCHEDULES,
    Segment,
    breakeven_move,
    compute_costs,
    round_trip_cost,
    segment_for,
)
from backend.strategy import options as opt
from backend.strategy import templates
from backend.strategy.runner import FillModel, run_backtest
from backend.strategy.schema import OptionRight, OptionSpec, Strategy
from backend.strategy.state import ClosedTrade, TradeReason


# ---------------------------------------------------------------------------
# Cost model — exact arithmetic
# ---------------------------------------------------------------------------

def test_equity_intraday_buy_hand_computed():
    """250 shares at 2950 = 737,500 turnover. Every line checked by hand."""
    c = compute_costs(Segment.EQUITY_INTRADAY, Side.BUY, 250, Decimal("2950"))

    assert c.turnover == Decimal("737500.00")
    assert c.brokerage == Decimal("20.00")          # min(20, 0.1% = 737.50)
    assert c.stt == Decimal("0.00")                 # intraday STT is sell-side only
    assert c.exchange == Decimal("21.90")           # 0.00297%
    assert c.sebi == Decimal("0.74")                # 0.0001%
    assert c.stamp == Decimal("22.13")              # 0.003% buy side
    # GST is 18% of (brokerage + exchange + sebi + ipft), never of STT or stamp
    expected_gst = (c.brokerage + c.exchange + c.sebi + c.ipft) * Decimal("0.18")
    assert abs(c.gst - expected_gst) <= Decimal("0.01")


def test_equity_intraday_sell_charges_stt():
    c = compute_costs(Segment.EQUITY_INTRADAY, Side.SELL, 250, Decimal("2950"))
    assert c.stt == Decimal("184.38")   # 0.025% of 737,500
    assert c.stamp == Decimal("0.00")   # stamp duty is buy-side only


def test_gst_never_applies_to_stt_or_stamp():
    """A common modelling error that inflates costs by ~18% of the STT line."""
    c = compute_costs(Segment.FUTURES, Side.SELL, 75, Decimal("24800"))
    taxable = c.brokerage + c.exchange + c.sebi + c.ipft
    assert abs(c.gst - taxable * Decimal("0.18")) <= Decimal("0.01")
    assert c.gst < c.stt  # if GST were applied to STT this would not hold


def test_futures_stt_uses_2026_hiked_rate():
    """Union Budget 2026 raised futures STT from 0.02% to 0.05%, sell side."""
    assert SCHEDULES[Segment.FUTURES].stt_pct == Decimal("0.05")
    c = compute_costs(Segment.FUTURES, Side.SELL, 75, Decimal("24800"))
    assert c.stt == Decimal("930.00")   # 0.05% of 1,860,000


def test_options_stt_uses_2026_hiked_rate():
    """Options premium STT went 0.10% -> 0.15% on 1 April 2026."""
    assert SCHEDULES[Segment.OPTIONS].stt_pct == Decimal("0.15")
    c = compute_costs(Segment.OPTIONS, Side.SELL, 75, Decimal("200"))
    assert c.stt == Decimal("22.50")    # 0.15% of 15,000


def test_option_costs_use_premium_not_notional():
    """Charging options on notional would overstate costs by ~100x."""
    premium = compute_costs(Segment.OPTIONS, Side.SELL, 75, Decimal("200"))
    assert premium.turnover == Decimal("15000.00")
    assert premium.total < Decimal("100")


def test_equity_delivery_stt_both_sides():
    buy = compute_costs(Segment.EQUITY_DELIVERY, Side.BUY, 100, Decimal("1000"))
    sell = compute_costs(Segment.EQUITY_DELIVERY, Side.SELL, 100, Decimal("1000"))
    assert buy.stt == Decimal("100.00")   # 0.1% of 100,000, both sides
    assert sell.stt == Decimal("100.00")


def test_delivery_sell_incurs_dp_charge():
    sell = compute_costs(Segment.EQUITY_DELIVERY, Side.SELL, 100, Decimal("1000"))
    buy = compute_costs(Segment.EQUITY_DELIVERY, Side.BUY, 100, Decimal("1000"))
    assert sell.dp == Decimal("20")
    assert buy.dp == Decimal("0")


def test_brokerage_takes_the_lower_of_flat_and_percent():
    small = compute_costs(Segment.EQUITY_INTRADAY, Side.BUY, 1, Decimal("100"))
    assert small.brokerage == Decimal("0.10")   # 0.1% of 100 beats the Rs 20 flat

    large = compute_costs(Segment.EQUITY_INTRADAY, Side.BUY, 1000, Decimal("3000"))
    assert large.brokerage == Decimal("20.00")  # flat wins


def test_options_brokerage_is_flat_not_percentage():
    big = compute_costs(Segment.OPTIONS, Side.BUY, 750, Decimal("500"))
    assert big.brokerage == Decimal("20.00")


def test_zero_turnover_costs_nothing():
    assert compute_costs(Segment.OPTIONS, Side.BUY, 0, Decimal("100")).total == Decimal("0")


def test_round_trip_sums_both_legs():
    rt = round_trip_cost(Segment.EQUITY_INTRADAY, 250, Decimal("2950"))
    buy = compute_costs(Segment.EQUITY_INTRADAY, Side.BUY, 250, Decimal("2950"))
    sell = compute_costs(Segment.EQUITY_INTRADAY, Side.SELL, 250, Decimal("2950"))
    assert rt.total == buy.total + sell.total


def test_breakeven_move_is_positive_and_sane():
    be = breakeven_move(Segment.FUTURES, 75, Decimal("24800"))
    assert Decimal("5") < be < Decimal("40")   # NIFTY futures: roughly 15 points


def test_segment_mapping():
    assert segment_for("index", True, "option") is Segment.OPTIONS
    assert segment_for("index", True, "future") is Segment.FUTURES
    assert segment_for("equity", True, "equity") is Segment.EQUITY_INTRADAY
    assert segment_for("equity", False, "equity") is Segment.EQUITY_DELIVERY
    assert segment_for("commodity", True, "option") is Segment.COMMODITY_OPTIONS


# ---------------------------------------------------------------------------
# THE GATE
# ---------------------------------------------------------------------------

def test_gate_costs_reconcile_to_the_rupee():
    """A strategy with zero gross edge must lose exactly its charges.

    Setup: a perfectly flat price series and zero slippage, so every round trip
    has gross P&L of exactly zero. The net loss must then equal the sum of the
    charge model's output, computed here independently of the engine.

    If these disagree, either the cost model or the P&L accounting is wrong, and
    every number in the metrics panel is unreliable.
    """
    price = 1000.0
    bars = 60
    ts = pd.date_range("2026-02-02 09:15", periods=bars, freq="5min", tz=IST)
    df = pd.DataFrame({
        "timestamp": ts,
        "open": [price] * bars, "high": [price] * bars,
        "low": [price] * bars, "close": [price] * bars,
        "volume": [1000] * bars,
    })

    # Enter immediately, exit on the next bar, repeat. No price movement at all.
    strategy = Strategy.model_validate({
        "name": "Cost Reconciliation",
        "instrument": {"symbol": "TEST", "segment": "equity"},
        "timeframe": "5m",
        "session": {"start": "09:15", "end": "15:10", "square_off": None},
        "entry": {"conditions": [{"left": "close", "cmp": ">", "right": 0}]},
        "exit": {"conditions": [{"left": "close", "cmp": ">", "right": 0}]},
        "legs": [{"id": "leg1", "action": "BUY", "kind": "equity", "lots": 1}],
        "risk": {"max_trades_per_day": 1000,
                 "re_entry": {"enabled": True, "max": 50, "cooldown_bars": 0}},
    })

    quantity = 10
    result = run_backtest(
        strategy, df, lot_size=quantity,
        fill_model=FillModel(slippage_bps=0.0, segment=Segment.EQUITY_INTRADAY),
    )

    trades = result.trades
    assert trades, "gate needs at least one round trip"

    # Every trade must have exactly zero gross P&L — the price never moved.
    for trade in trades:
        assert trade.gross_pnl == Decimal("0"), f"unexpected gross P&L {trade.gross_pnl}"

    # Independently recompute what the charges should have been.
    expected_fees = Decimal("0")
    for _ in trades:
        expected_fees += compute_costs(
            Segment.EQUITY_INTRADAY, Side.BUY, quantity, Decimal(str(price))
        ).total
        expected_fees += compute_costs(
            Segment.EQUITY_INTRADAY, Side.SELL, quantity, Decimal(str(price))
        ).total

    actual_fees = sum(t.fees for t in trades)
    assert actual_fees == expected_fees, (
        f"fees disagree: engine {actual_fees} vs hand-computed {expected_fees}"
    )

    net = sum(t.net_pnl for t in trades)
    assert net == -expected_fees, f"net P&L {net} != -{expected_fees}"

    # And the portfolio's own accounting must agree with the trade list.
    assert result.portfolio.fees_paid == expected_fees
    assert result.portfolio.realized_pnl == Decimal("0")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def make_trade(net: float, entry_day: int = 1, bars: int = 5) -> ClosedTrade:
    entry = datetime(2026, 2, entry_day, 9, 30, tzinfo=IST)
    return ClosedTrade(
        strategy_id="s", leg_id="l", symbol="X", direction="LONG", quantity=1,
        entry_time=entry, entry_price=Decimal("100"),
        exit_time=entry + timedelta(minutes=25), exit_price=Decimal("100") + Decimal(str(net)),
        gross_pnl=Decimal(str(net)), fees=Decimal("0"),
        reason=TradeReason.EXIT_SIGNAL, bars_held=bars,
    )


def make_curve(values: list[float]) -> list[tuple[datetime, Decimal]]:
    start = datetime(2026, 1, 1, 9, 15, tzinfo=IST)
    return [(start + timedelta(days=i), Decimal(str(v))) for i, v in enumerate(values)]


def test_total_return_and_cagr():
    curve = make_curve([100_000] + [110_000] * 364)
    m = M.compute_metrics(curve, [], Decimal("100000"), "1d")
    assert m.total_return_pct == pytest.approx(10.0, abs=0.01)
    assert m.cagr_pct == pytest.approx(10.0, abs=0.5)


def test_drawdown_depth_and_recovery():
    curve = make_curve([100, 120, 90, 95, 130])
    dd = M.compute_drawdown(pd.Series([v for _, v in [(t, float(x)) for t, x in curve]],
                                      index=[t for t, _ in curve]))
    assert dd.max_drawdown == pytest.approx(0.25)   # 120 -> 90
    assert dd.recovered


def test_drawdown_unrecovered():
    curve = make_curve([100, 150, 80])
    dd = M.compute_drawdown(pd.Series([100.0, 150.0, 80.0], index=[t for t, _ in curve]))
    assert dd.max_drawdown == pytest.approx(150 / 150 - 80 / 150)
    assert not dd.recovered


def test_win_rate_and_profit_factor():
    trades = [make_trade(100), make_trade(100), make_trade(-50), make_trade(-50)]
    m = M.compute_metrics(make_curve([100_000, 100_100]), trades, Decimal("100000"), "1d")
    assert m.win_rate_pct == pytest.approx(50.0)
    assert m.profit_factor == pytest.approx(2.0)   # 200 won / 100 lost
    assert m.expectancy == pytest.approx(25.0)


def test_profit_factor_with_no_losers_is_infinite_not_huge():
    """Reporting inf is honest; reporting 9999 implies a measured ratio."""
    trades = [make_trade(100), make_trade(50)]
    m = M.compute_metrics(make_curve([100_000, 100_150]), trades, Decimal("100000"), "1d")
    assert m.profit_factor == float("inf")


def test_consecutive_streaks():
    trades = [make_trade(x) for x in (10, 10, 10, -5, -5, 10)]
    m = M.compute_metrics(make_curve([100_000, 100_030]), trades, Decimal("100000"), "1d")
    assert m.max_consecutive_wins == 3
    assert m.max_consecutive_losses == 2


def test_cost_drag_is_reported():
    trades = [
        ClosedTrade("s", "l", "X", "LONG", 1,
                    datetime(2026, 2, 1, 9, 30, tzinfo=IST), Decimal("100"),
                    datetime(2026, 2, 1, 9, 55, tzinfo=IST), Decimal("110"),
                    Decimal("100"), Decimal("40"), TradeReason.EXIT_SIGNAL, 5)
    ]
    m = M.compute_metrics(make_curve([100_000, 100_060]), trades, Decimal("100000"), "1d")
    assert m.gross_pnl == 100.0
    assert m.total_fees == 40.0
    assert m.net_pnl == 60.0
    assert m.cost_drag_pct == pytest.approx(40.0)


def test_bars_per_year_scales_with_timeframe():
    assert M.bars_per_year("1d") == 252
    assert M.bars_per_year("5m") == 252 * 75
    assert M.bars_per_year("15m") == 252 * 25
    assert M.bars_per_year("1h") == 252 * 6


def test_sharpe_annualization_differs_by_timeframe():
    """Using the daily default on 5-minute data would inflate Sharpe ~9x."""
    curve = make_curve([100_000 * (1.001 ** i) for i in range(200)])
    daily = M.compute_metrics(curve, [], Decimal("100000"), "1d")
    intraday = M.compute_metrics(curve, [], Decimal("100000"), "5m")
    assert intraday.sharpe > daily.sharpe * 5


def test_metrics_on_empty_inputs():
    m = M.compute_metrics([], [], Decimal("100000"), "5m")
    assert m.total_trades == 0
    assert m.total_return_pct == 0.0


def test_split_is_chronological_not_random():
    curve = make_curve([100_000 + i * 10 for i in range(100)])
    trades = [make_trade(10, entry_day=(i % 28) + 1) for i in range(20)]
    split = M.split_metrics(curve, trades, Decimal("100000"), "1d", in_sample_fraction=0.7)

    assert split.in_sample.period_end <= split.out_of_sample.period_start
    assert split.split_date == curve[70][0]


def test_split_verdict_flags_overfit():
    """IS profitable, OOS not: the signature of a curve fit.

    Built explicitly rather than by index arithmetic so both windows are
    guaranteed enough trades for the verdict to be conclusive.
    """
    curve = make_curve(
        [100_000 + i * 100 for i in range(70)]        # in-sample: rising
        + [107_000 - i * 150 for i in range(30)]      # out-of-sample: falling
    )
    split_ts = curve[70][0]

    def trade_at(ts: datetime, net: float) -> ClosedTrade:
        return ClosedTrade(
            strategy_id="s", leg_id="l", symbol="X", direction="LONG", quantity=1,
            entry_time=ts - timedelta(hours=1), entry_price=Decimal("100"),
            exit_time=ts, exit_price=Decimal("100") + Decimal(str(net)),
            gross_pnl=Decimal(str(net)), fees=Decimal("0"),
            reason=TradeReason.EXIT_SIGNAL, bars_held=5,
        )

    trades = (
        [trade_at(curve[i][0], 100.0) for i in range(5, 65, 4)]      # 15 winners, IS
        + [trade_at(curve[i][0], -150.0) for i in range(72, 99, 2)]  # 14 losers, OOS
    )

    split = M.split_metrics(curve, trades, Decimal("100000"), "1d")

    assert split.split_date == split_ts
    assert split.in_sample.total_trades >= 10
    assert split.out_of_sample.total_trades >= 10
    assert split.in_sample.total_return_pct > 0
    assert split.out_of_sample.total_return_pct < 0
    assert "FAILED" in split.verdict


def test_split_verdict_recognises_a_strategy_that_holds_up():
    curve = make_curve([100_000 + i * 100 for i in range(100)])

    def trade_at(ts, net):
        return ClosedTrade(
            strategy_id="s", leg_id="l", symbol="X", direction="LONG", quantity=1,
            entry_time=ts - timedelta(hours=1), entry_price=Decimal("100"),
            exit_time=ts, exit_price=Decimal("110"),
            gross_pnl=Decimal(str(net)), fees=Decimal("0"),
            reason=TradeReason.EXIT_SIGNAL, bars_held=5,
        )

    trades = [trade_at(curve[i][0], 100.0) for i in range(5, 99, 3)]
    split = M.split_metrics(curve, trades, Decimal("100000"), "1d")
    assert "held up" in split.verdict


def test_trades_to_frame_shape():
    frame = M.trades_to_frame([make_trade(10), make_trade(-5)])
    assert len(frame) == 2
    assert "net_pnl" in frame.columns
    assert M.trades_to_frame([]).empty


# ---------------------------------------------------------------------------
# Option resolution
# ---------------------------------------------------------------------------

def test_atm_strike_rounds_to_step():
    assert opt.atm_strike(24_813, "NIFTY") == 24_800      # 50-point steps
    assert opt.atm_strike(54_060, "BANKNIFTY") == 54_100  # 100-point steps


def test_strike_rules():
    spot, und = 24_813.0, "NIFTY"
    assert opt.resolve_strike("ATM", spot, und, OptionRight.CE) == 24_800
    assert opt.resolve_strike("ATM+2", spot, und, OptionRight.CE) == 24_900
    assert opt.resolve_strike("ATM-2", spot, und, OptionRight.CE) == 24_700
    assert opt.resolve_strike("STRIKE:25000", spot, und, OptionRight.CE) == 25_000


def test_otm_is_direction_aware():
    """OTM must mean 'above spot' for a call and 'below spot' for a put.

    Getting this backwards silently converts a cheap OTM hedge into an expensive
    ITM position.
    """
    spot, und = 24_800.0, "NIFTY"
    call_otm = opt.resolve_strike("OTM+2", spot, und, OptionRight.CE)
    put_otm = opt.resolve_strike("OTM+2", spot, und, OptionRight.PE)
    assert call_otm > spot
    assert put_otm < spot


def test_itm_is_direction_aware():
    spot, und = 24_800.0, "NIFTY"
    assert opt.resolve_strike("ITM+2", spot, und, OptionRight.CE) < spot
    assert opt.resolve_strike("ITM+2", spot, und, OptionRight.PE) > spot


def test_delta_strike_rule():
    expiry = date(2026, 8, 25)
    strike = opt.resolve_strike("DELTA:0.30", 24_800.0, "NIFTY", OptionRight.CE, expiry, 0.13)
    assert strike > 24_800      # a 0.30-delta call is out of the money


def test_invalid_strike_rule_raises():
    with pytest.raises(ValueError):
        opt.resolve_strike("NEARBY", 24_800.0, "NIFTY", OptionRight.CE)


def test_weekly_expiry_resolution():
    monday = date(2026, 8, 10)
    expiry = opt.resolve_expiry("current_weekly", monday, "NIFTY")
    assert expiry >= monday
    assert (expiry - monday).days <= 7


def test_banknifty_weekly_falls_back_to_monthly():
    """NSE discontinued BANKNIFTY weeklies; we must not invent a contract."""
    as_of = date(2026, 8, 10)
    weekly = opt.resolve_expiry("current_weekly", as_of, "BANKNIFTY")
    monthly = opt.resolve_expiry("current_monthly", as_of, "BANKNIFTY")
    assert weekly == monthly


def test_expiry_rolls_off_holidays():
    from backend.data.calendar import is_trading_day

    for month in range(1, 13):
        expiry = opt.resolve_expiry("current_monthly", date(2026, month, 1), "NIFTY")
        assert is_trading_day(expiry), f"{expiry} is not a trading day"


def test_resolve_option_produces_label():
    resolved = opt.resolve_option(
        OptionSpec(right=OptionRight.CE, strike_rule="ATM+1", expiry="current_weekly"),
        spot=24_813.0, as_of=date(2026, 8, 10), underlying="NIFTY",
    )
    assert resolved.strike == 24_850
    assert resolved.synthetic
    assert "CE" in resolved.label


def test_synthetic_premium_series_shape():
    und = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 1, 31), "1d", seed=3)
    resolved = opt.ResolvedOption("NIFTY", OptionRight.CE, 24_800.0, date(2026, 2, 24))
    series = opt.synthetic_premium_series(und, resolved, volatility=0.13)

    assert len(series) == len(und)
    assert series.attrs["synthetic"] is True
    assert (series["close"] >= 0).all()
    assert (series["high"] >= series["low"]).all()


def test_realized_volatility_is_clamped():
    und = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 3, 31), "5m", seed=6)
    vol = opt.realized_volatility(und)
    assert 0.05 <= vol <= 1.5


# ---------------------------------------------------------------------------
# End to end with real costs
# ---------------------------------------------------------------------------

def test_real_costs_exceed_placeholder_on_futures():
    """The old flat Rs 20 understated futures costs badly. Prove the gap."""
    df = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 3, 31), "5m", seed=11)
    strategy = templates.load("ema_crossover", symbol="TEST")

    capital = Decimal("2000000")   # 1 NIFTY lot is ~Rs 18.6 lakh notional
    cheap = run_backtest(
        strategy, df, lot_size=75, starting_capital=capital, collect_events=False,
        fill_model=FillModel(slippage_bps=0, fee_per_order=Decimal("20")),
    )
    real = run_backtest(
        strategy, df, lot_size=75, starting_capital=capital, collect_events=False,
        fill_model=FillModel(slippage_bps=0, segment=Segment.FUTURES),
    )

    cheap_fees = sum(t.fees for t in cheap.trades)
    real_fees = sum(t.fees for t in real.trades)
    assert real_fees > cheap_fees * 5, (
        f"real futures costs {real_fees} vs flat-Rs20 {cheap_fees}"
    )


def test_metrics_end_to_end_on_real_backtest():
    df = generate_ohlcv("RELIANCE", date(2025, 1, 1), date(2026, 6, 30), "5m", seed=8)
    strategy = templates.load("ema_crossover", symbol="TEST")
    capital = Decimal("1000000")   # 250 RELIANCE shares is ~Rs 7.4 lakh notional
    result = run_backtest(strategy, df, lot_size=250, starting_capital=capital,
                          collect_events=False)

    m = M.compute_metrics(
        result.equity_curve, result.trades, capital, strategy.timeframe
    )
    assert m.total_trades == len(result.trades)
    assert m.bars == len(result.equity_curve)
    assert m.total_fees > 0
    assert m.max_drawdown_pct >= 0
    assert isinstance(m.summary(), str)


# ---------------------------------------------------------------------------
# Capital and leverage
# ---------------------------------------------------------------------------

def test_leverage_cap_blocks_oversized_positions():
    """Without a capital check the engine trades sizes the account cannot hold,
    runs equity negative, and produces impossible metrics (drawdown > 100%)."""
    df = generate_ohlcv("RELIANCE", date(2026, 1, 1), date(2026, 6, 30), "5m", seed=8)
    strategy = templates.load("ema_crossover", symbol="TEST")

    # 250 shares near Rs 2,950 is ~Rs 7.4 lakh of notional.
    starved = run_backtest(strategy, df, lot_size=250,
                           starting_capital=Decimal("50000"), collect_events=False)
    funded = run_backtest(strategy, df, lot_size=250,
                          starting_capital=Decimal("1000000"), collect_events=False)

    assert len(starved.trades) == 0, "undercapitalized account should take no trades"
    assert len(funded.trades) > 0


def test_metrics_flag_a_blown_account_instead_of_reporting_ratios():
    """A destroyed account must not report a positive Sharpe.

    Before this, a strategy that lost six times its capital showed Sharpe 1.19
    and a 578% drawdown, because percentage returns off a negative base are
    arithmetic noise that happens to look like performance.
    """
    curve = make_curve([100_000, 60_000, 20_000, -10_000, -40_000])
    trades = [make_trade(-35_000)]
    m = M.compute_metrics(curve, trades, Decimal("100000"), "1d")

    assert m.account_blown
    assert m.blown_at is not None
    assert m.max_drawdown_pct == 100.0          # never more than total loss
    assert m.cagr_pct == -100.0
    assert math.isnan(m.sharpe)                 # undefined, not flattering
    assert math.isnan(m.sortino)
    assert "ACCOUNT BLOWN" in m.summary()
    assert m.total_trades == 1                  # trade stats still valid


def test_surviving_account_reports_normal_ratios():
    curve = make_curve([100_000 + i * 50 for i in range(200)])
    m = M.compute_metrics(curve, [make_trade(50)], Decimal("100000"), "1d")
    assert not m.account_blown
    assert not math.isnan(m.sharpe)
    assert 0 <= m.max_drawdown_pct <= 100.0
