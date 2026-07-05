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

    # --- Market data (Tradier) ---
    # Create a free developer account at https://developer.tradier.com and paste
    # the access token here / as a Railway service variable.
    tradier_token: str = os.getenv("TRADIER_TOKEN", "")
    # Sandbox: https://sandbox.tradier.com/v1  |  Production: https://api.tradier.com/v1
    tradier_base_url: str = os.getenv(
        "TRADIER_BASE_URL", "https://sandbox.tradier.com/v1"
    )

    # Train models automatically on startup if none exist and a token is set.
    auto_train_on_start: bool = (
        os.getenv("AUTO_TRAIN_ON_START", "true").lower() == "true"
    )

    @property
    def has_data_source(self) -> bool:
        return bool(self.tradier_token)

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

    # --- BTC hourly prediction-market bot ---
    # Master switch for the autonomous bot loop.
    prediction_enabled: bool = (
        os.getenv("PREDICTION_BOT_ENABLED", "true").lower() == "true"
    )
    # "paper" simulates fills locally; "live" routes orders through moomoo OpenD.
    prediction_trade_mode: str = os.getenv("PREDICTION_TRADE_MODE", "paper").lower()
    prediction_cycle_seconds: int = int(os.getenv("PREDICTION_CYCLE_SECONDS", "60"))
    # Kalshi powers moomoo's prediction markets; its public market-data API is
    # keyless and is used to discover/quote the BTC hourly contracts.
    kalshi_base_url: str = os.getenv(
        "KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
    )
    prediction_series_ticker: str = os.getenv("PREDICTION_SERIES_TICKER", "KXBTCD")

    # Strategy knobs.
    prediction_min_edge: float = float(os.getenv("PREDICTION_MIN_EDGE", "0.06"))
    prediction_momentum_coeff: float = float(
        os.getenv("PREDICTION_MOMENTUM_COEFF", "0.35")
    )
    prediction_min_minutes_left: float = float(
        os.getenv("PREDICTION_MIN_MINUTES_LEFT", "8")
    )
    prediction_max_minutes_left: float = float(
        os.getenv("PREDICTION_MAX_MINUTES_LEFT", "55")
    )
    prediction_min_price_cents: int = int(os.getenv("PREDICTION_MIN_PRICE_CENTS", "10"))
    prediction_max_price_cents: int = int(os.getenv("PREDICTION_MAX_PRICE_CENTS", "90"))

    # Risk management.
    prediction_bankroll: float = float(os.getenv("PREDICTION_BANKROLL", "1000"))
    prediction_kelly_fraction: float = float(
        os.getenv("PREDICTION_KELLY_FRACTION", "0.25")
    )
    prediction_max_stake_usd: float = float(
        os.getenv("PREDICTION_MAX_STAKE_USD", "100")
    )
    prediction_max_stake_pct: float = float(
        os.getenv("PREDICTION_MAX_STAKE_PCT", "0.10")
    )
    prediction_max_daily_loss: float = float(
        os.getenv("PREDICTION_MAX_DAILY_LOSS", "150")
    )
    prediction_max_open: int = int(os.getenv("PREDICTION_MAX_OPEN", "1"))
    prediction_max_consecutive_losses: int = int(
        os.getenv("PREDICTION_MAX_CONSECUTIVE_LOSSES", "4")
    )
    prediction_cooldown_minutes: int = int(
        os.getenv("PREDICTION_COOLDOWN_MINUTES", "60")
    )

    # moomoo OpenD gateway (required only for live mode). Credentials/config are
    # injected by Railway service variables; never hard-code them.
    moomoo_opend_host: str = os.getenv("MOOMOO_OPEND_HOST", "")
    moomoo_opend_port: int = int(os.getenv("MOOMOO_OPEND_PORT", "11111"))
    moomoo_trd_env: str = os.getenv("MOOMOO_TRD_ENV", "SIMULATE").upper()
    # No trade password here on purpose: per moomoo's OpenAPI security policy,
    # trading is unlocked manually in the OpenD GUI, never via the SDK.
    moomoo_security_firm: str = os.getenv("MOOMOO_SECURITY_FIRM", "FUTUINC")
    moomoo_acc_id: int = int(os.getenv("MOOMOO_ACC_ID", "0"))
    # Prefix used to build the broker symbol from the Kalshi market ticker.
    moomoo_code_prefix: str = os.getenv("MOOMOO_CODE_PREFIX", "US.")

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
