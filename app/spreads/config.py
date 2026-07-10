"""Environment-driven configuration for the vertical-spread bot.

All knobs come from ``SPREADS_*`` environment variables so the bot can be
retuned on Railway (or locally via ``.env``) without a code change. The
moomoo OpenD connection settings are shared with the rest of the app and
read from the existing ``MOOMOO_*`` variables.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class SpreadsConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Master switches ---
    # paper = simulate fills locally; live = route through moomoo OpenD.
    trade_mode: str = os.getenv("SPREADS_TRADE_MODE", "paper").lower()
    underlying: str = os.getenv("SPREADS_UNDERLYING", "SPY").upper()

    # --- Market data: Polygon.io OPRA stream + snapshots ---
    polygon_api_key: str = os.getenv("POLYGON_API_KEY", "")
    polygon_ws_url: str = os.getenv(
        "POLYGON_WS_URL", "wss://socket.polygon.io/options"
    )
    polygon_rest_url: str = os.getenv("POLYGON_REST_URL", "https://api.polygon.io")
    # Seconds between REST snapshot refreshes (greeks/IV move slower than NBBO;
    # Polygon streams quotes over WS but serves greeks via the snapshot API).
    greeks_refresh_seconds: float = float(
        os.getenv("SPREADS_GREEKS_REFRESH_SECONDS", "15")
    )
    # Bounded tick queue: full queue drops the OLDEST tick so the reader
    # coroutine never blocks on a slow consumer (backpressure policy).
    tick_queue_size: int = int(os.getenv("SPREADS_TICK_QUEUE_SIZE", "10000"))

    # --- Universe / expiry focus (0-3 DTE) ---
    min_dte: int = int(os.getenv("SPREADS_MIN_DTE", "0"))
    max_dte: int = int(os.getenv("SPREADS_MAX_DTE", "3"))

    # --- IV-rank regime switching ---
    # IVR above this -> sell premium (credit spreads).
    iv_rank_credit_min: float = float(os.getenv("SPREADS_IV_RANK_CREDIT_MIN", "70"))
    # IVR below this -> buy premium (debit spreads).
    iv_rank_debit_max: float = float(os.getenv("SPREADS_IV_RANK_DEBIT_MAX", "20"))
    # Rolling window (calendar days) the rank is computed against.
    iv_rank_window_days: int = int(os.getenv("SPREADS_IV_RANK_WINDOW_DAYS", "30"))
    # Where ATM-IV observations persist between sessions (JSON).
    iv_history_path: str = os.getenv(
        "SPREADS_IV_HISTORY_PATH", "models_store/iv_history.json"
    )
    scan_interval_seconds: float = float(os.getenv("SPREADS_SCAN_INTERVAL", "5"))

    # --- Strike selection ---
    # Short leg targets this absolute delta (0.20 = ~80% POP short strike).
    short_leg_delta: float = float(os.getenv("SPREADS_SHORT_LEG_DELTA", "0.20"))
    # Long leg sits this many strikes further OTM (1-5 keeps risk defined
    # while minimising capital).
    wing_width_strikes: int = int(os.getenv("SPREADS_WING_WIDTH_STRIKES", "2"))
    # Reject candidates whose credit is under this fraction of spread width
    # (credit spreads) or whose debit exceeds this fraction (debit spreads).
    # A 0.20-delta short with a 2-strike wing realistically collects
    # ~10-15% of width, hence the 0.10 floor.
    min_credit_to_width: float = float(os.getenv("SPREADS_MIN_CREDIT_TO_WIDTH", "0.10"))
    max_debit_to_width: float = float(os.getenv("SPREADS_MAX_DEBIT_TO_WIDTH", "0.55"))
    # Skip strikes whose quoted bid/ask spread exceeds this fraction of mid
    # (untradeably wide markets poison mid-based limits).
    max_quote_spread_pct: float = float(os.getenv("SPREADS_MAX_QUOTE_SPREAD_PCT", "0.15"))

    # --- Execution ---
    contracts_per_trade: int = int(os.getenv("SPREADS_CONTRACTS_PER_TRADE", "1"))
    max_open_spreads: int = int(os.getenv("SPREADS_MAX_OPEN", "2"))
    # Limit price = mid +/- this fraction of mid (tight slippage tolerance).
    slippage_tolerance_pct: float = float(os.getenv("SPREADS_SLIPPAGE_PCT", "0.01"))
    # Order-timing guardrail: abort entry if the freshest tick backing the
    # decision is older than this when the order is about to go out.
    max_tick_staleness_ms: float = float(os.getenv("SPREADS_MAX_STALENESS_MS", "150"))
    # Cooldown between entries so one regime doesn't machine-gun the book.
    entry_cooldown_seconds: float = float(os.getenv("SPREADS_ENTRY_COOLDOWN", "120"))

    # --- Intraday watchdog / circuit breakers ---
    watchdog_interval_seconds: float = float(os.getenv("SPREADS_WATCHDOG_INTERVAL", "2"))
    # Flatten a spread once its adverse move consumes this fraction of the
    # position's defined maximum risk.
    position_stop_pct_of_max_risk: float = float(
        os.getenv("SPREADS_POSITION_STOP_PCT", "0.50")
    )
    # Flatten EVERYTHING if account equity drops this fraction below the
    # session's starting equity.
    daily_equity_stop_pct: float = float(os.getenv("SPREADS_DAILY_EQUITY_STOP_PCT", "0.03"))
    # Flatten everything if maintenance margin consumes more than this
    # fraction of equity (portfolio-margin headroom guard).
    margin_utilisation_max: float = float(os.getenv("SPREADS_MARGIN_UTILISATION_MAX", "0.60"))
    # Paper-mode equity baseline when no broker account is attached.
    paper_starting_equity: float = float(os.getenv("SPREADS_PAPER_EQUITY", "100000"))

    # --- moomoo OpenD gateway (shared with the rest of the app) ---
    moomoo_opend_host: str = os.getenv("MOOMOO_OPEND_HOST", "")
    moomoo_opend_port: int = int(os.getenv("MOOMOO_OPEND_PORT", "11111"))
    moomoo_trd_env: str = os.getenv("MOOMOO_TRD_ENV", "SIMULATE").upper()
    # Trading is unlocked manually in the OpenD GUI per moomoo's security
    # policy — never via the SDK, so no trade password exists here.
    moomoo_security_firm: str = os.getenv("MOOMOO_SECURITY_FIRM", "FUTUINC")
    moomoo_acc_id: int = int(os.getenv("MOOMOO_ACC_ID", "0"))


@lru_cache
def get_config() -> SpreadsConfig:
    return SpreadsConfig()
