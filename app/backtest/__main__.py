"""Run the signal backtest:  python -m app.backtest [--refresh]

Tunes the surge threshold on the TRAIN period only, then scores the
untouched holdout once with the frozen winner.
"""
from __future__ import annotations

import logging
import sys

from . import engine

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("daytrader.backtest")

SYMBOLS = ["SPY", "QQQ", "NVDA", "AMD", "MU", "META", "TSLA", "INTC"]
START, END = "2025-01-01", "2026-07-17"
CUTOFF = "2026-03-01"          # everything from here is holdout

HORIZONS = [5, 15, 30, 60]     # minutes
THRESHOLDS = [50, 60, 70, 80]

# Round-trip cost an option must overcome before the signal pays. A 0-3 DTE
# contract at a 3-5% spread with ~0.5 delta needs roughly this much
# underlying move just to break even.
HURDLE_BPS = 25.0


def main() -> int:
    refresh = "--refresh" in sys.argv
    log.info("Loading %d symbols %s -> %s", len(SYMBOLS), START, END)
    frames = engine.build_frames(SYMBOLS, START, END, refresh=refresh)
    if not frames:
        log.error("No data. Is OpenD running?")
        return 1

    train, test = engine.split(frames, CUTOFF)
    log.info(
        "\nTRAIN %d symbols | HOLDOUT %d symbols (cutoff %s)",
        len(train), len(test), CUTOFF,
    )

    log.info("\n%s\nTRAIN (tuning here is allowed)\n%s", "=" * 78, "=" * 78)
    best, best_key = None, None
    for h in HORIZONS:
        for thr in THRESHOLDS:
            r = engine.evaluate(train, thr, h, "train")
            if r.n_signals < 200:
                continue
            log.info(r.summary())
            # Rank by t-stat: reward consistency, not a big mean off few trades.
            if best is None or r.t_stat > best.t_stat:
                best, best_key = r, (thr, h)

    if best is None:
        log.error("No parameter set produced enough signals.")
        return 1

    thr, h = best_key
    log.info("\nBest on train: threshold=%.0f horizon=%dm (t=%.2f)", thr, h, best.t_stat)

    log.info("\n%s\nHOLDOUT (scored ONCE, parameters frozen)\n%s", "=" * 78, "=" * 78)
    ho = engine.evaluate(test, thr, h, "holdout")
    log.info(ho.summary())

    log.info("\n%s\nVERDICT\n%s", "=" * 78, "=" * 78)
    log.info("Edge on holdout : %+.2f bps per signal", ho.edge_bps)
    log.info("Option hurdle   : %.2f bps (spread round-trip)", HURDLE_BPS)
    net = ho.edge_bps - HURDLE_BPS
    log.info("Net of costs    : %+.2f bps", net)
    if ho.n_signals < 100:
        log.info("--> TOO FEW HOLDOUT SIGNALS to conclude anything.")
    elif abs(ho.t_stat) < 2.0:
        log.info("--> NOT STATISTICALLY SIGNIFICANT (|t| < 2). Consistent with noise.")
    elif net <= 0:
        log.info("--> Directional edge does NOT cover option costs. Not tradeable as-is.")
    else:
        log.info("--> Edge survives costs on unseen data. Worth paper-trading next.")

    if ho.per_symbol:
        log.info("\nPer symbol (holdout):")
        for sym, s in sorted(
            ho.per_symbol.items(), key=lambda kv: -kv[1]["mean_bps"]
        ):
            log.info(
                "  %-6s n=%-6d hit=%5.1f%%  mean=%+7.2f bps",
                sym, s["n"], s["hit"] * 100, s["mean_bps"],
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
