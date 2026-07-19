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

    # Data provider: "auto" (moomoo OpenD when configured, else Tradier),
    # "moomoo" (force OpenD), or "tradier" (force Tradier).
    data_provider: str = os.getenv("DATA_PROVIDER", "auto").lower()

    # Dashboard trade routing: "paper" fills locally; "moomoo" routes the
    # dashboard Buy/Close buttons through the moomoo OpenD gateway.
    dashboard_trade_mode: str = os.getenv("DASHBOARD_TRADE_MODE", "paper").lower()

    # Fraction of live US buying power this dashboard may commit to one
    # order. The remainder stays free so manual trades placed in the moomoo
    # app are never blocked by what the dashboard has already tied up.
    buying_power_fraction: float = float(
        os.getenv("BUYING_POWER_FRACTION", "0.6667")
    )

    # --- Autonomous scalper (app/autoscalp.py) ---
    # These live here rather than as module-level os.getenv calls so that
    # .env actually reaches them: pydantic-settings parses .env, it does not
    # populate os.environ, so a bare os.getenv silently ignored every value
    # set here -- including the paper/live switch.
    scalper_trade_mode: str = os.getenv("SCALPER_TRADE_MODE", "paper").lower()
    scalper_underlying: str = os.getenv("SCALPER_UNDERLYING", "SPY")
    scalper_qty: int = int(os.getenv("SCALPER_QTY", "2"))
    scalper_entry_burst: float = float(os.getenv("SCALPER_ENTRY_BURST", "0.0020"))
    scalper_burst_window: float = float(os.getenv("SCALPER_BURST_WINDOW", "45"))
    scalper_cooldown: float = float(os.getenv("SCALPER_COOLDOWN", "180"))
    scalper_max_daily_loss: float = float(
        os.getenv("SCALPER_MAX_DAILY_LOSS", "150")
    )

    # --- Session risk ---
    # Hard no-overnight rule: block entries after the cutoff and force-flatten
    # before the bell. Overnight gaps cannot be stopped out of -- there are no
    # prices between the close and the open -- so this is on by default and
    # should stay on.
    enforce_no_overnight: bool = (
        os.getenv("ENFORCE_NO_OVERNIGHT", "true").lower() == "true"
    )

    # --- Tunnel pointer (stable Railway front door -> ephemeral tunnel) ---
    # Shared secret the home launcher uses to publish its current tunnel URL.
    # Unset means the update endpoint refuses all writes, which is the safe
    # default: an open endpoint could repoint the bookmark at a phishing page.
    tunnel_update_secret: str = os.getenv("TUNNEL_UPDATE_SECRET", "")
    # Optional extra host to allow as a redirect target (a named tunnel).
    tunnel_custom_domain: str = os.getenv("TUNNEL_CUSTOM_DOMAIN", "")
    # Public base URL of the Railway deployment, used by the launcher.
    tunnel_publish_url: str = os.getenv("TUNNEL_PUBLISH_URL", "")

    # --- Dashboard access control ---
    # Required before exposing the UI over a tunnel: the dashboard can place
    # live orders, so an open public URL is an open brokerage account.
    dashboard_user: str = os.getenv("DASHBOARD_USER", "trader")
    dashboard_password: str = os.getenv("DASHBOARD_PASSWORD", "")

    # Train models automatically on startup if none exist and a token is set.
    auto_train_on_start: bool = (
        os.getenv("AUTO_TRAIN_ON_START", "true").lower() == "true"
    )

    @property
    def has_data_source(self) -> bool:
        # moomoo OpenD (real-time) OR a Tradier token counts as a source.
        return bool(self.tradier_token) or bool(self.moomoo_opend_host)

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

    # --- Movers scan (universe-wide options ranking + Surge Score) ---
    # High-volume, high-beta names prone to significant swings. Notes on the
    # requested list: SpaceX is private (DXYZ is the closest public proxy),
    # SK Hynix trades as the OTC ADR HXSCL (options rarely listed — skipped
    # gracefully), and "DRAM" has no US ticker (MU/SNDK/WDC cover memory).
    movers_watchlist: List[str] = [
        s.strip().upper()
        for s in os.getenv(
            "MOVERS_WATCHLIST",
            "NVDA,AMD,INTC,MU,SNDK,WDC,META,TSLA,SMCI,AVGO,"
            "SOXL,SOXS,GUSH,SPY,QQQ,DXYZ,HXSCL",
        ).split(",")
        if s.strip()
    ]
    movers_max_dte: int = int(os.getenv("MOVERS_MAX_DTE", "7"))
    # Wider premium cap than the lotto scanner — ranking does the filtering.
    movers_max_premium: float = float(os.getenv("MOVERS_MAX_PREMIUM", "5.00"))
    # Surge Score needed before a name earns a suggested play card.
    movers_surge_threshold: float = float(os.getenv("MOVERS_SURGE_THRESHOLD", "55"))
    movers_table_size: int = int(os.getenv("MOVERS_TABLE_SIZE", "60"))
    # Opening focus window: data caches tighten and a fast rescan job runs
    # only inside this local weekday window (defaults to the pre/at-open
    # 08:29-09:00 America/Chicago burst the user trades).
    movers_window_tz: str = os.getenv("MOVERS_WINDOW_TZ", "America/Chicago")
    movers_window_start: str = os.getenv("MOVERS_WINDOW_START", "08:29")
    movers_window_end: str = os.getenv("MOVERS_WINDOW_END", "09:00")
    # How often (seconds) to rescan the movers universe inside that window.
    movers_window_refresh_seconds: int = int(
        os.getenv("MOVERS_WINDOW_REFRESH_SECONDS", "45")
    )

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
