"""Real-time condition-gate tests.

The gate's whole job is to refuse two regimes the backtests tie to losses:
chop (low efficiency) and bursts fought against a decisive trend. These pin
that it lets clean, aligned tape through and blocks both losers -- plus the
not-enough-data guard, since the daemon calls it the moment it starts up.
"""
from __future__ import annotations

from app.trading import conditions


def _ramp(start: float, step: float, n: int) -> list[tuple[float, float]]:
    """A clean one-directional path: efficiency == 1.0 by construction."""
    return [(float(i), start + step * i) for i in range(n)]


def _chop(base: float, amp: float, n: int) -> list[tuple[float, float]]:
    """A saw-tooth that ends where it started: efficiency near 0."""
    return [(float(i), base + (amp if i % 2 else -amp)) for i in range(n)]


def test_efficiency_is_one_for_a_clean_ramp_and_low_for_chop():
    up = [p for _, p in _ramp(500.0, 0.1, 12)]
    saw = [p for _, p in _chop(500.0, 0.2, 12)]
    assert conditions.kaufman_efficiency(up) == 1.0
    assert conditions.kaufman_efficiency(saw) < 0.2


def test_clean_uptrend_lets_a_call_through():
    a = conditions.assess(_ramp(500.0, 0.10, 12), "up")
    assert a.tradeable and a.aligned
    assert a.trend_bps > 0 and a.efficiency > 0.9


def test_clean_downtrend_lets_a_put_through():
    a = conditions.assess(_ramp(500.0, -0.10, 12), "down")
    assert a.tradeable and a.aligned
    assert a.trend_bps < 0


def test_chop_blocks_even_a_burst():
    a = conditions.assess(_chop(500.0, 0.30, 14), "up")
    assert not a.tradeable
    assert any("chop" in r for r in a.reasons)


def test_call_into_a_decisive_downtrend_is_refused():
    # A clean slide of ~ -24 bps; a call burst here is fighting the drift.
    a = conditions.assess(_ramp(500.0, -0.10, 12), "up")
    assert not a.tradeable and not a.aligned
    assert any("downtrend" in r for r in a.reasons)


def test_put_into_a_decisive_uptrend_is_refused():
    a = conditions.assess(_ramp(500.0, 0.10, 12), "down")
    assert not a.tradeable and not a.aligned
    assert any("uptrend" in r for r in a.reasons)


def test_mild_drift_does_not_block_an_aligned_burst():
    """A near-flat lean must not be treated as a decisive opposing trend."""
    # ~ +2 bps over the window: below the 12 bps oppose threshold, so a call
    # is allowed and a put is not blocked on trend grounds (only alignment).
    samples = _ramp(500.0, 0.008, 12)      # tiny positive slope, clean
    up = conditions.assess(samples, "up")
    assert up.tradeable, up.describe()


def test_not_enough_samples_is_a_hard_skip():
    a = conditions.assess(_ramp(500.0, 0.1, 3), "up")
    assert not a.tradeable and a.n == 3
    assert any("samples" in r for r in a.reasons)


def test_flat_tape_has_zero_efficiency_and_is_blocked_as_chop():
    flat = [(float(i), 500.0) for i in range(12)]
    a = conditions.assess(flat, "up")
    assert a.efficiency == 0.0 and not a.tradeable
