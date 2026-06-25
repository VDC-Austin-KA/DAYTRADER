"""Background jobs: periodic signal refresh and weekly model retraining."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .config import settings
from .database import SessionLocal
from .models import ModelMeta
from .trading import paper, signals
from .training import train_universe

log = logging.getLogger("daytrader.scheduler")
_scheduler: BackgroundScheduler | None = None


def refresh_all_signals() -> None:
    db = SessionLocal()
    try:
        for symbol in settings.default_watchlist:
            try:
                signals.refresh_signal(db, symbol)
            except Exception:
                log.exception("signal refresh failed for %s", symbol)
        # Mark open positions to market.
        pf = paper.get_or_create_portfolio(db)
        paper.mark_to_market(db, pf)
    finally:
        db.close()


def retrain_if_needed() -> None:
    db = SessionLocal()
    try:
        trained = db.query(ModelMeta).count()
        if trained == 0:
            log.info("no models found; training universe on startup")
            train_universe(db)
    finally:
        db.close()


def start_scheduler() -> None:
    global _scheduler
    if not settings.enable_scheduler or _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        refresh_all_signals,
        "interval",
        minutes=settings.refresh_minutes,
        id="refresh_signals",
        replace_existing=True,
        max_instances=1,
    )
    # Weekly retrain (Sunday 06:00 UTC).
    _scheduler.add_job(
        lambda: train_universe(SessionLocal()),
        "cron",
        day_of_week="sun",
        hour=6,
        id="weekly_retrain",
        replace_existing=True,
        max_instances=1,
    )
    _scheduler.start()
    log.info("scheduler started (refresh every %s min)", settings.refresh_minutes)


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
