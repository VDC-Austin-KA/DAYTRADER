"""Simulate the autoscalp loop on SPY minute bars: burst -> bracket -> flatten.

WHAT IS REAL AND WHAT IS MODELED
--------------------------------
Real: every underlying price (actual SPY minute bars), the entry rule, the
bracket state machine (the same ``brackets.check`` the live daemon runs),
the session clock (entries until 15:00 ET, flatten 15:45 ET).

Modeled: option premiums. No historical 0DTE quotes exist on this data
plan, so premiums come from Black-Scholes with time-to-expiry decaying
minute by minute to the 16:00 ET expiry. For minutes-scale ATM holds the
P&L is dominated by delta/gamma of the real spot path plus theta and the
spread -- all of which are modeled. NOT modeled: intraday IV changes
(vol-of-vol). A vol spike helps longs and hurts this model's accuracy, so
results should be read as slightly conservative on winners, optimistic on
IV-crush days. Every run states its IV and spread assumptions; stress both.

Costs are charged the way the live loop pays them: buy at mid + half_spread,
every exit at mid - half_spread.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..trading import brackets

MINUTES_PER_YEAR = 365.0 * 24 * 60
EXPIRY_ET = 16 * 60           # minutes since midnight ET
LAST_ENTRY_ET = 15 * 60       # 14:00 CT
FLATTEN_ET = 15 * 60 + 45     # 14:45 CT


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_premium(spot: float, strike: float, minutes_left: float,
               iv: float, right: str) -> float:
    """Black-Scholes, zero rates -- fine at this horizon."""
    if minutes_left <= 0:
        intrinsic = spot - strike if right == "call" else strike - spot
        return max(0.0, intrinsic)
    t = minutes_left / MINUTES_PER_YEAR
    sig_rt = iv * math.sqrt(t)
    if sig_rt <= 0:
        return max(0.0, spot - strike if right == "call" else strike - spot)
    d1 = (math.log(spot / strike) + 0.5 * sig_rt * sig_rt) / sig_rt
    d2 = d1 - sig_rt
    if right == "call":
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


@dataclass
class SimParams:
    burst_bps: float = 8.0        # 1-bar (or windowed) move to trigger
    burst_bars: int = 1           # bars the move is measured over
    mode: str = "follow"          # follow the burst or fade it
    scale_gain: float = 0.75
    trail_pct: float = 0.25
    stop_pct: float = 0.35
    cooldown_bars: int = 5
    qty: int = 5
    # Time-based exits (see app/trading/brackets.py). 0 disables each.
    # give_up_minutes: cut an unscaled position that has not reached
    # +give_up_progress by the deadline -- the 0DTE theta-bleed loss the
    # -stop_pct floor never catches. max_hold_minutes: hard time backstop.
    give_up_minutes: float = 0.0
    give_up_progress: float = 0.10
    max_hold_minutes: float = 0.0
    iv: float = 0.15              # annualised; stress 0.10-0.25
    # Entry premium is priced at iv * this multiple: post-burst IV is
    # richer than the IV you exit at. 1.10 = a 10% IV-crush toll per trade.
    entry_iv_mult: float = 1.10
    half_spread: float = 0.01     # observed on ATM SPY 0DTE
    entry_start_et: int = 9 * 60 + 45   # skip the open rotation
    entry_end_et: int = LAST_ENTRY_ET


@dataclass
class Episode:
    day: str
    entry_time: str
    right: str
    strike: float
    entry_prem: float
    pnl: float                    # $ for the whole position
    exit_kind: str
    minutes_held: int


@dataclass
class SimResult:
    params: SimParams
    episodes: list = field(default_factory=list)

    def summary(self) -> dict:
        if not self.episodes:
            return {"n": 0}
        pnl = np.array([e.pnl for e in self.episodes])
        daily: dict[str, float] = {}
        for e in self.episodes:
            daily[e.day] = daily.get(e.day, 0.0) + e.pnl
        dvals = np.array(list(daily.values()))
        eq = np.cumsum(dvals)
        dd = float((np.maximum.accumulate(eq) - eq).max()) if len(eq) else 0.0
        se = pnl.std(ddof=1) / math.sqrt(len(pnl)) if len(pnl) > 1 else float("inf")
        kinds: dict[str, int] = {}
        for e in self.episodes:
            kinds[e.exit_kind] = kinds.get(e.exit_kind, 0) + 1

        # Win/loss anatomy: expectancy alone hides HOW it is earned. A
        # profit factor near 1 with a fat payoff is a very different (and
        # more fragile) bet than one built on a high hit rate.
        wins = pnl[pnl > 0]
        losses = pnl[pnl < 0]
        gross_win = float(wins.sum())
        gross_loss = float(-losses.sum())      # positive magnitude
        avg_win = float(wins.mean()) if len(wins) else 0.0
        avg_loss = float(losses.mean()) if len(losses) else 0.0   # negative
        profit_factor = (gross_win / gross_loss) if gross_loss else float("inf")
        payoff = (avg_win / abs(avg_loss)) if avg_loss else float("inf")

        # Daily Sharpe -- the P&L path's risk-adjusted return, annualised on
        # ~252 trading days. Reads across configs far better than raw total.
        if len(dvals) > 1 and dvals.std(ddof=1) > 0:
            sharpe = float(dvals.mean() / dvals.std(ddof=1) * math.sqrt(252))
        else:
            sharpe = 0.0
        avg_hold = float(np.mean([e.minutes_held for e in self.episodes]))

        return {
            "n": len(pnl), "hit": round(float((pnl > 0).mean()), 3),
            "mean_pnl": round(float(pnl.mean()), 2),
            "median_pnl": round(float(np.median(pnl)), 2),
            "t": round(float(pnl.mean() / se), 2) if se else 0.0,
            "total": round(float(pnl.sum()), 2),
            "profit_factor": round(profit_factor, 2),
            "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
            "payoff": round(payoff, 2),
            "avg_hold_min": round(avg_hold, 1),
            "sharpe": round(sharpe, 2),
            "days": len(daily), "win_days": int((dvals > 0).sum()),
            "worst_day": round(float(dvals.min()), 2),
            "best_day": round(float(dvals.max()), 2),
            "max_drawdown": round(dd, 2),
            "exits": kinds,
        }


def simulate(bars: pd.DataFrame, p: SimParams) -> SimResult:
    """Replay the whole history one session at a time."""
    res = SimResult(params=p)
    close = bars["Close"]
    minutes = bars.index.hour * 60 + bars.index.minute
    days = bars.index.date

    ret = close.pct_change(p.burst_bars).to_numpy()
    closes = close.to_numpy()
    mins = np.asarray(minutes)
    day_arr = np.array([str(d) for d in days])

    i, n = 1, len(bars)
    cooldown_until = -1
    while i < n:
        m = mins[i]
        if (m < p.entry_start_et or m >= p.entry_end_et or i <= cooldown_until
                or not np.isfinite(ret[i])):
            i += 1
            continue
        move = ret[i]
        if abs(move) * 10_000 < p.burst_bps:
            i += 1
            continue

        direction = "up" if move > 0 else "down"
        if p.mode == "fade":
            direction = "up" if direction == "down" else "down"
        right = "call" if direction == "up" else "put"
        # Fill on the NEXT bar: the burst is only knowable at this bar's
        # close, so the live daemon pays the post-burst price, not the
        # signal price. Filling at bar i would smuggle the burst itself
        # into the P&L.
        if i + 1 >= n or day_arr[i + 1] != day_arr[i]:
            i += 1
            continue
        i += 1
        m = mins[i]
        spot = closes[i]
        strike = round(spot)          # SPY has $1 strikes at the money
        mid = bs_premium(spot, strike, EXPIRY_ET - m, p.iv * p.entry_iv_mult, right)
        entry = mid + p.half_spread
        if entry < 0.05:              # untradeably cheap; skip
            i += 1
            continue

        st = brackets.BracketState(
            position_id=0, entry_price=round(entry, 2), quantity=p.qty,
            scale_out_gain=p.scale_gain, trail_pct=p.trail_pct,
            stop_loss_pct=p.stop_pct,
            give_up_minutes=p.give_up_minutes,
            give_up_progress=p.give_up_progress,
            max_hold_minutes=p.max_hold_minutes,
        )
        entry_minute = m               # minutes-since-midnight at the fill
        pnl = 0.0
        exit_kind = "flatten"
        j = i + 1
        this_day = day_arr[i]
        while j < n and day_arr[j] == this_day:
            mj = mins[j]
            mid_j = bs_premium(closes[j], strike, EXPIRY_ET - mj, p.iv, right)
            bid_j = max(0.0, round(mid_j - p.half_spread, 2))
            if mj >= FLATTEN_ET:
                pnl += st.remaining * (bid_j - st.entry_price) * 100
                st.remaining, st.closed = 0, True
                exit_kind = "flatten"
                break
            act = brackets.check(st, bid_j, minutes_held=mj - entry_minute)
            if act.kind != "none":
                pnl += act.sell_qty * (act.est_price - st.entry_price) * 100
                exit_kind = act.kind
            if st.closed:
                break
            j += 1
        else:
            # Data ended mid-position (last day): mark at final bid.
            if st.remaining:
                last_bid = max(0.0, bs_premium(
                    closes[min(j, n - 1)], strike,
                    max(0, EXPIRY_ET - mins[min(j, n - 1)]), p.iv, right,
                ) - p.half_spread)
                pnl += st.remaining * (last_bid - st.entry_price) * 100
                exit_kind = "data_end"

        # A scale-out that later flattens: both legs already accumulated.
        res.episodes.append(Episode(
            day=this_day, entry_time=str(bars.index[i]), right=right,
            strike=strike, entry_prem=round(entry, 2), pnl=round(pnl, 2),
            exit_kind=exit_kind, minutes_held=int(j - i),
        ))
        cooldown_until = j + p.cooldown_bars
        i = j + 1
    return res
