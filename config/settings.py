"""
Application settings loaded from environment variables and .env file.

Uses pydantic-settings for type-safe configuration with validation.
All trading parameters, risk limits, and API credentials are centralised here.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import ClassVar, Dict

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for the US/CA equity trading bot.

    Values are loaded from environment variables first, then from the ``.env``
    file in the project root.  Pydantic validates types and applies defaults
    so the rest of the codebase can rely on well-typed attributes.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Interactive Brokers connection ───────────────────────────────────────
    IBKR_HOST: str = "127.0.0.1"
    IBKR_PORT: int = 7497
    IBKR_CLIENT_ID: int = 1
    IBKR_ACCOUNT_ID: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def IS_PAPER_TRADING(self) -> bool:
        """Derive paper-trading flag from the gateway port.

        Port 7497 is the TWS/Gateway paper-trading port;
        port 7496 is live.
        """
        return self.IBKR_PORT == 7497

    # ── Capital allocation ──────────────────────────────────────────────────
    CAPITAL_BY_CURRENCY: dict[str, float] = Field(
        default={"USD": 9000.0, "CAD": 3000.0},
    )
    TOTAL_CAPITAL: float = 12_000.0

    def get_capital_for_currency(self, currency: str) -> float:
        """Return the allocated capital for *currency*.

        Args:
            currency: ISO 4217 currency code (e.g. ``"USD"``, ``"CAD"``).

        Returns:
            The capital amount, or ``0.0`` if the currency has no allocation.
        """
        return self.CAPITAL_BY_CURRENCY.get(currency.upper(), 0.0)

    # ── Position / risk limits ──────────────────────────────────────────────
    MAX_POSITION_SIZE_PCT: float = 0.015
    MAX_OPEN_POSITIONS: int = 25
    MAX_MOMENTUM_POSITIONS: int = 18
    MAX_SWING_POSITIONS: int = 15
    MAX_PEAD_POSITIONS: int = 5
    DAILY_LOSS_LIMIT_PCT: float = 0.015
    ATR_STOP_MULTIPLIER: float = 1.5
    RISK_REWARD_MIN: float = 1.8

    # ── Scanning & signal freshness ─────────────────────────────────────────
    SCAN_INTERVAL_MINUTES: int = 60
    SIGNAL_FRESHNESS_TOLERANCE_PCT: float = 0.01
    SIGNAL_MAX_AGE_MINUTES: int = 15

    # ── Broker reconnection (exponential backoff) ───────────────────────────
    # When the broker session drops, the engine retries connect() with
    # exponential backoff: delay = BASE * 2**(attempt-1), capped at MAX_DELAY,
    # for up to MAX_ATTEMPTS tries before giving up on the cycle.
    RECONNECT_MAX_ATTEMPTS: int = 5
    RECONNECT_BASE_DELAY_SECONDS: float = 2.0
    RECONNECT_MAX_DELAY_SECONDS: float = 60.0

    # ── Position management ─────────────────────────────────────────────────
    HOLD_MAX_DAYS: int = 20
    REENTRY_COOLDOWN_MINUTES: int = 90
    ORDER_CUTOFF_MINUTES_BEFORE_CLOSE: int = 5
    GHOST_POSITION_MAX_DEFER_HOURS: int = 48

    # ── Partial-take / trailing stop ────────────────────────────────────────
    ENABLE_PARTIAL_TAKE_TRAIL: bool = True

    # ── Market hours (Eastern Time) ─────────────────────────────────────────
    MARKET_OPEN_HOUR: int = 9
    MARKET_OPEN_MINUTE: int = 30
    MARKET_CLOSE_HOUR: int = 16
    MARKET_CLOSE_MINUTE: int = 0

    # ── AI veto layer (OpenRouter) ──────────────────────────────────────────
    # The AI news-veto layer uses OpenRouter (https://openrouter.ai).  Only the
    # API key is read from the environment (OPENROUTER_API_KEY); it is never
    # hard-coded.  Free models such as ``openai/gpt-oss-20b:free`` incur $0 cost.
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    OPENROUTER_MODEL: str = "openai/gpt-oss-20b:free"
    OPENROUTER_TIMEOUT_SECONDS: float = 45.0
    # Pricing per 1M tokens (free models are 0.0). Used for cost tracking only.
    OPENROUTER_INPUT_COST_PER_1M: float = 0.0
    OPENROUTER_OUTPUT_COST_PER_1M: float = 0.0
    # Enable/disable the paid Tier-2 LLM call. When False, only the free
    # Tier-1 earnings filter runs and everything else is auto-approved.
    AI_VETO_ENABLED: bool = True
    # How long (hours) an AI verdict is cached per symbol+strategy.
    AI_CACHE_TTL_HOURS: float = 4.0
    # Reject signals whose earnings fall within this many days (Tier-1 filter).
    AI_EARNINGS_BLACKOUT_DAYS: int = 14

    # ── Legacy Anthropic key (unused; kept for backward compat) ─────────────
    ANTHROPIC_API_KEY: str = ""

    # ── Telegram notifications ──────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # ── Dashboard (FastAPI) ─────────────────────────────────────────────────
    # HTTP Basic Auth guards the dashboard.  Auth is ENABLED by default; if no
    # password is configured the app refuses to start (fail-closed) so the
    # dashboard is never accidentally exposed without credentials.  Set
    # DASHBOARD_AUTH_ENABLED=False only for trusted local development.
    DASHBOARD_AUTH_ENABLED: bool = True
    DASHBOARD_USERNAME: str = "admin"
    DASHBOARD_PASSWORD: str = ""
    # Default bind host for the dashboard (documented for the run command).
    DASHBOARD_HOST: str = "127.0.0.1"
    DASHBOARD_PORT: int = 8501

    # ── Broker selection ────────────────────────────────────────────────────
    # PAPER TRADING IS THE DEFAULT.  "paper" runs the built-in simulated broker
    # (no TWS/Gateway, no API keys, works headless) so a user can open the
    # dashboard and start paper trading immediately with zero extra setup.
    # Switch to real-money trading ONLY by explicitly setting BROKER=ibkr and
    # pointing IBKR_PORT at a LIVE gateway (7496).
    BROKER: str = "paper"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def IS_LIVE_TRADING(self) -> bool:
        """Return ``True`` only when trading with REAL money.

        Live trading requires *both* the IBKR broker *and* a connection to the
        live gateway port (7496).  Every other configuration — the default
        simulated paper broker, or an IBKR connection to the 7497 paper
        gateway — is paper trading and risks no real capital.
        """
        return self.BROKER.lower() == "ibkr" and self.IBKR_PORT == 7496

    @computed_field  # type: ignore[prop-decorator]
    @property
    def TRADING_MODE(self) -> str:
        """Human-facing mode label: ``"LIVE"`` or ``"PAPER"``."""
        return "LIVE" if self.IS_LIVE_TRADING else "PAPER"

    @property
    def paper_broker_label(self) -> str:
        """Describe which paper backend is in use (for the dashboard)."""
        if self.BROKER.lower() == "ibkr":
            return "Interactive Brokers paper gateway (port 7497)"
        return "Built-in simulated broker (no gateway required)"

    # ── Paper-broker fill realism (slippage + commission) ───────────────────
    # The paper broker (and the backtester, which shares its fill maths)
    # models two real-world frictions so simulated P&L is not optimistic:
    #
    #   * Slippage — entries fill *above* the requested price and stop exits
    #     fill *below* the stop by PAPER_SLIPPAGE_BPS basis points
    #     (1 bp = 0.01%).  A gap that opens straight through a stop fills at
    #     the (worse) bar open instead of the stop price.
    #   * Commission — a per-share charge on both the entry and the exit;
    #     paper P&L is reported net of the round-trip commission.
    PAPER_SLIPPAGE_BPS: float = 5.0
    PAPER_COMMISSION_PER_SHARE: float = 0.005

    # ── Market-data provider selection ──────────────────────────────────────
    # "yfinance" uses the free Yahoo Finance backend (default, no key needed).
    # "alpaca" uses Alpaca's market-data API with optional websocket streaming
    # for low-latency exits (requires alpaca-py + API keys below).
    MARKET_DATA_PROVIDER: str = "yfinance"
    ALPACA_API_KEY: str = ""
    ALPACA_API_SECRET: str = ""
    ALPACA_DATA_FEED: str = "iex"  # "iex" (free) or "sip" (paid)

    # ── Data cache TTLs (seconds) ───────────────────────────────────────────
    # Fetched data is memoised in a thread-safe TTL cache to eliminate the
    # ~160 redundant Yahoo calls per scan cycle.  Current price is cached
    # briefly; OHLCV history (daily bars) is stable for far longer.
    PRICE_CACHE_TTL_SECONDS: float = 300.0  # 5 minutes
    OHLCV_CACHE_TTL_SECONDS: float = 3600.0  # 1 hour
    DATA_CACHE_ENABLED: bool = True

    # ── Fetch retry (exponential backoff) ───────────────────────────────────
    # Transient Yahoo/Alpaca failures are retried with exponential backoff:
    # delay = BASE * 2**(attempt-1), capped at MAX_DELAY.
    FETCH_MAX_RETRIES: int = 3
    FETCH_RETRY_BASE_DELAY_SECONDS: float = 0.5
    FETCH_RETRY_MAX_DELAY_SECONDS: float = 8.0

    # ── Realtime exit polling ───────────────────────────────────────────────
    # When a realtime-capable provider (e.g. alpaca) is active, the engine can
    # poll exits far more frequently than the SCAN_INTERVAL_MINUTES entry
    # cadence.  Between full scan cycles it wakes every
    # REALTIME_EXIT_POLL_SECONDS to run exit management only.  Ignored for the
    # yfinance provider (daily bars make sub-minute polling pointless).
    REALTIME_EXIT_POLL_SECONDS: float = 30.0
    ENABLE_REALTIME_EXITS: bool = True

    def is_realtime_provider(self) -> bool:
        """Return whether the active data provider supports realtime streaming.

        Only providers with an intraday/streaming feed benefit from the
        faster exit-poll loop; the default yfinance (daily-bar) provider does
        not.
        """
        return self.MARKET_DATA_PROVIDER.lower() in ("alpaca",)

    # ── Logging ─────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    # ── Data persistence ────────────────────────────────────────────────────
    DATA_DIR: Path = Path("data_store")

    # ── Scoring weights (class-level constants) ─────────────────────────────
    MOMENTUM_WEIGHTS: ClassVar[dict[str, float]] = {
        "ema": 0.25,
        "macd": 0.25,
        "ripster": 0.20,
        "volume": 0.20,
        "rsi": 0.10,
    }

    SWING_WEIGHTS: ClassVar[dict[str, float]] = {
        "rsi": 0.25,
        "ema": 0.25,
        "ripster": 0.20,
        "volume": 0.20,
        "macd": 0.10,
    }

    # ── Grade thresholds ────────────────────────────────────────────────────
    GRADE_A_THRESHOLD: float = 0.78
    GRADE_B_THRESHOLD: float = 0.65
    GRADE_C_THRESHOLD: float = 0.38

    # ── Hard veto thresholds ────────────────────────────────────────────────
    MIN_ATR_PCT: float = 0.015  # 1.5 % — below this ATR%  the stock is untradeable

    # ── Minimum OHLCV rows for indicator calculation ────────────────────────
    MIN_OHLCV_ROWS: int = 200


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton :class:`Settings` instance.

    The instance is created once and reused for the lifetime of the process.
    Call this instead of constructing ``Settings()`` directly so that
    environment variables and the ``.env`` file are read only once.
    """
    return Settings()


# ---------------------------------------------------------------------------
# Helper: resolve weights for a strategy name
# ---------------------------------------------------------------------------

#: Convenience references to the class-level weight dicts.
momentum_weights: Dict[str, float] = Settings.MOMENTUM_WEIGHTS
swing_weights: Dict[str, float] = Settings.SWING_WEIGHTS


def weights_for_strategy(strategy: str) -> Dict[str, float]:
    """Return the indicator-weight dict for *strategy*.

    Momentum-family strategies (momentum, vcp_breakout, pead) use
    :data:`momentum_weights`.  Swing-family strategies (swing,
    mean_reversion) use :data:`swing_weights`.

    Raises:
        ValueError: If *strategy* is not recognised.
    """
    strategy_lower = strategy.lower()
    if strategy_lower in ("momentum", "vcp_breakout", "pead"):
        return momentum_weights
    if strategy_lower in ("swing", "mean_reversion"):
        return swing_weights
    raise ValueError(f"Unknown strategy: {strategy!r}")
