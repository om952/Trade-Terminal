"""Performance metrics.

Two principles here:

**Report the uncomfortable numbers.** Max drawdown duration, cost drag and
exposure are the figures that tell you whether a strategy is tradeable; return
alone tells you almost nothing. A strategy that makes 30% while spending 200
days underwater is not the same product as one that makes 25% smoothly, and a
metrics panel that shows only the first number is marketing, not analysis.

**Annualize honestly.** Sharpe scales with the square root of periods per year,
and getting `periods_per_year` wrong on intraday data inflates it by ~9x on
5-minute bars. The caller must state the bar frequency; there is no silent default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import numpy as np
import pandas as pd

from backend.strategy.state import ClosedTrade

TRADING_DAYS = 252
RISK_FREE_RATE = 0.065  # India 10y-ish; used for Sharpe/Sortino


@dataclass(frozen=True, slots=True)
class DrawdownInfo:
    max_drawdown: float          # fraction, e.g. 0.18 == 18%
    max_drawdown_value: float    # currency
    peak_date: datetime | None
    trough_date: datetime | None
    recovery_date: datetime | None
    longest_days: int            # longest peak-to-recovery stretch

    @property
    def recovered(self) -> bool:
        return self.recovery_date is not None


@dataclass
class Metrics:
    """The full result panel."""

    # Returns
    starting_capital: float = 0.0
    ending_equity: float = 0.0
    total_return_pct: float = 0.0
    cagr_pct: float = 0.0

    # Risk
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    volatility_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_value: float = 0.0
    max_drawdown_days: int = 0

    # Trades
    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    avg_bars_held: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0

    # Costs — the number most backtests hide
    gross_pnl: float = 0.0
    total_fees: float = 0.0
    net_pnl: float = 0.0
    cost_drag_pct: float = 0.0   # fees as a share of gross profit

    # Context
    bars: int = 0
    trading_days: int = 0
    exposure_pct: float = 0.0
    period_start: datetime | None = None
    period_end: datetime | None = None

    # Set when equity reached zero or below. Every risk-adjusted statistic is
    # meaningless past that point — percentage returns off a negative base are
    # arithmetic noise, and they produced a *positive* Sharpe on a total loss
    # before this flag existed. When True, treat the ratios as undefined.
    account_blown: bool = False
    blown_at: datetime | None = None

    def as_dict(self) -> dict:
        out = {}
        for key in self.__dataclass_fields__:
            value = getattr(self, key)
            out[key] = value.isoformat() if isinstance(value, datetime) else value
        return out

    def summary(self) -> str:
        if self.account_blown:
            return (
                f"ACCOUNT BLOWN at {self.blown_at:%Y-%m-%d} — equity reached zero. "
                f"Net P&L {self.net_pnl:,.0f} on {self.total_trades} trades "
                f"(fees {self.total_fees:,.0f}). Risk ratios undefined."
            )
        return (
            f"Return {self.total_return_pct:+.2f}%  CAGR {self.cagr_pct:+.2f}%  "
            f"Sharpe {self.sharpe:.2f}  MaxDD {self.max_drawdown_pct:.2f}%  "
            f"Trades {self.total_trades}  Win {self.win_rate_pct:.1f}%  "
            f"PF {self.profit_factor:.2f}  Fees {self.total_fees:,.0f}"
        )


def bars_per_year(timeframe: str) -> int:
    """Bars in a trading year for a timeframe. Needed to annualize correctly."""
    from backend.data.upstox_client import Timeframe

    tf = Timeframe.parse(timeframe)
    if tf.unit == "days":
        return TRADING_DAYS // tf.interval
    if tf.unit == "weeks":
        return 52 // tf.interval
    if tf.unit == "months":
        return 12 // tf.interval

    minutes_per_session = 375
    per_bar = tf.interval * (60 if tf.unit == "hours" else 1)
    return max(1, TRADING_DAYS * (minutes_per_session // per_bar))


def compute_drawdown(equity: pd.Series) -> DrawdownInfo:
    """Peak-to-trough decline, and how long it took to recover.

    Duration matters as much as depth. A 15% drawdown that recovers in a week is
    an inconvenience; the same 15% taking eight months is what makes people
    abandon a working strategy at the worst possible moment.
    """
    if equity.empty:
        return DrawdownInfo(0.0, 0.0, None, None, None, 0)

    values = equity.astype("float64")
    running_peak = values.cummax()
    drawdown = (values - running_peak) / running_peak.replace(0, np.nan)

    trough_idx = drawdown.idxmin()
    max_dd = float(drawdown.min()) if not math.isnan(drawdown.min()) else 0.0

    peak_idx = values.loc[:trough_idx].idxmax() if trough_idx in values.index else None
    peak_value = float(values.loc[peak_idx]) if peak_idx is not None else 0.0
    trough_value = float(values.loc[trough_idx]) if trough_idx is not None else 0.0

    recovery_idx = None
    if peak_idx is not None:
        after = values.loc[trough_idx:]
        recovered = after[after >= peak_value]
        if not recovered.empty:
            recovery_idx = recovered.index[0]

    # Longest stretch spent below a previous peak, in calendar days.
    longest = 0
    if isinstance(equity.index, pd.DatetimeIndex):
        underwater = values < running_peak
        start = None
        for ts, is_under in underwater.items():
            if is_under and start is None:
                start = ts
            elif not is_under and start is not None:
                longest = max(longest, (ts - start).days)
                start = None
        if start is not None:
            longest = max(longest, (underwater.index[-1] - start).days)

    return DrawdownInfo(
        max_drawdown=abs(max_dd),
        max_drawdown_value=abs(peak_value - trough_value),
        peak_date=peak_idx if isinstance(peak_idx, datetime) else None,
        trough_date=trough_idx if isinstance(trough_idx, datetime) else None,
        recovery_date=recovery_idx if isinstance(recovery_idx, datetime) else None,
        longest_days=longest,
    )


def _consecutive(flags: list[bool]) -> tuple[int, int]:
    """(longest run of True, longest run of False)."""
    best_true = best_false = run = 0
    current: bool | None = None
    for flag in flags:
        if flag == current:
            run += 1
        else:
            current, run = flag, 1
        if flag:
            best_true = max(best_true, run)
        else:
            best_false = max(best_false, run)
    return best_true, best_false


def _fill_trade_stats(metrics: Metrics, trades: list[ClosedTrade], equity: pd.Series) -> None:
    """Trade-level statistics. Valid regardless of whether the account survived."""
    if not trades:
        return

    nets = [float(t.net_pnl) for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]

    metrics.total_trades = len(trades)
    metrics.winners = len(wins)
    metrics.losers = len(losses)
    metrics.win_rate_pct = len(wins) / len(trades) * 100.0
    metrics.avg_win = sum(wins) / len(wins) if wins else 0.0
    metrics.avg_loss = sum(losses) / len(losses) if losses else 0.0
    metrics.largest_win = max(wins) if wins else 0.0
    metrics.largest_loss = min(losses) if losses else 0.0
    metrics.expectancy = sum(nets) / len(nets)
    metrics.avg_bars_held = sum(t.bars_held for t in trades) / len(trades)

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    metrics.profit_factor = (
        gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit else 0.0)
    )

    metrics.max_consecutive_wins, metrics.max_consecutive_losses = _consecutive(
        [t.is_win for t in trades]
    )

    metrics.gross_pnl = float(sum(t.gross_pnl for t in trades))
    metrics.total_fees = float(sum(t.fees for t in trades))
    metrics.net_pnl = metrics.gross_pnl - metrics.total_fees
    if metrics.gross_pnl > 0:
        metrics.cost_drag_pct = metrics.total_fees / metrics.gross_pnl * 100.0
    elif metrics.total_fees > 0:
        metrics.cost_drag_pct = float("inf")

    total_bars_in_trade = sum(t.bars_held for t in trades)
    metrics.exposure_pct = min(100.0, total_bars_in_trade / len(equity) * 100.0)


def compute_metrics(
    equity_curve: list[tuple[datetime, Decimal]],
    trades: list[ClosedTrade],
    starting_capital: Decimal,
    timeframe: str = "5m",
    risk_free_rate: float = RISK_FREE_RATE,
) -> Metrics:
    """Build the full metrics panel from an equity curve and a trade list."""
    metrics = Metrics(starting_capital=float(starting_capital))

    if not equity_curve:
        return metrics

    index = pd.DatetimeIndex([ts for ts, _ in equity_curve])
    equity = pd.Series([float(v) for _, v in equity_curve], index=index)

    metrics.bars = len(equity)
    metrics.period_start = index[0].to_pydatetime()
    metrics.period_end = index[-1].to_pydatetime()
    metrics.trading_days = len(set(index.date))
    metrics.ending_equity = float(equity.iloc[-1])

    start = float(starting_capital)
    if start > 0:
        metrics.total_return_pct = (metrics.ending_equity - start) / start * 100.0

    # --- CAGR -------------------------------------------------------------
    years = max((index[-1] - index[0]).days / 365.25, 1e-9)
    if start > 0 and metrics.ending_equity > 0 and years > 0:
        metrics.cagr_pct = ((metrics.ending_equity / start) ** (1 / years) - 1) * 100.0
    elif metrics.ending_equity <= 0:
        metrics.cagr_pct = -100.0

    # --- account destruction check -----------------------------------------
    # Do this before any ratio is computed. Once equity crosses zero the series
    # stops being a return stream, and Sharpe/Sortino/drawdown computed over it
    # are not merely imprecise, they are meaningless in a way that reads as
    # success — a strategy that lost six times the account showed Sharpe 1.19.
    blown = equity <= 0
    if blown.any():
        first = blown.idxmax()
        metrics.account_blown = True
        metrics.blown_at = first.to_pydatetime()
        metrics.max_drawdown_pct = 100.0
        metrics.max_drawdown_value = start
        metrics.total_return_pct = max(metrics.total_return_pct, -100.0)
        metrics.cagr_pct = -100.0
        metrics.sharpe = metrics.sortino = metrics.calmar = float("nan")
        metrics.volatility_pct = float("nan")
        _fill_trade_stats(metrics, trades, equity)
        return metrics

    # --- risk-adjusted -----------------------------------------------------
    periods = bars_per_year(timeframe)
    returns = equity.pct_change().dropna()

    if len(returns) > 1 and returns.std(ddof=1) > 0:
        mean_r, std_r = returns.mean(), returns.std(ddof=1)
        rf_per_bar = risk_free_rate / periods
        metrics.volatility_pct = float(std_r * math.sqrt(periods) * 100.0)
        metrics.sharpe = float((mean_r - rf_per_bar) / std_r * math.sqrt(periods))

        downside = returns[returns < 0]
        if len(downside) > 1 and downside.std(ddof=1) > 0:
            metrics.sortino = float(
                (mean_r - rf_per_bar) / downside.std(ddof=1) * math.sqrt(periods)
            )

    # --- drawdown ---------------------------------------------------------
    dd = compute_drawdown(equity)
    metrics.max_drawdown_pct = dd.max_drawdown * 100.0
    metrics.max_drawdown_value = dd.max_drawdown_value
    metrics.max_drawdown_days = dd.longest_days
    if dd.max_drawdown > 0:
        metrics.calmar = metrics.cagr_pct / (dd.max_drawdown * 100.0)

    _fill_trade_stats(metrics, trades, equity)

    return metrics


# ---------------------------------------------------------------------------
# Out-of-sample validation
# ---------------------------------------------------------------------------

@dataclass
class SplitResult:
    """In-sample vs out-of-sample, shown together so neither can be cherry-picked."""

    in_sample: Metrics
    out_of_sample: Metrics
    split_date: datetime

    @property
    def degradation_pct(self) -> float:
        """How much worse OOS is than IS, on a duration-normalized basis.

        Compares CAGR, not total return. The two windows are different lengths by
        construction — a 70/30 split gives the out-of-sample window less than half
        the time to accumulate return — so comparing raw totals would label every
        strategy "degraded" regardless of merit.
        """
        baseline = self.in_sample.cagr_pct
        if baseline == 0:
            return 0.0
        return (self.out_of_sample.cagr_pct - baseline) / abs(baseline) * 100.0

    @property
    def verdict(self) -> str:
        if self.in_sample.total_trades < 10 or self.out_of_sample.total_trades < 10:
            return "inconclusive — too few trades in one or both windows"
        if self.out_of_sample.total_return_pct <= 0 < self.in_sample.total_return_pct:
            return "FAILED — profitable in sample, unprofitable out of sample"
        if self.degradation_pct < -60:
            return "weak — out-of-sample performance degraded sharply"
        if self.out_of_sample.total_return_pct > 0:
            return "held up out of sample"
        return "unprofitable in both windows"


def split_metrics(
    equity_curve: list[tuple[datetime, Decimal]],
    trades: list[ClosedTrade],
    starting_capital: Decimal,
    timeframe: str = "5m",
    in_sample_fraction: float = 0.7,
) -> SplitResult:
    """Split chronologically and compute both halves.

    Chronological, never random: shuffling time series data lets information from
    the future leak into the training window and makes every strategy look good.
    """
    if not equity_curve:
        raise ValueError("Cannot split an empty equity curve")

    cut = int(len(equity_curve) * in_sample_fraction)
    cut = max(1, min(cut, len(equity_curve) - 1))
    split_date = equity_curve[cut][0]

    is_curve = equity_curve[:cut]
    oos_curve = equity_curve[cut:]

    is_trades = [t for t in trades if t.exit_time <= split_date]
    oos_trades = [t for t in trades if t.exit_time > split_date]

    oos_start = oos_curve[0][1] if oos_curve else starting_capital

    return SplitResult(
        in_sample=compute_metrics(is_curve, is_trades, starting_capital, timeframe),
        out_of_sample=compute_metrics(oos_curve, oos_trades, oos_start, timeframe),
        split_date=split_date,
    )


def trades_to_frame(trades: list[ClosedTrade]) -> pd.DataFrame:
    """Trade log as a DataFrame, for the UI table and CSV export."""
    if not trades:
        return pd.DataFrame(
            columns=[
                "entry_time", "exit_time", "symbol", "direction", "quantity",
                "entry_price", "exit_price", "gross_pnl", "fees", "net_pnl",
                "return_pct", "reason", "bars_held",
            ]
        )
    return pd.DataFrame([t.as_dict() for t in trades])
