"""Market-data access layer backed by the Tradier API, with light caching.

All network calls live here so the rest of the app stays testable and the
data provider can be swapped out without touching strategy code.

Configure a (free) Tradier developer token via the TRADIER_TOKEN env var.
Sandbox base URL gives delayed quotes and real option chains, which is plenty
for an educational paper-trading app.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests

from ..config import settings

log = logging.getLogger("daytrader.data")

# Simple in-process TTL cache: {key: (timestamp, value)}
_CACHE: dict[str, tuple[float, object]] = {}
_PRICE_TTL = 60          # seconds for quotes
_HISTORY_TTL = 60 * 30   # seconds for daily history
_CHAIN_TTL = 60 * 5      # seconds for option chains

# Column shape the rest of the app expects from option-chain DataFrames.
_CHAIN_COLUMNS = [
    "contractSymbol", "strike", "lastPrice", "bid", "ask",
    "impliedVolatility", "openInterest", "volume",
]


def _cache_get(key: str, ttl: int):
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    return None


def _cache_set(key: str, value) -> None:
    _CACHE[key] = (time.time(), value)


def _request(path: str, params: dict) -> Optional[dict]:
    """Authenticated GET against the Tradier API. Returns parsed JSON or None."""
    if not settings.tradier_token:
        return None
    url = f"{settings.tradier_base_url.rstrip('/')}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {settings.tradier_token}",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code == 401:
            log.warning("Tradier auth failed (401) — check TRADIER_TOKEN")
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # network / json errors
        log.warning("Tradier request failed for %s: %s", path, exc)
        return None


def data_source_status() -> dict:
    """Report whether the market-data provider is configured and reachable."""
    if not settings.tradier_token:
        return {
            "configured": False,
            "ok": False,
            "provider": "tradier",
            "message": "No TRADIER_TOKEN set. Add a free Tradier token to load "
                       "live data (see README).",
        }
    data = _request("markets/quotes", {"symbols": "SPY"})
    ok = bool(data and data.get("quotes"))
    return {
        "configured": True,
        "ok": ok,
        "provider": "tradier",
        "base_url": settings.tradier_base_url,
        "message": "Connected to Tradier." if ok else
                   "Token set but Tradier did not return data — verify the token "
                   "and that it matches the base URL (sandbox vs production).",
    }


def get_history(symbol: str, years: int = 5, interval: str = "daily") -> pd.DataFrame:
    """Return a daily OHLCV DataFrame for ``symbol`` (Title-cased columns)."""
    key = f"hist:{symbol}:{years}:{interval}"
    cached = _cache_get(key, _HISTORY_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(max(years, 1) * 365.25) + 5)
    data = _request(
        "markets/history",
        {
            "symbol": symbol,
            "interval": interval,
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    )
    df = pd.DataFrame()
    if data and data.get("history"):
        days = data["history"].get("day", [])
        if isinstance(days, dict):  # single row comes back as a dict
            days = [days]
        if days:
            df = pd.DataFrame(days)
            df = df.rename(
                columns={
                    "open": "Open", "high": "High", "low": "Low",
                    "close": "Close", "volume": "Volume",
                }
            )
            df["Date"] = pd.to_datetime(df["date"])
            df = df.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]]
            df = df.astype(float).sort_index()
    _cache_set(key, df)
    return df


def get_quote(symbol: str) -> Optional[float]:
    """Return the latest price for ``symbol`` (best-effort)."""
    key = f"quote:{symbol}"
    cached = _cache_get(key, _PRICE_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]

    data = _request("markets/quotes", {"symbols": symbol})
    price = None
    if data and data.get("quotes") and data["quotes"].get("quote"):
        q = data["quotes"]["quote"]
        if isinstance(q, list):
            q = q[0] if q else {}
        price = q.get("last") or q.get("close") or q.get("prevclose")
        price = float(price) if price else None

    if not price:  # fall back to the most recent close in history
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

    data = _request(
        "markets/options/expirations",
        {"symbol": symbol, "includeAllRoots": "true", "strikes": "false"},
    )
    exps: list[str] = []
    if data and data.get("expirations") and data["expirations"].get("date"):
        dates = data["expirations"]["date"]
        exps = dates if isinstance(dates, list) else [dates]
    _cache_set(key, exps)
    return exps


def get_option_chain(symbol: str, expiry: str) -> dict[str, pd.DataFrame]:
    """Return {'calls': df, 'puts': df} for one expiration date."""
    key = f"chain:{symbol}:{expiry}"
    cached = _cache_get(key, _CHAIN_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]

    data = _request(
        "markets/options/chains",
        {"symbol": symbol, "expiration": expiry, "greeks": "true"},
    )
    calls_rows, puts_rows = [], []
    if data and data.get("options") and data["options"].get("option"):
        options = data["options"]["option"]
        if isinstance(options, dict):
            options = [options]
        for opt in options:
            greeks = opt.get("greeks") or {}
            row = {
                "contractSymbol": opt.get("symbol", ""),
                "strike": float(opt.get("strike", 0) or 0),
                "lastPrice": float(opt.get("last") or 0),
                "bid": float(opt.get("bid") or 0),
                "ask": float(opt.get("ask") or 0),
                "impliedVolatility": float(greeks.get("mid_iv") or 0),
                "openInterest": int(opt.get("open_interest") or 0),
                "volume": int(opt.get("volume") or 0),
            }
            if opt.get("option_type") == "call":
                calls_rows.append(row)
            elif opt.get("option_type") == "put":
                puts_rows.append(row)

    out = {
        "calls": pd.DataFrame(calls_rows, columns=_CHAIN_COLUMNS),
        "puts": pd.DataFrame(puts_rows, columns=_CHAIN_COLUMNS),
    }
    _cache_set(key, out)
    return out


def days_to_expiry(expiry: str) -> int:
    try:
        exp = datetime.strptime(expiry, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return max((exp - datetime.now(timezone.utc)).days, 0)
