"""Scale-out + trailing exit brackets: bank the double, ride the runner.

Born from a real tape (2026-07-17, ASTS): the calls needed selling at the
midday peak, the puts needed holding to the bell -- opposite exit rules on
the same day. No fixed rule wins both in hindsight. What loses least and
captures most across BOTH cases:

  1. SCALE OUT: sell ``scale_fraction`` of the position at
     ``scale_out_gain`` (default: half at +75%). The win is banked; the
     round-trip-to-zero can no longer touch it.
  2. TRAIL THE REST: the runner rides with a stop that follows the option's
     high-water mark down by ``trail_pct`` (default 25%). A move that keeps
     going keeps paying; a rollover gets stopped near the top instead of
     ridden back to zero.
  3. HARD FLOOR: below ``stop_loss_pct`` of entry, everything exits. This
     is the ASTS-puts rule -- the -66% capitulation happens at -35%
     instead, before averaging down feels tempting.
  4. THE BELL: app/trading/session.py flattens everything at 14:45 CT
     regardless. A 0DTE held to expiry is a coin flip with the whole stack;
     the flatten rule already exists and this module defers to it.

All monitoring runs against the BID (we are long; the bid is what an exit
actually pays), reusing the convention from scalp.py.

State machine per position:
    OPEN --(bid >= scale price)--> RUNNER (half banked, trail armed)
    OPEN --(bid <= hard stop)----> EXIT ALL (stop_loss)
    RUNNER --(bid <= trail)------> EXIT REST (trail_stop)
Trail only ratchets up: high-water * (1 - trail_pct), never lowered.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("daytrader.brackets")

# Defaults chosen from the user's own tape:
# * +75% scale-out: their SPY scalps routinely hit +20-100% fast; banking
#   half at +75% keeps the "sell the double" instinct without exiting flat.
# * 25% trail: ASTS calls peaked at ~$1.70 and closed worthless; a 25% trail
#   from the high exits around $1.28 -- most of the runner, none of the zero.
# * -35% hard stop: their winning SPY discipline cut at ~-15 to -20%; -35%
#   is the do-not-pass backstop for when the fast cut didn't happen.
SCALE_OUT_GAIN = 0.75
SCALE_FRACTION = 0.5
TRAIL_PCT = 0.25
STOP_LOSS_PCT = 0.35


@dataclass
class BracketState:
    """Exit plan + live state for one long option position."""
    position_id: int
    entry_price: float
    quantity: int
    # --- parameters (frozen at entry) ---
    scale_out_gain: float = SCALE_OUT_GAIN
    scale_fraction: float = SCALE_FRACTION
    trail_pct: float = TRAIL_PCT
    stop_loss_pct: float = STOP_LOSS_PCT
    # --- live state ---
    high_water: float = 0.0
    scaled_out: bool = False
    remaining: int = 0
    closed: bool = False

    def __post_init__(self) -> None:
        self.high_water = self.entry_price
        self.remaining = self.quantity

    # Derived levels ------------------------------------------------------
    @property
    def scale_price(self) -> float:
        return round(self.entry_price * (1 + self.scale_out_gain), 2)

    @property
    def hard_stop(self) -> float:
        return round(self.entry_price * (1 - self.stop_loss_pct), 2)

    @property
    def trail_stop(self) -> float:
        """Ratchet: follows the high-water mark, never the current bid."""
        return round(self.high_water * (1 - self.trail_pct), 2)

    def scale_qty(self) -> int:
        """Contracts to bank at the scale-out. At least 1, never all when
        quantity > 1 (a 1-lot has nothing to split -- it just trails)."""
        if self.quantity <= 1:
            return 0
        return max(1, int(self.quantity * self.scale_fraction))

    def describe(self) -> str:
        return (
            f"entry {self.entry_price:.2f}: bank {self.scale_qty()} @ "
            f"{self.scale_price:.2f} (+{self.scale_out_gain:.0%}), trail rest "
            f"{self.trail_pct:.0%} off the high, hard stop {self.hard_stop:.2f}"
        )


@dataclass
class BracketAction:
    kind: str          # "scale_out" | "trail_stop" | "hard_stop" | "none"
    sell_qty: int
    est_price: float   # live bid -- the honest fill estimate
    reason: str
    state: BracketState = field(repr=False, default=None)


def check(state: BracketState, bid: float) -> BracketAction:
    """Evaluate one quote against the bracket. Mutates ``state``.

    Order of checks matters: the hard stop outranks everything (a gapped
    quote can satisfy several conditions at once, and on a gap down you
    want out entirely, not a partial). Then the trail (only armed after
    scale-out), then the scale-out itself.
    """
    none = BracketAction("none", 0, bid, "holding", state)
    if state.closed or state.remaining <= 0 or bid <= 0:
        return none

    # Ratchet the high-water mark first so a new high lifts the trail
    # before this same tick is tested against it.
    if bid > state.high_water:
        state.high_water = bid

    # 1. Hard floor -- always live, exits everything.
    if bid <= state.hard_stop:
        qty, state.remaining, state.closed = state.remaining, 0, True
        return BracketAction(
            "hard_stop", qty, bid,
            f"bid {bid:.2f} <= hard stop {state.hard_stop:.2f} "
            f"(-{state.stop_loss_pct:.0%} from entry)", state)

    # 2. Trailing stop -- armed only once the runner exists.
    if state.scaled_out and bid <= state.trail_stop:
        qty, state.remaining, state.closed = state.remaining, 0, True
        return BracketAction(
            "trail_stop", qty, bid,
            f"bid {bid:.2f} <= trail {state.trail_stop:.2f} "
            f"({state.trail_pct:.0%} off high {state.high_water:.2f})", state)

    # 3. Scale-out -- bank the win, arm the trail.
    if not state.scaled_out and bid >= state.scale_price:
        qty = state.scale_qty()
        if qty > 0:
            state.scaled_out = True
            state.remaining -= qty
            return BracketAction(
                "scale_out", qty, bid,
                f"bid {bid:.2f} >= +{state.scale_out_gain:.0%} target "
                f"{state.scale_price:.2f}; banking {qty}, trailing "
                f"{state.remaining}", state)
        # 1-lot: nothing to split. Arm the trail so the runner logic works.
        state.scaled_out = True

    return none
