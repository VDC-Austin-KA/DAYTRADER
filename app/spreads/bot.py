"""SpreadBot — wires ingestion, scanning, execution and the watchdog.

Concurrent tasks on one event loop:

    stream      moomoo OpenD push (QUOTE + ORDER_BOOK) -> bounded queue
    consumer    queue -> ChainStore updates
    discovery   periodic chain scan + spot refresh + NBBO subscriptions
    scanner     IV-rank regime -> candidate -> guarded entry
    watchdog    stop-losses, equity/margin circuit breakers
"""
from __future__ import annotations

import asyncio
import logging
import time

from .chain import ChainStore
from .config import SpreadsConfig, get_config
from .execution import SpreadRouter, build_executor
from .ingest import ContractDiscovery, MoomooOptionsStream
from .ivrank import IVRankTracker
from .scanner import SpreadScanner
from .watchdog import RiskWatchdog

log = logging.getLogger("daytrader.spreads.bot")


class SpreadBot:
    def __init__(self, cfg: SpreadsConfig | None = None) -> None:
        self.cfg = cfg or get_config()
        self.chain = ChainStore(self.cfg.underlying)
        self.stream = MoomooOptionsStream(self.cfg, self.chain)
        self.discovery = ContractDiscovery(self.cfg, self.chain)
        self.iv_rank = IVRankTracker(self.cfg.iv_history_path, self.cfg.iv_rank_window_days)
        self.scanner = SpreadScanner(self.cfg)
        self.executor = build_executor(self.cfg)
        self.router = SpreadRouter(self.cfg, self.chain, self.executor)
        self.watchdog = RiskWatchdog(self.cfg, self.chain, self.router)
        self._last_entry_ts = 0.0

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        if not self.cfg.moomoo_opend_host:
            raise SystemExit(
                "MOOMOO_OPEND_HOST is not set — the options data feed needs a "
                "running, reachable OpenD gateway. Add it to .env / Railway "
                "service variables."
            )
        log.info(
            "spread bot starting: %s, %d-%d DTE, mode=%s",
            self.cfg.underlying, self.cfg.min_dte, self.cfg.max_dte, self.cfg.trade_mode,
        )
        tasks = [
            asyncio.create_task(self.stream.run(), name="moomoo-stream"),
            asyncio.create_task(self.stream.run_consumer(), name="moomoo-consumer"),
            asyncio.create_task(self.discovery.run(self.stream), name="discovery"),
            asyncio.create_task(self._scan_loop(), name="scanner"),
            asyncio.create_task(self._watchdog_loop(), name="watchdog"),
        ]
        try:
            # First crash/exit of any task brings the bot down loudly rather
            # than trading on with a dead pipeline.
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if exc:
                    raise exc
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.executor.close()
            log.info("spread bot stopped")

    # ------------------------------------------------------------------ #
    async def _scan_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.scan_interval_seconds)
            try:
                await self._scan_once()
            except Exception:
                log.exception("scan cycle failed")

    async def _scan_once(self) -> None:
        if self.watchdog.halted:
            return
        spot = self.discovery.spot
        if spot <= 0:
            return
        atm_iv = self.chain.atm_iv(spot)
        if atm_iv is None:
            return
        self.iv_rank.observe(atm_iv)
        rank = self.iv_rank.rank(atm_iv)
        if rank is None:
            log.debug("IV-rank window still warming up (ATM IV %.1f%%)", atm_iv * 100)
            return

        if len(self.watchdog.open_positions()) >= self.cfg.max_open_spreads:
            return
        if time.time() - self._last_entry_ts < self.cfg.entry_cooldown_seconds:
            return

        cand = self.scanner.scan(self.chain, rank, spot)
        if cand is None:
            return
        log.info("candidate: %s", cand.describe())
        pos = await self.router.open_spread(cand)
        if pos is not None:
            self.watchdog.positions.append(pos)
            self._last_entry_ts = time.time()

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.watchdog_interval_seconds)
            try:
                await self.watchdog.check()
            except Exception:
                log.exception("watchdog cycle failed")
