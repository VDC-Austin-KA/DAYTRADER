"""Confluence signal + walk-forward tests.

Confluence can only REMOVE trades the base VWAP-OR signal proposed, never add
them. These pin that: a clean, trending, non-exhausted break survives; a
blow-off (exhausted RSI) is filtered even though the base would fire; the
gate never invents a trade; no lookahead; live == backtest; and the
walk-forward harness produces disjoint, chronological folds.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtest import confluence, vwap, walkforward
from app.trading import strategy


def _session(prices, start="2026-03-02 09:30", vol=1e6):
    n = len(prices)
    idx = pd.date_range(start, periods=n, freq="1min")
    close = np.asarray(prices, dtype=float)
    return pd.DataFrame(
        {"Open": close, "High": close * 1.0002, "Low": close * 0.9998,
         "Close": close, "Volume": np.full(n, vol)},
        index=idx,
    )


def _breakout_with_pullbacks(n=130, base=500.0, or_minutes=30):
    """OR band, then an up-move WITH pullbacks so RSI stays healthy."""
    p = np.full(n, base, dtype=float)
    p[:or_minutes] = base + np.sin(np.arange(or_minutes)) * 0.05
    steps = np.tile([0.25, 0.25, -0.12], n)[: n - or_minutes]
    p[or_minutes:] = base + np.cumsum(steps)
    return _session(p)


def _blowoff(n=130, base=500.0, or_minutes=30):
    """OR band, then a straight vertical ramp -> RSI pegs, exhaustion."""
    p = np.full(n, base, dtype=float)
    p[:or_minutes] = base + np.sin(np.arange(or_minutes)) * 0.05
    p[or_minutes:] = base + 0.30 * np.arange(n - or_minutes)
    return _session(p)


def test_confluence_only_removes_never_adds():
    df = _breakout_with_pullbacks()
    base = vwap.compute_vwap_signal(df)
    conf = confluence.compute_confluence_signal(df)
    base_fire = base["surge"] >= 50
    conf_fire = conf["surge"] >= 50
    # Every confluence entry must be a subset of the base entries.
    assert (conf_fire & ~base_fire).sum() == 0


def test_healthy_breakout_survives_confluence():
    df = _breakout_with_pullbacks()
    conf = confluence.compute_confluence_signal(df)
    assert (conf["direction"] == "up").any(), "a clean, non-exhausted break should pass"


def test_blowoff_is_filtered_by_rsi_even_though_base_fires():
    df = _blowoff()
    base = vwap.compute_vwap_signal(df)
    conf = confluence.compute_confluence_signal(df)
    assert (base["surge"] >= 50).any(), "base should fire on the raw break"
    # The vertical move pegs RSI above the healthy band -> confluence refuses.
    fired = conf.index[conf["surge"] >= 50]
    for ts in fired:
        assert conf.loc[ts, "rsi"] <= 85.0


def test_confluence_has_no_lookahead():
    df = _breakout_with_pullbacks()
    full = confluence.compute_confluence_signal(df)
    cut = 100
    trunc = confluence.compute_confluence_signal(df.iloc[:cut])
    for col in ("surge", "direction", "rsi", "ema_fast"):
        a, b = full[col].iloc[cut - 1], trunc[col].iloc[-1]
        if isinstance(a, float) and np.isnan(a):
            assert np.isnan(b)
        else:
            assert a == b, f"{col} changed when future removed: {a} vs {b}"


def test_live_confluence_matches_backtest():
    df = _breakout_with_pullbacks()
    conf = confluence.compute_confluence_signal(df)
    ups = conf.index[conf["direction"] == "up"]
    assert len(ups) >= 1
    end = conf.index.get_loc(ups[0])
    d = strategy.assess_entry_confluence(df.iloc[: end + 1])
    assert d.side == "call", d.describe()


def test_walk_forward_folds_are_chronological_and_disjoint():
    # ~6 sessions of bars so folds have data.
    days = []
    for k in range(6):
        s = _breakout_with_pullbacks()
        s.index = s.index + pd.Timedelta(days=k)
        days.append(confluence.compute_confluence_signal(
            _session(s["Close"].to_numpy(), start=str(s.index[0]))))
    frame = pd.concat(days)
    wf = walkforward.walk_forward({"SPY": frame}, n_folds=3, horizon=5)
    assert len(wf.folds) == 3
    # Bounds are ordered and non-overlapping.
    for i in range(len(wf.bounds) - 1):
        assert wf.bounds[i][1] <= wf.bounds[i + 1][0]
