"""Option STRUCTURES: the instrument a signal is expressed through.

WHY THIS EXISTS
---------------
Every model in this repo -- burst, vwap, confluence, reversion, router -- was
priced as ONE LONG ATM 0DTE CONTRACT, because that is what ``scalpsim`` had
hardcoded. Long premium at zero days to expiry is the most theta-expensive
instrument available, so "the edge does not survive theta" was, in part, a
statement about an instrument choice nobody had ever varied.

This module makes the instrument a variable. The same direction signal can be
expressed as a long ATM contract, a long ITM contract, a debit vertical, a
credit vertical, or any of those at 1-3 DTE, and every version is priced
through the same Black-Scholes machinery and the same bracket state machine so
the dollar numbers are comparable.

THE STRIKE CONVENTION
---------------------
``offset`` is signed distance from the at-the-money strike **in the direction
the trade profits**, in dollars:

    up   trade:  strike = base + offset
    down trade:  strike = base - offset

So a positive offset is always "further out of the money in the favourable
direction" and a negative offset is always "deeper in the money", whichever way
the trade is pointed. One convention covers calls and puts, and a structure
definition never has to branch on direction.

``right`` is likewise relative: "primary" is the option you would buy outright
(call for an up trade, put for a down trade), "opposite" is the other one.

WHAT THE NORMALISED MARK DOES
-----------------------------
``brackets.check`` monitors a single number that rises when the position gains.
A net-debit structure supplies that directly. A net-CREDIT structure is a
liability -- its net value is negative and falls when you profit -- so it is
shifted by its collateral (the worst case that must be posted):

    mark(t) = net_value(t) + collateral

For a bull put spread this shift lands exactly on the value of the bull CALL
spread at the same strikes, which is not a coincidence but put-call parity:
at zero rates a short put spread and a long call spread on the same strikes
are the same trade. See ``test_structures.py`` -- that identity is asserted
numerically, not assumed. It is the reason "sell premium so theta works for
you" cannot rescue a directional signal: keeping the directional exposure
keeps the P&L, whatever the marketing on the ticket says.

COSTS SCALE WITH LEGS
---------------------
Each leg crosses its own bid-ask. A vertical pays two spreads, not one. That
is charged here rather than assumed away, and it is the main thing standing
between a spread and a free lunch.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

MINUTES_PER_YEAR = 365.0 * 24 * 60
MINUTES_PER_DAY = 24 * 60


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_premium(spot: float, strike: float, minutes_left: float,
               iv: float, right: str) -> float:
    """Black-Scholes, zero rates -- fine at this horizon."""
    if minutes_left <= 0:
        intrinsic = spot - strike if right == "call" else strike - spot
        return max(0.0, intrinsic)
    t = minutes_left / MINUTES_PER_YEAR
    sig_rt = iv * math.sqrt(t)
    if sig_rt <= 0:
        return max(0.0, spot - strike if right == "call" else strike - spot)
    d1 = (math.log(spot / strike) + 0.5 * sig_rt * sig_rt) / sig_rt
    d2 = d1 - sig_rt
    if right == "call":
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


@dataclass(frozen=True)
class Leg:
    """One leg. ``qty`` is +1 long / -1 short (per structure unit)."""
    qty: int = 1
    offset: float = 0.0        # $ from ATM, positive = favourable/OTM
    right: str = "primary"     # "primary" | "opposite"


@dataclass(frozen=True)
class Structure:
    """A tradeable expression of a direction signal.

    ``dte_days`` shifts expiry forward in CALENDAR days -- theta runs
    overnight, so a 1DTE contract held intraday pays decay at the rate set by
    its full remaining life, which is the whole point of holding one.
    """
    name: str
    legs: tuple[Leg, ...] = (Leg(),)
    dte_days: int = 0
    note: str = ""

    @property
    def n_legs(self) -> int:
        return len(self.legs)

    @property
    def is_credit(self) -> bool:
        """Net short premium: the ticket pays you at entry."""
        return sum(l.qty for l in self.legs) < 0

    @property
    def crush_beta(self) -> float:
        """How much of the entry IV-crush toll this tenor actually pays.

        The sim charges entries a richer IV than it marks them at, modelling
        the post-signal premium bulge that deflates once the move resolves.
        That toll was measured on 0DTE, and applying it flat across tenors is
        wrong in an expensive direction: front-expiry IV is what spikes and
        crushes, while a contract days out barely notices the same event and
        carries several times the vega to lose if it did. Charging it the
        full 0DTE toll manufactures a cost that would sink any longer-dated
        structure on arithmetic alone.

        The vol term structure damps roughly with the square root of tenor,
        so that is the haircut applied: full toll at 0DTE (leaving every
        existing result untouched), ~71% at 1DTE, ~58% at 2DTE. It is an
        ASSUMPTION, it moves the DTE ranking on its own, and
        ``run_structures.py --crush`` exists to stress it.
        """
        return 1.0 / math.sqrt(1.0 + self.dte_days)

    def entry_iv(self, iv: float, entry_iv_mult: float) -> float:
        """Tenor-adjusted entry IV: the toll this structure really pays."""
        return iv * (1.0 + (entry_iv_mult - 1.0) * self.crush_beta)

    def strike(self, base: float, leg: Leg, direction: str) -> float:
        """Absolute strike for a leg, given the trade's direction."""
        return base + leg.offset if direction == "up" else base - leg.offset

    def right_of(self, leg: Leg, direction: str) -> str:
        primary = "call" if direction == "up" else "put"
        if leg.right == "primary":
            return primary
        return "put" if primary == "call" else "call"

    def minutes_left(self, session_minutes_to_expiry: float) -> float:
        return session_minutes_to_expiry + self.dte_days * MINUTES_PER_DAY

    # -- valuation --------------------------------------------------------
    def net_value(self, spot: float, base: float, session_minutes: float,
                  iv: float, direction: str) -> float:
        """Signed value of the package. Negative for a net-credit structure."""
        ml = self.minutes_left(session_minutes)
        total = 0.0
        for leg in self.legs:
            total += leg.qty * bs_premium(
                spot, self.strike(base, leg, direction), ml, iv,
                self.right_of(leg, direction),
            )
        return total

    def collateral(self, base: float, direction: str) -> float:
        """Shift that makes the worst case zero, so the mark reads long.

        For a vertical this is the strike width; for a naked long it is 0.
        Computed from the payoff at expiry rather than assumed, so an odd
        structure still normalises correctly.
        """
        if not self.is_credit and all(l.qty > 0 for l in self.legs):
            return 0.0
        strikes = [self.strike(base, l, direction) for l in self.legs]
        lo, hi = min(strikes) - 5.0, max(strikes) + 5.0
        worst = min(
            self._expiry_value(s, base, direction)
            for s in _linspace(lo, hi, 400)
        )
        return max(0.0, -worst)

    def _expiry_value(self, spot: float, base: float, direction: str) -> float:
        total = 0.0
        for leg in self.legs:
            k = self.strike(base, leg, direction)
            right = self.right_of(leg, direction)
            intrinsic = max(0.0, spot - k) if right == "call" else max(0.0, k - spot)
            total += leg.qty * intrinsic
        return total

    def mark(self, spot: float, base: float, session_minutes: float,
             iv: float, direction: str, collateral: float | None = None) -> float:
        """Normalised, always-non-negative position value for the brackets."""
        coll = self.collateral(base, direction) if collateral is None else collateral
        return self.net_value(spot, base, session_minutes, iv, direction) + coll

    def leg_premiums(self, spot: float, base: float, session_minutes: float,
                     iv: float, direction: str) -> list[float]:
        ml = self.minutes_left(session_minutes)
        return [
            bs_premium(spot, self.strike(base, leg, direction), ml, iv,
                       self.right_of(leg, direction))
            for leg in self.legs
        ]

    def crossing_cost(self, abs_half_spread: float, rel_half_spread: float,
                      leg_premiums: list[float]) -> float:
        """Total half-spread paid to cross every leg once.

        With ``rel_half_spread = 0`` this is simply legs x the absolute half
        spread, which reproduces the single-contract model exactly. Turning
        the relative term on charges deep/expensive legs a proportional
        spread, which is how ITM contracts actually quote -- without it, an
        ITM structure looks cheaper to trade than it is.
        """
        return sum(
            max(abs_half_spread, rel_half_spread * prem) for prem in leg_premiums
        )


def _linspace(lo: float, hi: float, n: int) -> list[float]:
    if n <= 1:
        return [lo]
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


# --- the catalogue ------------------------------------------------------
# Each entry is a different answer to "what do we buy when the signal fires",
# holding the signal itself fixed.

LONG_ATM = Structure(
    "long_atm", (Leg(1, 0.0),), 0,
    "the incumbent: one long ATM 0DTE contract, maximum theta",
)
LONG_ITM1 = Structure(
    "long_itm1", (Leg(1, -1.0),), 0,
    "$1 in the money: more delta, less extrinsic to burn",
)
LONG_ITM2 = Structure(
    "long_itm2", (Leg(1, -2.0),), 0,
    "$2 in the money: mostly intrinsic, theta nearly parked",
)
LONG_OTM1 = Structure(
    "long_otm1", (Leg(1, 1.0),), 0,
    "$1 out of the money: the cheap lottery ticket, all extrinsic",
)
DEBIT_V2 = Structure(
    "debit_vert2", (Leg(1, 0.0), Leg(-1, 2.0)), 0,
    "long ATM, short $2 OTM: the short leg finances part of the decay",
)
DEBIT_V3 = Structure(
    "debit_vert3", (Leg(1, 0.0), Leg(-1, 3.0)), 0,
    "wider debit vertical: more upside kept, less decay financed",
)
CREDIT_V2 = Structure(
    "credit_vert2", (Leg(-1, 0.0, "opposite"), Leg(1, -2.0, "opposite")), 0,
    "sell the ATM opposite, buy $2 further out: the 'short theta' ticket",
)
LONG_ATM_1DTE = Structure(
    "long_atm_1dte", (Leg(1, 0.0),), 1,
    "same trade, tomorrow's expiry: theta per minute roughly halves",
)
LONG_ATM_2DTE = Structure(
    "long_atm_2dte", (Leg(1, 0.0),), 2,
    "two days out: decay gentler again, premium dearer again",
)
LONG_ITM1_1DTE = Structure(
    "long_itm1_1dte", (Leg(1, -1.0),), 1,
    "both cost levers at once: ITM strike and a day of life",
)
DEBIT_V2_1DTE = Structure(
    "debit_vert2_1dte", (Leg(1, 0.0), Leg(-1, 2.0)), 1,
    "financed decay plus a day of life",
)

CATALOGUE: tuple[Structure, ...] = (
    LONG_ATM, LONG_ITM1, LONG_ITM2, LONG_OTM1,
    DEBIT_V2, DEBIT_V3, CREDIT_V2,
    LONG_ATM_1DTE, LONG_ATM_2DTE, LONG_ITM1_1DTE, DEBIT_V2_1DTE,
)

BY_NAME = {s.name: s for s in CATALOGUE}
