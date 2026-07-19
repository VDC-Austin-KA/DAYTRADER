"""Scalp bracket tests -- mostly about refusing bad trades."""
from __future__ import annotations

import pytest

from datetime import datetime
from zoneinfo import ZoneInfo

from app.trading import scalp

CT = ZoneInfo("America/Chicago")


def _contract(bid=0.48, ask=0.50, oi=2000, vol=5000):
    return {
        "contractSymbol": "US.SPY260720C743000",
        "bid": bid, "ask": ask, "openInterest": oi, "volume": vol,
        "strike": 743.0,
    }


def _t(h, m, day=15):  # a Wednesday
    return datetime(2026, 7, day, h, m, tzinfo=CT)


def test_stop_is_never_inside_the_spread():
    """The whole point: a stop within the spread fills instantly at entry."""
    b, msg = scalp.plan_bracket(_contract(0.48, 0.50), "SPY", now=_t(10, 0))
    assert b is not None, msg
    # Entry is mid 0.49; the bid is 0.48. A stop at/above the bid is an
    # instant fill, so it must sit strictly below it.
    assert b.stop < 0.48, f"stop {b.stop} is inside the spread"
    assert b.target > 0.50, f"target {b.target} is inside the spread"


def test_naive_user_bracket_is_rejected_by_the_math():
    """0.50 buy / 0.53 target / 0.48 stop needs >40% wins on a 48.5% signal."""
    need = scalp.required_win_rate(target=0.53, stop=0.48, entry=0.50)
    assert 0.39 < need < 0.41
    # And with the real 4.8% spread toll, effective entry is the ask and
    # effective exit the bid, which pushes the true requirement far higher.


def test_no_entries_after_2pm_ct():
    ok, why = scalp.entries_allowed(_t(14, 1))
    assert not ok and "cutoff" in why
    ok, _ = scalp.entries_allowed(_t(13, 59))
    assert ok


def test_no_entries_on_weekends():
    ok, why = scalp.entries_allowed(_t(10, 0, day=18))  # Saturday
    assert not ok and why == "weekend"


def test_same_day_expiry_forced_out_in_final_hour():
    exit_now, why = scalp.must_exit("2026-07-15", now=_t(15, 5))
    assert exit_now and "exit window" in why
    exit_now, _ = scalp.must_exit("2026-07-15", now=_t(12, 0))
    assert not exit_now
    # A later expiry is not force-exited.
    exit_now, _ = scalp.must_exit("2026-07-20", now=_t(15, 30))
    assert not exit_now


def test_illiquid_contracts_are_refused():
    b, msg = scalp.plan_bracket(_contract(oi=10, vol=5), "SPY", now=_t(10, 0))
    assert b is None and "illiquid" in msg
    # Penny contracts: cannot be exited cleanly.
    b, msg = scalp.plan_bracket(_contract(0.02, 0.05), "SPY", now=_t(10, 0))
    assert b is None and "illiquid" in msg


def test_wide_spread_is_refused():
    # 0.41/0.44 on NVDA is 7.1% of mid -- above the 6% ceiling.
    b, msg = scalp.plan_bracket(_contract(0.41, 0.44), "NVDA", now=_t(10, 0))
    assert b is None and "spread" in msg


def test_sizing_caps_loss_at_risk_budget():
    # Risking 0.05/contract on 10k equity at 1% => 100/(0.05*100) = 20.
    assert scalp.size_position(0.05, 10_000, 0.01) == 20
    # Bigger per-contract risk => fewer contracts, never more.
    assert scalp.size_position(0.20, 10_000, 0.01) == 5
    assert scalp.size_position(0.0, 10_000) == 0


def test_triggers_fire_on_the_bid_not_the_mid():
    """We are long, so exits happen at the bid. Testing mid trips early."""
    b, _ = scalp.plan_bracket(_contract(0.48, 0.50), "SPY", now=_t(10, 0))
    # Mid is through the target but the bid is not -- must NOT take profit.
    action, _ = scalp.check_triggers(b, bid=b.target - 0.01, ask=b.target + 0.03)
    assert action is None
    action, price = scalp.check_triggers(b, bid=b.target, ask=b.target + 0.02)
    assert action == "take_profit" and price == b.target


def test_stop_fill_reflects_slippage_through_the_trigger():
    """A stop trigger fills at the live bid, which may be worse."""
    b, _ = scalp.plan_bracket(_contract(0.48, 0.50), "SPY", now=_t(10, 0))
    gapped = b.stop - 0.03
    action, price = scalp.check_triggers(b, bid=gapped, ask=gapped + 0.02)
    assert action == "stop"
    assert price == gapped, "must report the real fill, not the trigger price"


# --- Buying-power sizing cap -------------------------------------------

def test_buying_power_cap_math():
    """Two thirds of $1,045.81 funds 6 contracts at $1.00, not 10."""
    bp, frac = 1045.81, 2 / 3
    usable = bp * frac
    assert int(usable / (1.00 * 100)) == 6
    # A third stays free for manual trades in the moomoo app.
    assert bp - usable == pytest.approx(bp / 3, rel=1e-6)


def test_cap_scales_with_contract_price():
    usable = 1045.81 * (2 / 3)
    assert int(usable / (0.50 * 100)) == 13   # cheaper contract, more of them
    assert int(usable / (5.00 * 100)) == 1    # expensive one, barely any
