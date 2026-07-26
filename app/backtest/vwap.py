"""VWAP-anchored opening-range 0DTE signal, vectorised for backtesting.

THE METHOD, AND WHY IT SHOULD BEAT A RAW BURST
----------------------------------------------
The raw burst rule this repo already refuted bought *any* fast move, so it
bought the reversals too -- on 0DTE that means paying the spread and then
feeding theta. This signal conditions the entry on intraday STRUCTURE, the
framework desks actually trade, so it fires far less often and only when the
tape is organised:

  1. VWAP ANCHOR. Longs only when price is above a RISING session VWAP;
     shorts only below a falling one. VWAP is the session's volume-weighted
     fair value; a 0DTE position fighting it is the classic loser.
  2. OPENING-RANGE BREAKOUT. No signal until the first ``or_minutes`` range
     has formed; then trade only a genuine BREAK of that range in the
     VWAP-agreed direction. This is what filters the aimless chop.
  3. REGIME GATE. Kaufman efficiency must clear a floor -- trend, not thrash.
  4. DEBOUNCE. A signal is emitted only on the BAR THE BREAK HAPPENS (the
     rising edge), never on every bar it persists. Scoring every persisting
     bar would count one decision as hundreds of overlapping samples and
     manufacture significance -- the exact trap regime.py warns about.

NO LOOKAHEAD
------------
Every column at bar ``i`` uses only data up to ``i``. VWAP is a within-session
cumulative (trailing). The opening range is fixed once its window closes and
is applied only to LATER bars. Efficiency is a trailing window. The unit test
pins this by truncating the series and asserting the last signal is unchanged.

WHAT THIS MEASURES
------------------
Like ``engine.py``, this scores whether the UNDERLYING subsequently moves the
signalled way -- the honest foundation, since no historical option quotes
exist to price the option leg. Feed the frame it returns to
``engine.evaluate`` to get hit rate, edge, and a t-stat against a coin flip.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Session clock, in minutes since midnight ET (bars are ET, as in scalpsim).
SESSION_OPEN_ET = 9 * 60 + 30      # 09:30
LUNCH_START_ET = 11 * 60 + 30      # 11:30 -- midday drift is low-quality
LUNCH_END_ET = 13 * 60             # 13:00
LAST_SIGNAL_ET = 15 * 60           # 15:00 -- 0DTE gamma past here is a coin flip


def _kaufman_efficiency(close: pd.Series, window: int) -> pd.Series:
    """Net move / path travelled over a trailing window. 0 = chop, 1 = trend."""
    net = (close - close.shift(window)).abs()
    path = close.diff().abs().rolling(window).sum()
    return (net / path.clip(lower=1e-12)).fillna(0.0)


def compute_vwap_signal(
    df: pd.DataFrame,
    or_minutes: int = 30,
    slope_lookback: int = 10,
    eff_window: int = 20,
    min_efficiency: float = 0.40,
    skip_lunch: bool = True,
) -> pd.DataFrame:
    """Per-bar VWAP/opening-range signal, session-aware and causal.

    Returns a frame with ``close``, ``direction`` ("up"/"down"/"neutral") and
    ``surge`` (100 on the entry bar, else 0) so it drops straight into
    ``engine.evaluate`` / ``engine.split``.
    """
    close = df["Close"]
    high, low, vol = df["High"], df["Low"], df["Volume"]
    typical = (high + low + close) / 3.0
    minutes = df.index.hour * 60 + df.index.minute
    mins = np.asarray(minutes)
    from_open = mins - SESSION_OPEN_ET
    day = pd.Index([str(d) for d in df.index.date])

    vwap = pd.Series(index=df.index, dtype=float)
    or_high = pd.Series(index=df.index, dtype=float)
    or_low = pd.Series(index=df.index, dtype=float)

    # Session-by-session: VWAP is a within-day cumulative; the opening range
    # is frozen at the end of its window and applied only to later bars.
    pv = typical * vol
    for d, idx in df.groupby(day).groups.items():
        cpv = pv.loc[idx].cumsum()
        cv = vol.loc[idx].cumsum().clip(lower=1e-9)
        vwap.loc[idx] = (cpv / cv).to_numpy()

        fo = (df.loc[idx].index.hour * 60 + df.loc[idx].index.minute) - SESSION_OPEN_ET
        in_or = np.asarray(fo) < or_minutes
        if in_or.any():
            hi = float(high.loc[idx][in_or].max())
            lo = float(low.loc[idx][in_or].min())
        else:
            hi, lo = np.nan, np.nan
        # Known only AFTER the window closes -> assign to post-window bars.
        or_high.loc[idx] = np.where(np.asarray(fo) >= or_minutes, hi, np.nan)
        or_low.loc[idx] = np.where(np.asarray(fo) >= or_minutes, lo, np.nan)

    vwap_rising = vwap > vwap.shift(slope_lookback)
    vwap_falling = vwap < vwap.shift(slope_lookback)
    eff = _kaufman_efficiency(close, eff_window)

    tradeable_time = (from_open >= or_minutes) & (mins < LAST_SIGNAL_ET)
    if skip_lunch:
        tradeable_time &= ~((mins >= LUNCH_START_ET) & (mins < LUNCH_END_ET))

    long_raw = (
        tradeable_time & (close > vwap) & vwap_rising
        & (close > or_high) & (eff >= min_efficiency)
    )
    short_raw = (
        tradeable_time & (close < vwap) & vwap_falling
        & (close < or_low) & (eff >= min_efficiency)
    )

    # Debounce to the rising edge, per session, so persisting bars are not
    # re-counted as fresh decisions.
    same_day = day == pd.Index(day).to_series().shift(1).to_numpy()
    long_edge = long_raw & ~(long_raw.shift(1).fillna(False) & same_day)
    short_edge = short_raw & ~(short_raw.shift(1).fillna(False) & same_day)

    direction = pd.Series("neutral", index=df.index)
    direction[long_edge] = "up"
    direction[short_edge] = "down"
    surge = pd.Series(np.where(long_edge | short_edge, 100.0, 0.0), index=df.index)

    return pd.DataFrame({
        "close": close, "surge": surge, "direction": direction,
        "vwap": vwap, "or_high": or_high, "or_low": or_low, "efficiency": eff,
    })
