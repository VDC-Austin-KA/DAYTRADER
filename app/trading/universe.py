"""Dynamic movers universe: what is actually in play today, not a fixed list.

A hardcoded watchlist goes stale the moment attention moves. This ranks a
broad candidate pool each scan on the things that precede tradeable option
moves:

* LIQUIDITY  -- dollar turnover. Options on thin names have spreads that
                eat the trade regardless of how good the signal is, so this
                is a gate before it is a score.
* UNUSUAL VOLUME -- today's volume against its own 20-day average. The
                classic "something is happening here" tell.
* MOVEMENT   -- absolute change and intraday amplitude. Options need
                movement; direction is scored elsewhere.
* SHORT INTEREST -- short-sell rate. Heavily shorted names squeeze, and
                squeezes are the outsized option moves worth catching.

moomoo also exposes get_top_movers_rank / get_hot_list /
get_short_selling_rank. Those are used opportunistically -- they returned
empty on this account outside market hours, so they can add names but are
never depended on. Everything above is computed from get_market_snapshot,
which is verified working.
"""
from __future__ import annotations

import logging
import time

from ..config import settings

log = logging.getLogger("daytrader.universe")

# Broad candidate pool: liquid optionable US names across the sectors that
# actually move. Scored down to the working set each scan.
CANDIDATE_POOL = [
    # Index / leveraged
    "SPY", "QQQ", "IWM", "DIA", "TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL",
    # Mega / momentum tech
    "NVDA", "AMD", "INTC", "MU", "AVGO", "SMCI", "TSM", "ARM", "MRVL",
    "AAPL", "MSFT", "META", "GOOGL", "AMZN", "NFLX", "TSLA", "PLTR",
    # High-beta / retail favourites / squeeze candidates
    "COIN", "MSTR", "RIVN", "LCID", "SOFI", "HOOD", "RIOT", "MARA",
    "GME", "AMC", "CVNA", "UPST", "AFRM", "DKNG", "RBLX", "SNAP",
    "ASTS", "IONQ", "RGTI", "QBTS", "OKLO", "SMR", "JOBY", "ACHR",
    # Energy / commodity / macro
    "XLE", "GUSH", "USO", "GLD", "SLV", "UNG", "TLT", "XLF", "XBI",
    # Recent movers
    "SNDK", "WDC", "STX", "DELL", "ORCL", "CRWD", "PANW", "NOW",
]

_CACHE_TTL = 300.0
_cache: dict = {"ts": 0.0, "universe": []}


def _num(row, col, default=0.0) -> float:
    try:
        v = row.get(col)
        if v is None or v != v or v == "N/A":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def score_candidates(limit: int = 18, min_turnover: float = 5e7,
                     max_price: float | None = None) -> list[dict]:
    """Rank the pool. Returns the top ``limit`` with their component scores.

    ``max_price`` gates on TRADEABILITY, not quality. An ATM weekly premium
    runs roughly 1-3% of spot, so a $1,350 stock has ~$20-40 options -- far
    past the movers premium cap, and past what this account can fund. A
    name whose options cannot be bought is not a trade idea however hot it
    is, and ranking it merely pushes out names that are.
    """
    from ..data import moomoo_data as mm

    if not mm.configured() or not mm._reachable():
        return []

    rows: list[dict] = []
    codes = [mm._us_code(s) for s in CANDIDATE_POOL]
    for i in range(0, len(codes), 200):          # snapshot batch limit
        snap = mm._call("get_market_snapshot", codes[i:i + 200])
        if snap is None or not len(snap):
            continue
        for _, r in snap.iterrows():
            sym = str(r.get("code", "")).replace("US.", "")
            price = _num(r, "last_price")
            prev = _num(r, "prev_close_price") or price
            turnover = _num(r, "turnover")
            vol = _num(r, "volume")
            if price <= 0 or turnover < min_turnover:
                continue          # illiquid: option spreads would eat it
            if max_price and price > max_price:
                continue          # ATM premium would exceed the cap
            change_pct = ((price / prev - 1.0) * 100) if prev else 0.0
            rows.append({
                "symbol": sym, "price": round(price, 2),
                "change_pct": round(change_pct, 2),
                "turnover": turnover, "volume": vol,
                "volume_ratio": _num(r, "volume_ratio", 1.0),
                "amplitude": _num(r, "amplitude"),
                "short_rate": _num(r, "short_sell_rate"),
            })
    if not rows:
        return []

    # Percentile-rank each component so one large-cap's raw turnover cannot
    # dominate; we want relative standing within today's pool.
    def pct_rank(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        out = [0.0] * len(vals)
        for pos, i in enumerate(order):
            out[i] = pos / max(len(vals) - 1, 1)
        return out

    liq = pct_rank([r["turnover"] for r in rows])
    unusual = pct_rank([r["volume_ratio"] for r in rows])
    move = pct_rank([abs(r["change_pct"]) for r in rows])
    amp = pct_rank([r["amplitude"] for r in rows])
    short = pct_rank([r["short_rate"] for r in rows])

    for i, r in enumerate(rows):
        # Weights: unusual volume and movement lead, liquidity is a
        # multiplier-ish floor, short interest is a kicker for squeezes.
        r["liquidity_rank"] = round(liq[i], 3)
        r["unusual_rank"] = round(unusual[i], 3)
        r["move_rank"] = round(move[i], 3)
        r["short_rank"] = round(short[i], 3)
        r["heat"] = round(100 * (
            0.30 * unusual[i] + 0.25 * move[i] + 0.20 * amp[i]
            + 0.15 * liq[i] + 0.10 * short[i]
        ), 1)
    rows.sort(key=lambda r: -r["heat"])
    return rows[:limit]


def _rank_extras(limit: int = 6) -> list[str]:
    """Names from moomoo's own hot/movers/short rankings, if entitled.

    Returns [] rather than raising when unavailable -- these were empty on
    this account outside market hours, so they enrich but never gate.
    """
    from ..data import moomoo_data as mm

    out: list[str] = []
    if not mm.configured() or not mm._reachable():
        return out
    try:
        from moomoo import Market

        for fn, kw in (
            ("get_top_movers_rank", {"market": Market.US, "count": limit}),
            ("get_hot_list", {"market": Market.US, "count": limit}),
            ("get_short_selling_rank", {"market": Market.US, "count": limit}),
        ):
            df = mm._call(fn, **kw)
            if df is None or not hasattr(df, "columns") or not len(df):
                continue
            col = next((c for c in ("code", "stock_code") if c in df.columns), None)
            if col:
                out += [str(x).replace("US.", "") for x in df[col].tolist()]
    except Exception as exc:
        log.debug("rank extras unavailable: %s", exc)
    return out


def affordable_price_ceiling() -> float:
    """Highest underlying price whose ATM options plausibly fit the cap.

    Premium ~= 2% of spot for a near-dated ATM contract, so the ceiling is
    about 50x the max premium. Deliberately generous: the chain scan does
    the exact filtering, this only stops obviously untradeable names from
    crowding out the rest.
    """
    return max(50.0, settings.movers_max_premium * 50)


def get_universe(refresh: bool = False, limit: int = 18) -> list[str]:
    """Today's working watchlist. Falls back to the configured list."""
    now = time.time()
    if not refresh and _cache["universe"] and now - _cache["ts"] < _CACHE_TTL:
        return _cache["universe"]

    scored = score_candidates(limit=limit, max_price=affordable_price_ceiling())
    syms = [r["symbol"] for r in scored]
    for extra in _rank_extras():
        if extra and extra not in syms and len(syms) < limit + 6:
            syms.append(extra)
    if not syms:
        syms = list(settings.movers_watchlist)
        log.warning("dynamic universe empty; using configured watchlist")
    _cache.update(ts=now, universe=syms)
    log.info("universe (%d): %s", len(syms), ", ".join(syms[:12]))
    return syms


def universe_detail(refresh: bool = False, limit: int = 18) -> list[dict]:
    """Scored rows, for showing WHY each name is in the list."""
    if refresh:
        _cache["ts"] = 0.0
    return score_candidates(limit=limit, max_price=affordable_price_ceiling())
