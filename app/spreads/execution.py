"""Asynchronous execution bridge to moomoo OpenD (with a paper twin).

The moomoo OpenAPI SDK is synchronous, so every call is pushed onto a
worker thread with ``asyncio.to_thread`` behind an ``asyncio.Lock`` — the
event loop (and therefore the market-data pipeline) never blocks on the
gateway. One ``OpenSecTradeContext`` is kept alive for the session instead
of paying the connect/handshake tax per order.

Order safety:

* **Staleness guardrail** — immediately before an order goes out, the
  freshest WebSocket tick behind each leg is re-checked against the
  execution engine's own clock; anything older than
  ``max_tick_staleness_ms`` (default 150ms) aborts the entry.
* **Leg sequencing** — the long (buy) leg always fills first so the book
  never holds a naked short option; if the short leg is then rejected the
  long leg is unwound at once.
* **Limit pricing** — buys at mid * (1 + slippage), sells at
  mid * (1 - slippage): crosses a tight market immediately, never lifts a
  runaway quote.

moomoo has no native multi-leg combo ticket over OpenAPI, so verticals are
legged with the sequencing above.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from .chain import ChainStore
from .config import SpreadsConfig
from .models import (
    LegFill,
    OptionContract,
    Quote,
    SpreadCandidate,
    SpreadKind,
    SpreadPosition,
)

log = logging.getLogger("daytrader.spreads.execution")


# --------------------------------------------------------------------------- #
# Pure pricing / guardrail helpers (unit-tested, no I/O)
# --------------------------------------------------------------------------- #
def limit_price(mid: float, side: str, slippage_pct: float) -> float:
    """Marketable limit around mid with a tight slippage tolerance."""
    factor = 1 + slippage_pct if side == "BUY" else 1 - slippage_pct
    return max(0.01, round(mid * factor, 2))


def quotes_fresh(quotes: list[Quote], max_staleness_ms: float, now_ns: int | None = None) -> bool:
    """Order-timing guardrail: every leg's tick must beat the staleness cap."""
    now_ns = now_ns or time.time_ns()
    return all(0 < q.ts_ns and q.age_ms(now_ns) <= max_staleness_ms for q in quotes)


def plan_entry_legs(cand: SpreadCandidate, slippage_pct: float) -> list[LegFill]:
    """Entry legs in execution order (long/buy leg first, short covered)."""
    buy = LegFill(
        contract=cand.long_leg.contract,
        side="BUY",
        quantity=cand.quantity,
        limit_price=limit_price(cand.long_leg.mid, "BUY", slippage_pct),
    )
    sell = LegFill(
        contract=cand.short_leg.contract,
        side="SELL",
        quantity=cand.quantity,
        limit_price=limit_price(cand.short_leg.mid, "SELL", slippage_pct),
    )
    return [buy, sell]


def plan_exit_legs(pos: SpreadPosition, chain: ChainStore, slippage_pct: float) -> list[LegFill]:
    """Closing legs (short bought back first — lift the risk leg first)."""
    cand = pos.candidate
    legs: list[LegFill] = []
    for quote_ref, side in ((cand.short_leg, "BUY"), (cand.long_leg, "SELL")):
        live = chain.quote(quote_ref.contract) or quote_ref
        legs.append(
            LegFill(
                contract=quote_ref.contract,
                side=side,
                quantity=cand.quantity,
                limit_price=limit_price(live.mid, side, slippage_pct),
            )
        )
    return legs


# --------------------------------------------------------------------------- #
# Executors
# --------------------------------------------------------------------------- #
class PaperSpreadExecutor:
    """Fills every leg at its limit — for dry runs and SIMULATE-less dev."""

    mode = "paper"

    async def place_leg(self, leg: LegFill) -> LegFill:
        leg.ok = True
        leg.order_id = f"paper-{uuid.uuid4().hex[:10]}"
        leg.message = (
            f"paper fill {leg.side} {leg.quantity}x "
            f"{leg.contract.occ_symbol} @ {leg.limit_price:.2f}"
        )
        return leg

    async def account_snapshot(self) -> dict[str, float]:
        return {}

    async def close(self) -> None:
        return None


class MoomooSpreadExecutor:
    """Async adapter over one persistent OpenSecTradeContext."""

    mode = "live"
    _MIN_ORDER_INTERVAL = 0.35  # stay under OpenD order-rate limits

    def __init__(self, cfg: SpreadsConfig) -> None:
        self.cfg = cfg
        self._ctx = None
        self._lock = asyncio.Lock()
        self._last_order_ts = 0.0

    def _context(self):
        """Build (once) and return the trade context. Runs on a worker thread."""
        if self._ctx is not None:
            return self._ctx
        from moomoo import (  # type: ignore[import-not-found]
            OpenSecTradeContext,
            SecurityFirm,
            SysConfig,
            TrdMarket,
        )

        SysConfig.set_all_thread_daemon(True)
        self._ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=self.cfg.moomoo_opend_host,
            port=self.cfg.moomoo_opend_port,
            security_firm=getattr(
                SecurityFirm, self.cfg.moomoo_security_firm, SecurityFirm.FUTUINC
            ),
        )
        # Trading must already be unlocked in the OpenD GUI (moomoo security
        # policy forbids SDK unlock); orders fail loudly otherwise.
        return self._ctx

    async def place_leg(self, leg: LegFill) -> LegFill:
        async with self._lock:
            wait = self._MIN_ORDER_INTERVAL - (time.monotonic() - self._last_order_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_order_ts = time.monotonic()
            return await asyncio.to_thread(self._place_leg_sync, leg)

    def _place_leg_sync(self, leg: LegFill) -> LegFill:
        try:
            from moomoo import RET_OK, OrderType, TrdEnv, TrdSide  # type: ignore

            ctx = self._context()
            kwargs = dict(
                price=leg.limit_price,
                qty=leg.quantity,
                code=leg.contract.moomoo_code,
                trd_side=TrdSide.BUY if leg.side == "BUY" else TrdSide.SELL,
                order_type=OrderType.NORMAL,
                trd_env=TrdEnv.REAL if self.cfg.moomoo_trd_env == "REAL" else TrdEnv.SIMULATE,
            )
            if self.cfg.moomoo_acc_id:
                kwargs["acc_id"] = self.cfg.moomoo_acc_id
            ret, data = ctx.place_order(**kwargs)
            if ret != RET_OK:
                leg.ok, leg.message = False, f"place_order failed: {data}"
                return leg
            leg.ok = True
            leg.order_id = str(data["order_id"][0]) if "order_id" in data else ""
            leg.message = (
                f"live order {leg.side} {leg.quantity}x "
                f"{leg.contract.moomoo_code} @ {leg.limit_price:.2f}"
            )
        except Exception as exc:
            leg.ok, leg.message = False, f"OpenD error: {exc}"
        return leg

    async def account_snapshot(self) -> dict[str, float]:
        """Portfolio-margin numbers for the watchdog (empty dict on failure)."""
        async with self._lock:
            return await asyncio.to_thread(self._account_snapshot_sync)

    def _account_snapshot_sync(self) -> dict[str, float]:
        try:
            from moomoo import RET_OK, TrdEnv  # type: ignore

            ctx = self._context()
            ret, data = ctx.accinfo_query(
                trd_env=TrdEnv.REAL if self.cfg.moomoo_trd_env == "REAL" else TrdEnv.SIMULATE
            )
            if ret != RET_OK or len(data) == 0:
                log.warning("accinfo_query failed: %s", data)
                return {}
            row = data.iloc[0]

            def _num(col: str) -> float:
                try:
                    v = float(row[col])
                    return v if v == v else 0.0
                except (KeyError, TypeError, ValueError):
                    return 0.0

            return {
                "equity": _num("total_assets"),
                "cash": _num("cash"),
                "maintenance_margin": _num("maintenance_margin"),
                "initial_margin": _num("initial_margin"),
                "buying_power": _num("power"),
            }
        except Exception as exc:
            log.warning("account snapshot error: %s", exc)
            return {}

    async def close(self) -> None:
        ctx, self._ctx = self._ctx, None
        if ctx is not None:
            try:
                await asyncio.to_thread(ctx.close)
            except Exception:
                pass


Executor = PaperSpreadExecutor | MoomooSpreadExecutor


def build_executor(cfg: SpreadsConfig) -> Executor:
    if cfg.trade_mode == "live":
        if cfg.moomoo_opend_host:
            return MoomooSpreadExecutor(cfg)
        log.error(
            "SPREADS_TRADE_MODE=live but MOOMOO_OPEND_HOST is unset; "
            "falling back to paper execution"
        )
    return PaperSpreadExecutor()


# --------------------------------------------------------------------------- #
# Order router: guardrail + sequenced legging
# --------------------------------------------------------------------------- #
class SpreadRouter:
    def __init__(self, cfg: SpreadsConfig, chain: ChainStore, executor: Executor) -> None:
        self.cfg = cfg
        self.chain = chain
        self.executor = executor

    def _live_legs(self, cand: SpreadCandidate) -> list[Quote] | None:
        """Re-read both legs from the chain so the guardrail sees the
        freshest ticks, not the ones the scanner captured."""
        legs = [self.chain.quote(cand.short_leg.contract), self.chain.quote(cand.long_leg.contract)]
        return None if any(q is None for q in legs) else legs  # type: ignore[return-value]

    async def open_spread(self, cand: SpreadCandidate) -> SpreadPosition | None:
        live = self._live_legs(cand)
        if live is None:
            log.info("entry aborted: leg vanished from the chain")
            return None
        if not quotes_fresh(live, self.cfg.max_tick_staleness_ms):
            ages = ", ".join(f"{q.age_ms():.0f}ms" for q in live)
            log.warning(
                "entry aborted by staleness guardrail (>%.0fms): %s",
                self.cfg.max_tick_staleness_ms, ages,
            )
            return None

        legs = plan_entry_legs(cand, self.cfg.slippage_tolerance_pct)
        buy_leg = await self.executor.place_leg(legs[0])
        if not buy_leg.ok:
            log.error("long leg rejected, spread abandoned: %s", buy_leg.message)
            return None
        sell_leg = await self.executor.place_leg(legs[1])
        if not sell_leg.ok:
            log.error("short leg rejected (%s); unwinding long leg", sell_leg.message)
            unwind = LegFill(
                contract=buy_leg.contract,
                side="SELL",
                quantity=buy_leg.quantity,
                limit_price=limit_price(
                    (self.chain.quote(buy_leg.contract) or cand.long_leg).mid,
                    "SELL",
                    self.cfg.slippage_tolerance_pct * 3,  # get out, generously
                ),
            )
            await self.executor.place_leg(unwind)
            return None

        entry = sell_leg.limit_price - buy_leg.limit_price
        pos = SpreadPosition(candidate=cand, fills=[buy_leg, sell_leg], entry_price=entry)
        log.info("OPENED %s | net %.2f", cand.describe(), entry)
        return pos

    async def close_spread(self, pos: SpreadPosition, panic: bool = False) -> None:
        """Flatten one spread. ``panic`` widens slippage tolerance so the
        circuit breaker clears the book immediately (unhedged flattening —
        both legs go out back-to-back without waiting on each other)."""
        slippage = self.cfg.slippage_tolerance_pct * (5 if panic else 1)
        for leg in plan_exit_legs(pos, self.chain, slippage):
            result = await self.executor.place_leg(leg)
            pos.fills.append(result)
            if not result.ok:
                log.error("close leg failed: %s", result.message)
        pos.closed = True
        log.info("CLOSED %s%s", pos.candidate.describe(), " [PANIC]" if panic else "")
