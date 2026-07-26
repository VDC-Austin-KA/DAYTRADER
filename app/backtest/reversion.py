"""Exhaustion reversion: locate actual tops and bottoms, not just extension.

WHY THIS EXISTS -- THE GAP IN THE CURRENT STRATEGY
--------------------------------------------------
Everything shipped so far (vwap.py, confluence.py) is a CONTINUATION model.
Every entry requires high Kaufman efficiency, i.e. a clean trend. Two
consequences follow, and both cost money on a fluctuating tape:

  1. A breakout BUYS THE TOP by construction. It enters after the move has
     already extended, which is the worst price of the leg. It is structurally
     incapable of catching a high or a low -- catching those is the opposite
     trade.
  2. It STANDS DOWN in ranging tape, which is precisely where highs and lows
     are identifiable and repeatable. regime.py hypothesised that reversion
     should work where efficiency is LOW; that half was never built. This is
     that half.

The two models are mutually exclusive BY CONSTRUCTION: continuation demands
efficiency above its floor, this demands efficiency below its ceiling. They
cannot both fire on the same bar, so they compose into one always-applicable
strategy rather than two rules fighting each other (see router.py).

FADING IS DANGEROUS -- WHAT MAKES IT DISCIPLINED
------------------------------------------------
"Price went up a lot, so it must be a top" is how accounts die on trend days.
Extension alone is NOT a signal; it is a precondition. A top is only a top
when the move is also demonstrably running out of buyers. So an entry needs
statistical extension PLUS independent evidence of exhaustion:

  * EXTENSION -- price at >= ``min_z`` volume-weighted standard deviations
    from session VWAP. The VWAP sigma-band is the honest measure of "far",
    because it is scaled to the session's own realised dispersion rather than
    to a fixed number of points.
  * DIVERGENCE -- price makes a new N-bar extreme but RSI does NOT. The
    canonical exhaustion tell: a higher high on weaker momentum means the
    marginal buyer is gone.
  * CLIMAX VOLUME -- a volume spike into the extreme. Blow-off/capitulation
    prints are where the last participants transact.
  * REJECTION WICK -- the bar closes well off its own extreme. The market
    probed higher, found nothing, and was pushed back. This is the turn
    beginning rather than being predicted.

Requiring ``min_confirms`` of the latter three, on top of extension and the
trend-day guard, is what separates a top-finder from a trend-fighter.

NO LOOKAHEAD
------------
Session VWAP and its sigma are within-day cumulatives. RSI, rolling extremes,
and volume averages are trailing windows. Wick geometry is same-bar only.
Test-pinned by truncation invariance.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import confluence, vwap as vwap_mod


def session_vwap_bands(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Session VWAP and its volume-weighted standard deviation, cumulative.

    sigma^2 = E_v[tp^2] - vwap^2, both terms running cumulatives within the
    session, so every bar sees only its own past.
    """
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    tp = (high + low + close) / 3.0
    day = pd.Index([str(d) for d in df.index.date])

    vwap = pd.Series(index=df.index, dtype=float)
    sigma = pd.Series(index=df.index, dtype=float)
    for _, idx in df.groupby(day).groups.items():
        v = vol.loc[idx]
        cv = v.cumsum().clip(lower=1e-9)
        m1 = (tp.loc[idx] * v).cumsum() / cv
        m2 = (tp.loc[idx] ** 2 * v).cumsum() / cv
        vwap.loc[idx] = m1.to_numpy()
        sigma.loc[idx] = np.sqrt(np.maximum(m2 - m1 ** 2, 0.0).to_numpy())
    return vwap, sigma


def compute_reversion_signal(
    df: pd.DataFrame,
    min_z: float = 2.0,
    extreme_window: int = 20,
    rsi_period: int = 14,
    divergence_margin: float = 2.0,
    climax_mult: float = 1.8,
    vol_window: int = 20,
    wick_frac: float = 0.45,
    min_confirms: int = 2,
    max_efficiency: float = 0.55,
    eff_window: int = 20,
    trend_window: int = 90,
    warmup_minutes: int = 30,
) -> pd.DataFrame:
    """Fade statistically extended moves that show exhaustion.

    Emits the engine-compatible schema (``close``/``surge``/``direction``).
    ``direction`` is the direction of the EXPECTED MOVE, not of the extension:
    a top produces "down" (buy puts), a bottom produces "up" (buy calls).
    """
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    minutes = df.index.hour * 60 + df.index.minute
    mins = np.asarray(minutes)
    from_open = mins - vwap_mod.SESSION_OPEN_ET
    day = pd.Index([str(d) for d in df.index.date])

    vwap, sigma = session_vwap_bands(df)
    band_z = (close - vwap) / sigma.clip(lower=1e-9)
    r = confluence.rsi(close, rsi_period)
    eff = vwap_mod._kaufman_efficiency(close, eff_window)
    # Session-scale efficiency: the trend-day measure. See the guard below --
    # this MUST be a longer window than the local one, or the guard rejects
    # the very spikes it should be taking.
    trend_eff = vwap_mod._kaufman_efficiency(close, trend_window)

    # --- Extension: far from fair value in the session's own sigma units.
    stretched_up = band_z >= min_z
    stretched_dn = band_z <= -min_z

    # --- Divergence: new price extreme, momentum not confirming.
    px_hi = close >= close.rolling(extreme_window).max()
    px_lo = close <= close.rolling(extreme_window).min()
    div_top = px_hi & (r < r.rolling(extreme_window).max() - divergence_margin)
    div_bot = px_lo & (r > r.rolling(extreme_window).min() + divergence_margin)

    # --- Climax volume: the blow-off / capitulation print.
    vol_avg = vol.rolling(vol_window).mean().clip(lower=1e-9)
    climax = vol >= climax_mult * vol_avg

    # --- Rejection wick: closed well off the bar's own extreme.
    rng = (high - low).clip(lower=1e-9)
    rej_top = (high - close) / rng >= wick_frac
    rej_bot = (close - low) / rng >= wick_frac

    top_confirms = (div_top.astype(int) + climax.astype(int)
                    + rej_top.astype(int))
    bot_confirms = (div_bot.astype(int) + climax.astype(int)
                    + rej_bot.astype(int))

    # --- Trend-day guard, measured at SESSION scale, not locally.
    #
    # This distinction is the whole difference between a top-finder and a
    # trend-fighter, and getting it wrong breaks the model in either
    # direction:
    #
    #   * A vertical blow-off is, by definition, locally efficient -- it is a
    #     straight line up. Guarding on LOCAL efficiency therefore vetoes
    #     exactly the climactic spikes that are the best fades. (This is not
    #     hypothetical: the first cut of this module did precisely that, and
    #     the blow-off test caught it.)
    #   * A genuine trend day is efficient at BOTH scales -- it grinds one way
    #     all session -- and must never be faded at any extension.
    #
    # So the spike-out-of-a-range and the trend day are separated by the LONG
    # window: both are locally efficient, only the trend day is efficient
    # across the session. Guard on that.
    ranging = trend_eff <= max_efficiency

    tradeable = (
        ranging
        & (from_open >= warmup_minutes)          # sigma needs a session base
        & (mins < vwap_mod.LAST_SIGNAL_ET)
        & band_z.notna() & r.notna()
    )

    fade_top = tradeable & stretched_up & (top_confirms >= min_confirms)
    fade_bot = tradeable & stretched_dn & (bot_confirms >= min_confirms)

    # Debounce per session to the rising edge: one decision per exhaustion,
    # not one per bar the condition happens to persist.
    same_day = day == pd.Index(day).to_series().shift(1).to_numpy()
    top_edge = fade_top & ~(fade_top.shift(1).fillna(False) & same_day)
    bot_edge = fade_bot & ~(fade_bot.shift(1).fillna(False) & same_day)

    direction = pd.Series("neutral", index=df.index)
    direction[top_edge] = "down"        # fade the high -> buy puts
    direction[bot_edge] = "up"          # fade the low  -> buy calls
    surge = pd.Series(np.where(top_edge | bot_edge, 100.0, 0.0), index=df.index)

    return pd.DataFrame({
        "close": close, "surge": surge, "direction": direction,
        "vwap": vwap, "sigma": sigma, "band_z": band_z, "rsi": r,
        "efficiency": eff, "trend_efficiency": trend_eff,
        "confirms": np.where(top_edge, top_confirms,
                             np.where(bot_edge, bot_confirms, 0)),
    })


def mean_target(row_vwap: float, entry_px: float, capture: float = 0.75) -> float:
    """Underlying price to take profit at on a fade: most of the way back.

    A reversion trade has a DEFINED destination -- the mean -- unlike a
    breakout, which is open-ended. Trailing a fade gives the edge back,
    because the move terminates at VWAP instead of extending through it.
    Targeting ``capture`` of the distance leaves room for the common case
    where price stalls just short of the mean.
    """
    return entry_px + (row_vwap - entry_px) * capture
