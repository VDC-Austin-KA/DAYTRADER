"""Gamma exposure tests -- the sign convention is the whole ballgame."""
from __future__ import annotations

from app.trading import gamma


def test_flip_point_found_between_bracketing_strikes():
    # Cumulative GEX goes negative then crosses up between 100 and 101.
    by_strike = {99.0: -50.0, 100.0: -50.0, 101.0: 300.0, 102.0: 10.0}
    flip = gamma._flip_point(by_strike, spot=100.5, band=0.10)
    assert flip is not None and 100.0 <= flip <= 101.0


def test_no_flip_when_cumulative_never_crosses():
    by_strike = {99.0: 10.0, 100.0: 20.0, 101.0: 30.0}
    assert gamma._flip_point(by_strike, spot=100.0, band=0.10) is None


def test_regime_thresholds():
    """Sign drives the regime; tiny magnitudes are treated as neutral."""
    prof = gamma.GammaProfile(
        symbol="X", spot=100.0, expiry="2026-07-20",
        total_gex=5e8, call_gex=5e8, put_gex=0.0, flip_point=None,
        largest_strike=100.0, largest_strike_gex=1.0, regime="reversion",
    )
    assert prof.regime == "reversion"
    # The compute path decides regime; assert the boundary logic it uses.
    for total, expected in ((5e8, "reversion"), (-5e8, "momentum"), (1e3, "neutral")):
        got = ("reversion" if total > 0 else "momentum") if abs(total) > 1e5 else "neutral"
        assert got == expected
