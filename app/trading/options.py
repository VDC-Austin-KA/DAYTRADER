"""Options-chain scanning constrained to short-dated contracts (< 1 month).

Selects a liquid, near-the-money contract in the desired direction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from ..config import settings
from ..data import market_data as md


@dataclass
class OptionPick:
    symbol: str
    option_type: str  # call / put
    contract_symbol: str
    strike: float
    expiry: str
    dte: int
    last_price: float
    bid: float
    ask: float
    implied_vol: float
    open_interest: int
    volume: int
    underlying: float

    @property
    def mid_price(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return round((self.bid + self.ask) / 2, 2)
        return self.last_price

    @property
    def breakeven(self) -> float:
        if self.option_type == "call":
            return round(self.strike + self.mid_price, 2)
        return round(self.strike - self.mid_price, 2)


def _liquidity_score(row: pd.Series) -> float:
    oi = float(row.get("openInterest", 0) or 0)
    vol = float(row.get("volume", 0) or 0)
    bid = float(row.get("bid", 0) or 0)
    ask = float(row.get("ask", 0) or 0)
    spread_pen = 0.0
    if bid > 0 and ask > 0:
        spread_pen = (ask - bid) / ((ask + bid) / 2)
    return oi + 2 * vol - 50 * spread_pen


def select_contract(
    symbol: str,
    direction: str,
    underlying: Optional[float] = None,
) -> Optional[OptionPick]:
    """Pick the best short-dated call/put for the given direction."""
    if direction not in ("call", "put"):
        return None
    underlying = underlying or md.get_quote(symbol)
    if not underlying:
        return None

    candidates: list[OptionPick] = []
    for expiry in md.get_expirations(symbol):
        dte = md.days_to_expiry(expiry)
        if dte < settings.min_dte or dte > settings.max_dte:
            continue
        chain = md.get_option_chain(symbol, expiry)
        df = chain["calls"] if direction == "call" else chain["puts"]
        if df is None or df.empty:
            continue

        # Near the money: within +/-12% of spot.
        df = df.copy()
        df["moneyness"] = (df["strike"] - underlying).abs() / underlying
        df = df[df["moneyness"] <= 0.12]
        if df.empty:
            continue
        df["liq"] = df.apply(_liquidity_score, axis=1)
        # Prefer liquid contracts close to the money.
        df = df.sort_values(["liq", "moneyness"], ascending=[False, True])
        row = df.iloc[0]

        pick = OptionPick(
            symbol=symbol,
            option_type=direction,
            contract_symbol=str(row.get("contractSymbol", "")),
            strike=float(row.get("strike", 0)),
            expiry=expiry,
            dte=dte,
            last_price=float(row.get("lastPrice", 0) or 0),
            bid=float(row.get("bid", 0) or 0),
            ask=float(row.get("ask", 0) or 0),
            implied_vol=float(row.get("impliedVolatility", 0) or 0),
            open_interest=int(row.get("openInterest", 0) or 0),
            volume=int(row.get("volume", 0) or 0),
            underlying=float(underlying),
        )
        if pick.mid_price > 0:
            candidates.append(pick)

    if not candidates:
        return None
    # Among valid expiries, prefer the soonest with a tradable price.
    candidates.sort(key=lambda p: (p.dte, -_liq_of(p)))
    return candidates[0]


def _liq_of(p: OptionPick) -> float:
    return p.open_interest + 2 * p.volume
