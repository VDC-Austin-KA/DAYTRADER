"""Backtest integrity tests.

The only bug class that matters here is lookahead: if a bar's score can see
the future, the backtest reports an edge that cannot exist live. These
tests are the guardrail on that.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.backtest import engine, signals


def _synthetic(n: int = 3000, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 0.001, n)
    close = 100 * np.exp(np.cumsum(steps))
    high = close * (1 + np.abs(rng.normal(0, 0.0005, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.0005, n)))
    return pd.DataFrame(
        {
            "Open": close, "High": high, "Low": low, "Close": close,
            "Volume": rng.uniform(1e5, 1e6, n),
        },
        index=pd.date_range("2025-01-02 09:30", periods=n, freq="1min"),
    )


def test_surge_has_no_lookahead():
    """A bar's score must not change when future bars are removed."""
    df = _synthetic()
    full = signals.compute_surge_series(df)
    cut = 2500
    truncated = signals.compute_surge_series(df.iloc[:cut])

    for col in ("surge", "squeeze", "burst", "momentum_z"):
        a = full[col].iloc[cut - 1]
        b = truncated[col].iloc[-1]
        assert a == pytest.approx(b, rel=1e-9, nan_ok=True), (
            f"{col} changed when future data was removed: {a} vs {b} "
            "-- this is lookahead and invalidates every result."
        )


def test_forward_returns_do_not_leak_into_entry():
    """Forward return at i must start at i, using only bars after it."""
    close = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0])
    fwd = engine._forward_returns(close, 2)
    # From index 0, two bars ahead is 102 -> +200 bps.
    assert fwd.iloc[0] == pytest.approx((102 / 100 - 1) * 10_000)
    # The last two entries have no future and must be NaN, not fabricated.
    assert fwd.iloc[-1] != fwd.iloc[-1]
    assert fwd.iloc[-2] != fwd.iloc[-2]


def test_split_is_chronological_and_disjoint():
    df = _synthetic(2000)
    frames = {"TEST": signals.compute_surge_series(df)}
    cutoff = str(df.index[1200])
    train, test = engine.split(frames, cutoff)
    assert train["TEST"].index.max() < test["TEST"].index.min()


def test_random_data_yields_no_significant_edge():
    """Sanity check on the scorer: pure noise must not look profitable.

    If this ever fails, the harness is manufacturing an edge and any real
    result it reports is suspect.
    """
    frames = {
        f"N{i}": signals.compute_surge_series(_synthetic(4000, seed=i))
        for i in range(3)
    }
    r = engine.evaluate(frames, threshold=60, horizon=15, label="noise")
    assert r.n_signals > 100
    assert abs(r.t_stat) < 3.0, (
        f"Found t={r.t_stat:.2f} on random walks -- the harness is biased."
    )
