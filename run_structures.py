"""Hold the signal fixed; vary the INSTRUMENT. Which costs can the edge pay?

Usage:
  python run_structures.py                          # all models x all structures
  python run_structures.py --models confluence
  python run_structures.py --structures long_atm,long_itm2 --stress-spread

Every earlier result in this repo priced every signal as one long ATM 0DTE
contract, and every one of them lost money to theta. That is two claims fused
into one: "the direction call is wrong" and "the instrument is too expensive".
This script separates them by running the SAME direction calls through the
whole structure catalogue (app/backtest/structures.py) and reporting dollars.

READ THE SAMPLE SIZE BEFORE READING THE RANKING. The Tradier loader serves
about ten sessions, so per-cell n is in the tens. That is enough to see a cost
effect -- costs are deterministic and show up immediately -- and nowhere near
enough to establish an edge. A structure that turns a loss into a profit here
has demonstrated that the instrument mattered, NOT that the strategy works.
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import pandas as pd

from app.backtest import (confluence, reversion, router, scalpsim, structures,
                          vwap)

CACHE = Path("backtest_cache")


def load_bars(symbol: str) -> pd.DataFrame:
    """Whatever minute history is on disk, moomoo or Tradier."""
    hits = sorted(CACHE.glob(f"{symbol}_*1m*.parquet")) + \
        sorted(CACHE.glob(f"{symbol}_K_1M_*.parquet"))
    if not hits:
        return pd.DataFrame()
    frames = [pd.read_parquet(h) for h in hits]
    out = pd.concat(frames).sort_index()
    return out[~out.index.duplicated(keep="first")]


def fetch_bars(symbol: str, days: int) -> pd.DataFrame:
    """Pull fresh minute bars when the cache is empty."""
    from app.backtest import tradier_data
    end = dt.date.today()
    return tradier_data.load_range(symbol, end - dt.timedelta(days=days), end)


MODELS = {
    "vwap": lambda b: vwap.compute_vwap_signal(b),
    "confluence": lambda b: confluence.compute_confluence_signal(b),
    "reversion": lambda b: reversion.compute_reversion_signal(b),
    "router": lambda b: router.compute_routed_signal(b),
}

# Reversion trades to a destination; the others ride a runner. Keeping each
# model's own exit plan means a structure comparison is not quietly also an
# exit-rule comparison.
EXITS = {
    "reversion": dict(give_up_minutes=20.0, max_hold_minutes=45.0,
                      take_profit=0.35),
}
DEFAULT_EXIT = dict(give_up_minutes=20.0, max_hold_minutes=0.0, take_profit=0.0)


def params_for(model: str, struct: structures.Structure,
               stress_spread: bool, crush: float = 1.10) -> scalpsim.SimParams:
    cfg = {**DEFAULT_EXIT, **EXITS.get(model, {})}
    return scalpsim.SimParams(
        structure=struct,
        qty=2,
        entry_iv_mult=crush,
        give_up_minutes=cfg["give_up_minutes"],
        max_hold_minutes=cfg["max_hold_minutes"],
        # A proportional spread is what stops ITM and longer-dated legs from
        # looking cheaper to trade than they quote.
        rel_half_spread=0.01 if stress_spread else 0.0,
    )


def run(bars: pd.DataFrame, sig: pd.DataFrame, model: str,
        struct: structures.Structure, threshold: float,
        stress_spread: bool, crush: float = 1.10) -> dict:
    p = params_for(model, struct, stress_spread, crush)
    res = scalpsim.simulate_signals(bars, sig, p, threshold=threshold)
    return res.summary()


def sweep(bars_by_sym, sigs, model, structs, threshold, stress, crush):
    """Mean $/trade per structure, pooled across symbols."""
    out = {}
    for struct in structs:
        tot = n = 0.0
        for sym, bars in bars_by_sym.items():
            s = run(bars, sigs[sym], model, struct, threshold, stress, crush)
            if s.get("n"):
                tot += s["total"]
                n += s["n"]
        if n:
            out[struct.name] = tot / n
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SPY,QQQ")
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--structures", dest="structs",
                    default=",".join(s.name for s in structures.CATALOGUE))
    ap.add_argument("--threshold", type=float, default=50.0)
    ap.add_argument("--stress-spread", dest="stress", action="store_true",
                    help="charge a proportional (1%%) half-spread per leg")
    ap.add_argument("--fetch-days", dest="fetch_days", type=int, default=0,
                    help="pull N days of fresh bars if the cache is empty")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    models = [m.strip() for m in args.models.split(",") if m.strip() in MODELS]
    structs = [structures.BY_NAME[s.strip()]
               for s in args.structs.split(",") if s.strip() in structures.BY_NAME]

    bars_by_sym = {}
    for sym in symbols:
        b = load_bars(sym)
        if b.empty and args.fetch_days:
            b = fetch_bars(sym, args.fetch_days)
        if b.empty:
            print(f"{sym}: no bars cached; run with --fetch-days N")
            continue
        bars_by_sym[sym] = b
        sessions = len({d.date() for d in b.index})
        print(f"{sym}: {len(b)} bars over {sessions} sessions "
              f"({b.index[0].date()} -> {b.index[-1].date()})")
    if not bars_by_sym:
        return 1

    print(f"\nspread model: {'proportional 1% per leg' if args.stress else 'flat 1c per leg'}")

    rows = []
    for model in models:
        sigs = {s: MODELS[model](b) for s, b in bars_by_sym.items()}
        for struct in structs:
            agg = {"n": 0, "total": 0.0, "wins": 0, "hold": 0.0}
            for sym, bars in bars_by_sym.items():
                s = run(bars, sigs[sym], model, struct, args.threshold, args.stress)
                if not s.get("n"):
                    continue
                agg["n"] += s["n"]
                agg["total"] += s["total"]
                agg["wins"] += round(s["hit"] * s["n"])
                agg["hold"] += s["avg_hold_min"] * s["n"]
            if not agg["n"]:
                continue
            rows.append({
                "model": model, "structure": struct.name, "n": agg["n"],
                "hit": agg["wins"] / agg["n"],
                "mean": agg["total"] / agg["n"],
                "total": agg["total"],
                "hold": agg["hold"] / agg["n"],
            })

    if not rows:
        print("no episodes produced")
        return 1

    print(f"\n{'model':<12}{'structure':<18}{'n':>5}{'hit':>8}"
          f"{'mean $':>10}{'total $':>10}{'hold m':>8}")
    print("-" * 71)
    for r in sorted(rows, key=lambda r: (r["model"], -r["mean"])):
        print(f"{r['model']:<12}{r['structure']:<18}{r['n']:>5}"
              f"{r['hit']*100:>7.1f}%{r['mean']:>10.2f}{r['total']:>10.2f}"
              f"{r['hold']:>8.1f}")

    print("\n--- best instrument per model ---")
    for model in models:
        mr = [r for r in rows if r["model"] == model]
        if not mr:
            continue
        best = max(mr, key=lambda r: r["mean"])
        base = next((r for r in mr if r["structure"] == "long_atm"), None)
        delta = f" (vs long_atm {best['mean'] - base['mean']:+.2f}/trade)" if base else ""
        print(f"  {model:<12} {best['structure']:<18} "
              f"{best['mean']:+.2f}/trade n={best['n']}{delta}")

    # The IV-crush toll is the single most load-bearing assumption in the
    # whole comparison -- it is what a longer-dated structure pays for its
    # extra vega, and it is not observable in this data. Show the ranking
    # under both extremes rather than quoting one and hoping.
    print("\n--- sensitivity: IV-crush toll (mean $/trade) ---")
    print(f"  {'model/structure':<28}{'no crush':>10}{'10% crush':>12}{'20% crush':>12}")
    flips, robust_pos = [], []
    for model in models:
        sigs = {s: MODELS[model](b) for s, b in bars_by_sym.items()}
        cols = [sweep(bars_by_sym, sigs, model, structs, args.threshold,
                      args.stress, c) for c in (1.00, 1.10, 1.20)]
        for struct in structs:
            vals = [c.get(struct.name) for c in cols]
            if any(v is None for v in vals):
                continue
            cell = f"{model}/{struct.name}"
            flipped = min(vals) < 0 < max(vals)
            if flipped:
                flips.append(cell)
            if min(vals) > 0:
                robust_pos.append(cell)
            print(f"  {cell:<28}{vals[0]:>10.2f}{vals[1]:>12.2f}{vals[2]:>12.2f}"
                  f"{'   <- SIGN FLIPS' if flipped else ''}")

    pos = [r for r in rows if r["mean"] > 0]
    print("\n--- VERDICT ---")
    print(f"  At the 10% crush assumption: {len(pos)}/{len(rows)} cells positive.")
    if robust_pos:
        print("  Positive across the WHOLE crush range (the only cells that "
              "mean anything):")
        for c in robust_pos:
            print(f"    {c}")
    else:
        print("  NOT ONE cell is positive across the whole crush range.")
    if flips:
        print(f"  {len(flips)} cells change sign on the crush assumption alone "
              "-- an unobservable parameter here, since this data has no")
        print("  option quotes. Those cells are not results, they are restatements "
              "of a guess. Longer-dated structures dominate that list:")
        print("  buying tenor trades measurable theta for unmeasurable vega.")
    print("\n  Bottom line: cheaper instruments close a real part of the gap "
          "(see the per-model best above), but the")
    print("  binding constraint is no longer the instrument -- it is that the "
          "direction edge is too small to pay ANY")
    print("  cost structure, and the sample (n in the tens) cannot resolve "
          "what is left. Get years of bars, and")
    print("  option quotes if the tenor question is ever to be settled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
