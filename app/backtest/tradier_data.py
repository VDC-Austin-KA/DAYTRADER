"""Minute-bar loader via Tradier, for backtesting without a moomoo gateway.

``data.py`` sources bars from moomoo OpenD, which needs a logged-in gateway on
a machine you control. That is unavailable in CI, in a cloud container, or on
any box where OpenD is not running -- so the backtest could not be executed at
all there. This loader fills that gap using the Tradier token the app already
holds.

KNOW THE LIMIT BEFORE TRUSTING A RESULT
---------------------------------------
Tradier's ``timesales`` endpoint serves only the last ~10 trading days of
1-minute history (older dates return HTTP 400). That is enough to EXERCISE a
strategy and get a directional read; it is nowhere near enough to establish an
edge. Roughly ten sessions of a signal that fires a couple of times a day is a
few dozen decisions -- a sample where noise dominates and no t-stat should be
believed. ``load_range`` returns what it can and the caller is expected to
report the sample size next to any number derived from it.

For a real study, point ``data.load_minute_bars`` at a moomoo OpenD gateway
(years of history) and use this only as the fallback.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path

import pandas as pd
import requests

from ..config import settings

log = logging.getLogger("daytrader.backtest.tradier")

CACHE_DIR = Path("backtest_cache")
_PAUSE = 1.2          # sandbox rate limits are tight; stay well under them


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.tradier_token}",
            "Accept": "application/json"}


def fetch_day(symbol: str, day: dt.date, session: requests.Session | None = None
              ) -> pd.DataFrame:
    """One session of 1-minute bars. Empty frame when unavailable."""
    get = (session or requests).get
    try:
        r = get(
            f"{settings.tradier_base_url}/markets/timesales",
            params={"symbol": symbol, "interval": "1min",
                    "start": f"{day} 09:30", "end": f"{day} 16:00"},
            headers=_headers(), timeout=30,
        )
    except requests.RequestException as e:
        log.warning("%s %s: %s", symbol, day, e)
        return pd.DataFrame()
    if r.status_code != 200:
        return pd.DataFrame()
    try:
        series = r.json().get("series")
    except ValueError:
        return pd.DataFrame()
    if not series or not isinstance(series.get("data"), list):
        return pd.DataFrame()

    rows = series["data"]
    df = pd.DataFrame(rows)
    if df.empty or "time" not in df:
        return pd.DataFrame()
    df["time"] = pd.to_datetime(df["time"])
    df = (
        df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                           "close": "Close", "volume": "Volume"})
        .set_index("time")
        .sort_index()[["Open", "High", "Low", "Close", "Volume"]]
        .astype(float)
    )
    return df


def load_range(symbol: str, start: dt.date, end: dt.date,
               refresh: bool = False) -> pd.DataFrame:
    """Concatenated minute bars over a date range, cached to parquet."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache = CACHE_DIR / f"{symbol}_tradier_1m_{start}_{end}.parquet"
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)

    frames, day = [], start
    with requests.Session() as s:
        while day <= end:
            if day.weekday() < 5:                 # skip weekends outright
                d = fetch_day(symbol, day, session=s)
                if len(d):
                    frames.append(d)
                    log.info("%s %s: %d bars", symbol, day, len(d))
                time.sleep(_PAUSE)
            day += dt.timedelta(days=1)

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep="first")]
    out.to_parquet(cache)
    return out
