"""Deflated Sharpe Ratio — the bar a strategy has to clear to mean anything.

Pure-stdlib port of `nq-edge-lab/edgelab/metrics.py`. The deployment has no
third-party dependencies, so the normal distribution comes from
`statistics.NormalDist` rather than scipy. The formulas are identical and the
two repos are expected to agree to floating-point noise.

Why this file exists: `nq/backtest.py` ranks the published strategy canon by
measured expectancy. Ranking N candidates and reporting the best one is an
order statistic, not an estimate — with enough candidates, the winner is
whoever got luckiest. These functions quantify how lucky "luckiest" is
expected to be, so the leader can be compared against it.

Reference: Bailey & Lopez de Prado, "The Deflated Sharpe Ratio" (2014).
"""

from __future__ import annotations

import math
from statistics import NormalDist

EULER_MASCHERONI = 0.5772156649015329

_N = NormalDist()


def sharpe_ratio(returns: list[float]) -> float:
    """Per-observation Sharpe. Not annualised — everything here stays on the
    per-trade scale so the deflation variance cannot be scale-mismatched."""
    n = len(returns)
    if n < 2:
        return float("nan")
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var <= 0:
        # Zero dispersion is undefined, not infinitely good.
        return float("nan")
    return mean / math.sqrt(var)


def _moment(returns: list[float], order: int) -> float:
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n
    if var <= 0:
        return float("nan")
    sd = math.sqrt(var)
    return sum(((r - mean) / sd) ** order for r in returns) / n


def skewness(returns: list[float]) -> float:
    return _moment(returns, 3) if len(returns) >= 2 else float("nan")


def kurtosis(returns: list[float]) -> float:
    """Raw kurtosis — a normal distribution scores 3.0, not 0."""
    return _moment(returns, 4) if len(returns) >= 2 else float("nan")


def probabilistic_sharpe_ratio(
    observed_sr: float,
    n_obs: int,
    skew: float,
    kurt: float,
    benchmark_sr: float = 0.0,
) -> float:
    """Probability that the true Sharpe exceeds `benchmark_sr`.

    Corrects the naive Sharpe t-test for non-normality: negative skew and fat
    tails make a given Sharpe less impressive than it looks.
    """
    if n_obs < 2 or not math.isfinite(observed_sr):
        return float("nan")
    if not (math.isfinite(skew) and math.isfinite(kurt)):
        return float("nan")

    variance = 1.0 - skew * observed_sr + ((kurt - 1.0) / 4.0) * observed_sr**2
    if variance <= 0:
        # The approximation has broken down. Refuse to report a number rather
        # than report a wrong one.
        return float("nan")

    z = (observed_sr - benchmark_sr) * math.sqrt(n_obs - 1) / math.sqrt(variance)
    return _N.cdf(z)


def null_sharpe_variance(n_obs: int) -> float:
    """Variance of per-observation Sharpe estimates across zero-edge trials.

    For strategies with no edge each trial's Sharpe has standard error about
    1/sqrt(n_obs), so the variance is about 1/n_obs. This is the *floor* — it
    assumes trials differ only by luck. Genuinely different strategies disperse
    wider, which raises the bar, which is the honest direction.
    """
    if n_obs < 2:
        raise ValueError("n_obs must be at least 2")
    return 1.0 / n_obs


def expected_max_sharpe(n_trials: int, variance_of_trial_sharpes: float) -> float:
    """Expected best Sharpe from `n_trials` strategies that all have zero edge.

    This is the number the winner has to beat to mean anything.
    """
    if n_trials < 1:
        raise ValueError("n_trials must be at least 1")
    if variance_of_trial_sharpes < 0:
        raise ValueError("variance_of_trial_sharpes cannot be negative")
    if n_trials == 1 or variance_of_trial_sharpes == 0:
        return 0.0

    z1 = _N.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(variance_of_trial_sharpes) * (
        (1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2
    )


def deflated_sharpe_ratio(
    observed_sr: float,
    n_obs: int,
    skew: float,
    kurt: float,
    n_trials: int,
    variance_of_trial_sharpes: float | None = None,
) -> float:
    """Probability the best-of-N strategy has a genuinely positive edge.

    Read it as a probability: below ~0.95 nothing has been demonstrated.
    """
    if variance_of_trial_sharpes is None:
        variance_of_trial_sharpes = null_sharpe_variance(n_obs)
    benchmark = expected_max_sharpe(n_trials, variance_of_trial_sharpes)
    return probabilistic_sharpe_ratio(
        observed_sr, n_obs, skew, kurt, benchmark_sr=benchmark
    )
