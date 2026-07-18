"""Historical bar loader for backtests, cached to disk.

moomoo serves paged intraday klines through OpenD. Pulling a year of
1-minute bars for a dozen symbols is thousands of round trips, so every
symbol/timeframe is cached as a parquet file and re-read on later runs.
Delete ``backtest_cache/`` to force a refetch.

Only the UNDERLYING is available here. Historical option quotes need a
Polygon options entitlement (the flat-file archive currently 403s), so
nothing in this package can price an option after the fact -- see the
docstring in ``engine.py`` for why that bounds what a backtest can claim.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd

log = logging.getLogger("daytrader.backtest.data")

CACHE_DIR = Path("backtest_cache")
# OpenD rejects bursts; ~3 req/s is comfortably under the observed limit.
_PAGE_PAUSE = 0.35
_MAX_PAGES = 400


def _ctx():
    from moomoo import OpenQuoteContext, SysConfig

    from ..config import settings

    SysConfig.set_all_thread_daemon(True)
    return OpenQuoteContext(
        host=settings.moomoo_opend_host or "127.0.0.1",
        port=settings.moomoo_opend_port,
    )


def load_minute_bars(
    symbol: str,
    start: str,
    end: str,
    ktype: str = "K_1M",
    refresh: bool = False,
) -> pd.DataFrame:
    """Minute OHLCV for one symbol, cached. Empty frame if unavailable."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache = CACHE_DIR / f"{symbol}_{ktype}_{start}_{end}.parquet"
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)

    from moomoo import RET_OK, KLType

    ctx = _ctx()
    frames: list[pd.DataFrame] = []
    page_key = None
    try:
        for page in range(_MAX_PAGES):
            ret, df, page_key = ctx.request_history_kline(
                f"US.{symbol}" if not symbol.startswith("US.") else symbol,
                start=start, end=end, ktype=getattr(KLType, ktype),
                max_count=1000, page_req_key=page_key,
            )
            if ret != RET_OK:
                log.warning("%s page %d failed: %s", symbol, page, df)
                break
            if len(df):
                frames.append(df)
            if page_key is None:
                break
            time.sleep(_PAGE_PAUSE)
    finally:
        ctx.close()

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    out["time_key"] = pd.to_datetime(out["time_key"])
    out = (
        out.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        })
        .drop_duplicates(subset="time_key")
        .set_index("time_key")
        .sort_index()[["Open", "High", "Low", "Close", "Volume"]]
        .astype(float)
    )
    out.to_parquet(cache)
    log.info("%s: %d bars %s -> %s", symbol, len(out), out.index[0], out.index[-1])
    return out
