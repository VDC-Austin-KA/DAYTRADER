"""VWAP opening-range entry model for the live/paper daemon.

This is the same method as ``app/backtest/vwap.py`` -- deliberately the SAME
code, not a re-implementation. The daemon maintains a rolling intraday
minute-bar frame for the session and calls :func:`assess_entry` on each new
bar; the decision it gets is byte-for-byte what the backtest scored, so a
result confirmed offline is the result that trades. Any divergence between a
"backtested" rule and a "live" rule is how paper edges evaporate in
production; sharing one function removes that gap by construction.

It emits a side ONLY on the bar a fresh breakout occurs (the debounced rising
edge), so the daemon enters once per break, not every bar the condition holds.
Returns ``None`` when there is no fresh signal -- most bars.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..backtest import vwap


@dataclass
class EntryDecision:
    side: str | None                 # "call" | "put" | None
    reason: str
    readings: dict = field(default_factory=dict)

    def describe(self) -> str:
        if self.side is None:
            return f"no entry ({self.reason})"
        return f"{self.side.upper()} — {self.reason}"


def assess_entry(session_bars: pd.DataFrame, **kw) -> EntryDecision:
    """Decide whether the newest bar in ``session_bars`` is a fresh entry.

    ``session_bars`` is an OHLCV frame (columns Open/High/Low/Close/Volume,
    DatetimeIndex in ET) for the CURRENT session up to and including the bar
    just closed. Keyword args pass straight through to
    ``vwap.compute_vwap_signal`` (or_minutes, min_efficiency, ...).
    """
    if session_bars is None or len(session_bars) < 2:
        return EntryDecision(None, "insufficient bars this session")

    sig = vwap.compute_vwap_signal(session_bars, **kw)
    last = sig.iloc[-1]
    direction = last["direction"]
    readings = {
        "close": round(float(last["close"]), 2),
        "vwap": round(float(last["vwap"]), 2) if pd.notna(last["vwap"]) else None,
        "or_high": round(float(last["or_high"]), 2) if pd.notna(last["or_high"]) else None,
        "or_low": round(float(last["or_low"]), 2) if pd.notna(last["or_low"]) else None,
        "efficiency": round(float(last["efficiency"]), 3),
    }

    if last["surge"] < 50 or direction == "neutral":
        return EntryDecision(None, "no fresh VWAP/opening-range break", readings)

    if direction == "up":
        return EntryDecision(
            "call",
            f"break above OR high {readings['or_high']} over rising VWAP "
            f"{readings['vwap']} (eff {readings['efficiency']})",
            readings,
        )
    return EntryDecision(
        "put",
        f"break below OR low {readings['or_low']} under falling VWAP "
        f"{readings['vwap']} (eff {readings['efficiency']})",
        readings,
    )
