"""Offline tests for the movers scan: Surge Score, wing picks, headlines."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.trading import movers


def make_history(
    days: int = 260,
    base: float = 100.0,
    daily_vol: float = 0.01,
    squeeze_tail: int = 0,
    burst_last_day: float = 0.0,
    volume_spike: float = 1.0,
    seed: int = 7,
) -> pd.DataFrame:
    """Synthetic OHLCV: optional late-period volatility squeeze and a
    final-day burst with a volume spike."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, daily_vol, days)
    if squeeze_tail:
        rets[-squeeze_tail:] = rng.normal(0, daily_vol * 0.15, squeeze_tail)
    if burst_last_day:
        rets[-1] = burst_last_day
    close = base * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, daily_vol / 2, days)))
    low = close * (1 - np.abs(rng.normal(0, daily_vol / 2, days)))
    volume = np.full(days, 1e6)
    volume[-1] *= volume_spike
    idx = pd.bdate_range(end="2026-07-10", periods=days)
    return pd.DataFrame(
        {"Open": close, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=idx,
    )


def patch_data(monkeypatch, df: pd.DataFrame, quote: float | None = None):
    monkeypatch.setattr(movers.md, "get_history", lambda sym, years=1: df)
    monkeypatch.setattr(
        movers.md, "get_quote", lambda sym: quote or float(df["Close"].iloc[-1])
    )


def test_squeezed_coil_scores_higher_than_normal_tape(monkeypatch):
    quiet = make_history(squeeze_tail=25)
    patch_data(monkeypatch, quiet)
    squeezed = movers.compute_surge("TEST")

    normal = make_history()
    patch_data(monkeypatch, normal)
    baseline = movers.compute_surge("TEST")

    assert squeezed is not None and baseline is not None
    assert squeezed.squeeze > baseline.squeeze
    assert squeezed.surge > baseline.surge


def test_burst_with_volume_lifts_surge_and_sets_direction(monkeypatch):
    df = make_history(burst_last_day=0.05, volume_spike=4.0)
    patch_data(monkeypatch, df)
    r = movers.compute_surge("TEST")
    assert r is not None
    assert r.burst > 0.5
    assert r.volume_ratio > 3.0
    assert r.direction == "up"
    assert r.day_change_pct == pytest.approx(5.0, abs=1.5)


def test_downside_burst_leans_down(monkeypatch):
    df = make_history(burst_last_day=-0.06, volume_spike=4.0)
    patch_data(monkeypatch, df)
    r = movers.compute_surge("TEST")
    assert r is not None and r.direction == "down"


def test_insufficient_history_returns_none(monkeypatch):
    patch_data(monkeypatch, make_history(days=30))
    assert movers.compute_surge("TEST") is None


# --------------------------------------------------------------------------- #
# Wing selection (the "make it free" leg)
# --------------------------------------------------------------------------- #
def _contract(strike, mid, otype="call", expiry="2026-07-11"):
    return {
        "symbol": "TEST", "option_type": otype, "strike": strike, "mid": mid,
        "expiry": expiry, "contract_symbol": f"T{strike}", "cost": mid * 100,
        "dte": 1, "prob_profit": 0.4, "success": 0.45, "potential_return": 1.0,
    }


def test_pick_wing_prefers_nearest_otm_strike_in_premium_band():
    entry = _contract(100, 2.00)
    slate = [
        entry,
        _contract(105, 1.20),   # in band, nearest -> winner
        _contract(110, 0.90),   # in band but further
        _contract(115, 0.30),   # too cheap (below 40% of entry)
        _contract(95, 3.00),    # ITM side — ineligible
    ]
    wing = movers._pick_wing(entry, slate)
    assert wing is not None and wing["strike"] == 105


def test_pick_wing_put_side_goes_lower():
    entry = _contract(100, 2.00, otype="put")
    slate = [entry, _contract(95, 1.10, otype="put"), _contract(105, 1.10, otype="put")]
    wing = movers._pick_wing(entry, slate)
    assert wing is not None and wing["strike"] == 95


def test_pick_wing_none_when_nothing_qualifies():
    entry = _contract(100, 2.00)
    assert movers._pick_wing(entry, [entry, _contract(105, 0.10)]) is None


# --------------------------------------------------------------------------- #
# Headlines
# --------------------------------------------------------------------------- #
def _reading(sym, surge, direction="up", whipsaw=False):
    return movers.SurgeReading(
        symbol=sym, price=100.0, surge=surge, squeeze=0.5, burst=0.5,
        momentum_z=1.0, direction=direction, whipsaw=whipsaw,
        day_change_pct=2.0, volume_ratio=2.0,
    )


def test_headlines_flag_score_jumps_and_hot_names():
    movers._prev_scores = {"NVDA": 40.0, "AMD": 70.0}
    items = movers._make_headlines([_reading("NVDA", 62.0), _reading("AMD", 71.0)], [])
    joined = " ".join(items)
    assert "NVDA" in joined and "40 → 62" in joined   # jump headline
    assert not any("AMD surge score jumped" in i for i in items)  # +1 is noise
    # State rolls forward for the next diff.
    assert movers._prev_scores["NVDA"] == 62.0


def test_headlines_feature_top_plays_and_dedupe():
    movers._prev_scores = {}
    reading = _reading("MU", 80.0, whipsaw=True)
    play = movers._build_play(reading, _contract(100, 1.50), [_contract(100, 1.50)])
    items = movers._make_headlines([reading], [play, play])
    assert any("🔥 MU" in i for i in items)
    assert sum("Top play: MU" in i for i in items) == 1  # deduped
    assert any("free-spread setup" in i for i in items)


# --------------------------------------------------------------------------- #
# scan_universe wiring (md + chain scan mocked)
# --------------------------------------------------------------------------- #
def test_scan_universe_ranks_across_symbols_and_caches(monkeypatch):
    movers._scan_cache.update(ts=0.0, result=None)
    movers._prev_scores = {}
    monkeypatch.setattr(
        movers.settings, "movers_watchlist", ["HOT", "COLD"], raising=False
    )
    readings = {
        "HOT": _reading("HOT", 85.0, whipsaw=True),
        "COLD": _reading("COLD", 20.0),
    }
    monkeypatch.setattr(movers, "compute_surge", lambda s: readings[s])
    slates = {
        "HOT": [_contract(100, 1.50), _contract(105, 1.00)],
        "COLD": [_contract(50, 0.50)],
    }
    calls = []
    def fake_scan(sym, **kw):
        calls.append(sym)
        return {"opportunities": [dict(c, symbol=sym) for c in slates[sym]]}
    monkeypatch.setattr(movers.opp, "scan", fake_scan)

    r = movers.scan_universe(refresh=True)
    assert [x["symbol"] for x in r["readings"]] == ["HOT", "COLD"]
    # Global table blends surge into ranking: HOT contracts outrank COLD's.
    assert r["options"][0]["symbol"] == "HOT"
    assert all("blended_score" in o for o in r["options"])
    # Only HOT clears the surge threshold for a play; whipsaw carries the wing.
    assert len(r["plays"]) == 1 and r["plays"][0]["symbol"] == "HOT"
    assert r["plays"][0]["wing_plan"]
    assert any("HOT" in h for h in r["headlines"])

    # Second call inside the TTL returns the cache without re-scanning.
    n = len(calls)
    assert movers.scan_universe() is r
    assert len(calls) == n
