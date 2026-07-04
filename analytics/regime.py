"""
Market regime detection (feature 13).

Classifies the broad market as **bull**, **bear**, or **sideways** from a
benchmark's moving-average structure and realised volatility, then returns a
per-strategy-family weight multiplier so the engine can lean into momentum in
bull regimes and into mean reversion in bear/sideways regimes (and de-risk when
volatility is high).

:func:`detect_regime` is a pure function over an OHLCV frame; :func:`current_regime`
wires it to the live data fetcher for the configured benchmark.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

import pandas as pd

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Strategy families (mirrors config.settings.weights_for_strategy).
_MOMENTUM_FAMILY = {"momentum", "vcp_breakout", "pead"}
_SWING_FAMILY = {"swing", "mean_reversion"}

_MIN_MULT = 0.3
_MAX_MULT = 1.5


@dataclass
class RegimeResult:
    """Detected market regime and the strategy-family weight multipliers."""

    regime: str = "sideways"
    trend: str = "flat"
    volatility: str = "normal"
    fast_ma: float = 0.0
    slow_ma: float = 0.0
    realized_vol: float = 0.0
    weight_multipliers: Dict[str, float] = field(
        default_factory=lambda: {"momentum": 1.0, "swing": 1.0}
    )
    reason: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "regime": self.regime,
            "trend": self.trend,
            "volatility": self.volatility,
            "fast_ma": round(self.fast_ma, 4),
            "slow_ma": round(self.slow_ma, 4),
            "realized_vol": round(self.realized_vol, 6),
            "weight_multipliers": {k: round(v, 4) for k, v in self.weight_multipliers.items()},
            "reason": self.reason,
        }


def _clamp(x: float) -> float:
    return max(_MIN_MULT, min(_MAX_MULT, x))


# Base multipliers per regime, before the high-volatility risk-off scaling.
_BASE_MULTIPLIERS = {
    "bull": {"momentum": 1.2, "swing": 0.9},
    "bear": {"momentum": 0.6, "swing": 1.1},
    "sideways": {"momentum": 0.85, "swing": 1.1},
}


def detect_regime(df: pd.DataFrame, settings) -> RegimeResult:
    """Classify the market regime from an OHLCV *df*.  Pure; never raises."""
    slow_n = int(getattr(settings, "REGIME_SLOW_MA", 200))
    fast_n = int(getattr(settings, "REGIME_FAST_MA", 50))
    vol_n = int(getattr(settings, "REGIME_VOL_WINDOW", 20))
    high_vol = float(getattr(settings, "REGIME_HIGH_VOL_PCT", 0.018))

    if df is None or "Close" not in getattr(df, "columns", []) or len(df) < slow_n:
        return RegimeResult(
            regime="sideways",
            reason="insufficient history",
            weight_multipliers={"momentum": 1.0, "swing": 1.0},
        )

    close = df["Close"].astype(float)
    fast_ma = float(close.rolling(fast_n).mean().iloc[-1])
    slow_ma = float(close.rolling(slow_n).mean().iloc[-1])
    last = float(close.iloc[-1])
    realized_vol = float(close.pct_change().rolling(vol_n).std().iloc[-1])
    if realized_vol != realized_vol:  # NaN guard
        realized_vol = 0.0

    if last > slow_ma and fast_ma > slow_ma:
        regime, trend = "bull", "up"
    elif last < slow_ma and fast_ma < slow_ma:
        regime, trend = "bear", "down"
    else:
        regime, trend = "sideways", "flat"

    volatility = "high" if realized_vol >= high_vol else "normal"
    mult = dict(_BASE_MULTIPLIERS[regime])
    if volatility == "high":
        mult = {k: v * 0.85 for k, v in mult.items()}
    mult = {k: _clamp(v) for k, v in mult.items()}

    reason = (
        f"{regime} regime (last {last:.2f} vs slow MA {slow_ma:.2f}, "
        f"fast MA {fast_ma:.2f}); {volatility} volatility {realized_vol:.4f}"
    )
    return RegimeResult(
        regime=regime,
        trend=trend,
        volatility=volatility,
        fast_ma=fast_ma,
        slow_ma=slow_ma,
        realized_vol=realized_vol,
        weight_multipliers=mult,
        reason=reason,
    )


def current_regime(
    settings,
    fetcher: Optional[Callable[[str], Optional[pd.DataFrame]]] = None,
) -> RegimeResult:
    """Detect the current regime for the configured benchmark.  Never raises."""
    neutral = RegimeResult(
        regime="sideways",
        reason="regime detection disabled",
        weight_multipliers={"momentum": 1.0, "swing": 1.0},
    )
    if not getattr(settings, "REGIME_DETECTION_ENABLED", True):
        return neutral
    if fetcher is None:
        try:
            from data.fetcher import fetch_ohlcv as fetcher  # type: ignore
        except Exception:  # noqa: BLE001
            return neutral
    try:
        df = fetcher(getattr(settings, "REGIME_BENCHMARK", "SPY"))
    except Exception as exc:  # noqa: BLE001
        log.warning("regime.fetch_failed", error=str(exc))
        return neutral
    if df is None or len(df) == 0:
        return neutral
    return detect_regime(df, settings)


def multiplier_for_strategy(strategy: str, result: RegimeResult) -> float:
    """Return the weight multiplier for *strategy* under *result*."""
    s = str(strategy or "").lower()
    if s in _MOMENTUM_FAMILY:
        return result.weight_multipliers.get("momentum", 1.0)
    if s in _SWING_FAMILY:
        return result.weight_multipliers.get("swing", 1.0)
    return 1.0
