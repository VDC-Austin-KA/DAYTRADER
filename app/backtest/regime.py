"""Regime-conditioned backtest: does a regime filter rescue the signals?

Historical dealer GEX cannot be reconstructed without historical option OI
(arrives with the Polygon entitlement). These PROXIES, computable from the
underlying alone, track the same states the literature ties to dealer
positioning:

* trail_ret  -- sign/size of the trailing session return. Gamma tends
                negative after selloffs (customers own puts, dealers short
                them), positive in calm rallies.
* atr_pctile -- realized-vol percentile. Short-gamma tape is high-vol tape.
* efficiency -- Kaufman ratio of net move to path length. Long-gamma pinning
                shows up as low efficiency (lots of path, no progress).

Each proxy splits bars into two regimes. In each regime we evaluate BOTH
primitive strategies:

* momentum:  follow the last N-bar move
* reversion: fade the last N-bar move

If the gamma-regime hypothesis is right, momentum should work where the
proxies say "short-gamma-like" and reversion where they say "long-gamma-
like" -- and the unconditional flatness of the original backtest should
resolve into two opposing conditional edges.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import engine


def add_regime_columns(sig: pd.DataFrame, bars_per_day: int = 390) -> pd.DataFrame:
    """Append regime proxy columns. Trailing windows only -- no lookahead."""
    out = sig.copy()
    close = out["close"]

    # Trailing one-session return.
    out["trail_ret"] = close.pct_change(bars_per_day)

    # ATR percentile over ~5 sessions of its own history.
    out["atr_pctile"] = out["atr_pct"].rolling(5 * bars_per_day).rank(pct=True)

    # 30-bar efficiency: |net| / path.
    diffs = close.diff().abs()
    path = diffs.rolling(30).sum()
    net = (close - close.shift(30)).abs()
    out["eff30"] = (net / path.clip(lower=1e-9)).fillna(0.5)
    return out


def primitive_signal(sig: pd.DataFrame, lookback: int, kind: str) -> pd.Series:
    """Direction from the last ``lookback`` bars: follow it or fade it."""
    move = sig["close"].pct_change(lookback)
    raw = np.sign(move)
    if kind == "reversion":
        raw = -raw
    return pd.Series(raw, index=sig.index)


def evaluate_conditional(
    frames: dict[str, pd.DataFrame],
    regime_col: str,
    threshold: float,
    above_is: str,          # which strategy the regime >= threshold gets
    lookback: int,
    horizon: int,
    min_move: float = 0.0005,   # ignore sub-5bp trailing moves (no signal)
) -> dict[str, engine.Result]:
    """Score momentum & reversion in each half of a regime split."""
    results: dict[str, list[np.ndarray]] = {
        "above_mom": [], "above_rev": [], "below_mom": [], "below_rev": [],
    }
    for sym, sig in frames.items():
        fwd = (sig["close"].shift(-horizon) / sig["close"] - 1.0) * 10_000
        move = sig["close"].pct_change(lookback)
        ok = fwd.notna() & move.notna() & sig[regime_col].notna()
        ok &= move.abs() >= min_move
        above = ok & (sig[regime_col] >= threshold)
        below = ok & (sig[regime_col] < threshold)
        direction = np.sign(move)
        for mask, tag in ((above, "above"), (below, "below")):
            if not mask.any():
                continue
            mom = (fwd[mask] * direction[mask]).to_numpy()
            results[f"{tag}_mom"].append(mom)
            results[f"{tag}_rev"].append(-mom)

    out: dict[str, engine.Result] = {}
    for key, chunks in results.items():
        if not chunks:
            continue
        m = np.concatenate(chunks)
        se = m.std(ddof=1) / np.sqrt(len(m)) if len(m) > 1 else np.inf
        out[key] = engine.Result(
            label=key, horizon=horizon, threshold=threshold,
            n_signals=len(m), hit_rate=float((m > 0).mean()),
            mean_bps=float(m.mean()), median_bps=float(np.median(m)),
            baseline_hit=0.5, baseline_bps=0.0, edge_bps=float(m.mean()),
            t_stat=float(m.mean() / se) if se else 0.0,
        )
    return out
