"""In-memory real-time options chain.

One numpy matrix per expiry: rows are strikes, one plane per right
(call/put), columns are the live fields (bid, ask, delta, gamma, IV, tick
timestamp). Quote ticks from the WebSocket touch two floats and a
timestamp — no allocation on the hot path — while the scanner reads a
consistent per-expiry view. All mutation happens on the event loop, so no
locking is needed.
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np

from .models import OptionContract, Quote, Right

# Field layout of the per-expiry array (axis 2).
F_BID, F_ASK, F_DELTA, F_GAMMA, F_IV, F_TS = range(6)
_N_FIELDS = 6
_RIGHT_IDX = {Right.CALL: 0, Right.PUT: 1}
_GROW = 64  # strike rows allocated at a time


class ExpirySlice:
    """All strikes for one expiry: ``data[strike_idx, right_idx, field]``."""

    def __init__(self, expiry: date) -> None:
        self.expiry = expiry
        self.strikes = np.empty(0, dtype=np.float64)     # sorted, unique
        self.data = np.full((0, 2, _N_FIELDS), np.nan)
        self._index: dict[float, int] = {}

    def _row(self, strike: float) -> int:
        idx = self._index.get(strike)
        if idx is not None:
            return idx
        # New strike: extend (rare — only when a contract is first seen).
        pos = int(np.searchsorted(self.strikes, strike))
        self.strikes = np.insert(self.strikes, pos, strike)
        self.data = np.insert(self.data, pos, np.nan, axis=0)
        self._index = {s: i for i, s in enumerate(self.strikes)}
        return pos

    def update_quote(self, contract: OptionContract, bid: float, ask: float, ts_ns: int) -> None:
        row = self._row(contract.strike)
        plane = self.data[row, _RIGHT_IDX[contract.right]]
        plane[F_BID] = bid
        plane[F_ASK] = ask
        plane[F_TS] = ts_ns

    def update_greeks(self, contract: OptionContract, delta: float, gamma: float, iv: float) -> None:
        row = self._row(contract.strike)
        plane = self.data[row, _RIGHT_IDX[contract.right]]
        plane[F_DELTA] = delta
        plane[F_GAMMA] = gamma
        plane[F_IV] = iv

    def get(self, root: str, strike: float, right: Right) -> Quote | None:
        idx = self._index.get(strike)
        if idx is None:
            return None
        plane = self.data[idx, _RIGHT_IDX[right]]
        if math.isnan(plane[F_BID]):
            return None
        return Quote(
            contract=OptionContract(root=root, expiry=self.expiry, right=right, strike=strike),
            bid=float(plane[F_BID]),
            ask=float(plane[F_ASK]),
            delta=float(plane[F_DELTA]),
            gamma=float(plane[F_GAMMA]),
            iv=float(plane[F_IV]),
            ts_ns=0 if math.isnan(plane[F_TS]) else int(plane[F_TS]),
        )


class ChainStore:
    """Live chain for one underlying, keyed by expiry."""

    def __init__(self, root: str) -> None:
        self.root = root
        self._slices: dict[date, ExpirySlice] = {}
        self.quote_ticks = 0  # ingest counter for health logging

    def _slice(self, expiry: date) -> ExpirySlice:
        sl = self._slices.get(expiry)
        if sl is None:
            sl = self._slices[expiry] = ExpirySlice(expiry)
        return sl

    def update_quote(self, contract: OptionContract, bid: float, ask: float, ts_ns: int) -> None:
        self._slice(contract.expiry).update_quote(contract, bid, ask, ts_ns)
        self.quote_ticks += 1

    def update_greeks(self, contract: OptionContract, delta: float, gamma: float, iv: float) -> None:
        self._slice(contract.expiry).update_greeks(contract, delta, gamma, iv)

    def quote(self, contract: OptionContract) -> Quote | None:
        sl = self._slices.get(contract.expiry)
        return sl.get(self.root, contract.strike, contract.right) if sl else None

    def expiries(self, min_dte: int, max_dte: int, today: date | None = None) -> list[date]:
        today = today or date.today()
        return sorted(
            e for e in self._slices if min_dte <= (e - today).days <= max_dte
        )

    def quotes_for(self, expiry: date, right: Right) -> list[Quote]:
        """All quotable strikes for one expiry/right, sorted by strike."""
        sl = self._slices.get(expiry)
        if sl is None:
            return []
        out: list[Quote] = []
        for strike in sl.strikes:
            q = sl.get(self.root, float(strike), right)
            if q is not None:
                out.append(q)
        return out

    def atm_iv(self, spot: float) -> float | None:
        """IV of the strike nearest spot on the nearest expiry (regime gauge).

        Averages the call/put IV where both sides mark, halving the noise a
        one-sided stale mark would inject into the IV-rank series.
        """
        for expiry in sorted(self._slices):
            sl = self._slices[expiry]
            if sl.strikes.size == 0:
                continue
            idx = int(np.abs(sl.strikes - spot).argmin())
            ivs = [
                float(sl.data[idx, r, F_IV])
                for r in range(2)
                if not math.isnan(sl.data[idx, r, F_IV])
            ]
            if ivs:
                return sum(ivs) / len(ivs)
        return None
