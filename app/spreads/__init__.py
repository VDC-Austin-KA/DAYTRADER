"""Intraday options vertical-spread bot (0-3 DTE).

Asynchronous pipeline:

    moomoo OpenD push (QUOTE + ORDER_BOOK) ──> asyncio.Queue ──> ChainStore (numpy)
                                                        │
    ContractDiscovery (chain scan + spot) ──────────────┤
                                                        ▼
    IVRankTracker ──> SpreadScanner ──> MoomooSpreadExecutor
                                                        │
    RiskWatchdog (margin / stop-loss / equity circuit breaker)

Run standalone with ``python -m app.spreads``. Everything is configured
through environment variables (see ``app/spreads/config.py`` and
``.env.example``); no credentials live in code.
"""
from .bot import SpreadBot

__all__ = ["SpreadBot"]
