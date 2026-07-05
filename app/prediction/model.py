"""Probability model for BTC hourly above/below contracts.

Models the remaining-to-settlement log return as Gaussian with:
  * volatility  — EWMA of recent 1-minute realized volatility, and
  * drift       — a damped momentum term from the last few minutes.

P(settle > strike) then follows in closed form. This is transparent,
needs no training data at runtime, and degrades gracefully: with no
recent bars it falls back to a conservative default volatility.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

# Conservative per-minute BTC log-return volatility used when no data is
# available (~0.045%/min ≈ 55% annualized).
DEFAULT_SIGMA_PER_MIN = 0.00045
MOMENTUM_WINDOW = 15  # minutes of returns feeding the drift term


@dataclass
class ProbabilityEstimate:
    p_above: float           # P(settlement price > strike)
    spot: float
    strike: float
    minutes_remaining: float
    sigma_per_min: float
    drift_per_min: float

    def rationale(self) -> str:
        return (
            f"spot={self.spot:.2f} strike={self.strike:.2f} "
            f"t={self.minutes_remaining:.1f}m sigma/min={self.sigma_per_min:.5f} "
            f"drift/min={self.drift_per_min:.6f} p_above={self.p_above:.3f}"
        )


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def realized_sigma_per_min(minute_returns: pd.Series, halflife: int = 60) -> float:
    """EWMA std of 1-minute log returns; default if too little data."""
    r = minute_returns.dropna()
    if len(r) < 30:
        return DEFAULT_SIGMA_PER_MIN
    sigma = float(r.ewm(halflife=halflife).std().iloc[-1])
    if not math.isfinite(sigma) or sigma <= 0:
        return DEFAULT_SIGMA_PER_MIN
    # Clamp to a sane band so one bad print cannot distort sizing.
    return min(max(sigma, 0.0001), 0.01)


def estimate_p_above(
    spot: float,
    strike: float,
    minutes_remaining: float,
    minute_returns: pd.Series,
    momentum_coeff: float = 0.35,
) -> ProbabilityEstimate:
    """Closed-form P(BTC settles above ``strike``) at the hour boundary."""
    t = max(minutes_remaining, 0.5)
    sigma = realized_sigma_per_min(minute_returns)

    drift = 0.0
    recent = minute_returns.dropna().tail(MOMENTUM_WINDOW)
    if len(recent) >= 5:
        drift = momentum_coeff * float(recent.mean())
        # Momentum must never dominate the noise term.
        drift = min(max(drift, -0.5 * sigma), 0.5 * sigma)

    mu = drift * t
    sd = sigma * math.sqrt(t)
    z = (math.log(strike / spot) - mu) / sd
    p_above = 1.0 - _norm_cdf(z)
    p_above = min(max(p_above, 0.01), 0.99)

    return ProbabilityEstimate(
        p_above=p_above,
        spot=spot,
        strike=strike,
        minutes_remaining=minutes_remaining,
        sigma_per_min=sigma,
        drift_per_min=drift,
    )
