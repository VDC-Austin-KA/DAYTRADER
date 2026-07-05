"""Order execution for prediction-market contracts.

Two executors share one interface:

* ``PaperExecutor``   — always available; fills at the quoted ask and lets
  the bot settle against the real BTC index at the hour boundary.
* ``MoomooExecutor``  — routes real orders through the moomoo OpenD gateway
  using credentials/config from the environment (Railway service
  variables). It is used only when ``PREDICTION_TRADE_MODE=live`` and the
  gateway is reachable; any failure falls back to paper with a loud log so
  the bot keeps running rather than dying mid-session.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass

from ..config import settings

log = logging.getLogger("daytrader.prediction.execution")


@dataclass
class ExecutionResult:
    ok: bool
    mode: str            # paper / live
    order_id: str = ""
    fill_price: float = 0.0  # dollars per contract
    message: str = ""


class PaperExecutor:
    mode = "paper"

    def place_order(
        self, ticker: str, side: str, quantity: int, limit_price: float
    ) -> ExecutionResult:
        return ExecutionResult(
            ok=True,
            mode=self.mode,
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
            fill_price=limit_price,
            message=f"paper fill {side} {quantity}x {ticker} @ {limit_price:.2f}",
        )


class MoomooExecutor:
    """Thin adapter over the moomoo OpenAPI (OpenD gateway).

    Orders are throttled to stay under OpenD's order-rate limits. The
    Kalshi-style ticker is mapped to a broker code via MOOMOO_CODE_PREFIX
    so the symbol convention can be adjusted from the environment without
    a code change.
    """

    mode = "live"
    _MIN_ORDER_INTERVAL = 3.0  # seconds between orders
    # Hard ceiling on one order attempt: the SDK retries a dead gateway
    # forever, which would otherwise wedge the scheduler cycle permanently.
    _ORDER_TIMEOUT = 30.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_order_ts = 0.0

    @staticmethod
    def configured() -> bool:
        return bool(settings.moomoo_opend_host)

    def place_order(
        self, ticker: str, side: str, quantity: int, limit_price: float
    ) -> ExecutionResult:
        with self._lock:
            wait = self._MIN_ORDER_INTERVAL - (time.time() - self._last_order_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_order_ts = time.time()

        # Run the SDK call on a daemon thread with a hard timeout.
        results: list[ExecutionResult] = []
        worker = threading.Thread(
            target=lambda: results.append(
                self._place_order(ticker, side, quantity, limit_price)
            ),
            daemon=True,
        )
        worker.start()
        worker.join(self._ORDER_TIMEOUT)
        if not results:
            return ExecutionResult(
                ok=False,
                mode=self.mode,
                message=f"OpenD did not respond within {self._ORDER_TIMEOUT:.0f}s "
                        "(is the gateway running and logged in?)",
            )
        return results[0]

    def _place_order(
        self, ticker: str, side: str, quantity: int, limit_price: float
    ) -> ExecutionResult:
        try:
            from moomoo import (  # type: ignore[import-not-found]
                RET_OK,
                OpenSecTradeContext,
                OrderType,
                SecurityFirm,
                SysConfig,
                TrdEnv,
                TrdMarket,
                TrdSide,
            )
        except ImportError:
            return ExecutionResult(
                ok=False, mode=self.mode, message="moomoo-api package not installed"
            )

        code = f"{settings.moomoo_code_prefix}{ticker}"
        # A YES position is a buy of the YES contract; a NO position is a buy
        # of the NO contract (same code, price = 1 - yes). moomoo quotes event
        # contracts per side, so both map to a plain BUY at our limit.
        price = round(limit_price, 2)
        trd_env = TrdEnv.REAL if settings.moomoo_trd_env == "REAL" else TrdEnv.SIMULATE
        firm = getattr(SecurityFirm, settings.moomoo_security_firm, SecurityFirm.FUTUINC)

        ctx = None
        try:
            # SDK threads must not keep the process alive if the gateway hangs.
            SysConfig.set_all_thread_daemon(True)
            ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.US,
                host=settings.moomoo_opend_host,
                port=settings.moomoo_opend_port,
                security_firm=firm,
            )
            # NOTE: trading is NOT unlocked via the SDK. Per moomoo's OpenAPI
            # security policy, unlock trading manually in the OpenD GUI; live
            # orders fail until that is done.
            kwargs = dict(
                price=price,
                qty=quantity,
                code=code,
                trd_side=TrdSide.BUY,
                order_type=OrderType.NORMAL,
                trd_env=trd_env,
            )
            if settings.moomoo_acc_id:
                kwargs["acc_id"] = settings.moomoo_acc_id
            ret, data = ctx.place_order(**kwargs)
            if ret != RET_OK:
                return ExecutionResult(
                    ok=False, mode=self.mode, message=f"place_order failed: {data}"
                )
            order_id = str(data["order_id"][0]) if "order_id" in data else ""
            return ExecutionResult(
                ok=True,
                mode=self.mode,
                order_id=order_id,
                fill_price=price,
                message=f"live order {side} {quantity}x {code} @ {price:.2f}",
            )
        except Exception as exc:
            return ExecutionResult(
                ok=False, mode=self.mode, message=f"OpenD error: {exc}"
            )
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass


_moomoo_singleton = MoomooExecutor()


def get_executor() -> PaperExecutor | MoomooExecutor:
    """Live executor when configured for live trading, else paper."""
    if settings.prediction_trade_mode == "live":
        if MoomooExecutor.configured():
            return _moomoo_singleton
        log.error(
            "PREDICTION_TRADE_MODE=live but MOOMOO_OPEND_HOST is not set; "
            "falling back to paper execution"
        )
    return PaperExecutor()
