"""CLI for the scalp simulator: one config or a grid slice, JSON out.

Usage:
  python run_scalpsim.py '{"burst_bps": 8}'                 # one config, train
  python run_scalpsim.py '{"grid": {...}}' --out slice.json # sweep a slice
  python run_scalpsim.py '{"burst_bps": 8}' --holdout       # holdout period
"""
import itertools
import json
import sys

import pandas as pd

from app.backtest import data as bdata
from app.backtest.scalpsim import SimParams, simulate

TRAIN = ("2025-01-01", "2026-02-28")
HOLDOUT = ("2026-03-01", "2026-07-17")


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
    out_path = None
    if "--out" in sys.argv:
        out_path = sys.argv[sys.argv.index("--out") + 1]
    bars = load(HOLDOUT if holdout else TRAIN)

    results = []
    if "grid" in spec:
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
