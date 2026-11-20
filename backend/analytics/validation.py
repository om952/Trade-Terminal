"""Is a Sharpe ratio real, or the best of many tries? (P4.2)

Two statistics from Bailey and López de Prado:

* **Probabilistic Sharpe ratio (PSR):** the probability that the true Sharpe
  exceeds a benchmark, given the observed Sharpe, how many daily returns it
  rests on, and their skew and kurtosis. Fat tails and negative skew make a
  Sharpe less trustworthy than a normal-returns formula would say.

* **Deflated Sharpe ratio (DSR):** the PSR against the Sharpe that the *best* of
  N independent tries of a worthless idea would be expected to reach. N is the
  number of distinct experiments in the strategy's family (each pair of strategy
  version and data set counts once: rerunning the identical test teaches
  nothing new, but retuning a number or switching the instrument is a new try).
  Every backtest has been recorded with its family since Phase 2, which is what
  makes N knowable.

All Sharpe ratios here are per day (annualized Sharpe / sqrt(252)), matching
how they are estimated: from daily returns (backend/analytics/metrics.py).

A DSR of 0.95 or more is the usual bar: 95% confident the edge survives the
search that found it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

from backend.analytics.metrics import TRADING_DAYS

EULER_GAMMA = 0.5772156649015329
_N = NormalDist()
DSR_THRESHOLD = 0.95


def per_day(annual_sharpe: float) -> float:
    return annual_sharpe / math.sqrt(TRADING_DAYS)


def probabilistic_sharpe(sr: float, benchmark: float, days: int, skew: float, kurtosis: float) -> float | None:
    """P(true per-day Sharpe > benchmark). None when there is too little to say anything."""
    if days < 3 or not all(math.isfinite(x) for x in (sr, benchmark, skew, kurtosis)):
        return None
    variance = 1 - skew * sr + (kurtosis - 1) / 4 * sr * sr
    if variance <= 0:
        return None
    return _N.cdf((sr - benchmark) * math.sqrt(days - 1) / math.sqrt(variance))


def expected_max_sharpe(trials: int, variance: float) -> float:
    """The per-day Sharpe the best of `trials` worthless tries is expected to show.

    `variance` is the variance of Sharpe estimates across the tries. With one
    try there is no selection, and the benchmark is zero.
    """
    if trials <= 1 or variance <= 0:
        return 0.0
    return math.sqrt(variance) * (
        (1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / trials)
        + EULER_GAMMA * _N.inv_cdf(1 - 1 / (trials * math.e))
    )


@dataclass
class Deflated:
    sharpe: float                    # annualized, as reported
    days: int
    trials: int
    psr: float | None                # P(true Sharpe > 0)
    benchmark_sharpe: float          # annualized: what the best of `trials` noise tries reaches
    dsr: float | None                # P(true Sharpe > that benchmark)

    @property
    def verdict(self) -> str:
        if self.dsr is None:
            return "too little data to judge"
        if self.dsr >= DSR_THRESHOLD:
            return "likely real after the search"
        if self.psr is not None and self.psr >= DSR_THRESHOLD:
            return "good on its own, not after the search"
        return "could be luck"

    def as_dict(self) -> dict:
        def r(x: float | None) -> float | None:
            return None if x is None else round(x, 4)

        return {
            "sharpe": r(self.sharpe), "days": self.days, "trials": self.trials,
            "psr": r(self.psr), "benchmark_sharpe": r(self.benchmark_sharpe), "dsr": r(self.dsr),
            "threshold": DSR_THRESHOLD, "verdict": self.verdict,
        }


def deflate(metrics: dict, trial_sharpes: list[float | None], trials: int) -> Deflated:
    """Deflate one run's Sharpe by the search that produced it.

    `trial_sharpes` are the annualized Sharpe ratios of the family's distinct
    experiments (this one included), used for the variance across tries; with
    fewer than two, the variance of a Sharpe estimate under the null (1 / days)
    stands in.
    """
    annual = float(metrics.get("sharpe") or 0.0)
    days = int(metrics.get("return_days") or 0)
    skew = float(metrics.get("return_skew") or 0.0)
    kurt = float(metrics.get("return_kurtosis") or 3.0)
    sr = per_day(annual)
    finite = [per_day(s) for s in trial_sharpes if s is not None and math.isfinite(s)]
    if len(finite) >= 2:
        mean = sum(finite) / len(finite)
        variance = sum((x - mean) ** 2 for x in finite) / (len(finite) - 1)
    else:
        variance = 1 / max(days - 1, 1)
    sr0 = expected_max_sharpe(max(trials, 1), variance)
    return Deflated(
        sharpe=annual, days=days, trials=max(trials, 1),
        psr=probabilistic_sharpe(sr, 0.0, days, skew, kurt),
        benchmark_sharpe=sr0 * math.sqrt(TRADING_DAYS),
        dsr=probabilistic_sharpe(sr, sr0, days, skew, kurt),
    )
