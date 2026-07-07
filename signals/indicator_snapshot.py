"""
Full indicator-series snapshot for the TA chart (TA1).

The scoring engine (:mod:`signals.combined_filter`) computes complete
indicator series on every scan and keeps only the last-bar scalars.  This
module recomputes the *same* series with the *same* math and exposes them
per-bar, aligned to the trailing ``SNAPSHOT_BARS`` daily bars, so the
dashboard can draw the exact chart the system "looked at" when it entered.

The snapshot is attached to ``Signal.raw_data["indicators"]`` at scoring
time and persisted (v2) into ``trade_rationale.jsonl``; for older trades
and for open positions' *current* view the dashboard recomputes the same
payload on demand from cached bars — identical code path.

Every series here must stay in lockstep with its ``signals/*`` twin:
RSI uses Wilder smoothing (:mod:`signals.rsi_signals`), MACD is 12/26/9
(:mod:`signals.macd_signals`), EMAs are ``ewm(span, adjust=False)``
(:mod:`signals.ema_signals`), OBV is ``sign(diff) * volume`` cumsum
(:mod:`signals.volume_signals`), and ATR(14) matches
``combined_filter._compute_atr``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import structlog

from signals.support_resistance import find_levels

log = structlog.get_logger(__name__)

#: Bars captured in the snapshot — matches ``journal.rationale.CHART_BARS``
#: so the series stay aligned with the persisted bar snapshot.
SNAPSHOT_BARS = 90

#: Bollinger Band parameters (20-period SMA, 2 standard deviations).
BB_PERIOD = 20
BB_STDDEV = 2.0

#: Round series values to this many decimal places for persistence.
ROUND_DP = 4


def compute_indicator_series(df: pd.DataFrame) -> Dict[str, pd.Series]:
    """Compute all full-length indicator series from an OHLCV DataFrame.

    Args:
        df: DataFrame with ``Open``/``High``/``Low``/``Close``/``Volume``
            columns and a DatetimeIndex.

    Returns:
        Mapping of series name -> full-length ``pd.Series`` (aligned to
        *df*'s index; leading values may be NaN where a lookback window
        has not filled yet).
    """
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    volume = df["Volume"].astype(float)

    series: Dict[str, pd.Series] = {}

    # EMAs — identical to signals/ema_signals.calculate_ema.
    for span in (9, 20, 50, 200):
        series[f"ema{span}"] = close.ewm(span=span, adjust=False).mean()

    # RSI(14), Wilder smoothing — identical to signals/rsi_signals.
    period = 14
    delta = close.diff()
    gains = delta.where(delta > 0, 0.0)
    losses = (-delta).where(delta < 0, 0.0)
    avg_gain = gains.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # fillna(50) only where the average window has filled (parity with
    # rsi_signals, which fills the no-losses case with the neutral 50).
    series["rsi"] = rsi.where(avg_gain.isna(), rsi.fillna(50.0))

    # MACD 12/26/9 — identical to signals/macd_signals.calculate_macd.
    fast_ema = close.ewm(span=12, adjust=False).mean()
    slow_ema = close.ewm(span=26, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    series["macd"] = macd_line
    series["macd_signal"] = signal_line
    series["macd_hist"] = macd_line - signal_line

    # Bollinger Bands (20, 2σ) — new computation for TA1.
    bb_mid = close.rolling(window=BB_PERIOD).mean()
    bb_std = close.rolling(window=BB_PERIOD).std(ddof=0)
    series["bb_mid"] = bb_mid
    series["bb_up"] = bb_mid + BB_STDDEV * bb_std
    series["bb_lo"] = bb_mid - BB_STDDEV * bb_std

    # OBV — identical to signals/volume_signals.calculate_volume.
    obv_direction = np.sign(close.diff()).fillna(0).astype(int)
    series["obv"] = (obv_direction * volume).cumsum()

    # 20-day average volume (rolling twin of volume_signals' last-bar mean).
    series["vol_avg20"] = volume.rolling(window=20).mean()

    # ATR(14) — identical to combined_filter._compute_atr, as a series.
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    series["atr14"] = true_range.rolling(window=14).mean()

    return series


def _round_tail(s: pd.Series, n: int) -> List[Optional[float]]:
    """Last *n* values rounded to :data:`ROUND_DP`, NaN -> ``None``."""
    out: List[Optional[float]] = []
    for v in s.iloc[-n:]:
        f = float(v)
        out.append(round(f, ROUND_DP) if f == f and np.isfinite(f) else None)
    return out


def _bars_tail(df: pd.DataFrame, n: int) -> List[Dict[str, Any]]:
    """Trailing OHLCV bars in the rationale-store bar format."""
    bars: List[Dict[str, Any]] = []
    for idx, row in df.tail(n).iterrows():
        try:
            bars.append({
                "t": str(getattr(idx, "date", lambda: idx)())[:10],
                "o": round(float(row["Open"]), ROUND_DP),
                "h": round(float(row["High"]), ROUND_DP),
                "l": round(float(row["Low"]), ROUND_DP),
                "c": round(float(row["Close"]), ROUND_DP),
                "v": int(float(row.get("Volume", 0) or 0)),
            })
        except (ValueError, TypeError, KeyError):
            continue
    return bars


def _state_flags(df: pd.DataFrame) -> Dict[str, Any]:
    """Last-bar indicator states used by the explanation templates.

    Each block is computed by the exact ``signals/*`` function the scorer
    uses, so the prose can never disagree with the trade decision.  Any
    block that fails is simply omitted (best-effort).
    """
    state: Dict[str, Any] = {}
    try:
        from signals.rsi_signals import calculate_rsi

        rsi = calculate_rsi(df)
        state.update({
            "rsi_value": round(rsi.rsi_value, 2),
            "rsi_momentum_zone": rsi.is_momentum_zone,
            "rsi_rising": rsi.is_rising,
            "rsi_overbought": rsi.is_overbought,
        })
    except Exception:  # noqa: BLE001
        pass
    try:
        from signals.macd_signals import calculate_macd

        macd = calculate_macd(df)
        state.update({
            "macd_line": round(macd.macd_line, ROUND_DP),
            "macd_signal_line": round(macd.signal_line, ROUND_DP),
            "macd_histogram": round(macd.histogram, ROUND_DP),
            "macd_bullish_crossover": macd.has_bullish_crossover,
            "macd_confirmed_bullish": macd.is_confirmed_bullish,
            "macd_momentum_intact": macd.is_momentum_intact,
        })
    except Exception:  # noqa: BLE001
        pass
    try:
        from signals.ema_signals import calculate_ema

        ema = calculate_ema(df)
        state.update({
            "ema20": round(ema.ema20, ROUND_DP),
            "price": round(ema.price, ROUND_DP),
            "above_ema200": ema.is_above_ema200,
            "bullish_stack": ema.has_bullish_stack,
            "partial_stack": ema.has_partial_stack,
            "pct_above_ema20": (
                round((ema.price - ema.ema20) / ema.ema20 * 100.0, 2)
                if ema.ema20 > 0 else None
            ),
        })
    except Exception:  # noqa: BLE001
        pass
    try:
        from signals.volume_signals import calculate_volume

        vol = calculate_volume(df)
        state.update({
            "volume_ratio": round(vol.volume_ratio, 2),
            "volume_surge": vol.has_bullish_surge,
            "obv_confirming": vol.is_obv_confirming,
            "above_vwap": vol.is_above_vwap,
        })
    except Exception:  # noqa: BLE001
        pass
    return state


def build_indicator_snapshot(
    df: pd.DataFrame,
    n: int = SNAPSHOT_BARS,
) -> Optional[Dict[str, Any]]:
    """Build the complete v2 indicator snapshot from OHLCV data.

    Args:
        df: OHLCV DataFrame (the same one the scorer used, when captured
            at signal time).
        n: Number of trailing bars to include.

    Returns:
        ``{"v": 2, "bars": [...], "series": {...}, "atr14": float,
        "levels": {...}, "state": {...}}`` — or ``None`` on any failure
        (snapshot capture is best-effort and must never block an entry).
    """
    try:
        if df is None or df.empty or len(df) < 2:
            return None
        n = max(1, min(int(n), len(df)))
        full = compute_indicator_series(df)
        series = {name: _round_tail(s, n) for name, s in full.items()}
        atr_tail = series.get("atr14") or [None]
        snapshot: Dict[str, Any] = {
            "v": 2,
            "bars": _bars_tail(df, n),
            "series": series,
            "atr14": atr_tail[-1],
            "levels": find_levels(df),
            "state": _state_flags(df),
        }
        return snapshot
    except Exception:  # noqa: BLE001 -- snapshot must never break scoring
        log.debug("indicator_snapshot.build_failed", exc_info=True)
        return None
