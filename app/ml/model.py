"""Gradient-boosting directional model: train, persist, predict."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ..config import settings
from . import features as feat


@dataclass
class TrainResult:
    symbol: str
    accuracy: float
    roc_auc: float
    n_samples: int
    feature_importance: dict[str, float] = field(default_factory=dict)
    trained_at: str = ""


def _model_path(symbol: str) -> str:
    os.makedirs(settings.model_store_dir, exist_ok=True)
    return os.path.join(settings.model_store_dir, f"{symbol.upper()}.joblib")


def train_symbol(symbol: str, df: pd.DataFrame) -> Optional[TrainResult]:
    """Train and persist a model for ``symbol``. Returns metrics or None."""
    X, y = feat.build_dataset(df, settings.horizon_days, settings.target_move)
    if len(X) < 200 or y.nunique() < 2:
        return None

    # Time-ordered split: train on the past, validate on the recent tail.
    split = int(len(X) * 0.8)
    X_train, X_test = X.iloc[:split], X.iloc[split:]
    y_train, y_test = y.iloc[:split], y.iloc[split:]
    if y_train.nunique() < 2 or len(X_test) < 20:
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, shuffle=False
        )

    pipe = Pipeline(
        [
            ("scaler", StandardScaler()),
            (
                "clf",
                GradientBoostingClassifier(
                    n_estimators=200,
                    max_depth=3,
                    learning_rate=0.05,
                    subsample=0.85,
                    random_state=42,
                ),
            ),
        ]
    )
    pipe.fit(X_train, y_train)

    proba = pipe.predict_proba(X_test)[:, 1]
    preds = (proba >= 0.5).astype(int)
    acc = float(accuracy_score(y_test, preds))
    try:
        auc = float(roc_auc_score(y_test, proba))
    except ValueError:
        auc = 0.5

    importances = pipe.named_steps["clf"].feature_importances_
    fi = {c: float(v) for c, v in zip(feat.FEATURE_COLUMNS, importances)}

    joblib.dump(pipe, _model_path(symbol))
    meta = {
        "symbol": symbol.upper(),
        "accuracy": acc,
        "roc_auc": auc,
        "n_samples": int(len(X)),
        "trained_at": datetime.utcnow().isoformat(),
        "feature_importance": fi,
    }
    with open(_model_path(symbol) + ".json", "w") as fh:
        json.dump(meta, fh)

    return TrainResult(
        symbol=symbol.upper(),
        accuracy=acc,
        roc_auc=auc,
        n_samples=int(len(X)),
        feature_importance=fi,
        trained_at=meta["trained_at"],
    )


def load_model(symbol: str):
    path = _model_path(symbol)
    if os.path.exists(path):
        try:
            return joblib.load(path)
        except Exception:
            return None
    return None


def predict_latest(symbol: str, df: pd.DataFrame) -> Optional[float]:
    """Return P(up move) for the most recent bar, or None if unavailable."""
    model = load_model(symbol)
    if model is None:
        return None
    X = feat.build_features(df).dropna()
    if X.empty:
        return None
    latest = X.iloc[[-1]]
    try:
        return float(model.predict_proba(latest)[:, 1][0])
    except Exception:
        return None
