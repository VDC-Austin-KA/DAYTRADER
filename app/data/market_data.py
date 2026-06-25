"""Market-data access layer wrapping yfinance with light caching.

All network calls live here so the rest of the app stays testable and the
data provider can be swapped out without touching strategy code.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

try:
    import yfinance as yf
except Exception:  # pragma: no cover - yfinance optional at import time
    yf = None

# Simple in-process TTL cache: {key: (timestamp, value)}
_CACHE: dict[str, tuple[float, object]] = {}
_PRICE_TTL = 60          # seconds for quotes
_HISTORY_TTL = 60 * 30   # seconds for daily history
_CHAIN_TTL = 60 * 5      # seconds for option chains


def _cache_get(key: str, ttl: int):
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    return None


def _cache_set(key: str, value) -> None:
    _CACHE[key] = (time.time(), value)


def get_history(symbol: str, years: int = 5, interval: str = "1d") -> pd.DataFrame:
    """Return a daily OHLCV DataFrame for ``symbol``."""
    key = f"hist:{symbol}:{years}:{interval}"
    cached = _cache_get(key, _HISTORY_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]
    if yf is None:
        raise RuntimeError("yfinance is not installed")

    period = f"{max(years, 1)}y"
    df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
    if df is None or df.empty:
        df = pd.DataFrame()
    else:
        df = df.rename(columns=str.title)
        df.index = pd.to_datetime(df.index)
    _cache_set(key, df)
    return df


def get_quote(symbol: str) -> Optional[float]:
    """Return the latest price for ``symbol`` (best-effort)."""
    key = f"quote:{symbol}"
    cached = _cache_get(key, _PRICE_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]
    if yf is None:
        return None
    price = None
    try:
        fast = yf.Ticker(symbol).fast_info
        price = float(fast.get("last_price") or fast.get("lastPrice"))
    except Exception:
        price = None
    if not price:
        hist = get_history(symbol, years=1)
        if not hist.empty:
            price = float(hist["Close"].iloc[-1])
    if price:
        _cache_set(key, price)
    return price


def get_expirations(symbol: str) -> list[str]:
    """List available option expiration dates (YYYY-MM-DD)."""
    key = f"exp:{symbol}"
    cached = _cache_get(key, _CHAIN_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]
    if yf is None:
        return []
    try:
        exps = list(yf.Ticker(symbol).options or [])
    except Exception:
        exps = []
    _cache_set(key, exps)
    return exps


def get_option_chain(symbol: str, expiry: str) -> dict[str, pd.DataFrame]:
    """Return {'calls': df, 'puts': df} for one expiration date."""
    key = f"chain:{symbol}:{expiry}"
    cached = _cache_get(key, _CHAIN_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]
    if yf is None:
        return {"calls": pd.DataFrame(), "puts": pd.DataFrame()}
    try:
        chain = yf.Ticker(symbol).option_chain(expiry)
        out = {"calls": chain.calls.copy(), "puts": chain.puts.copy()}
    except Exception:
        out = {"calls": pd.DataFrame(), "puts": pd.DataFrame()}
    _cache_set(key, out)
    return out


def days_to_expiry(expiry: str) -> int:
    try:
        exp = datetime.strptime(expiry, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return max((exp - datetime.now(timezone.utc)).days, 0)
