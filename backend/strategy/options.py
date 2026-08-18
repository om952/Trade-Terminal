"""Option leg resolution — turning "ATM+2 CE, current weekly" into a tradeable contract.

Two jobs:

  1. **Strike and expiry selection.** A strategy names a contract relatively
     ("ATM", "OTM+2", "DELTA:0.30") because the absolute strike depends on where
     the underlying is when the signal fires. This resolves that at entry time.

  2. **Producing a premium series.** A backtest needs the option's price on every
     bar, not just at entry. Where real historical premiums exist we use them;
     where they do not — which is most of the time for expired Indian contracts —
     we price Black-Scholes off the underlying.

The synthetic path is clearly labelled everywhere it is used. Black-Scholes on a
GBM underlying has no bid-ask spread, no volatility smile and no liquidity holes,
so a strategy that harvests that smoothness will look excellent here and lose
money live. Treat synthetic option results as a test of strategy *logic*, never
as evidence of edge.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from backend.core.config import IST
from backend.data.calendar import is_trading_day
from backend.data.simulator import black_scholes, build_option_series
from backend.strategy.schema import OptionSpec, OptionRight

log = logging.getLogger(__name__)

# Strike intervals for Indian index options.
STRIKE_STEP = {
    "NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50,
    "MIDCPNIFTY": 25, "SENSEX": 100, "BANKEX": 100,
}
DEFAULT_STRIKE_STEP = 50

# Weekly expiry weekday by index. NSE moved NIFTY weeklies to Tuesday and
# discontinued BANKNIFTY weeklies entirely — BANKNIFTY resolves to monthly.
WEEKLY_EXPIRY_WEEKDAY = {"NIFTY": 1, "SENSEX": 1, "FINNIFTY": 1, "MIDCPNIFTY": 1}
NO_WEEKLY = {"BANKNIFTY", "BANKEX"}


@dataclass(frozen=True, slots=True)
class ResolvedOption:
    """A concrete contract, ready to trade."""

    underlying: str
    right: OptionRight
    strike: float
    expiry: date
    instrument_key: str | None = None   # None when synthetic
    synthetic: bool = True

    @property
    def label(self) -> str:
        return f"{self.underlying} {self.strike:g} {self.right.value} {self.expiry:%d%b%y}".upper()


def strike_step(underlying: str) -> int:
    """Strike interval for an underlying.

    Matches the LONGEST name first: "NIFTY" is a substring of "BANKNIFTY", so
    naive iteration hands BANKNIFTY the 50-point NIFTY step and resolves strikes
    that do not exist on the exchange.
    """
    key = underlying.upper().replace(" ", "")
    for name in sorted(STRIKE_STEP, key=len, reverse=True):
        if name in key:
            return STRIKE_STEP[name]
    return DEFAULT_STRIKE_STEP


def atm_strike(spot: float, underlying: str) -> float:
    """Nearest tradeable strike to spot."""
    step = strike_step(underlying)
    return float(round(spot / step) * step)


def resolve_strike(rule: str, spot: float, underlying: str, right: OptionRight,
                   expiry: date | None = None, volatility: float = 0.15) -> float:
    """Turn a strike rule into an absolute strike.

    Rules:
        ATM, ATM+2, ATM-1   steps from the money
        OTM+2, ITM+1        steps away from / toward the money, direction-aware
        STRIKE:24800        absolute
        DELTA:0.30          nearest strike to that delta (needs an expiry)
    """
    rule = rule.strip().upper()
    step = strike_step(underlying)
    atm = atm_strike(spot, underlying)

    if rule.startswith("STRIKE:"):
        return float(rule.split(":", 1)[1])

    if rule.startswith("DELTA:"):
        target = abs(float(rule.split(":", 1)[1]))
        if expiry is None:
            raise ValueError("DELTA strike rules need an expiry to price against")
        return _strike_for_delta(target, spot, underlying, right, expiry, volatility)

    match = re.match(r"^(ATM|ITM|OTM)([+-]\d+)?$", rule)
    if not match:
        raise ValueError(f"Unrecognized strike rule {rule!r}")

    moneyness, offset_text = match.groups()
    steps = int(offset_text) if offset_text else 0

    if moneyness == "ATM":
        return atm + steps * step

    # OTM/ITM are direction-aware: for a call, OTM is above spot; for a put,
    # below. Writing "OTM+2" and silently getting an ITM put is the kind of
    # error that only shows up as an inexplicable loss.
    magnitude = abs(steps) if steps else 1
    is_call = right is OptionRight.CE

    if moneyness == "OTM":
        direction = 1 if is_call else -1
    else:  # ITM
        direction = -1 if is_call else 1

    return atm + direction * magnitude * step


def _strike_for_delta(
    target: float, spot: float, underlying: str, right: OptionRight,
    expiry: date, volatility: float,
) -> float:
    """Scan strikes for the one whose delta is closest to the target."""
    step = strike_step(underlying)
    atm = atm_strike(spot, underlying)
    tte = max((expiry - datetime.now(IST).date()).days, 1) / 365.0

    best_strike, best_error = atm, float("inf")
    for offset in range(-30, 31):
        strike = atm + offset * step
        if strike <= 0:
            continue
        greeks = black_scholes(spot, strike, tte, volatility, right=right.value)
        error = abs(abs(greeks.delta) - target)
        if error < best_error:
            best_strike, best_error = strike, error
    return best_strike


# ---------------------------------------------------------------------------
# Expiry selection
# ---------------------------------------------------------------------------

def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def _roll_to_trading_day(day: date) -> date:
    """Exchanges move an expiry that lands on a holiday to the prior session."""
    guard = 0
    while not is_trading_day(day) and guard < 10:
        day -= timedelta(days=1)
        guard += 1
    return day


def resolve_expiry(rule: str, as_of: date, underlying: str) -> date:
    """Resolve current/next weekly/monthly to a concrete expiry date.

    BANKNIFTY has no weeklies since the NSE rationalization, so a weekly rule on
    it resolves to the monthly expiry rather than inventing a contract that does
    not exist.
    """
    key = underlying.upper().replace(" ", "")
    has_weekly = not any(name in key for name in NO_WEEKLY)

    weekday = 1  # Tuesday
    for name, wd in WEEKLY_EXPIRY_WEEKDAY.items():
        if name in key:
            weekday = wd
            break

    if rule in ("current_weekly", "next_weekly") and not has_weekly:
        log.debug("%s has no weekly expiry; using monthly for rule %s", underlying, rule)
        rule = "current_monthly" if rule == "current_weekly" else "next_monthly"

    if rule in ("current_weekly", "next_weekly"):
        days_ahead = (weekday - as_of.weekday()) % 7
        expiry = as_of + timedelta(days=days_ahead)
        if rule == "next_weekly" or expiry < as_of:
            expiry += timedelta(days=7)
        return _roll_to_trading_day(expiry)

    if rule in ("current_monthly", "next_monthly"):
        expiry = _roll_to_trading_day(_last_weekday_of_month(as_of.year, as_of.month, weekday))
        if rule == "next_monthly" or expiry < as_of:
            year, month = (as_of.year + 1, 1) if as_of.month == 12 else (as_of.year, as_of.month + 1)
            expiry = _roll_to_trading_day(_last_weekday_of_month(year, month, weekday))
        return expiry

    raise ValueError(f"Unknown expiry rule {rule!r}")


def resolve_option(
    spec: OptionSpec, spot: float, as_of: date, underlying: str, volatility: float = 0.15
) -> ResolvedOption:
    """Resolve a leg's option spec to a concrete contract."""
    expiry = resolve_expiry(spec.expiry, as_of, underlying)
    strike = resolve_strike(spec.strike_rule, spot, underlying, spec.right, expiry, volatility)
    return ResolvedOption(
        underlying=underlying, right=spec.right, strike=strike,
        expiry=expiry, synthetic=True,
    )


# ---------------------------------------------------------------------------
# Premium series
# ---------------------------------------------------------------------------

def synthetic_premium_series(
    underlying_df: pd.DataFrame,
    option: ResolvedOption,
    volatility: float = 0.15,
    rate: float = 0.065,
) -> pd.DataFrame:
    """Black-Scholes premiums across the underlying's bars.

    Returns the same OHLCV shape as a real candle series so the engine cannot
    tell the difference — which is the point, and also the risk. Anything
    produced here is synthetic and must be labelled as such in results.
    """
    series = build_option_series(
        underlying_df, option.strike, option.expiry,
        right=option.right.value, volatility=volatility, rate=rate,
    )
    series.attrs["synthetic"] = True
    series.attrs["contract"] = option.label
    return series


def realized_volatility(df: pd.DataFrame, window: int = 20, bars_per_year: int = 252 * 75) -> float:
    """Estimate volatility from the underlying, for pricing synthetic options.

    Using a single fixed IV for a whole backtest would price options identically
    in calm and violent regimes, which flatters every short-premium strategy
    exactly when it should be losing.
    """
    import numpy as np

    closes = df["close"].astype("float64")
    log_returns = np.log(closes / closes.shift(1)).dropna()
    if len(log_returns) < window:
        return 0.15
    vol = float(log_returns.tail(window * 10).std() * np.sqrt(bars_per_year))
    return min(max(vol, 0.05), 1.5)  # clamp to a sane band
