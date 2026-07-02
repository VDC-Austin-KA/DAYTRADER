"""Opportunity scanner: rank cheap, short-dated options for one ticker.

Given a symbol, finds contracts that (a) expire within `max_dte` days, (b) cost
no more than `max_premium` per share and `max_cost` total, then scores each by a
blend of *probability of profit* and *potential return*, using the ML directional
model as a tie-breaking edge.

This is intentionally honest: cheap <=3 DTE options are cheap because they are
statistically unlikely to expire in the money. The scanner surfaces them but
reports a real probability of profit so the risk is visible.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

import numpy as np

from ..config import settings
from ..data import market_data as md
from ..ml import model as ml_model
from . import pricing


@dataclass
class Opportunity:
    symbol: str
    option_type: str
    contract_symbol: str
    strike: float
    expiry: str
    dte: int
    underlying: float
    mid: float
    cost: float
    bid: float
    ask: float
    iv: float
    open_interest: int
    volume: int
    breakeven: float
    breakeven_move_pct: float   # % underlying must move to breakeven
    prob_profit: float          # 0-1, lognormal + IV
    model_lean: float           # 0-1, model confidence in required direction
    success: float              # blended chance of success 0-1
    potential_return: float     # fractional return if a ~1-day IV move hits
    score: float                # composite ranking


def _historical_vol(symbol: str) -> float:
    """Annualized realized vol, used when a contract has no usable IV."""
    df = md.get_history(symbol, years=1)
    if df.empty or len(df) < 20:
        return 0.0
    rets = np.log(df["Close"] / df["Close"].shift(1)).dropna()
    if rets.empty:
        return 0.0
    return float(rets.std() * np.sqrt(pricing.TRADING_DAYS))


def scan(
    symbol: str,
    max_dte: int = 3,
    min_dte: int = 0,
    max_premium: float = 1.00,
    max_cost: float = 100.0,
    side: str = "both",          # both / call / put
    limit: int = 25,
) -> dict:
    symbol = symbol.upper()
    underlying = md.get_quote(symbol)
    if not underlying:
        return {"symbol": symbol, "underlying": None, "opportunities": [],
                "error": "No quote/data for this symbol."}

    # Directional edge from the ML model (neutral 0.5 if untrained).
    df = md.get_history(symbol, years=settings.history_years)
    model_prob = ml_model.predict_latest(symbol, df) if not df.empty else None
    model_trained = model_prob is not None
    hist_vol = _historical_vol(symbol)

    sides = ["call", "put"] if side == "both" else [side]
    results: list[Opportunity] = []

    for expiry in md.get_expirations(symbol):
        dte = md.days_to_expiry(expiry)
        if dte < min_dte or dte > max_dte:
            continue
        chain = md.get_option_chain(symbol, expiry)
        for opt_type in sides:
            df_side = chain["calls"] if opt_type == "call" else chain["puts"]
            if df_side is None or df_side.empty:
                continue
            for _, row in df_side.iterrows():
                bid = float(row.get("bid", 0) or 0)
                ask = float(row.get("ask", 0) or 0)
                last = float(row.get("lastPrice", 0) or 0)
                mid = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else last
                if mid <= 0 or mid > max_premium:
                    continue
                cost = mid * 100
                if cost > max_cost:
                    continue

                strike = float(row.get("strike", 0) or 0)
                iv = float(row.get("impliedVolatility", 0) or 0)
                sigma = iv if iv > 0.01 else hist_vol
                if sigma <= 0:
                    continue

                oi = int(row.get("openInterest", 0) or 0)
                vol = int(row.get("volume", 0) or 0)
                # Skip totally illiquid/untradeable contracts.
                if bid <= 0 and vol == 0 and oi == 0:
                    continue

                breakeven = (strike + mid) if opt_type == "call" else (strike - mid)
                be_move = abs(breakeven - underlying) / underlying
                T = max(dte, 0.5) / 365.0

                pop = pricing.prob_of_profit(underlying, breakeven, T, sigma, opt_type)
                pot = pricing.potential_return(underlying, strike, dte, sigma, mid, opt_type)

                if model_trained:
                    lean = model_prob if opt_type == "call" else (1 - model_prob)
                else:
                    lean = 0.5
                success = 0.6 * pop + 0.4 * lean
                # Composite: reward success and upside, cap runaway lotto return.
                score = success * (1 + min(max(pot, 0.0), 3.0))

                results.append(
                    Opportunity(
                        symbol=symbol, option_type=opt_type,
                        contract_symbol=str(row.get("contractSymbol", "")),
                        strike=strike, expiry=expiry, dte=dte,
                        underlying=round(underlying, 2), mid=mid,
                        cost=round(cost, 2), bid=bid, ask=ask,
                        iv=round(sigma, 4), open_interest=oi, volume=vol,
                        breakeven=round(breakeven, 2),
                        breakeven_move_pct=round(be_move * 100, 2),
                        prob_profit=round(pop, 4),
                        model_lean=round(lean, 4),
                        success=round(success, 4),
                        potential_return=round(pot, 4),
                        score=round(score, 4),
                    )
                )

    results.sort(key=lambda o: o.score, reverse=True)
    return {
        "symbol": symbol,
        "underlying": round(underlying, 2),
        "model_trained": model_trained,
        "model_prob": round(model_prob, 4) if model_trained else None,
        "count": len(results),
        "opportunities": [asdict(o) for o in results[:limit]],
    }
