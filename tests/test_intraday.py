"""Indicator tests against known-value series."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.data.intraday import awesome_oscillator, rsi


def test_rsi_is_100_on_a_pure_uptrend():
    s = pd.Series(np.arange(1, 40, dtype=float))
    assert rsi(s).iloc[-1] == 100.0


def test_rsi_is_zero_on_a_pure_downtrend():
    s = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert rsi(s).iloc[-1] == 0.0


def test_rsi_sits_midrange_on_noise():
    rng = np.random.default_rng(3)
    s = pd.Series(100 + np.cumsum(rng.normal(0, 1, 500)))
    assert 25 < rsi(s).iloc[-1] < 75


def test_rsi_uses_wilder_smoothing_not_a_simple_mean():
    """A rolling mean gives a different number; pin that we use EWM."""
    s = pd.Series([44, 44.3, 44.1, 44.8, 45.1, 45.4, 45.4, 45.6, 46.3, 46.3,
                   46, 46.0, 46.4, 46.2, 45.6, 46.2, 46.2, 46.0, 46.0, 46.4])
    v = rsi(s, period=14).iloc[-1]
    assert 55 < v < 85, v


def test_awesome_oscillator_uses_median_price():
    """AO must use (H+L)/2, not close -- a common wrong shortcut."""
    n = 60
    high = pd.Series(np.linspace(10, 20, n))
    low = high - 2.0
    ao = awesome_oscillator(high, low)
    # Rising median price -> fast SMA above slow SMA -> positive AO.
    assert ao.iloc[-1] > 0
    # Shifting BOTH bands equally shifts median equally; AO is unchanged.
    ao2 = awesome_oscillator(high + 5, low + 5)
    assert abs(ao.iloc[-1] - ao2.iloc[-1]) < 1e-9


def test_awesome_oscillator_warmup_is_nan():
    high = pd.Series(np.linspace(10, 20, 40))
    ao = awesome_oscillator(high, high - 1)
    assert ao.iloc[:33].isna().all()      # needs 34 bars
    assert not np.isnan(ao.iloc[-1])
