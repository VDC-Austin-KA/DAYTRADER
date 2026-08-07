"""Structure layer: strike convention, normalisation, costs, and the parity
result that decides whether credit spreads can rescue a directional signal.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.backtest import structures as S
from app.backtest.scalpsim import (EXPIRY_ET, SimParams,
                                   simulate_from_directions)


# --- strike convention ---------------------------------------------------

def test_offset_is_relative_to_the_profitable_direction():
    """One convention, both directions: +offset is always further OTM."""
    otm = S.Structure("t", (S.Leg(1, 2.0),))
    assert otm.strike(500.0, otm.legs[0], "up") == 502.0     # call above spot
    assert otm.strike(500.0, otm.legs[0], "down") == 498.0   # put below spot

    itm = S.Structure("t", (S.Leg(1, -2.0),))
    assert itm.strike(500.0, itm.legs[0], "up") == 498.0
    assert itm.strike(500.0, itm.legs[0], "down") == 502.0


def test_primary_and_opposite_rights_follow_direction():
    st = S.Structure("t", (S.Leg(1, 0.0, "primary"), S.Leg(1, 0.0, "opposite")))
    assert st.right_of(st.legs[0], "up") == "call"
    assert st.right_of(st.legs[1], "up") == "put"
    assert st.right_of(st.legs[0], "down") == "put"
    assert st.right_of(st.legs[1], "down") == "call"


# --- the incumbent still prices exactly as it always did -----------------

def test_long_atm_reproduces_the_single_contract_model():
    """The baseline must be bit-identical or no comparison means anything."""
    spot, base, mins, iv = 500.3, 500.0, 120.0, 0.15
    prem = S.LONG_ATM.leg_premiums(spot, base, mins, iv, "up")
    assert len(prem) == 1
    assert prem[0] == pytest.approx(
        S.bs_premium(spot, base, mins, iv, "call"))
    assert S.LONG_ATM.collateral(base, "up") == 0.0
    # one leg, flat penny spread, no proportional term
    assert S.LONG_ATM.crossing_cost(0.01, 0.0, prem) == pytest.approx(0.01)


def test_default_structure_is_the_incumbent():
    assert SimParams().structure is S.LONG_ATM
    assert SimParams().rel_half_spread == 0.0


# --- normalisation -------------------------------------------------------

def test_credit_vertical_collateral_is_the_width():
    coll = S.CREDIT_V2.collateral(500.0, "up")
    assert coll == pytest.approx(2.0, abs=0.05)


def test_debit_structures_need_no_collateral():
    for st in (S.LONG_ATM, S.LONG_ITM1, S.LONG_OTM1):
        assert st.collateral(500.0, "up") == 0.0


def test_normalised_mark_is_non_negative_for_credit_structure():
    """A credit spread must read as a long position for the brackets."""
    for spot in (490.0, 500.0, 510.0):
        mark = S.CREDIT_V2.mark(spot, 500.0, 60.0, 0.15, "up")
        assert mark >= -1e-9


# --- the parity result ---------------------------------------------------

def test_selling_premium_does_not_change_a_directional_trade():
    """Short put spread == long call spread, same strikes, zero rates.

    This is why "be short theta instead" cannot rescue a directional signal:
    hold the directional exposure fixed and the P&L is the SAME instrument
    wearing a different name. Asserted numerically across spot and time so it
    is a property of the pricing, not an opinion in a docstring.
    """
    base = 500.0
    # long call spread at the same strikes the credit structure uses:
    # short ATM put / long $2-OTM put  <->  long ATM call / short $2 lower call
    call_spread = S.Structure(
        "cs", (S.Leg(1, -2.0, "primary"), S.Leg(-1, 0.0, "primary")))
    for spot in (494.0, 498.0, 500.0, 503.0, 508.0):
        for mins in (5.0, 60.0, 240.0):
            a = S.CREDIT_V2.mark(spot, base, mins, 0.15, "up")
            b = call_spread.mark(spot, base, mins, 0.15, "up")
            assert a == pytest.approx(b, abs=1e-6)


# --- cost accounting -----------------------------------------------------

def test_every_leg_crosses_its_own_spread():
    """A vertical pays two spreads. That is the price of financing decay."""
    prem = [1.20, 0.40]
    assert S.DEBIT_V2.crossing_cost(0.01, 0.0, prem) == pytest.approx(0.02)
    assert S.LONG_ATM.crossing_cost(0.01, 0.0, [1.20]) == pytest.approx(0.01)


def test_relative_spread_charges_expensive_legs_more():
    """Without this an ITM contract looks cheaper to trade than it quotes."""
    cheap, rich = [0.50], [4.00]
    assert S.LONG_ATM.crossing_cost(0.01, 0.01, cheap) == pytest.approx(0.01)
    assert S.LONG_ATM.crossing_cost(0.01, 0.01, rich) == pytest.approx(0.04)


# --- the actual cost claims ---------------------------------------------

def _decay(st: S.Structure, minutes: float) -> float:
    """Fraction of the mark lost to time alone, spot pinned."""
    hold = st.mark(500.0, 500.0, minutes, 0.15, "up")
    later = st.mark(500.0, 500.0, minutes - 30.0, 0.15, "up")
    return (hold - later) / hold if hold else 0.0


def test_itm_and_longer_dated_decay_less_than_atm_0dte():
    """The premise of the whole exercise, checked rather than assumed."""
    atm = _decay(S.LONG_ATM, 120.0)
    assert _decay(S.LONG_ITM2, 120.0) < atm
    assert _decay(S.LONG_ATM_1DTE, 120.0) < atm


def test_otm_0dte_decays_worst_of_all():
    assert _decay(S.LONG_OTM1, 120.0) > _decay(S.LONG_ATM, 120.0)


def test_longer_dated_costs_more_up_front():
    """The trade-off is real: gentler decay is bought, not granted."""
    atm = S.LONG_ATM.mark(500.0, 500.0, 120.0, 0.15, "up")
    dte1 = S.LONG_ATM_1DTE.mark(500.0, 500.0, 120.0, 0.15, "up")
    assert dte1 > atm


# --- end to end through the simulator ------------------------------------

def _flat_bars(n: int = 90) -> pd.DataFrame:
    idx = pd.date_range("2026-07-20 10:00", periods=n, freq="1min")
    px = np.full(n, 500.0)
    return pd.DataFrame(
        {"Open": px, "High": px, "Low": px, "Close": px,
         "Volume": np.full(n, 1e6)}, index=idx)


def test_every_catalogue_structure_runs_end_to_end():
    bars = _flat_bars()
    dirs = np.full(len(bars), "", dtype=object)
    dirs[5] = "up"
    for st in S.CATALOGUE:
        res = simulate_from_directions(
            bars, dirs, SimParams(structure=st, qty=1, give_up_minutes=0.0))
        assert res.summary()["n"] <= 1, st.name


def _flat_tape_loss(st: S.Structure, iv_mult: float) -> float:
    bars = _flat_bars()
    dirs = np.full(len(bars), "", dtype=object)
    dirs[5] = "up"
    res = simulate_from_directions(
        bars, dirs, SimParams(structure=st, qty=1, entry_iv_mult=iv_mult))
    return res.summary().get("total", 0.0)


def test_flat_tape_ranks_structures_by_theta_when_iv_is_stable():
    """A dead-flat session is pure cost. With IV pinned, theta is the cost."""
    atm = _flat_tape_loss(S.LONG_ATM, 1.0)
    assert _flat_tape_loss(S.LONG_ITM2, 1.0) > atm
    assert _flat_tape_loss(S.LONG_ATM_1DTE, 1.0) > atm


_HOLD = 354.0


def _theta(st: S.Structure) -> float:
    return (st.mark(500., 500., _HOLD, .15, "up")
            - st.mark(500., 500., _HOLD - 83, .15, "up"))


def _vega(st: S.Structure) -> float:
    return (st.mark(500., 500., _HOLD, .165, "up")
            - st.mark(500., 500., _HOLD, .15, "up"))


def test_going_longer_dated_swaps_theta_risk_for_vega_risk():
    """"Just buy more time" is not a cost cut, it is a cost SWAP.

    Decay falls with tenor and vega rises with it. Which one wins is not a
    property of the structure -- it is a property of how hard IV crushes,
    which this data cannot observe. That is the finding, so it is asserted
    rather than resolved.
    """
    assert _theta(S.LONG_ATM_1DTE) < _theta(S.LONG_ATM)
    assert _vega(S.LONG_ATM_1DTE) > _vega(S.LONG_ATM)


def test_itm_is_the_only_structure_cheaper_on_both_axes():
    """Less theta AND less vega -- no swap, a straight reduction."""
    assert _theta(S.LONG_ITM2) < _theta(S.LONG_ATM)
    assert _vega(S.LONG_ITM2) < _vega(S.LONG_ATM)


def test_crush_toll_is_damped_by_tenor_but_untouched_at_0dte():
    """The 0DTE-calibrated toll must not be charged flat across tenors.

    Applying the full front-expiry crush to a 2DTE contract invents a cost
    big enough to sink it on arithmetic alone. 0DTE keeps the full toll, so
    every result predating this adjustment reproduces exactly.
    """
    assert S.LONG_ATM.crush_beta == 1.0
    assert S.LONG_ATM.entry_iv(0.15, 1.10) == pytest.approx(0.165)
    assert S.LONG_ATM_2DTE.crush_beta < S.LONG_ATM_1DTE.crush_beta < 1.0
    assert S.LONG_ATM_1DTE.entry_iv(0.15, 1.10) < 0.165
    # No toll to damp: every tenor prices at the base IV.
    for st in (S.LONG_ATM, S.LONG_ATM_1DTE, S.LONG_ATM_2DTE):
        assert st.entry_iv(0.15, 1.0) == pytest.approx(0.15)


def test_whether_longer_dated_wins_depends_on_the_crush_assumption():
    """The load-bearing uncertainty, pinned as a regression.

    With IV stable, 1DTE's slower decay wins on a flat tape. Crank the crush
    to 20% and its extra vega loses instead. Any claim about tenor that does
    not state a crush assumption is unfalsifiable -- hence the sensitivity
    table in run_structures.py.
    """
    assert _flat_tape_loss(S.LONG_ATM_1DTE, 1.00) > _flat_tape_loss(S.LONG_ATM, 1.00)
    assert _flat_tape_loss(S.LONG_ATM_1DTE, 1.20) < _flat_tape_loss(S.LONG_ATM, 1.20)


def test_negative_vega_structure_improves_as_the_crush_worsens():
    """The one structure a violent IV crush HELPS.

    An ITM-to-ATM vertical is net short vega, so the same crush that taxes
    every long-premium ticket pays this one. That is a real, direction-
    independent cost lever -- and the reason credit_vert2 tops the table.
    """
    assert _vega(S.CREDIT_V2) < 0
    assert _flat_tape_loss(S.CREDIT_V2, 1.20) > _flat_tape_loss(S.CREDIT_V2, 1.00)
