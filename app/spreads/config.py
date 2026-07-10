"""Environment-driven configuration for the vertical-spread bot.

All knobs come from ``SPREADS_*`` environment variables so the bot can be
retuned on Railway (or locally via ``.env``) without a code change. The
moomoo OpenD connection settings are shared with the rest of the app and
read from the existing ``MOOMOO_*`` variables.

Every field declares an explicit ``validation_alias`` pinned to its exact
env var name. Without this, pydantic-settings additionally binds each field
by its own bare Python name (case-insensitive) — so e.g. ``min_dte`` would
silently pick up a same-named ``MIN_DTE`` set by an unrelated part of the
app (the equity-scanner config in ``app/config.py`` sets exactly that),
overriding the intended ``SPREADS_MIN_DTE`` default with no error or
warning. The alias makes each field's source unambiguous.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SpreadsConfig(BaseSettings):
    # populate_by_name deliberately stays False: pydantic-settings applies it
    # uniformly to every source, including the env/dotenv source — turning
    # it on to let callers construct with the plain attribute name would
    # simultaneously let a same-named bare env var (e.g. another subsystem's
    # MIN_DTE) satisfy this field again, reopening the exact collision the
    # validation_alias below exists to prevent. Direct construction must use
    # the alias (e.g. SpreadsConfig(SPREADS_MIN_DTE=5)); tests do this via
    # the make_config() helper in tests/test_spreads.py, which translates
    # friendly kwargs through SpreadsConfig.model_fields[...].validation_alias.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Master switches ---
    # paper = simulate fills locally; live = route through moomoo OpenD.
    trade_mode: str = Field(default="paper", validation_alias="SPREADS_TRADE_MODE")
    underlying: str = Field(default="SPY", validation_alias="SPREADS_UNDERLYING")

    # --- Market data: moomoo OpenD real-time push (QUOTE + ORDER_BOOK) ---
    # Contracts roll on/off the 0-3 DTE window as expiries pass; OpenD has no
    # "new contract" push, so a discovery task re-scans the chain and
    # subscribes anything new on this interval. A given expiry's strike list
    # is fetched at most once (cached) — only new expiries entering the
    # window and previously-failed fetches are retried.
    contract_discovery_seconds: float = Field(
        default=15.0, validation_alias="SPREADS_CONTRACT_DISCOVERY_SECONDS"
    )
    # get_option_chain is rate-limited by OpenD/the broker; cap how many
    # distinct-expiry chain calls one discovery cycle may issue and pace
    # them, so a wide DTE window can't burst past the quota.
    max_chain_queries_per_cycle: int = Field(
        default=6, validation_alias="SPREADS_MAX_CHAIN_QUERIES_PER_CYCLE"
    )
    chain_query_pause_seconds: float = Field(
        default=2.0, validation_alias="SPREADS_CHAIN_QUERY_PAUSE_SECONDS"
    )
    # New subscriptions are sent in batches (OpenD/broker enforce a
    # subscription-rate quota) with a pause between batches.
    subscribe_batch_size: int = Field(
        default=40, validation_alias="SPREADS_SUBSCRIBE_BATCH_SIZE"
    )
    subscribe_batch_pause_seconds: float = Field(
        default=1.0, validation_alias="SPREADS_SUBSCRIBE_BATCH_PAUSE_SECONDS"
    )
    # Bounded tick queue: full queue drops the OLDEST tick so the push
    # callback thread never blocks waiting on a slow consumer (backpressure).
    tick_queue_size: int = Field(
        default=10000, validation_alias="SPREADS_TICK_QUEUE_SIZE"
    )

    # --- Universe / expiry focus (0-3 DTE) ---
    min_dte: int = Field(default=0, validation_alias="SPREADS_MIN_DTE")
    max_dte: int = Field(default=3, validation_alias="SPREADS_MAX_DTE")

    # --- IV-rank regime switching ---
    # IVR above this -> sell premium (credit spreads).
    iv_rank_credit_min: float = Field(
        default=70.0, validation_alias="SPREADS_IV_RANK_CREDIT_MIN"
    )
    # IVR below this -> buy premium (debit spreads).
    iv_rank_debit_max: float = Field(
        default=20.0, validation_alias="SPREADS_IV_RANK_DEBIT_MAX"
    )
    # Rolling window (calendar days) the rank is computed against.
    iv_rank_window_days: int = Field(
        default=30, validation_alias="SPREADS_IV_RANK_WINDOW_DAYS"
    )
    # Where ATM-IV observations persist between sessions (JSON).
    iv_history_path: str = Field(
        default="models_store/iv_history.json",
        validation_alias="SPREADS_IV_HISTORY_PATH",
    )
    scan_interval_seconds: float = Field(
        default=5.0, validation_alias="SPREADS_SCAN_INTERVAL"
    )

    # --- Strike selection ---
    # Short leg targets this absolute delta (0.20 = ~80% POP short strike).
    short_leg_delta: float = Field(
        default=0.20, validation_alias="SPREADS_SHORT_LEG_DELTA"
    )
    # Long leg sits this many strikes further OTM (1-5 keeps risk defined
    # while minimising capital).
    wing_width_strikes: int = Field(
        default=2, validation_alias="SPREADS_WING_WIDTH_STRIKES"
    )
    # Reject candidates whose credit is under this fraction of spread width
    # (credit spreads) or whose debit exceeds this fraction (debit spreads).
    # A 0.20-delta short with a 2-strike wing realistically collects
    # ~10-15% of width, hence the 0.10 floor.
    min_credit_to_width: float = Field(
        default=0.10, validation_alias="SPREADS_MIN_CREDIT_TO_WIDTH"
    )
    max_debit_to_width: float = Field(
        default=0.55, validation_alias="SPREADS_MAX_DEBIT_TO_WIDTH"
    )
    # Skip strikes whose quoted bid/ask spread exceeds this fraction of mid
    # (untradeably wide markets poison mid-based limits).
    max_quote_spread_pct: float = Field(
        default=0.15, validation_alias="SPREADS_MAX_QUOTE_SPREAD_PCT"
    )

    # --- Execution ---
    contracts_per_trade: int = Field(
        default=1, validation_alias="SPREADS_CONTRACTS_PER_TRADE"
    )
    max_open_spreads: int = Field(default=2, validation_alias="SPREADS_MAX_OPEN")
    # Limit price = mid +/- this fraction of mid (tight slippage tolerance).
    slippage_tolerance_pct: float = Field(
        default=0.01, validation_alias="SPREADS_SLIPPAGE_PCT"
    )
    # Order-timing guardrail: abort entry if the freshest tick backing the
    # decision is older than this when the order is about to go out.
    max_tick_staleness_ms: float = Field(
        default=150.0, validation_alias="SPREADS_MAX_STALENESS_MS"
    )
    # Cooldown between entries so one regime doesn't machine-gun the book.
    entry_cooldown_seconds: float = Field(
        default=120.0, validation_alias="SPREADS_ENTRY_COOLDOWN"
    )

    # --- Intraday watchdog / circuit breakers ---
    watchdog_interval_seconds: float = Field(
        default=2.0, validation_alias="SPREADS_WATCHDOG_INTERVAL"
    )
    # Flatten a spread once its adverse move consumes this fraction of the
    # position's defined maximum risk.
    position_stop_pct_of_max_risk: float = Field(
        default=0.50, validation_alias="SPREADS_POSITION_STOP_PCT"
    )
    # Flatten EVERYTHING if account equity drops this fraction below the
    # session's starting equity.
    daily_equity_stop_pct: float = Field(
        default=0.03, validation_alias="SPREADS_DAILY_EQUITY_STOP_PCT"
    )
    # Flatten everything if maintenance margin consumes more than this
    # fraction of equity (portfolio-margin headroom guard).
    margin_utilisation_max: float = Field(
        default=0.60, validation_alias="SPREADS_MARGIN_UTILISATION_MAX"
    )
    # Paper-mode equity baseline when no broker account is attached.
    paper_starting_equity: float = Field(
        default=100000.0, validation_alias="SPREADS_PAPER_EQUITY"
    )

    # --- moomoo OpenD gateway (shared with the rest of the app) ---
    moomoo_opend_host: str = Field(default="", validation_alias="MOOMOO_OPEND_HOST")
    moomoo_opend_port: int = Field(default=11111, validation_alias="MOOMOO_OPEND_PORT")
    moomoo_trd_env: str = Field(default="SIMULATE", validation_alias="MOOMOO_TRD_ENV")
    # Trading is unlocked manually in the OpenD GUI per moomoo's security
    # policy — never via the SDK, so no trade password exists here.
    moomoo_security_firm: str = Field(
        default="FUTUINC", validation_alias="MOOMOO_SECURITY_FIRM"
    )
    moomoo_acc_id: int = Field(default=0, validation_alias="MOOMOO_ACC_ID")

    @field_validator("trade_mode", mode="after")
    @classmethod
    def _lower_trade_mode(cls, v: str) -> str:
        return v.lower()

    @field_validator("underlying", mode="after")
    @classmethod
    def _upper_underlying(cls, v: str) -> str:
        return v.upper()

    @field_validator("moomoo_trd_env", mode="after")
    @classmethod
    def _upper_trd_env(cls, v: str) -> str:
        return v.upper()


@lru_cache
def get_config() -> SpreadsConfig:
    return SpreadsConfig()
