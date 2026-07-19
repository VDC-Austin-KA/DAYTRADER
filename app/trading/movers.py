"""Universe-wide movers scan: Surge Score, ranked options, plays, headlines.

Instead of scanning one ticker at a time, this module sweeps a watchlist of
high-volume, high-beta names and surfaces the best option contracts across
the WHOLE universe, ranked by blended likelihood of profit.

The custom indicator — **Surge Score** (0-100) — estimates how primed a name
is for a significant near-term price move, from signals that have
historically preceded expansions:

* **Squeeze** (40%): Bollinger-band *width percentile* over the past year.
  Unusually tight bands mean compressed volatility, and volatility is
  mean-reverting — tight coils precede big expansions (the classic
  TTM-squeeze observation).
* **Burst** (30%): is the move already igniting? Today's range/return vs.
  its own ATR plus the volume ratio against the 20-day average. Volume
  confirms; price moves on air fade.
* **Momentum** (30%): |z-score| of the 5-day rate of change — persistent
  directional pressure rather than one-day noise. Its sign (helped by
  band position) sets the directional lean.

A separate **whipsaw gauge** (Kaufman efficiency ratio < 0.35 while ATR%
sits in its upper half) flags names that are moving hard but *chopping* —
big swings that keep reversing. For those, the suggested play is the
two-step the user asked for: buy the option on the signal leg, then when
the swing runs, SELL a further-OTM contract of the same type/expiry to
recoup the entry cost — a "free" vertical with risk taken off the table.

Nothing here is a guarantee: these are statistical tendencies surfaced
with their inputs visible so the risk stays honest.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field

import numpy as np

from ..config import settings
from ..data import market_data as md
from . import opportunities as opp

log = logging.getLogger("daytrader.trading.movers")

# Scan results are expensive (N tickers x chains) — cache for a short TTL.
_SCAN_TTL = 120.0
_scan_cache: dict = {"ts": 0.0, "result": None}
# Previous blended top-scores, used to detect "recently became hot" headlines.
_prev_scores: dict[str, float] = {}


@dataclass
class SurgeReading:
    symbol: str
    price: float
    surge: float            # 0-100 composite
    squeeze: float          # 0-1 band-width tightness percentile
    burst: float            # 0-1 ignition (range + volume confirmation)
    momentum_z: float       # signed z-score of 5d ROC
    direction: str          # up / down / neutral
    whipsaw: bool           # choppy big-swing regime
    day_change_pct: float
    volume_ratio: float


@dataclass
class Play:
    symbol: str
    surge: float
    direction: str
    whipsaw: bool
    contract_symbol: str
    option_type: str
    strike: float
    expiry: str
    dte: int
    mid: float
    cost: float
    prob_profit: float
    success: float
    potential_return: float
    blended_score: float
    entry: str
    exit_plan: str
    wing_plan: str = ""     # only for whipsaw names: the free-spread leg
    headline: str = ""


def compute_surge(symbol: str) -> SurgeReading | None:
    """Surge Score from daily history + the latest quote. None if no data."""
    df = md.get_history(symbol, years=1)
    if df.empty or len(df) < 60:
        return None
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    price = md.get_quote(symbol) or float(close.iloc[-1])

    # --- Squeeze: Bollinger width percentile (lower width -> higher score).
    ma20 = close.rolling(20).mean()
    sd20 = close.rolling(20).std()
    width = ((4 * sd20) / ma20).dropna()
    if len(width) < 30:
        return None
    # Fraction of the past year whose bands were WIDER than today's: 1.0 means
    # today is the tightest coil of the year.
    squeeze = float((width > width.iloc[-1]).mean())

    # --- ATR% and today's burst.
    prev_close = close.shift(1)
    tr = np.maximum(high - low, np.maximum(abs(high - prev_close), abs(low - prev_close)))
    atr_pct = (tr.rolling(14).mean() / close).dropna()
    day_ret = price / float(close.iloc[-2]) - 1.0 if len(close) > 1 else 0.0
    range_burst = min(abs(day_ret) / max(float(atr_pct.iloc[-1]), 1e-4), 2.5) / 2.5
    vol_ratio = float(vol.iloc[-1] / max(vol.rolling(20).mean().iloc[-1], 1.0))
    vol_burst = min(vol_ratio, 3.0) / 3.0
    burst = 0.6 * range_burst + 0.4 * vol_burst

    # --- Momentum: 5-day ROC z-score against its own year of history.
    roc5 = close.pct_change(5).dropna()
    mu, sigma = float(roc5.mean()), float(roc5.std() or 1e-9)
    mom_z = (float(roc5.iloc[-1]) - mu) / sigma
    momentum = min(abs(mom_z), 3.0) / 3.0

    surge = round(100 * (0.40 * squeeze + 0.30 * burst + 0.30 * momentum), 1)

    # Directional lean: momentum sign, confirmed by position in the bands.
    band_pos = float((price - ma20.iloc[-1]) / max(2 * sd20.iloc[-1], 1e-9))
    lean_raw = 0.7 * np.sign(mom_z) * min(abs(mom_z), 2) + 0.3 * np.clip(band_pos, -2, 2)
    direction = "up" if lean_raw > 0.25 else "down" if lean_raw < -0.25 else "neutral"

    # Whipsaw: low efficiency (net move << path length) while ranges are big.
    diffs = close.diff().dropna().tail(10)
    path = float(diffs.abs().sum())
    net = abs(float(close.iloc[-1] - close.iloc[-11])) if len(close) > 11 else path
    efficiency = net / path if path > 0 else 1.0
    atr_hot = float((atr_pct.iloc[-1] >= atr_pct.quantile(0.5)))
    whipsaw = efficiency < 0.35 and atr_hot > 0

    return SurgeReading(
        symbol=symbol, price=round(price, 2), surge=surge,
        squeeze=round(squeeze, 3), burst=round(burst, 3),
        momentum_z=round(mom_z, 2), direction=direction, whipsaw=whipsaw,
        day_change_pct=round(day_ret * 100, 2), volume_ratio=round(vol_ratio, 2),
    )


def _build_play(reading: SurgeReading, contract: dict, slate: list[dict]) -> Play:
    """Attach entry/exit/wing guidance to the chosen contract."""
    sym, mid = reading.symbol, contract["mid"]
    otype, strike, expiry = contract["option_type"], contract["strike"], contract["expiry"]
    entry = (
        f"Buy {sym} {expiry} {strike:g}{'C' if otype == 'call' else 'P'} "
        f"@ ~${mid:.2f} (${contract['cost']:.0f}/contract)"
    )
    exit_plan = (
        f"Take profit at +50% (${mid * 1.5:.2f}) or cut at -40% (${mid * 0.6:.2f}); "
        f"never hold into the close on a fading Surge Score."
    )

    wing_plan = ""
    if reading.whipsaw:
        wing = _pick_wing(contract, slate)
        if wing is not None:
            wing_plan = (
                f"Whipsaw tape: if the swing runs your way, SELL the {wing['expiry']} "
                f"{wing['strike']:g}{'C' if otype == 'call' else 'P'} (now ~${wing['mid']:.2f}) "
                f"once it trades >= ${mid:.2f} — that refunds your entry and leaves a "
                f"free {abs(wing['strike'] - strike):g}-wide vertical, so the round trips "
                f"can't hurt you."
            )
        else:
            wing_plan = (
                "Whipsaw tape: after a favorable swing, sell a further-OTM "
                "same-expiry option for >= your entry cost to lock a free vertical."
            )

    blended = round(0.55 * contract["success"] + 0.45 * min(reading.surge / 100, 1.0), 4)
    return Play(
        symbol=sym, surge=reading.surge, direction=reading.direction,
        whipsaw=reading.whipsaw, contract_symbol=contract["contract_symbol"],
        option_type=otype, strike=strike, expiry=expiry, dte=contract["dte"],
        mid=mid, cost=contract["cost"], prob_profit=contract["prob_profit"],
        success=contract["success"], potential_return=contract["potential_return"],
        blended_score=blended, entry=entry, exit_plan=exit_plan, wing_plan=wing_plan,
    )


def _pick_wing(entry: dict, slate: list[dict]) -> dict | None:
    """Further-OTM, same type/expiry contract worth selling later.

    Prefer the closest strike whose CURRENT mid is 40-90% of the entry mid:
    near enough that a favorable swing lifts it to the entry cost (making
    the vertical free), far enough to leave spread width as profit.
    """
    otype, strike, expiry, mid = (
        entry["option_type"], entry["strike"], entry["expiry"], entry["mid"],
    )
    further = [
        c for c in slate
        if c["option_type"] == otype and c["expiry"] == expiry
        and (c["strike"] > strike if otype == "call" else c["strike"] < strike)
        and 0.4 * mid <= c["mid"] <= 0.9 * mid
    ]
    if not further:
        return None
    return min(further, key=lambda c: abs(c["strike"] - strike))


def scan_universe(refresh: bool = False, per_symbol: int = 6) -> dict:
    """Sweep the movers watchlist; return readings, ranked options, plays,
    and headline items. Cached for a couple of minutes."""
    now = time.time()
    if not refresh and _scan_cache["result"] and now - _scan_cache["ts"] < _SCAN_TTL:
        return _scan_cache["result"]

    readings: list[SurgeReading] = []
    all_options: list[dict] = []
    plays: list[Play] = []

    # Dynamic universe: what is actually in play today (unusual volume,
    # movement, liquidity, short interest) rather than a list fixed months
    # ago. Falls back to the configured watchlist if the gateway is down.
    from . import universe as uni

    watchlist = uni.get_universe(refresh=refresh)
    # OpenD caps get_option_chain at 10 calls / 30s. Scanning the whole
    # universe unpaced blew straight through it and most chains came back
    # empty, which surfaced as "0 ranked contracts" with no error. Pace to
    # stay just inside the limit; the scan cache absorbs the extra wall time.
    chain_pause = 30.0 / 9
    for idx, sym in enumerate(watchlist):
        if idx:
            time.sleep(chain_pause)
        try:
            reading = compute_surge(sym)
        except Exception:
            log.exception("surge computation failed for %s", sym)
            continue
        if reading is None:
            continue
        readings.append(reading)

        # Wider slate than the single-ticker scanner: pricier contracts are
        # allowed here because the ranking (not a budget) does the filtering.
        side = (
            "call" if reading.direction == "up"
            else "put" if reading.direction == "down" else "both"
        )
        try:
            res = opp.scan(
                sym, max_dte=settings.movers_max_dte, min_dte=0,
                max_premium=settings.movers_max_premium,
                max_cost=settings.movers_max_premium * 100,
                side=side, limit=40,
            )
        except Exception:
            log.exception("chain scan failed for %s", sym)
            continue
        slate = res.get("opportunities", [])
        for c in slate[:per_symbol]:
            c = dict(c)
            c["surge"] = reading.surge
            c["direction"] = reading.direction
            c["whipsaw"] = reading.whipsaw
            c["blended_score"] = round(
                0.55 * c["success"] + 0.45 * min(reading.surge / 100, 1.0), 4
            )
            all_options.append(c)

        if reading.surge >= settings.movers_surge_threshold and slate:
            plays.append(_build_play(reading, slate[0], slate))

    all_options.sort(key=lambda c: c["blended_score"], reverse=True)
    plays.sort(key=lambda p: p.blended_score, reverse=True)

    headlines = _make_headlines(readings, plays)
    result = {
        "generated_at": now,
        "watchlist": watchlist,
        "readings": [asdict(r) for r in sorted(readings, key=lambda r: r.surge, reverse=True)],
        "options": all_options[:settings.movers_table_size],
        "plays": [asdict(p) for p in plays[:8]],
        "headlines": headlines,
    }
    _scan_cache.update(ts=now, result=result)
    return result


def _make_headlines(readings: list[SurgeReading], plays: list[Play]) -> list[str]:
    """Breaking-style items: names whose scores just jumped + featured plays."""
    global _prev_scores
    items: list[str] = []
    current = {r.symbol: r.surge for r in readings}
    for sym, score in current.items():
        prev = _prev_scores.get(sym)
        if prev is not None and score - prev >= 15 and score >= 50:
            items.append(
                f"🚨 {sym} surge score jumped {prev:.0f} → {score:.0f} — "
                "volatility expansion setting up"
            )
    for r in readings[:]:
        if r.surge >= 75:
            arrow = "▲" if r.direction == "up" else "▼" if r.direction == "down" else "◆"
            items.append(
                f"🔥 {r.symbol} {arrow} surge {r.surge:.0f} "
                f"({r.day_change_pct:+.1f}% today, {r.volume_ratio:.1f}x volume)"
            )
    for p in plays[:3]:
        items.append(
            f"💡 Top play: {p.symbol} {p.expiry} {p.strike:g}"
            f"{'C' if p.option_type == 'call' else 'P'} — "
            f"POP {p.prob_profit * 100:.0f}%, surge {p.surge:.0f}"
            f"{' (whipsaw: free-spread setup)' if p.whipsaw else ''}"
        )
    _prev_scores = current
    # De-dupe while keeping order.
    seen: set[str] = set()
    return [i for i in items if not (i in seen or seen.add(i))][:12]
