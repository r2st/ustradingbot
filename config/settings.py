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

    # ── API keys & external services ────────────────────────────────────────
    ANTHROPIC_API_KEY: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

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
