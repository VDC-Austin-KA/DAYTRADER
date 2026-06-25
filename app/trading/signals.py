"""Combine model predictions with the options scanner to produce signals."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..data import market_data as md
from ..ml import model as ml_model
from ..models import Signal
from . import options


def generate_signal(symbol: str) -> Optional[dict]:
    """Run the model for ``symbol`` and attach a recommended contract."""
    symbol = symbol.upper()
    df = md.get_history(symbol, years=settings.history_years)
    if df.empty:
        return None

    prob = ml_model.predict_latest(symbol, df)
    if prob is None:
        return None

    underlying = md.get_quote(symbol) or float(df["Close"].iloc[-1])

    if prob >= settings.signal_threshold:
        direction, opt_type = "bullish", "call"
    elif prob <= (1 - settings.signal_threshold):
        direction, opt_type = "bearish", "put"
    else:
        direction, opt_type = "neutral", ""

    result = {
        "symbol": symbol,
        "direction": direction,
        "probability": round(prob, 4),
        "underlying_price": round(underlying, 2),
        "option_type": "",
        "contract_symbol": "",
        "strike": 0.0,
        "expiry": "",
        "dte": 0,
        "option_price": 0.0,
        "breakeven": 0.0,
        "rationale": "",
    }

    if opt_type:
        pick = options.select_contract(symbol, opt_type, underlying)
        if pick:
            result.update(
                option_type=pick.option_type,
                contract_symbol=pick.contract_symbol,
                strike=pick.strike,
                expiry=pick.expiry,
                dte=pick.dte,
                option_price=pick.mid_price,
                breakeven=pick.breakeven,
            )
            conf = abs(prob - 0.5) * 200
            result["rationale"] = (
                f"Model is {conf:.0f}% confident in a {direction} {settings.horizon_days}-day "
                f"move (>{settings.target_move:.1%}). Suggests {pick.option_type.upper()} "
                f"${pick.strike:g} exp {pick.expiry} ({pick.dte} DTE), "
                f"mid ${pick.mid_price:.2f}, breakeven ${pick.breakeven:g}."
            )
        else:
            result["rationale"] = (
                f"{direction.title()} bias (p={prob:.2f}) but no liquid short-dated "
                f"contract within {settings.max_dte} DTE was found."
            )
    else:
        result["rationale"] = (
            f"No edge: model probability {prob:.2f} is inside the neutral band "
            f"around the {settings.signal_threshold:.2f} threshold."
        )
    return result


def refresh_signal(db: Session, symbol: str) -> Optional[Signal]:
    """Generate a signal and upsert it into the database."""
    data = generate_signal(symbol)
    if not data:
        return None
    row = db.query(Signal).filter(Signal.symbol == symbol.upper()).one_or_none()
    if row is None:
        row = Signal(symbol=symbol.upper())
        db.add(row)
    for key, value in data.items():
        setattr(row, key, value)
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row
