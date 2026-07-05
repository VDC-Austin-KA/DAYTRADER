"""The autonomous bot loop: settle, gate, estimate, trade.

Called by the scheduler every ``PREDICTION_CYCLE_SECONDS``. Every cycle is
crash-isolated — any exception is logged and the next cycle starts clean —
and all state lives in the database, so restarts and redeploys are safe.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..models import PredictionBotState, PredictionTrade
from . import data, markets, model, risk
from .execution import get_executor
from .markets import MarketQuote

log = logging.getLogger("daytrader.prediction.bot")

# Grace period after the hour boundary before settling (lets the final
# minute bar land in the feed).
_SETTLE_GRACE = timedelta(minutes=2)


def get_state(db: Session) -> PredictionBotState:
    state = db.query(PredictionBotState).first()
    if state is None:
        state = PredictionBotState(paused=0, reason="")
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def set_paused(db: Session, paused: bool, reason: str = "") -> PredictionBotState:
    state = get_state(db)
    state.paused = 1 if paused else 0
    state.reason = reason
    state.updated_at = datetime.utcnow()
    db.commit()
    return state


def settle_due_trades(db: Session) -> int:
    """Settle open trades whose contract hour has closed.

    Both paper and live trades are marked against the BTC index price at
    the close time; for live trades this is an estimate of the broker's
    official settlement (noted on the row) but keeps risk limits and the
    dashboard current without extra broker calls.
    """
    now = datetime.utcnow()
    due = (
        db.query(PredictionTrade)
        .filter(PredictionTrade.status == "open")
        .filter(PredictionTrade.close_time <= now - _SETTLE_GRACE)
        .all()
    )
    settled = 0
    for trade in due:
        price = data.price_at(trade.close_time.replace(tzinfo=timezone.utc))
        if price is None:
            log.warning("no settlement price yet for %s; retrying next cycle", trade.ticker)
            continue
        above = price > trade.strike
        won = above if trade.side == "yes" else not above
        trade.settlement_price = price
        trade.result = "win" if won else "loss"
        trade.pnl = round(
            trade.quantity * (1.0 - trade.entry_price) if won
            else -trade.quantity * trade.entry_price,
            2,
        )
        trade.status = "settled"
        trade.settled_at = now
        if trade.mode == "live":
            trade.rationale += " | pnl estimated from index; broker statement is authoritative"
        settled += 1
        log.info(
            "settled %s %s x%d: %s pnl=%.2f (btc=%.2f strike=%.2f)",
            trade.ticker, trade.side, trade.quantity, trade.result,
            trade.pnl, price, trade.strike,
        )
    if settled:
        db.commit()
    return settled


def _pick_side(est: model.ProbabilityEstimate, market: MarketQuote):
    """Return (side, p_win, ask_dollars, edge) for the better edge, or None."""
    lo = settings.prediction_min_price_cents / 100.0
    hi = settings.prediction_max_price_cents / 100.0
    options = []
    if lo <= market.yes_ask <= hi:
        options.append(("yes", est.p_above, market.yes_ask))
    if lo <= market.no_ask <= hi:
        options.append(("no", 1.0 - est.p_above, market.no_ask))
    best = None
    for side, p, ask in options:
        edge = p - ask
        if edge >= settings.prediction_min_edge and (best is None or edge > best[3]):
            best = (side, p, ask, edge)
    return best


def run_cycle() -> dict:
    """One decision cycle; returns a summary dict (also used by the API)."""
    from ..database import SessionLocal

    summary: dict = {"action": "none"}
    db = SessionLocal()
    try:
        settle_due_trades(db)

        state = get_state(db)
        if state.paused:
            return summary | {"skipped": f"paused: {state.reason}"}

        now = datetime.utcnow()
        decision = risk.check(db, now)
        if not decision.allowed:
            log.info("risk gate: %s", decision.reason)
            return summary | {"skipped": decision.reason}

        spot = data.get_spot()
        if spot is None:
            log.warning("no BTC price available; skipping cycle")
            return summary | {"skipped": "no BTC price"}

        market = markets.select_hourly_market(spot)
        if market is None:
            return summary | {"skipped": "no open hourly market found"}

        minutes = market.minutes_remaining()
        if not (
            settings.prediction_min_minutes_left
            <= minutes
            <= settings.prediction_max_minutes_left
        ):
            return summary | {"skipped": f"{minutes:.1f}m to settle is outside window"}

        already = (
            db.query(PredictionTrade)
            .filter(PredictionTrade.ticker == market.ticker)
            .count()
        )
        if already:
            return summary | {"skipped": f"already traded {market.ticker}"}

        est = model.estimate_p_above(
            spot=spot,
            strike=market.strike,
            minutes_remaining=minutes,
            minute_returns=data.get_minute_returns(),
            momentum_coeff=settings.prediction_momentum_coeff,
        )
        pick = _pick_side(est, market)
        if pick is None:
            log.info("no edge on %s (%s)", market.ticker, est.rationale())
            return summary | {"skipped": "no edge", "estimate": est.rationale()}

        side, p_win, ask, edge = pick
        bankroll = risk.current_bankroll(db)
        contracts, stake = risk.size_position(bankroll, p_win, ask)
        if contracts < 1:
            return summary | {"skipped": "stake below one contract"}

        executor = get_executor()
        result = executor.place_order(market.ticker, side, contracts, ask)
        if not result.ok:
            log.error("order failed for %s: %s", market.ticker, result.message)
            return summary | {"action": "error", "error": result.message}

        trade = PredictionTrade(
            ticker=market.ticker,
            series=market.series,
            side=side,
            strike=market.strike,
            close_time=market.close_time.astimezone(timezone.utc).replace(tzinfo=None),
            probability=round(p_win, 4),
            entry_price=result.fill_price,
            quantity=contracts,
            stake=stake,
            mode=result.mode,
            order_id=result.order_id,
            rationale=f"edge={edge:.3f} | {est.rationale()}",
        )
        db.add(trade)
        db.commit()
        log.info(
            "TRADE %s: %s x%d %s @ %.2f (p=%.3f edge=%.3f stake=%.2f mode=%s)",
            market.ticker, side, contracts, market.subtitle or market.strike,
            result.fill_price, p_win, edge, stake, result.mode,
        )
        return {
            "action": "trade",
            "ticker": market.ticker,
            "side": side,
            "quantity": contracts,
            "price": result.fill_price,
            "probability": round(p_win, 4),
            "edge": round(edge, 4),
            "mode": result.mode,
        }
    except Exception:
        log.exception("prediction bot cycle failed")
        return {"action": "error", "error": "cycle exception (see logs)"}
    finally:
        db.close()
