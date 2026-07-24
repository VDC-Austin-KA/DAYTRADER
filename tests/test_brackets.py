"""Bracket state-machine tests, including a replay of the real ASTS tape.

The module exists because of one day's trading (2026-07-17): calls that
needed selling at the peak and puts that needed holding to the bell. The
replay tests pin that the bracket beats what actually happened on the
calls, and loses less than what actually happened on the puts.
"""
from __future__ import annotations

from app.trading import brackets


def _run(state, bids):
    """Feed a bid path; collect (kind, qty, price) actions."""
    out = []
    for b in bids:
        a = brackets.check(state, b)
        if a.kind != "none":
            out.append((a.kind, a.sell_qty, a.est_price))
        if state.closed:
            break
    return out


def test_scale_out_banks_half_then_trail_arms():
    st = brackets.BracketState(position_id=1, entry_price=0.40, quantity=10)
    # +75% target = 0.70; ride up through it.
    acts = _run(st, [0.45, 0.55, 0.70])
    assert acts == [("scale_out", 5, 0.70)]
    assert st.remaining == 5 and not st.closed


def test_trail_ratchets_up_and_stops_on_rollover():
    st = brackets.BracketState(position_id=1, entry_price=0.40, quantity=10)
    _run(st, [0.70])                       # scale out, trail armed
    acts = _run(st, [1.00, 1.60, 1.30, 1.19])
    # High-water 1.60 -> trail 1.20; the 1.19 print exits the runner.
    assert acts == [("trail_stop", 5, 1.19)]
    assert st.closed


def test_hard_stop_exits_everything_and_outranks():
    st = brackets.BracketState(position_id=1, entry_price=0.40, quantity=10)
    acts = _run(st, [0.35, 0.30, 0.26])
    # -35% floor = 0.26: everything out at once, no partial.
    assert acts == [("hard_stop", 10, 0.26)]
    assert st.closed


def test_one_lot_never_splits_but_still_trails():
    st = brackets.BracketState(position_id=1, entry_price=0.40, quantity=1)
    acts = _run(st, [0.70, 1.00, 0.74])
    # No scale-out possible; trail arms at target and exits on rollover.
    assert acts == [("trail_stop", 1, 0.74)]


def test_asts_calls_replay_beats_actual():
    """14 calls @ ~0.145; actual exit 0.30 (+$217); peak ~1.70; close 0.00.

    Bracket: bank 7 at +75% (~0.25), trail 7 through the 1.70 peak, stop
    at 1.275 on the rollover. Must beat +$217 by a wide margin AND never
    ride to worthless.
    """
    st = brackets.BracketState(position_id=1, entry_price=0.145, quantity=14)
    path = [0.16, 0.25, 0.30, 0.60, 1.00, 1.40, 1.70, 1.50, 1.27, 0.90]
    acts = _run(st, path)
    kinds = [a[0] for a in acts]
    assert kinds == ["scale_out", "trail_stop"]
    pnl = sum(q * (px - 0.145) * 100 for _, q, px in acts)
    assert pnl > 700, f"bracket made ${pnl:.0f}, should dwarf the actual $217"
    # And the position is flat long before the 0.00 expiry print.
    assert st.closed


def test_asts_puts_replay_loses_less_than_actual():
    """35 puts @ ~0.64, actual capitulation at 0.21 (-$1,479-ish).

    The bracket hard-stops at 0.42 (-35%) on the way down. Losing ~$770
    instead of ~$1,500. It does NOT capture the close-at-1.23 recovery --
    no honest rule does without also riding other trades to zero.
    """
    st = brackets.BracketState(position_id=1, entry_price=0.64, quantity=35)
    path = [0.60, 0.50, 0.42, 0.30, 0.21, 0.13]
    acts = _run(st, path)
    assert [a[0] for a in acts] == ["hard_stop"]
    _, qty, px = acts[0]
    loss = qty * (0.64 - px) * 100
    assert qty == 35
    assert loss < 1479 * 0.6, f"bracket loss ${loss:.0f} must be well under the real $1,479"


def test_gap_through_both_levels_takes_hard_stop():
    """A quote gapping below both trail and hard stop must exit ALL."""
    st = brackets.BracketState(position_id=1, entry_price=0.40, quantity=10)
    _run(st, [0.70, 1.00])                 # scaled out, high water 1.00
    acts = _run(st, [0.20])                # gap: below trail AND hard stop
    assert acts == [("hard_stop", 5, 0.20)]


def test_failed_sell_can_be_retried():
    """Scheduler un-winds state on a rejected order; check() must re-fire."""
    st = brackets.BracketState(position_id=1, entry_price=0.40, quantity=10)
    a = brackets.check(st, 0.70)
    assert a.kind == "scale_out"
    # Simulate the scheduler's rollback after a moomoo reject.
    st.scaled_out = False
    st.remaining += a.sell_qty
    a2 = brackets.check(st, 0.71)
    assert a2.kind == "scale_out" and a2.sell_qty == 5


# --- Time-based exits: give-up on a stall, and the hard time backstop ----

def test_time_rules_are_inert_without_minutes_held():
    """Callers that don't pass minutes_held get the price-only machine."""
    st = brackets.BracketState(
        position_id=1, entry_price=0.40, quantity=10,
        give_up_minutes=15, max_hold_minutes=30)
    # Flat, unprofitable bid held forever: no minutes_held => never cut here.
    for _ in range(50):
        assert brackets.check(st, 0.39).kind == "none"
    assert not st.closed


def test_give_up_cuts_a_stalled_position_after_the_deadline():
    """A dead-flat contract is cut at the deadline, before the -35% floor."""
    st = brackets.BracketState(
        position_id=1, entry_price=0.40, quantity=10, give_up_minutes=15)
    # +10% give-up price is 0.44; bid sits at 0.39 (a small loss, above the
    # 0.26 hard stop). Before the deadline: hold. At it: cut.
    assert brackets.check(st, 0.39, minutes_held=14).kind == "none"
    a = brackets.check(st, 0.39, minutes_held=15)
    assert a.kind == "give_up" and a.sell_qty == 10 and a.est_price == 0.39
    assert st.closed


def test_give_up_is_spared_when_progress_is_made():
    """Clear +give_up_progress by the deadline and the cut is waived."""
    st = brackets.BracketState(
        position_id=1, entry_price=0.40, quantity=10,
        give_up_minutes=15, give_up_progress=0.10)
    # Bid 0.45 >= give-up price 0.44: progress made, hold past the deadline.
    assert brackets.check(st, 0.45, minutes_held=20).kind == "none"
    assert not st.closed


def test_give_up_does_not_fire_once_scaled_out():
    """A runner that banked its win rides the trail, not the give-up rule."""
    st = brackets.BracketState(
        position_id=1, entry_price=0.40, quantity=10, give_up_minutes=15)
    _run(st, [0.70])                       # scale out at +75%, trail armed
    assert st.scaled_out
    # Long after the deadline, bid back near entry but above the trail: the
    # give-up rule must stay silent -- only the trail governs the runner.
    a = brackets.check(st, 0.60, minutes_held=40)
    assert a.kind == "none" and not st.closed


def test_hard_stop_still_outranks_the_give_up_deadline():
    """A gap through the floor at the deadline exits as a hard stop, not give_up."""
    st = brackets.BracketState(
        position_id=1, entry_price=0.40, quantity=10, give_up_minutes=15)
    a = brackets.check(st, 0.20, minutes_held=20)   # 0.20 < 0.26 hard stop
    assert a.kind == "hard_stop" and a.sell_qty == 10


def test_max_hold_time_stop_flattens_everything():
    """The absolute backstop exits the whole position regardless of P&L."""
    st = brackets.BracketState(
        position_id=1, entry_price=0.40, quantity=10, max_hold_minutes=30)
    assert brackets.check(st, 0.50, minutes_held=29).kind == "none"
    a = brackets.check(st, 0.50, minutes_held=30)
    assert a.kind == "time_stop" and a.sell_qty == 10 and st.closed
