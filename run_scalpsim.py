"""CLI for the scalp simulator: one config or a grid slice, JSON out.

Usage:
  python run_scalpsim.py '{"burst_bps": 8}'                 # one config, train
  python run_scalpsim.py '{"grid": {...}}' --out slice.json # sweep a slice
  python run_scalpsim.py '{"burst_bps": 8}' --holdout       # holdout period
  python run_scalpsim.py '{"burst_bps": 8}' --stress        # cost robustness

Tuning the time-based exits (see app/trading/brackets.py):
  python run_scalpsim.py '{"grid": {"give_up_minutes": [0, 10, 15, 20],
                                     "give_up_progress": [0.1, 0.2]}}'
Pick on train; confirm the winner ONCE on --holdout. The give-up rule is a
loss-cutter -- look for a higher profit_factor / less-negative avg_loss and a
better sharpe, not just a bigger total, and check --stress before believing it.

``--stress`` re-runs the chosen config across an IV / IV-crush / spread grid.
Option premiums here are modelled, not observed, so a config that only wins at
one IV/spread point is a picture of that assumption, not an edge. A rule worth
trading survives the whole cost envelope.
"""
import itertools
import json
import sys

import pandas as pd

from app.backtest import data as bdata
from app.backtest.scalpsim import SimParams, simulate

TRAIN = ("2025-01-01", "2026-02-28")
HOLDOUT = ("2026-03-01", "2026-07-17")

# Cost envelope for --stress: the assumptions the modelled P&L is most
# sensitive to on 0DTE. Every combination is run and reported.
STRESS_GRID = {
    "iv": [0.10, 0.15, 0.20, 0.25],
    "entry_iv_mult": [1.0, 1.10, 1.20],
    "half_spread": [0.01, 0.02, 0.03],
}


def load(period):
    bars = bdata.load_minute_bars("SPY", "2025-01-01", "2026-07-17")
    bars = bars[(bars.index >= period[0]) & (bars.index <= period[1] + " 23:59")]
    return bars


def main():
    arg = sys.argv[1]
    if arg.startswith("@"):          # @file.json -> read spec from file
        with open(arg[1:]) as f:
            spec = json.load(f)
    else:
        spec = json.loads(arg)
    holdout = "--holdout" in sys.argv
    stress = "--stress" in sys.argv
    out_path = None
    if "--out" in sys.argv:
        out_path = sys.argv[sys.argv.index("--out") + 1]
    bars = load(HOLDOUT if holdout else TRAIN)

    results = []
    if stress:
        # Hold every non-cost knob from the base spec fixed; sweep the cost
        # envelope so robustness (not a single lucky point) is what's judged.
        base = {k: v for k, v in spec.items() if k != "grid"}
        keys = list(STRESS_GRID)
        for combo in itertools.product(*(STRESS_GRID[k] for k in keys)):
            kw = {**base, **dict(zip(keys, combo))}
            r = simulate(bars, SimParams(**kw))
            results.append({"params": dict(zip(keys, combo)), **r.summary()})
    elif "grid" in spec:
        grid = spec["grid"]
        keys = list(grid)
        for combo in itertools.product(*(grid[k] for k in keys)):
            kw = dict(zip(keys, combo))
            r = simulate(bars, SimParams(**kw))
            results.append({"params": kw, **r.summary()})
    else:
        r = simulate(bars, SimParams(**spec))
        results.append({"params": spec, **r.summary()})

    payload = json.dumps(results, indent=1)
    if out_path:
        with open(out_path, "w") as f:
            f.write(payload)
        print(f"wrote {len(results)} results to {out_path}")
        best = sorted((r for r in results if r.get("n", 0) >= 50),
                      key=lambda r: -r.get("t", 0))[:5]
        print(json.dumps(best, indent=1))
    else:
        print(payload)


if __name__ == "__main__":
    main()
