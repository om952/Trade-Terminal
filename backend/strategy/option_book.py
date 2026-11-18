"""OptionBook — the missing link between an option leg and a price.

Until this existed, a leg declaring `kind: option` still filled at the
underlying's price. A "sell BANKNIFTY ATM PE" was booked as shorting 55,000
points of index rather than collecting ~300 of premium, so every option backtest
reported P&L roughly two orders of magnitude too large and pointing the wrong way
on the wrong risk. The strike/expiry resolver in `options.py` was already written
and tested; nothing called it.

What this does:

  * At entry, resolves the leg's spec ("ATM PE, current monthly") against the
    spot at that instant into a concrete contract.
  * For every bar the position is open, produces that contract's OHLC so stops,
    targets and trailing exits are evaluated in premium terms — which is the only
    place they mean anything. A 30% stop on a short option is 30% of the premium,
    not 30% of the index.
  * Releases the contract when the position closes, so the next entry resolves a
    fresh strike against wherever the market has moved to.

Premiums are Black-Scholes off the underlying, priced with the underlying's own
realized volatility rather than a fixed IV. That last part matters: a single
constant IV prices options identically in calm and violent regimes, which
flatters every short-premium strategy in exactly the conditions that should be
destroying it.

This is still synthetic. There is no bid-ask spread, no volatility smile, no
liquidity hole, and no gap risk beyond what the underlying printed. Short-premium
strategies harvest precisely those frictions' absence, so treat results here as a
test of the strategy's *logic*, never as evidence of edge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from backend.core.config import IST
from backend.data.simulator import black_scholes
from backend.indicators.base import PriceBar
from backend.strategy.options import ResolvedOption, resolve_option
from backend.strategy.schema import Leg, LegKind, OptionRight, Strategy

log = logging.getLogger(__name__)

MARKET_CLOSE_HOUR, MARKET_CLOSE_MINUTE = 15, 30

# Premium below which a contract is effectively untradeable. Black-Scholes will
# happily quote 0.03 for a far OTM option; the exchange will not fill it, and a
# backtest that "sells" it books premium nobody would have received.
MIN_TRADEABLE_PREMIUM = 0.05


@dataclass
class _Active:
    option: ResolvedOption
    opened_index: int


class OptionBook:
    """Resolves and prices option contracts for one backtest run."""

    def __init__(
        self,
        strategy: Strategy,
        df: pd.DataFrame,
        volatility: float | None = None,
        rate: float = 0.065,
        iv_premium: float = 0.0,
    ) -> None:
        self.strategy = strategy
        self.df = df.reset_index(drop=True)
        self.rate = rate
        self.underlying = _underlying_name(strategy.instrument.symbol)

        self._timestamps = pd.to_datetime(self.df["timestamp"], utc=True).dt.tz_convert(IST)
        # A rolling estimate, not one number for the whole run. A single vol
        # prices options identically in a calm October and a violent March, which
        # flatters every short-premium strategy in exactly the conditions that
        # should be destroying it — and understates what a long option costs in
        # the panic where it finally pays.
        self._vol_series = (
            None if volatility is not None else _rolling_volatility(self.df, self._timestamps)
        )
        self._fixed_vol = volatility
        # Options do not trade at realized volatility. Indian index options carry
        # a persistent volatility risk premium — implied runs a few points above
        # subsequently-realized, which is the compensation option sellers earn for
        # bearing gap risk. Pricing at pure RV therefore makes every long option
        # cheaper than it really was and every short option collect less than it
        # really would, biasing any buy-versus-sell comparison toward buying.
        # Expressed in vol points: 0.04 means "IV = RV + 4 points".
        self.iv_premium = iv_premium
        self._active: dict[str, _Active] = {}
        self.contracts: list[dict] = []      # every contract traded, for the report

    # -- lifecycle -----------------------------------------------------------

    @property
    def option_legs(self) -> list[Leg]:
        return [leg for leg in self.strategy.legs if leg.kind is LegKind.OPTION]

    @property
    def has_options(self) -> bool:
        return bool(self.option_legs)

    def open(self, leg: Leg, index: int) -> ResolvedOption:
        """Resolve the contract this leg is about to trade."""
        spot = float(self.df["close"].iloc[index])
        as_of = self._timestamps.iloc[index].date()
        option = resolve_option(
            leg.option, spot, as_of, self.underlying, volatility=self.vol_at(index)
        )
        self._active[leg.id] = _Active(option=option, opened_index=index)
        self.contracts.append(
            {
                "leg_id": leg.id,
                "contract": option.label,
                "strike": option.strike,
                "right": option.right.value,
                "expiry": option.expiry.isoformat(),
                "spot_at_entry": round(spot, 2),
                "vol_pct": round(self.vol_at(index) * 100, 2),
                "entered_at": self._timestamps.iloc[index].isoformat(),
            }
        )
        return option

    def release(self, leg_id: str) -> None:
        self._active.pop(leg_id, None)

    def active(self, leg_id: str) -> ResolvedOption | None:
        entry = self._active.get(leg_id)
        return entry.option if entry else None

    # -- pricing -------------------------------------------------------------

    def bar(self, leg_id: str, index: int) -> PriceBar | None:
        """The active contract's OHLC on this bar, or None if nothing is open."""
        entry = self._active.get(leg_id)
        if entry is None:
            return None
        return self.price_bar(entry.option, index)

    def price_bar(self, option: ResolvedOption, index: int) -> PriceBar:
        """Price one contract across one underlying bar.

        A call's high comes from the underlying's high; a put's from its low.
        Getting that inversion wrong makes every put's stop trigger on the move
        that should have been profitable — silently, because the numbers still
        look like prices.
        """
        row = self.df.iloc[index]
        tte = self._tte(option.expiry, index)
        vol = self.vol_at(index)
        is_call = option.right is OptionRight.CE

        o = self._price(float(row["open"]), option, tte, vol)
        c = self._price(float(row["close"]), option, tte, vol)
        a = self._price(float(row["high"]), option, tte, vol)
        b = self._price(float(row["low"]), option, tte, vol)
        hi, lo = (a, b) if is_call else (b, a)

        if tte > 0:
            # Before expiry nothing changes hands below the minimum tick, however
            # far out of the money Black-Scholes says it is. Quoting 0.02 lets a
            # backtest close a short for free, which is the single most flattering
            # error available to an option-selling strategy.
            floor = MIN_TRADEABLE_PREMIUM
            o, c, hi, lo = (max(x, floor) for x in (o, c, hi, lo))
        # At expiry the option settles at intrinsic, and zero is the correct
        # answer for one that finished out of the money.

        return PriceBar(
            open=o,
            high=max(hi, o, c),
            low=min(lo, o, c),
            close=c,
            volume=float(row["volume"]) / 20.0,   # options trade thinner
        )

    def vol_at(self, index: int) -> float:
        base = self._fixed_vol if self._fixed_vol is not None else float(self._vol_series[index])
        return max(0.01, base + self.iv_premium)

    def _price(self, spot: float, option: ResolvedOption, tte: float, vol: float) -> float:
        return round(
            black_scholes(spot, option.strike, tte, vol, self.rate, option.right.value).price,
            2,
        )

    def _tte(self, expiry: date, index: int) -> float:
        expiry_at = pd.Timestamp(
            datetime.combine(
                expiry, datetime.min.time().replace(hour=MARKET_CLOSE_HOUR,
                                                    minute=MARKET_CLOSE_MINUTE)
            ),
            tz=IST,
        )
        seconds = (expiry_at - self._timestamps.iloc[index]).total_seconds()
        return max(0.0, seconds / (365.25 * 24 * 3600))

    # -- reporting -----------------------------------------------------------

    def summary(self) -> dict:
        if not self.contracts:
            return {}
        strikes = [c["strike"] for c in self.contracts]
        vols = [c["vol_pct"] for c in self.contracts]
        return {
            "synthetic": True,
            "pricing": "black_scholes",
            "volatility_mode": "fixed" if self._fixed_vol is not None else "rolling_realized",
            "volatility_pct": {
                "min": round(min(vols), 2),
                "median": round(sorted(vols)[len(vols) // 2], 2),
                "max": round(max(vols), 2),
            },
            "rate_pct": round(self.rate * 100, 2),
            "iv_premium_pts": round(self.iv_premium * 100, 2),
            "contracts_traded": len(self.contracts),
            "distinct_strikes": len(set(strikes)),
            "rights": sorted({c["right"] for c in self.contracts}),
            "expiries": sorted({c["expiry"] for c in self.contracts}),
        }


def _underlying_name(symbol: str) -> str:
    """'NSE_INDEX|Nifty Bank' -> 'BANKNIFTY', so strike steps resolve correctly."""
    tail = symbol.split("|")[-1].upper().replace(" ", "")
    aliases = {
        "NIFTYBANK": "BANKNIFTY",
        "NIFTY50": "NIFTY",
        "NIFTYFINSERVICE": "FINNIFTY",
        "NIFTYMIDCAPSELECT": "MIDCPNIFTY",
    }
    return aliases.get(tail, tail)


def _rolling_volatility(
    df: pd.DataFrame, timestamps: pd.Series, days: int = 20
) -> "pd.Series":
    """Annualized realized volatility over a trailing window, per bar.

    Bars-per-day is measured from the data rather than assumed, so this is
    correct for 1m, 5m, 15m or daily bars without being told which it is getting.
    Guessing 75 bars a day and being handed daily candles would annualize by a
    factor of nearly nine.
    """
    import numpy as np

    per_day = max(1, int(timestamps.dt.date.value_counts().median()))
    window = max(20, days * per_day)
    bars_per_year = per_day * 252

    closes = df["close"].astype("float64")
    log_returns = np.log(closes / closes.shift(1))
    vol = log_returns.rolling(window, min_periods=max(20, window // 5)).std() * np.sqrt(
        bars_per_year
    )
    # Early bars have no window yet; back-fill from the first real estimate
    # rather than leaving NaN, which would price every option at zero.
    return vol.bfill().fillna(0.15).clip(0.05, 1.5).to_numpy()
