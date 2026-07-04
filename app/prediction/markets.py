"""Discovery and quotes for BTC hourly prediction-market contracts.

moomoo's prediction markets are Kalshi event contracts, so contract
discovery and pricing use Kalshi's public (keyless) market-data API —
series ``KXBTCD`` is the hourly "Bitcoin above/below X" ladder. Results
are TTL-cached and requests retried with backoff so the bot stays well
inside rate limits.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from ..config import settings

log = logging.getLogger("daytrader.prediction.markets")

_CACHE: dict[str, tuple[float, object]] = {}
_MARKETS_TTL = 20  # seconds


@dataclass
class MarketQuote:
    ticker: str
    series: str
    strike: float
    close_time: datetime  # aware UTC
    yes_bid: float = 0.0  # dollars per contract (contract pays $1)
    yes_ask: float = 0.0
    no_bid: float = 0.0
    no_ask: float = 0.0
    subtitle: str = ""

    def minutes_remaining(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (self.close_time - now).total_seconds() / 60.0


def _get(path: str, params: dict) -> Optional[dict]:
    """GET with small retry/backoff; returns parsed JSON or None."""
    url = f"{settings.kalshi_base_url}{path}"
    delay = 1.0
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"status {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            if attempt == 2:
                log.warning("market-data request failed (%s %s): %s", path, params, exc)
                return None
            time.sleep(delay)
            delay *= 2
    return None


def _parse_close(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_strike(m: dict) -> Optional[float]:
    for key in ("floor_strike", "cap_strike", "strike"):
        v = m.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _price(m: dict, base: str) -> float:
    """Price in dollars from either the ``*_dollars`` string or legacy cents."""
    v = m.get(f"{base}_dollars")
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    v = m.get(base)
    if v is not None:
        try:
            return float(v) / 100.0
        except (TypeError, ValueError):
            pass
    return 0.0


def get_market(ticker: str) -> Optional[dict]:
    """Single-market detail — the list endpoint omits quotes (cached)."""
    key = f"market:{ticker}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _MARKETS_TTL:
        return hit[1]  # type: ignore[return-value]
    payload = _get(f"/markets/{ticker}", {})
    market = (payload or {}).get("market")
    if market:
        _CACHE[key] = (time.time(), market)
    return market


def fetch_open_markets(series: str | None = None) -> list[dict]:
    """Open markets for the configured hourly BTC series (cached)."""
    series = series or settings.prediction_series_ticker
    key = f"markets:{series}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _MARKETS_TTL:
        return hit[1]  # type: ignore[return-value]
    payload = _get(
        "/markets", {"series_ticker": series, "status": "open", "limit": 200}
    )
    markets = (payload or {}).get("markets", []) or []
    _CACHE[key] = (time.time(), markets)
    return markets


def select_hourly_market(
    spot: float, now: datetime | None = None
) -> Optional[MarketQuote]:
    """Pick the next-settling contract with strike nearest the BTC spot.

    Range ("between") markets are skipped — the bot trades the simple
    above/below ladder where the closed-form probability model applies.
    """
    now = now or datetime.now(timezone.utc)
    candidates: list[MarketQuote] = []
    for m in fetch_open_markets():
        if m.get("strike_type") not in (None, "", "greater", "greater_or_equal"):
            continue
        strike = _extract_strike(m)
        close_time = _parse_close(m.get("close_time") or "")
        if strike is None or close_time is None:
            continue
        minutes = (close_time - now).total_seconds() / 60.0
        # Only the contract settling within roughly the next hour.
        if minutes <= 0 or minutes > 70:
            continue
        candidates.append(
            MarketQuote(
                ticker=m.get("ticker", ""),
                series=settings.prediction_series_ticker,
                strike=strike,
                close_time=close_time,
                subtitle=m.get("subtitle") or m.get("yes_sub_title") or "",
            )
        )
    if not candidates:
        return None
    soonest_close = min(c.close_time for c in candidates)
    at_next_hour = [c for c in candidates if c.close_time == soonest_close]
    pick = min(at_next_hour, key=lambda c: abs(c.strike - spot))

    detail = get_market(pick.ticker)
    if detail:
        pick.yes_bid = _price(detail, "yes_bid")
        pick.yes_ask = _price(detail, "yes_ask")
        pick.no_bid = _price(detail, "no_bid")
        pick.no_ask = _price(detail, "no_ask")
        pick.subtitle = detail.get("no_sub_title") or pick.subtitle
    return pick
