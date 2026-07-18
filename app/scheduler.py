"""Background jobs: periodic signal refresh and weekly model retraining."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from .config import settings
from .database import SessionLocal
from .models import ModelMeta
from .prediction import bot as prediction_bot
from .trading import paper, signals
from .trading import session as _session_mod
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


def flatten_all_positions() -> None:
    """Force-close every open position. Runs every minute; acts near the bell.

    This is the enforcement half of the no-overnight rule -- blocking late
    entries is not enough, because a position opened at 13:59 still has to
    be exited. Runs on a 1-minute interval rather than a single cron fire so
    a restart, a paused scheduler, or one failed attempt cannot let a
    position slip through into the night.
    """
    from .trading import session as sess

    if not settings.enforce_no_overnight:
        return
    should, why = sess.must_flatten()
    if not should:
        return

    db = SessionLocal()
    try:
        pf = paper.get_or_create_portfolio(db)
        open_positions = [p for p in pf.positions if p.is_open]
        if not open_positions:
            return
        log.warning("FLATTEN: %s - closing %d position(s)", why, len(open_positions))
        for pos in open_positions:
            try:
                _, msg = paper.close_position(db, pf, pos.id, note="auto-flatten EOD")
                log.warning("FLATTEN %s: %s", pos.symbol, msg)
            except Exception:
                # Keep going: one stuck position must not strand the others.
                log.exception("FLATTEN failed for position %s", pos.id)
    except Exception:
        log.exception("flatten sweep failed")
    finally:
        db.close()


def open_window_refresh() -> None:
    """Fast refresh that only fires inside the opening focus window.

    Re-scans the movers universe (bypassing its cache) and re-marks open
    positions so the dashboard is seconds-fresh during 08:29-09:00 CT
    without polling the APIs hard the rest of the day.
    """
    from .data import market_data as md

    if not md.in_open_window():
        return
    try:
        from .trading import movers

        movers.scan_universe(refresh=True)
    except Exception:
        log.exception("open-window movers rescan failed")
    db = SessionLocal()
    try:
        paper.mark_to_market(db, paper.get_or_create_portfolio(db))
    except Exception:
        log.exception("open-window mark-to-market failed")
    finally:
        db.close()


def bootstrap() -> None:
    """One-time startup task: train models if none exist, then refresh signals.

    Skips quietly when no market-data token is configured so the app still
    boots and the dashboard can explain what's missing.
    """
    if not settings.has_data_source:
        log.warning("no market-data token configured; skipping startup training")
        return
    db = SessionLocal()
    try:
        if settings.auto_train_on_start and db.query(ModelMeta).count() == 0:
            log.info("no models found; training universe on startup")
            train_universe(db)
        refresh_all_signals()
    except Exception:
        log.exception("startup bootstrap failed")
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
    if settings.prediction_enabled:
        _scheduler.add_job(
            prediction_bot.run_cycle,
            "interval",
            seconds=settings.prediction_cycle_seconds,
            id="prediction_bot",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        log.info(
            "prediction bot scheduled every %ss (mode=%s)",
            settings.prediction_cycle_seconds,
            settings.prediction_trade_mode,
        )
    # Opening focus window: frequent rescan, self-gated to weekdays
    # 08:29-09:00 in the configured tz (job itself no-ops outside it).
    _scheduler.add_job(
        open_window_refresh,
        "interval",
        seconds=settings.movers_window_refresh_seconds,
        id="open_window_refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    log.info(
        "opening-window rescan every %ss, active %s-%s %s",
        settings.movers_window_refresh_seconds,
        settings.movers_window_start, settings.movers_window_end,
        settings.movers_window_tz,
    )
    # No-overnight enforcement: sweep every minute, act inside the flatten
    # window. Interval (not cron) so a restart cannot skip the one firing
    # that matters.
    _scheduler.add_job(
        flatten_all_positions,
        "interval",
        minutes=1,
        id="flatten_all_positions",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    log.info(
        "no-overnight enforcement %s (last entry %s CT, flatten %s CT)",
        "ON" if settings.enforce_no_overnight else "OFF",
        _session_mod.LAST_ENTRY.strftime("%H:%M"),
        _session_mod.FLATTEN_AT.strftime("%H:%M"),
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
    # One-shot startup bootstrap a few seconds after boot (keeps healthcheck fast).
    _scheduler.add_job(
        bootstrap,
        "date",
        run_date=datetime.now() + timedelta(seconds=8),
        id="startup_bootstrap",
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
