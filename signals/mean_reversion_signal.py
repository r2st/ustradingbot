"""
Mean-Reversion detector -- sharp-drop recovery in structurally strong stocks.

This is the highest-risk of the five strategies and is always sized at 50% of
normal (enforced by the risk manager's strategy modifier).  It is the
screener's last resort: it fires only when no higher-priority strategy matches.

The thesis: a stock still in a long-term uptrend (above EMA200) that has
dropped hard on exhausting selling volume, is stretched to the lower Bollinger
band, and whose RSI has bottomed and is turning up, tends to snap back toward
its mean (EMA50).

Public API::

    from signals.mean_reversion_signal import detect
    signal = detect("AAPL", df)   # -> Signal | None
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import structlog

from signals.signal_types import Grade, Signal

log = structlog.get_logger(__name__)

_MIN_ROWS = 200


def _rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    """Return the full Wilder RSI series for a close series."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    """Return the latest ATR(period) value."""
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    val = tr.rolling(window=period).mean().iloc[-1]
    return float(val) if not pd.isna(val) else 0.0


def detect(symbol: str, df: pd.DataFrame) -> Optional[Signal]:
    """Detect a mean-reversion bounce setup for *symbol*.

    Args:
        symbol: Ticker symbol.
        df: OHLCV DataFrame.

    Returns:
        A populated :class:`Signal`, or ``None`` when the setup is absent.
    """
    try:
        if df is None or len(df) < _MIN_ROWS:
            return None

        close = df["Close"].astype(float)
        low = df["Low"].astype(float)
        volume = df["Volume"].astype(float)

        price = float(close.iloc[-1])
        if price <= 0:
            return None

        # 1) Regime filter: still above EMA200.
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])
        if price <= ema200:
            return None

        # 2) Sharp drop of 12-25% over the last 10 days.
        ref_price = float(close.iloc[-11]) if len(close) >= 11 else float(close.iloc[0])
        if ref_price <= 0:
            return None
        drop_pct = (ref_price - price) / ref_price
        if not (0.12 <= drop_pct <= 0.25):
            return None

        # 3) RSI dipped below 35 and is now rising two consecutive days.
        rsi = _rsi_series(close).fillna(50.0)
        rsi_today = float(rsi.iloc[-1])
        rsi_y1 = float(rsi.iloc[-2])
        rsi_y2 = float(rsi.iloc[-3])
        prior_rsi = float(rsi.iloc[-10:].min())
        rising_two_days = rsi_today > rsi_y1 > rsi_y2
        if prior_rsi >= 35.0 or not rising_two_days:
            return None

        # 4) Bollinger %B < 0.20 (20-period, 2 std).
        ma20 = close.rolling(window=20).mean()
        sd20 = close.rolling(window=20).std()
        upper = float((ma20 + 2.0 * sd20).iloc[-1])
        lower = float((ma20 - 2.0 * sd20).iloc[-1])
        band = upper - lower
        if band <= 0:
            return None
        bb_pct = (price - lower) / band
        if bb_pct >= 0.20:
            return None

        # 5) Selling volume declining (recent 3-day avg < prior 3-day avg).
        recent_vol = float(volume.iloc[-3:].mean())
        prior_vol = float(volume.iloc[-6:-3].mean())
        if prior_vol <= 0 or recent_vol >= prior_vol:
            return None

        # ── Price levels ──────────────────────────────────────────────────
        atr = _atr(df)
        low_3d = float(low.iloc[-3:].min())
        stop_price = low_3d - 0.5 * atr
        risk_per_share = price - stop_price
        if risk_per_share <= 0:
            return None
        # Target = reversion to EMA50.
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        if ema50 <= price:
            return None
        target_price = ema50

        # ── Signal strength ───────────────────────────────────────────────
        bb_stretch = float(np.clip(1.0 - bb_pct / 0.20, 0.0, 1.0))
        rsi_depth = float(np.clip(max(0.0, 35.0 - prior_rsi) / 35.0, 0.0, 1.0))
        drop_magnitude = float(np.clip(drop_pct / 0.25, 0.0, 1.0))
        strength = float(
            np.clip(
                0.40 * bb_stretch + 0.30 * rsi_depth + 0.30 * drop_magnitude,
                0.0,
                1.0,
            )
        )

        signal = Signal(
            symbol=symbol,
            strategy="mean_reversion",
            entry_price=round(price, 4),
            stop_price=round(stop_price, 4),
            target_price=round(target_price, 4),
            signal_strength=round(strength, 4),
            grade=Grade.from_score(strength),
            rsi_value=round(rsi_today, 2),
            raw_data={
                "drop_pct": round(drop_pct, 4),
                "bb_pct": round(bb_pct, 4),
                "prior_rsi": round(prior_rsi, 2),
                "ema50_target": round(ema50, 4),
            },
        )
        log.info(
            "mean_reversion.detected",
            symbol=symbol,
            grade=signal.grade.value,
            strength=signal.signal_strength,
            drop_pct=round(drop_pct, 4),
        )
        return signal

    except Exception:
        log.exception("mean_reversion.detect_error", symbol=symbol)
        return None
