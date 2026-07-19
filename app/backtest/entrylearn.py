"""Learn the user's entry rule from their own fills.

The burst rule I invented failed holdout. This asks a different question:
given the 702 real round trips in the account history, is there a
combination of observable features that separated the winners from the
losers -- and did the user's own call direction beat a coin flip?

METHOD
------
Each entry becomes one labelled example: features computed from SPY minute
bars using ONLY data up to the entry timestamp, labelled with the realised
P&L of that round trip. Chronological train/holdout split; the model is
fitted on train only and scored once on holdout.

WHAT WOULD MAKE THIS FAIL HONESTLY
----------------------------------
* If the classifier cannot beat the base rate on holdout, the entry edge
  is not in these features (likely it lives in order-book/tape data we
  never recorded), and automating entries stays unjustified.
* Sample: ~700 trips but far fewer independent DECISIONS (partial fills
  of one decision share a timestamp). Decisions are collapsed first --
  counting partials as separate samples would inflate significance the
  same way overlapping bars did in the earlier regime test.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def collapse_decisions(fills: pd.DataFrame, window_s: int = 10) -> pd.DataFrame:
    """Group partial fills of the same decision into one row.

    A 32-lot filled as 7 prints inside a second is ONE decision. Treating
    each print as a sample would multiply the apparent sample size without
    adding information.
    """
    f = fills.sort_values("ts").copy()
    f["right"] = f["code"].str.extract(r"\d{6}([CP])")[0]
    groups, cur, last_key = [], [], None
    for _, r in f.iterrows():
        key = (r["code"], r["trd_side"])
        if (last_key == key and cur
                and (r["ts"] - cur[-1]["ts"]).total_seconds() <= window_s):
            cur.append(r)
        else:
            if cur:
                groups.append(cur)
            cur, last_key = [r], key
    if cur:
        groups.append(cur)

    rows = []
    for g in groups:
        qty = sum(x["qty"] for x in g)
        notional = sum(x["qty"] * x["price"] for x in g)
        rows.append({
            "ts": g[0]["ts"], "code": g[0]["code"], "side": g[0]["trd_side"],
            "right": g[0]["right"], "qty": qty,
            "price": notional / qty if qty else 0.0,
        })
    return pd.DataFrame(rows)


def features_at(bars: pd.DataFrame, ts: pd.Timestamp) -> dict | None:
    """Observable state at ``ts``, trailing windows only."""
    win = bars[bars.index <= ts]
    if len(win) < 60:
        return None
    c = win["Close"]
    spot = float(c.iloc[-1])
    h60, l60 = float(win["High"].tail(60).max()), float(win["Low"].tail(60).min())
    rng = max(h60 - l60, 1e-9)

    def ret(n):
        return float(c.iloc[-1] / c.iloc[-n - 1] - 1.0) * 10_000 if len(c) > n else 0.0

    v = win["Volume"]
    atr = float((win["High"] - win["Low"]).tail(14).mean())
    diffs = c.diff().abs().tail(30).sum()
    net = abs(float(c.iloc[-1] - c.iloc[-31])) if len(c) > 31 else 0.0
    return {
        "range_pos": (spot - l60) / rng * 100,        # 0 = 60m low, 100 = high
        "ret_1": ret(1), "ret_5": ret(5), "ret_15": ret(15), "ret_60": ret(60),
        "atr_pct": atr / spot * 10_000,
        "vol_ratio": float(v.iloc[-1] / max(v.tail(20).mean(), 1.0)),
        "efficiency": net / max(diffs, 1e-9),
        "minutes_from_open": (ts.hour * 60 + ts.minute) - 570,
        "dist_from_vwap": (spot - float(
            (win["Close"] * win["Volume"]).tail(60).sum()
            / max(v.tail(60).sum(), 1.0))) / spot * 10_000,
    }


def build_dataset(decisions: pd.DataFrame, trips: pd.DataFrame,
                  bars: pd.DataFrame) -> pd.DataFrame:
    """One row per BUY decision: features + realised outcome."""
    buys = decisions[decisions["side"] == "BUY"].copy()
    # Attribute each trip's P&L to its nearest preceding buy decision.
    rows = []
    for _, b in buys.iterrows():
        ts = b["ts"].tz_localize(None) if b["ts"].tzinfo else b["ts"]
        feats = features_at(bars, ts)
        if feats is None:
            continue
        rel = trips[(trips["code"] == b["code"])
                    & (trips["entry"].round(2) == round(b["price"], 2))]
        if rel.empty:
            continue
        pnl = float(rel["pnl"].sum())
        rows.append({
            **feats, "ts": ts, "code": b["code"], "right": b["right"],
            "qty": b["qty"], "pnl": pnl, "win": int(pnl > 0),
            "is_call": int(b["right"] == "C"),
        })
    return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
