"""Intraday bars with extended hours, plus RSI and Awesome Oscillator.

moomoo's ``request_history_kline(extended_time=True)`` returns the full
session -- 960 one-minute bars from 04:01 to 20:00 ET versus 390 for
regular hours alone -- so premarket and after-hours are simply a flag, not
a separate data source.

Indicators are computed server-side so the browser receives values rather
than a formula to re-implement, and so they can be unit-tested against
known series.

Tick is a special case: OpenD serves tick-by-tick only for subscribed
codes and only as a live buffer, so there is no tick HISTORY to fetch.
``interval="tick"`` therefore returns the most recent prints and says so.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ..config import settings

log = logging.getLogger("daytrader.intraday")

# Browser-facing name -> moomoo KLType attribute.
INTERVALS = {
    "1m": "K_1M", "3m": "K_3M", "5m": "K_5M",
    "15m": "K_15M", "30m": "K_30M", "60m": "K_60M",
}
# Days of history to request per interval -- enough context for the
# indicators without dragging a year of minute bars over the wire.
LOOKBACK_DAYS = {"1m": 3, "3m": 5, "5m": 8, "15m": 15, "30m": 30, "60m": 60}


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. Uses an EWM with alpha=1/period, as Wilder specified --
    a simple rolling mean gives visibly different values."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    # All-gain stretches divide by zero -> RSI 100, which is correct.
    return out.fillna(100.0).where(avg_gain > 0, 0.0).clip(0, 100)


def awesome_oscillator(high: pd.Series, low: pd.Series,
                       fast: int = 5, slow: int = 34) -> pd.Series:
    """Bill Williams' AO: SMA(median price, 5) - SMA(median price, 34).

    Median price, not close -- using close is a common and wrong shortcut.
    """
    median = (high + low) / 2.0
    return median.rolling(fast).mean() - median.rolling(slow).mean()


def _session_of(ts: pd.Timestamp) -> str:
    """Label each bar so the chart can shade premarket/after-hours."""
    t = ts.time()
    if t < pd.Timestamp("09:30").time():
        return "pre"
    if t >= pd.Timestamp("16:00").time():
        return "post"
    return "regular"


def get_intraday(symbol: str, interval: str = "1m",
                 extended: bool = True) -> dict:
    """OHLCV + indicators. Empty payload (never an exception) on failure."""
    from ..data import moomoo_data as mm

    empty = {"symbol": symbol, "interval": interval, "extended": extended,
             "bars": [], "message": ""}
    if interval == "tick":
        return _recent_ticks(symbol)
    ktype_name = INTERVALS.get(interval)
    if ktype_name is None:
        empty["message"] = f"Unsupported interval {interval!r}."
        return empty
    if not mm.configured() or not mm._reachable():
        empty["message"] = "moomoo OpenD not reachable."
        return empty

    from moomoo import KLType, RET_OK

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=LOOKBACK_DAYS.get(interval, 5) + 4)
    with mm._lock:
        try:
            ctx = mm._context()
            ret, df, _ = ctx.request_history_kline(
                mm._us_code(symbol), start=start.isoformat(),
                end=end.isoformat(), ktype=getattr(KLType, ktype_name),
                max_count=1000, extended_time=extended,
            )
            if ret != RET_OK:
                empty["message"] = str(df)[:200]
                return empty
        except Exception as exc:
            mm._reset_context()
            empty["message"] = f"{type(exc).__name__}: {exc}"
            return empty

    if df is None or not len(df):
        empty["message"] = "No bars returned."
        return empty

    df = df.copy()
    df["ts"] = pd.to_datetime(df["time_key"])
    df = df.sort_values("ts")
    close, high, low = df["close"], df["high"], df["low"]
    df["rsi"] = rsi(close)
    df["ao"] = awesome_oscillator(high, low)

    bars = [
        {
            "t": r.ts.strftime("%Y-%m-%d %H:%M"),
            "o": round(float(r.open), 2), "h": round(float(r.high), 2),
            "l": round(float(r.low), 2), "c": round(float(r.close), 2),
            "v": float(r.volume),
            "rsi": None if pd.isna(r.rsi) else round(float(r.rsi), 2),
            "ao": None if pd.isna(r.ao) else round(float(r.ao), 4),
            "session": _session_of(r.ts),
        }
        for r in df.itertuples()
    ]
    return {"symbol": symbol.upper(), "interval": interval,
            "extended": extended, "bars": bars, "message": ""}


def _recent_ticks(symbol: str) -> dict:
    """Live tick buffer. OpenD keeps no tick history, so this is 'recent'."""
    from moomoo import RET_OK, SubType

    from ..data import moomoo_data as mm

    out = {"symbol": symbol.upper(), "interval": "tick", "extended": False,
           "bars": [], "message": ""}
    if not mm.configured() or not mm._reachable():
        out["message"] = "moomoo OpenD not reachable."
        return out
    code = mm._us_code(symbol)
    with mm._lock:
        try:
            ctx = mm._context()
            ctx.subscribe([code], [SubType.TICKER])   # required before query
            ret, tk = ctx.get_rt_ticker(code, num=500)
            if ret != RET_OK:
                out["message"] = str(tk)[:200]
                return out
        except Exception as exc:
            mm._reset_context()
            out["message"] = f"{type(exc).__name__}: {exc}"
            return out

    if tk is None or not len(tk):
        out["message"] = "No ticks yet -- subscription just started."
        return out
    tk = tk.copy()
    tk["ts"] = pd.to_datetime(tk["time"])
    tk = tk.sort_values("ts")
    px = tk["price"].astype(float)
    tk["rsi"] = rsi(px)
    tk["ao"] = awesome_oscillator(px, px)
    out["bars"] = [
        {"t": r.ts.strftime("%H:%M:%S"), "o": float(r.price), "h": float(r.price),
         "l": float(r.price), "c": float(r.price), "v": float(r.volume),
         "rsi": None if pd.isna(r.rsi) else round(float(r.rsi), 2),
         "ao": None if pd.isna(r.ao) else round(float(r.ao), 4),
         "session": "regular",
         "dir": str(getattr(r, "ticker_direction", ""))}
        for r in tk.itertuples()
    ]
    out["message"] = ("Live tick buffer -- OpenD keeps no tick history, "
                      "so this shows only recent prints.")
    return out
