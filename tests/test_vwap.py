"""VWAP opening-range signal tests: correctness, no-lookahead, live == backtest.

The one bug class that invalidates any backtest is lookahead. The rest pin
that the structure actually fires the way the method describes -- a break of
the opening range in the VWAP-agreed, trending direction -- and refuses chop.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtest import engine, vwap
from app.trading import strategy


def _session(prices, start="2026-03-02 09:30", vol=1e6):
    """One session of 1-min bars from a close path (H/L hug close)."""
    n = len(prices)
    idx = pd.date_range(start, periods=n, freq="1min")
    close = np.asarray(prices, dtype=float)
    return pd.DataFrame(
        {"Open": close, "High": close * 1.0002, "Low": close * 0.9998,
         "Close": close, "Volume": np.full(n, vol)},
        index=idx,
    )


def _trend_up_session(n=120, base=500.0, or_minutes=30):
    """Flat opening range, then a clean breakout to the upside."""
    p = np.full(n, base)
    # First `or_minutes` oscillate in a tight band -> defines the OR.
    p[:or_minutes] = base + np.sin(np.arange(or_minutes)) * 0.05
    # Then a steady ramp up: breaks the OR high, VWAP rises, high efficiency.
    p[or_minutes:] = base + 0.10 * np.arange(n - or_minutes)
    return _session(p)


def _chop_session(n=120, base=500.0):
    """Two-sided saw-tooth all day: never a clean trending break."""
    p = base + (np.arange(n) % 2) * 0.3
    return _session(p)


def test_no_lookahead_signal_is_truncation_stable():
    """A bar's signal must not change when future bars are removed."""
    df = _trend_up_session(n=120)
    full = vwap.compute_vwap_signal(df)
    cut = 90
    trunc = vwap.compute_vwap_signal(df.iloc[:cut])
    for col in ("surge", "direction", "vwap", "or_high"):
        a, b = full[col].iloc[cut - 1], trunc[col].iloc[-1]
        if isinstance(a, float) and np.isnan(a):
            assert np.isnan(b)
        else:
            assert a == b, f"{col} changed when future was removed: {a} vs {b}"


def test_no_signal_during_the_opening_range():
    df = _trend_up_session(or_minutes=30)
    sig = vwap.compute_vwap_signal(df, or_minutes=30)
    # Before the OR window closes there is nothing to break out of.
    assert (sig["surge"].iloc[:30] == 0).all()
    assert sig["or_high"].iloc[:30].isna().all()


def test_clean_uptrend_break_fires_a_call_once():
    df = _trend_up_session()
    sig = vwap.compute_vwap_signal(df)
    ups = sig.index[sig["direction"] == "up"]
    assert len(ups) >= 1, "a clean OR breakout over rising VWAP should fire"
    # Debounced: the first firing bar carries surge, and it is a rising edge
    # (the bar before it did not already fire).
    first = sig.index.get_loc(ups[0])
    assert sig["surge"].iloc[first] == 100
    assert sig["surge"].iloc[first - 1] == 0


def test_chop_does_not_fire():
    df = _chop_session()
    sig = vwap.compute_vwap_signal(df, min_efficiency=0.40)
    assert (sig["surge"] == 0).all(), "two-sided chop must not signal"


def test_frame_drops_into_engine_evaluate():
    frames = {"SPY": vwap.compute_vwap_signal(_trend_up_session(n=200))}
    r = engine.evaluate(frames, threshold=50, horizon=10, label="vwap")
    # It runs and only counts the debounced decisions, not every bar.
    assert r.n_signals >= 1


def test_live_assess_entry_matches_the_backtest_rule():
    """strategy.assess_entry must equal the backtest signal on the same bars."""
    df = _trend_up_session()
    sig = vwap.compute_vwap_signal(df)
    ups = sig.index[sig["direction"] == "up"]
    assert len(ups) >= 1
    # Feed the session up to and including the first firing bar.
    end = sig.index.get_loc(ups[0])
    decision = strategy.assess_entry(df.iloc[: end + 1])
    assert decision.side == "call", decision.describe()
    # One bar earlier there is no fresh break.
    assert strategy.assess_entry(df.iloc[:end]).side is None


def test_assess_entry_guards_short_history():
    assert strategy.assess_entry(None).side is None
    assert strategy.assess_entry(_session([500.0])).side is None
