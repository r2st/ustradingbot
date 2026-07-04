"""
VCP Breakout detector -- Mark Minervini's Volatility Contraction Pattern.

A VCP forms when a stock makes a high, then consolidates with progressively
decreasing volatility and volume, and finally breaks out of that consolidation
on surging volume.  It is a contraction-then-expansion pattern with a
mechanically defined stop (just below the consolidation low), which gives it
the tightest, cleanest risk/reward of the five strategies -- hence its top
priority in the screener.

Public API::

    from signals.vcp_signal import detect
    signal = detect("AAPL", df)   # -> Signal | None
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import structlog

from config.settings import get_settings
from signals.signal_types import Grade, Signal

log = structlog.get_logger(__name__)

_MIN_ROWS = 200


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Return the ATR(period) series for an OHLCV frame."""
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window=period).mean()


def _rsi(close: pd.Series, period: int = 14) -> float:
    """Return the latest Wilder RSI value for a close series."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    val = rsi.iloc[-1]
    return float(val) if not pd.isna(val) else 50.0


def detect(symbol: str, df: pd.DataFrame) -> Optional[Signal]:
    """Detect a VCP breakout for *symbol*.

    Args:
        symbol: Ticker symbol.
        df: OHLCV DataFrame (DatetimeIndex; Open/High/Low/Close/Volume).

    Returns:
        A populated :class:`Signal`, or ``None`` if the pattern is absent
        or any data problem occurs.
    """
    try:
        if df is None or len(df) < _MIN_ROWS:
            return None

        settings = get_settings()
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        volume = df["Volume"].astype(float)

        price = float(close.iloc[-1])
        if price <= 0:
            return None

        # 1) Regime filter: price above EMA200.
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])
        if price <= ema200:
            return None

        # 2) A 90-day high occurred 5-70 days ago.
        high_lookback = high.iloc[-90:] if len(high) >= 90 else high
        # Position (bars-ago) of the highest high in the 90-day window.
        hh_pos = int(np.argmax(high_lookback.values))
        bars_ago = (len(high_lookback) - 1) - hh_pos
        if not (5 <= bars_ago <= 70):
            return None
        pivot_high = float(high_lookback.iloc[hh_pos])

        # 3) Consolidation = bars from the pivot high to yesterday.
        cons_high = high.iloc[-bars_ago:-1] if bars_ago >= 2 else high.iloc[-1:]
        cons_low_series = low.iloc[-bars_ago:-1] if bars_ago >= 2 else low.iloc[-1:]
        if cons_low_series.empty or cons_high.empty:
            return None
        cons_low = float(cons_low_series.min())
        consolidation_depth = (pivot_high - cons_low) / pivot_high
        if not (0.08 <= consolidation_depth <= 0.30):
            return None

        # 4) Volume dry-up during consolidation vs pre-consolidation.
        pre_start = max(0, len(volume) - bars_ago - 30)
        pre_vol = volume.iloc[pre_start : len(volume) - bars_ago]
        cons_vol = volume.iloc[-bars_ago:-1] if bars_ago >= 2 else volume.iloc[-1:]
        if pre_vol.empty or cons_vol.empty:
            return None
        pre_vol_mean = float(pre_vol.mean())
        if pre_vol_mean <= 0:
            return None
        vol_contraction_ratio = float(cons_vol.mean()) / pre_vol_mean
        if vol_contraction_ratio >= 0.65:
            return None

        # 5) ATR contraction: ATR during consolidation < 80% of ATR at the high.
        # The pivot high sits `bars_ago` bars back from the last row.
        atr_series = _atr(df)
        atr_at_high = float(atr_series.iloc[len(df) - 1 - bars_ago])
        atr_now = float(atr_series.iloc[-1])
        if pd.isna(atr_at_high) or atr_at_high <= 0 or pd.isna(atr_now):
            return None
        if atr_now / atr_at_high >= 0.80:
            return None

        # 6) Breakout: today's price breaks above the 90th-pct high of the range.
        breakout_level = float(np.percentile(cons_high.values, 90))
        if price <= breakout_level:
            return None

        # 7) Volume surge: today >= 2.0x the 30-day average.
        avg_vol_30 = float(volume.iloc[-30:].mean())
        if avg_vol_30 <= 0:
            return None
        volume_ratio = float(volume.iloc[-1]) / avg_vol_30
        if volume_ratio < 2.0:
            return None

        # 8) RSI not exhausted.
        rsi_value = _rsi(close)
        if not (48.0 <= rsi_value <= 75.0):
            return None

        # ── Price levels ──────────────────────────────────────────────────
        stop_price = cons_low * 0.995  # just below the consolidation low
        risk_per_share = price - stop_price
        if risk_per_share <= 0:
            return None
        target_price = price + risk_per_share * max(settings.RISK_REWARD_MIN, 2.5)

        # ── Signal strength ───────────────────────────────────────────────
        volume_surge_quality = min(volume_ratio, 5.0) / 5.0
        vol_contraction_quality = np.clip(1.0 - vol_contraction_ratio / 0.65, 0.0, 1.0)
        consolidation_tightness = np.clip(1.0 - consolidation_depth / 0.30, 0.0, 1.0)
        strength = float(
            np.clip(
                0.50 * volume_surge_quality
                + 0.30 * vol_contraction_quality
                + 0.20 * consolidation_tightness,
                0.0,
                1.0,
            )
        )

        signal = Signal(
            symbol=symbol,
            strategy="vcp_breakout",
            entry_price=round(price, 4),
            stop_price=round(stop_price, 4),
            target_price=round(target_price, 4),
            signal_strength=round(strength, 4),
            grade=Grade.from_score(strength),
            rsi_value=round(rsi_value, 2),
            volume_ratio=round(volume_ratio, 4),
            raw_data={
                "consolidation_depth": round(consolidation_depth, 4),
                "vol_contraction_ratio": round(vol_contraction_ratio, 4),
                "breakout_level": round(breakout_level, 4),
                "pivot_high": round(pivot_high, 4),
                "bars_since_high": bars_ago,
            },
        )
        log.info(
            "vcp.detected",
            symbol=symbol,
            grade=signal.grade.value,
            strength=signal.signal_strength,
            volume_ratio=signal.volume_ratio,
        )
        return signal

    except Exception:
        log.exception("vcp.detect_error", symbol=symbol)
        return None
