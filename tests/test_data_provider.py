"""Tests for provider routing, the opening window, and moomoo order codes."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from app.data import market_data as md
from app.data import moomoo_data


# --------------------------------------------------------------------------- #
# Opening focus window
# --------------------------------------------------------------------------- #
def test_open_window_true_inside_and_false_outside(monkeypatch):
    monkeypatch.setattr(md.settings, "movers_window_tz", "America/Chicago", raising=False)
    monkeypatch.setattr(md.settings, "movers_window_start", "08:29", raising=False)
    monkeypatch.setattr(md.settings, "movers_window_end", "09:00", raising=False)

    # 2026-07-10 is a Friday. 13:45 UTC = 08:45 CDT -> inside.
    inside = datetime(2026, 7, 10, 13, 45, tzinfo=timezone.utc)
    assert md.in_open_window(inside)
    # 15:00 UTC = 10:00 CDT -> outside.
    assert not md.in_open_window(datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc))
    # 2026-07-11 is Saturday -> always closed even mid-window.
    assert not md.in_open_window(datetime(2026, 7, 11, 13, 45, tzinfo=timezone.utc))


def test_ttls_tighten_inside_window(monkeypatch):
    monkeypatch.setattr(md, "in_open_window", lambda now=None: True)
    assert md._price_ttl() == 5 and md._chain_ttl() == 20
    monkeypatch.setattr(md, "in_open_window", lambda now=None: False)
    assert md._price_ttl() == 60 and md._chain_ttl() == 300


# --------------------------------------------------------------------------- #
# Provider selection
# --------------------------------------------------------------------------- #
def test_provider_auto_prefers_moomoo_when_configured(monkeypatch):
    monkeypatch.setattr(md.settings, "data_provider", "auto", raising=False)
    monkeypatch.setattr(md.moomoo_data, "configured", lambda: True)
    assert md._provider() == "moomoo"
    monkeypatch.setattr(md.moomoo_data, "configured", lambda: False)
    assert md._provider() == "tradier"


def test_provider_forced_modes(monkeypatch):
    monkeypatch.setattr(md.moomoo_data, "configured", lambda: False)
    monkeypatch.setattr(md.settings, "data_provider", "moomoo", raising=False)
    assert md._provider() == "moomoo"
    monkeypatch.setattr(md.settings, "data_provider", "tradier", raising=False)
    assert md._provider() == "tradier"


def test_quote_uses_moomoo_then_falls_back_to_tradier(monkeypatch):
    md._CACHE.clear()
    monkeypatch.setattr(md.settings, "data_provider", "auto", raising=False)
    monkeypatch.setattr(md.moomoo_data, "configured", lambda: True)
    monkeypatch.setattr(md.moomoo_data, "get_quote", lambda s: 123.45)
    assert md.get_quote("NVDA") == 123.45

    # moomoo returns nothing in auto mode -> Tradier path is tried.
    md._CACHE.clear()
    monkeypatch.setattr(md.moomoo_data, "get_quote", lambda s: None)
    called = {}
    def fake_request(path, params):
        called["path"] = path
        return {"quotes": {"quote": {"last": 99.0}}}
    monkeypatch.setattr(md, "_request", fake_request)
    assert md.get_quote("NVDA") == 99.0
    assert called["path"] == "markets/quotes"


def test_forced_moomoo_does_not_touch_tradier(monkeypatch):
    md._CACHE.clear()
    monkeypatch.setattr(md.settings, "data_provider", "moomoo", raising=False)
    monkeypatch.setattr(md.moomoo_data, "get_quote", lambda s: None)
    monkeypatch.setattr(
        md.moomoo_data, "get_history",
        lambda s, years=1, interval="daily": pd.DataFrame(),
    )
    def boom(*a, **k):
        raise AssertionError("Tradier must not be called in forced moomoo mode")
    monkeypatch.setattr(md, "_request", boom)
    assert md.get_quote("NVDA") is None


def test_option_chain_moomoo_rows_short_circuit_tradier(monkeypatch):
    md._CACHE.clear()
    monkeypatch.setattr(md.settings, "data_provider", "auto", raising=False)
    monkeypatch.setattr(md.moomoo_data, "configured", lambda: True)
    chain = {
        "calls": pd.DataFrame([{"contractSymbol": "US.X", "strike": 100.0}]),
        "puts": pd.DataFrame(),
    }
    monkeypatch.setattr(md.moomoo_data, "get_option_chain", lambda s, e: chain)
    monkeypatch.setattr(md, "_request", lambda *a, **k: pytest.fail("Tradier hit"))
    out = md.get_option_chain("NVDA", "2026-07-11")
    assert list(out["calls"]["contractSymbol"]) == ["US.X"]


# --------------------------------------------------------------------------- #
# moomoo order-code mapping
# --------------------------------------------------------------------------- #
def test_moomoo_order_code_mapping():
    from app.trading import moomoo_orders as mo

    assert mo._to_moomoo_code("NVDA", "US.NVDA260711C120000") == "US.NVDA260711C120000"
    assert mo._to_moomoo_code("NVDA", "NVDA260711C120000") == "US.NVDA260711C120000"
    assert mo._to_moomoo_code("NVDA", "") == "US.NVDA"


def test_moomoo_order_not_configured_returns_error(monkeypatch):
    from app.trading import moomoo_orders as mo

    monkeypatch.setattr(mo.settings, "moomoo_opend_host", "", raising=False)
    res = mo.place_option_order("NVDA", "US.NVDA260711C120000", "BUY", 1, 1.2)
    assert not res.ok and "not configured" in res.message


# --------------------------------------------------------------------------- #
# moomoo data provider shape (SDK mocked out via _call)
# --------------------------------------------------------------------------- #
def test_moomoo_get_quote_reads_last_price(monkeypatch):
    snap = pd.DataFrame([{"last_price": 250.5, "prev_close_price": 249.0}])
    monkeypatch.setattr(moomoo_data, "_call", lambda fn, *a, **k: snap)
    assert moomoo_data.get_quote("NVDA") == 250.5


def test_moomoo_chain_normalises_iv_and_splits_sides(monkeypatch):
    contracts = pd.DataFrame([
        {"code": "US.NVDA_C", "strike_price": 120.0, "option_type": "CALL"},
        {"code": "US.NVDA_P", "strike_price": 120.0, "option_type": "PUT"},
    ])
    snap = pd.DataFrame([
        {"code": "US.NVDA_C", "last_price": 2.0, "bid_price": 1.9, "ask_price": 2.1,
         "option_implied_volatility": 45.0, "option_open_interest": 100, "volume": 50},
        {"code": "US.NVDA_P", "last_price": 1.5, "bid_price": 1.4, "ask_price": 1.6,
         "option_implied_volatility": 40.0, "option_open_interest": 80, "volume": 30},
    ])

    def fake_call(fn, *a, **k):
        if fn == "get_option_chain":
            return contracts
        if fn == "get_market_snapshot":
            arg = a[0] if a else k.get("code")
            if arg == ["US.NVDA"] or arg == "US.NVDA":
                return pd.DataFrame([{"last_price": 120.0, "prev_close_price": 119.0}])
            return snap
        return None

    monkeypatch.setattr(moomoo_data, "_call", fake_call)
    out = moomoo_data.get_option_chain("NVDA", "2026-07-11")
    assert len(out["calls"]) == 1 and len(out["puts"]) == 1
    # 45.0% -> 0.45 fraction.
    assert out["calls"].iloc[0]["impliedVolatility"] == pytest.approx(0.45)
    assert out["calls"].iloc[0]["openInterest"] == 100
