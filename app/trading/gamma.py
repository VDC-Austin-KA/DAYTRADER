"""Dealer gamma exposure (GEX): trade the crowd's positioning, not the crowd.

WHY THIS EXISTS
---------------
The Surge Score tried to predict direction and had no edge (see app/backtest:
48.5% hit rate out of sample). This takes the opposite approach the user
proposed -- rather than copying what everyone is doing, measure what their
positioning FORCES them to do next, and stand on the other side of it.

The mechanism is not a theory, it is an obligation. 0DTE contracts are now
40-50% of SPX option volume, and every one of them sits on a dealer's book.
Dealers hedge delta-neutrally, so their inventory gamma dictates their
required flow:

* Dealers LONG gamma  -> they sell rallies and buy dips to stay neutral.
  Their hedging DAMPENS moves. Expect mean reversion and strike pinning.
* Dealers SHORT gamma -> they buy rallies and sell dips. Their hedging
  AMPLIFIES moves. Expect momentum and breakouts.

Dim, Eraker and Vilkov (SSRN 4692190) find exactly this empirically:
market-maker inventory gamma is negatively related to future intraday
volatility, and positive (negative) inventory gamma strengthens intraday
price reversal (momentum).

THE KEY INSIGHT FOR US
----------------------
GEX is not a directional signal -- it will not tell you whether SPY goes up
or down, and anyone selling it that way is overselling it. It is a REGIME
signal: it says which KIND of strategy should work in the next few hours.
That is precisely what my backtest was missing. Surge fired the same way in
both regimes, so its reversion wins and momentum wins cancelled to zero. A
regime filter is the difference between an unconditional coin flip and a
conditional edge.

MEASUREMENT CAVEAT, STATED UP FRONT
-----------------------------------
True dealer positioning is not public. The standard convention -- calls are
dealer-long gamma, puts dealer-short -- is an ASSUMPTION about who sold
what, and it is wrong at the margin (customers buy calls too). So treat the
SIGN and the RELATIVE LEVEL as informative and the absolute dollar figure as
indicative only. This is why it is built as a regime filter for strategies
that are tested independently, not as a standalone trade trigger.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..data import market_data as md

log = logging.getLogger("daytrader.gamma")

CONTRACT_MULTIPLIER = 100
# GEX is conventionally quoted per 1% move in the underlying.
PCT_MOVE = 0.01


@dataclass
class GammaProfile:
    symbol: str
    spot: float
    expiry: str
    total_gex: float               # $ per 1% move; >0 dealers long gamma
    call_gex: float
    put_gex: float
    flip_point: float | None       # spot level where cumulative GEX crosses 0
    largest_strike: float | None   # biggest single-strike gamma ("the pin")
    largest_strike_gex: float
    regime: str                    # "reversion" | "momentum" | "neutral"
    by_strike: list[dict] = field(default_factory=list)

    def describe(self) -> str:
        pin = f", pin ${self.largest_strike:g}" if self.largest_strike else ""
        flip = f", flip ${self.flip_point:,.2f}" if self.flip_point else ""
        return (
            f"{self.symbol} spot ${self.spot:,.2f} | GEX "
            f"${self.total_gex/1e6:,.1f}M/1% -> {self.regime.upper()}{pin}{flip}"
        )


def compute_gamma_profile(
    symbol: str, expiry: str | None = None, band: float = 0.05
) -> GammaProfile | None:
    """Dealer gamma exposure by strike for one expiry. None if no data.

    ``band`` limits the flip-point search to strikes within this fraction of
    spot; far-out strikes carry negligible gamma and only add noise.
    """
    spot = md.get_quote(symbol)
    if not spot:
        return None
    if expiry is None:
        expiries = md.get_expirations(symbol)
        if not expiries:
            return None
        expiry = expiries[0]

    chain = md.get_option_chain(symbol, expiry)
    calls, puts = chain.get("calls"), chain.get("puts")
    if calls is None or puts is None or calls.empty:
        return None

    # moomoo does not return gamma in the chain columns we normalise, so
    # pull it per contract from the snapshot layer when available.
    gammas = _gamma_by_contract(symbol, expiry)
    if not gammas:
        return None

    rows: list[dict] = []
    call_gex = put_gex = 0.0
    for side, df in (("call", calls), ("put", puts)):
        for _, r in df.iterrows():
            code = str(r.get("contractSymbol") or "")
            g = gammas.get(code)
            oi = float(r.get("openInterest") or 0)
            if not g or oi <= 0:
                continue
            # Dollar gamma per 1% move.
            dollar_gex = g * oi * CONTRACT_MULTIPLIER * (spot ** 2) * PCT_MOVE
            # Convention: dealers are assumed short customer calls (long
            # gamma) and long customer puts (short gamma). An assumption,
            # not an observation -- see the module docstring.
            signed = dollar_gex if side == "call" else -dollar_gex
            if side == "call":
                call_gex += signed
            else:
                put_gex += signed
            rows.append({
                "strike": float(r["strike"]), "side": side,
                "gamma": g, "open_interest": oi, "gex": signed,
            })

    if not rows:
        return None

    total = call_gex + put_gex
    by_strike: dict[float, float] = {}
    for r in rows:
        by_strike[r["strike"]] = by_strike.get(r["strike"], 0.0) + r["gex"]

    # The pin: the strike with the most absolute gamma nearby. Dealer hedging
    # is heaviest here, so price tends to gravitate to it in a long-gamma
    # regime.
    near = {k: v for k, v in by_strike.items()
            if abs(k - spot) / spot <= band} or by_strike
    largest = max(near.items(), key=lambda kv: abs(kv[1]))

    return GammaProfile(
        symbol=symbol, spot=spot, expiry=expiry,
        total_gex=total, call_gex=call_gex, put_gex=put_gex,
        flip_point=_flip_point(by_strike, spot, band),
        largest_strike=largest[0], largest_strike_gex=largest[1],
        regime=("reversion" if total > 0 else "momentum") if abs(total) > 1e5
               else "neutral",
        by_strike=[{"strike": k, "gex": v} for k, v in sorted(by_strike.items())],
    )


def _flip_point(by_strike: dict[float, float], spot: float, band: float):
    """Strike where cumulative GEX flips sign -- the regime boundary.

    Above it dealers are net long gamma (stabilising); below it, short
    (amplifying). Crossing it intraday is the moment behaviour changes.
    """
    strikes = sorted(by_strike)
    cum = 0.0
    prev_k, prev_c = None, None
    for k in strikes:
        cum += by_strike[k]
        if prev_c is not None and (prev_c < 0 <= cum or prev_c > 0 >= cum):
            # Linear interpolation between the bracketing strikes.
            span = cum - prev_c
            if span:
                return round(prev_k + (k - prev_k) * (-prev_c / span), 2)
            return k
        prev_k, prev_c = k, cum
    return None


def _gamma_by_contract(symbol: str, expiry: str) -> dict[str, float]:
    """{contract_code: gamma} from the moomoo snapshot layer."""
    from ..data import moomoo_data as mm

    if not mm.configured():
        return {}
    contracts = mm._call(
        "get_option_chain", code=mm._us_code(symbol), start=expiry, end=expiry
    )
    if contracts is None or len(contracts) == 0:
        return {}
    spot = md.get_quote(symbol) or 0
    if spot:
        lo, hi = spot * 0.88, spot * 1.12
        contracts = contracts[
            (contracts["strike_price"] >= lo) & (contracts["strike_price"] <= hi)
        ]
    codes = contracts["code"].tolist()
    out: dict[str, float] = {}
    for i in range(0, len(codes), 400):
        snap = mm._call("get_market_snapshot", codes[i:i + 400])
        if snap is None or len(snap) == 0:
            continue
        for _, r in snap.iterrows():
            try:
                g = float(r.get("option_gamma") or 0)
            except (TypeError, ValueError):
                g = 0.0
            if g:
                out[str(r["code"])] = g
    return out
