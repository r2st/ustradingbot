"""
Configuration for the short-selling strategy module.

Every tunable parameter is a dataclass field with a documented default (spec
section 9).  Per-strategy parameters are plain dataclasses; the top-level
operational knobs (master switch, caps, ATR stop/sizing, filter toggles) are
additionally overridable from the environment / ``.env`` via
:class:`ShortEnvSettings`, mirroring how the main ``config.settings`` works.

No new *required* environment variables: every value defaults safely, so a
deploy needs no prod ``.env`` edits.

Usage::

    from short_strategies.common.config import get_short_config
    cfg = get_short_config()          # cached singleton
    get_short_config.cache_clear()    # tests: re-read env
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field

from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Environment-overridable operational knobs
# ---------------------------------------------------------------------------


class ShortEnvSettings(BaseSettings):
    """Env-tunable top-level knobs (all optional; safe defaults)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    #: Master switch for the short scan in the engine cycle.
    SHORT_STRATEGIES_ENABLED: bool = True
    #: Strategy-family position cap (enforced by RiskManager.check_strategy_cap).
    SHORT_MAX_POSITIONS: int = 5
    #: Total short notional cap as a fraction of TOTAL_CAPITAL.
    SHORT_MAX_EXPOSURE_PCT: float = 0.25
    #: Scales the per-trade risk budget for shorts inside build_order.
    #: 0.65 x MAX_POSITION_SIZE_PCT (1.5%) ~= 1% account risk per trade,
    #: inside the spec's 0.5-1% band.
    SHORT_RISK_MODIFIER: float = 0.65
    #: ATR(14) multiple for the buy-stop above entry.
    SHORT_STOP_ATR_MULT: float = 1.5
    #: Reward:risk multiple for the cover target below entry.
    SHORT_TARGET_RR: float = 2.0
    #: No new short entries within this many days of upcoming earnings.
    SHORT_EARNINGS_BLACKOUT_DAYS: int = 2
    #: Require the benchmark (SPY) to be below trend before shorting.
    SHORT_REGIME_FILTER_ENABLED: bool = True
    #: Require strict bear regime (False allows sideways too).
    SHORT_REGIME_REQUIRE_BEAR: bool = False
    #: Apply the ADX confirmation filter to trend-following short setups.
    SHORT_ADX_CONFIRM_ENABLED: bool = True
    #: Reject when short % of float exceeds this (squeeze risk); 0 disables.
    SHORT_MAX_SHORT_PCT_FLOAT: float = 0.20
    #: When short-interest data is missing, pass (True) or reject (False).
    SHORT_INTEREST_FAIL_OPEN: bool = True


# ---------------------------------------------------------------------------
# Per-strategy parameter dataclasses (documented defaults)
# ---------------------------------------------------------------------------


@dataclass
class SupportBreakdownConfig:
    """Strategy 1 — support breakdown with volume confirmation."""

    #: Close must be at least this fraction below the support level.
    break_pct: float = 0.002
    #: Today's volume / 20-day average must be at least this.
    volume_ratio_min: float = 1.5
    #: Minimum pivot touches for a support level to count as significant.
    min_touches: int = 2
    #: Trailing bars scanned for support pivots.
    lookback_bars: int = 120


@dataclass
class MaCrossunderConfig:
    """Strategy 2 — fast EMA crosses below slow SMA."""

    fast_ema_span: int = 20
    slow_sma_window: int = 50
    #: The crossunder must have happened within this many bars.
    cross_within_bars: int = 3
    #: Price must also close below both averages.
    require_close_below: bool = True


@dataclass
class BearFlagConfig:
    """Strategy 3 — bear flag continuation."""

    #: Flagpole: minimum decline over at most pole_max_bars.
    pole_min_drop_pct: float = 0.08
    pole_max_bars: int = 10
    #: Flag consolidation length bounds.
    flag_min_bars: int = 3
    flag_max_bars: int = 15
    #: Flag may retrace at most this fraction of the pole.
    flag_max_retrace: float = 0.5
    #: Entry: close below the flag low by this fraction.
    break_pct: float = 0.001


@dataclass
class AdxFilterConfig:
    """Strategy 4 — ADX trend-strength confirmation."""

    period: int = 14
    #: Minimum ADX for a confirmed trend.
    adx_min: float = 20.0
    #: Standalone detector mode (off: filter-only, per spec section 6).
    standalone_enabled: bool = False


@dataclass
class RelativeWeaknessConfig:
    """Strategy 5 — sector/peer relative-strength ranking."""

    #: Trailing return window used for the ranking.
    rank_lookback_days: int = 20
    #: Short the bottom fraction of the (sector) ranking.
    bottom_decile: float = 0.10
    #: Sectors with fewer members fall back to the whole scan universe.
    min_peers: int = 5
    #: Candidate must also be below its 50-day SMA.
    require_below_ma: bool = True
    ma_window: int = 50


@dataclass
class LaggardFadeConfig:
    """Strategy 6 — laggard fade on market pullback days."""

    #: Benchmark day return must be at or below -market_down_pct.
    market_down_pct: float = 0.01
    #: Symbol must underperform the benchmark's day return by at least this.
    underperform_pct: float = 0.01
    #: Close must sit in the bottom fraction of the day's range.
    close_range_max: float = 0.35


@dataclass
class OverboughtFadeConfig:
    """Strategy 7 — overbought momentum fade."""

    rsi_min: float = 75.0
    #: Upper wick must be at least this multiple of the candle body.
    wick_body_ratio: float = 1.5
    #: Close must land in the lower fraction of the day's range.
    close_range_max: float = 0.5
    #: The high must come within this fraction of a resistance level
    #: (0 disables the resistance requirement).
    resistance_proximity_pct: float = 0.02


@dataclass
class GapFailConfig:
    """Strategy 8 — failed gap-up ('gap and crap')."""

    #: Open must gap up at least this fraction over the prior close.
    gap_min_pct: float = 0.03
    #: Close must be below the open (opening range failed).
    require_below_open: bool = True
    #: Additionally require close below the prior close.
    require_below_prior_close: bool = False


@dataclass
class EarningsPopFadeConfig:
    """Strategy 9 — post-earnings pop fade."""

    #: Earnings must have occurred within this many days back.
    days_after_earnings: int = 3
    #: The earnings-day gap must be at least this fraction.
    gap_min_pct: float = 0.05
    #: The fade must give back at least this fraction of the gap.
    fade_min_pct: float = 0.5


@dataclass
class VwapRejectionConfig:
    """Strategy 10 — VWAP rejection short in a downtrend."""

    #: Rolling daily-bar VWAP window (fallback intraday-VWAP approximation).
    vwap_window: int = 20
    #: Close must finish at least this fraction below the VWAP.
    reject_close_pct: float = 0.005
    #: Trend classification MA parameters (established downtrend required).
    fast_ema_span: int = 20
    slow_sma_window: int = 50
    #: Prefer a true intraday VWAP (from real intraday bars) when a fetcher is
    #: injected; falls back to the daily ``rolling_vwap`` approximation.
    use_intraday: bool = True
    #: Bar grain for the intraday VWAP fetch (5m/15m are the practical choices).
    intraday_interval: str = "5m"
    #: Look-back window for the intraday fetch (clamped by the provider tier).
    intraday_period: str = "5d"


@dataclass
class BuyingClimaxConfig:
    """Strategy 11 — buying climax / distribution-day reversal."""

    #: Minimum advance over advance_lookback bars before the climax.
    advance_min_pct: float = 0.15
    advance_lookback: int = 40
    #: Climax-day volume must be at least this multiple of the 20-day average.
    climax_volume_ratio: float = 3.0
    #: How many bars back the climax day may sit (reversal day is later).
    climax_within_bars: int = 3


@dataclass
class SharedFilterConfig:
    """Section 7 — shared risk/execution filter parameters."""

    max_short_pct_float: float = 0.20
    short_interest_fail_open: bool = True
    regime_filter_enabled: bool = True
    regime_require_bear: bool = False
    stop_atr_mult: float = 1.5
    target_rr: float = 2.0
    earnings_blackout_days: int = 2
    max_short_exposure_pct: float = 0.25
    max_short_positions: int = 5
    #: Structural (detector-supplied) stops are kept only when within this
    #: multiple of ATR from entry; otherwise the ATR stop replaces them.
    max_stop_atr_mult: float = 2.5
    #: Minimum ATR%-of-price for a short candidate (mirrors MIN_ATR_PCT).
    min_atr_pct: float = 0.015


@dataclass
class ShortModuleConfig:
    """Aggregated configuration for the whole module."""

    enabled: bool = True
    risk_modifier: float = 0.65
    adx_confirm_enabled: bool = True
    filters: SharedFilterConfig = field(default_factory=SharedFilterConfig)
    support_breakdown: SupportBreakdownConfig = field(default_factory=SupportBreakdownConfig)
    ma_crossunder: MaCrossunderConfig = field(default_factory=MaCrossunderConfig)
    bear_flag: BearFlagConfig = field(default_factory=BearFlagConfig)
    adx_filter: AdxFilterConfig = field(default_factory=AdxFilterConfig)
    relative_weakness: RelativeWeaknessConfig = field(default_factory=RelativeWeaknessConfig)
    laggard_fade: LaggardFadeConfig = field(default_factory=LaggardFadeConfig)
    overbought_fade: OverboughtFadeConfig = field(default_factory=OverboughtFadeConfig)
    gap_fail: GapFailConfig = field(default_factory=GapFailConfig)
    earnings_pop_fade: EarningsPopFadeConfig = field(default_factory=EarningsPopFadeConfig)
    vwap_rejection: VwapRejectionConfig = field(default_factory=VwapRejectionConfig)
    buying_climax: BuyingClimaxConfig = field(default_factory=BuyingClimaxConfig)


@functools.lru_cache(maxsize=1)
def get_short_config() -> ShortModuleConfig:
    """Return the cached module config, with env overrides applied.

    The dataclass defaults are the single source of truth for strategy
    parameters; the environment can override only the operational knobs
    (see :class:`ShortEnvSettings`).  Call ``get_short_config.cache_clear()``
    in tests after mutating the environment.
    """
    env = ShortEnvSettings()
    cfg = ShortModuleConfig()
    cfg.enabled = env.SHORT_STRATEGIES_ENABLED
    cfg.risk_modifier = env.SHORT_RISK_MODIFIER
    cfg.adx_confirm_enabled = env.SHORT_ADX_CONFIRM_ENABLED
    f = cfg.filters
    f.max_short_positions = env.SHORT_MAX_POSITIONS
    f.max_short_exposure_pct = env.SHORT_MAX_EXPOSURE_PCT
    f.stop_atr_mult = env.SHORT_STOP_ATR_MULT
    f.target_rr = env.SHORT_TARGET_RR
    f.earnings_blackout_days = env.SHORT_EARNINGS_BLACKOUT_DAYS
    f.regime_filter_enabled = env.SHORT_REGIME_FILTER_ENABLED
    f.regime_require_bear = env.SHORT_REGIME_REQUIRE_BEAR
    f.max_short_pct_float = env.SHORT_MAX_SHORT_PCT_FLOAT
    f.short_interest_fail_open = env.SHORT_INTEREST_FAIL_OPEN
    return cfg
