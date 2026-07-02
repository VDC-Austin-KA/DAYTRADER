"""Offline tests: feature engineering, model training, and the paper engine.

These run without any network access by synthesizing a price series.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.ml import features as feat
from app.ml import model as ml_model


def _synthetic_ohlcv(n: int = 600, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    # Trending random walk with momentum so the label is learnable.
    rets = rng.normal(0.0005, 0.012, n) + 0.02 * np.sin(np.arange(n) / 25)
    close = 100 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.005, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.005, n)))
    vol = rng.integers(1_000_000, 5_000_000, n)
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": close, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


def test_build_features_shape():
    df = _synthetic_ohlcv()
    X = feat.build_features(df)
    assert list(X.columns) == feat.FEATURE_COLUMNS
    assert len(X) == len(df)


def test_build_dataset_drops_na_and_aligns():
    df = _synthetic_ohlcv()
    X, y = feat.build_dataset(df, horizon=3, target_move=0.01)
    assert len(X) == len(y)
    assert not X.isna().any().any()
    assert set(y.unique()).issubset({0, 1})


def test_train_and_predict(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "model_store_dir", str(tmp_path))
    df = _synthetic_ohlcv()
    result = ml_model.train_symbol("TEST", df)
    assert result is not None
    assert 0.0 <= result.accuracy <= 1.0
    assert result.n_samples > 0

    prob = ml_model.predict_latest("TEST", df)
    assert prob is not None
    assert 0.0 <= prob <= 1.0


def test_datasource_status_without_token(monkeypatch):
    from app import config
    from app.data import market_data as md

    monkeypatch.setattr(config.settings, "tradier_token", "")
    status = md.data_source_status()
    assert status["configured"] is False
    assert status["ok"] is False
    # With no token, network calls short-circuit and return empty/None.
    assert md.get_history("AAPL").empty
    assert md.get_quote("AAPL") is None
    assert md.get_expirations("AAPL") == []


def test_option_chain_parsing(monkeypatch):
    """Tradier JSON -> DataFrame shape the scanner expects (no network)."""
    from app.data import market_data as md

    fake = {
        "options": {
            "option": [
                {"symbol": "AAPL_C", "option_type": "call", "strike": 190,
                 "last": 3.1, "bid": 3.0, "ask": 3.2, "open_interest": 1200,
                 "volume": 300, "greeks": {"mid_iv": 0.25}},
                {"symbol": "AAPL_P", "option_type": "put", "strike": 190,
                 "last": 2.5, "bid": 2.4, "ask": 2.6, "open_interest": 800,
                 "volume": 150, "greeks": {"mid_iv": 0.27}},
            ]
        }
    }
    monkeypatch.setattr(md, "_request", lambda path, params: fake)
    md._CACHE.clear()
    chain = md.get_option_chain("AAPL", "2099-01-15")
    assert list(chain["calls"].columns) == md._CHAIN_COLUMNS
    assert len(chain["calls"]) == 1 and len(chain["puts"]) == 1
    assert float(chain["calls"].iloc[0]["strike"]) == 190.0


def test_pricing_helpers():
    from app.trading import pricing

    # ATM call worth more with more time / vol; prob within [0,1].
    p_short = pricing.bs_price(100, 100, 3 / 365, 0.3, "call")
    p_long = pricing.bs_price(100, 100, 30 / 365, 0.3, "call")
    assert 0 < p_short < p_long
    pop = pricing.prob_of_profit(100, 101, 3 / 365, 0.3, "call")
    assert 0.0 <= pop <= 1.0
    # Deep OTM call has low probability of profit.
    pop_otm = pricing.prob_of_profit(100, 130, 3 / 365, 0.3, "call")
    assert pop_otm < 0.1


def test_opportunity_scan(monkeypatch):
    """Scanner ranks cheap short-dated contracts (no network)."""
    import pandas as pd

    from app.data import market_data as md
    from app.trading import opportunities as opp

    monkeypatch.setattr(md, "get_quote", lambda s: 100.0)
    monkeypatch.setattr(md, "get_history", lambda s, years=5: pd.DataFrame())
    monkeypatch.setattr(md, "get_expirations", lambda s: ["2099-01-03"])
    monkeypatch.setattr(md, "days_to_expiry", lambda e: 2)
    monkeypatch.setattr(opp, "_historical_vol", lambda s: 0.30)
    monkeypatch.setattr(
        opp.ml_model, "predict_latest", lambda s, df: None
    )

    calls = pd.DataFrame([
        {"contractSymbol": "C101", "strike": 101, "lastPrice": 0.5,
         "bid": 0.45, "ask": 0.55, "impliedVolatility": 0.3,
         "openInterest": 500, "volume": 100},
        {"contractSymbol": "C120", "strike": 120, "lastPrice": 0.05,
         "bid": 0.03, "ask": 0.07, "impliedVolatility": 0.3,
         "openInterest": 10, "volume": 1},
        {"contractSymbol": "C_EXPENSIVE", "strike": 100, "lastPrice": 5.0,
         "bid": 4.9, "ask": 5.1, "impliedVolatility": 0.3,
         "openInterest": 500, "volume": 100},  # too expensive, filtered
    ])
    puts = pd.DataFrame(columns=[
        "contractSymbol", "strike", "lastPrice", "bid", "ask",
        "impliedVolatility", "openInterest", "volume",
    ])
    monkeypatch.setattr(md, "get_option_chain", lambda s, e: {"calls": calls, "puts": puts})

    res = opp.scan("TEST", max_dte=3, max_premium=1.0, max_cost=100)
    contracts = [o["contract_symbol"] for o in res["opportunities"]]
    assert "C_EXPENSIVE" not in contracts        # over budget -> excluded
    assert "C101" in contracts and "C120" in contracts
    # Nearer-the-money contract has higher probability of profit than deep OTM.
    by = {o["contract_symbol"]: o for o in res["opportunities"]}
    assert by["C101"]["prob_profit"] > by["C120"]["prob_profit"]
    for o in res["opportunities"]:
        assert o["cost"] <= 100 and o["mid"] <= 1.0


def test_paper_engine(tmp_path, monkeypatch):
    # Use an isolated SQLite DB.
    from app import config

    db_path = tmp_path / "t.db"
    monkeypatch.setattr(config.settings, "database_url", f"sqlite:///{db_path}")

    # Rebuild engine bound to the temp DB.
    import importlib

    from app import database

    importlib.reload(database)
    database.init_db()

    from app.models import Portfolio  # noqa: F401
    from app.trading import paper

    db = database.SessionLocal()
    try:
        pf = paper.get_or_create_portfolio(db)
        start_cash = pf.cash
        pos, msg = paper.open_position(
            db, pf, "AAPL", "call", "AAPL240101C", 190.0, "2099-01-01", 2, 3.50
        )
        assert pos is not None, msg
        assert pf.cash == start_cash - 3.50 * 2 * 100

        ok, _ = paper.close_position(db, pf, pos.id, price=5.00)
        assert ok
        assert pf.cash == start_cash + (5.00 - 3.50) * 2 * 100
    finally:
        db.close()
