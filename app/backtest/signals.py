"""Intraday Surge Score, vectorised over a whole bar history.

This mirrors ``app.trading.movers.compute_surge`` component for component --
squeeze 40%, burst 30%, momentum 30%, plus the whipsaw gauge -- but computes
every bar's score at once so a backtest can replay years in seconds, and on
an intraday clock rather than a daily one.

Every window is expressed in BARS and every value at index ``i`` uses only
data up to and including ``i``. That is the whole ballgame for a backtest:
one lookahead (a rolling window that peeks, a percentile over the full
sample) silently manufactures an edge that does not exist live. The unit
test in tests/test_backtest.py pins this by scoring a truncated series and
asserting the last value is unchanged.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute_surge_series(
    df: pd.DataFrame,
    ma_window: int = 20,
    atr_window: int = 14,
    pct_window: int = 390,      # ~1 session of 1-min bars
    roc_window: int = 5,
    z_window: int = 390,
) -> pd.DataFrame:
    """Per-bar surge components. Returns a frame aligned to ``df``."""
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]

    # --- Squeeze: rolling percentile of Bollinger width, trailing only.
    ma = close.rolling(ma_window).mean()
    sd = close.rolling(ma_window).std()
    width = (4 * sd) / ma
    # Fraction of the trailing window that was WIDER than now: 1.0 = tightest.
    # rolling().rank(pct=True) gives the fraction <= current, so invert it.
    # (Equivalent to the apply() form but vectorised -- an apply here is
    # ~390 python calls per bar and turns a minute-level run into hours.)
    squeeze = 1.0 - width.rolling(pct_window).rank(pct=True)

    # --- Burst: move vs own ATR, confirmed by volume.
    prev_close = close.shift(1)
    tr = np.maximum(
        high - low, np.maximum((high - prev_close).abs(), (low - prev_close).abs())
    )
    atr_pct = tr.rolling(atr_window).mean() / close
    bar_ret = close.pct_change()
    range_burst = (bar_ret.abs() / atr_pct.clip(lower=1e-4)).clip(upper=2.5) / 2.5
    vol_ratio = vol / vol.rolling(ma_window).mean().clip(lower=1.0)
    vol_burst = vol_ratio.clip(upper=3.0) / 3.0
    burst = 0.6 * range_burst + 0.4 * vol_burst

    # --- Momentum: z-score of N-bar ROC against its own trailing window.
    roc = close.pct_change(roc_window)
    mom_z = (roc - roc.rolling(z_window).mean()) / roc.rolling(z_window).std().clip(
        lower=1e-9
    )
    momentum = mom_z.abs().clip(upper=3.0) / 3.0

    surge = 100 * (0.40 * squeeze + 0.30 * burst + 0.30 * momentum)

    # --- Directional lean: momentum sign confirmed by band position.
    band_pos = ((close - ma) / (2 * sd).clip(lower=1e-9)).clip(-2, 2)
    lean = 0.7 * np.sign(mom_z) * mom_z.abs().clip(upper=2) + 0.3 * band_pos
    direction = pd.Series(
        np.where(lean > 0.25, "up", np.where(lean < -0.25, "down", "neutral")),
        index=df.index,
    )

    # --- Whipsaw: net move small relative to path travelled, ranges hot.
    diffs = close.diff().abs()
    path = diffs.rolling(10).sum()
    net = (close - close.shift(10)).abs()
    efficiency = (net / path.clip(lower=1e-9)).fillna(1.0)
    atr_hot = atr_pct > atr_pct.rolling(pct_window).median()
    whipsaw = (efficiency < 0.35) & atr_hot

    return pd.DataFrame({
        "close": close, "surge": surge, "squeeze": squeeze, "burst": burst,
        "momentum_z": mom_z, "direction": direction, "whipsaw": whipsaw,
        "efficiency": efficiency, "atr_pct": atr_pct, "vol_ratio": vol_ratio,
    })
