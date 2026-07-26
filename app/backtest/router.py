"""Regime router: continuation in trend, reversion at exhaustion, never both.

THE POINT
---------
Continuation (confluence.py) fires on LOCAL efficiency above its floor.
Reversion (reversion.py) fires on SESSION-SCALE efficiency below its ceiling.

Those are different measurements, so -- deliberately -- the two models are NOT
mutually exclusive by arithmetic. An earlier draft of this router claimed they
were; that was wrong, and worth stating plainly because the reason is the
interesting part. Guarding reversion on local efficiency would have vetoed
every blow-off spike (a vertical move is locally efficient by definition),
which is the best fade there is. Fixing that cost the tidy exclusivity proof.

What actually separates them in practice is MOMENTUM HEALTH, not efficiency:
continuation additionally requires RSI inside a healthy band, while reversion
requires statistical extension plus exhaustion -- which normally puts RSI
outside that band. A bar that is simultaneously a healthy-momentum breakout
and a climactic exhaustion is close to a contradiction, but it is not
forbidden, so this router arbitrates explicitly instead of assuming.

Precedence: continuation wins, because it has the longer validation history.
``coverage()`` reports how often the overlap actually occurs; if that number
is not ~0, the thresholds need re-separating rather than trusting precedence.

What the pairing buys, relative to continuation alone:

  * COVERAGE. The old strategy stood down in ranging tape -- the majority of
    an average session, and exactly where highs and lows repeat. The router
    has an applicable model in both states instead of one.
  * ENTRY PRICE. A breakout enters after the move has extended; a fade enters
    AT the extreme, against a defined target. On 0DTE, where spread and theta
    are the whole toll, entry basis is most of the expectancy.

HONEST FAILURE MODE
-------------------
The efficiency split is a hypothesis, not a fact: it is the one regime.py
raised and never tested to completion. If the router's blended walk-forward
is no better than continuation alone, then the split does not carry
information and the reversion half should be switched off -- which is exactly
what run_router.py reports. Coverage is only worth having if the added trades
are not negative.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import confluence, reversion


def compute_routed_signal(
    df: pd.DataFrame,
    min_efficiency: float = 0.55,
    max_efficiency: float = 0.55,
    enable_continuation: bool = True,
    enable_reversion: bool = True,
    continuation_kw: dict | None = None,
    reversion_kw: dict | None = None,
) -> pd.DataFrame:
    """Route each bar to the model its regime supports.

    Returns the engine-compatible schema plus a ``model`` column
    ("continuation" | "reversion" | "") so results can be attributed per
    model -- a blended number that hides which half is carrying it would be
    useless for deciding what to keep.
    """
    ckw = dict(continuation_kw or {})
    rkw = dict(reversion_kw or {})
    ckw.setdefault("min_efficiency", min_efficiency)
    rkw.setdefault("max_efficiency", max_efficiency)

    if enable_continuation:
        cont = confluence.compute_confluence_signal(df, **ckw)
    else:
        cont = None
    if enable_reversion:
        rev = reversion.compute_reversion_signal(df, **rkw)
    else:
        rev = None

    base = cont if cont is not None else rev
    if base is None:
        raise ValueError("at least one model must be enabled")

    direction = pd.Series("neutral", index=df.index)
    surge = pd.Series(0.0, index=df.index)
    model = pd.Series("", index=df.index)

    if cont is not None:
        fire = cont["surge"] >= 50
        direction[fire] = cont.loc[fire, "direction"]
        surge[fire] = 100.0
        model[fire] = "continuation"

    if rev is not None:
        raw = rev["surge"] >= 50
        # Explicit arbitration: continuation wins, having the longer
        # validation history. Overlap is expected to be rare, not impossible
        # (see the module docstring) -- so it is COUNTED rather than assumed
        # away, and surfaced by coverage() as `contended`.
        fire = raw & (model == "")
        out_contended = int((raw & (model != "")).sum())
        direction[fire] = rev.loc[fire, "direction"]
        surge[fire] = 100.0
        model[fire] = "reversion"

    out = pd.DataFrame({
        "close": df["Close"], "surge": surge, "direction": direction,
        "model": model,
    })
    # Attribute overlap so it can never hide: a nonzero total here means the
    # regimes are contending and the thresholds want re-separating.
    out.attrs["contended"] = out_contended if rev is not None else 0
    for name, frame in (("cont", cont), ("rev", rev)):
        if frame is not None and "efficiency" in frame:
            out[f"{name}_efficiency"] = frame["efficiency"]
    if rev is not None:
        out["band_z"] = rev["band_z"]
        out["vwap"] = rev["vwap"]
    return out


def split_by_model(frames: dict[str, pd.DataFrame], model: str) -> dict:
    """Mask a routed frame down to one model's decisions, for attribution."""
    out = {}
    for sym, f in frames.items():
        g = f.copy()
        keep = g["model"] == model
        g.loc[~keep, "surge"] = 0.0
        g.loc[~keep, "direction"] = "neutral"
        out[sym] = g
    return out


def coverage(frames: dict[str, pd.DataFrame]) -> dict:
    """How many decisions each model contributed -- the coverage claim, checked."""
    counts: dict[str, int] = {}
    contended = 0
    for f in frames.values():
        for m, n in f.loc[f["surge"] >= 50, "model"].value_counts().items():
            counts[m] = counts.get(m, 0) + int(n)
        contended += int(f.attrs.get("contended", 0))
    total = sum(counts.values())
    return {"total": total, **counts, "contended": contended,
            "reversion_share": round(counts.get("reversion", 0) / total, 3)
            if total else 0.0}
