"""Notification tests: no trade may happen silently."""
from __future__ import annotations

from app.trading import notify


def setup_function():
    notify.clear()


def test_events_are_ordered_and_incremental():
    a = notify.record("entry", "BOUGHT 2 x SPY")
    b = notify.record("exit", "SOLD 2 x SPY")
    assert b["id"] > a["id"] == 1
    assert notify.latest_id() == b["id"]


def test_since_returns_only_newer_events():
    first = notify.record("entry", "one")
    notify.record("exit", "two")
    fresh = notify.since(after_id=first["id"])
    assert [e["title"] for e in fresh] == ["two"]
    assert notify.since(after_id=notify.latest_id()) == []


def test_ring_buffer_bounds_memory():
    for i in range(notify._MAX + 25):
        notify.record("entry", f"t{i}")
    assert len(notify._events) == notify._MAX
    # ids keep incrementing so the dashboard cursor never goes backwards.
    assert notify.latest_id() == notify._MAX + 25


def test_extra_fields_are_carried():
    e = notify.record("exit", "SOLD", "detail", level="trade",
                      code="US.SPY260720C743000", qty=2, pnl=-12.5)
    assert e["code"].endswith("C743000") and e["qty"] == 2 and e["pnl"] == -12.5
