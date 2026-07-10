"""
PEAD detector -- Post-Earnings Announcement Drift.

PEAD exploits the well-documented anomaly (Bernard & Thomas, 1989) where
stocks that beat earnings on heavy volume tend to keep drifting in the
direction of the surprise for several weeks.  The edge persists because of
analyst-revision lag and retail under-reaction.

Critically, this detector does **not** buy on the earnings day itself
(spreads are wide, ~half of gaps reverse).  It waits 1-5 trading days and
enters only once the stock confirms continuation.

Public API::

    from signals.pead_signal import detect
    signal = detect("AAPL", df)   # -> Signal | None
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import structlog

from config.settings import get_settings
from data.earnings import get_recent_earnings
from signals.signal_types import Grade, Signal

log = structlog.get_logger(__name__)

_MIN_ROWS = 200


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
    """Detect a PEAD continuation entry for *symbol*.

    Args:
        symbol: Ticker symbol.
        df: OHLCV DataFrame.

    Returns:
        A populated :class:`Signal`, or ``None`` if no qualifying
        post-earnings drift setup is present.
    """
    try:
        if df is None or len(df) < _MIN_ROWS:
            return None

        settings = get_settings()

        # 1) Earnings in the last 1-5 trading days with a positive surprise.
        info = get_recent_earnings(symbol, lookback_days=5)
        if info is None:
            return None
        if info.price_move_pct <= 5.0:
            return None
        if info.volume_ratio <= 2.0:
            return None

        # 1b) Fundamental beat/miss context (Feature 1c).  When earnings-results
        #     tracking is enabled, only let gap-and-go fire on a genuine beat —
        #     a large upward price move on a reported *miss* is often a squeeze
        #     that fades.  Fail-open: when results are unavailable PEAD falls
        #     back to the price-based proxy (unchanged behaviour).
        earnings_result = None
        if getattr(settings, "EARNINGS_RESULTS_ENABLED", False):
            from data.earnings import get_earnings_result

            earnings_result = get_earnings_result(symbol, settings)
            if earnings_result is not None and earnings_result.verdict == "miss":
                log.info(
                    "pead.rejected_on_miss",
                    symbol=symbol,
                    eps_surprise_pct=earnings_result.eps_surprise_pct,
                )
                return None

        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        open_ = df["Open"].astype(float)
        volume = df["Volume"].astype(float)

        price = float(close.iloc[-1])
        if price <= 0:
            return None

        # 2) Regime filter.
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])
        if price <= ema200:
            return None

        # 3) RSI in the healthy 50-78 band.
        rsi_value = _rsi(close)
        if not (50.0 <= rsi_value <= 78.0):
            return None

        # 4) Continuation confirmation: today green OR today's high above
        #    the earnings-day close (approximated by the recent close 1-5
        #    bars ago -- the earnings bar sits in that window).
        today_green = float(close.iloc[-1]) > float(open_.iloc[-1])
        window = close.iloc[-6:-1] if len(close) >= 6 else close.iloc[:-1]
        earnings_ref_close = float(window.max()) if not window.empty else price
        high_exceeds = float(high.iloc[-1]) > earnings_ref_close
        if not (today_green or high_exceeds):
            return None

        # ── Price levels ──────────────────────────────────────────────────
        # Stop 1% below the recent (5-bar) low -- brackets the earnings base.
        recent_low = float(low.iloc[-5:].min())
        stop_price = recent_low * 0.99
        risk_per_share = price - stop_price
        if risk_per_share <= 0:
            return None
        # Risk must be < 12% of price.
        if (risk_per_share / price) >= 0.12:
            return None
        target_price = price + risk_per_share * max(settings.RISK_REWARD_MIN, 2.5)

        # ── Signal strength ───────────────────────────────────────────────
        surprise_magnitude = min(info.price_move_pct / 15.0, 1.0)
        earnings_vol = min(info.volume_ratio / 5.0, 1.0)
        rsi_quality = float(np.clip(1.0 - abs(rsi_value - 62.0) / 32.0, 0.0, 1.0))
        avg_vol_20 = float(volume.iloc[-21:-1].mean())
        today_vol_ratio = (
            float(volume.iloc[-1]) / avg_vol_20 if avg_vol_20 > 0 else 0.0
        )
        continuation_strength = 0.7 if today_vol_ratio > 1.2 else 0.5
        strength = float(
            np.clip(
                0.35 * surprise_magnitude
                + 0.25 * earnings_vol
                + 0.20 * rsi_quality
                + 0.20 * continuation_strength,
                0.0,
                1.0,
            )
        )

        signal = Signal(
            symbol=symbol,
            strategy="pead",
            entry_price=round(price, 4),
            stop_price=round(stop_price, 4),
            target_price=round(target_price, 4),
            signal_strength=round(strength, 4),
            grade=Grade.from_score(strength),
            rsi_value=round(rsi_value, 2),
            volume_ratio=round(today_vol_ratio, 4),
            raw_data={
                "earnings_date": str(info.earnings_date),
                "earnings_move_pct": info.price_move_pct,
                "earnings_vol_ratio": info.volume_ratio,
                "gap_direction": info.gap_direction,
                **(
                    {
                        "earnings_verdict": earnings_result.verdict,
                        "eps_surprise_pct": earnings_result.eps_surprise_pct,
                    }
                    if earnings_result is not None
                    else {}
                ),
            },
        )
        log.info(
            "pead.detected",
            symbol=symbol,
            grade=signal.grade.value,
            strength=signal.signal_strength,
            earnings_move=info.price_move_pct,
        )
        return signal

    except Exception:
        log.exception("pead.detect_error", symbol=symbol)
        return None
