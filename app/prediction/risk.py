"""Risk management: stop conditions and position sizing.

All limits are computed from the trade ledger each cycle (stateless), so a
process restart can never wipe out a triggered stop.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..config import settings
from ..models import PredictionTrade

log = logging.getLogger("daytrader.prediction.risk")


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""


def today_realized_pnl(db: Session, now: datetime) -> float:
    start = datetime(now.year, now.month, now.day)
    rows = (
        db.query(PredictionTrade)
        .filter(PredictionTrade.status == "settled")
        .filter(PredictionTrade.settled_at >= start)
        .all()
    )
    return float(sum(r.pnl for r in rows))


def consecutive_losses(db: Session) -> tuple[int, datetime | None]:
    """(streak length, timestamp of most recent settle)."""
    rows = (
        db.query(PredictionTrade)
        .filter(PredictionTrade.status == "settled")
        .order_by(PredictionTrade.settled_at.desc())
        .limit(settings.prediction_max_consecutive_losses)
        .all()
    )
    streak = 0
    last = rows[0].settled_at if rows else None
    for r in rows:
        if r.result == "loss":
            streak += 1
        else:
            break
    return streak, last


def open_positions(db: Session) -> list[PredictionTrade]:
    return db.query(PredictionTrade).filter(PredictionTrade.status == "open").all()


def check(db: Session, now: datetime) -> RiskDecision:
    """Gate a new entry against every stop condition."""
    if len(open_positions(db)) >= settings.prediction_max_open:
        return RiskDecision(False, "max open positions reached")

    pnl = today_realized_pnl(db, now)
    if pnl <= -settings.prediction_max_daily_loss:
        return RiskDecision(
            False,
            f"daily loss limit hit ({pnl:.2f} <= -{settings.prediction_max_daily_loss:.2f})",
        )

    streak, last = consecutive_losses(db)
    if streak >= settings.prediction_max_consecutive_losses and last is not None:
        until = last + timedelta(minutes=settings.prediction_cooldown_minutes)
        if now < until:
            return RiskDecision(
                False, f"{streak} consecutive losses; cooling down until {until:%H:%M}"
            )
    return RiskDecision(True)


def current_bankroll(db: Session) -> float:
    """Configured bankroll adjusted by all settled bot P&L."""
    total = (
        db.query(PredictionTrade)
        .filter(PredictionTrade.status == "settled")
        .with_entities(PredictionTrade.pnl)
        .all()
    )
    return settings.prediction_bankroll + float(sum(p[0] for p in total))


def size_position(bankroll: float, p_win: float, cost: float) -> tuple[int, float]:
    """(contracts, stake) via fractional Kelly, capped by hard limits.

    ``cost`` is dollars per contract (contract pays $1 on a win), so the
    full-Kelly fraction for a binary payout is (p - cost) / (1 - cost).
    """
    if bankroll <= 0 or not (0 < cost < 1):
        return 0, 0.0
    kelly = (p_win - cost) / (1.0 - cost)
    if kelly <= 0:
        return 0, 0.0
    stake = kelly * settings.prediction_kelly_fraction * bankroll
    stake = min(
        stake,
        settings.prediction_max_stake_usd,
        settings.prediction_max_stake_pct * bankroll,
    )
    contracts = int(math.floor(stake / cost))
    return max(contracts, 0), round(contracts * cost, 2)
