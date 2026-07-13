"""
Configuration for the highly selective strategy module.

Every tunable parameter is a dataclass field with a documented default.
Per-strategy parameters are plain dataclasses; the top-level operational
knobs (master switch, caps, risk modifier) are additionally overridable
from the environment / ``.env`` via :class:`SelectiveEnvSettings`,
mirroring how the main ``config.settings`` and ``short_strategies`` work.

No new *required* environment variables: every value defaults safely, so a
deploy needs no prod ``.env`` edits.

Usage::

    from selective_strategies.config import get_selective_config
    cfg = get_selective_config()          # cached singleton
    get_selective_config.cache_clear()    # tests: re-read env
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field

from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Environment-overridable operational knobs
# ---------------------------------------------------------------------------


class SelectiveEnvSettings(BaseSettings):
    """Env-tunable top-level knobs (all optional; safe defaults)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    #: Master switch for the selective scan in the engine cycle.
    SELECTIVE_STRATEGIES_ENABLED: bool = True
    #: Strategy-family position cap.
    SELECTIVE_MAX_POSITIONS: int = 3
    #: Scales the per-trade risk budget for selective strategies inside
    #: build_order.  0.75 x MAX_POSITION_SIZE_PCT gives reduced risk per
    #: trade, reflecting higher selectivity / lower frequency.
    SELECTIVE_RISK_MODIFIER: float = 0.75


# ---------------------------------------------------------------------------
# Per-strategy parameter dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RSI2ReversalConfig:
    """RSI(2) mean-reversion near horizontal support."""

    rsi_threshold: float = 5
    support_proximity_pct: float = 0.005
    min_support_touches: int = 2
    support_lookback_days: int = 60
    volume_ratio_min: float = 1.2
    take_profit_rsi: float = 65
    stop_atr_mult: float = 1.5
    time_stop_days: int = 6


@dataclass
class TripleTimeframeConfig:
    """Triple time-frame momentum alignment breakout."""

    sma_fast: int = 50
    sma_slow: int = 200
    slope_lookback: int = 10
    consolidation_bars: int = 15
    breakout_volume_ratio: float = 1.5
    chandelier_atr_period: int = 22
    chandelier_atr_mult: float = 3.0
    event_blackout_days: int = 2


@dataclass
class BBClimaxConfig:
    """Bollinger Band climax reversal."""

    bb_period: int = 20
    bb_std: float = 2.0
    volume_percentile_threshold: int = 95
    volume_lookback: int = 100
    accel_days: int = 3
    target_bb_middle: bool = True
    stop_atr_mult: float = 0.25
    time_stop_days: int = 5


@dataclass
class PEADDriftConfig:
    """Post-Earnings Announcement Drift continuation."""

    min_gap_pct: float = 0.03
    min_volume_ratio: float = 2.0
    min_close_range_pct: float = 0.67
    hold_days: int = 10
    stop_atr_mult: float = 2.0


@dataclass
class GapFillConfig:
    """Overnight gap-fill reversion."""

    gap_min_pct: float = 0.003
    gap_max_pct: float = 0.01
    max_body_pct: float = 0.30
    stop_buffer_atr_mult: float = 0.1
    #: Prefer a real intraday ATR (from intraday bars) for the stop buffer when
    #: a fetcher is injected; falls back to the ``daily ATR / 5`` proxy.
    use_intraday: bool = True
    #: Bar grain for the intraday ATR fetch.
    intraday_interval: str = "5m"
    #: Look-back window for the intraday fetch (clamped by the provider tier).
    intraday_period: str = "5d"
    #: Divisor turning a daily ATR into a 5-min-ish proxy when intraday is
    #: unavailable (5m bars are ~1/5 the range of a daily bar, empirically).
    daily_atr_intraday_divisor: float = 5.0


@dataclass
class TurnaroundTuesdayConfig:
    """Turnaround Tuesday mean-reversion pattern."""

    min_monday_drop_pct: float = 0.01
    max_ibs: float = 0.2
    require_trend_filter: bool = True
    sma_period: int = 200
    hard_stop_pct: float = 0.02


# ---------------------------------------------------------------------------
# Master configuration
# ---------------------------------------------------------------------------


@dataclass
class SelectiveConfig:
    """Top-level configuration for all selective strategies.

    Composed of per-strategy configs plus operational knobs read from the
    environment via :class:`SelectiveEnvSettings`.
    """

    enabled: bool = True
    risk_modifier: float = 0.75
    max_positions: int = 3

    rsi2_reversal: RSI2ReversalConfig = field(default_factory=RSI2ReversalConfig)
    triple_timeframe: TripleTimeframeConfig = field(default_factory=TripleTimeframeConfig)
    bb_climax: BBClimaxConfig = field(default_factory=BBClimaxConfig)
    pead_drift: PEADDriftConfig = field(default_factory=PEADDriftConfig)
    gap_fill: GapFillConfig = field(default_factory=GapFillConfig)
    turnaround_tuesday: TurnaroundTuesdayConfig = field(
        default_factory=TurnaroundTuesdayConfig
    )


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def get_selective_config() -> SelectiveConfig:
    """Build (and cache) the module configuration from environment settings.

    Call ``get_selective_config.cache_clear()`` in tests to force a re-read.
    """
    env = SelectiveEnvSettings()
    return SelectiveConfig(
        enabled=env.SELECTIVE_STRATEGIES_ENABLED,
        risk_modifier=env.SELECTIVE_RISK_MODIFIER,
        max_positions=env.SELECTIVE_MAX_POSITIONS,
    )
