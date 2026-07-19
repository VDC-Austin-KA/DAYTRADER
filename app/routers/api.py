"""JSON API endpoints."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..data import market_data as md
from ..database import get_db
from ..models import ModelMeta, PredictionTrade, Signal
from ..prediction import bot as prediction_bot
from ..prediction import risk as prediction_risk
from ..schemas import (
    AmendRequest, BrokerCloseRequest, CancelRequest, CloseRequest,
    TradeRequest, TrainRequest,
)
from ..trading import options, paper, signals
from ..training import train_universe

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/datasource")
def datasource():
    """Whether the market-data provider is configured and reachable."""
    return md.data_source_status()


@router.get("/train/status")
def train_status():
    from ..training import TRAINING_STATUS

    return TRAINING_STATUS


@router.get("/config")
def config():
    from ..data import market_data as md

    return {
        "max_dte": settings.max_dte,
        "min_dte": settings.min_dte,
        "signal_threshold": settings.signal_threshold,
        "horizon_days": settings.horizon_days,
        "target_move": settings.target_move,
        "watchlist": settings.default_watchlist,
        "data_provider": settings.data_provider,
        "dashboard_trade_mode": settings.dashboard_trade_mode,
        "in_open_window": md.in_open_window(),
        "open_window": (
            f"{settings.movers_window_start}-{settings.movers_window_end} "
            f"{settings.movers_window_tz}"
        ),
    }


@router.get("/quote/{symbol}")
def quote(symbol: str):
    price = md.get_quote(symbol.upper())
    if price is None:
        raise HTTPException(404, f"No quote for {symbol}")
    return {"symbol": symbol.upper(), "price": price}


@router.get("/history/{symbol}")
def history(symbol: str, days: int = 180):
    df = md.get_history(symbol.upper(), years=2)
    if df.empty:
        raise HTTPException(404, f"No history for {symbol}")
    df = df.tail(days)
    return {
        "symbol": symbol.upper(),
        "dates": [d.strftime("%Y-%m-%d") for d in df.index],
        "close": [round(float(c), 2) for c in df["Close"]],
    }


@router.get("/signals")
def list_signals(db: Session = Depends(get_db)):
    rows = db.query(Signal).order_by(Signal.probability.desc()).all()
    return [
        {
            "symbol": r.symbol,
            "direction": r.direction,
            "probability": r.probability,
            "underlying_price": r.underlying_price,
            "option_type": r.option_type,
            "contract_symbol": r.contract_symbol,
            "strike": r.strike,
            "expiry": r.expiry,
            "dte": r.dte,
            "option_price": r.option_price,
            "breakeven": r.breakeven,
            "rationale": r.rationale,
            "updated_at": r.updated_at.isoformat(),
        }
        for r in rows
    ]


@router.post("/signals/refresh")
def refresh_signals(db: Session = Depends(get_db)):
    out = []
    for sym in settings.default_watchlist:
        row = signals.refresh_signal(db, sym)
        if row:
            out.append({"symbol": row.symbol, "direction": row.direction,
                        "probability": row.probability})
    return {"refreshed": out}


@router.get("/signal/{symbol}")
def one_signal(symbol: str, db: Session = Depends(get_db)):
    row = signals.refresh_signal(db, symbol.upper())
    if not row:
        raise HTTPException(404, f"Could not generate signal for {symbol}. "
                                 "Is the model trained?")
    return {
        "symbol": row.symbol,
        "direction": row.direction,
        "probability": row.probability,
        "underlying_price": row.underlying_price,
        "option_type": row.option_type,
        "contract_symbol": row.contract_symbol,
        "strike": row.strike,
        "expiry": row.expiry,
        "dte": row.dte,
        "option_price": row.option_price,
        "breakeven": row.breakeven,
        "rationale": row.rationale,
    }


@router.get("/opportunities/{symbol}")
def opportunities(
    symbol: str,
    max_dte: int = 3,
    min_dte: int = 0,
    max_premium: float = 1.00,
    max_cost: float = 100.0,
    side: str = "both",
    limit: int = 25,
):
    """Rank cheap, short-dated options for a ticker by success x potential return."""
    if not settings.has_data_source:
        raise HTTPException(400, "No market-data source configured (start moomoo "
                                 "OpenD + set MOOMOO_OPEND_HOST, or set TRADIER_TOKEN).")
    if side not in ("both", "call", "put"):
        raise HTTPException(422, "side must be both/call/put")
    from ..trading import opportunities as opp

    result = opp.scan(
        symbol.upper(), max_dte=max_dte, min_dte=min_dte,
        max_premium=max_premium, max_cost=max_cost, side=side, limit=limit,
    )
    if result.get("error"):
        raise HTTPException(404, result["error"])
    return result


@router.get("/movers")
def movers(refresh: bool = False):
    """Universe-wide movers scan: Surge Scores, globally ranked options,
    suggested plays, and headline items for the ticker bar."""
    if not settings.has_data_source:
        raise HTTPException(400, "No market-data source configured (start moomoo "
                                 "OpenD + set MOOMOO_OPEND_HOST, or set TRADIER_TOKEN).")
    from ..trading import movers as mv

    return mv.scan_universe(refresh=refresh)


@router.get("/options/{symbol}")
def scan_options(symbol: str, direction: str = "call"):
    pick = options.select_contract(symbol.upper(), direction)
    if not pick:
        raise HTTPException(404, "No suitable short-dated contract found.")
    return pick.__dict__ | {"mid_price": pick.mid_price, "breakeven": pick.breakeven}


@router.post("/train")
def train(req: TrainRequest, background: BackgroundTasks,
          db: Session = Depends(get_db)):
    if not settings.has_data_source:
        raise HTTPException(
            400,
            "No market-data source configured. Set TRADIER_TOKEN (free) so models "
            "have data to train on. See the README.",
        )
    from ..training import TRAINING_STATUS

    if TRAINING_STATUS.get("running"):
        raise HTTPException(409, "Training is already in progress.")
    symbols = req.symbols or settings.default_watchlist
    background.add_task(_train_bg, symbols)
    return {"status": "training_started", "symbols": symbols, "count": len(symbols)}


def _train_bg(symbols: list[str]) -> None:
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        train_universe(db, symbols)
    finally:
        db.close()


@router.get("/models")
def models(db: Session = Depends(get_db)):
    rows = db.query(ModelMeta).all()
    return [
        {
            "symbol": r.symbol,
            "accuracy": round(r.accuracy, 4),
            "roc_auc": round(r.roc_auc, 4),
            "n_samples": r.n_samples,
            "trained_at": r.trained_at.isoformat(),
        }
        for r in rows
    ]


# --- Paper trading ---

@router.get("/portfolio")
def portfolio(db: Session = Depends(get_db)):
    pf = paper.get_or_create_portfolio(db)
    paper.mark_to_market(db, pf)
    summary = paper.portfolio_summary(pf)
    positions = [
        {
            "id": p.id,
            "symbol": p.symbol,
            "contract_symbol": p.contract_symbol,
            "option_type": p.option_type,
            "strike": p.strike,
            "expiry": p.expiry,
            "quantity": p.quantity,
            "entry_price": p.entry_price,
            "current_price": p.current_price,
            "cost_basis": round(p.cost_basis, 2),
            "market_value": round(p.market_value, 2),
            "unrealized_pnl": round(p.unrealized_pnl, 2),
            "status": p.status,
        }
        for p in pf.positions
        if p.status == "open"
    ]
    return {"summary": summary, "positions": positions}


@router.post("/trade")
def trade(req: TradeRequest, db: Session = Depends(get_db)):
    pf = paper.get_or_create_portfolio(db)
    pos, msg = paper.open_position(
        db, pf, req.symbol.upper(), req.option_type, req.contract_symbol,
        req.strike, req.expiry, req.quantity, req.price, req.note,
    )
    if pos is None:
        raise HTTPException(400, msg)
    return {"message": msg, "position_id": pos.id}


@router.post("/close")
def close(req: CloseRequest, db: Session = Depends(get_db)):
    pf = paper.get_or_create_portfolio(db)
    ok, msg = paper.close_position(db, pf, req.position_id, req.price)
    if not ok:
        raise HTTPException(400, msg)
    return {"message": msg}


@router.post("/flip")
def flip(req: CloseRequest, db: Session = Depends(get_db)):
    """Reverse a position: close it and open the opposite side, same size.

    Ordering matters. The replacement leg is located and validated BEFORE
    anything is closed, because the failure mode of doing it the other way
    round is being left flat when you meant to be reversed -- or worse, flat
    without realising it. If we cannot open the other side, we close nothing
    and say why.
    """
    from ..trading import session as sess

    pf = paper.get_or_create_portfolio(db)
    pos = next((p for p in pf.positions if p.id == req.position_id and p.is_open), None)
    if pos is None:
        raise HTTPException(404, "Position not found or already closed.")

    # Refuse the whole operation if a new entry is not permitted right now,
    # rather than half-completing it.
    if settings.enforce_no_overnight:
        allowed, why = sess.can_open()
        if not allowed:
            raise HTTPException(400, f"Flip blocked: {why}. Use Close instead.")

    opposite = "put" if pos.option_type == "call" else "call"
    chain = md.get_option_chain(pos.symbol, pos.expiry)
    side = chain.get("puts" if opposite == "put" else "calls")
    if side is None or side.empty:
        raise HTTPException(400, f"No {opposite} chain available for {pos.symbol}.")

    # Prefer the identical strike; fall back to the nearest listed one.
    exact = side[side["strike"] == pos.strike]
    row = exact.iloc[0] if len(exact) else side.iloc[
        (side["strike"] - pos.strike).abs().argsort().iloc[0]
    ]
    bid, ask = float(row.get("bid") or 0), float(row.get("ask") or 0)
    if bid <= 0 or ask <= 0:
        raise HTTPException(400, f"{opposite.title()} has no two-sided market; cannot flip.")
    price = round((bid + ask) / 2, 2)

    close_ok, close_msg = paper.close_position(db, pf, pos.id)
    if not close_ok:
        raise HTTPException(400, f"Flip aborted, nothing changed: {close_msg}")

    new_pos, open_msg = paper.open_position(
        db, pf, pos.symbol, opposite, str(row["contractSymbol"]),
        float(row["strike"]), pos.expiry, pos.quantity, price,
        note=f"flip from {pos.option_type} {pos.strike}",
    )
    if new_pos is None:
        # The close already happened; be explicit that we are now flat.
        raise HTTPException(
            400,
            f"Closed {pos.option_type} but could NOT open {opposite}: {open_msg}. "
            "You are now FLAT.",
        )
    return {
        "message": f"Flipped {pos.symbol} {pos.option_type} ${pos.strike} -> "
                   f"{opposite} ${row['strike']} x{pos.quantity} @ ${price:.2f}",
        "position_id": new_pos.id,
    }


@router.get("/gamma/{symbol}")
def gamma_profile(symbol: str, expiry: str | None = None):
    """Dealer gamma exposure: which regime the tape is in right now.

    Not a directional call -- it says whether dealer hedging is currently
    dampening moves (reversion) or amplifying them (momentum).
    """
    from ..trading import gamma as gx

    prof = gx.compute_gamma_profile(symbol.upper(), expiry)
    if prof is None:
        raise HTTPException(404, f"No gamma data for {symbol}.")
    return {
        "symbol": prof.symbol, "spot": prof.spot, "expiry": prof.expiry,
        "total_gex": prof.total_gex, "call_gex": prof.call_gex,
        "put_gex": prof.put_gex, "flip_point": prof.flip_point,
        "pin_strike": prof.largest_strike, "regime": prof.regime,
        "summary": prof.describe(),
        "by_strike": prof.by_strike[:60],
    }


# --- Live moomoo account (real balances, positions, working orders) ---

@router.get("/account")
def account():
    """Real broker balances. Distinct from /api/portfolio, which is paper."""
    from ..trading import moomoo_account as ma

    if not ma.configured():
        return {"ok": False, "message": "MOOMOO_OPEND_HOST not set."}
    return ma.account_summary()


@router.post("/trade/close_broker")
def close_broker_position(req: BrokerCloseRequest):
    """Sell a real broker position directly, bypassing the paper ledger.

    These positions exist at the broker, not in our database -- they may
    predate this app entirely -- so closing goes straight to moomoo.
    """
    from ..trading import moomoo_orders

    if req.qty <= 0:
        raise HTTPException(400, "Quantity must be positive.")
    res = moomoo_orders.place_option_order(
        symbol=req.code, contract_symbol=req.code,
        side="SELL", quantity=req.qty, price=req.price,
    )
    if not res.ok:
        raise HTTPException(400, f"moomoo rejected: {res.message}")
    return {"message": f"Sell {req.qty} {req.code} submitted (#{res.order_id})."}


@router.get("/broker/positions")
def broker_positions():
    from ..trading import moomoo_account as ma

    return ma.positions()


@router.get("/broker/orders")
def broker_orders(open_only: bool = True):
    from ..trading import moomoo_account as ma

    return ma.orders(open_only=open_only)


@router.post("/broker/orders/cancel")
def broker_cancel(req: CancelRequest):
    from ..trading import moomoo_account as ma

    ok, msg = ma.cancel_order(req.order_id)
    if not ok:
        raise HTTPException(400, msg)
    return {"message": msg}


@router.post("/broker/orders/amend")
def broker_amend(req: AmendRequest):
    from ..trading import moomoo_account as ma

    ok, msg = ma.amend_order(req.order_id, req.qty, req.price)
    if not ok:
        raise HTTPException(400, msg)
    return {"message": msg}


# --- BTC hourly prediction-market bot ---

@router.get("/prediction/status")
def prediction_status(db: Session = Depends(get_db)):
    from datetime import datetime

    state = prediction_bot.get_state(db)
    return {
        "enabled": settings.prediction_enabled,
        "mode": settings.prediction_trade_mode,
        "paused": bool(state.paused),
        "reason": state.reason,
        "bankroll": round(prediction_risk.current_bankroll(db), 2),
        "today_pnl": round(prediction_risk.today_realized_pnl(db, datetime.utcnow()), 2),
        "open_positions": len(prediction_risk.open_positions(db)),
        "limits": {
            "min_edge": settings.prediction_min_edge,
            "max_stake_usd": settings.prediction_max_stake_usd,
            "max_daily_loss": settings.prediction_max_daily_loss,
            "max_open": settings.prediction_max_open,
        },
    }


@router.get("/prediction/trades")
def prediction_trades(limit: int = 100, db: Session = Depends(get_db)):
    rows = (
        db.query(PredictionTrade)
        .order_by(PredictionTrade.opened_at.desc())
        .limit(min(limit, 500))
        .all()
    )
    return [
        {
            "ticker": r.ticker,
            "side": r.side,
            "strike": r.strike,
            "close_time": r.close_time.isoformat(),
            "probability": r.probability,
            "entry_price": r.entry_price,
            "quantity": r.quantity,
            "stake": r.stake,
            "mode": r.mode,
            "status": r.status,
            "result": r.result,
            "pnl": r.pnl,
            "settlement_price": r.settlement_price,
            "opened_at": r.opened_at.isoformat(),
            "rationale": r.rationale,
        }
        for r in rows
    ]


@router.post("/prediction/pause")
def prediction_pause(db: Session = Depends(get_db)):
    prediction_bot.set_paused(db, True, "paused via API")
    return {"paused": True}


@router.post("/prediction/resume")
def prediction_resume(db: Session = Depends(get_db)):
    prediction_bot.set_paused(db, False, "")
    return {"paused": False}


@router.post("/prediction/run")
def prediction_run_once():
    """Trigger one decision cycle immediately (debug/ops helper)."""
    return prediction_bot.run_cycle()


@router.get("/trades")
def trades(db: Session = Depends(get_db)):
    pf = paper.get_or_create_portfolio(db)
    rows = sorted(pf.trades, key=lambda t: t.timestamp, reverse=True)[:100]
    return [
        {
            "symbol": t.symbol,
            "contract_symbol": t.contract_symbol,
            "side": t.side,
            "option_type": t.option_type,
            "quantity": t.quantity,
            "price": t.price,
            "realized_pnl": round(t.realized_pnl, 2),
            "timestamp": t.timestamp.isoformat(),
        }
        for t in rows
    ]
