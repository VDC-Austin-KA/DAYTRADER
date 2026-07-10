"""Intraday risk watchdog (2026 intraday framework compliance).

Runs on its own task every ``watchdog_interval_seconds`` and enforces, in
escalation order:

1. **Per-position hard stop** — a spread whose adverse mark-to-market
   consumes more than ``position_stop_pct_of_max_risk`` of its defined
   maximum risk is flattened immediately.
2. **Daily equity circuit breaker** — if account equity (from the broker's
   portfolio-margin endpoint, or the paper baseline plus open P&L) drops
   ``daily_equity_stop_pct`` below the session's starting equity, every
   position is cleared with unhedged panic-flattening orders and the bot
   is halted for the day.
3. **Margin-utilisation guard** — maintenance margin above
   ``margin_utilisation_max`` of equity triggers the same full flatten:
   never wait for the broker's own liquidation engine.

Marks come straight off the live WebSocket chain, so the stop reacts at
tick speed; broker equity/margin is polled each cycle via the async
executor bridge.
"""
from __future__ import annotations

import logging

from .chain import ChainStore
from .config import SpreadsConfig
from .execution import SpreadRouter
from .models import SpreadPosition

log = logging.getLogger("daytrader.spreads.watchdog")


class RiskWatchdog:
    def __init__(self, cfg: SpreadsConfig, chain: ChainStore, router: SpreadRouter) -> None:
        self.cfg = cfg
        self.chain = chain
        self.router = router
        self.positions: list[SpreadPosition] = []
        self.session_start_equity: float | None = None
        self.halted = False
        self.halt_reason = ""

    # ------------------------------------------------------------------ #
    # Marking
    # ------------------------------------------------------------------ #
    def current_net_mid(self, pos: SpreadPosition) -> float | None:
        short = self.chain.quote(pos.candidate.short_leg.contract)
        long_ = self.chain.quote(pos.candidate.long_leg.contract)
        if short is None or long_ is None or short.mid <= 0 or long_.mid <= 0:
            return None
        return short.mid - long_.mid

    def open_positions(self) -> list[SpreadPosition]:
        return [p for p in self.positions if not p.closed]

    def _open_unrealized_pnl(self) -> float:
        total = 0.0
        for pos in self.open_positions():
            mid = self.current_net_mid(pos)
            if mid is not None:
                # entry - current, signed the direction we hold the spread.
                total += (pos.entry_price - mid) * 100.0 * pos.candidate.quantity
        return total

    # ------------------------------------------------------------------ #
    # One watchdog cycle
    # ------------------------------------------------------------------ #
    async def check(self) -> None:
        if self.halted:
            return

        # --- per-position hard stop ----------------------------------- #
        for pos in self.open_positions():
            mid = self.current_net_mid(pos)
            if mid is None:
                continue
            max_risk = pos.candidate.max_risk_per_spread * pos.candidate.quantity
            loss = pos.unrealized_loss(mid)
            if max_risk > 0 and loss >= self.cfg.position_stop_pct_of_max_risk * max_risk:
                log.warning(
                    "STOP-LOSS: %s losing $%.0f of $%.0f max risk (%.0f%% cap)",
                    pos.candidate.describe(), loss, max_risk,
                    self.cfg.position_stop_pct_of_max_risk * 100,
                )
                await self.router.close_spread(pos, panic=True)

        # --- account-level breakers ------------------------------------ #
        snapshot = await self.router.executor.account_snapshot()
        equity = snapshot.get("equity") or (
            self.cfg.paper_starting_equity + self._open_unrealized_pnl()
        )
        if self.session_start_equity is None:
            self.session_start_equity = equity
            log.info("watchdog armed: session equity baseline $%.2f", equity)

        drawdown_floor = self.session_start_equity * (1 - self.cfg.daily_equity_stop_pct)
        if equity < drawdown_floor:
            await self._trip(
                f"daily equity circuit breaker: ${equity:,.2f} < floor "
                f"${drawdown_floor:,.2f} "
                f"(-{self.cfg.daily_equity_stop_pct * 100:.1f}% of start)"
            )
            return

        margin = snapshot.get("maintenance_margin", 0.0)
        if equity > 0 and margin / equity > self.cfg.margin_utilisation_max:
            await self._trip(
                f"margin guard: maintenance ${margin:,.0f} is "
                f"{margin / equity:.0%} of equity "
                f"(cap {self.cfg.margin_utilisation_max:.0%})"
            )

    async def _trip(self, reason: str) -> None:
        """Circuit breaker: clear the whole book NOW, then halt entries."""
        self.halted = True
        self.halt_reason = reason
        log.critical("CIRCUIT BREAKER TRIPPED — flattening book: %s", reason)
        for pos in self.open_positions():
            await self.router.close_spread(pos, panic=True)
        log.critical("book flat; no further entries this session")
