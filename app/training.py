"""High-level training orchestration used by the API and scheduler."""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from .config import settings
from .data import market_data as md
from .ml import model as ml_model
from .models import ModelMeta

log = logging.getLogger("daytrader.training")

# Lightweight in-memory progress so the dashboard can show what's happening.
TRAINING_STATUS: dict = {
    "running": False,
    "done": 0,
    "total": 0,
    "current": "",
    "last_results": [],
    "finished_at": None,
}


def train_one(db: Session, symbol: str) -> dict:
    symbol = symbol.upper()
    df = md.get_history(symbol, years=settings.history_years)
    if df.empty:
        return {"symbol": symbol, "status": "no_data"}

    result = ml_model.train_symbol(symbol, df)
    if result is None:
        return {"symbol": symbol, "status": "insufficient_data"}

    meta = db.query(ModelMeta).filter(ModelMeta.symbol == symbol).one_or_none()
    if meta is None:
        meta = ModelMeta(symbol=symbol)
        db.add(meta)
    meta.accuracy = result.accuracy
    meta.roc_auc = result.roc_auc
    meta.n_samples = result.n_samples
    meta.trained_at = datetime.utcnow()
    meta.features = json.dumps(result.feature_importance)
    db.commit()

    return {
        "symbol": symbol,
        "status": "trained",
        "accuracy": round(result.accuracy, 4),
        "roc_auc": round(result.roc_auc, 4),
        "n_samples": result.n_samples,
    }


def train_universe(db: Session, symbols: list[str] | None = None) -> list[dict]:
    symbols = symbols or settings.default_watchlist
    results: list[dict] = []
    TRAINING_STATUS.update(
        running=True, done=0, total=len(symbols), current="",
        last_results=[], finished_at=None,
    )
    for sym in symbols:
        TRAINING_STATUS["current"] = sym
        try:
            results.append(train_one(db, sym))
        except Exception as exc:  # pragma: no cover - resilience
            log.exception("training failed for %s", sym)
            results.append({"symbol": sym, "status": "error", "detail": str(exc)})
        TRAINING_STATUS["done"] += 1
    TRAINING_STATUS.update(
        running=False, current="", last_results=results,
        finished_at=datetime.utcnow().isoformat(),
    )
    return results
