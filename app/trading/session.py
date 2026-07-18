"""Session rules: nothing is ever held overnight.

This is a HARD constraint, not a tunable preference. Every position opened
must be closable the same session, and anything still open near the bell is
flattened. The rule exists because overnight gap risk is unhedgeable while
you sleep -- it is the tail that ends short-premium accounts (Feb 2018,
March 2020 were both gap events), and no stop-loss protects against a price
that never trades between the close and the open.

Three gates, all in America/Chicago:

  08:30  market open
  14:00  LAST ENTRY -- nothing new after this
  14:45  FLATTEN -- force-close whatever is still open
  15:00  close

The flatten window sits BEFORE the close, not at it. Exiting into the
closing auction means paying whatever spread is left when everyone else is
doing the same thing; fifteen minutes of margin is the difference between
choosing an exit and accepting one.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("daytrader.session")

MARKET_TZ = ZoneInfo("America/Chicago")

MARKET_OPEN = dtime(8, 30)
LAST_ENTRY = dtime(14, 0)
FLATTEN_AT = dtime(14, 45)
MARKET_CLOSE = dtime(15, 0)


def now_ct(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(MARKET_TZ)


def is_trading_day(now: datetime | None = None) -> bool:
    return now_ct(now).weekday() < 5


def can_open(now: datetime | None = None) -> tuple[bool, str]:
    """May we open a NEW position right now?"""
    t = now_ct(now)
    if t.weekday() >= 5:
        return False, "weekend"
    if t.time() < MARKET_OPEN:
        return False, "pre-market"
    if t.time() >= LAST_ENTRY:
        return False, (
            f"past {LAST_ENTRY:%H:%M} CT last-entry cutoff "
            "(no position may be opened that cannot be closed today)"
        )
    return True, "ok"


def must_flatten(now: datetime | None = None) -> tuple[bool, str]:
    """Should every open position be closed NOW?"""
    t = now_ct(now)
    if t.weekday() >= 5:
        # A position that survived into the weekend is already a rule
        # violation; say so loudly rather than waiting for Monday.
        return True, "position open outside a trading day - flatten immediately"
    if t.time() >= FLATTEN_AT:
        return True, f"past {FLATTEN_AT:%H:%M} CT flatten window - no overnight holds"
    return False, "within session"


def minutes_until_flatten(now: datetime | None = None) -> float:
    """Minutes of runway left before the forced exit. Negative once past."""
    t = now_ct(now)
    target = t.replace(
        hour=FLATTEN_AT.hour, minute=FLATTEN_AT.minute, second=0, microsecond=0
    )
    return (target - t).total_seconds() / 60.0


def validate_expiry(expiry: str, now: datetime | None = None) -> tuple[bool, str]:
    """Reject any contract that cannot be exited before the flatten window.

    Same-day expiry is ideal. Later expiries are permitted -- they are still
    closed today -- but an ALREADY EXPIRED contract is a hard error.
    """
    t = now_ct(now)
    today = t.strftime("%Y-%m-%d")
    if expiry < today:
        return False, f"expiry {expiry} is in the past"
    return True, "same-day expiry" if expiry == today else "closed intraday regardless"
