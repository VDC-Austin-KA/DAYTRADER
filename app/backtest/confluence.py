"""Multi-factor confluence 0DTE signal: a break is necessary, not sufficient.

WHY CONFLUENCE
--------------
The single-trigger VWAP opening-range break (app/backtest/vwap.py) is already
structure-aware, but a lone breakout still fails often -- it fires into
exhausted moves and against the larger trend. The losers cluster in two
recognisable states, and each has an INDEPENDENT confirmation that filters it:

  * TREND. A break is only trustworthy with the higher-timeframe trend behind
    it. Confirmation: the fast EMA over the slow EMA (up) or under it (down).
  * EXHAUSTION. A break on an already-stretched RSI is the top/bottom, not the
    start. Confirmation: RSI rising but NOT extended (a healthy band, not >~85
    for longs / <~15 for shorts).

The rule: take the VWAP-OR break ONLY when both independent factors agree with
its direction. Independence is the point -- three things that fail on
different tape rarely fail together, so confluence trades far less often and,
where it does, the setup is cleaner. It is a filter on top of a signal, never
a new signal, so it can only ever REMOVE trades, never invent them.

NO LOOKAHEAD
------------
The base signal is already causal and truncation-tested. EMA and RSI here are
`ewm`/rolling constructs that use only past bars. The unit test pins that a
bar's confluence verdict is unchanged when future bars are removed.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import vwap


def ema(close: pd.Series, span: int) -> pd.Series:
    """Exponential moving average -- causal (no centring)."""
    return close.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI, 0-100. Neutral 50 while the window fills. Causal."""
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    roll_up = up.ewm(alpha=1.0 / period, adjust=False).mean()
    roll_down = down.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = roll_up / roll_down.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def compute_confluence_signal(
    df: pd.DataFrame,
    ema_fast: int = 9,
    ema_slow: int = 21,
    rsi_period: int = 14,
    rsi_long_band: tuple[float, float] = (50.0, 85.0),
    rsi_short_band: tuple[float, float] = (15.0, 50.0),
    min_confirms: int = 2,
    **vwap_kw,
) -> pd.DataFrame:
    """VWAP-OR break gated by trend (EMA) and momentum-health (RSI).

    Returns the same engine-compatible schema as ``vwap.compute_vwap_signal``
    (``close``/``surge``/``direction``), with the confirmation columns kept
    for inspection. ``surge`` stays 100 only where the break AND >=
    ``min_confirms`` independent factors agree; every gated-out break drops to
    neutral/0.
    """
    base = vwap.compute_vwap_signal(df, **vwap_kw)
    close = df["Close"]

    ef, es = ema(close, ema_fast), ema(close, ema_slow)
    r = rsi(close, rsi_period)

    trend_up = ef > es
    trend_down = ef < es
    rsi_up_ok = (r >= rsi_long_band[0]) & (r <= rsi_long_band[1])
    rsi_down_ok = (r >= rsi_short_band[0]) & (r <= rsi_short_band[1])

    is_long = base["direction"] == "up"
    is_short = base["direction"] == "down"

    long_confirms = trend_up.astype(int) + rsi_up_ok.astype(int)
    short_confirms = trend_down.astype(int) + rsi_down_ok.astype(int)

    keep_long = is_long & (long_confirms >= min_confirms)
    keep_short = is_short & (short_confirms >= min_confirms)
    keep = keep_long | keep_short

    direction = pd.Series("neutral", index=df.index)
    direction[keep_long] = "up"
    direction[keep_short] = "down"
    surge = pd.Series(np.where(keep, 100.0, 0.0), index=df.index)

    out = base.copy()
    out["surge"] = surge
    out["direction"] = direction
    out["ema_fast"], out["ema_slow"], out["rsi"] = ef, es, r
    out["confirms"] = np.where(is_long, long_confirms,
                               np.where(is_short, short_confirms, 0))
    return out
