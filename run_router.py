"""Walk-forward the regime router, ATTRIBUTED per model.

Usage:
  python run_router.py                      # SPY+QQQ, 5 folds, h=15m
  python run_router.py --folds 6 --horizon 20 --symbols SPY,QQQ,NVDA

The question this answers is not "is the router positive?" but "does adding
the reversion half EARN its trades?". A blended number can look fine while the
new half quietly bleeds, so every run reports continuation and reversion
separately as well as combined. Decision rule, stated before seeing results:

  * reversion mean_bps <= 0 out of sample  -> switch the half OFF, it is
    coverage bought at a cost.
  * reversion positive but continuation degraded -> the thresholds overlap;
    re-separate them rather than keeping both.
  * both positive and consistent across folds -> the efficiency split carries
    information, which is the claim regime.py raised and never settled.
"""
from __future__ import annotations

import argparse

from app.backtest import data as bdata, router, walkforward


def build(symbols, start, end, **kw):
    out = {}
    for sym in symbols:
        bars = bdata.load_minute_bars(sym, start, end)
        if bars.empty or len(bars) < 4000:
            print(f"{sym}: insufficient bars ({len(bars)}), skipping")
            continue
        out[sym] = router.compute_routed_signal(bars, **kw)
        print(f"{sym}: {len(bars)} bars scored")
    return out


def _report(name, frames, folds, horizon):
    wf = walkforward.walk_forward(frames, n_folds=folds, horizon=horizon)
    print(f"\n=== {name} ===")
    print(wf.summary())
    return wf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SPY,QQQ")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-07-17")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--horizon", type=int, default=15)
    ap.add_argument("--min-eff", dest="min_eff", type=float, default=0.55)
    ap.add_argument("--max-eff", dest="max_eff", type=float, default=0.55)
    args = ap.parse_args()

    if args.max_eff > args.min_eff:
        print(f"WARNING: max_eff {args.max_eff} > min_eff {args.min_eff} -- the "
              "regimes OVERLAP, models can contend for the same bar.")

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    frames = build(symbols, args.start, args.end,
                   min_efficiency=args.min_eff, max_efficiency=args.max_eff)
    if not frames:
        print("no frames -- need a moomoo OpenD cache (see app/backtest/data.py)")
        return

    cov = router.coverage(frames)
    print(f"\ncoverage: {cov}")

    combined = _report("COMBINED (router)", frames, args.folds, args.horizon)
    cont = _report("continuation only",
                   router.split_by_model(frames, "continuation"),
                   args.folds, args.horizon)
    rev = _report("reversion only",
                  router.split_by_model(frames, "reversion"),
                  args.folds, args.horizon)

    rev_mean = sum(f.mean_bps for f in rev.folds) / max(len(rev.folds), 1)
    cont_mean = sum(f.mean_bps for f in cont.folds) / max(len(cont.folds), 1)
    print("\n--- VERDICT ---")
    print(f"continuation mean {cont_mean:+.2f}bps | "
          f"reversion mean {rev_mean:+.2f}bps | "
          f"combined stability-t {combined.aggregate_t:+.2f}")
    if rev_mean <= 0:
        print("  Reversion half does NOT earn its trades. Turn it off "
              "(enable_reversion=False); coverage is not worth paying for.")
    elif combined.aggregate_t < 1.5:
        print("  Reversion positive but the blend is not consistent across "
              "folds. Not an edge yet -- stays paper.")
    else:
        print("  Both halves positive and the blend is fold-consistent. The "
              "efficiency split carries information. Still paper: confirm on "
              "a live paper record before it means anything in dollars.")


if __name__ == "__main__":
    main()
