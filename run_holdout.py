"""Spend the holdout ONCE on the sweep's top candidates, with the
multiple-testing context that decides whether any of it means anything.
"""
import json
import math

import numpy as np

from app.backtest import data as bdata
from app.backtest.scalpsim import SimParams, simulate

TRAIN = ("2025-01-01", "2026-02-28")
HOLDOUT = ("2026-03-01", "2026-07-17")

# Top survivors by train t-stat, from the 96-config sweep.
CANDIDATES = [
    {"burst_bps": 20, "burst_bars": 1, "mode": "follow", "qty": 5,
     "entry_start_et": 780, "entry_end_et": 900},
    {"burst_bps": 20, "burst_bars": 5, "mode": "follow", "qty": 5,
     "entry_start_et": 780, "entry_end_et": 900},
    {"burst_bps": 20, "burst_bars": 5, "mode": "fade", "qty": 5,
     "entry_start_et": 585, "entry_end_et": 690},
    {"burst_bps": 20, "burst_bars": 1, "mode": "follow", "qty": 5,
     "entry_start_et": 585, "entry_end_et": 900},
]
TRAIN_T = [1.83, 1.67, 1.48, 1.49]

N_CONFIGS = 96          # configs searched on train
T_THRESHOLD = 1.0


def load(period):
    bars = bdata.load_minute_bars("SPY", "2025-01-01", "2026-07-17")
    return bars[(bars.index >= period[0]) & (bars.index <= period[1] + " 23:59")]


def main():
    # --- How many t>1 configs does pure noise produce? -------------------
    # One-sided P(t > 1) ~= 0.159 for a large sample.
    p_one = 0.159
    expected = N_CONFIGS * p_one
    print("=" * 74)
    print("MULTIPLE-TESTING CONTEXT")
    print("=" * 74)
    print(f"configs searched on train : {N_CONFIGS}")
    print(f"configs with t>1 found    : 13")
    print(f"expected from pure noise  : {expected:.1f}")
    print("-> the survivor count is what a coin-flip search produces.\n")

    train_bars, hold_bars = load(TRAIN), load(HOLDOUT)

    print("=" * 74)
    print("HOLDOUT (2026-03-01 .. 2026-07-17) -- spent ONCE")
    print("=" * 74)
    print(f"{'config':<46}{'n':>5}{'hit':>7}{'mean$':>9}{'t':>7}{'total$':>10}")
    print("-" * 84)
    rows = []
    for cfg, t_train in zip(CANDIDATES, TRAIN_T):
        r = simulate(hold_bars, SimParams(**cfg))
        s = r.summary()
        label = (f"{cfg['mode']} {cfg['burst_bps']}bps/{cfg['burst_bars']}bar "
                 f"{cfg['entry_start_et']}-{cfg['entry_end_et']}")
        if s.get("n", 0) == 0:
            print(f"{label:<46}{'0':>5}  (no signals)")
            continue
        rows.append((label, t_train, s))
        print(f"{label:<46}{s['n']:>5}{s['hit']:>7.3f}{s['mean_pnl']:>9.2f}"
              f"{s['t']:>7.2f}{s['total']:>10.0f}")

    print("\n" + "=" * 74)
    print("TRAIN -> HOLDOUT DECAY")
    print("=" * 74)
    for label, t_train, s in rows:
        keep = (s["t"] / t_train * 100) if t_train else 0
        print(f"{label:<46} t {t_train:>5.2f} -> {s['t']:>5.2f}  "
              f"({keep:>5.0f}% retained)")

    sig = [s for _, _, s in rows if s["t"] >= 2.0 and s["mean_pnl"] > 0]
    print("\n" + "=" * 74)
    print("VERDICT")
    print("=" * 74)
    print(f"candidates reaching t>=2.0 with positive mean on holdout: {len(sig)}")
    if not sig:
        print("-> NOTHING is statistically validated. Live trading is NOT justified.")
    else:
        print("-> at least one survived; still check drawdown and worst day.")

    with open("holdout_results.json", "w") as f:
        json.dump([{"label": l, "train_t": t, **s} for l, t, s in rows], f, indent=1)


if __name__ == "__main__":
    main()
