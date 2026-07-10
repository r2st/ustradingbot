"""
Combined scoring filter — the weighted multi-indicator scoring engine.

Runs all five technical indicators (RSI, MACD, EMA structure, Volume,
Ripster EMA Clouds) against a single symbol's OHLCV data, applies hard
vetoes, computes a strategy-weighted combined score, and returns a
:class:`~signals.signal_types.Signal` or ``None`` if a hard veto fires.

Usage::

    from signals.combined_filter import score_symbol

    signal = score_symbol("AAPL", "momentum", df)
    if signal is not None and signal.grade in (Grade.A, Grade.B):
        # proceed to AI veto and risk check
        ...
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import structlog

from config.settings import (
    Settings,
    get_settings,
    momentum_weights,
    swing_weights,
    weights_for_strategy,
)
from signals.ema_signals import EMAState, calculate_ema
from signals.ema_signals import bullish_score as ema_bullish_score
from signals.macd_signals import MACDState, calculate_macd
from signals.macd_signals import bullish_score as macd_bullish_score
from signals.ripster_cloud import RipsterState, calculate_ripster
from signals.ripster_cloud import bullish_score as ripster_bullish_score
from signals.rsi_signals import RSIState, calculate_rsi
from signals.rsi_signals import bullish_score as rsi_bullish_score
from signals.signal_types import Grade, Signal
from signals.volume_signals import VolumeState, calculate_volume
from signals.volume_signals import bullish_score as volume_bullish_score

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Hard veto checks
# ---------------------------------------------------------------------------

def _check_bear_regime(ema_state: EMAState) -> Optional[str]:
    """Veto if price is below EMA-200 (bear regime).

    Args:
        ema_state: Pre-computed EMA state.

    Returns:
        Veto reason string, or ``None`` if no veto.
    """
    if not ema_state.is_above_ema200:
        return "bear_regime: price below EMA200"
    return None


def _check_obv_divergence(volume_state: VolumeState) -> Optional[str]:
    """Veto if OBV is diverging from price (bearish warning).

    Args:
        volume_state: Pre-computed volume state.

    Returns:
        Veto reason string, or ``None`` if no veto.
    """
    if volume_state.has_obv_divergence:
        return "obv_divergence: price up but OBV down over 10 days"
    return None


def _check_bearish_volume_surge(volume_state: VolumeState) -> Optional[str]:
    """Veto if there is a bearish volume surge (distribution day).

    Args:
        volume_state: Pre-computed volume state.

    Returns:
        Veto reason string, or ``None`` if no veto.
    """
    if volume_state.has_bearish_surge:
        return "bearish_volume_surge: down day with volume > 1.5x average"
    return None


def _check_ripster_cross_below(ripster_state: RipsterState) -> Optional[str]:
    """Veto if price is below both Ripster clouds.

    Args:
        ripster_state: Pre-computed Ripster state.

    Returns:
        Veto reason string, or ``None`` if no veto.
    """
    if ripster_state.price_below_both:
        return "ripster_cross_below: price below both EMA clouds"
    return None


def _check_low_atr(df: pd.DataFrame, min_atr_pct: float) -> Optional[str]:
    """Veto if ATR(14) / price is below the minimum threshold.

    Low ATR% means the stock doesn't move enough to justify the spread
    and commission costs of a trade.

    Args:
        df: OHLCV DataFrame.
        min_atr_pct: Minimum ATR percentage (e.g. 0.015 for 1.5%).

    Returns:
        Veto reason string, or ``None`` if no veto.
    """
    if len(df) < 15:
        return "low_atr: insufficient data for ATR(14)"

    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)

    # True Range components
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr_14 = true_range.rolling(window=14).mean().iloc[-1]

    current_price = float(close.iloc[-1])
    if current_price <= 0:
        return "low_atr: zero or negative price"

    atr_pct = float(atr_14) / current_price

    if atr_pct < min_atr_pct:
        return (
            f"low_atr: ATR%={atr_pct:.4f} < minimum {min_atr_pct:.4f}"
        )
    return None


def _resolve_strategy(symbol: str, strategy: str) -> str:
    """Resolve the effective strategy for a symbol.

    Canadian stocks (symbols ending in ``.TO``) always use swing
    strategy weights regardless of the requested strategy.

    Args:
        symbol: Ticker symbol.
        strategy: Requested strategy name.

    Returns:
        The effective strategy name to use for scoring.
    """
    if symbol.upper().endswith(".TO"):
        log.debug(
            "canadian_stock_override",
            symbol=symbol,
            requested_strategy=strategy,
            effective_strategy="swing",
        )
        return "swing"
    return strategy


# ---------------------------------------------------------------------------
# Stop and target calculation
# ---------------------------------------------------------------------------

def _compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Compute ATR(period) from an OHLCV DataFrame.

    Args:
        df: OHLCV DataFrame.
        period: ATR period (default 14).

    Returns:
        ATR value as a float.
    """
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return float(true_range.rolling(window=period).mean().iloc[-1])


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_symbol(
    symbol: str,
    strategy: str,
    df: pd.DataFrame,
    capture_series: bool = True,
) -> Optional[Signal]:
    """Run all five indicators, apply hard vetoes, and compute weighted score.

    This is the main entry point for the signal scoring engine.  It:

    1. Resolves the effective strategy (Canadian stocks forced to swing).
    2. Validates minimum data requirements.
    3. Calculates all five indicator states.
    4. Runs hard veto checks — if any fire, returns ``None``.
    5. Scores each indicator using the effective strategy.
    6. Combines scores using strategy-specific weights.
    7. Derives a grade from the combined score.
    8. Builds and returns a :class:`Signal` with all fields populated.

    Args:
        symbol: Ticker symbol (e.g. ``"AAPL"`` or ``"SHOP.TO"``).
        strategy: Strategy name (``"momentum"``, ``"swing"``,
            ``"vcp_breakout"``, ``"pead"``, ``"mean_reversion"``).
        df: OHLCV DataFrame with columns ``Open``, ``High``, ``Low``,
            ``Close``, ``Volume`` and a ``DatetimeIndex``.
        capture_series: Attach the full per-bar indicator snapshot
            (:mod:`signals.indicator_snapshot`) to ``Signal.raw_data``
            so the TA chart can show exactly what the scorer saw.  Only
            computed for signals that pass every veto; disable in tight
            loops (backtests) where the snapshot is never persisted.

    Returns:
        A fully populated :class:`Signal`, or ``None`` if a hard veto
        fired (bear regime, OBV divergence, bearish volume surge,
        Ripster cross below, or low ATR%).
    """
    settings: Settings = get_settings()
    effective_strategy = _resolve_strategy(symbol, strategy)

    # --- Data validation ----------------------------------------------------
    if len(df) < settings.MIN_OHLCV_ROWS:
        log.warning(
            "score_symbol.insufficient_data",
            symbol=symbol,
            rows=len(df),
            required=settings.MIN_OHLCV_ROWS,
        )
        return None

    # --- Calculate all indicator states -------------------------------------
    try:
        rsi_state: RSIState = calculate_rsi(df)
        macd_state: MACDState = calculate_macd(df)
        ema_state: EMAState = calculate_ema(df)
        volume_state: VolumeState = calculate_volume(df)
        ripster_state: RipsterState = calculate_ripster(df)
    except (ValueError, KeyError) as exc:
        log.warning(
            "score_symbol.indicator_error",
            symbol=symbol,
            strategy=effective_strategy,
            error=str(exc),
        )
        return None

    # --- Hard vetoes (checked before any scoring) ---------------------------
    # ETFs are calm by construction; hold them to a lower ATR% floor so a
    # diversified basket isn't vetoed purely for being less volatile than a
    # single name.
    from config.etf_universe import is_etf

    min_atr_pct = (
        float(getattr(settings, "MIN_ATR_PCT_ETF", 0.008))
        if is_etf(symbol)
        else settings.MIN_ATR_PCT
    )
    veto_checks = [
        _check_bear_regime(ema_state),
        _check_obv_divergence(volume_state),
        _check_bearish_volume_surge(volume_state),
        _check_ripster_cross_below(ripster_state),
        _check_low_atr(df, min_atr_pct),
    ]

    for veto_reason in veto_checks:
        if veto_reason is not None:
            log.info(
                "score_symbol.hard_veto",
                symbol=symbol,
                strategy=effective_strategy,
                veto_reason=veto_reason,
            )
            return None

    # --- Score each indicator -----------------------------------------------
    rsi_score = rsi_bullish_score(rsi_state, effective_strategy)
    macd_score = macd_bullish_score(macd_state)
    ema_score = ema_bullish_score(ema_state, effective_strategy)
    vol_score = volume_bullish_score(volume_state, effective_strategy)
    rip_score = ripster_bullish_score(ripster_state)

    # --- Combine with strategy-specific weights -----------------------------
    weights = weights_for_strategy(effective_strategy)

    combined_score = (
        weights["rsi"] * rsi_score
        + weights["macd"] * macd_score
        + weights["ema"] * ema_score
        + weights["volume"] * vol_score
        + weights["ripster"] * rip_score
    )

    # Clamp to [0.0, 1.0]
    combined_score = float(np.clip(combined_score, 0.0, 1.0))

    # --- Grade --------------------------------------------------------------
    grade = Grade.from_score(combined_score)

    # --- Price levels -------------------------------------------------------
    entry_price = float(df["Close"].iloc[-1])
    atr = _compute_atr(df)
    stop_price = entry_price - (settings.ATR_STOP_MULTIPLIER * atr)
    risk_per_share = entry_price - stop_price

    # Target uses the configured R:R minimum
    target_price = entry_price + (risk_per_share * settings.RISK_REWARD_MIN)

    # --- Currency -----------------------------------------------------------
    currency = "CAD" if symbol.upper().endswith(".TO") else "USD"

    # --- Build Signal -------------------------------------------------------
    signal = Signal(
        symbol=symbol,
        strategy=strategy,  # original requested strategy for journal
        entry_price=round(entry_price, 4),
        stop_price=round(stop_price, 4),
        target_price=round(target_price, 4),
        signal_strength=round(combined_score, 4),
        grade=grade,
        rsi_value=round(rsi_state.rsi_value, 2),
        rsi_score=round(rsi_score, 4),
        macd_histogram=round(macd_state.histogram, 4),
        macd_score=round(macd_score, 4),
        ema_score=round(ema_score, 4),
        volume_ratio=round(volume_state.volume_ratio, 4),
        volume_score=round(vol_score, 4),
        ripster_score=round(rip_score, 4),
        obv_confirming=volume_state.is_obv_confirming,
    )

    # Persist the full indicator series (TA1) — best-effort, never blocks.
    if capture_series:
        try:
            from signals.indicator_snapshot import build_indicator_snapshot

            snapshot = build_indicator_snapshot(df)
            if snapshot is not None:
                signal.raw_data["indicators"] = snapshot
        except Exception:  # noqa: BLE001
            log.debug("score_symbol.snapshot_failed", symbol=symbol, exc_info=True)

    log.info(
        "score_symbol.scored",
        symbol=symbol,
        strategy=strategy,
        effective_strategy=effective_strategy,
        combined_score=signal.signal_strength,
        grade=grade.value,
        rsi_score=signal.rsi_score,
        macd_score=signal.macd_score,
        ema_score=signal.ema_score,
        volume_score=signal.volume_score,
        ripster_score=signal.ripster_score,
        entry_price=signal.entry_price,
        stop_price=signal.stop_price,
        target_price=signal.target_price,
    )

    return signal
