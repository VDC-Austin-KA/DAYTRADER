"""Application configuration loaded from environment variables."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", protected_namespaces=("settings_",)
    )

    # --- Web ---
    app_name: str = "ML Options Day Trader"
    secret_key: str = os.getenv("SECRET_KEY", "dev-secret-change-me")

    # --- Database ---
    # Railway provides DATABASE_URL for the attached Postgres plugin.
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./daytrader.db")

    # --- Paper-trading defaults ---
    starting_cash: float = float(os.getenv("STARTING_CASH", "100000"))

    # --- Strategy parameters ---
    # Only consider options expiring within this many days (the < 1 month focus).
    max_dte: int = int(os.getenv("MAX_DTE", "30"))
    min_dte: int = int(os.getenv("MIN_DTE", "1"))
    # Model probability above which we emit a directional signal.
    signal_threshold: float = float(os.getenv("SIGNAL_THRESHOLD", "0.58"))
    # Forward horizon (trading days) the classifier is trained to predict.
    horizon_days: int = int(os.getenv("HORIZON_DAYS", "3"))
    # Move size (fraction) that counts as a "win" for the label.
    target_move: float = float(os.getenv("TARGET_MOVE", "0.01"))
    # Years of daily history used to train.
    history_years: int = int(os.getenv("HISTORY_YEARS", "5"))

    # --- Universe ---
    default_watchlist: List[str] = [
        s.strip().upper()
        for s in os.getenv(
            "WATCHLIST",
            "SPY,QQQ,AAPL,MSFT,NVDA,TSLA,AMD,AMZN,META,GOOGL",
        ).split(",")
        if s.strip()
    ]

    # --- Scheduler ---
    enable_scheduler: bool = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
    refresh_minutes: int = int(os.getenv("REFRESH_MINUTES", "30"))

    model_store_dir: str = os.getenv("MODEL_STORE_DIR", "models_store")

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        # SQLAlchemy needs the postgresql+psycopg2 dialect; Railway gives postgres://
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg2://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
