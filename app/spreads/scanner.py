"""Vertical-spread scanning: IV-rank regime switch + delta strike selection.

Regime:
    IV rank > ``iv_rank_credit_min``  -> sell premium  (credit verticals)
    IV rank < ``iv_rank_debit_max``   -> buy premium   (debit verticals)
    in between                        -> stand down

Strike selection (both regimes anchor the SHORT leg on delta):
    * Short leg = OTM strike nearest ``short_leg_delta`` (|delta|).
    * Credit: long wing sits ``wing_width_strikes`` FURTHER OTM — minimum
      capital, strictly defined max risk.
    * Debit: long leg sits ``wing_width_strikes`` CLOSER to the money, so
      the position owns the move while the 0.20-delta short wing caps cost.

Both calls and puts are scanned across every expiry in the 0-3 DTE window;
the candidate with the best reward-to-risk that passes the liquidity and
pricing filters wins.
"""
from __future__ import annotations

import logging
import math
from datetime import date

from .chain import ChainStore
from .config import SpreadsConfig
from .models import Quote, Right, SpreadCandidate, SpreadKind

log = logging.getLogger("daytrader.spreads.scanner")

# The delta anchor must land reasonably close to target, else the ladder is
# too coarse/illiquid to trust.
_DELTA_TOLERANCE = 0.10


class SpreadScanner:
    def __init__(self, cfg: SpreadsConfig) -> None:
        self.cfg = cfg

    def regime(self, iv_rank: float) -> SpreadKind | None:
        if iv_rank > self.cfg.iv_rank_credit_min:
            return SpreadKind.CREDIT
        if iv_rank < self.cfg.iv_rank_debit_max:
            return SpreadKind.DEBIT
        return None

    def scan(
        self,
        chain: ChainStore,
        iv_rank: float,
        spot: float,
        today: date | None = None,
    ) -> SpreadCandidate | None:
        kind = self.regime(iv_rank)
        if kind is None or spot <= 0:
            return None

        best: SpreadCandidate | None = None
        best_score = -math.inf
        for expiry in chain.expiries(self.cfg.min_dte, self.cfg.max_dte, today):
            for right in (Right.PUT, Right.CALL):
                cand = self._build(chain, expiry, right, kind, iv_rank, spot)
                if cand is None:
                    continue
                score = self._score(cand)
                if score > best_score:
                    best, best_score = cand, score
        return best

    # ------------------------------------------------------------------ #
    def _build(
        self,
        chain: ChainStore,
        expiry: date,
        right: Right,
        kind: SpreadKind,
        iv_rank: float,
        spot: float,
    ) -> SpreadCandidate | None:
        # OTM ladder ordered from the money outward.
        quotes = [
            q for q in chain.quotes_for(expiry, right)
            if not math.isnan(q.delta)
            and (q.contract.strike > spot if right is Right.CALL else q.contract.strike < spot)
        ]
        if right is Right.PUT:
            quotes.reverse()
        if len(quotes) <= self.cfg.wing_width_strikes:
            return None

        # Anchor: OTM strike whose |delta| is nearest the target.
        target = self.cfg.short_leg_delta
        anchor_idx = min(
            range(len(quotes)), key=lambda i: abs(abs(quotes[i].delta) - target)
        )
        anchor = quotes[anchor_idx]
        if abs(abs(anchor.delta) - target) > _DELTA_TOLERANCE:
            return None

        if kind is SpreadKind.CREDIT:
            wing_idx = anchor_idx + self.cfg.wing_width_strikes  # further OTM
            if wing_idx >= len(quotes):
                return None
            short_leg, long_leg = anchor, quotes[wing_idx]
        else:
            long_idx = anchor_idx - self.cfg.wing_width_strikes  # nearer the money
            if long_idx < 0:
                return None
            short_leg, long_leg = anchor, quotes[long_idx]

        for leg in (short_leg, long_leg):
            if leg.mid <= 0 or leg.spread_pct > self.cfg.max_quote_spread_pct:
                return None

        cand = SpreadCandidate(
            kind=kind,
            short_leg=short_leg,
            long_leg=long_leg,
            iv_rank=iv_rank,
            quantity=self.cfg.contracts_per_trade,
        )
        return cand if self._acceptable(cand) else None

    def _acceptable(self, cand: SpreadCandidate) -> bool:
        width = cand.width
        if width <= 0:
            return False
        if cand.kind is SpreadKind.CREDIT:
            if cand.net_mid <= 0:
                return False
            return cand.net_mid / width >= self.cfg.min_credit_to_width
        debit = -cand.net_mid
        if debit <= 0:
            return False
        return debit / width <= self.cfg.max_debit_to_width

    @staticmethod
    def _score(cand: SpreadCandidate) -> float:
        """Reward-to-risk: premium kept (credit) or payoff bought (debit)
        per dollar of defined risk."""
        risk = cand.max_risk_per_spread
        if risk <= 0:
            return -math.inf
        if cand.kind is SpreadKind.CREDIT:
            return cand.net_mid * 100.0 / risk
        return (cand.width + cand.net_mid) * 100.0 / risk
