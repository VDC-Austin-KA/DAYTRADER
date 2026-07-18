"""Paper-trading engine: open/close option positions against cached prices."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..data import market_data as md
from ..models import Portfolio, Position, Trade
from . import session


def get_or_create_portfolio(db: Session, name: str = "default") -> Portfolio:
    pf = db.query(Portfolio).filter(Portfolio.name == name).one_or_none()
    if pf is None:
        pf = Portfolio(
            name=name,
            cash=settings.starting_cash,
            starting_cash=settings.starting_cash,
        )
        db.add(pf)
        db.commit()
        db.refresh(pf)
    return pf


def _contract_price(symbol: str, contract_symbol: str, expiry: str,
                    strike: float, option_type: str) -> Optional[float]:
    """Look up the current mid price for a specific contract."""
    chain = md.get_option_chain(symbol, expiry)
    df = chain["calls"] if option_type == "call" else chain["puts"]
    if df is None or df.empty:
        return None
    match = df[df["contractSymbol"] == contract_symbol]
    if match.empty:
        match = df[df["strike"] == strike]
    if match.empty:
        return None
    row = match.iloc[0]
    bid = float(row.get("bid", 0) or 0)
    ask = float(row.get("ask", 0) or 0)
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 2)
    return float(row.get("lastPrice", 0) or 0)


def open_position(
    db: Session,
    portfolio: Portfolio,
    symbol: str,
    option_type: str,
    contract_symbol: str,
    strike: float,
    expiry: str,
    quantity: int,
    price: float,
    note: str = "",
) -> tuple[Optional[Position], str]:
    quantity = int(quantity)
    if quantity <= 0:
        return None, "Quantity must be positive."

    # No-overnight rule, enforced at the order path so no caller -- dashboard,
    # scanner or bot -- can open something that would have to be held.
    if settings.enforce_no_overnight:
        allowed, why = session.can_open()
        if not allowed:
            return None, f"Entry blocked: {why}."
        valid, why = session.validate_expiry(expiry)
        if not valid:
            return None, f"Entry blocked: {why}."

    cost = price * quantity * 100
    if cost > portfolio.cash:
        return None, f"Insufficient cash: need ${cost:,.2f}, have ${portfolio.cash:,.2f}."

    # Route to the real moomoo account when the dashboard is in live mode.
    broker_note = ""
    if settings.dashboard_trade_mode == "moomoo":
        from . import moomoo_orders

        res = moomoo_orders.place_option_order(
            symbol.upper(), contract_symbol, "BUY", quantity, price
        )
        if not res.ok:
            return None, f"moomoo order failed: {res.message}"
        if res.filled_price:
            price = res.filled_price
            cost = price * quantity * 100
        broker_note = f" [moomoo #{res.order_id}]"

    underlying = md.get_quote(symbol) or 0.0
    pos = Position(
        portfolio_id=portfolio.id,
        symbol=symbol.upper(),
        contract_symbol=contract_symbol,
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        quantity=quantity,
        entry_price=price,
        entry_underlying=underlying,
        current_price=price,
        note=(note + broker_note).strip(),
    )
    portfolio.cash -= cost
    db.add(pos)
    db.add(
        Trade(
            portfolio_id=portfolio.id,
            symbol=symbol.upper(),
            contract_symbol=contract_symbol,
            side="buy",
            option_type=option_type,
            quantity=quantity,
            price=price,
        )
    )
    db.commit()
    db.refresh(pos)
    return pos, "Position opened."


def close_position(db: Session, portfolio: Portfolio, position_id: int,
                   price: Optional[float] = None) -> tuple[bool, str]:
    pos = (
        db.query(Position)
        .filter(Position.id == position_id, Position.portfolio_id == portfolio.id)
        .one_or_none()
    )
    if pos is None or pos.status != "open":
        return False, "Position not found or already closed."

    if price is None:
        price = _contract_price(
            pos.symbol, pos.contract_symbol, pos.expiry, pos.strike, pos.option_type
        )
        if price is None:
            price = pos.current_price

    # Sell to close through the real account when in live mode.
    if settings.dashboard_trade_mode == "moomoo":
        from . import moomoo_orders

        res = moomoo_orders.place_option_order(
            pos.symbol, pos.contract_symbol, "SELL", pos.quantity, price
        )
        if not res.ok:
            return False, f"moomoo close failed: {res.message}"
        if res.filled_price:
            price = res.filled_price

    proceeds = price * pos.quantity * 100
    realized = proceeds - pos.cost_basis

    portfolio.cash += proceeds
    pos.status = "closed"
    pos.current_price = price
    db.add(
        Trade(
            portfolio_id=portfolio.id,
            symbol=pos.symbol,
            contract_symbol=pos.contract_symbol,
            side="sell",
            option_type=pos.option_type,
            quantity=pos.quantity,
            price=price,
            realized_pnl=realized,
        )
    )
    db.commit()
    return True, f"Closed for ${proceeds:,.2f} (P&L ${realized:,.2f})."


def mark_to_market(db: Session, portfolio: Portfolio) -> None:
    """Refresh current prices on all open positions."""
    for pos in portfolio.positions:
        if pos.status != "open":
            continue
        price = _contract_price(
            pos.symbol, pos.contract_symbol, pos.expiry, pos.strike, pos.option_type
        )
        if price is not None and price > 0:
            pos.current_price = price
    db.commit()


def portfolio_summary(portfolio: Portfolio) -> dict:
    open_positions = [p for p in portfolio.positions if p.status == "open"]
    market_value = sum(p.market_value for p in open_positions)
    unrealized = sum(p.unrealized_pnl for p in open_positions)
    equity = portfolio.cash + market_value
    return {
        "cash": round(portfolio.cash, 2),
        "market_value": round(market_value, 2),
        "equity": round(equity, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_return_pct": round(
            (equity - portfolio.starting_cash) / portfolio.starting_cash * 100, 2
        ),
        "open_positions": len(open_positions),
        "starting_cash": portfolio.starting_cash,
    }
