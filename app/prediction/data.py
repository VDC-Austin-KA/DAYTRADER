"""BTC real-time price access for the prediction bot.

The project's Tradier data layer is equities/options only, so the bot has
its own self-contained BTC feed: Coinbase's keyless public endpoints for
the spot price and 1-minute candles (the Kalshi/moomoo BTC contracts also
settle on a spot-index reference). Everything is TTL-cached and
best-effort — every function degrades to ``None``/empty rather than
raising, and the bot skips the cycle when data is missing.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import requests

log = logging.getLogger("daytrader.prediction.data")

_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
# Public market data; returns up to 300 most recent candles per request.
_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"

_CACHE: dict[str, tuple[float, object]] = {}
_SPOT_TTL = 15      # seconds
_CANDLES_TTL = 30   # seconds


def _cached(key: str, ttl: int):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def get_spot() -> Optional[float]:
    """Latest BTC-USD spot price (cached), or None."""
    cached = _cached("spot", _SPOT_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]
    try:
        resp = requests.get(_SPOT_URL, timeout=5)
        resp.raise_for_status()
        price = float(resp.json()["data"]["amount"])
        _CACHE["spot"] = (time.time(), price)
        return price
    except Exception:
        log.warning("coinbase BTC spot failed", exc_info=True)
    bars = get_minute_bars()
    if not bars.empty:
        return float(bars["Close"].iloc[-1])
    return None


def get_minute_bars() -> pd.DataFrame:
    """Last ~300 one-minute BTC OHLCV bars with a UTC index (may be empty)."""
    cached = _cached("candles", _CANDLES_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]
    try:
        resp = requests.get(_CANDLES_URL, params={"granularity": 60}, timeout=10)
        resp.raise_for_status()
        rows = resp.json()  # [[time, low, high, open, close, volume], ...] newest first
        df = pd.DataFrame(
            rows, columns=["time", "Low", "High", "Open", "Close", "Volume"]
        )
        df.index = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.drop(columns=["time"]).sort_index()
    except Exception:
        log.warning("coinbase BTC candles failed", exc_info=True)
        df = pd.DataFrame()
    _CACHE["candles"] = (time.time(), df)
    return df


def get_minute_returns(window: int = 240) -> pd.Series:
    """Log returns of the last ``window`` one-minute closes."""
    bars = get_minute_bars()
    if bars.empty or "Close" not in bars:
        return pd.Series(dtype=float)
    close = bars["Close"].dropna().tail(window + 1)
    return pd.Series(np.log(close.astype(float)).diff().dropna())


def price_at(when: datetime) -> Optional[float]:
    """Best-effort BTC price at ``when`` (UTC) from the 1-minute candles.

    Used to settle paper trades at the contract's close time. Falls back to
    the live spot if the candle history does not cover ``when``.
    """
    bars = get_minute_bars()
    if not bars.empty and "Close" in bars:
        ts = pd.Timestamp(when)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        upto = bars.loc[bars.index <= ts]
        if not upto.empty:
            return float(upto["Close"].iloc[-1])
    return get_spot()
