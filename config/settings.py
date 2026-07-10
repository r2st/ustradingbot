"""
Application settings loaded from environment variables and .env file.

Uses pydantic-settings for type-safe configuration with validation.
All trading parameters, risk limits, and API credentials are centralised here.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any, ClassVar, Dict
from zoneinfo import ZoneInfo

from pydantic import Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ── Canonical timezone for the trading bot ────────────────────────────────
# All user-facing timestamps (dashboard, journal, exports) and
# market-hours logic should reference this single constant so there is
# exactly one place to change if the deployment ever targets a different
# exchange timezone.
EASTERN = ZoneInfo("America/New_York")

# ── Loose key files (keys/ directory) ───────────────────────────────────────
# Some credentials are kept as loose files under ``keys/`` (gitignored) rather
# than inline in ``.env``.  When the corresponding setting is not supplied via
# an environment variable or ``.env``, its value is read from the mapped file.
# Set the ``USTB_SKIP_KEY_FILES`` environment variable to disable this fallback
# (the test suite does so to stay hermetic).
_KEYS_DIR: Path = Path(__file__).resolve().parent.parent / "keys"
_KEY_FILES: Dict[str, str] = {
    "POLYGON_API_KEY": "polygon_api_key",
}


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
    MAX_SELECTIVE_POSITIONS: int = 3
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

    # ── Dynamic stop management ──────────────────────────────────────────────
    # Master switch for ALL dynamic stop mechanisms (trailing, breakeven,
    # time-based tightening).  ``ENABLE_PARTIAL_TAKE_TRAIL`` is the legacy
    # name kept for backward compatibility; new deployments should use
    # ``ENABLE_DYNAMIC_STOPS`` instead.  If either is explicitly set to
    # ``False`` in the environment, dynamic stops are disabled.
    ENABLE_DYNAMIC_STOPS: bool = True
    ENABLE_PARTIAL_TAKE_TRAIL: bool = True  # legacy alias — prefer ENABLE_DYNAMIC_STOPS

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

    # ── AI commentary dashboard (TA2) ───────────────────────────────────────
    # Display-only "live analyst" commentary over open positions, watchlist
    # setups, and market conditions.  Unlike the AI veto (fail-closed), this
    # layer is fail-OPEN: on any error it renders deterministic template prose
    # from the same computed facts, because commentary influences no order.
    # Uses OpenRouter free models; at most 3 LLM calls per refresh cycle
    # (one batched prompt per panel), hard-capped per day.
    AI_COMMENTARY_ENABLED: bool = True
    AI_COMMENTARY_MODEL: str = "openai/gpt-oss-20b:free"
    AI_COMMENTARY_INTERVAL_MINUTES: int = 5
    AI_COMMENTARY_MAX_CALLS_PER_DAY: int = 150
    # Only the top-N watchlist symbols (ranked signal > near_entry > rejected)
    # get indicator recomputation + LLM prose; the rest render computed data.
    AI_COMMENTARY_WATCHLIST_LIMIT: int = 10
    # Skip scheduled refreshes when no client has polled within this window
    # (no browser open -> no provider/LLM spend).
    AI_COMMENTARY_IDLE_SUPPRESS_MINUTES: int = 15

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
    # "polygon" uses the Polygon.io REST API (requires POLYGON_API_KEY).
    # The provider can be switched at runtime from the dashboard, which writes
    # the choice back to .env and restarts the engine.
    MARKET_DATA_PROVIDER: str = "yfinance"
    ALPACA_API_KEY: str = ""
    ALPACA_API_SECRET: str = ""
    ALPACA_DATA_FEED: str = "iex"  # "iex" (free) or "sip" (paid)
    POLYGON_API_KEY: str = ""

    # ── Fallback market-data provider ───────────────────────────────────────
    # The primary provider (above) is wrapped so that when it fails or is
    # rate-limited, the fetch transparently falls back to this secondary
    # backend.  This is essential on free API tiers: Polygon's free plan caps
    # at ~5 requests/minute and returns HTTP 429 well before a 41-symbol scan
    # completes, which starves the screener of data and produces ZERO signals
    # (and therefore zero trades).  Yahoo Finance has no such per-minute cap,
    # so it makes a reliable free fallback.  Set empty ("") to disable the
    # fallback entirely.  Ignored when it equals the primary provider.
    MARKET_DATA_FALLBACK_PROVIDER: str = "yfinance"
    # After this many *consecutive* primary failures the circuit breaker trips
    # and routes every fetch straight to the fallback for a cooldown, so a
    # rate-limited primary is not hammered once per symbol for the whole scan.
    PROVIDER_FALLBACK_TRIP_THRESHOLD: int = 3
    PROVIDER_FALLBACK_COOLDOWN_SECONDS: float = 300.0  # 5 minutes

    @property
    def alpaca_keys_present(self) -> bool:
        """Return whether both Alpaca API credentials are configured."""
        return bool(self.ALPACA_API_KEY and self.ALPACA_API_SECRET)

    @property
    def polygon_key_present(self) -> bool:
        """Return whether a Polygon.io API key is configured."""
        return bool(self.POLYGON_API_KEY)

    @model_validator(mode="after")
    def _load_keys_from_files(self) -> "Settings":
        """Fill mapped API-key settings from ``keys/`` files when unset.

        An explicit environment variable or ``.env`` value always wins; the
        loose file is only consulted when the setting is still empty.  Disabled
        when ``USTB_SKIP_KEY_FILES`` is set (keeps tests independent of the
        developer's ``keys/`` directory).
        """
        if os.environ.get("USTB_SKIP_KEY_FILES"):
            return self
        for field_name, filename in _KEY_FILES.items():
            if getattr(self, field_name, ""):
                continue  # env / .env / explicit value takes precedence
            path = _KEYS_DIR / filename
            try:
                if path.is_file():
                    value = path.read_text(encoding="utf-8").strip()
                    if value:
                        setattr(self, field_name, value)
            except OSError:
                continue
        return self

    # ── Data cache TTLs (seconds) ───────────────────────────────────────────
    # Fetched data is memoised in a thread-safe TTL cache to eliminate the
    # ~160 redundant Yahoo calls per scan cycle.  Current price is cached
    # briefly; OHLCV history (daily bars) is stable for far longer.
    PRICE_CACHE_TTL_SECONDS: float = 300.0  # 5 minutes
    OHLCV_CACHE_TTL_SECONDS: float = 300.0  # 5 minutes (was 1 hour; reduced so stop-loss checks see fresh bar data)
    DATA_CACHE_ENABLED: bool = True

    # ── Dashboard quote service (monitoring F1) ─────────────────────────────
    # The dashboard's shared quote service serves current price / prev-close /
    # day-change from its own short-TTL cache so live-P&L polling never turns
    # into one provider call per poll per symbol.
    QUOTE_CACHE_TTL_SECONDS: float = 15.0

    # ── Position proximity alerts (monitoring F3/F6) ────────────────────────
    # Warn (UI highlight + optional alert) when the current price is within
    # this percentage of a position's stop or target (1.0 == 1%).
    POSITION_PROXIMITY_ALERT_PCT: float = 1.0

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

    # ── Dynamic stop-loss configuration ─────────────────────────────────────
    # Four complementary stop mechanisms, each independently toggleable and
    # overridable per strategy (see STOP_OVERRIDES_BY_STRATEGY).  All of them
    # only ever *ratchet the stop up* — a computed stop below the current stop
    # is ignored, so protection never loosens.
    #
    #   * Trailing stop — trail price by TRAIL_ATR_MULTIPLIER x ATR once the
    #     trade is at least TRAIL_ACTIVATION_PROFIT_PCT in profit.
    #   * Breakeven stop — move the stop to entry (+ a small buffer) once the
    #     trade has earned BREAKEVEN_TRIGGER_R times its initial risk (1R).
    #   * Time-based tightening — if a trade has been open at least
    #     TIME_STOP_TIGHTEN_DAYS days and gone nowhere (unrealised move below
    #     TIME_STOP_STAGNANT_PROFIT_PCT), tighten the trail to
    #     TIME_STOP_TIGHTEN_ATR_MULTIPLIER x ATR to free the capital sooner.
    #   * Volatility-adjusted — the trail distance is a multiple of ATR rather
    #     than a fixed percentage, so it widens in volatile names and tightens
    #     in quiet ones automatically.
    ENABLE_TRAILING_STOP: bool = True
    TRAIL_ATR_MULTIPLIER: float = 2.0
    TRAIL_ACTIVATION_PROFIT_PCT: float = 0.05
    ENABLE_BREAKEVEN_STOP: bool = True
    BREAKEVEN_TRIGGER_R: float = 1.0
    BREAKEVEN_BUFFER_PCT: float = 0.001
    ENABLE_TIME_STOP_TIGHTENING: bool = True
    TIME_STOP_TIGHTEN_DAYS: int = 5
    TIME_STOP_TIGHTEN_ATR_MULTIPLIER: float = 1.0
    TIME_STOP_STAGNANT_PROFIT_PCT: float = 0.02
    ENABLE_VOLATILITY_STOPS: bool = True
    STOP_ATR_PERIOD: int = 14
    # Per-strategy overrides for any of the dynamic-stop settings above.  Keys
    # are strategy names (lowercase); values are dicts of {setting_name: value}
    # applied on top of the global defaults.  Example::
    #     {"mean_reversion": {"TRAIL_ATR_MULTIPLIER": 1.5,
    #                         "ENABLE_TIME_STOP_TIGHTENING": True,
    #                         "TIME_STOP_TIGHTEN_DAYS": 3}}
    STOP_OVERRIDES_BY_STRATEGY: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # ── Advanced order execution ────────────────────────────────────────────
    # These enrich the basic bracket order and are honoured by both the
    # PaperBroker and the IBKRBroker.
    #
    #   * Limit-order expiry — a resting entry that only fills when the market
    #     trades at or below the limit, and is auto-cancelled after
    #     LIMIT_ORDER_EXPIRY_HOURS if still unfilled.
    #   * Scale-in — split an entry into SCALE_IN_TRANCHES tranches spaced
    #     SCALE_IN_STEP_PCT apart, so the average fill improves on a pullback.
    #   * Partial profit-taking — sell PARTIAL_TAKE_PCT of the position at a
    #     first target (PARTIAL_TAKE_TARGET_R times risk) and let the remainder
    #     run under the dynamic trailing stop.
    #   * Market-on-close — submit the entry as a MOC order so it fills at the
    #     closing auction, avoiding intraday noise for end-of-day setups.
    LIMIT_ORDER_EXPIRY_HOURS: float = 4.0
    ENABLE_SCALE_IN: bool = False
    SCALE_IN_TRANCHES: int = 3
    SCALE_IN_STEP_PCT: float = 0.01
    ENABLE_PARTIAL_TAKE: bool = True
    PARTIAL_TAKE_PCT: float = 0.5
    PARTIAL_TAKE_TARGET_R: float = 1.0
    # When enabled, signals that arrive inside the order-cutoff window (too
    # close to the close for a clean intraday entry) are submitted as
    # market-on-close orders instead of being skipped.
    ENABLE_MOC_ENTRIES: bool = False

    # ── Mode switching (paper ⇄ live) ───────────────────────────────────────
    # The dashboard can flip BROKER/IBKR_PORT and ask the engine to restart.
    # Switching *to live* requires the admin password (DASHBOARD_PASSWORD).
    ALLOW_MODE_SWITCH: bool = True

    # ── Email alerts (SMTP) ─────────────────────────────────────────────────
    EMAIL_ALERTS_ENABLED: bool = False
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    EMAIL_FROM: str = ""
    EMAIL_TO: str = ""

    # ── Alert routing & thresholds ──────────────────────────────────────────
    ALERT_ON_ENTRY: bool = True
    ALERT_ON_EXIT: bool = True
    ALERT_ON_MODE_SWITCH: bool = True
    # Fire a drawdown alert when peak-to-trough equity drawdown exceeds this
    # fraction; fire a daily-loss alert when the day's loss exceeds this
    # fraction of total capital.  Each alert is de-duplicated per session.
    ALERT_DRAWDOWN_PCT: float = 0.05
    ALERT_DAILY_LOSS_PCT: float = 0.01

    # ── Multi-timeframe analysis ────────────────────────────────────────────
    # Confirm each daily signal against the weekly trend.  When enabled, a
    # daily long signal is only taken if the weekly trend is up (weekly close
    # above a rising WEEKLY_TREND_EMA_PERIOD-week EMA).
    ENABLE_MULTI_TIMEFRAME: bool = True
    WEEKLY_TREND_EMA_PERIOD: int = 30
    MTF_REQUIRE_WEEKLY_UPTREND: bool = True

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

    # ── ETF support (Feature 3) ─────────────────────────────────────────────
    # ETFs are diversified baskets: lower single-name idiosyncratic risk and
    # lower realised volatility than individual stocks.  They therefore get a
    # slightly larger risk budget and a larger notional cap, but must not be
    # vetoed purely for being calm — hence a lower ATR% floor.
    ETF_RISK_MODIFIER: float = 1.3          # risk-budget multiplier vs a stock
    ETF_NOTIONAL_CAP_PCT: float = 0.15      # max notional per ETF (vs 0.10 stock)
    STOCK_NOTIONAL_CAP_PCT: float = 0.10    # explicit single-name notional cap
    MIN_ATR_PCT_ETF: float = 0.008          # 0.8 % ATR floor for ETFs
    # Sector-rotation strategy: rank the 11 sector ETFs by relative strength vs
    # SPY and go long the top-N rotating leaders.  Off by default (a new
    # strategy competing for capital); also selectable from the Trade Selection
    # UI so the operator can enable it per their preference.
    SECTOR_ROTATION_ENABLED: bool = False
    SECTOR_ROTATION_TOP_N: int = 3
    SECTOR_ROTATION_LOOKBACK_DAYS: int = 63  # ~3 trading months

    # ── Minimum OHLCV rows for indicator calculation ────────────────────────
    MIN_OHLCV_ROWS: int = 200

    # ── OHLCV look-back window fetched per symbol ───────────────────────────
    # The screener needs at least MIN_OHLCV_ROWS bars (EMA-200 is the longest
    # indicator).  A "6mo" window only yields ~123 trading days — BELOW the
    # 200-row minimum — which silently rejected *every* symbol at the row check
    # and produced zero signals (and therefore zero trades).  "2y" (~500 bars)
    # gives comfortable headroom above the minimum for all strategies.
    OHLCV_FETCH_PERIOD: str = "2y"

    # ── Watchlist management ────────────────────────────────────────────────
    # When enabled the engine scans the user-managed watchlists persisted to
    # ``DATA_DIR/watchlists.json`` (organised into named lists such as "tech" or
    # "energy") instead of the hard-coded universe.  If the file is missing the
    # store seeds itself from the built-in US/CA universe, so behaviour is
    # unchanged until the user edits their lists from the dashboard.
    USE_WATCHLIST_FILE: bool = True

    # ── Tiered scanning (Full Stock Universe) ──────────────────────────────
    # When the universe database exists (data_store/universe.db), the engine
    # uses a three-tier scanning architecture to cover thousands of symbols
    # efficiently.  Tier 1 (active watchlist) runs every cycle; Tier 2 rotates
    # through sectors; Tier 3 sweeps the full universe daily.
    TIERED_SCANNING_ENABLED: bool = True
    # Tier 1: user's active watchlist — scanned every cycle.
    TIER1_WORKERS: int = 4
    # Tier 2: sector rotation — 1-2 sectors per cycle, rotated through all.
    TIER2_ENABLED: bool = True
    TIER2_WORKERS: int = 8
    TIER2_INTERVAL_MINUTES: int = 30
    # Tier 3: full universe sweep — once daily (lightweight pre-screen).
    TIER3_ENABLED: bool = True
    TIER3_WORKERS: int = 16
    TIER3_PRESCREEN_PRICE_CHANGE_PCT: float = 0.03  # 3% daily move
    TIER3_PRESCREEN_VOLUME_RATIO: float = 2.0       # 2x avg volume
    # Batch data fetching for large symbol sets.
    BATCH_DOWNLOAD_SIZE: int = 50

    # ── News sentiment filter (Finnhub) ─────────────────────────────────────
    # A free-tier Finnhub key (https://finnhub.io) fetches recent company news;
    # the built-in headline scorer rejects an entry when the average sentiment
    # over the lookback window is below NEWS_SENTIMENT_MIN_SCORE.  Disabled by
    # default so the bot runs with zero extra configuration.
    NEWS_SENTIMENT_ENABLED: bool = False
    FINNHUB_API_KEY: str = ""
    NEWS_LOOKBACK_DAYS: int = 3
    NEWS_SENTIMENT_MIN_SCORE: float = -0.15
    NEWS_MIN_ARTICLES: int = 2
    NEWS_CACHE_TTL_MINUTES: float = 30.0

    # ── Earnings filter + results (Features 1, 2) ───────────────────────────
    # A first-class, configurable pre-earnings gate that supersedes the coarse
    # 14-day AI-layer blackout.  ``block`` rejects new entries within
    # EARNINGS_BLOCK_DAYS of a scheduled report; ``flag`` lets the trade through
    # but annotates it for the dashboard; ``off`` disables the gate (the AI
    # blackout still applies).  PEAD is always exempt (it trades the drift).
    EARNINGS_FILTER_MODE: str = "off"      # off | flag | block
    EARNINGS_BLOCK_DAYS: int = 2
    # Earnings *results* (beat/miss, EPS/revenue surprise) from Finnhub, used by
    # the beat-aware PEAD signal and the daily earnings tracker.  Fail-open.
    EARNINGS_RESULTS_ENABLED: bool = False
    EARNINGS_RESULTS_CACHE_TTL_MINUTES: float = 360.0  # 6 h
    # Optional Financial Modeling Prep key (richer earnings/ratings source).
    FMP_API_KEY: str = ""
    # Daily earnings tracker (Feature 2): same-sector contagion alert fires when
    # a bellwether's EPS surprise exceeds this magnitude (percent).
    CONTAGION_SURPRISE_THRESHOLD: float = 5.0
    EARNINGS_HISTORY_ENABLED: bool = True

    # ── Third-party ratings filter (Feature 5) ──────────────────────────────
    # An entry filter over a normalized quant rating (STRONG_BUY > BUY > HOLD >
    # SELL > STRONG_SELL) sourced from a clean, licensed API — Finnhub by
    # default (analyst recommendation trends + price targets; key already in
    # repo), FMP optionally.  Seeking Alpha is a documented, user-supplied
    # extension only (its ToS prohibit scraping) and is never shipped.  Off by
    # default and fail-open: a symbol with no coverage passes through.
    RATINGS_FILTER_ENABLED: bool = False
    RATINGS_PROVIDER: str = "finnhub"      # finnhub | fmp
    RATINGS_MIN: str = "hold"              # strong_sell | sell | hold | buy | strong_buy
    RATINGS_FAIL_OPEN: bool = True
    RATINGS_CACHE_TTL_MINUTES: float = 720.0  # 12 h (ratings change daily at most)

    # ── Market regime detection ─────────────────────────────────────────────
    # Classify the broad market as bull / bear / sideways from a benchmark's
    # moving-average structure and realised volatility, then scale each
    # strategy family's weight (momentum favoured in bull regimes, mean
    # reversion in bear/sideways).  Applied only when enabled.
    REGIME_DETECTION_ENABLED: bool = True
    REGIME_BENCHMARK: str = "SPY"
    REGIME_FAST_MA: int = 50
    REGIME_SLOW_MA: int = 200
    REGIME_VOL_WINDOW: int = 20
    REGIME_HIGH_VOL_PCT: float = 0.018

    # ── Strategy auto-tuning ────────────────────────────────────────────────
    # Nudge the grade thresholds up or down based on the recent hit-rate of the
    # last AUTOTUNE_LOOKBACK_TRADES closed trades: a cold streak raises the bar
    # (fewer, higher-quality entries); a hot streak relaxes it slightly.  The
    # adjustment is clamped to +/- AUTOTUNE_MAX_GRADE_ADJUST.  Off by default.
    AUTOTUNE_ENABLED: bool = False
    AUTOTUNE_LOOKBACK_TRADES: int = 30
    AUTOTUNE_MIN_TRADES: int = 15
    AUTOTUNE_MAX_GRADE_ADJUST: float = 0.08

    # ── Scheduler + automated reports ───────────────────────────────────────
    # A lightweight in-process scheduler (no external deps) that fires daily and
    # weekly P&L email reports and nightly backtests.  Each job is independently
    # toggleable; times are local "HH:MM" strings.
    SCHEDULER_ENABLED: bool = False
    PNL_REPORT_ENABLED: bool = False
    PNL_REPORT_DAILY_TIME: str = "17:00"
    PNL_REPORT_WEEKLY_ENABLED: bool = True
    PNL_REPORT_WEEKLY_DAY: str = "FRI"
    SCHEDULED_BACKTEST_ENABLED: bool = False
    SCHEDULED_BACKTEST_TIME: str = "02:00"
    SCHEDULED_BACKTEST_LOOKBACK_DAYS: int = 180

    # ── Pre-market scanner ──────────────────────────────────────────────────
    # Flag symbols gapping more than PREMARKET_GAP_PCT off the prior close or
    # trading at more than PREMARKET_VOLUME_RATIO times their average volume.
    PREMARKET_GAP_PCT: float = 0.02
    PREMARKET_VOLUME_RATIO: float = 1.5

    # ── Extended-hours data + overnight-gap filter (Feature 4) ──────────────
    # When enabled, the provider abstraction is asked for true pre/post-market
    # quotes (IBKR in live mode, else Alpaca IEX) instead of the daily-bar gap
    # proxy.  The gap filter then skips or resizes a morning entry when the
    # stock gapped sharply against the trade overnight.  All off by default and
    # fail-open: no extended-hours data means the entry proceeds untouched.
    EXTENDED_HOURS_ENABLED: bool = False
    EXT_HOURS_PROVIDER: str = "auto"        # auto | ibkr | alpaca
    EXT_HOURS_CACHE_TTL_SECONDS: float = 60.0
    GAP_FILTER_ENABLED: bool = False
    GAP_DOWN_SKIP_PCT: float = -0.05        # skip longs gapping <= -5% overnight
    GAP_UP_CHASE_PCT: float = 0.08          # skip longs already gapped up >= +8%
    GAP_RESIZE_PCT: float = 0.03            # resize (not skip) beyond this gap
    GAP_RESIZE_MODIFIER: float = 0.5        # size multiplier when resizing
    EXT_UNUSUAL_VOLUME_RATIO: float = 3.0

    # ── Monte Carlo projection ──────────────────────────────────────────────
    MONTE_CARLO_RUNS: int = 1000
    MONTE_CARLO_HORIZON: int = 50

    # ── Multi-user support ──────────────────────────────────────────────────
    # When enabled the dashboard exposes registration/login and stores per-user
    # accounts (with their own strategies, capital, and watchlists) in
    # ``DATA_DIR/users.json``.  The HTTP Basic admin remains a superuser.
    MULTI_USER_ENABLED: bool = False

    # ── REST API ────────────────────────────────────────────────────────────
    # A documented, API-key-authenticated JSON API under /api/v1.  Keys are
    # minted from the dashboard and stored (hashed) in ``DATA_DIR/api_keys.json``.
    REST_API_ENABLED: bool = True


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
    if strategy_lower in ("momentum", "vcp_breakout", "pead", "sector_rotation"):
        return momentum_weights
    if strategy_lower in ("swing", "mean_reversion"):
        return swing_weights
    raise ValueError(f"Unknown strategy: {strategy!r}")
