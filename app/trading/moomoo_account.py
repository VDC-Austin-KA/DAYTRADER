"""Live account state from moomoo: balances, positions, open orders.

The dashboard's original summary cards read a simulated portfolio out of
the local database -- a fixed STARTING_CASH balance that has nothing to do
with the real brokerage account. This module replaces that with the truth
from OpenD, so what the screen says matches what the account actually holds.

Currency: the account's base currency may not be USD (this one reports in
HKD). US options buying power lives in the ``usd_*`` fields, so both are
surfaced separately rather than silently mixing units -- conflating them
would overstate what can actually be spent on a US options trade.

Everything degrades to None/empty on failure: a dead gateway must never
take the dashboard down, and a blank panel is safer than a stale one that
looks live.
"""
from __future__ import annotations

import logging
import threading
import time

from ..config import settings

log = logging.getLogger("daytrader.moomoo_account")

_lock = threading.Lock()
_trade_ctx = None

# Balances/positions are polled by the UI; cache briefly so a fast refresh
# interval cannot hammer OpenD.
_CACHE_TTL = 3.0
_cache: dict[str, tuple[float, object]] = {}


def configured() -> bool:
    return bool(settings.moomoo_opend_host)


def _num(row, col, default=0.0) -> float:
    """moomoo returns 'N/A' strings for fields a given account lacks."""
    try:
        v = row.get(col)
        if v is None or v != v or v == "N/A":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _context():
    global _trade_ctx
    if _trade_ctx is not None:
        return _trade_ctx
    from moomoo import (  # type: ignore[import-not-found]
        OpenSecTradeContext, SecurityFirm, SysConfig, TrdMarket,
    )

    SysConfig.set_all_thread_daemon(True)
    firm = getattr(SecurityFirm, settings.moomoo_security_firm, SecurityFirm.FUTUINC)
    _trade_ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host=settings.moomoo_opend_host,
        port=settings.moomoo_opend_port,
        security_firm=firm,
    )
    return _trade_ctx


def _reset() -> None:
    global _trade_ctx
    ctx, _trade_ctx = _trade_ctx, None
    if ctx is not None:
        try:
            ctx.close()
        except Exception:
            pass


def _trd_env():
    from moomoo import TrdEnv  # type: ignore[import-not-found]

    return TrdEnv.REAL if settings.moomoo_trd_env == "REAL" else TrdEnv.SIMULATE


def _call(fn_name: str, **kwargs):
    """One trade-context call. Returns a DataFrame, or None on any failure."""
    if not configured():
        return None
    from ..data import moomoo_data

    if not moomoo_data._reachable():      # fail fast, don't block in the SDK
        return None
    with _lock:
        try:
            from moomoo import RET_OK  # type: ignore[import-not-found]

            ctx = _context()
            kwargs.setdefault("trd_env", _trd_env())
            if settings.moomoo_acc_id:
                kwargs.setdefault("acc_id", settings.moomoo_acc_id)
            ret, data = getattr(ctx, fn_name)(**kwargs)
            if ret != RET_OK:
                log.warning("moomoo %s failed: %s", fn_name, data)
                return None
            return data
        except ImportError:
            return None
        except Exception as exc:
            log.warning("moomoo %s error: %s (resetting)", fn_name, exc)
            _reset()
            return None


def _cached(key: str, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def account_summary() -> dict:
    """Balances as the broker sees them. ``ok`` False means no live data."""

    def _fetch():
        df = _call("accinfo_query")
        if df is None or len(df) == 0:
            return {"ok": False, "message": "No account data from OpenD."}
        r = df.iloc[0]
        return {
            "ok": True,
            "env": settings.moomoo_trd_env,
            "currency": r.get("currency") or "",
            # Account-wide, in the account's base currency.
            "total_assets": _num(r, "total_assets"),
            "cash": _num(r, "cash"),
            "market_value": _num(r, "market_val"),
            "frozen_cash": _num(r, "frozen_cash"),
            "unrealized_pl": _num(r, "unrealized_pl"),
            "realized_pl": _num(r, "realized_pl"),
            # US sleeve -- what a US options order can actually draw on.
            "us_cash": _num(r, "us_cash"),
            "us_assets": _num(r, "usd_assets"),
            "us_buying_power": _num(r, "usd_net_cash_power"),
            "risk_status": r.get("risk_status") or "",
            "is_pdt": bool(r.get("is_pdt")),
        }

    return _cached("summary", _fetch)


def positions() -> list[dict]:
    """Real open positions. Empty list when flat or unavailable."""

    def _fetch():
        df = _call("position_list_query")
        if df is None or len(df) == 0:
            return []
        out = []
        for _, r in df.iterrows():
            qty = _num(r, "qty")
            if not qty:
                continue
            out.append({
                "code": r.get("code") or "",
                "name": r.get("stock_name") or "",
                "qty": qty,
                "can_sell_qty": _num(r, "can_sell_qty"),
                "cost_price": _num(r, "cost_price"),
                "current_price": _num(r, "nominal_price"),
                "market_value": _num(r, "market_val"),
                "pl_val": _num(r, "pl_val"),
                "pl_ratio": _num(r, "pl_ratio"),
                "today_pl_val": _num(r, "today_pl_val"),
                "currency": r.get("currency") or "",
            })
        return out

    return _cached("positions", _fetch)


# Order states that can still be cancelled or amended.
_LIVE_STATES = {
    "SUBMITTING", "SUBMITTED", "WAITING_SUBMIT", "FILLED_PART",
}


def orders(open_only: bool = True) -> list[dict]:
    """Today's orders. ``open_only`` keeps just those still working."""

    def _fetch():
        df = _call("order_list_query")
        if df is None or len(df) == 0:
            return []
        out = []
        for _, r in df.iterrows():
            status = str(r.get("order_status") or "")
            live = status.upper() in _LIVE_STATES
            if open_only and not live:
                continue
            out.append({
                "order_id": str(r.get("order_id") or ""),
                "code": r.get("code") or "",
                "name": r.get("stock_name") or "",
                "side": str(r.get("trd_side") or ""),
                "order_type": str(r.get("order_type") or ""),
                "status": status,
                "qty": _num(r, "qty"),
                "price": _num(r, "price"),
                "dealt_qty": _num(r, "dealt_qty"),
                "dealt_avg_price": _num(r, "dealt_avg_price"),
                "create_time": str(r.get("create_time") or ""),
                "err": str(r.get("last_err_msg") or ""),
                "cancellable": live,
            })
        return out

    return _cached(f"orders_{open_only}", _fetch)


def _modify(order_id: str, op: str, qty: float = 0, price: float = 0) -> tuple[bool, str]:
    """Shared path for cancel / amend via modify_order."""
    if not configured():
        return False, "moomoo not configured."
    from moomoo import ModifyOrderOp, RET_OK  # type: ignore[import-not-found]

    op_enum = {
        "cancel": ModifyOrderOp.CANCEL,
        "normal": ModifyOrderOp.NORMAL,
    }.get(op)
    if op_enum is None:
        return False, f"Unsupported operation {op!r}."

    with _lock:
        try:
            ctx = _context()
            kwargs = {
                "modify_order_op": op_enum,
                "order_id": order_id,
                "qty": qty,
                "price": price,
                "trd_env": _trd_env(),
            }
            if settings.moomoo_acc_id:
                kwargs["acc_id"] = settings.moomoo_acc_id
            ret, data = ctx.modify_order(**kwargs)
            if ret != RET_OK:
                return False, f"moomoo rejected: {data}"
        except Exception as exc:
            _reset()
            return False, f"moomoo error: {exc}"

    _cache.pop("orders_True", None)     # reflect the change immediately
    _cache.pop("orders_False", None)
    return True, "Order cancelled." if op == "cancel" else "Order updated."


def cancel_order(order_id: str) -> tuple[bool, str]:
    return _modify(order_id, "cancel")


def amend_order(order_id: str, qty: float, price: float) -> tuple[bool, str]:
    """Change price and/or quantity of a working order."""
    if qty <= 0 or price <= 0:
        return False, "Quantity and price must both be positive."
    return _modify(order_id, "normal", qty=qty, price=price)
