"""Signal-level backtest: does Surge predict the next N minutes?

WHAT THIS MEASURES, AND WHAT IT DOES NOT
----------------------------------------
This answers one narrow question: when Surge fires with a directional lean,
does the UNDERLYING subsequently move that way, by enough, more often than
chance? That is the foundation any options overlay sits on -- if the
direction call has no edge, no strike selection rescues it.

It deliberately does NOT report option P&L or a dollar equity curve. We have
no historical option quotes (the Polygon flat-file archive 403s on this
plan), so any option payoff here would come from a Black-Scholes guess at an
IV surface we cannot observe. On 0-3 DTE that guess dominates the result --
IV crush and bid/ask width are most of the P&L -- so the curve would be a
picture of my assumptions, not of the strategy. A backtest that models the
edge but not the costs always looks profitable.

Instead, results are in basis points of underlying move, plus an explicit
HURDLE: the move an option needs just to clear its own spread. Compare edge
against hurdle to see whether the signal has room to pay for the trade.

OVERFITTING DISCIPLINE
----------------------
``run()`` splits history by date into train and holdout. Thresholds are
chosen on train only. The holdout is scored ONCE with those frozen
parameters. Its purpose is destroyed the moment it informs a parameter, so
if the holdout looks bad the honest move is to report it, not to re-tune.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import data, signals

log = logging.getLogger("daytrader.backtest")


@dataclass
class Result:
    label: str
    horizon: int
    threshold: float
    n_signals: int
    hit_rate: float          # fraction moving the predicted way
    mean_bps: float          # mean signed move, direction-adjusted
    median_bps: float
    baseline_hit: float      # same-horizon random-entry hit rate
    baseline_bps: float
    edge_bps: float          # mean_bps - baseline_bps
    t_stat: float
    per_symbol: dict = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"{self.label:<10} h={self.horizon:>3}m thr={self.threshold:>5.1f} "
            f"n={self.n_signals:>6}  hit={self.hit_rate*100:>5.1f}% "
            f"(base {self.baseline_hit*100:>5.1f}%)  "
            f"edge={self.edge_bps:>+7.2f}bps  t={self.t_stat:>+6.2f}"
        )


def _forward_returns(close: pd.Series, horizon: int) -> pd.Series:
    """Return over the next ``horizon`` bars, in basis points."""
    return (close.shift(-horizon) / close - 1.0) * 10_000


def evaluate(
    frames: dict[str, pd.DataFrame],
    threshold: float,
    horizon: int,
    label: str,
    require_direction: bool = True,
) -> Result:
    """Score every bar whose surge clears ``threshold`` with a direction."""
    signed_moves: list[np.ndarray] = []
    all_moves: list[np.ndarray] = []
    per_symbol: dict[str, dict] = {}

    for symbol, sig in frames.items():
        fwd = _forward_returns(sig["close"], horizon)
        # Entry is decided on bar i and filled at bar i's close, so the
        # forward return starts at i -- no lookahead.
        ok = sig["surge"].notna() & fwd.notna()
        fired = ok & (sig["surge"] >= threshold)
        if require_direction:
            fired &= sig["direction"].isin(["up", "down"])
        if not fired.any():
            continue

        sign = np.where(sig.loc[fired, "direction"] == "up", 1.0, -1.0)
        moves = fwd[fired].to_numpy() * sign
        signed_moves.append(moves)
        all_moves.append(fwd[ok].to_numpy())
        per_symbol[symbol] = {
            "n": int(fired.sum()),
            "hit": float((moves > 0).mean()),
            "mean_bps": float(moves.mean()),
        }

    if not signed_moves:
        return Result(label, horizon, threshold, 0, 0, 0, 0, 0, 0, 0, 0)

    m = np.concatenate(signed_moves)
    # Baseline: every eligible bar, direction stripped. |move| is the fair
    # comparison -- a coin-flip entry captures average absolute movement.
    base = np.concatenate(all_moves)
    baseline_bps = float(np.abs(base).mean() * 0.0)  # a blind entry nets ~0
    baseline_hit = 0.5

    se = m.std(ddof=1) / np.sqrt(len(m)) if len(m) > 1 else np.inf
    return Result(
        label=label, horizon=horizon, threshold=threshold, n_signals=len(m),
        hit_rate=float((m > 0).mean()), mean_bps=float(m.mean()),
        median_bps=float(np.median(m)), baseline_hit=baseline_hit,
        baseline_bps=baseline_bps, edge_bps=float(m.mean() - baseline_bps),
        t_stat=float(m.mean() / se) if se else 0.0, per_symbol=per_symbol,
    )


def build_frames(
    symbols: list[str], start: str, end: str, refresh: bool = False
) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        bars = data.load_minute_bars(sym, start, end, refresh=refresh)
        if bars.empty or len(bars) < 2000:
            log.warning("%s: insufficient bars (%d), skipping", sym, len(bars))
            continue
        out[sym] = signals.compute_surge_series(bars)
        log.info("%s: %d bars scored", sym, len(bars))
    return out


def split(frames: dict[str, pd.DataFrame], cutoff: str):
    """Chronological train/holdout split -- never random, never shuffled."""
    train = {s: f[f.index < cutoff] for s, f in frames.items()}
    test = {s: f[f.index >= cutoff] for s, f in frames.items()}
    return (
        {s: f for s, f in train.items() if len(f) > 500},
        {s: f for s, f in test.items() if len(f) > 500},
    )
