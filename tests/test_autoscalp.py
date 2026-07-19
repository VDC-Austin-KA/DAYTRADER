"""Burst detector tests -- the entry trigger must not fire on drift."""
from __future__ import annotations

import time

from app import autoscalp


def _reset():
    autoscalp._spot_path.clear()
    autoscalp.CACHE.spot = 0.0


def test_burst_up_and_down(monkeypatch):
    _reset()
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    autoscalp._record_spot(743.00)
    autoscalp._record_spot(743.70)          # +9.4 bps inside the window
    assert autoscalp.detect_burst() == "up"
    _reset()
    autoscalp._record_spot(743.00)
    autoscalp._record_spot(742.30)
    assert autoscalp.detect_burst() == "down"


def test_slow_drift_does_not_fire(monkeypatch):
    """The same size move spread over > window must NOT trigger."""
    _reset()
    t = {"now": time.time()}
    monkeypatch.setattr(time, "time", lambda: t["now"])
    autoscalp._record_spot(743.00)
    # Drift the same +9 bps but 10s per step, 90s total > 45s window.
    for i, px in enumerate([743.1, 743.2, 743.3, 743.4, 743.5, 743.6, 743.7]):
        t["now"] += 15
        autoscalp._record_spot(px)
    # Window only holds the last ~3 points: 743.5 -> 743.7 = 2.7 bps.
    assert autoscalp.detect_burst() is None


def test_empty_path_is_quiet():
    _reset()
    assert autoscalp.detect_burst() is None
