"""Join real fills to captured microstructure; test what separates winners.

Run daily as capture/ accumulates. It answers the one question that
price/volume data could not: do order-book imbalance or tape aggression at
entry separate the user's winning trades from the losers?

DISCIPLINE: do NOT change the live trigger until there are enough LOSING
trades to test against. A high-win-rate day yields few losers, and a rule
fit to a handful of losses is fit to noise. The gate below (MIN_LOSSES) is
deliberate; raise the sample, then decide.
"""
from __future__ import annotations

import sys
from collections import defaultdict, deque

import numpy as np
import pandas as pd

MIN_LOSSES = 30          # below this, report only -- never retune the trigger
ET_TO_UTC_HOURS = 4      # fills are ET; capture writes datetime.utcnow()


def fifo_trips(deals: pd.DataFrame) -> pd.DataFrame:
    deals = deals.sort_values("ts")
    lots, trips = defaultdict(deque), []
    for _, r in deals.iterrows():
        if r.trd_side == "BUY":
            lots[r.code].append([r.qty, r.price, r.ts])
        else:
            rem = r.qty
            while rem > 0 and lots[r.code]:
                lq, lpx, lts = lots[r.code][0]
                take = min(rem, lq)
                trips.append({"code": r.code, "entry_ts": lts,
                              "pnl": take * (r.price - lpx) * 100})
                lq -= take
                rem -= take
                if lq <= 0:
                    lots[r.code].popleft()
                else:
                    lots[r.code][0][0] = lq
    t = pd.DataFrame(trips)
    if len(t):
        t["win"] = (t.pnl > 0).astype(int)
    return t


def join_capture(trips: pd.DataFrame, cap: pd.DataFrame,
                 tol_s: float = 20.0) -> pd.DataFrame:
    cap = cap.sort_values("ts")
    trips = trips.copy()
    trips["cap_ts"] = trips.entry_ts + pd.Timedelta(hours=ET_TO_UTC_HOURS)
    rows = []
    for _, r in trips.iterrows():
        sub = cap[cap.code == r.code]
        if not len(sub):
            continue
        idx = (sub.ts - r.cap_ts).abs().idxmin()
        row = cap.loc[idx]
        if abs((row.ts - r.cap_ts).total_seconds()) <= tol_s:
            rows.append({
                "win": r.win, "pnl": r.pnl,
                "book_imb": row.book_imbalance,
                "tape_imb": row.tape_imbalance,
                "spread_pct": row.spread_pct,
            })
    return pd.DataFrame(rows)


def report(j: pd.DataFrame) -> None:
    n_loss = int((1 - j.win).sum())
    print(f"joined {len(j)} trips  wins={int(j.win.sum())} losses={n_loss}")
    features = ["book_imb", "tape_imb", "spread_pct"]
    sig = []
    for f in features:
        w, l = j[j.win == 1][f], j[j.win == 0][f]
        if len(w) >= 2 and len(l) >= 2:
            se = np.sqrt(w.var() / len(w) + l.var() / len(l))
            ts = (w.mean() - l.mean()) / se if se else 0.0
            flag = " *" if abs(ts) >= 2 else ""
            print(f"  {f:<11} win {w.mean():+.3f}  loss {l.mean():+.3f}  t={ts:+.2f}{flag}")
            if abs(ts) >= 2:
                sig.append((f, ts))
        else:
            print(f"  {f:<11} too few losses to test")
    print()
    if n_loss < MIN_LOSSES:
        print(f"VERDICT: {n_loss} losses < {MIN_LOSSES} gate. Report only -- "
              "do NOT retune the live trigger. Accumulate more sessions.")
    elif sig:
        print(f"VERDICT: {len(sig)} feature(s) separate winners at |t|>=2: {sig}")
        print("Enough losses AND significance -- worth a gated trigger change.")
    else:
        print("VERDICT: enough losses, but no feature separates at |t|>=2. "
              "Microstructure does not predict these entries either.")


def main() -> int:
    from moomoo import (OpenSecTradeContext, RET_OK, SecurityFirm,
                        SysConfig, TrdEnv, TrdMarket)

    from app.config import settings

    day = sys.argv[1] if len(sys.argv) > 1 else "2026-07-20"
    cap_path = f"capture/micro_{day}.parquet"
    try:
        cap = pd.read_parquet(cap_path)
    except Exception:
        print(f"no capture file {cap_path}")
        return 1
    cap["ts"] = pd.to_datetime(cap["ts"])

    SysConfig.set_all_thread_daemon(True)
    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US, host=settings.moomoo_opend_host or "127.0.0.1",
        port=settings.moomoo_opend_port, security_firm=SecurityFirm.FUTUINC)
    ret, d = ctx.history_deal_list_query(
        start=f"{day} 00:00:00", end=f"{day} 23:59:59",
        trd_env=TrdEnv.REAL, acc_id=settings.moomoo_acc_id)
    ctx.close()
    if ret != RET_OK or not len(d):
        print("no fills")
        return 1
    d["ts"] = pd.to_datetime(d["create_time"])
    trips = fifo_trips(d)
    if not len(trips):
        print("no completed round trips")
        return 0
    print(f"{day}: {len(trips)} trips, realized ${trips.pnl.sum():+,.0f}, "
          f"win rate {trips.win.mean()*100:.0f}%\n")
    report(join_capture(trips, cap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
