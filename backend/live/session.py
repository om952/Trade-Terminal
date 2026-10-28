"""PaperSession — the live trading loop.

This is the class that has to be boring. Every interesting decision was already
made and tested in `StrategyEngine`; all this does is hand it bars in the right
order and route what comes back.

The loop is a deliberate, line-for-line mirror of `run_backtest`:

    for each closed bar:
        1. advance indicators to this bar
        2. fill what the PREVIOUS bar decided, at THIS bar's open
        3. evaluate this bar's close
        4. fill resting orders (stops, targets, square-off) inside this bar

Step 2 before step 3 is what makes a live signal unable to fill on the bar that
produced it — the same guarantee the backtest gives, enforced the same way. If
these two loops ever drift apart, every backtest result becomes a claim about a
system that no longer exists, so `test_live.py` asserts they produce identical
trades over identical bars.

What is genuinely new here, and where the risk lives, is everything around the
loop: a feed that can drop, a process that can die mid-fill, and a clock that
keeps running whether or not data arrives.
"""

from __future__ import annotations

import logging
import re
import threading
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from backend.core.config import IST, Mode, settings
from backend.core.interfaces import Bar, Order, OrderType, Side
from backend.execution.costs import segment_for
from backend.indicators.base import PriceBar
from backend.live import journal as J
from backend.live.alerts import Alerts
from backend.live.broker import SimulatedBroker
from backend.live.feed import LiveFeed
from backend.strategy.engine import OrderIntent, StrategyEngine
from backend.strategy.risk import Block, RiskGuard
from backend.strategy.runner import FillModel, _session_context, preflight
from backend.strategy.schema import Strategy
from backend.strategy.state import ClosedTrade, Portfolio, TradeReason

log = logging.getLogger(__name__)

ZERO = Decimal("0")


def make_session_id(strategy: Strategy, instrument_key: str, day: date | None = None) -> str:
    """Stable per strategy, per instrument, per day.

    Stability is the point: restarting after a crash must land on the same
    journal so the session resumes rather than starting a second one that knows
    nothing about the position the first one opened.
    """
    day = day or datetime.now(IST).date()
    safe = re.sub(r"[^A-Za-z0-9]+", "-", instrument_key).strip("-")
    return f"{day:%Y%m%d}-{safe}-{strategy.content_hash()[:8]}"


@dataclass
class SessionConfig:
    strategy: Strategy
    instrument_key: str
    starting_capital: Decimal = Decimal("100000")
    lot_size: int = 1
    slippage_bps: float = 5.0
    max_leverage: Decimal = Decimal("5")
    warmup_bars: int = 400
    mode: Mode = Mode.PAPER
    alerts_enabled: bool = True
    flatten_at_session_end: bool = True
    # Where bars come from: "live" (the broker feed) or "replay" (stored candles).
    # Journaled so a restarted app knows which interrupted sessions it can offer
    # to resume.
    source_kind: str = "live"


@dataclass
class _Live:
    """Mutable session state, read by the dashboard under a lock."""

    state: str = "created"
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    stop_reason: str = ""
    bars_seen: int = 0
    last_bar: Bar | None = None
    gap_filled_bars: int = 0
    blocked: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    resumed: bool = False
    reconciled: bool = True


class PaperSession:
    """Runs one strategy against a live feed, journaling as it goes."""

    def __init__(
        self,
        config: SessionConfig,
        feed: LiveFeed,
        broker: SimulatedBroker,
        journal: J.Journal,
        alerts: Alerts | None = None,
        risk: RiskGuard | None = None,
    ) -> None:
        self.config = config
        self.feed = feed
        self.broker = broker
        self.journal = journal
        self.alerts = alerts or Alerts(enabled=config.alerts_enabled)

        strategy = config.strategy
        self.portfolio = Portfolio(starting_capital=config.starting_capital)
        self.risk = risk or RiskGuard(
            rules=strategy.risk,
            max_leverage=config.max_leverage,
            account_max_daily_loss=(
                Decimal(str(settings.max_daily_loss)) if settings.max_daily_loss else None
            ),
            account_max_positions=settings.max_concurrent_positions,
        )
        self.engine = StrategyEngine(
            strategy, portfolio=self.portfolio, risk=self.risk, lot_size=config.lot_size
        )
        self.fill_model = FillModel(
            slippage_bps=config.slippage_bps,
            segment=segment_for(
                strategy.instrument.segment, strategy.is_intraday, strategy.legs[0].kind.value
            ),
        )
        self.indicators = self.engine.prepare_live()

        self.live = _Live()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._pending: list[OrderIntent] = []
        self._index = -1
        self._bar_ts: datetime | None = None
        self._frame_rows: list[dict] = []
        self._needs_context = _session_context is not None and self._context_referenced()
        self._recent: list[dict] = []

    # -- construction --------------------------------------------------------

    @classmethod
    def create(
        cls,
        config: SessionConfig,
        source=None,
        journal_dir=None,
        session_id: str | None = None,
        history_frame: pd.DataFrame | None = None,
        history_before: datetime | None = None,
    ) -> PaperSession:
        """Build a session with the default REST-polling feed.

        `history_frame` pins what the indicators warm up on. It must be supplied
        whenever the source does not continue from the present — replaying an old
        window while priming from the newest bars in the store means the first
        several bars are evaluated against indicator values drawn from a year
        later, and the session diverges from its own backtest until they decay.
        """
        from backend.live.sources import RestPollingSource

        strategy = config.strategy
        source = source or RestPollingSource(config.instrument_key, strategy.timeframe)
        feed = LiveFeed(
            config.instrument_key, strategy.timeframe, source,
            history_frame=history_frame, history_before=history_before,
        )
        broker = SimulatedBroker(
            FillModel(
                slippage_bps=config.slippage_bps,
                segment=segment_for(
                    strategy.instrument.segment, strategy.is_intraday, strategy.legs[0].kind.value
                ),
            )
        )
        sid = session_id or make_session_id(strategy, config.instrument_key)
        return cls(config, feed, broker, J.Journal(sid, journal_dir))

    def _context_referenced(self) -> bool:
        from backend.strategy.schema import CONTEXT_FIELDS

        strategy = self.config.strategy
        referenced = set(strategy.entry.referenced_ids())
        if strategy.exit:
            referenced |= strategy.exit.referenced_ids()
        return bool(referenced & CONTEXT_FIELDS)

    # -- lifecycle -----------------------------------------------------------

    def run(self) -> None:
        """Block until the feed ends or `stop()` is called."""
        try:
            self._start()
        except Exception as exc:
            self._fail("startup", exc)
            raise

        reason = "feed_exhausted"
        try:
            for bar in self.feed.stream():
                if self._stop.is_set():
                    break
                self._on_bar(bar)
            # `stop()` also stops the source, so the stream usually just ends
            # rather than yielding one more bar. Either way the session was
            # stopped, not exhausted: keep the real reason, which decides whether
            # positions are flattened and whether a restart resumes.
            if self._stop.is_set():
                reason = self.live.stop_reason or "stopped"
        except Exception as exc:
            self._fail("loop", exc)
            reason = "error"
            raise
        finally:
            self._finish(reason)

    def stop(self, reason: str = "manual") -> None:
        with self._lock:
            self.live.stop_reason = reason
        self._stop.set()
        stop = getattr(self.feed.source, "stop", None)
        if callable(stop):
            stop()

    # -- start ---------------------------------------------------------------

    def _start(self) -> None:
        config, strategy = self.config, self.config.strategy
        with self._lock:
            self.live.state = "warming_up"
            self.live.started_at = datetime.now(IST)

        if config.mode is Mode.LIVE:
            RiskGuard.require_account_limits()

        # Recover before warming up, not after: the journal says how far this
        # session already got, and warm-up has to reach that point rather than
        # stopping where the feed's window nominally starts.
        recovered = J.recover(self.journal.session_id, self.journal.path.parent)
        resuming = recovered.resumable

        warmup = self.feed.warmup(
            config.instrument_key,
            config.warmup_bars,
            through=recovered.last_bar_time if resuming else None,
        )
        frame = self.feed.warmup_frame
        if frame is not None and not frame.empty:
            for warning in preflight(strategy, frame):
                log.warning("PREFLIGHT %s: %s", strategy.name, warning)
                self.live.warnings.append(warning)

        for bar in warmup:
            self._advance_indicators(bar, warming=True)
        if warmup and self._needs_context:
            # Computed once at the end rather than per warmup bar: the values only
            # matter from the first live bar onward, and this keeps priming from
            # being quadratic in the length of the history.
            context = self._compute_context()
            if context:
                self.indicators.set_context(context)

        if warmup and not self.indicators.ready:
            raise RuntimeError(
                f"Indicators are still not warm after {len(warmup)} bars. "
                f"The longest indicator period exceeds the history available, so "
                f"the strategy would trade on values the backtest never produced. "
                f"Fetch more history for {config.instrument_key} "
                f"{strategy.timeframe}, or shorten the indicator periods."
            )

        if resuming:
            self._restore(recovered)
            # Do not re-deliver bars the journal already recorded. A live feed
            # gets this for free (the market only moves forward); a replay does
            # not, and would otherwise re-trade the whole window.
            prime = getattr(self.feed.source, "prime", None)
            if callable(prime):
                prime(recovered.last_bar_time)

        self.journal.append(
            J.Event.SESSION_START,
            {
                "strategy": strategy.model_dump(mode="json"),
                "strategy_hash": strategy.content_hash(),
                "symbol": config.instrument_key,
                "timeframe": strategy.timeframe,
                "mode": config.mode.value,
                "source_kind": config.source_kind,
                "starting_capital": config.starting_capital,
                "lot_size": config.lot_size,
                "slippage_bps": config.slippage_bps,
                "warmup_bars": len(warmup),
                "resumed": self.live.resumed,
                "warnings": self.live.warnings,
            },
        )

        with self._lock:
            self.live.state = "running"

        self.alerts.session_start(
            strategy.name, config.instrument_key, strategy.timeframe, config.mode.value
        )
        log.info(
            "Session %s running: %s on %s %s (%d warmup bars%s)",
            self.journal.session_id, strategy.name, config.instrument_key,
            strategy.timeframe, len(warmup), ", resumed" if self.live.resumed else "",
        )

    def _restore(self, recovered: J.Recovered) -> None:
        """Rebuild position state from the journal after a crash.

        Snapshots are replayed, not recomputed. A stop that was set from the entry
        bar's ATR must come back as the same number — recomputing it from an
        indicator series that warmed up differently would silently move a live
        stop because the process restarted.
        """
        portfolio = self.portfolio

        if recovered.portfolio:
            portfolio.realized_pnl = Decimal(str(recovered.portfolio.get("realized_pnl", "0")))
            portfolio.fees_paid = Decimal(str(recovered.portfolio.get("fees_paid", "0")))
            portfolio.day_realized_pnl = Decimal(
                str(recovered.portfolio.get("day_realized_pnl", "0"))
            )
            portfolio.day_trade_count = int(recovered.portfolio.get("day_trade_count", 0))
            portfolio.day_entry_count = int(recovered.portfolio.get("day_entry_count", 0))

        for leg_id, snap in recovered.positions.items():
            position = portfolio.position(leg_id, snap["symbol"], self.config.lot_size)
            position.quantity = int(snap["quantity"])
            position.average_price = Decimal(str(snap["average_price"]))
            position.realized_pnl = Decimal(str(snap.get("realized_pnl", "0")))
            position.fees_paid = Decimal(str(snap.get("fees_paid", "0")))
            position.entry_fees = Decimal(str(snap.get("entry_fees", "0")))
            position.entry_bar_index = int(snap.get("entry_bar_index", 0))
            position.stop_price = _opt_decimal(snap.get("stop_price"))
            position.target_price = _opt_decimal(snap.get("target_price"))
            position.trail_anchor = _opt_decimal(snap.get("trail_anchor"))
            position.trail_armed = bool(snap.get("trail_armed", False))
            opened = snap.get("opened_at")
            position.opened_at = datetime.fromisoformat(opened) if opened else None

        for record in recovered.trades:
            portfolio.closed_trades.append(_trade_from_dict(record))

        self.broker.restore(recovered.filled_tags)

        if recovered.risk:
            self.risk.entries_today = int(recovered.risk.get("entries_today", 0))
            last_exit = recovered.risk.get("last_exit_bar")
            self.risk.last_exit_bar = int(last_exit) if last_exit is not None else None
            if recovered.risk.get("halted"):
                self.risk.halted = True
                self.risk.halt_reason = Block(recovered.risk.get("halt_reason", "none"))

        # A decision made on the last bar before the crash still fills on the
        # next one. Dropping it would make the resumed session diverge from an
        # uninterrupted one in a way nothing downstream could detect.
        self._pending = [_intent_from_dict(d) for d in recovered.pending]

        # Tell the engine which day it is already in, or the first bar after the
        # restart looks like a new session and rolls the day over.
        #
        # That rollover is not cosmetic: `start_new_day` zeroes the daily realized
        # P&L, the trade counter and the re-entry cooldown. A strategy that had
        # hit its max-trades-per-day limit, or tripped its daily loss cap, would
        # come back from a crash with a clean slate and keep trading — the risk
        # limits silently reset by an event that has nothing to do with risk.
        # Restarting must never be a way to buy more trades.
        if recovered.last_bar_time is not None:
            self.engine._current_day = recovered.last_bar_time.date()

        # Continue the journal's bar numbering rather than restarting it, so
        # cooldowns and holding periods measured before the crash stay comparable
        # to the bars that come after it.
        if recovered.last_bar_index >= 0:
            self._index = recovered.last_bar_index

        with self._lock:
            self.live.resumed = True

        open_legs = recovered.open_legs
        log.warning(
            "Resumed session %s from journal: %d trade(s), %d open position(s) %s",
            recovered.session_id, len(recovered.trades), len(open_legs), open_legs or "",
        )
        self.alerts.send(
            f"♻️ Resumed {self.config.strategy.name} from journal\n"
            f"{len(recovered.trades)} trade(s), {len(open_legs)} open"
        )

    # -- the loop ------------------------------------------------------------

    def _on_bar(self, bar: Bar) -> None:
        ts = bar.timestamp
        # A fill is stamped with the bar it filled on, not the bar that decided
        # it. The backtest passes the current bar's timestamp into `apply_fill`
        # for exactly this reason: an entry signalled at 13:30 and filled at
        # 13:35 opened at 13:35, and recording 13:30 would credit the position
        # with five minutes of a move it was not in.
        self._bar_ts = ts
        price_bar = self._advance_indicators(bar)

        # 1. fill what the previous bar decided, at this bar's open
        for intent in self._pending:
            self._execute(intent, bar.open, OrderType.MARKET)
        self._pending = []

        # 2. evaluate this bar's close
        event = self.engine.on_bar(self._index, ts, price_bar, self.indicators)

        # 3. resting orders execute inside this bar
        deferred: list[OrderIntent] = []
        for intent in event.intents:
            if intent.same_bar and intent.limit_price is not None:
                self._execute(intent, intent.limit_price, OrderType.SL_M)
            else:
                deferred.append(intent)
        self._pending = deferred

        self.broker.mark(bar.symbol, bar.close)
        self._journal_bar(bar, event)
        self._record(bar, event)

    def _advance_indicators(self, bar: Bar, warming: bool = False) -> PriceBar:
        """Feed one bar to the streaming indicators and the context frame.

        Warm-up bars prime indicators but do not advance the bar index. The index
        is the engine's clock, and it has to mean the same thing across a restart:
        `last_exit_bar` recovered from the journal is compared against it to
        enforce the re-entry cooldown, and `bars_held` is measured in it. If the
        index restarted from however many bars warmup happened to load, a resumed
        session would compare a fresh small index against a recovered large one,
        the difference would go negative, and the cooldown would never expire —
        the strategy would simply stop entering, with nothing to say why.
        """
        if not warming:
            self._index += 1

        price_bar = PriceBar(
            float(bar.open), float(bar.high), float(bar.low), float(bar.close), float(bar.volume)
        )
        self.indicators.update(price_bar)

        if self._needs_context:
            self._frame_rows.append(
                {
                    "timestamp": bar.timestamp,
                    "open": float(bar.open), "high": float(bar.high),
                    "low": float(bar.low), "close": float(bar.close),
                    "volume": float(bar.volume),
                }
            )
            if not warming:
                context = self._compute_context()
                if context:
                    self.indicators.set_context(context)

        # Reproduce the vectorized warm-up gate exactly: the engine may not trade
        # until every indicator has produced a real value, and `warmup_bars()` in
        # the backtest is precisely the first index at which that is true. Only
        # live bars count — history already primed them, or startup refused.
        if not warming and not self.indicators.ready:
            self.engine._warmup = self._index + 1

        return price_bar

    def _compute_context(self) -> dict[str, float]:
        """Session values (pivots, VWAP) recomputed over the bars seen so far.

        Recomputing the whole frame each bar is wasteful and deliberate: it is the
        same call the backtest makes over the same rows, so the numbers cannot
        drift from it. A session is a few thousand bars; the cost is noise.
        """
        frame = pd.DataFrame(self._frame_rows)
        try:
            context = _session_context(self.config.strategy, frame)
        except Exception as exc:
            log.warning("Context computation failed: %s", exc)
            return {}
        if context is None or context.empty:
            return {}
        return {k: float(v) for k, v in context.iloc[-1].items()}

    def _execute(self, intent: OrderIntent, reference: Decimal, order_type: OrderType) -> None:
        """Route one intent through the broker, then book the fill in the engine."""
        tag = f"{self.journal.session_id}#{intent.bar_index}#{intent.leg_id}#{intent.reason.value}"
        order = Order(
            client_tag=tag,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            order_type=order_type,
            price=reference,
            strategy_id=self.config.strategy.id,
            leg_id=intent.leg_id,
        )
        self.journal.append(
            J.Event.ORDER,
            {
                "client_tag": tag, "leg_id": intent.leg_id, "symbol": intent.symbol,
                "side": intent.side.value, "quantity": intent.quantity,
                "order_type": order_type.value, "reference_price": reference,
                "reason": intent.reason.value, "index": intent.bar_index,
                "same_bar": intent.same_bar,
            },
        )

        fill = self.broker.place(order)
        if fill is None:
            self._note_error(f"order {tag} rejected by broker")
            return

        trade = self.engine.apply_fill(
            intent, fill.price, fill.fees, self._bar_ts, self._index, self.indicators
        )
        position = self.portfolio.positions[intent.leg_id]

        self.journal.append(
            J.Event.FILL,
            {
                "client_tag": tag,
                "broker_order_id": fill.broker_order_id,
                "leg_id": intent.leg_id,
                "symbol": intent.symbol,
                "side": fill.side.value,
                "quantity": fill.quantity,
                "price": fill.price,
                "fees": fill.fees,
                "reason": intent.reason.value,
                "index": self._index,
                "position": _position_snapshot(position),
                "portfolio": _portfolio_snapshot(self.portfolio),
                "risk": _risk_snapshot(self.risk),
            },
        )

        if trade is not None:
            self.journal.append(J.Event.TRADE, trade.as_dict())
            self.alerts.exit(
                trade.symbol, trade.quantity, trade.exit_price, trade.net_pnl, trade.reason.value
            )
        elif position.is_open:
            self.alerts.entry(intent.symbol, intent.side.value, intent.quantity, fill.price)

        report = self.broker.reconcile_against(self.portfolio)
        if not report.agrees:
            with self._lock:
                self.live.reconciled = False
            message = "Position mismatch: " + "; ".join(report.differences)
            self._note_error(message)
            self.alerts.error(message + "\nHalting — the session no longer knows what it holds.")
            self.stop("reconcile_failed")

    # -- journaling and snapshots -------------------------------------------

    def _journal_bar(self, bar: Bar, event) -> None:
        self.journal.append(
            J.Event.BAR,
            {
                "index": self._index, "t": bar.timestamp,
                "o": bar.open, "h": bar.high, "l": bar.low, "c": bar.close,
                "v": bar.volume,
                # Carried so a restart can honour a decision made on this bar
                # whose fill bar had not arrived yet.
                "pending": [_intent_snapshot(i) for i in self._pending],
            },
        )
        if event.entry_signal or event.exit_signal or event.blocked is not Block.NONE:
            self.journal.append(
                J.Event.SIGNAL,
                {
                    "index": self._index,
                    "entry": event.entry_signal,
                    "exit": event.exit_signal,
                    "blocked": event.blocked.value,
                    "note": event.note,
                },
            )

    def _record(self, bar: Bar, event) -> None:
        with self._lock:
            self.live.bars_seen += 1
            self.live.last_bar = bar
            if event.blocked is not Block.NONE:
                key = event.blocked.value
                self.live.blocked[key] = self.live.blocked.get(key, 0) + 1
            self._recent.append(
                {
                    "t": bar.timestamp.isoformat(),
                    "o": float(bar.open), "h": float(bar.high),
                    "l": float(bar.low), "c": float(bar.close),
                    "v": bar.volume,
                    "entry": event.entry_signal, "exit": event.exit_signal,
                    "blocked": event.blocked.value if event.blocked is not Block.NONE else None,
                }
            )
            del self._recent[:-500]

    def _note_error(self, message: str) -> None:
        log.error(message)
        with self._lock:
            self.live.errors.append(message)
        self.journal.append(J.Event.ERROR, {"message": message})

    def _fail(self, where: str, exc: Exception) -> None:
        message = f"{where}: {type(exc).__name__}: {exc}"
        log.exception("Session failure in %s", where)
        with self._lock:
            self.live.state = "error"
            self.live.errors.append(message)
        self.journal.append(
            J.Event.ERROR, {"message": message, "traceback": traceback.format_exc()[-4000:]}
        )
        self.alerts.error(message)

    def _finish(self, reason: str) -> None:
        if self.config.flatten_at_session_end and reason in ("feed_exhausted", "session_end"):
            self._flatten("session end")

        prices = self._mark_prices()
        with self._lock:
            if self.live.state != "error":
                self.live.state = "stopped"
            self.live.stopped_at = datetime.now(IST)
            self.live.stop_reason = self.live.stop_reason or reason

        self.journal.append(
            J.Event.SESSION_END,
            {
                "reason": reason,
                "bars": self.live.bars_seen,
                "trades": len(self.portfolio.closed_trades),
                "equity": self.portfolio.equity(prices),
                "realized_pnl": self.portfolio.realized_pnl,
                "fees_paid": self.portfolio.fees_paid,
            },
        )
        self.journal.close()

        net = self.portfolio.realized_pnl - self.portfolio.fees_paid
        self.alerts.session_end(
            self.config.strategy.name, len(self.portfolio.closed_trades), net, reason
        )
        log.info(
            "Session %s ended (%s): %d bars, %d trades, net %.2f",
            self.journal.session_id, reason, self.live.bars_seen,
            len(self.portfolio.closed_trades), float(net),
        )

    def _flatten(self, why: str) -> None:
        """Close anything still open, at the last known close.

        Mirrors the backtest, which flattens at the end of its data. Leaving a
        position open would park an unrealized loss outside the reported result.
        """
        last = self.live.last_bar
        if last is None or not self.portfolio.has_open_positions:
            return

        for position in list(self.portfolio.open_positions):
            intent = OrderIntent(
                leg_id=position.leg_id,
                symbol=position.symbol,
                side=Side.SELL if position.is_long else Side.BUY,
                quantity=abs(position.quantity),
                reason=TradeReason.SESSION_END,
                bar_index=self._index,
                timestamp=last.timestamp,
            )
            log.info("Flattening %s at %s (%s)", position.leg_id, last.close, why)
            self._execute(intent, last.close, OrderType.MARKET)

    def _mark_prices(self) -> dict[str, Decimal]:
        last = self.live.last_bar
        return {self.config.instrument_key: last.close} if last else {}

    # -- dashboard -----------------------------------------------------------

    def snapshot(self, include_bars: bool = False) -> dict[str, Any]:
        """A consistent view of the session. Safe to call from another thread."""
        with self._lock:
            prices = self._mark_prices()
            portfolio = self.portfolio
            equity = portfolio.equity(prices)
            unrealized = portfolio.unrealized_pnl(prices)
            last = self.live.last_bar

            payload: dict[str, Any] = {
                "session_id": self.journal.session_id,
                "state": self.live.state,
                "mode": self.config.mode.value,
                "resumed": self.live.resumed,
                "strategy": {
                    "name": self.config.strategy.name,
                    "hash": self.config.strategy.content_hash(),
                    "timeframe": self.config.strategy.timeframe,
                },
                "symbol": self.config.instrument_key,
                "started_at": _iso(self.live.started_at),
                "stopped_at": _iso(self.live.stopped_at),
                "stop_reason": self.live.stop_reason,
                "bars_seen": self.live.bars_seen,
                "last_bar": {
                    "t": _iso(last.timestamp), "o": float(last.open), "h": float(last.high),
                    "l": float(last.low), "c": float(last.close), "v": last.volume,
                } if last else None,
                "starting_capital": float(portfolio.starting_capital),
                "equity": float(equity),
                "realized_pnl": float(portfolio.realized_pnl),
                "unrealized_pnl": float(unrealized),
                "fees_paid": float(portfolio.fees_paid),
                "net_pnl": float(portfolio.realized_pnl - portfolio.fees_paid + unrealized),
                "open_positions": [
                    {
                        "leg_id": p.leg_id, "symbol": p.symbol, "quantity": p.quantity,
                        "direction": "LONG" if p.is_long else "SHORT",
                        "average_price": float(p.average_price),
                        "last_price": float(prices.get(p.symbol, p.average_price)),
                        "unrealized_pnl": float(
                            p.unrealized_pnl(prices.get(p.symbol, p.average_price))
                        ),
                        "stop_price": _f(p.stop_price),
                        "target_price": _f(p.target_price),
                        "trail_armed": p.trail_armed,
                        "opened_at": _iso(p.opened_at),
                    }
                    for p in portfolio.open_positions
                ],
                "trades": [t.as_dict() for t in portfolio.closed_trades],
                "feed": self.feed.source.status.as_dict(),
                "risk": {"halted": self.risk.halted, "reason": self.risk.halt_reason.value},
                "blocked": dict(self.live.blocked),
                "reconciled": self.live.reconciled,
                "warnings": list(self.live.warnings),
                "errors": self.live.errors[-10:],
                "pending_orders": len(self._pending),
            }
            if include_bars:
                payload["bars"] = list(self._recent)
            return payload


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _f(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _opt_decimal(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value not in (None, "") else None


def _position_snapshot(position) -> dict:
    return {
        "symbol": position.symbol,
        "leg_id": position.leg_id,
        "quantity": position.quantity,
        "average_price": position.average_price,
        "realized_pnl": position.realized_pnl,
        "fees_paid": position.fees_paid,
        "entry_fees": position.entry_fees,
        "entry_bar_index": position.entry_bar_index,
        "opened_at": position.opened_at,
        "stop_price": position.stop_price,
        "target_price": position.target_price,
        "trail_anchor": position.trail_anchor,
        "trail_armed": position.trail_armed,
    }


def _intent_snapshot(intent: OrderIntent) -> dict:
    return {
        "leg_id": intent.leg_id,
        "symbol": intent.symbol,
        "side": intent.side.value,
        "quantity": intent.quantity,
        "reason": intent.reason.value,
        "bar_index": intent.bar_index,
        "timestamp": intent.timestamp,
        "limit_price": intent.limit_price,
        "same_bar": intent.same_bar,
    }


def _intent_from_dict(data: dict) -> OrderIntent:
    return OrderIntent(
        leg_id=data["leg_id"],
        symbol=data["symbol"],
        side=Side(data["side"]),
        quantity=int(data["quantity"]),
        reason=TradeReason(data["reason"]),
        bar_index=int(data["bar_index"]),
        timestamp=datetime.fromisoformat(data["timestamp"]),
        limit_price=_opt_decimal(data.get("limit_price")),
        same_bar=bool(data.get("same_bar", False)),
    )


def _risk_snapshot(risk: RiskGuard) -> dict:
    return {
        "entries_today": risk.entries_today,
        "last_exit_bar": risk.last_exit_bar,
        "halted": risk.halted,
        "halt_reason": risk.halt_reason.value,
    }


def _portfolio_snapshot(portfolio: Portfolio) -> dict:
    return {
        "realized_pnl": portfolio.realized_pnl,
        "fees_paid": portfolio.fees_paid,
        "day_realized_pnl": portfolio.day_realized_pnl,
        "day_trade_count": portfolio.day_trade_count,
        "day_entry_count": portfolio.day_entry_count,
    }


def _trade_from_dict(record: dict) -> ClosedTrade:
    return ClosedTrade(
        strategy_id=record["strategy_id"],
        leg_id=record["leg_id"],
        symbol=record["symbol"],
        direction=record["direction"],
        quantity=int(record["quantity"]),
        entry_time=datetime.fromisoformat(record["entry_time"]),
        entry_price=Decimal(str(record["entry_price"])),
        exit_time=datetime.fromisoformat(record["exit_time"]),
        exit_price=Decimal(str(record["exit_price"])),
        gross_pnl=Decimal(str(record["gross_pnl"])),
        fees=Decimal(str(record["fees"])),
        reason=TradeReason(record["reason"]),
        bars_held=int(record["bars_held"]),
    )
