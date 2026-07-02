"""JSON API endpoints."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..data import market_data as md
from ..database import get_db
from ..models import ModelMeta, Signal
from ..schemas import CloseRequest, TradeRequest, TrainRequest
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
    return {
        "max_dte": settings.max_dte,
        "min_dte": settings.min_dte,
        "signal_threshold": settings.signal_threshold,
        "horizon_days": settings.horizon_days,
        "target_move": settings.target_move,
        "watchlist": settings.default_watchlist,
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
        raise HTTPException(400, "No market-data source configured (set TRADIER_TOKEN).")
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
