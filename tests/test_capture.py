"""Tape-aggression stats: the feature price/volume data cannot provide."""
from __future__ import annotations

from collections import deque

from app.capture import _tape_stats


def test_imbalance_signs_correctly():
    now = 1000.0
    dq = deque([
        {"t": now, "v": 30.0, "dir": "BUY"},
        {"t": now, "v": 10.0, "dir": "SELL"},
    ])
    s = _tape_stats(dq, now)
    assert s["tape_imbalance"] == 0.5      # (30-10)/40
    assert s["tape_buy_vol"] == 30.0 and s["tape_sell_vol"] == 10.0


def test_old_prints_fall_out_of_the_window():
    now = 1000.0
    dq = deque([
        {"t": now - 500, "v": 999.0, "dir": "BUY"},   # stale
        {"t": now, "v": 10.0, "dir": "SELL"},
    ])
    s = _tape_stats(dq, now)
    assert s["tape_n"] == 1 and s["tape_imbalance"] == -1.0


def test_empty_tape_is_neutral_not_an_error():
    s = _tape_stats(deque(), 1000.0)
    assert s["tape_n"] == 0 and s["tape_imbalance"] == 0.0
