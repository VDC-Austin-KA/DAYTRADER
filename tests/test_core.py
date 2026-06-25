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
