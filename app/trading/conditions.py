"""Real-time market-condition gate for 0DTE momentum entries.

WHY THIS EXISTS
---------------
This repo has already shown, out of sample, that a raw burst carries no
directional edge (Surge hit 48.5%). The regime study
(``app/backtest/regime.py``) located WHERE the losses live: low-efficiency,
two-sided tape -- chop that trips a burst and then reverses, handing the
position straight to 0DTE theta. Buying a call into a decisive downtrend (or
a put into a rip) is the other recurring loser: a burst *against* the drift
is usually the reversal that traps a late entry.

So before every entry the autonomous daemon consults this gate. It does NOT
predict direction -- nothing computable from price alone does that here -- it
REFUSES the conditions that historically bled. That is the honest way to
raise a win rate with no directional edge: trade less often, only in tape
that is cleanly trending and in agreement with the burst. Fewer entries,
better entries.

Pure and synchronous: it takes the ``(timestamp, price)`` samples the daemon
already buffers and returns a go/no-go plus the readings behind it, so the
whole gate is unit-testable without a live gateway.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Assessment:
    """The gate's verdict for one proposed entry, with its readings."""
    tradeable: bool
    efficiency: float       # Kaufman ratio over the window, 0..1 (1 = clean trend)
    trend_bps: float        # net drift over the window, signed basis points
    aligned: bool           # burst direction agrees with the drift
    n: int                  # samples the reading is built from
    reasons: list[str] = field(default_factory=list)

    def describe(self) -> str:
        verdict = "OK" if self.tradeable else "SKIP"
        why = "; ".join(self.reasons) or "clean trending tape, aligned"
        return (
            f"{verdict}: efficiency={self.efficiency:.2f} "
            f"trend={self.trend_bps:+.1f}bps aligned={self.aligned} ({why})"
        )


def kaufman_efficiency(prices: list[float]) -> float:
    """Net move / total path travelled over the series. 0 = pure chop.

    The Kaufman efficiency ratio is exactly the whipsaw gauge the movers/
    backtest code already uses, computed here on the live spot path: a value
    near 1 means the tape went somewhere in a straight line; near 0 means it
    thrashed and ended up where it started.
    """
    if len(prices) < 2:
        return 0.0
    net = abs(prices[-1] - prices[0])
    path = sum(abs(prices[i] - prices[i - 1]) for i in range(1, len(prices)))
    if path <= 0:
        return 0.0
    return net / path


def assess(
    samples: list[tuple[float, float]],
    direction: str,
    *,
    min_samples: int = 8,
    min_efficiency: float = 0.35,
    oppose_trend_bps: float = 12.0,
) -> Assessment:
    """Score the recent spot path for a proposed ``direction`` ("up"|"down").

    Two gates, both drawn from where this repo's own backtests found the
    losses:

    * CHOP -- efficiency below ``min_efficiency`` means the burst fired inside
      two-sided noise; a reversal into theta is the base case, so refuse.
    * FIGHTING THE DRIFT -- a call while the window's drift is below
      ``-oppose_trend_bps`` (or a put while it is above ``+oppose_trend_bps``)
      is a burst against a decisive trend; refuse it.

    A near-flat drift is allowed: a burst is itself the start of a move, so we
    only block entries that oppose a *decisive* one, not every mild lean.
    """
    prices = [p for _, p in samples if p and p > 0]
    n = len(prices)
    if n < min_samples:
        return Assessment(
            False, 0.0, 0.0, False, n,
            [f"only {n} samples (<{min_samples}); not enough tape yet"],
        )

    eff = kaufman_efficiency(prices)
    trend_bps = (prices[-1] / prices[0] - 1.0) * 10_000
    reasons: list[str] = []

    chop_ok = eff >= min_efficiency
    if not chop_ok:
        reasons.append(f"chop: efficiency {eff:.2f} < {min_efficiency:.2f}")

    aligned = True
    if direction == "up" and trend_bps <= -oppose_trend_bps:
        aligned = False
        reasons.append(f"call into downtrend ({trend_bps:+.1f}bps)")
    elif direction == "down" and trend_bps >= oppose_trend_bps:
        aligned = False
        reasons.append(f"put into uptrend ({trend_bps:+.1f}bps)")

    return Assessment(chop_ok and aligned, eff, trend_bps, aligned, n, reasons)
