"""Does anything in observable data separate the user's winning entries?"""
import numpy as np
import pandas as pd

from app.backtest import data as bdata
from app.backtest.entrylearn import build_dataset, collapse_decisions

# --- rebuild trips (FIFO) from the saved fills --------------------------
from collections import defaultdict, deque

d = pd.read_csv("fills_history.csv")
d["ts"] = pd.to_datetime(d["create_time"]).dt.tz_localize(None)
d = d[d.code.str.startswith("US.SPY")].sort_values("ts")

lots, trips = defaultdict(deque), []
for _, r in d.iterrows():
    if r.trd_side == "BUY":
        lots[r.code].append([r.qty, r.price])
    else:
        rem = r.qty
        while rem > 0 and lots[r.code]:
            lq, lpx = lots[r.code][0]
            take = min(rem, lq)
            trips.append({"code": r.code, "entry": lpx, "exit": r.price,
                          "qty": take, "pnl": take * (r.price - lpx) * 100})
            lq -= take; rem -= take
            if lq <= 0: lots[r.code].popleft()
            else: lots[r.code][0][0] = lq
trips = pd.DataFrame(trips)

dec = collapse_decisions(d)
bars = bdata.load_minute_bars("SPY", "2025-01-01", "2026-07-17")
ds = build_dataset(dec, trips, bars)

print("=" * 70)
print(f"decisions: {len(dec)}  |  BUY decisions with features+outcome: {len(ds)}")
print(f"trips: {len(trips)}  (collapsing partials removed "
      f"{len(d[d.trd_side=='BUY']) - len(dec[dec.side=='BUY'])} duplicate prints)")
if len(ds) < 40:
    print("\nTOO FEW independent decisions to learn anything. Stopping.")
    raise SystemExit(0)

base = ds.win.mean()
print(f"base win rate: {base*100:.1f}%   mean P&L ${ds.pnl.mean():,.0f}")

# --- direction skill: did calls vs puts beat a coin flip? ---------------
print("\n" + "=" * 70)
print("DIRECTION SKILL")
print("=" * 70)
for r, g in ds.groupby("right"):
    print(f"  {r}: n={len(g):3d}  win={g.win.mean()*100:5.1f}%  "
          f"mean=${g.pnl.mean():8,.0f}")

# --- univariate: does any feature separate winners? ---------------------
FEATS = ["range_pos", "ret_1", "ret_5", "ret_15", "ret_60", "atr_pct",
         "vol_ratio", "efficiency", "minutes_from_open", "dist_from_vwap"]
print("\n" + "=" * 70)
print("FEATURE SEPARATION (winners vs losers, train period)")
print("=" * 70)
cut = int(len(ds) * 0.7)
tr, ho = ds.iloc[:cut], ds.iloc[cut:]
print(f"train n={len(tr)}  holdout n={len(ho)}\n")
print(f"{'feature':<20}{'win mean':>12}{'loss mean':>12}{'t':>8}")
print("-" * 52)
sig = []
for f in FEATS:
    w, l = tr[tr.win == 1][f], tr[tr.win == 0][f]
    if len(w) < 5 or len(l) < 5:
        continue
    se = np.sqrt(w.var(ddof=1)/len(w) + l.var(ddof=1)/len(l))
    t = (w.mean() - l.mean()) / se if se else 0
    print(f"{f:<20}{w.mean():>12.2f}{l.mean():>12.2f}{t:>8.2f}")
    if abs(t) >= 2:
        sig.append((f, t))

print("\n" + "=" * 70)
print("VERDICT")
print("=" * 70)
if not sig:
    print("No feature separates winners from losers at |t|>=2 on train.")
    print("-> The entry edge is NOT in observable price/volume features.")
    print("-> Automating entry SELECTION from this data is not justified.")
else:
    print(f"Candidate features: {sig}")
    print("-> Fit and score on holdout before believing it.")
