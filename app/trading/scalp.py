"""Bracketed scalp engine: entry + take-profit + stop, liquidity-gated.

THE SPREAD IS THE WHOLE PROBLEM
-------------------------------
A naive bracket -- buy 0.50, sell at 0.53, stop at 0.48 -- is guaranteed to
lose. You buy at the ASK and can only exit at the BID, so on a 0.48/0.50
quote the 0.48 stop is already touched the moment you are filled. Any
bracket narrower than the spread is triggered by quote noise, not by the
underlying moving.

So every level here is derived FROM the measured spread, never from a fixed
cent amount:

    target = entry_mid + max(min_ticks, TARGET_MULT * spread)
    stop   = entry_mid - max(min_ticks, STOP_MULT  * spread)

with both required to clear the spread by a margin. Measured on live SPY /
QQQ / NVDA chains, the median spread is ~2c, or 4.8% of mid -- that 4.8% is
the round-trip toll on every scalp, paid before any edge appears.

EXPECTANCY, PLAINLY
-------------------
With no directional edge (see app/backtest -- Surge hit 48.5% out of
sample), a bracket's expectancy is negative by roughly the spread. This
module therefore defaults to PAPER and reports realised expectancy so the
question is settled by data rather than by hope. ``required_win_rate()``
gives the break-even hit rate for the configured bracket; compare it to
what the engine actually achieves before ever letting this touch real money.

TIME RULES (user-specified, and they matter)
--------------------------------------------
* No entries after ``no_entry_after`` (default 14:00 America/Chicago).
  Late 0DTE entries face accelerating gamma and vanishing exit liquidity.
* Same-day-expiry positions are exited inside the final hour
  (``force_exit_after``, default 15:00 CT) rather than held to the bell.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("daytrader.scalp")

MARKET_TZ = ZoneInfo("America/Chicago")

# --- Bracket geometry, in multiples of the measured spread ---------------
TARGET_MULT = 2.0      # take profit at 2x the spread above mid
STOP_MULT = 1.5        # stop at 1.5x the spread below mid
MIN_TICKS = 0.02       # never tighter than 2c regardless of spread

# --- Liquidity gates: "could I actually sell this?" ----------------------
MIN_OPEN_INTEREST = 500
MIN_VOLUME = 250
MAX_SPREAD_PCT = 0.06        # 6% of mid; above this the toll eats the trade
MIN_BID = 0.10               # sub-dime contracts cannot be exited cleanly

# --- Time rules ----------------------------------------------------------
NO_ENTRY_AFTER = dtime(14, 0)      # 2pm CT
FORCE_EXIT_AFTER = dtime(15, 0)    # final hour for same-day expiries
MARKET_CLOSE = dtime(15, 0)


@dataclass
class Bracket:
    """A planned scalp: entry, target, stop, size, and why it passed."""
    symbol: str
    contract_symbol: str
    quantity: int
    entry: float
    target: float
    stop: float
    spread: float
    spread_pct: float
    reward_risk: float
    required_win_rate: float
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return (
            f"{self.symbol} {self.contract_symbol} x{self.quantity} "
            f"entry {self.entry:.2f} -> tp {self.target:.2f} / sl {self.stop:.2f} "
            f"(spread {self.spread:.2f} = {self.spread_pct*100:.1f}%, "
            f"R:R {self.reward_risk:.2f}, needs {self.required_win_rate*100:.0f}% wins)"
        )


def now_ct(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(MARKET_TZ)


def entries_allowed(now: datetime | None = None) -> tuple[bool, str]:
    """Entry gate: weekday, market hours, and before the 2pm CT cutoff."""
    t = now_ct(now)
    if t.weekday() >= 5:
        return False, "weekend"
    if t.time() >= NO_ENTRY_AFTER:
        return False, f"past {NO_ENTRY_AFTER:%H:%M} CT entry cutoff"
    if t.time() < dtime(8, 30):
        return False, "pre-market"
    return True, "ok"


def must_exit(expiry: str, now: datetime | None = None) -> tuple[bool, str]:
    """Same-day expiries are flattened inside the final hour, not held."""
    t = now_ct(now)
    if expiry != t.strftime("%Y-%m-%d"):
        return False, "not same-day expiry"
    if t.time() >= FORCE_EXIT_AFTER:
        return True, f"0DTE past {FORCE_EXIT_AFTER:%H:%M} CT - exit window"
    return False, "0DTE, exit window not reached"


def required_win_rate(target: float, stop: float, entry: float) -> float:
    """Break-even hit rate for this bracket. Sanity check before trading."""
    win = target - entry
    loss = entry - stop
    if win <= 0 or loss <= 0:
        return 1.0
    return loss / (win + loss)


def check_liquidity(contract: dict) -> tuple[bool, list[str]]:
    """Can this realistically be SOLD again? Reasons listed on failure."""
    fails: list[str] = []
    bid = float(contract.get("bid") or 0)
    ask = float(contract.get("ask") or 0)
    oi = int(contract.get("openInterest") or 0)
    vol = int(contract.get("volume") or 0)

    if bid < MIN_BID:
        fails.append(f"bid {bid:.2f} < {MIN_BID:.2f} (no clean exit)")
    if ask <= bid:
        fails.append("crossed/locked quote")
        return False, fails

    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid if mid else 1.0
    if spread_pct > MAX_SPREAD_PCT:
        fails.append(f"spread {spread_pct*100:.1f}% > {MAX_SPREAD_PCT*100:.0f}%")
    if oi < MIN_OPEN_INTEREST:
        fails.append(f"OI {oi} < {MIN_OPEN_INTEREST}")
    if vol < MIN_VOLUME:
        fails.append(f"volume {vol} < {MIN_VOLUME}")
    return not fails, fails


def plan_bracket(
    contract: dict,
    symbol: str,
    quantity: int = 1,
    now: datetime | None = None,
) -> tuple[Bracket | None, str]:
    """Build a spread-aware bracket, or explain why we refuse to trade it."""
    ok, why = entries_allowed(now)
    if not ok:
        return None, f"no entry: {why}"

    liquid, fails = check_liquidity(contract)
    if not liquid:
        return None, "illiquid: " + "; ".join(fails)

    bid = float(contract["bid"])
    ask = float(contract["ask"])
    mid = (bid + ask) / 2
    spread = ask - bid

    # Levels derived from the spread, floored at MIN_TICKS. Both sit outside
    # the quote, so neither can be triggered by the spread alone.
    target = round(mid + max(MIN_TICKS, TARGET_MULT * spread), 2)
    stop = round(mid - max(MIN_TICKS, STOP_MULT * spread), 2)
    entry = round(mid, 2)

    if stop <= 0:
        return None, "stop would be at or below zero"

    win_rate = required_win_rate(target, stop, entry)
    rr = (target - entry) / max(entry - stop, 1e-9)

    notes = [
        f"round-trip spread cost ~{spread / mid * 100:.1f}% of mid",
        f"break-even win rate {win_rate*100:.0f}%",
    ]
    if win_rate > 0.5:
        notes.append(
            "WARNING: needs a better-than-coinflip hit rate; no measured "
            "directional edge supports that (see app/backtest)"
        )

    return Bracket(
        symbol=symbol, contract_symbol=contract.get("contractSymbol", ""),
        quantity=quantity, entry=entry, target=target, stop=stop,
        spread=round(spread, 3), spread_pct=round(spread / mid, 4),
        reward_risk=round(rr, 2), required_win_rate=round(win_rate, 4),
        notes=notes,
    ), "ok"


def check_triggers(
    bracket: Bracket, bid: float, ask: float
) -> tuple[str | None, float]:
    """Evaluate a live quote against the bracket. Returns (action, price).

    Trigger semantics, made explicit because they decide the fill:

    * We are LONG, so every exit happens at the **bid**. Both the target and
      the stop are therefore tested against the bid, never the mid or the
      last trade. Testing a stop against the mid trips it roughly half a
      spread early, every time.
    * The stop is a TRIGGER, not a resting limit: once bid <= stop we push a
      sell. That sell lands at whatever the bid is *then*, which on a fast
      move is below the trigger. So a stop-trigger is strictly worse than a
      resting limit at the same level -- it adds slippage on exactly the
      trades that are already going against you. The estimated fill returned
      here is the current bid, not the trigger price, so backtests and paper
      runs do not quietly assume a perfect exit.
    """
    if bid >= bracket.target:
        return "take_profit", bid
    if bid <= bracket.stop:
        # Slippage: we get the live bid, which may be through the trigger.
        return "stop", bid
    return None, bid


def size_position(
    bracket_risk: float, account_equity: float, max_risk_pct: float = 0.01
) -> int:
    """Contracts to buy so one loss costs at most ``max_risk_pct`` of equity.

    This is what "buy in bulk on high-probability setups" has to mean in
    practice: size scales with the risk budget, never with confidence. A
    high-conviction read that is wrong still has to be survivable, and
    conviction is exactly the input that has been shown to be unreliable.
    """
    if bracket_risk <= 0:
        return 0
    budget = account_equity * max_risk_pct
    return max(0, int(budget / (bracket_risk * 100)))
