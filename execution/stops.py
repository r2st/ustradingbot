"""
Dynamic stop-loss engine.

The exit manager uses this module to compute where an open position's stop
*should* be on the current bar.  Four complementary mechanisms are supported,
each independently toggleable and overridable per strategy:

* **Trailing stop** — ratchet the stop up to ``price - k*ATR`` once the trade
  is meaningfully in profit.  Because the distance is a multiple of ATR it is
  inherently *volatility-adjusted*: wide in fast names, tight in quiet ones.
* **Breakeven stop** — once the trade has earned ``BREAKEVEN_TRIGGER_R`` times
  its initial risk (1R by default), lift the stop to entry plus a small buffer
  so the trade can no longer become a loser.
* **Time-based tightening** — if a position has been open for at least
  ``TIME_STOP_TIGHTEN_DAYS`` days and gone nowhere, tighten the trail multiple
  so stagnant capital is freed sooner.
* **Volatility-adjusted** — all ATR-based distances scale with realised
  volatility automatically; this flag simply gates whether ATR sizing is used
  at all (vs. a fixed-percentage fallback).

Every mechanism only ever proposes a stop *above* the current one — the
:class:`StopDecision` returned by :func:`compute_dynamic_stop` is applied by the
caller through the broker, which itself refuses to lower a stop.  This keeps the
"stops never loosen" invariant intact regardless of configuration.

All functions are pure and take explicit values or an OHLCV frame, so they are
trivially unit-testable without a broker or network.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd

from config.settings import Settings


# ---------------------------------------------------------------------------
# Resolved per-strategy configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StopConfig:
    """Resolved dynamic-stop parameters for a single strategy.

    Built by :func:`resolve_stop_config`, which layers any per-strategy
    overrides from ``settings.STOP_OVERRIDES_BY_STRATEGY`` on top of the global
    settings defaults.
    """

    enable_trailing: bool = True
    trail_atr_multiplier: float = 2.0
    trail_activation_profit_pct: float = 0.05
    enable_breakeven: bool = True
    breakeven_trigger_r: float = 1.0
    breakeven_buffer_pct: float = 0.001
    enable_time_tighten: bool = True
    time_tighten_days: int = 5
    time_tighten_atr_multiplier: float = 1.0
    time_stagnant_profit_pct: float = 0.02
    enable_volatility_stops: bool = True
    atr_period: int = 14


# Map StopConfig fields to the settings attribute that seeds them.
_SETTING_FOR_FIELD: Dict[str, str] = {
    "enable_trailing": "ENABLE_TRAILING_STOP",
    "trail_atr_multiplier": "TRAIL_ATR_MULTIPLIER",
    "trail_activation_profit_pct": "TRAIL_ACTIVATION_PROFIT_PCT",
    "enable_breakeven": "ENABLE_BREAKEVEN_STOP",
    "breakeven_trigger_r": "BREAKEVEN_TRIGGER_R",
    "breakeven_buffer_pct": "BREAKEVEN_BUFFER_PCT",
    "enable_time_tighten": "ENABLE_TIME_STOP_TIGHTENING",
    "time_tighten_days": "TIME_STOP_TIGHTEN_DAYS",
    "time_tighten_atr_multiplier": "TIME_STOP_TIGHTEN_ATR_MULTIPLIER",
    "time_stagnant_profit_pct": "TIME_STOP_STAGNANT_PROFIT_PCT",
    "enable_volatility_stops": "ENABLE_VOLATILITY_STOPS",
    "atr_period": "STOP_ATR_PERIOD",
}

# Accept overrides keyed either by StopConfig field name or by the SETTINGS
# constant name (both forms are convenient in a config file).
_OVERRIDE_ALIASES: Dict[str, str] = {
    **{s: f for f, s in _SETTING_FOR_FIELD.items()},
    **{f: f for f in _SETTING_FOR_FIELD},
}


def resolve_stop_config(settings: Settings, strategy: str) -> StopConfig:
    """Return the :class:`StopConfig` for *strategy*.

    Starts from the global settings values, then applies any per-strategy
    override dict found under ``settings.STOP_OVERRIDES_BY_STRATEGY``.  Unknown
    override keys are ignored so a typo can never crash the exit loop.
    """
    base: Dict[str, Any] = {
        field.name: getattr(settings, _SETTING_FOR_FIELD[field.name])
        for field in fields(StopConfig)
    }

    overrides = settings.STOP_OVERRIDES_BY_STRATEGY.get(strategy.lower(), {})
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            field_name = _OVERRIDE_ALIASES.get(key)
            if field_name is not None:
                base[field_name] = value

    return StopConfig(**base)


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Return ATR(*period*) from an OHLCV frame, or ``0.0`` when undefined."""
    if df is None or len(df) < period + 1:
        return 0.0
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    val = tr.rolling(window=period).mean().iloc[-1]
    return float(val) if not pd.isna(val) else 0.0


# ---------------------------------------------------------------------------
# Decision object
# ---------------------------------------------------------------------------


@dataclass
class StopDecision:
    """The outcome of evaluating dynamic stops for one position.

    Attributes:
        new_stop: The proposed stop price (always above the current stop).
        reason: Which mechanism produced it (``"trailing"``, ``"breakeven"``,
            ``"time_tighten"``).
        raised: Whether a raise is proposed at all.
    """

    new_stop: float
    reason: str
    raised: bool = True


# ---------------------------------------------------------------------------
# Individual mechanisms (pure)
# ---------------------------------------------------------------------------


def trailing_stop(current_price: float, atr: float, multiplier: float) -> float:
    """Return the trailing stop ``price - multiplier * ATR``."""
    return current_price - multiplier * atr


def breakeven_stop(entry_price: float, buffer_pct: float) -> float:
    """Return the breakeven stop: entry plus a small protective buffer."""
    return entry_price * (1.0 + buffer_pct)


def reached_r_multiple(
    entry_price: float,
    current_price: float,
    initial_stop: float,
    trigger_r: float,
) -> bool:
    """Return whether unrealised gain has reached *trigger_r* times initial risk."""
    initial_risk = entry_price - initial_stop
    if initial_risk <= 0:
        return False
    gain = current_price - entry_price
    return (gain / initial_risk) >= trigger_r


def days_between(entry_time: Any, now: datetime) -> Optional[float]:
    """Return calendar days between *entry_time* and *now*, or ``None``."""
    if not entry_time:
        return None
    if isinstance(entry_time, datetime):
        entry_dt = entry_time
    else:
        try:
            entry_dt = datetime.fromisoformat(str(entry_time))
        except (ValueError, TypeError):
            return None
    return (now - entry_dt).total_seconds() / 86_400.0


# ---------------------------------------------------------------------------
# Top-level evaluation
# ---------------------------------------------------------------------------


def compute_dynamic_stop(
    position: Dict[str, Any],
    df: pd.DataFrame,
    config: StopConfig,
    now: Optional[datetime] = None,
) -> Optional[StopDecision]:
    """Compute the highest (most protective) stop for *position*, or ``None``.

    Evaluates every enabled mechanism, keeps the *highest* candidate stop that
    is strictly above the position's current stop, and returns it as a
    :class:`StopDecision`.  Returns ``None`` when nothing would raise the stop.

    Args:
        position: Open-position dict with ``entry_price``, ``stop_price``,
            optionally ``original_stop_loss`` and ``entry_time``.
        df: Recent OHLCV for the symbol (needs enough rows for ATR).
        config: Resolved :class:`StopConfig` for the position's strategy.
        now: Reference time for the time-based mechanism (defaults to now).
    """
    now = now or datetime.now()

    entry = _f(position.get("entry_price"))
    current_stop = _f(position.get("stop_price"))
    # Prefer the entry-time stop for R calculations so a raised stop does not
    # shrink the measured initial risk.
    initial_stop = _f(position.get("original_stop_loss"), default=current_stop)
    if entry <= 0:
        return None

    if df is None or df.empty:
        return None
    current_price = float(df["Close"].iloc[-1])
    if current_price <= 0:
        return None

    atr = compute_atr(df, config.atr_period) if config.enable_volatility_stops else 0.0
    gain_pct = (current_price - entry) / entry

    candidates: list[tuple[float, str]] = []

    # 1) Trailing stop — only once sufficiently in profit and ATR is defined.
    if (
        config.enable_trailing
        and atr > 0
        and gain_pct >= config.trail_activation_profit_pct
    ):
        candidates.append(
            (trailing_stop(current_price, atr, config.trail_atr_multiplier), "trailing")
        )

    # 2) Breakeven stop — once the trade has earned trigger_r of initial risk.
    if config.enable_breakeven and reached_r_multiple(
        entry, current_price, initial_stop, config.breakeven_trigger_r
    ):
        candidates.append(
            (breakeven_stop(entry, config.breakeven_buffer_pct), "breakeven")
        )

    # 3) Time-based tightening — stale + stagnant → tighten the trail.
    if config.enable_time_tighten and atr > 0:
        held = days_between(position.get("entry_time"), now)
        if (
            held is not None
            and held >= config.time_tighten_days
            and gain_pct < config.time_stagnant_profit_pct
        ):
            candidates.append(
                (
                    trailing_stop(
                        current_price, atr, config.time_tighten_atr_multiplier
                    ),
                    "time_tighten",
                )
            )

    if not candidates:
        return None

    # Keep the highest (tightest / most protective) candidate.
    best_stop, best_reason = max(candidates, key=lambda c: c[0])
    best_stop = round(best_stop, 4)

    # Never propose a stop at or below the current one, and never above price.
    if best_stop <= current_stop or best_stop >= current_price:
        return None

    return StopDecision(new_stop=best_stop, reason=best_reason)


def _f(value: Any, default: float = 0.0) -> float:
    """Coerce a position-dict cell to float, falling back to *default*."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
