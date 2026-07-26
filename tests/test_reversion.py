"""Exhaustion-reversion + router tests.

The danger with any fade model is that it degenerates into fighting trends.
These pin the guards: extension alone never fires, a clean trend day is never
faded, and the router can never emit two models on one bar.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtest import reversion, router
from app.trading import brackets


def _frame(close, high=None, low=None, vol=None,
           start="2026-03-02 09:30"):
    n = len(close)
    close = np.asarray(close, dtype=float)
    idx = pd.date_range(start, periods=n, freq="1min")
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.0005 if high is None else np.asarray(high, float),
            "Low": close * 0.9995 if low is None else np.asarray(low, float),
            "Close": close,
            "Volume": np.full(n, 1e6) if vol is None else np.asarray(vol, float),
        },
        index=idx,
    )


def _blowoff_top(n=120, base=500.0):
    """Chop to build a VWAP base, then a vertical spike that gets rejected."""
    p = base + np.sin(np.arange(n) * 0.7) * 0.25      # ranging base
    spike = np.linspace(0, 4.0, 18)                    # sharp extension up
    p[n - 18:] = base + spike
    high = p.copy()
    low = p.copy() * 0.9995
    vol = np.full(n, 1e6)
    # Climax volume + a long upper wick on the final print: the rejection.
    vol[-1] = 6e6
    high[-1] = p[-1] + 1.2
    low[-1] = p[-1] - 0.05
    p[-1] = p[-1] - 0.9                                # closes off the high
    return _frame(p, high=high, low=low, vol=vol)


def _clean_trend_day(n=140, base=500.0):
    """A relentless one-way ramp: the tape you must never fade."""
    return _frame(base + 0.14 * np.arange(n))


def test_vwap_sigma_bands_are_causal_and_nonnegative():
    df = _blowoff_top()
    vwap, sigma = reversion.session_vwap_bands(df)
    assert (sigma.dropna() >= 0).all()
    cut = 80
    v2, s2 = reversion.session_vwap_bands(df.iloc[:cut])
    assert vwap.iloc[cut - 1] == v2.iloc[-1]
    assert sigma.iloc[cut - 1] == s2.iloc[-1]


def test_no_lookahead_in_reversion_signal():
    df = _blowoff_top()
    full = reversion.compute_reversion_signal(df)
    cut = 90
    trunc = reversion.compute_reversion_signal(df.iloc[:cut])
    for col in ("surge", "direction", "band_z", "rsi"):
        a, b = full[col].iloc[cut - 1], trunc[col].iloc[-1]
        if isinstance(a, float) and np.isnan(a):
            assert np.isnan(b)
        else:
            assert a == b, f"{col} changed when future removed: {a} vs {b}"


def test_clean_trend_day_is_never_faded():
    """The critical guard: a high-efficiency ramp must produce no fade."""
    sig = reversion.compute_reversion_signal(_clean_trend_day())
    assert (sig["surge"] == 0).all(), "fading a clean trend is the way to ruin"


def test_extension_alone_does_not_fire():
    """Stretched but with no exhaustion evidence => no trade."""
    df = _blowoff_top()
    # Demand all three confirmations; the constructed bar has at most climax
    # + rejection, so requiring divergence too must silence it.
    sig = reversion.compute_reversion_signal(df, min_confirms=3,
                                             divergence_margin=90.0)
    assert (sig["surge"] == 0).all()


def test_blowoff_top_is_detected_as_a_short():
    df = _blowoff_top()
    sig = reversion.compute_reversion_signal(df, min_confirms=2, min_z=1.0)
    fired = sig[sig["surge"] >= 50]
    assert len(fired) >= 1, "a climactic, rejected spike should read as a top"
    assert (fired["direction"] == "down").all(), "a top is faded with puts"
    assert (fired["band_z"] > 0).all()


def test_signals_are_debounced_to_one_per_exhaustion():
    df = _blowoff_top()
    sig = reversion.compute_reversion_signal(df, min_confirms=2, min_z=1.0)
    fires = (sig["surge"] >= 50).sum()
    assert fires <= 3, f"{fires} fires on one exhaustion is not debounced"


def test_router_never_emits_two_models_on_one_bar():
    for maker in (_blowoff_top, _clean_trend_day):
        out = router.compute_routed_signal(maker())
        fired = out[out["surge"] >= 50]
        assert fired["model"].isin(["continuation", "reversion"]).all()
        # model is a single label per bar by construction; assert non-empty
        # whenever a signal fired, so a fire can always be attributed.
        assert (fired["model"] != "").all()


def test_router_disable_flags_are_respected():
    df = _blowoff_top()
    only_cont = router.compute_routed_signal(df, enable_reversion=False)
    assert (only_cont.loc[only_cont["surge"] >= 50, "model"]
            != "reversion").all()
    only_rev = router.compute_routed_signal(df, enable_continuation=False)
    assert (only_rev.loc[only_rev["surge"] >= 50, "model"]
            != "continuation").all()


def test_router_counts_contention_instead_of_assuming_none():
    """Overlap is rare, not impossible -- it must be surfaced, never hidden."""
    df = _blowoff_top()
    out = router.compute_routed_signal(df, max_efficiency=0.99)
    assert "contended" in out.attrs
    assert out.attrs["contended"] >= 0
    cov = router.coverage({"SPY": out})
    assert "contended" in cov


def test_coverage_reports_the_reversion_share():
    df = _blowoff_top()
    frames = {"SPY": router.compute_routed_signal(df, max_efficiency=0.99)}
    cov = router.coverage(frames)
    assert "total" in cov and "reversion_share" in cov
    assert 0.0 <= cov["reversion_share"] <= 1.0


# --- Defined-target exit (what a fade needs instead of a trail) ----------

def test_take_profit_banks_the_whole_position_at_target():
    st = brackets.BracketState(position_id=1, entry_price=0.40, quantity=10,
                               take_profit_gain=0.40)
    assert brackets.check(st, 0.55).kind == "none"      # 0.56 is the target
    a = brackets.check(st, 0.56)
    assert a.kind == "take_profit" and a.sell_qty == 10 and st.closed


def test_take_profit_outranks_scale_out_and_leaves_no_runner():
    """A fade has a destination; it should not leave a runner to give back."""
    st = brackets.BracketState(position_id=1, entry_price=0.40, quantity=10,
                               take_profit_gain=0.40)   # target 0.56
    a = brackets.check(st, 0.75)      # also past the +75% scale price (0.70)
    assert a.kind == "take_profit" and a.sell_qty == 10


def test_hard_stop_still_outranks_the_target():
    st = brackets.BracketState(position_id=1, entry_price=0.40, quantity=10,
                               take_profit_gain=0.40)
    a = brackets.check(st, 0.20)
    assert a.kind == "hard_stop"


def test_no_target_preserves_the_open_ended_runner():
    """Continuation trades must be untouched: scale out, then trail."""
    st = brackets.BracketState(position_id=1, entry_price=0.40, quantity=10)
    assert brackets.check(st, 0.70).kind == "scale_out"
    assert not st.closed and st.remaining == 5


def test_mean_target_sits_between_entry_and_the_mean():
    t = reversion.mean_target(row_vwap=500.0, entry_px=504.0, capture=0.75)
    assert 500.0 < t < 504.0
    assert t == 501.0
