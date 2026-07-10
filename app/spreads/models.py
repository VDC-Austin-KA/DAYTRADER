"""Shared datatypes and option-symbol helpers for the spread bot."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class Right(str, Enum):
    CALL = "C"
    PUT = "P"


class SpreadKind(str, Enum):
    CREDIT = "credit"
    DEBIT = "debit"


# OCC option symbol: ROOT (padded) + YYMMDD + C/P + strike*1000 (8 digits).
_OCC_RE = re.compile(r"^(?P<root>[A-Z.]{1,6})(?P<exp>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")
# moomoo US option code: same fields as OCC but the strike (x1000) is NOT
# zero-padded, e.g. "SPY260713C620000" (strike 620) or "SPY260713C1500" (1.5).
_MOOMOO_RE = re.compile(r"^(?P<root>[A-Z.]{1,6})(?P<exp>\d{6})(?P<right>[CP])(?P<strike>\d+)$")


@dataclass(frozen=True)
class OptionContract:
    """One listed contract, identified the OCC way."""

    root: str
    expiry: date
    right: Right
    strike: float

    @property
    def occ_symbol(self) -> str:
        return (
            f"{self.root}{self.expiry:%y%m%d}"
            f"{self.right.value}{int(round(self.strike * 1000)):08d}"
        )

    @property
    def moomoo_code(self) -> str:
        """moomoo US option code, e.g. ``US.SPY260713C620000``.

        Same fields as OCC but the strike (x1000) is not zero-padded.
        """
        return (
            f"US.{self.root}{self.expiry:%y%m%d}"
            f"{self.right.value}{int(round(self.strike * 1000))}"
        )

    @classmethod
    def from_occ(cls, symbol: str) -> "OptionContract":
        sym = symbol.upper().removeprefix("O:").replace(" ", "")
        m = _OCC_RE.match(sym)
        if not m:
            raise ValueError(f"not an OCC option symbol: {symbol!r}")
        exp = m.group("exp")
        return cls(
            root=m.group("root"),
            expiry=date(2000 + int(exp[:2]), int(exp[2:4]), int(exp[4:6])),
            right=Right(m.group("right")),
            strike=int(m.group("strike")) / 1000.0,
        )

    @classmethod
    def from_moomoo_code(cls, code: str) -> "OptionContract":
        sym = code.upper().removeprefix("US.").replace(" ", "")
        m = _MOOMOO_RE.match(sym)
        if not m:
            raise ValueError(f"not a moomoo option code: {code!r}")
        exp = m.group("exp")
        return cls(
            root=m.group("root"),
            expiry=date(2000 + int(exp[:2]), int(exp[2:4]), int(exp[4:6])),
            right=Right(m.group("right")),
            strike=int(m.group("strike")) / 1000.0,
        )

    def dte(self, today: date | None = None) -> int:
        return (self.expiry - (today or date.today())).days


@dataclass
class Quote:
    """Latest NBBO + greeks for one contract, as held in the chain store."""

    contract: OptionContract
    bid: float = 0.0
    ask: float = 0.0
    delta: float = float("nan")
    gamma: float = float("nan")
    iv: float = float("nan")
    # Monotonic-free wall clock of the most recent quote tick, in ns since
    # epoch (compared against time.time_ns() by the staleness guardrail).
    ts_ns: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0 if self.ask > 0 else 0.0

    @property
    def spread_pct(self) -> float:
        """Quoted bid/ask width as a fraction of mid (inf if unquotable)."""
        mid = self.mid
        return (self.ask - self.bid) / mid if mid > 0 else float("inf")

    def age_ms(self, now_ns: int | None = None) -> float:
        return ((now_ns or time.time_ns()) - self.ts_ns) / 1e6


@dataclass
class SpreadCandidate:
    """A fully specified two-leg vertical picked by the scanner."""

    kind: SpreadKind
    short_leg: Quote   # the leg we sell (credit) / the far wing we sell (debit)
    long_leg: Quote    # the leg we buy
    iv_rank: float
    quantity: int = 1

    @property
    def width(self) -> float:
        return abs(self.short_leg.contract.strike - self.long_leg.contract.strike)

    @property
    def net_mid(self) -> float:
        """Positive = net credit received, negative = net debit paid."""
        return self.short_leg.mid - self.long_leg.mid

    @property
    def max_risk_per_spread(self) -> float:
        """Defined risk in dollars for one spread (100 multiplier)."""
        if self.kind is SpreadKind.CREDIT:
            return (self.width - self.net_mid) * 100.0
        return -self.net_mid * 100.0  # debit paid is the whole risk

    def describe(self) -> str:
        s, l = self.short_leg.contract, self.long_leg.contract
        return (
            f"{self.kind.value} {s.right.value}-spread {s.root} {s.expiry:%m/%d} "
            f"short {s.strike:g} / long {l.strike:g} x{self.quantity} "
            f"(net mid {self.net_mid:+.2f}, max risk ${self.max_risk_per_spread:.0f}, "
            f"IVR {self.iv_rank:.0f})"
        )


@dataclass
class LegFill:
    contract: OptionContract
    side: str            # BUY / SELL
    quantity: int
    limit_price: float
    order_id: str = ""
    ok: bool = False
    message: str = ""


@dataclass
class SpreadPosition:
    """An open vertical the watchdog marks to market and can flatten."""

    candidate: SpreadCandidate
    fills: list[LegFill] = field(default_factory=list)
    entry_price: float = 0.0        # net credit(+)/debit(-) actually posted
    opened_ts: float = field(default_factory=time.time)
    closed: bool = False

    def unrealized_loss(self, current_net_mid: float) -> float:
        """Adverse P&L in dollars for the whole position (>=0 means losing).

        ``net_mid`` is always short-leg mid minus long-leg mid and
        ``entry_price`` carries the same sign convention (credit positive,
        debit negative), so a rising net mid hurts a credit spread (costlier
        to buy back) and a debit spread alike (the owned spread, worth
        ``-net_mid``, shrinks). One formula covers both kinds.
        """
        per_spread = current_net_mid - self.entry_price
        return max(0.0, per_spread) * 100.0 * self.candidate.quantity
