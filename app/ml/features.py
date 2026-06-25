"""Technical-indicator feature engineering for the directional model.

Pure pandas/numpy so it is fully unit-testable without network access.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FEATURE_COLUMNS = [
    "ret_1d",
    "ret_5d",
    "ret_10d",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "sma_ratio",
    "ema_ratio",
    "bb_pos",
    "atr_pct",
    "vol_ratio",
    "volatility_20d",
    "momentum_10d",
    "high_low_range",
]


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Return a DataFrame of engineered features aligned to ``df``'s index."""
    if df.empty or len(df) < 60:
        return pd.DataFrame(columns=FEATURE_COLUMNS)

    out = pd.DataFrame(index=df.index)
    close = df["Close"]

    out["ret_1d"] = close.pct_change(1)
    out["ret_5d"] = close.pct_change(5)
    out["ret_10d"] = close.pct_change(10)

    out["rsi_14"] = _rsi(close, 14) / 100.0

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    out["macd"] = macd / close
    out["macd_signal"] = signal / close
    out["macd_hist"] = (macd - signal) / close

    sma20 = close.rolling(20).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    out["sma_ratio"] = close / sma20 - 1
    out["ema_ratio"] = close / ema50 - 1

    std20 = close.rolling(20).std()
    upper = sma20 + 2 * std20
    lower = sma20 - 2 * std20
    out["bb_pos"] = (close - lower) / (upper - lower).replace(0, np.nan)

    atr = _atr(df, 14)
    out["atr_pct"] = atr / close

    vol = df["Volume"]
    out["vol_ratio"] = vol / vol.rolling(20).mean()
    out["volatility_20d"] = close.pct_change().rolling(20).std()
    out["momentum_10d"] = close / close.shift(10) - 1
    out["high_low_range"] = (df["High"] - df["Low"]) / close

    return out[FEATURE_COLUMNS]


def build_labels(df: pd.DataFrame, horizon: int, target_move: float) -> pd.Series:
    """Binary label: 1 if forward ``horizon``-day return exceeds ``target_move``."""
    fwd_return = df["Close"].shift(-horizon) / df["Close"] - 1
    return (fwd_return > target_move).astype(int)


def build_dataset(
    df: pd.DataFrame, horizon: int, target_move: float
) -> tuple[pd.DataFrame, pd.Series]:
    X = build_features(df)
    y = build_labels(df, horizon, target_move)
    data = X.copy()
    data["__label__"] = y
    data = data.dropna()
    if data.empty:
        return pd.DataFrame(columns=FEATURE_COLUMNS), pd.Series(dtype=int)
    return data[FEATURE_COLUMNS], data["__label__"].astype(int)
