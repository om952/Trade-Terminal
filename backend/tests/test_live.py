"""Phase 6 gate: paper trading is the backtest with a different input.

The gate is `test_gate_paper_session_matches_backtest`. The same strategy over
the same bars, once through `run_backtest` and once through the full live path —
streaming indicators, simulated broker, journal and all — must produce identical
trades, to the paisa.

That test is the only thing standing between "paper results predict live results"
and "paper results are a second, unvalidated implementation that happens to look
similar." Everything else here defends a specific failure mode that a live
session has and a backtest does not: a feed that drops, a process that dies
holding a position, a clock that keeps moving when data stops.
"""

from __future__ import annotations

import json
import warnings
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pandas as pd
import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

from backend.core.config import IST, Mode
from backend.core.interfaces import Order, OrderType, Side
from backend.data.simulator import generate_ohlcv
from backend.data.upstox_client import Timeframe
from backend.execution.costs import Segment
from backend.live import journal as J
from backend.live.aggregator import BarAggregator, Tick, bucket_start
from backend.live.broker import SimulatedBroker
from backend.live.feed import LiveFeed
from backend.live.session import PaperSession, SessionConfig, make_session_id
from backend.live.sources import ReplaySource
from backend.strategy import templates
from backend.strategy.risk import RiskGuard
from backend.strategy.runner import FillModel, run_backtest
from backend.strategy.schema import Strategy

SYMBOL = "SIM|LIVETEST"


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    """Deterministic 5-minute bars covering several sessions."""
    return generate_ohlcv("RELIANCE", date(2026, 1, 1), date(2026, 3, 31), "5m", seed=909)


@pytest.fixture(scope="module")
def daily_frame() -> pd.DataFrame:
    return generate_ohlcv("RELIANCE", date(2024, 1, 1), date(2026, 6, 30), "1d", seed=707)


def _strategy(key: str = "ema_crossover", timeframe: str = "5m") -> Strategy:
    return templates.load(key, symbol=SYMBOL, timeframe=timeframe)


def _tick(minute: str, ltp: float, cum: int | None = None, day: str = "2026-01-05") -> Tick:
    return Tick(
        symbol=SYMBOL,
        timestamp=datetime.fromisoformat(f"{day}T{minute}").replace(tzinfo=IST),
        ltp=ltp,
        cum_volume=cum,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

class TestAggregator:
    def test_buckets_align_to_session_open_not_the_clock(self):
        """Upstox's 5m candles start at 09:15. Ours must land on the same grid."""
        tf = Timeframe.parse("5m")
        cases = {
            "09:15:00": "09:15", "09:17:30": "09:15", "09:19:59": "09:15",
            "09:20:00": "09:20", "10:03:00": "10:00", "11:47:12": "11:45",
        }
        for raw, expected in cases.items():
            ts = datetime.fromisoformat(f"2026-01-05T{raw}").replace(tzinfo=IST)
            got = bucket_start(ts, tf)
            assert got is not None and got.strftime("%H:%M") == expected, raw

    def test_fifteen_minute_grid_starts_at_0915(self):
        tf = Timeframe.parse("15m")
        ts = datetime.fromisoformat("2026-01-05T09:44:00").replace(tzinfo=IST)
        assert bucket_start(ts, tf).strftime("%H:%M") == "09:30"

    def test_pre_open_ticks_are_rejected(self):
        """The pre-open auction is a different mechanism, not part of the 09:15 bar."""
        tf = Timeframe.parse("5m")
        ts = datetime.fromisoformat("2026-01-05T09:07:00").replace(tzinfo=IST)
        assert bucket_start(ts, tf) is None

    def test_closing_ticks_clamp_into_the_final_bar(self):
        """A print at 15:30 belongs in the closing candle, not a stub that never closes."""
        tf = Timeframe.parse("5m")
        ts = datetime.fromisoformat("2026-01-05T15:30:00").replace(tzinfo=IST)
        assert bucket_start(ts, tf).strftime("%H:%M") == "15:25"

    def test_ohlc_is_built_from_the_ticks_in_the_window(self):
        agg = BarAggregator(SYMBOL, "5m")
        for minute, price in [("09:15:01", 100.0), ("09:16:00", 103.0),
                              ("09:17:00", 98.0), ("09:19:00", 101.0)]:
            assert agg.on_tick(_tick(minute, price)) == []

        closed = agg.on_tick(_tick("09:20:01", 105.0))
        assert len(closed) == 1
        bar = closed[0].bar
        assert (bar.open, bar.high, bar.low, bar.close) == (
            Decimal("100.0"), Decimal("103.0"), Decimal("98.0"), Decimal("101.0")
        )
        assert bar.timestamp.strftime("%H:%M") == "09:15"

    def test_volume_is_a_difference_of_cumulative_not_a_sum(self):
        """Ticks carry the day's running total. Summing it inflates every bar."""
        agg = BarAggregator(SYMBOL, "5m")
        agg.on_tick(_tick("09:15:01", 100.0, cum=500))
        agg.on_tick(_tick("09:18:00", 101.0, cum=1200))
        first = agg.on_tick(_tick("09:20:01", 102.0, cum=1500))[0]
        assert first.bar.volume == 1200          # not 500 + 1200

        agg.on_tick(_tick("09:24:00", 103.0, cum=1900))
        second = agg.on_tick(_tick("09:25:01", 104.0, cum=2000))[0]
        assert second.bar.volume == 700          # 1900 - 1200

    def test_clock_closes_a_bar_when_no_tick_arrives(self):
        """A thin instrument must not hold its 15:20 bar open past square-off."""
        agg = BarAggregator(SYMBOL, "5m")
        agg.on_tick(_tick("09:15:30", 100.0))

        early = datetime.fromisoformat("2026-01-05T09:19:00").replace(tzinfo=IST)
        assert agg.flush_due(early) == []

        due = datetime.fromisoformat("2026-01-05T09:20:05").replace(tzinfo=IST)
        closed = agg.flush_due(due)
        assert len(closed) >= 1
        assert closed[0].bar.timestamp.strftime("%H:%M") == "09:15"

    def test_empty_windows_are_filled_and_flagged(self):
        agg = BarAggregator(SYMBOL, "5m")
        agg.on_tick(_tick("09:15:30", 100.0))
        closed = agg.on_tick(_tick("09:31:00", 108.0))

        assert closed[0].gap_filled is False
        gaps = [c for c in closed if c.gap_filled]
        # 09:20 and 09:25 are empty; 09:30 is the bar the 09:31 tick opened.
        assert [g.bar.timestamp.strftime("%H:%M") for g in gaps] == ["09:20", "09:25"]
        for gap in gaps:
            bar = gap.bar
            assert bar.open == bar.high == bar.low == bar.close == Decimal("100.0")
            assert bar.volume == 0
        assert agg.gap_filled_count == 2

    def test_out_of_order_ticks_are_dropped_not_folded_in(self):
        agg = BarAggregator(SYMBOL, "5m")
        agg.on_tick(_tick("09:22:00", 100.0))
        assert agg.on_tick(_tick("09:16:00", 999.0)) == []
        assert agg.rejected_ticks == 1

        closed = agg.on_tick(_tick("09:25:01", 101.0))
        assert closed[0].bar.high == Decimal("100.0")   # 999 never landed

    def test_new_day_resets_cumulative_volume(self):
        """The exchange restarts the day counter at zero; carrying yesterday's
        total forward would make the first bar of every day hugely negative."""
        agg = BarAggregator(SYMBOL, "5m")
        agg.on_tick(_tick("15:20:00", 100.0, cum=90_000))

        agg.on_tick(_tick("09:15:30", 101.0, cum=400, day="2026-01-06"))
        first = agg.on_tick(_tick("09:21:00", 102.0, cum=900, day="2026-01-06"))
        assert first[-1].bar.timestamp.strftime("%H:%M") == "09:15"
        assert first[-1].bar.volume == 400              # not 400 - 90000

        second = agg.on_tick(_tick("09:26:00", 103.0, cum=1500, day="2026-01-06"))
        assert second[-1].bar.volume == 500             # 900 - 400


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

class TestJournal:
    def test_round_trip_preserves_decimals_exactly(self, tmp_path):
        with J.Journal("t1", tmp_path) as journal:
            journal.append(J.Event.FILL, {"price": Decimal("2951.4567"), "qty": 5})

        records = list(J.read(tmp_path / "t1.jsonl"))
        assert len(records) == 1
        # Money survives as a string; a float round trip would not be exact.
        assert records[0].data["price"] == "2951.4567"
        assert Decimal(records[0].data["price"]) == Decimal("2951.4567")

    def test_a_torn_final_line_ends_the_read_without_losing_the_prefix(self, tmp_path):
        """The signature of a crash mid-write. Everything before it is valid."""
        path = tmp_path / "t2.jsonl"
        with J.Journal("t2", tmp_path) as journal:
            for i in range(5):
                journal.append(J.Event.BAR, {"index": i})

        with path.open("a") as fh:
            fh.write('{"seq": 6, "ts": "2026-01-05T09:15:00+05:30", "type": "ba')

        records = list(J.read(path))
        assert len(records) == 5
        assert [r.data["index"] for r in records] == [0, 1, 2, 3, 4]

    def test_sequence_continues_when_a_session_is_reopened(self, tmp_path):
        with J.Journal("t3", tmp_path) as first:
            first.append(J.Event.BAR, {"index": 0})
            first.append(J.Event.BAR, {"index": 1})
        with J.Journal("t3", tmp_path) as second:
            record = second.append(J.Event.BAR, {"index": 2})
        assert record.seq == 3

    def test_recover_rebuilds_position_and_portfolio_state(self, tmp_path):
        with J.Journal("t4", tmp_path) as journal:
            journal.append(J.Event.SESSION_START, {
                "strategy": {"name": "x"}, "strategy_hash": "abc",
                "symbol": SYMBOL, "timeframe": "5m", "starting_capital": "100000",
                "lot_size": 1,
            })
            journal.append(J.Event.BAR, {"index": 0, "t": "2026-01-05T09:15:00+05:30"})
            journal.append(J.Event.FILL, {
                "client_tag": "tag-1", "leg_id": "leg1",
                "position": {"symbol": SYMBOL, "quantity": 10, "average_price": "100"},
                "portfolio": {"realized_pnl": "0", "fees_paid": "12.5"},
                "risk": {"entries_today": 1, "last_exit_bar": None,
                         "halted": False, "halt_reason": "none"},
            })

        state = J.recover("t4", tmp_path)
        assert state.started and not state.ended
        assert state.symbol == SYMBOL
        assert state.open_legs == ["leg1"]
        assert state.filled_tags == {"tag-1"}
        assert state.portfolio["fees_paid"] == "12.5"
        assert state.risk["entries_today"] == 1

    def test_recover_on_a_missing_journal_is_empty_not_an_error(self, tmp_path):
        state = J.recover("never-existed", tmp_path)
        assert not state.started and state.open_legs == []


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------

class TestSimulatedBroker:
    def _order(self, tag: str, side: Side = Side.BUY, qty: int = 10, price: str = "100") -> Order:
        return Order(
            client_tag=tag, symbol=SYMBOL, side=side, quantity=qty,
            order_type=OrderType.MARKET, price=Decimal(price),
        )

    def test_slippage_always_works_against_you(self):
        broker = SimulatedBroker(FillModel(slippage_bps=10, segment=Segment.FUTURES))
        buy = broker.place(self._order("b", Side.BUY))
        sell = broker.place(self._order("s", Side.SELL))
        assert buy.price > Decimal("100")
        assert sell.price < Decimal("100")

    def test_a_repeated_client_tag_never_fills_twice(self):
        """The property crash recovery depends on."""
        broker = SimulatedBroker(FillModel(slippage_bps=0, segment=Segment.FUTURES))
        first = broker.place(self._order("same-tag"))
        second = broker.place(self._order("same-tag"))

        assert second is first
        assert len(broker.fills) == 1
        assert broker.positions()[0].quantity == 10        # not 20

    def test_restore_makes_replayed_orders_no_ops(self):
        broker = SimulatedBroker(FillModel(slippage_bps=0, segment=Segment.FUTURES))
        broker.restore({"already-done"})
        assert broker.place(self._order("already-done")) is None
        assert broker.positions() == []

    def test_reconcile_catches_a_ledger_that_disagrees_with_the_engine(self):
        from backend.strategy.state import Portfolio

        broker = SimulatedBroker(FillModel(slippage_bps=0, segment=Segment.FUTURES))
        broker.place(self._order("x", Side.BUY, qty=10))

        portfolio = Portfolio()
        position = portfolio.position("leg1", SYMBOL)
        position.apply_fill(Side.BUY, 10, Decimal("100"))
        assert broker.reconcile_against(portfolio).agrees

        position.quantity = 7                    # simulate an accounting drift
        report = broker.reconcile_against(portfolio)
        assert not report.agrees
        assert "engine +7 vs broker +10" in report.differences[0]


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _paper(
    strategy: Strategy,
    df: pd.DataFrame,
    tmp_path,
    *,
    warmup: pd.DataFrame | None = None,
    history_frame: pd.DataFrame | None = None,
    history_before=None,
    session_id: str = "gate",
    capital: Decimal = Decimal("1000000"),
    lot_size: int = 250,
) -> PaperSession:
    """A session wired to replay `df`.

    `warmup` pre-slices the priming history. `history_frame` + `history_before`
    is the production wiring instead — the feed gets the whole frame and the
    window boundary, so a resume can warm up past that boundary.
    """
    config = SessionConfig(
        strategy=strategy,
        instrument_key=SYMBOL,
        starting_capital=capital,
        lot_size=lot_size,
        slippage_bps=5.0,
        max_leverage=Decimal("5"),
        mode=Mode.PAPER,
        alerts_enabled=False,
    )
    feed = LiveFeed(
        SYMBOL, strategy.timeframe,
        ReplaySource(df, SYMBOL),
        history_frame=(
            history_frame if history_frame is not None
            else (warmup if warmup is not None else df.iloc[:0])
        ),
        history_before=history_before,
    )
    broker = SimulatedBroker(
        FillModel(slippage_bps=5.0, segment=_segment(strategy)),
    )
    risk = RiskGuard(rules=strategy.risk, max_leverage=Decimal("5"))
    return PaperSession(config, feed, broker, J.Journal(session_id, tmp_path), risk=risk)


def _segment(strategy: Strategy) -> Segment:
    from backend.execution.costs import segment_for

    return segment_for(
        strategy.instrument.segment, strategy.is_intraday, strategy.legs[0].kind.value
    )


def _backtest(strategy: Strategy, df: pd.DataFrame, capital=Decimal("1000000"), lot_size=250):
    return run_backtest(
        strategy, df,
        starting_capital=capital,
        lot_size=lot_size,
        fill_model=FillModel(slippage_bps=5.0, segment=_segment(strategy)),
        risk=RiskGuard(rules=strategy.risk, max_leverage=Decimal("5")),
        collect_events=False,
    )


# ---------------------------------------------------------------------------
# THE GATE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["ema_crossover", "rsi_mean_reversion", "bollinger_breakout"])
def test_gate_paper_session_matches_backtest(frame, tmp_path, key):
    """The live path and the backtest path must produce identical trades.

    Different indicator engine (streaming vs vectorized), different order path
    (through a broker vs direct), different bar delivery (a feed vs a DataFrame) —
    and the same answer. Any divergence here means a backtest result is a claim
    about code that does not run in paper or live.
    """
    strategy = _strategy(key)

    expected = _backtest(strategy, frame)
    session = _paper(strategy, frame, tmp_path, session_id=f"gate-{key}")
    session.run()

    live_trades = session.portfolio.closed_trades
    assert len(live_trades) == len(expected.trades), (
        f"{len(live_trades)} live trades vs {len(expected.trades)} backtest trades"
    )
    assert live_trades, "strategy took no trades — the gate would pass vacuously"

    for live, back in zip(live_trades, expected.trades):
        # Decisions must be bit-identical: same bar, same side, same reason.
        # There is no tolerance for these — a different bar is a different trade.
        assert live.entry_time == back.entry_time
        assert live.exit_time == back.exit_time
        assert live.direction == back.direction
        assert live.quantity == back.quantity
        assert live.reason is back.reason
        assert live.bars_held == back.bars_held

        # Prices derived from indicators (an ATR stop or target) inherit the
        # streaming-vs-vectorized difference that Phase 2 pins at 1e-9. Demanding
        # bit-equality here would be asserting something the indicator contract
        # never promised. What matters is that the difference is far inside one
        # tick (NSE trades in 5-paisa steps), so it can never change a fill.
        assert abs(live.entry_price - back.entry_price) < Decimal("1e-6")
        assert abs(live.exit_price - back.exit_price) < Decimal("1e-6")
        assert abs(live.net_pnl - back.net_pnl) < Decimal("0.01")
        assert live.fees == back.fees          # rounded to paisa, so exact

    assert abs(session.portfolio.realized_pnl - expected.portfolio.realized_pnl) < Decimal("0.01")
    assert session.portfolio.fees_paid == expected.portfolio.fees_paid


def test_gate_daily_timeframe_paper_matches_backtest(daily_frame, tmp_path):
    """Daily bars have no intraday session, and the live path must agree there too."""
    strategy = _strategy("ema_crossover", timeframe="1d")

    expected = _backtest(strategy, daily_frame)
    session = _paper(strategy, daily_frame, tmp_path, session_id="gate-daily")
    session.run()

    assert len(session.portfolio.closed_trades) == len(expected.trades)
    assert abs(session.portfolio.realized_pnl - expected.portfolio.realized_pnl) < Decimal("0.01")


def test_gate_crash_midway_recovers_to_the_uninterrupted_result(frame, tmp_path):
    """Kill the process mid-session; the resumed run must end where one that never
    crashed would have.

    This is the test that matters most, because the failure it guards against is
    invisible: a resumed session that quietly forgets an open position, a pending
    entry, or a re-entry cooldown still produces a plausible-looking trade log.
    """
    strategy = _strategy("ema_crossover")

    reference = _backtest(strategy, frame)
    assert len(reference.trades) >= 3, "need several trades for this to prove anything"

    split = len(frame) // 2
    first, second = frame.iloc[:split], frame.iloc[split:]

    # --- run, then "crash": stop without flattening or ending the session ----
    crashed = _paper(strategy, first, tmp_path, session_id="crash")
    crashed.config.flatten_at_session_end = False
    crashed.run()
    before = len(crashed.portfolio.closed_trades)
    open_before = len(crashed.portfolio.open_positions)

    # The journal must not be marked ended, or recovery would decline to resume.
    records = list(J.read(tmp_path / "crash.jsonl"))
    ends = [r for r in records if r.type is J.Event.SESSION_END]
    for record in ends:                      # simulate the process dying instead
        records.remove(record)
    (tmp_path / "crash.jsonl").write_text(
        "".join(
            json.dumps({"seq": r.seq, "ts": r.ts.isoformat(),
                        "type": r.type.value, "data": r.data}) + "\n"
            for r in records
        )
    )

    # --- resume: same session id, warmup replays the bars already seen -------
    resumed = _paper(strategy, second, tmp_path, warmup=first, session_id="crash")
    resumed.run()

    assert resumed.live.resumed, "session did not detect the journal and resume"
    assert len(resumed.portfolio.closed_trades) == len(reference.trades), (
        f"recovered {len(resumed.portfolio.closed_trades)} trades "
        f"({before} before the crash, {open_before} open) "
        f"vs {len(reference.trades)} uninterrupted"
    )
    assert abs(resumed.portfolio.realized_pnl - reference.portfolio.realized_pnl) < Decimal("0.01")
    assert resumed.portfolio.fees_paid == reference.portfolio.fees_paid

    for got, want in zip(resumed.portfolio.closed_trades, reference.trades):
        assert got.entry_time == want.entry_time
        assert got.reason is want.reason
        assert abs(got.exit_price - want.exit_price) < Decimal("1e-6")
        assert abs(got.net_pnl - want.net_pnl) < Decimal("0.01")


def test_gate_resume_warms_up_through_the_crash_point(frame, tmp_path):
    """A resumed session must be primed on every bar the crashed one processed.

    Recovery skips bars the journal already recorded — correctly, or they would be
    traded twice. But skipping them for the *feed* must not also hide them from
    the *indicators*: an EMA resumed from the window's start, blind to the 375
    bars the first run consumed, carries on from the right position with the
    wrong state. That produced a session that silently disagreed with an
    uninterrupted one by a whole trade, with nothing in the log to point at.

    This exercises the production wiring — whole frame plus window boundary —
    rather than a pre-sliced warm-up, because the pre-sliced form cannot
    reproduce the bug.
    """
    strategy = _strategy("ema_crossover")
    split_at, crash_at = 600, 1100
    boundary = pd.Timestamp(frame["timestamp"].iloc[split_at]).tz_convert(IST).to_pydatetime()
    window = frame.iloc[split_at:].reset_index(drop=True)

    reference = _paper(
        strategy, window, tmp_path,
        history_frame=frame, history_before=boundary, session_id="resume-ref",
    )
    reference.run()

    crashed = _paper(
        strategy, window.iloc[: crash_at - split_at], tmp_path,
        history_frame=frame, history_before=boundary, session_id="resume",
    )
    crashed.config.flatten_at_session_end = False
    crashed.run()
    _strip_session_end(tmp_path / "resume.jsonl")

    resumed = _paper(
        strategy, window, tmp_path,
        history_frame=frame, history_before=boundary, session_id="resume",
    )
    resumed.run()

    assert resumed.live.resumed
    assert len(resumed.portfolio.closed_trades) == len(reference.portfolio.closed_trades), (
        f"resumed {len(resumed.portfolio.closed_trades)} trades vs "
        f"{len(reference.portfolio.closed_trades)} uninterrupted"
    )
    assert abs(
        resumed.portfolio.realized_pnl - reference.portfolio.realized_pnl
    ) < Decimal("0.01")

    for got, want in zip(resumed.portfolio.closed_trades, reference.portfolio.closed_trades):
        assert got.entry_time == want.entry_time
        assert got.exit_time == want.exit_time
        assert got.reason is want.reason
        assert abs(got.net_pnl - want.net_pnl) < Decimal("0.01")

    tags = [f.client_tag for f in resumed.broker.fills]
    assert len(tags) == len(set(tags))


def test_gate_a_restart_does_not_reset_the_daily_risk_counters(frame, tmp_path):
    """Restarting must never be a way to buy more trades.

    `start_new_day` zeroes the daily P&L, the trade counter and the re-entry
    cooldown. If a resumed session rolls the day over just because it is a fresh
    process, a strategy that had hit its daily loss cap comes back with a clean
    slate and keeps trading — the risk limits reset by an event that has nothing
    to do with risk, and nothing in the trade log would ever show it.
    """
    strategy = _strategy("ema_crossover")

    crashed = _paper(strategy, frame.iloc[:900], tmp_path, session_id="risk")
    crashed.config.flatten_at_session_end = False
    crashed.run()

    _strip_session_end(tmp_path / "risk.jsonl")
    recovered = J.recover("risk", tmp_path)
    assert recovered.risk, "risk state was never journaled"

    resumed = _paper(strategy, frame.iloc[900:920], tmp_path,
                     warmup=frame.iloc[:900], session_id="risk")
    resumed._start()

    assert resumed.engine._current_day == recovered.last_bar_time.date(), (
        "the engine did not know which day it resumed into, so the first bar "
        "would roll the day and wipe the counters"
    )
    assert resumed.risk.entries_today == recovered.risk["entries_today"]
    assert resumed.risk.last_exit_bar == recovered.risk["last_exit_bar"]
    assert resumed._index == recovered.last_bar_index, "bar numbering restarted"


def _strip_session_end(path) -> None:
    """Rewrite a journal as if the process had died rather than shut down."""
    records = [r for r in J.read(path) if r.type is not J.Event.SESSION_END]
    path.write_text(
        "".join(
            json.dumps({"seq": r.seq, "ts": r.ts.isoformat(),
                        "type": r.type.value, "data": r.data}) + "\n"
            for r in records
        )
    )


def test_gate_replayed_orders_after_a_crash_do_not_double_fill(frame, tmp_path):
    """Recovery re-arms idempotency, so a replayed intent produces no second order."""
    strategy = _strategy("ema_crossover")

    session = _paper(strategy, frame.iloc[:400], tmp_path, session_id="dupes")
    session.config.flatten_at_session_end = False
    session.run()

    tags = [f.client_tag for f in session.broker.fills]
    assert tags, "no fills to test against"
    assert len(tags) == len(set(tags)), "duplicate client tags were issued"

    replay = _paper(strategy, frame.iloc[400:420], tmp_path,
                    warmup=frame.iloc[:400], session_id="dupes")
    replay.run()

    recovered = J.recover("dupes", tmp_path)
    all_tags = [
        r.data["client_tag"] for r in J.read(tmp_path / "dupes.jsonl")
        if r.type is J.Event.FILL
    ]
    assert len(all_tags) == len(set(all_tags)), "a tag filled twice across the restart"
    assert recovered.filled_tags


def test_gate_a_feed_gap_never_double_fills_or_breaks_reconciliation(frame, tmp_path):
    """Drop a block of bars mid-session, as a 60-second outage would.

    The engine must carry on from the bars it does get, and the broker's ledger
    must still agree with the engine's positions afterwards. A disagreement means
    the session no longer knows what it holds.
    """
    strategy = _strategy("ema_crossover")

    gapped = pd.concat([frame.iloc[:300], frame.iloc[320:]], ignore_index=True)
    session = _paper(strategy, gapped, tmp_path, session_id="gap")
    session.run()

    assert session.live.reconciled, session.live.errors
    assert session.broker.reconcile_against(session.portfolio).agrees
    assert session.live.state == "stopped"
    tags = [f.client_tag for f in session.broker.fills]
    assert len(tags) == len(set(tags))


def test_gate_repeated_paper_runs_are_deterministic(frame, tmp_path):
    strategy = _strategy("bollinger_breakout")

    first = _paper(strategy, frame, tmp_path, session_id="det-1")
    first.run()
    second = _paper(strategy, frame, tmp_path, session_id="det-2")
    second.run()

    assert [t.as_dict() for t in first.portfolio.closed_trades] == [
        t.as_dict() for t in second.portfolio.closed_trades
    ]


# ---------------------------------------------------------------------------
# Session behaviour
# ---------------------------------------------------------------------------

class TestSession:
    def test_snapshot_has_what_the_dashboard_needs(self, frame, tmp_path):
        strategy = _strategy("ema_crossover")
        session = _paper(strategy, frame.iloc[:500], tmp_path, session_id="snap")
        session.run()

        snap = session.snapshot(include_bars=True)
        assert set(snap) >= {
            "session_id", "state", "mode", "symbol", "equity", "realized_pnl",
            "unrealized_pnl", "net_pnl", "open_positions", "trades", "feed",
            "risk", "blocked", "reconciled", "bars_seen",
        }
        assert snap["state"] == "stopped"
        assert snap["bars_seen"] == 500
        assert isinstance(snap["bars"], list)
        json.dumps(snap)      # the API returns this; it must serialize

    def test_session_id_is_stable_for_the_same_strategy_and_day(self):
        strategy = _strategy("ema_crossover")
        day = date(2026, 1, 5)
        assert make_session_id(strategy, SYMBOL, day) == make_session_id(strategy, SYMBOL, day)
        assert make_session_id(strategy, SYMBOL, day) != make_session_id(
            strategy, "NSE_EQ|OTHER", day
        )

    def test_open_positions_are_flattened_when_the_feed_ends(self, frame, tmp_path):
        strategy = _strategy("ema_crossover")
        session = _paper(strategy, frame, tmp_path, session_id="flat")
        session.run()
        assert not session.portfolio.has_open_positions

    def test_cold_start_without_history_refuses_to_trade(self, frame, tmp_path):
        """Priming on too little history would trade on values no backtest produced."""
        strategy = _strategy("ema_crossover")
        session = _paper(strategy, frame.iloc[:50], tmp_path,
                         warmup=frame.iloc[:5], session_id="cold")
        with pytest.raises(RuntimeError, match="not warm"):
            session.run()

    def test_warmup_primes_indicators_so_a_midday_start_is_not_a_cold_start(
        self, frame, tmp_path
    ):
        """Same bars, but split into warmup + live. Trades must still match."""
        strategy = _strategy("ema_crossover")
        expected = _backtest(strategy, frame)

        split = 600
        session = _paper(strategy, frame.iloc[split:], tmp_path,
                         warmup=frame.iloc[:split], session_id="midday")
        session.run()

        # Trades entirely after the split must be identical.
        cutoff = pd.Timestamp(frame["timestamp"].iloc[split]).tz_convert(IST)
        after = [t for t in expected.trades if t.entry_time >= cutoff]
        assert len(session.portfolio.closed_trades) == len(after)
        for got, want in zip(session.portfolio.closed_trades, after):
            assert got.entry_time == want.entry_time
            assert abs(got.net_pnl - want.net_pnl) < Decimal("0.01")


# ---------------------------------------------------------------------------
# The streaming-context regression
# ---------------------------------------------------------------------------

def test_streaming_context_is_visible_to_previous(frame):
    """`previous()` must reach session context, or pivot crossovers can never fire.

    The vectorized path was fixed for this once already. The streaming path kept
    context in a side dict that `previous()` never consulted, so a CPR strategy
    that back-tests fine would have taken zero trades live — and looked exactly
    like a strategy that found no setups.
    """
    from backend.strategy.indicator_set import StreamingIndicatorSet
    from backend.indicators.base import PriceBar

    indicators = StreamingIndicatorSet([], [])

    indicators.update(PriceBar(100.0, 101.0, 99.0, 100.5, 1000))
    indicators.set_context({"tc": 102.0, "pivot": 100.0})
    assert indicators.value("tc") == 102.0

    indicators.update(PriceBar(100.5, 103.0, 100.0, 102.5, 1200))
    indicators.set_context({"tc": 102.4, "pivot": 100.2})

    assert indicators.previous("tc") == 102.0, "context did not survive into previous()"
    assert indicators.value("tc") == 102.4


def test_streaming_readiness_ignores_context(frame):
    """Pivots are NaN on day one by definition; that must not stall warmup."""
    from backend.strategy.indicator_set import StreamingIndicatorSet
    from backend.indicators.base import PriceBar

    indicators = StreamingIndicatorSet([], [])
    indicators.update(PriceBar(100.0, 101.0, 99.0, 100.5, 1000))
    indicators.set_context({"pivot": float("nan")})
    assert indicators.ready


# ---------------------------------------------------------------------------
# Polling source
# ---------------------------------------------------------------------------

def test_polling_source_never_emits_a_bar_still_forming(frame, monkeypatch):
    """Upstox returns the in-progress candle. Acting on it is acting on a close
    that has not happened — the exact lookahead the backtest forbids."""
    from backend.live import sources

    now = datetime.fromisoformat("2026-01-05T09:33:00").replace(tzinfo=IST)
    candles = pd.DataFrame(
        {
            "timestamp": [
                datetime.fromisoformat(f"2026-01-05T{t}").replace(tzinfo=IST)
                for t in ("09:15:00", "09:20:00", "09:25:00", "09:30:00")
            ],
            "open": [100.0, 101, 102, 103], "high": [104.0, 104, 104, 104],
            "low": [99.0, 99, 99, 99], "close": [101.0, 102, 103, 104],
            "volume": [10, 20, 30, 40], "oi": [0, 0, 0, 0],
        }
    )

    source = sources.RestPollingSource(SYMBOL, "5m", clock=lambda: now)
    emitted = list(source._emit_closed(candles, now))

    times = [c.bar.timestamp.strftime("%H:%M") for c in emitted]
    # 09:30 closes at 09:35, which has not happened at 09:33.
    assert times == ["09:15", "09:20", "09:25"]


def test_polling_source_does_not_repeat_bars_across_polls(monkeypatch):
    from backend.live import sources

    now = datetime.fromisoformat("2026-01-05T09:33:00").replace(tzinfo=IST)
    candles = pd.DataFrame(
        {
            "timestamp": [
                datetime.fromisoformat(f"2026-01-05T{t}").replace(tzinfo=IST)
                for t in ("09:15:00", "09:20:00", "09:25:00")
            ],
            "open": [100.0, 101, 102], "high": [104.0, 104, 104],
            "low": [99.0, 99, 99], "close": [101.0, 102, 103],
            "volume": [10, 20, 30], "oi": [0, 0, 0],
        }
    )

    source = sources.RestPollingSource(SYMBOL, "5m", clock=lambda: now)
    assert len(list(source._emit_closed(candles, now))) == 3
    assert list(source._emit_closed(candles, now)) == []      # same poll again
