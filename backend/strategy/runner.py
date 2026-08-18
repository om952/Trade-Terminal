"""Backtest runner — wires the engine to historical bars and a fill simulator.

The loop order is the whole design:

    for each bar:
        1. fill orders queued by the PREVIOUS bar, at THIS bar's open
        2. evaluate this bar's close, producing new intents
        3. fill same-bar intents (stops, targets, square-off) inside this bar

Step 1 before step 2 is what enforces bar-close semantics. A signal generated at
bar i cannot possibly fill before bar i+1 opens, because the fill happens on the
next pass of the loop, using a price the engine had not seen when it decided.

Phase 4 replaces the flat slippage model here with a real Indian brokerage,
STT, exchange fee, GST and stamp-duty calculation, and adds the metrics layer.
The seam is `FillModel`, so nothing above it has to change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import pandas as pd

from backend.core.interfaces import Side
from backend.execution.costs import CostBreakdown, Segment, compute_costs, segment_for
from backend.indicators.base import PriceBar
from backend.strategy.option_book import MIN_TRADEABLE_PREMIUM, OptionBook
from backend.strategy.engine import (
    BarEvent,
    EngineResult,
    OrderIntent,
    StrategyEngine,
    bars_with_timestamps,
)
from backend.strategy.risk import RiskGuard
from backend.strategy.schema import Strategy
from backend.strategy.state import Portfolio, TradeReason

log = logging.getLogger(__name__)

ZERO = Decimal("0")


@dataclass
class FillModel:
    """Turns an intended price into a realistic one, and prices the order.

    Slippage always works against you — added when buying, subtracted when
    selling. Modelling it symmetrically around mid would make it cancel out
    across a round trip, which is precisely the error that makes high-frequency
    backtests look profitable.

    Fees route through the real Indian charge schedule: brokerage, STT, exchange
    transaction charges, SEBI turnover fee, IPFT, GST and stamp duty, per
    segment. `fee_per_order` remains as an override for tests that need exact
    hand-computable arithmetic.
    """

    slippage_bps: float = 5.0
    segment: Segment = Segment.EQUITY_INTRADAY
    fee_per_order: Decimal | None = None      # set to bypass the real model

    def fill_price(self, side: Side, price: Decimal) -> Decimal:
        slip = price * Decimal(str(self.slippage_bps)) / Decimal(10_000)
        return price + slip if side is Side.BUY else price - slip

    def fees(self, side: Side, quantity: int, price: Decimal) -> Decimal:
        if self.fee_per_order is not None:
            return self.fee_per_order
        return compute_costs(self.segment, side, quantity, price).total

    def cost_breakdown(self, side: Side, quantity: int, price: Decimal) -> CostBreakdown:
        return compute_costs(self.segment, side, quantity, price)


def run_backtest(
    strategy: Strategy,
    df: pd.DataFrame,
    starting_capital: Decimal = Decimal("100000"),
    lot_size: int = 1,
    fill_model: FillModel | None = None,
    risk: RiskGuard | None = None,
    collect_events: bool = True,
    iv_premium: float = 0.0,
) -> EngineResult:
    """Run a strategy over historical bars.

    `collect_events` keeps a per-bar record for the UI and for debugging. Turn it
    off for large sweeps where only the trades and equity curve matter.
    """
    if df.empty:
        raise ValueError("Cannot backtest an empty DataFrame")

    for warning in preflight(strategy, df):
        log.warning("PREFLIGHT %s: %s", strategy.name, warning)

    fills = fill_model or FillModel(
        slippage_bps=strategy.costs.slippage_bps,
        segment=segment_for(
            strategy.instrument.segment,
            strategy.is_intraday,
            strategy.legs[0].kind.value,
        ),
    )
    portfolio = Portfolio(starting_capital=starting_capital)
    engine = StrategyEngine(
        strategy,
        portfolio=portfolio,
        risk=risk or RiskGuard(rules=strategy.risk),
        lot_size=lot_size,
    )

    indicators = engine.prepare_backtest(df)
    context_frame = _session_context(strategy, df)
    if context_frame is not None:
        indicators.attach_context(context_frame)
    result = EngineResult(
        strategy_id=strategy.id,
        strategy_hash=strategy.content_hash(),
        portfolio=portfolio,
    )

    # Option legs trade a contract, not the index the signal is read from.
    book = (
        OptionBook(strategy, df, iv_premium=iv_premium)
        if _has_option_legs(strategy) else None
    )
    result.option_book = book

    pending: list[OrderIntent] = []
    last_ts: datetime | None = None
    last_close = ZERO

    for index, ts, bar in bars_with_timestamps(df):
        indicators.seek(index)
        last_ts, last_close = ts, Decimal(str(bar.close))

        # --- 1. fill what the previous bar decided, at this bar's open --------
        for intent in pending:
            filled, reference = _prepare_fill(
                book, intent, index, Decimal(str(bar.open)), opening=True
            )
            _execute(engine, filled, reference, fills, ts, index, indicators, book)
        pending = []

        # --- 2. evaluate this bar --------------------------------------------
        # Positions are managed in the premium the leg actually holds; signals
        # still come off the underlying.
        exec_bars = _exec_bars(book, portfolio, index)
        event = engine.on_bar(index, ts, bar, indicators, exec_bars=exec_bars)

        # --- 3. resting orders execute inside this bar ------------------------
        deferred: list[OrderIntent] = []
        for intent in event.intents:
            if intent.same_bar and intent.limit_price is not None:
                _execute(engine, intent, intent.limit_price, fills, ts, index, indicators, book)
            else:
                deferred.append(intent)
        pending = deferred

        if collect_events:
            result.events.append(event)

        result.equity_curve.append(
            (ts, portfolio.equity(_mark_prices(strategy, book, portfolio, index, bar)))
        )

    # --- flatten anything still open at the end of the data ------------------
    # Leaving a position open would let an unrealized loss hide from the metrics.
    if portfolio.has_open_positions and last_ts is not None:
        final = indicators.length - 1
        for position in list(portfolio.open_positions):
            intent = OrderIntent(
                leg_id=position.leg_id,
                symbol=position.symbol,
                side=Side.SELL if position.is_long else Side.BUY,
                quantity=abs(position.quantity),
                reason=TradeReason.SESSION_END,
                bar_index=final,
                timestamp=last_ts,
            )
            filled, reference = _prepare_fill(book, intent, final, last_close)
            _execute(engine, filled, reference, fills, last_ts, final, indicators, book)

    log.info(
        "%s: %d trades, net P&L %.2f over %d bars",
        strategy.name, len(portfolio.closed_trades),
        float(portfolio.realized_pnl - portfolio.fees_paid), len(df),
    )
    return result


def preflight(strategy: Strategy, df: pd.DataFrame) -> list[str]:
    """Checks that need the data, not just the document.

    The schema validator catches a strategy that is malformed. This catches one
    that is well-formed but cannot possibly trade against *this* data — which
    produces zero trades and looks exactly like a strategy that simply found no
    setups. Being told why beats staring at an empty trade log.
    """
    warnings: list[str] = []

    referenced = set(strategy.entry.referenced_ids())
    if strategy.exit:
        referenced |= strategy.exit.referenced_ids()

    uses_volume = "volume" in referenced or any(
        registry_needs_volume(i.type) for i in strategy.indicators
    )
    if uses_volume and float(df["volume"].sum()) == 0.0:
        warnings.append(
            "strategy gates on volume but this instrument reports none "
            "(indices are calculated, not traded, so their volume is always 0). "
            "Any volume condition will always be False and the strategy cannot "
            "fire. Use a future or an equity instead."
        )

    from backend.data.upstox_client import Timeframe

    tf = Timeframe.parse(strategy.timeframe)
    if len(df) < 100:
        warnings.append(f"only {len(df)} bars supplied; results will not be meaningful")

    if "timestamp" in df.columns and len(df) > 1:
        gap = pd.to_datetime(df["timestamp"]).diff().median()
        expected = pd.Timedelta(minutes=tf.interval) if tf.unit == "minutes" else None
        if expected is not None and gap is not None and gap < expected * 0.5:
            warnings.append(
                f"data looks finer than the strategy timeframe "
                f"(median bar gap {gap}, strategy says {strategy.timeframe})"
            )

    return warnings


def registry_needs_volume(indicator_type: str) -> bool:
    from backend.indicators import registry

    try:
        return registry.get(indicator_type).needs_volume
    except KeyError:
        return False


def _session_context(strategy: Strategy, df: pd.DataFrame) -> pd.DataFrame | None:
    """Precompute session-scoped values a strategy may reference.

    Pivots, prior-day levels and session VWAP are not rolling indicators — they
    are derived from the previous completed session and held constant through the
    current one. They live here rather than in the indicator registry because
    they need a calendar, and an indicator that needs a calendar is a different
    kind of thing.

    Returns None when the strategy references none of them, so the common case
    costs nothing.
    """
    from backend.indicators import pivots as piv
    from backend.indicators.volume import vwap
    from backend.strategy.schema import CONTEXT_FIELDS

    referenced = set(strategy.entry.referenced_ids())
    if strategy.exit:
        referenced |= strategy.exit.referenced_ids()
    if not (referenced & CONTEXT_FIELDS):
        return None

    if "timestamp" not in df.columns:
        raise ValueError(
            "This strategy uses session values (pivots/VWAP) which need a "
            "'timestamp' column in the candle data."
        )

    levels = piv.pivots(df, method="cpr")
    prev_day = piv.previous_period_levels(df, "day")
    session_vwap = vwap(df, anchor="session")

    frame = pd.concat([levels, prev_day], axis=1)
    frame["vwap"] = session_vwap
    frame = frame.rename(
        columns={
            "prev_day_high": "prev_day_high",
            "prev_day_low": "prev_day_low",
            "prev_day_close": "prev_day_close",
        }
    )

    keep = [c for c in frame.columns if c in CONTEXT_FIELDS]
    # NaN is preserved deliberately: the first session has no prior session, so
    # its levels genuinely do not exist. Conditions against NaN evaluate False.
    return frame[keep].reset_index(drop=True)


def _execute(
    engine: StrategyEngine,
    intent: OrderIntent,
    reference_price: Decimal,
    fills: FillModel,
    ts: datetime,
    bar_index: int,
    indicators,
    book: OptionBook | None = None,
) -> None:
    price = fills.fill_price(intent.side, reference_price)
    fee = fills.fees(intent.side, intent.quantity, price)
    trade = engine.apply_fill(intent, price, fee, ts, bar_index, indicators)

    # A closed option position releases its contract, so the next entry resolves
    # a fresh strike against wherever the market has moved to. Holding the old
    # one would keep trading a strike chosen for a spot that no longer exists.
    if book is not None and trade is not None:
        book.release(intent.leg_id)


def _has_option_legs(strategy: Strategy) -> bool:
    from backend.strategy.schema import LegKind

    return any(leg.kind is LegKind.OPTION for leg in strategy.legs)


def _leg_by_id(strategy: Strategy, leg_id: str):
    return next((leg for leg in strategy.legs if leg.id == leg_id), None)


def _prepare_fill(
    book: OptionBook | None,
    intent: OrderIntent,
    index: int,
    underlying_price: Decimal,
    opening: bool = False,
) -> tuple[OrderIntent, Decimal]:
    """Resolve what an intent actually trades, and at what price.

    For an option leg this returns the intent rewritten to name the concrete
    contract, plus that contract's premium. The contract is resolved here — at
    fill time, not signal time — because the fill lands on the next bar and the
    strike has to be chosen from the spot that existed when the order went in.

    Naming the contract on the intent matters beyond cosmetics: `position.symbol`
    is what the equity curve marks against, so leaving it as the index would
    value a 300-rupee position at 55,000 and turn the equity curve into a picture
    of BANKNIFTY rather than of the strategy.
    """
    if book is None:
        return intent, underlying_price

    from dataclasses import replace

    from backend.strategy.schema import LegKind

    leg = _leg_by_id(book.strategy, intent.leg_id)
    if leg is None or leg.kind is not LegKind.OPTION:
        return intent, underlying_price

    option = book.active(intent.leg_id)
    if option is None:
        if not opening:
            # Closing something with no active contract should be impossible;
            # falling back to spot would book a 55,000-point exit against a
            # 300-point position, so refuse rather than produce a plausible
            # wrong number.
            raise RuntimeError(
                f"Leg {intent.leg_id} has no resolved contract to price "
                f"{intent.reason.value} against at bar {index}"
            )
        option = book.open(leg, index)

    bar = book.price_bar(option, index)
    price = bar.open if opening else bar.close
    return (
        replace(intent, symbol=option.label),
        Decimal(str(max(price, MIN_TRADEABLE_PREMIUM))),
    )


def _exec_bars(
    book: OptionBook | None, portfolio: Portfolio, index: int
) -> dict[str, PriceBar] | None:
    """Per-leg bars for position management: the contract's own OHLC."""
    if book is None:
        return None
    out: dict[str, PriceBar] = {}
    for position in portfolio.open_positions:
        bar = book.bar(position.leg_id, index)
        if bar is not None:
            out[position.leg_id] = bar
    return out or None


def _mark_prices(
    strategy: Strategy,
    book: OptionBook | None,
    portfolio: Portfolio,
    index: int,
    bar: PriceBar,
) -> dict[str, Decimal]:
    """Prices for marking the equity curve.

    An open option leg has to be marked at its premium. Marking it at the index
    would value a 300-rupee position at 55,000 and make the equity curve a
    picture of the underlying rather than of the strategy.
    """
    prices = {strategy.instrument.symbol: Decimal(str(bar.close))}
    if book is None:
        return prices
    for position in portfolio.open_positions:
        option_bar = book.bar(position.leg_id, index)
        if option_bar is not None:
            prices[position.symbol] = Decimal(str(option_bar.close))
    return prices
