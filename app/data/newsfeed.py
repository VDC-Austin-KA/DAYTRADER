"""Market-moving news + economic calendar.

NEWS: free public RSS (MarketWatch, CNBC, Yahoo Finance). No key, no quota.
Headlines are scored for market-moving language -- Fed/CPI/jobs/guidance
language ranks above routine coverage -- so the feed leads with what
actually moves a tape rather than whatever was published most recently.

ECONOMIC CALENDAR: this is the honest gap. There is no reliable free API
giving expected/actual/previous. The good ones (Trading Economics, FMP,
Econoday) are paid or key-gated, and scraping Investing.com breaks their
terms. So the calendar here is a STATIC schedule of the recurring releases
that move equity vol -- CPI, NFP, FOMC, PPI, PCE, claims -- with their
usual release times, and it fetches consensus/actual ONLY if an API key is
configured. Without a key it tells you what is coming and when, which is
the part that matters for not being caught in a 08:30 print, and says
plainly that it has no numbers.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from urllib.request import Request, urlopen

log = logging.getLogger("daytrader.news")

FEEDS = {
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "CNBC": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "Yahoo": "https://finance.yahoo.com/news/rssindex",
}

# Terms that historically precede repricing, weighted by how hard they hit.
MOVERS = {
    3.0: ["fomc", "fed decision", "rate cut", "rate hike", "powell",
          "cpi", "inflation report", "jobs report", "nonfarm", "payrolls"],
    2.0: ["guidance", "earnings beat", "earnings miss", "downgrade",
          "upgrade", "halted", "recall", "bankruptcy", "merger",
          "acquisition", "tariff", "sanctions"],
    1.0: ["earnings", "revenue", "outlook", "forecast", "sec filing",
          "buyback", "dividend", "layoffs", "lawsuit"],
}

_CACHE_TTL = 300.0
_cache: dict = {"ts": 0.0, "items": []}


def _score(title: str) -> float:
    t = title.lower()
    return sum(w for w, terms in MOVERS.items() for k in terms if k in t)


def _fetch(url: str, timeout: float = 6.0) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (DAYTRADER)"})
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def get_news(limit: int = 25, refresh: bool = False) -> list[dict]:
    """Market-moving headlines, highest-impact first."""
    now = time.time()
    if not refresh and _cache["items"] and now - _cache["ts"] < _CACHE_TTL:
        return _cache["items"][:limit]

    items: list[dict] = []
    for source, url in FEEDS.items():
        try:
            root = ET.fromstring(_fetch(url))
        except Exception as exc:
            log.warning("feed %s failed: %s", source, exc)
            continue
        for node in root.iter("item"):
            title = (node.findtext("title") or "").strip()
            if not title:
                continue
            items.append({
                "source": source,
                "title": title,
                "link": (node.findtext("link") or "").strip(),
                "published": (node.findtext("pubDate") or "").strip(),
                "impact": _score(title),
                "tickers": sorted(set(re.findall(r"\b[A-Z]{2,5}\b", title))
                                  - {"THE", "AND", "FOR", "NEW", "CEO", "IPO",
                                     "USA", "GDP", "CPI", "FED"})[:3],
            })
    # Impact first, then recency order within the feed.
    items.sort(key=lambda x: -x["impact"])
    _cache.update(ts=now, items=items)
    if not items:
        log.warning("no headlines fetched from any feed")
    return items[:limit]


# --- Economic calendar ---------------------------------------------------
# Recurring US releases that move equity vol, with their usual ET times.
RECURRING = [
    ("CPI", "08:30", "monthly", 13),
    ("PPI", "08:30", "monthly", 14),
    ("Nonfarm Payrolls", "08:30", "monthly-first-friday", 0),
    ("Initial Jobless Claims", "08:30", "weekly-thursday", 0),
    ("Retail Sales", "08:30", "monthly", 15),
    ("PCE Price Index", "08:30", "monthly", 28),
    ("FOMC Rate Decision", "14:00", "fomc", 0),
    ("ISM Manufacturing PMI", "10:00", "monthly", 1),
    ("Consumer Confidence", "10:00", "monthly", 25),
]
# 2026 FOMC decision dates (published schedule).
FOMC_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
             "2026-07-29", "2026-09-16", "2026-11-04", "2026-12-16"]


def _next_occurrence(kind: str, day_hint: int, frm: date) -> date | None:
    if kind == "fomc":
        for d in FOMC_2026:
            dt = date.fromisoformat(d)
            if dt >= frm:
                return dt
        return None
    if kind == "weekly-thursday":
        return frm + timedelta((3 - frm.weekday()) % 7)
    if kind == "monthly-first-friday":
        first = frm.replace(day=1)
        fri = first + timedelta((4 - first.weekday()) % 7)
        if fri < frm:
            nxt = (first + timedelta(32)).replace(day=1)
            fri = nxt + timedelta((4 - nxt.weekday()) % 7)
        return fri
    try:
        cand = frm.replace(day=min(day_hint, 28))
    except ValueError:
        return None
    if cand < frm:
        nxt = (frm.replace(day=1) + timedelta(32)).replace(day=1)
        cand = nxt.replace(day=min(day_hint, 28))
    return cand


def get_calendar(days: int = 14) -> dict:
    """Upcoming releases. ``has_values`` is False without a data key."""
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=days)
    rows = []
    for name, at, kind, hint in RECURRING:
        when = _next_occurrence(kind, hint, today)
        if when and today <= when <= horizon:
            rows.append({
                "event": name, "date": when.isoformat(), "time_et": at,
                "expected": None, "actual": None, "previous": None,
                "days_out": (when - today).days,
            })
    rows.sort(key=lambda r: (r["date"], r["time_et"]))
    return {
        "events": rows,
        "has_values": False,
        "note": ("Schedule only. Expected/actual/previous need a paid "
                 "calendar API (Trading Economics, FMP, Econoday) -- no "
                 "free source provides them reliably."),
    }
