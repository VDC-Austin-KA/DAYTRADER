"""Black-Scholes pricing + probability helpers (pure stdlib, no scipy).

Used by the opportunity scanner to estimate an option's probability of profit
and its potential return if the underlying makes a typical move.
"""
from __future__ import annotations

import math

TRADING_DAYS = 252


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, sigma: float,
             option_type: str, r: float = 0.0) -> float:
    """Black-Scholes price for a European call/put."""
    is_call = option_type == "call"
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:  # fall back to intrinsic value
        return max(0.0, (S - K) if is_call else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def prob_of_profit(S: float, breakeven: float, T: float, sigma: float,
                   option_type: str) -> float:
    """Probability the option finishes profitable at expiration.

    Lognormal terminal-price model with zero drift (conservative over a few
    days). For a call: P(S_T > breakeven); for a put: P(S_T < breakeven).
    """
    if S <= 0 or breakeven <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d2 = (math.log(S / breakeven) - 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    return norm_cdf(d2) if option_type == "call" else norm_cdf(-d2)


def expected_one_day_move(sigma: float) -> float:
    """IV-implied typical one-trading-day move as a fraction of spot."""
    if sigma <= 0:
        return 0.0
    return sigma * math.sqrt(1.0 / TRADING_DAYS)


def potential_return(S: float, K: float, dte: int, sigma: float, mid: float,
                     option_type: str) -> float:
    """Return if the underlying makes a ~1-day IV-move in the favorable direction.

    Reprices the option one day forward (time decay included) at the moved spot
    and compares to the current mid premium.
    """
    if mid <= 0 or sigma <= 0 or dte <= 0:
        return 0.0
    move = expected_one_day_move(sigma)
    target = S * (1 + move) if option_type == "call" else S * (1 - move)
    t_after = max((dte - 1) / 365.0, 0.5 / 365.0)
    new_price = bs_price(target, K, t_after, sigma, option_type)
    return (new_price - mid) / mid
