"""Walk-forward evaluation: is the edge CONSISTENT, or one lucky window?

A single train/holdout split answers "did it work on one out-of-sample
period?". For a rule-based strategy with no fitted parameters, the sharper and
more honest question is whether the SAME frozen rule holds up across many
consecutive out-of-sample windows. An edge that shows up in one fold and
reverses in the next is noise wearing a t-stat.

This cuts the history into ``n_folds`` contiguous, disjoint time slices and
scores each with ``engine.evaluate``. Consistency across folds -- most folds
positive, the aggregate t-stat surviving -- is the robustness signal. Any fold
can be read on its own; nothing here is fitted, so every fold is out of sample.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import engine


@dataclass
class WalkForwardResult:
    folds: list = field(default_factory=list)          # list[engine.Result]
    bounds: list = field(default_factory=list)          # list[(start, end)]

    @property
    def n_positive(self) -> int:
        return sum(1 for f in self.folds if f.mean_bps > 0)

    @property
    def aggregate_t(self) -> float:
        """Inverse-variance-ish summary: mean of fold t-stats, penalised by
        their spread. Positive-and-stable beats one big spike."""
        ts = [f.t_stat for f in self.folds if f.n_signals > 1]
        if not ts:
            return 0.0
        arr = np.asarray(ts)
        return float(arr.mean() / (arr.std(ddof=1) + 1.0)) if len(arr) > 1 else float(arr[0])

    def summary(self) -> str:
        lines = [
            f"walk-forward: {len(self.folds)} folds, "
            f"{self.n_positive}/{len(self.folds)} positive, "
            f"stability-adjusted t={self.aggregate_t:+.2f}"
        ]
        for (lo, hi), f in zip(self.bounds, self.folds):
            lines.append(
                f"  {lo}..{hi}: n={f.n_signals:>4} hit={f.hit_rate*100:5.1f}% "
                f"mean={f.mean_bps:+7.2f}bps t={f.t_stat:+5.2f}"
            )
        return "\n".join(lines)


def _time_bounds(frames: dict[str, pd.DataFrame], n_folds: int):
    starts = [f.index.min() for f in frames.values() if len(f)]
    ends = [f.index.max() for f in frames.values() if len(f)]
    lo, hi = min(starts), max(ends)
    # Equal-width time edges; ordinals keep it robust to uneven bar density.
    edges = pd.to_datetime(np.linspace(lo.value, hi.value, n_folds + 1))
    return [(edges[i], edges[i + 1]) for i in range(n_folds)]


def walk_forward(
    frames: dict[str, pd.DataFrame],
    n_folds: int = 5,
    threshold: float = 50.0,
    horizon: int = 15,
) -> WalkForwardResult:
    """Score ``frames`` across ``n_folds`` contiguous out-of-sample windows."""
    res = WalkForwardResult()
    for lo, hi in _time_bounds(frames, n_folds):
        sub = {
            s: f[(f.index >= lo) & (f.index < hi)]
            for s, f in frames.items()
        }
        sub = {s: f for s, f in sub.items() if len(f) > 200}
        r = engine.evaluate(sub, threshold=threshold, horizon=horizon,
                            label=f"{lo.date()}")
        res.folds.append(r)
        res.bounds.append((str(lo.date()), str(hi.date())))
    return res
