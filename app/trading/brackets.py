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
  4. GIVE UP ON A STALL: on 0DTE the guaranteed enemy is theta. A position
     that has not made progress within ``give_up_minutes`` is cut before
     the slow bleed reaches the hard stop -- a dead-flat or grinding-lower
     contract is losing money every minute it is held for nothing. This is
     the loss the -35% floor never sees: not a sharp reversal, just decay.
     The rule is skipped once the position has scaled out; a runner that
     already banked its win rides the trail instead. Disabled by default
     (``give_up_minutes = 0``) so existing behaviour is unchanged until a
     backtest earns the number.
  5. TIME BACKSTOP: ``max_hold_minutes`` flattens the whole position after
     a fixed hold regardless of P&L -- a per-trade analogue of the bell,
     for capping how long any single 0DTE trade ties up capital. Off by
     default.
  6. THE BELL: app/trading/session.py flattens everything at 14:45 CT
     regardless. A 0DTE held to expiry is a coin flip with the whole stack;
     the flatten rule already exists and this module defers to it.

All monitoring runs against the BID (we are long; the bid is what an exit
actually pays), reusing the convention from scalp.py.

The time-based rules (4, 5) only fire when the caller passes ``minutes_held``
into :func:`check`. Live and paper callers that do not track elapsed time
call ``check(state, bid)`` exactly as before and get the price-only machine
-- the time rules are strictly additive, never a change to existing exits.

State machine per position:
    OPEN --(bid >= scale price)------> RUNNER (half banked, trail armed)
    OPEN --(bid <= hard stop)--------> EXIT ALL (hard_stop)
    OPEN --(held, no progress)-------> EXIT ALL (give_up)   [needs time]
    ANY  --(held >= max_hold)--------> EXIT ALL (time_stop) [needs time]
    RUNNER --(bid <= trail)----------> EXIT REST (trail_stop)
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
# Time-based exits are OFF by default: 0 disables each. Turn them on only
# with a value a backtest (app/backtest/scalpsim.py) supports out of sample.
GIVE_UP_MINUTES = 0.0
GIVE_UP_PROGRESS = 0.10
MAX_HOLD_MINUTES = 0.0


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
    # Cut a still-unprofitable position after this many minutes (0 = off),
    # unless the bid has already cleared entry by ``give_up_progress``.
    give_up_minutes: float = GIVE_UP_MINUTES
    give_up_progress: float = GIVE_UP_PROGRESS
    # Absolute time backstop: flatten after this many minutes (0 = off).
    max_hold_minutes: float = MAX_HOLD_MINUTES
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

    @property
    def give_up_price(self) -> float:
        """Minimum bid by the give-up deadline to be spared the cut."""
        return round(self.entry_price * (1 + self.give_up_progress), 2)

    def scale_qty(self) -> int:
        """Contracts to bank at the scale-out. At least 1, never all when
        quantity > 1 (a 1-lot has nothing to split -- it just trails)."""
        if self.quantity <= 1:
            return 0
        return max(1, int(self.quantity * self.scale_fraction))

    def describe(self) -> str:
        base = (
            f"entry {self.entry_price:.2f}: bank {self.scale_qty()} @ "
            f"{self.scale_price:.2f} (+{self.scale_out_gain:.0%}), trail rest "
            f"{self.trail_pct:.0%} off the high, hard stop {self.hard_stop:.2f}"
        )
        if self.give_up_minutes:
            base += (
                f", give up after {self.give_up_minutes:.0f}m below "
                f"{self.give_up_price:.2f} (+{self.give_up_progress:.0%})"
            )
        if self.max_hold_minutes:
            base += f", time stop {self.max_hold_minutes:.0f}m"
        return base


@dataclass
class BracketAction:
    kind: str          # scale_out | trail_stop | hard_stop |
                       # give_up | time_stop | none
    sell_qty: int
    est_price: float   # live bid -- the honest fill estimate
    reason: str
    state: BracketState = field(repr=False, default=None)


def check(
    state: BracketState, bid: float, minutes_held: float | None = None
) -> BracketAction:
    """Evaluate one quote against the bracket. Mutates ``state``.

    ``minutes_held`` is the elapsed time since entry, in minutes. When the
    caller supplies it the time-based rules (give-up, time stop) are live;
    when it is ``None`` (the default) they are skipped entirely and the
    machine behaves exactly as the price-only bracket always has.

    Order of checks matters: the hard stop outranks everything (a gapped
    quote can satisfy several conditions at once, and on a gap down you
    want out entirely, not a partial). Then the absolute time backstop and
    the give-up-on-a-stall rule, then the trail (only armed after
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

    # 2. Time-based exits -- only when the caller tracks elapsed time.
    if minutes_held is not None:
        # 2a. Absolute backstop: out after max_hold, whatever the P&L.
        if state.max_hold_minutes and minutes_held >= state.max_hold_minutes:
            qty, state.remaining, state.closed = state.remaining, 0, True
            return BracketAction(
                "time_stop", qty, bid,
                f"held {minutes_held:.0f}m >= max {state.max_hold_minutes:.0f}m "
                f"time stop", state)
        # 2b. Give up on a stall: unscaled and no progress by the deadline.
        # A scaled runner has banked its win and rides the trail instead.
        if (state.give_up_minutes and not state.scaled_out
                and minutes_held >= state.give_up_minutes
                and bid < state.give_up_price):
            qty, state.remaining, state.closed = state.remaining, 0, True
            return BracketAction(
                "give_up", qty, bid,
                f"held {minutes_held:.0f}m >= {state.give_up_minutes:.0f}m with "
                f"bid {bid:.2f} < {state.give_up_price:.2f} "
                f"(+{state.give_up_progress:.0%}); cutting the theta bleed",
                state)

    # 4. Trailing stop -- armed only once the runner exists.
    if state.scaled_out and bid <= state.trail_stop:
        qty, state.remaining, state.closed = state.remaining, 0, True
        return BracketAction(
            "trail_stop", qty, bid,
            f"bid {bid:.2f} <= trail {state.trail_stop:.2f} "
            f"({state.trail_pct:.0%} off high {state.high_water:.2f})", state)

    # 5. Scale-out -- bank the win, arm the trail.
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
