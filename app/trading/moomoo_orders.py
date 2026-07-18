"""Route dashboard option orders through the moomoo OpenD gateway.

Used when ``DASHBOARD_TRADE_MODE=moomoo`` so the Buy/Close buttons hit the
user's real (or paper/SIMULATE) moomoo account instead of the local paper
ledger. One persistent ``OpenSecTradeContext`` is reused; every call runs
behind a lock and returns a small result dict.

Security: trading must be unlocked by hand in the OpenD GUI (moomoo policy
forbids SDK ``unlock_trade``); this module never calls it. Set
``MOOMOO_TRD_ENV=SIMULATE`` until order routing is verified against the
account.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from ..config import settings

log = logging.getLogger("daytrader.trading.moomoo_orders")

_lock = threading.Lock()
_trade_ctx = None


@dataclass
class OrderResult:
    ok: bool
    order_id: str = ""
    filled_price: float = 0.0
    message: str = ""


def configured() -> bool:
    return bool(settings.moomoo_opend_host)


def _to_moomoo_code(symbol: str, contract_symbol: str) -> str:
    """Best-effort map an OCC-style contract symbol to a moomoo US.<...> code.

    moomoo's own option codes come back from its data layer already prefixed
    with ``US.`` — pass those straight through. Anything else is assumed to be
    an OCC symbol and gets the prefix; if the chain provided the code directly
    (the common case here) this is a no-op.
    """
    cs = (contract_symbol or "").strip()
    if cs.upper().startswith("US."):
        return cs
    if cs:
        return f"US.{cs}"
    return f"US.{symbol.upper()}"


def _context():
    global _trade_ctx
    if _trade_ctx is not None:
        return _trade_ctx
    from moomoo import (  # type: ignore[import-not-found]
        OpenSecTradeContext,
        SecurityFirm,
        SysConfig,
        TrdMarket,
    )

    SysConfig.set_all_thread_daemon(True)
    _trade_ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host=settings.moomoo_opend_host,
        port=settings.moomoo_opend_port,
        security_firm=getattr(
            SecurityFirm, settings.moomoo_security_firm, SecurityFirm.FUTUINC
        ),
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


def place_option_order(
    symbol: str, contract_symbol: str, side: str, quantity: int, price: float
) -> OrderResult:
    """Submit a normal limit order for one option contract. ``side`` is
    BUY or SELL; ``price`` is per share (x100 multiplier applied by moomoo)."""
    if not configured():
        return OrderResult(False, message="moomoo OpenD not configured (MOOMOO_OPEND_HOST).")
    with _lock:
        try:
            from moomoo import (  # type: ignore[import-not-found]
                RET_OK,
                OrderType,
                TrdEnv,
                TrdSide,
            )

            ctx = _context()
            trd_env = TrdEnv.REAL if settings.moomoo_trd_env == "REAL" else TrdEnv.SIMULATE
            kwargs = dict(
                price=round(float(price), 2),
                qty=int(quantity),
                code=_to_moomoo_code(symbol, contract_symbol),
                trd_side=TrdSide.BUY if side.upper() == "BUY" else TrdSide.SELL,
                order_type=OrderType.NORMAL,
                trd_env=trd_env,
            )
            if settings.moomoo_acc_id:
                kwargs["acc_id"] = settings.moomoo_acc_id
            ret, data = ctx.place_order(**kwargs)
            if ret != RET_OK:
                return OrderResult(False, message=f"moomoo rejected order: {data}")
            order_id = str(data["order_id"][0]) if "order_id" in data else ""
            return OrderResult(
                ok=True, order_id=order_id, filled_price=round(float(price), 2),
                message=f"moomoo {side} {quantity}x {kwargs['code']} @ {price:.2f} "
                        f"({settings.moomoo_trd_env})",
            )
        except ImportError:
            return OrderResult(False, message="moomoo-api package not installed.")
        except Exception as exc:
            _reset()
            return OrderResult(False, message=f"moomoo OpenD error: {exc}")
