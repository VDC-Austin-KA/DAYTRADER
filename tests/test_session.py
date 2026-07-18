"""No-overnight rule tests.

The user's constraint is absolute: nothing is held overnight. These tests
pin both halves -- refusing late entries, and flattening what is open --
because either half alone leaves positions exposed to the gap.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.trading import session

CT = ZoneInfo("America/Chicago")


def _t(h, m, day=15):  # 2026-07-15 is a Wednesday
    return datetime(2026, 7, day, h, m, tzinfo=CT)


def test_entries_blocked_after_cutoff():
    ok, why = session.can_open(_t(14, 0))
    assert not ok and "last-entry" in why
    ok, _ = session.can_open(_t(13, 59))
    assert ok


def test_entries_blocked_before_open_and_on_weekends():
    ok, why = session.can_open(_t(7, 0))
    assert not ok and why == "pre-market"
    ok, why = session.can_open(_t(10, 0, day=18))  # Saturday
    assert not ok and why == "weekend"


def test_flatten_window_triggers_before_the_close():
    """Flatten must fire BEFORE the bell, not at it."""
    should, _ = session.must_flatten(_t(14, 44))
    assert not should
    should, why = session.must_flatten(_t(14, 45))
    assert should and "flatten window" in why
    assert session.FLATTEN_AT < session.MARKET_CLOSE


def test_position_surviving_into_weekend_flattens_immediately():
    """A weekend position is already a violation -- do not wait for Monday."""
    should, why = session.must_flatten(_t(11, 0, day=18))
    assert should and "outside a trading day" in why


def test_entry_cutoff_leaves_runway_before_flatten():
    """Anything openable must have time to be closed in the same session."""
    assert session.LAST_ENTRY < session.FLATTEN_AT
    runway = session.minutes_until_flatten(_t(13, 59))
    assert runway > 0


def test_expired_contracts_are_rejected():
    ok, why = session.validate_expiry("2026-07-14", now=_t(10, 0))
    assert not ok and "in the past" in why
    ok, _ = session.validate_expiry("2026-07-15", now=_t(10, 0))
    assert ok
    # A later expiry is allowed -- it is still closed intraday.
    ok, _ = session.validate_expiry("2026-08-21", now=_t(10, 0))
    assert ok
