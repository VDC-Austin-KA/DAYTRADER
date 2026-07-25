"""Backtest the VWAP opening-range signal with train/holdout discipline.

Usage:
  python run_vwap.py                       # default params, SPY+QQQ, h=15m
  python run_vwap.py --horizon 30 --symbols SPY,QQQ,NVDA
  python run_vwap.py --or 20 --min-eff 0.5 --holdout

Thresholds/params are chosen on TRAIN. The holdout is scored ONCE with them
frozen; if it looks bad, the honest move is to report it, not re-tune. This
reuses engine.evaluate so the number is the same hit-rate / edge / t-stat the
rest of the backtest speaks in -- a t-stat that does not clear ~2-3 out of
sample means the structure is not an edge, and it stays in paper.
"""
from __future__ import annotations

import argparse

from app.backtest import data as bdata
from app.backtest import engine, vwap

TRAIN_END = "2026-03-01"      # everything before is train; on/after is holdout


def build_frames(symbols, start, end, **kw):
    out = {}
    for sym in symbols:
        bars = bdata.load_minute_bars(sym, start, end)
        if bars.empty or len(bars) < 2000:
            print(f"{sym}: insufficient bars ({len(bars)}), skipping")
            continue
        out[sym] = vwap.compute_vwap_signal(bars, **kw)
        print(f"{sym}: {len(bars)} bars scored")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SPY,QQQ")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-07-17")
    ap.add_argument("--horizon", type=int, default=15)
    ap.add_argument("--or", dest="or_minutes", type=int, default=30)
    ap.add_argument("--min-eff", dest="min_eff", type=float, default=0.40)
    ap.add_argument("--holdout", action="store_true")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    frames = build_frames(
        symbols, args.start, args.end,
        or_minutes=args.or_minutes, min_efficiency=args.min_eff,
    )
    if not frames:
        print("no frames -- need a moomoo OpenD cache (see app/backtest/data.py)")
        return

    train, test = engine.split(frames, TRAIN_END)
    which = test if args.holdout else train
    label = "HOLDOUT" if args.holdout else "TRAIN"
    r = engine.evaluate(which, threshold=50, horizon=args.horizon, label=label)
    print("\n" + r.summary())
    print(f"  n_decisions={r.n_signals}  median={r.median_bps:+.2f}bps")
    for sym, s in sorted(r.per_symbol.items()):
        print(f"    {sym}: n={s['n']:>4} hit={s['hit']*100:5.1f}% "
              f"mean={s['mean_bps']:+.2f}bps")
    if abs(r.t_stat) < 2.0:
        print("\n  VERDICT: t-stat under 2 -- no edge demonstrated. Stays paper.")
    else:
        print(f"\n  VERDICT: t={r.t_stat:+.2f}. Promising on {label.lower()}; "
              "confirm on holdout before trusting it.")


if __name__ == "__main__":
    main()
