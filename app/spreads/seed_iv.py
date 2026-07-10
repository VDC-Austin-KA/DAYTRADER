"""Pre-seed the IV-rank history from CBOE short-dated volatility indices.

The IV-rank window normally needs ~an hour of live ATM-IV samples before
the bot makes its first regime call, and a fresh deployment starts cold.
This module warms it up from free data: ``^VIX1D`` (CBOE 1-day SPX implied
volatility) is the closest public proxy for the 0-3 DTE ATM IV the bot
samples off the live chain, with ``^VIX9D``/``^VIX`` as fallbacks. Daily
closes over the rank window are written in the tracker's JSON format.

Run manually (or from a deploy hook) with::

    python -m app.spreads.seed_iv            # writes SPREADS_IV_HISTORY_PATH
    python -m app.spreads.seed_iv path.json  # explicit target

Once the bot is running, live chain samples accumulate on top and the
seed ages out of the rolling window naturally.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import requests

from .config import get_config

log = logging.getLogger("daytrader.spreads.seed_iv")

# Best proxy first: 1-day SPX IV, then 9-day, then 30-day.
_SYMBOLS = ("^VIX1D", "^VIX9D", "^VIX")
_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
_MIN_SAMPLES = 12  # IVRankTracker refuses to rank on fewer points


def fetch_vol_series(window_days: int) -> tuple[str, list[tuple[float, float]]]:
    """Daily (epoch_s, iv_decimal) samples for the first symbol that works."""
    cutoff = time.time() - window_days * 86400
    last_error: Exception | None = None
    for sym in _SYMBOLS:
        try:
            resp = requests.get(
                _CHART_URL.format(sym=sym),
                params={"range": f"{window_days + 15}d", "interval": "1d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json()["chart"]["result"][0]
            closes = result["indicators"]["quote"][0]["close"]
            samples = [
                (float(t), round(c / 100.0, 6))
                for t, c in zip(result["timestamp"], closes)
                if c is not None and t >= cutoff
            ]
            if len(samples) >= _MIN_SAMPLES:
                return sym, samples
            log.warning("%s returned only %d usable points", sym, len(samples))
        except Exception as exc:  # try the next proxy index
            last_error = exc
            log.warning("fetch %s failed: %s", sym, exc)
    raise RuntimeError(f"no volatility index reachable (last error: {last_error})")


def seed(path: str | None = None, window_days: int | None = None) -> Path:
    cfg = get_config()
    target = Path(path or cfg.iv_history_path)
    sym, samples = fetch_vol_series(window_days or cfg.iv_rank_window_days)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(samples))
    log.info(
        "seeded %s with %d samples from %s (latest IV %.1f%%)",
        target, len(samples), sym, samples[-1][1] * 100,
    )
    return target


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s: %(message)s")
    seed(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    main()
