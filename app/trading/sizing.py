"""Position sizing: fixed-fractional and deliberately anti-martingale.

WHY THIS EXISTS
---------------
The user's own 702-trip history shows a genuinely positive per-trip edge
(57% win rate, +$371 avg win vs -$344 avg loss, +$63 expectancy). The
account still gave back 44% from its peak. The reconstruction shows why:

    contracts traded after a WIN  day : 684 avg
    contracts traded after a LOSS day : 230 avg

Size escalated ~3x following wins. A positive-expectancy edge run at
increasing size after winning streaks puts the largest bets immediately
after the run that created the confidence -- which is exactly when a
reversion is most likely. The edge survived; the bankroll did not.

So the rules here are the inverse of what confidence suggests:

1. FIXED FRACTIONAL. Contracts derive from current equity and the
   per-trade risk budget, never from recent results, never from a
   conviction score. Equity grows -> size grows, slowly and only because
   the base grew.

2. NEVER SIZE UP AFTER A WIN. Today's size is capped at yesterday's
   baseline. Winning cannot raise it. This is the single rule that
   addresses the documented failure.

3. SIZE DOWN AFTER LOSSES. Consecutive losing days shrink the multiplier
   geometrically, so a bad stretch costs progressively less while the
   question "is the edge still there?" is still open.

4. HARD FLOOR AND CEILING. Never more than ``max_contracts``, never more
   than the buying-power cap allows, and below one contract the answer is
   zero rather than a rounded-up gamble.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("daytrader.sizing")

# Risk budget per trade as a fraction of equity. 2% is the conventional
# ceiling for a discretionary edge; with a ~35% bracket stop that means a
# full stop-out costs about 2% of the account.
RISK_PER_TRADE = 0.02
# Each consecutive losing day multiplies size by this.
LOSS_DAY_DECAY = 0.6
# Floor so a drawdown cannot shrink size to nothing and prevent recovery.
MIN_MULTIPLIER = 0.25
MAX_CONTRACTS = 20


@dataclass
class SizingDecision:
    contracts: int
    multiplier: float
    equity: float
    reason: str

    def describe(self) -> str:
        return (f"{self.contracts} contract(s) "
                f"[x{self.multiplier:.2f} on ${self.equity:,.0f}] {self.reason}")


def streak_multiplier(recent_daily_pnl: list[float]) -> tuple[float, str]:
    """Multiplier from recent daily results. Never exceeds 1.0.

    ``recent_daily_pnl`` is most-recent-first. Winning days do not raise
    the multiplier -- that asymmetry is the whole point.
    """
    if not recent_daily_pnl:
        return 1.0, "no history"
    losses = 0
    for pnl in recent_daily_pnl:
        if pnl < 0:
            losses += 1
        else:
            break
    if losses == 0:
        # Last day was a win (or flat). Baseline -- explicitly NOT more.
        return 1.0, "baseline (wins never increase size)"
    mult = max(MIN_MULTIPLIER, LOSS_DAY_DECAY ** losses)
    return mult, f"{losses} consecutive losing day(s) -> x{mult:.2f}"


def contracts_for(
    equity: float,
    entry_price: float,
    stop_pct: float,
    recent_daily_pnl: list[float] | None = None,
    buying_power: float | None = None,
    bp_fraction: float = 0.6667,
    risk_per_trade: float = RISK_PER_TRADE,
    max_contracts: int = MAX_CONTRACTS,
) -> SizingDecision:
    """How many contracts to buy. Zero is a valid, common answer."""
    if equity <= 0 or entry_price <= 0 or stop_pct <= 0:
        return SizingDecision(0, 0.0, equity, "invalid inputs")

    mult, why = streak_multiplier(recent_daily_pnl or [])

    # Risk per contract = premium lost if the bracket's hard stop fires.
    risk_per_contract = entry_price * stop_pct * 100
    budget = equity * risk_per_trade * mult
    n = int(budget / risk_per_contract)

    # Buying power is a separate, harder constraint: you cannot spend what
    # you do not have, regardless of what the risk budget permits.
    if buying_power is not None and buying_power > 0:
        affordable = int((buying_power * bp_fraction) / (entry_price * 100))
        if affordable < n:
            n = affordable
            why += f"; capped by buying power (${buying_power:,.0f})"

    n = max(0, min(n, max_contracts))
    if n == 0:
        why += "; below one contract -> no trade"
    return SizingDecision(n, mult, equity, why)
