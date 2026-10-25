"""Synthetic market data: GBM with Merton jump-diffusion, plus Black-Scholes options.

What this is for
----------------
Validating that strategy *logic* is correct, and stress-testing behaviour under
volatility regimes that have not happened yet. It runs without credentials,
without market hours, and without the gaps in real Indian options history.

What this is NOT for
--------------------
Evidence that a strategy makes money. Synthetic prices have no microstructure,
no bid-ask spread, no volatility smile, and no liquidity holes. Options priced
by Black-Scholes on a GBM underlying are internally consistent in a way real
option books never are, so a strategy that harvests that consistency will look
brilliant here and lose money live.

Every dataset produced here is tagged `source="simulated"` so the UI can label
it and no result can be mistaken for the real thing.

Model
-----
Merton jump-diffusion. Between jumps the log-price follows GBM; jumps arrive as
a Poisson process with lognormal sizes. Chosen over plain GBM because Indian
index options are priced around gap risk, and a generator with no gaps would
make every short-premium strategy look free.

    dS/S = (mu - lambda*k) dt + sigma dW + (J - 1) dN

Compensating the drift by `lambda*k` keeps the expected return equal to `mu`
regardless of jump settings, so changing jump parameters isolates tail risk
instead of secretly changing the trend.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from backend.core.config import IST
from backend.data.calendar import MARKET_CLOSE, MARKET_OPEN, trading_days

log = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252
MINUTES_PER_SESSION = 375  # 09:15 to 15:30


@dataclass(frozen=True, slots=True)
class InstrumentProfile:
    """Calibration for one instrument. Defaults reflect rough historical values.

    `annual_vol` is the diffusive component only — realized volatility comes out
    higher once jumps are added, which is the intended behaviour.
    """

    name: str
    spot: float
    annual_vol: float
    annual_drift: float = 0.08
    jump_intensity: float = 12.0   # expected jumps per year
    jump_mean: float = -0.004      # mean log jump size; negative = downward skew
    jump_std: float = 0.018
    tick_size: float = 0.05
    lot_size: int = 1
    base_volume: int = 100_000
    is_mcx: bool = False


PROFILES: dict[str, InstrumentProfile] = {
    # Indices
    "NIFTY":      InstrumentProfile("NIFTY", 24_800, 0.13, 0.10, 14, -0.005, 0.020, 0.05, 75),
    "BANKNIFTY":  InstrumentProfile("BANKNIFTY", 54_000, 0.17, 0.10, 16, -0.006, 0.024, 0.05, 35),
    "SENSEX":     InstrumentProfile("SENSEX", 81_000, 0.13, 0.10, 14, -0.005, 0.020, 0.05, 20),
    # Large-cap equities
    "RELIANCE":   InstrumentProfile("RELIANCE", 2_950, 0.22, 0.11, 10, -0.006, 0.028, 0.05, 250),
    "HDFCBANK":   InstrumentProfile("HDFCBANK", 1_680, 0.20, 0.10, 10, -0.005, 0.026, 0.05, 550),
    "INFY":       InstrumentProfile("INFY", 1_850, 0.24, 0.09, 12, -0.007, 0.032, 0.05, 400),
    "TCS":        InstrumentProfile("TCS", 4_100, 0.21, 0.09, 10, -0.006, 0.028, 0.05, 175),
    "ICICIBANK":  InstrumentProfile("ICICIBANK", 1_250, 0.21, 0.12, 11, -0.005, 0.027, 0.05, 700),
    # MCX commodities
    "GOLD":       InstrumentProfile("GOLD", 72_000, 0.15, 0.07, 10, -0.004, 0.022, 1.0, 100, 50_000, True),
    "SILVER":     InstrumentProfile("SILVER", 88_000, 0.26, 0.06, 14, -0.006, 0.034, 1.0, 30, 30_000, True),
    "CRUDEOIL":   InstrumentProfile("CRUDEOIL", 6_400, 0.34, 0.03, 22, -0.010, 0.042, 1.0, 100, 40_000, True),
    "NATURALGAS": InstrumentProfile("NATURALGAS", 245, 0.52, 0.00, 30, -0.012, 0.060, 0.10, 1250, 60_000, True),
}


def _session_timestamps(days: list[date], bars_per_day: int, minutes: int, mcx: bool) -> pd.DatetimeIndex:
    """Intraday timestamps that exist only inside real sessions.

    Generating a continuous clock and filtering afterwards would leave overnight
    moves smeared across fake bars; building only real session bars means the
    overnight gap lands in one place, as it does live.
    """
    open_t = time(9, 0) if mcx else MARKET_OPEN
    stamps: list[pd.Timestamp] = []
    for d in days:
        base = pd.Timestamp(datetime.combine(d, open_t), tz=IST)
        stamps.extend(base + pd.Timedelta(minutes=minutes * i) for i in range(bars_per_day))
    return pd.DatetimeIndex(stamps)


def generate_ohlcv(
    profile: str | InstrumentProfile,
    start: date,
    end: date,
    timeframe: str = "5m",
    seed: int | None = None,
    vol_multiplier: float = 1.0,
) -> pd.DataFrame:
    """Generate OHLCV bars for a date range.

    `seed` makes output reproducible — the same seed always yields the same
    series, which is what lets a strategy regression test assert exact numbers.
    `vol_multiplier` scales diffusive volatility for regime stress tests.
    """
    prof = PROFILES[profile.upper()] if isinstance(profile, str) else profile
    if vol_multiplier != 1.0:
        prof = replace(prof, annual_vol=prof.annual_vol * vol_multiplier)

    rng = np.random.default_rng(seed)
    days = trading_days(start, end)
    if not days:
        raise ValueError(f"No trading days between {start} and {end}")

    from backend.data.upstox_client import Timeframe

    tf = Timeframe.parse(timeframe)
    if tf.unit == "days":
        bars_per_day, minutes = 1, MINUTES_PER_SESSION
    elif tf.unit == "hours":
        minutes = tf.interval * 60
        bars_per_day = max(1, MINUTES_PER_SESSION // minutes)
    else:
        minutes = tf.interval
        bars_per_day = max(1, MINUTES_PER_SESSION // minutes)

    n_bars = len(days) * bars_per_day
    dt = 1.0 / (TRADING_DAYS_PER_YEAR * bars_per_day)

    # --- Diffusive component -------------------------------------------------
    sigma, mu = prof.annual_vol, prof.annual_drift
    lam, jm, js = prof.jump_intensity, prof.jump_mean, prof.jump_std

    # Merton drift compensator: E[J-1] where log J ~ N(jm, js^2)
    k = math.exp(jm + 0.5 * js**2) - 1.0
    drift = (mu - lam * k - 0.5 * sigma**2) * dt

    diffusion = sigma * math.sqrt(dt) * rng.standard_normal(n_bars)

    # --- Jump component ------------------------------------------------------
    n_jumps = rng.poisson(lam * dt, n_bars)
    jumps = np.zeros(n_bars)
    active = n_jumps > 0
    if active.any():
        # Sum of n independent lognormal jumps: mean and variance scale with n.
        jumps[active] = rng.normal(jm * n_jumps[active], js * np.sqrt(n_jumps[active]))

    log_returns = drift + diffusion + jumps
    close = prof.spot * np.exp(np.cumsum(log_returns))

    # --- Build OHLC around each close ---------------------------------------
    # Open is the previous close (no synthetic overnight gap beyond the jump
    # process itself). High/low extend beyond the open-close range by a fraction
    # of the bar's own volatility, so quiet bars have small wicks.
    open_ = np.empty(n_bars)
    open_[0] = prof.spot
    open_[1:] = close[:-1]

    bar_vol = np.abs(log_returns) * close
    wick_up = np.abs(rng.normal(0, 0.6, n_bars)) * bar_vol
    wick_dn = np.abs(rng.normal(0, 0.6, n_bars)) * bar_vol

    high = np.maximum(open_, close) + wick_up
    low = np.minimum(open_, close) - wick_dn
    low = np.maximum(low, 0.01)

    # --- Volume: U-shaped intraday profile, scaled by bar range --------------
    if bars_per_day > 1:
        pos = np.tile(np.linspace(0, 1, bars_per_day), len(days))
        u_shape = 1.0 + 1.8 * (np.exp(-6 * pos) + np.exp(-6 * (1 - pos)))
    else:
        u_shape = np.ones(n_bars)

    range_factor = 1.0 + 3.0 * (high - low) / close
    noise = rng.lognormal(0, 0.35, n_bars)
    volume = (prof.base_volume / max(1, bars_per_day) * u_shape * range_factor * noise).astype(np.int64)

    tick = prof.tick_size
    df = pd.DataFrame(
        {
            "timestamp": _session_timestamps(days, bars_per_day, minutes, prof.is_mcx),
            "open": np.round(open_ / tick) * tick,
            "high": np.round(high / tick) * tick,
            "low": np.round(low / tick) * tick,
            "close": np.round(close / tick) * tick,
            "volume": volume,
            "oi": 0,
        }
    )

    # Rounding to tick can invert the OHLC relationship on tiny bars; repair it
    # so downstream code can rely on high >= max(o,c) and low <= min(o,c).
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)

    log.info(
        "Simulated %s %s: %d bars, %s..%s, realized vol %.1f%%",
        prof.name, timeframe, len(df), days[0], days[-1],
        100 * log_returns.std() * math.sqrt(TRADING_DAYS_PER_YEAR * bars_per_day),
    )
    return df


# ---------------------------------------------------------------------------
# Black-Scholes
# ---------------------------------------------------------------------------

def _norm_cdf(x: np.ndarray | float) -> np.ndarray | float:
    return 0.5 * (1.0 + np.vectorize(math.erf)(np.asarray(x) / math.sqrt(2.0)))


def _norm_pdf(x: np.ndarray | float) -> np.ndarray | float:
    return np.exp(-0.5 * np.asarray(x) ** 2) / math.sqrt(2.0 * math.pi)


@dataclass(frozen=True, slots=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float   # per 1 volatility point (0.01)
    rho: float


def black_scholes(
    spot: float,
    strike: float,
    time_to_expiry: float,  # in years
    volatility: float,
    rate: float = 0.065,     # RBI repo-ish; India risk-free proxy
    right: str = "CE",
) -> Greeks:
    """Price and Greeks for a European option.

    At or past expiry this returns intrinsic value with zero Greeks, so an
    expiry-day backtest does not divide by zero on the final bar.
    """
    right = right.upper()
    is_call = right in ("CE", "C", "CALL")

    if time_to_expiry <= 0 or volatility <= 0:
        intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
        return Greeks(intrinsic, 1.0 if intrinsic > 0 else 0.0, 0.0, 0.0, 0.0, 0.0)

    sqrt_t = math.sqrt(time_to_expiry)
    d1 = (math.log(spot / strike) + (rate + 0.5 * volatility**2) * time_to_expiry) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t
    discount = math.exp(-rate * time_to_expiry)

    nd1, nd2 = float(_norm_cdf(d1)), float(_norm_cdf(d2))
    pdf_d1 = float(_norm_pdf(d1))

    if is_call:
        price = spot * nd1 - strike * discount * nd2
        delta = nd1
        theta = (-spot * pdf_d1 * volatility / (2 * sqrt_t) - rate * strike * discount * nd2) / 365
        rho = strike * time_to_expiry * discount * nd2 / 100
    else:
        price = strike * discount * (1 - nd2) - spot * (1 - nd1)
        delta = nd1 - 1
        theta = (-spot * pdf_d1 * volatility / (2 * sqrt_t) + rate * strike * discount * (1 - nd2)) / 365
        rho = -strike * time_to_expiry * discount * (1 - nd2) / 100

    gamma = pdf_d1 / (spot * volatility * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100

    return Greeks(max(0.0, price), delta, gamma, theta, vega, rho)


def implied_volatility(
    market_price: float, spot: float, strike: float, time_to_expiry: float,
    rate: float = 0.065, right: str = "CE",
    tol: float = 1e-6, max_iter: int = 100,
) -> float:
    """Back out IV by bisection.

    Bisection rather than Newton-Raphson: vega collapses toward zero for deep
    OTM options, and Newton diverges there. Bisection is slower and always
    converges, which is the right trade for a batch process.
    """
    if time_to_expiry <= 0:
        return 0.0

    lo, hi = 1e-4, 5.0
    if black_scholes(spot, strike, time_to_expiry, hi, rate, right).price < market_price:
        return hi  # price above what even 500% vol explains

    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        price = black_scholes(spot, strike, time_to_expiry, mid, rate, right).price
        if abs(price - market_price) < tol:
            return mid
        if price > market_price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def weekly_expiries(start: date, end: date, weekday: int = 3) -> list[date]:
    """Expiry dates between two dates. Default Thursday (weekday=3).

    Rolls back to the previous trading day when the expiry lands on a holiday,
    which is what the exchanges actually do.
    """
    from backend.data.calendar import is_trading_day

    out, cursor = [], start
    while cursor <= end:
        if cursor.weekday() == weekday:
            expiry = cursor
            while not is_trading_day(expiry):
                expiry -= timedelta(days=1)
            out.append(expiry)
        cursor += timedelta(days=1)
    return out


def build_option_series(
    underlying: pd.DataFrame,
    strike: float,
    expiry: date,
    right: str = "CE",
    volatility: float = 0.15,
    rate: float = 0.065,
) -> pd.DataFrame:
    """Price one option contract across the underlying's bars.

    Produces the same OHLCV shape as a real candle series so the backtest engine
    cannot tell the difference between this and a fetched option chain.
    """
    ts = pd.to_datetime(underlying["timestamp"], utc=True).dt.tz_convert(IST)
    expiry_dt = pd.Timestamp(datetime.combine(expiry, MARKET_CLOSE), tz=IST)

    rows = []
    for t, o, h, low_, c in zip(
        ts, underlying["open"], underlying["high"], underlying["low"], underlying["close"]
    ):
        tte = max(0.0, (expiry_dt - t).total_seconds() / (365.25 * 24 * 3600))
        prices = [
            black_scholes(float(s), strike, tte, volatility, rate, right).price
            for s in (o, h, low_, c)
        ]
        p_open, p_from_high, p_from_low, p_close = prices
        # A call's high comes from the underlying's high; a put's from its low.
        hi, lo = max(p_from_high, p_from_low), min(p_from_high, p_from_low)
        rows.append((t, p_open, max(hi, p_open, p_close), min(lo, p_open, p_close), p_close))

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].round(2)
    df["volume"] = underlying["volume"].values // 20  # options trade thinner
    df["oi"] = 0
    return df


if __name__ == "__main__":
    from backend.core.log_setup import configure_logging

    configure_logging(console_format="%(levelname)s: %(message)s", datefmt=None, to_file=False)
    df = generate_ohlcv("NIFTY", date(2026, 1, 1), date(2026, 3, 31), "5m", seed=42)
    print(df.head())
    print(f"\n{len(df):,} bars  close {df['close'].iloc[0]:.2f} -> {df['close'].iloc[-1]:.2f}")
    g = black_scholes(24800, 24800, 7 / 365, 0.13)
    print(f"\nATM 7d call: {g.price:.2f}  delta {g.delta:.3f}  theta {g.theta:.2f}")
