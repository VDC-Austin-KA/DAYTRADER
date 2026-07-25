"""Walk-forward backtest of the multi-factor confluence 0DTE method.

Usage:
  python run_confluence.py                        # SPY+QQQ, 5 folds, h=15m
  python run_confluence.py --symbols SPY,QQQ,NVDA --folds 6 --horizon 20
  python run_confluence.py --min-confirms 1       # loosen the gate (compare)

This is the honest scoreboard for "is my method actually better?": it runs the
frozen confluence rule across several consecutive out-of-sample windows and
reports whether the edge is CONSISTENT. A rule that only wins in one fold, or
whose stability-adjusted t-stat is under ~1.5-2, is not an edge -- it stays in
paper regardless of how good the average looks.
"""
from __future__ import annotations

import argparse

from app.backtest import confluence, data as bdata, walkforward


def build_frames(symbols, start, end, **kw):
    out = {}
    for sym in symbols:
        bars = bdata.load_minute_bars(sym, start, end)
        if bars.empty or len(bars) < 4000:
            print(f"{sym}: insufficient bars ({len(bars)}), skipping")
            continue
        out[sym] = confluence.compute_confluence_signal(bars, **kw)
        print(f"{sym}: {len(bars)} bars scored")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SPY,QQQ")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-07-17")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--horizon", type=int, default=15)
    ap.add_argument("--min-confirms", dest="min_confirms", type=int, default=2)
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    frames = build_frames(symbols, args.start, args.end,
                          min_confirms=args.min_confirms)
    if not frames:
        print("no frames -- need a moomoo OpenD cache (see app/backtest/data.py)")
        return

    wf = walkforward.walk_forward(frames, n_folds=args.folds, horizon=args.horizon)
    print("\n" + wf.summary())

    t, pos, k = wf.aggregate_t, wf.n_positive, len(wf.folds)
    if pos >= (k + 1) // 2 + 1 and t >= 1.5:
        print(f"\n  VERDICT: {pos}/{k} folds positive, stability-adjusted "
              f"t={t:+.2f}. Consistent enough to keep testing in paper and "
              "watch the live paper record; NOT a green light for real money.")
    else:
        print(f"\n  VERDICT: {pos}/{k} folds positive, stability-adjusted "
              f"t={t:+.2f}. Not a consistent edge -- stays paper.")


if __name__ == "__main__":
    main()
