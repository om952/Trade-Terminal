"""RiskGuard — the three-level kill switch.

Levels, innermost first:

  1. **Leg**    — stop loss, target, trailing stop on an individual position.
  2. **Strategy** — daily loss cap, trade count, concurrent positions, re-entry.
  3. **Account** — a hard daily loss limit that halts everything.

The account level is enforced here, not in the strategy document, and a strategy
cannot raise it. That asymmetry is the point: strategy limits are a trading
decision the author makes, the account limit is a survival decision the operator
makes, and one must not be able to override the other.

`halted` is sticky within a day. Once tripped it stays tripped until the next
session, because a strategy that trips a loss limit and then resumes on the next
green bar is exactly how a bad day becomes a catastrophic one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from backend.strategy.state import Portfolio, TradeReason
from backend.strategy.schema import RiskRules

log = logging.getLogger(__name__)

ZERO = Decimal("0")


class Block(str, Enum):
    """Why an entry was refused. Surfaced in the UI so silence is never a mystery."""

    NONE = "none"
    ACCOUNT_HALTED = "account_halted"
    INSUFFICIENT_CAPITAL = "insufficient_capital"
    ACCOUNT_BLOWN = "account_blown"
    DAILY_LOSS = "daily_loss_limit"
    DAILY_PROFIT = "daily_profit_target"
    MAX_TRADES = "max_trades_per_day"
    MAX_POSITIONS = "max_concurrent_positions"
    RE_ENTRY_EXHAUSTED = "re_entry_exhausted"
    RE_ENTRY_COOLDOWN = "re_entry_cooldown"
    OUTSIDE_SESSION = "outside_session"
    WARMING_UP = "warming_up"


@dataclass
class RiskDecision:
    allowed: bool
    reason: Block = Block.NONE
    detail: str = ""

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class RiskGuard:
    """Evaluates whether trading may proceed, and whether to force an exit."""

    rules: RiskRules
    account_max_daily_loss: Decimal | None = None
    account_max_positions: int | None = None

    # Leverage cap. Without one the engine will happily open a position many
    # times the account's size, run equity negative, and produce a metrics panel
    # full of impossible numbers (drawdowns over 100%, positive Sharpe on a
    # catastrophic loss). Indian intraday equity margin is roughly 5x; F&O is
    # margin-based rather than notional, so raise this deliberately per strategy.
    max_leverage: Decimal = Decimal("5")

    halted: bool = field(default=False, init=False)
    halt_reason: Block = field(default=Block.NONE, init=False)
    entries_today: int = field(default=0, init=False)
    last_exit_bar: int | None = field(default=None, init=False)

    # -- session -------------------------------------------------------------

    def start_new_day(self) -> None:
        self.halted = False
        self.halt_reason = Block.NONE
        self.entries_today = 0
        self.last_exit_bar = None

    def record_entry(self) -> None:
        self.entries_today += 1

    def record_exit(self, bar_index: int) -> None:
        self.last_exit_bar = bar_index

    # -- entry gate ----------------------------------------------------------

    def may_enter(
        self,
        portfolio: Portfolio,
        bar_index: int,
        notional: Decimal | None = None,
        equity: Decimal | None = None,
    ) -> RiskDecision:
        """Whether a new entry is permitted right now.

        `notional` and `equity` enable the capital check. They are optional so
        unit tests of the other limits stay terse, but the engine always supplies
        them — an unchecked position size is how a backtest ends up reporting a
        578% drawdown.
        """
        if self.halted:
            return RiskDecision(False, self.halt_reason, "trading halted for the session")

        if equity is not None:
            if equity <= 0:
                self._halt(Block.ACCOUNT_BLOWN)
                return RiskDecision(
                    False, Block.ACCOUNT_BLOWN,
                    f"equity {equity:.2f} is at or below zero — the account is gone",
                )
            if notional is not None and self.max_leverage > 0:
                allowed = equity * self.max_leverage
                if notional > allowed:
                    return RiskDecision(
                        False, Block.INSUFFICIENT_CAPITAL,
                        f"position notional {notional:,.0f} exceeds {self.max_leverage}x "
                        f"equity {equity:,.0f} (max {allowed:,.0f})",
                    )

        account = self._check_account(portfolio)
        if not account:
            return account

        if self.rules.max_loss_per_day is not None:
            if portfolio.day_realized_pnl <= -Decimal(str(self.rules.max_loss_per_day)):
                self._halt(Block.DAILY_LOSS)
                return RiskDecision(
                    False, Block.DAILY_LOSS,
                    f"day P&L {portfolio.day_realized_pnl} hit strategy limit "
                    f"-{self.rules.max_loss_per_day}",
                )

        if self.rules.max_profit_per_day is not None:
            if portfolio.day_realized_pnl >= Decimal(str(self.rules.max_profit_per_day)):
                self._halt(Block.DAILY_PROFIT)
                return RiskDecision(
                    False, Block.DAILY_PROFIT,
                    f"day P&L {portfolio.day_realized_pnl} hit profit target "
                    f"{self.rules.max_profit_per_day}",
                )

        if self.rules.max_trades_per_day is not None:
            if self.entries_today >= self.rules.max_trades_per_day:
                return RiskDecision(
                    False, Block.MAX_TRADES,
                    f"{self.entries_today} entries today, limit {self.rules.max_trades_per_day}",
                )

        open_count = len(portfolio.open_positions)
        if open_count >= self.rules.max_concurrent_positions:
            return RiskDecision(
                False, Block.MAX_POSITIONS,
                f"{open_count} open, limit {self.rules.max_concurrent_positions}",
            )

        return self._check_re_entry(bar_index)

    def _check_account(self, portfolio: Portfolio) -> RiskDecision:
        if self.account_max_daily_loss is not None:
            if portfolio.day_realized_pnl <= -self.account_max_daily_loss:
                self._halt(Block.ACCOUNT_HALTED)
                return RiskDecision(
                    False, Block.ACCOUNT_HALTED,
                    f"ACCOUNT limit breached: day P&L {portfolio.day_realized_pnl} "
                    f"vs -{self.account_max_daily_loss}",
                )

        if self.account_max_positions is not None:
            if len(portfolio.open_positions) >= self.account_max_positions:
                return RiskDecision(
                    False, Block.MAX_POSITIONS,
                    f"account position cap {self.account_max_positions} reached",
                )

        return RiskDecision(True)

    def _check_re_entry(self, bar_index: int) -> RiskDecision:
        """Governs entering again after a position has already closed today."""
        if self.last_exit_bar is None:
            return RiskDecision(True)  # first entry of the day

        rule = self.rules.re_entry
        if not rule.enabled:
            return RiskDecision(
                False, Block.RE_ENTRY_EXHAUSTED, "re-entry disabled for this strategy"
            )

        if rule.max and self.entries_today > rule.max:
            return RiskDecision(
                False, Block.RE_ENTRY_EXHAUSTED,
                f"{self.entries_today} entries, re-entry max {rule.max}",
            )

        waited = bar_index - self.last_exit_bar
        if waited < rule.cooldown_bars:
            return RiskDecision(
                False, Block.RE_ENTRY_COOLDOWN,
                f"{waited}/{rule.cooldown_bars} cooldown bars since last exit",
            )

        return RiskDecision(True)

    # -- forced exit ---------------------------------------------------------

    def must_flatten(self, portfolio: Portfolio) -> TradeReason | None:
        """Whether open positions must be closed regardless of strategy signals."""
        if self.account_max_daily_loss is not None:
            total = portfolio.day_realized_pnl
            if total <= -self.account_max_daily_loss:
                self._halt(Block.ACCOUNT_HALTED)
                return TradeReason.RISK_LIMIT

        if self.rules.max_loss_per_day is not None:
            if portfolio.day_realized_pnl <= -Decimal(str(self.rules.max_loss_per_day)):
                self._halt(Block.DAILY_LOSS)
                return TradeReason.RISK_LIMIT

        if self.rules.max_profit_per_day is not None:
            if portfolio.day_realized_pnl >= Decimal(str(self.rules.max_profit_per_day)):
                self._halt(Block.DAILY_PROFIT)
                return TradeReason.RISK_LIMIT

        return None

    def _halt(self, reason: Block) -> None:
        if not self.halted:
            log.warning("RiskGuard halted trading: %s", reason.value)
        self.halted = True
        self.halt_reason = reason

    @classmethod
    def from_settings(cls, rules: RiskRules) -> RiskGuard:
        """Build a guard with account limits pulled from .env.

        Unset account limits mean "no account-level cap". That is fine for
        backtests; `require_account_limits` enforces them before live trading.
        """
        from backend.core.config import settings

        return cls(
            rules=rules,
            account_max_daily_loss=(
                Decimal(str(settings.max_daily_loss)) if settings.max_daily_loss else None
            ),
            account_max_positions=settings.max_concurrent_positions,
        )

    @staticmethod
    def require_account_limits() -> None:
        """Refuse to run live without account-level limits configured.

        Called by the live runner. A backtest with no account cap is a modelling
        choice; live trading with no account cap is an unbounded loss.
        """
        from backend.core.config import settings

        missing = []
        if settings.max_daily_loss is None:
            missing.append("MAX_DAILY_LOSS")
        if settings.max_concurrent_positions is None:
            missing.append("MAX_CONCURRENT_POSITIONS")

        if missing:
            raise RuntimeError(
                f"Refusing to trade live: {', '.join(missing)} not set in .env. "
                f"The account-level kill switch must be configured before real orders."
            )
