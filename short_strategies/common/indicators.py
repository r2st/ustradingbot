"""
Indicator library shared by the short strategies.

Every function takes a daily OHLCV DataFrame (columns ``Open``/``High``/
``Low``/``Close``/``Volume``, DatetimeIndex) — the same shape the long
screener consumes — and returns plain floats / Series.  All functions are
pure and NaN-guarded so detectors can call them without try/except noise.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Latest ATR(period) value, or ``None`` on insufficient data."""
    if df is None or len(df) < period + 1:
        return None
    series = atr_series(df, period)
    val = float(series.iloc[-1])
    return val if val == val and val > 0 else None


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Full ATR(period) series (same maths as ``signals.vcp_signal._atr``)."""
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window=period).mean()


def rsi(close: pd.Series, period: int = 14) -> float:
    """Latest Wilder RSI value (50.0 on insufficient data)."""
    if close is None or len(close) < period + 1:
        return 50.0
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    last_gain = float(avg_gain.iloc[-1])
    last_loss = float(avg_loss.iloc[-1])
    if last_loss == 0.0:
        # No losses in the window: RSI is pinned at the top (or neutral on
        # a perfectly flat series).
        return 100.0 if last_gain > 0 else 50.0
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    val = out.iloc[-1]
    return float(val) if not pd.isna(val) else 50.0


def ema(close: pd.Series, span: int) -> pd.Series:
    """Exponential moving average series."""
    return close.astype(float).ewm(span=span, adjust=False).mean()


def sma(close: pd.Series, window: int) -> pd.Series:
    """Simple moving average series."""
    return close.astype(float).rolling(window=window).mean()


def adx(df: pd.DataFrame, period: int = 14) -> Optional[dict]:
    """Wilder ADX with directional indicators.

    Returns ``{"adx": float, "plus_di": float, "minus_di": float}`` for the
    latest bar, or ``None`` when there is not enough history (needs roughly
    ``2 * period`` bars for the double smoothing to stabilise).
    """
    if df is None or len(df) < 2 * period + 1:
        return None
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=df.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=df.index,
    )
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)

    alpha = 1.0 / period  # Wilder smoothing
    atr_s = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_s.replace(0.0, np.nan)
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_s.replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx_s = dx.ewm(alpha=alpha, adjust=False).mean()

    vals = (adx_s.iloc[-1], plus_di.iloc[-1], minus_di.iloc[-1])
    if any(pd.isna(v) for v in vals):
        return None
    return {
        "adx": float(vals[0]),
        "plus_di": float(vals[1]),
        "minus_di": float(vals[2]),
    }


def rolling_vwap(df: pd.DataFrame, window: int = 20) -> Optional[float]:
    """Rolling typical-price VWAP over the trailing *window* daily bars.

    A daily-bar approximation of intraday VWAP: sum(TP x V) / sum(V) with
    TP = (H + L + C) / 3.  Returns ``None`` on insufficient data or zero
    volume in the window.
    """
    if df is None or len(df) < window:
        return None
    tail = df.tail(window)
    tp = (
        tail["High"].astype(float)
        + tail["Low"].astype(float)
        + tail["Close"].astype(float)
    ) / 3.0
    vol = tail["Volume"].astype(float)
    total_vol = float(vol.sum())
    if total_vol <= 0:
        return None
    return float((tp * vol).sum() / total_vol)


def volume_ratio(df: pd.DataFrame, window: int = 20) -> Optional[float]:
    """Latest volume divided by the *window*-day average volume."""
    if df is None or len(df) < window + 1:
        return None
    vol = df["Volume"].astype(float)
    avg = float(vol.iloc[-(window + 1):-1].mean())
    if avg <= 0:
        return None
    return float(vol.iloc[-1]) / avg


def trend_state(
    df: pd.DataFrame,
    fast_ema_span: int = 20,
    slow_sma_window: int = 50,
) -> str:
    """Classify the trend as ``"up"`` / ``"down"`` / ``"flat"``.

    Reusable utility (spec strategy 2): ``"down"`` when the fast EMA is below
    the slow SMA *and* price closes below both; ``"up"`` is the mirror; any
    mixed structure is ``"flat"``.  Returns ``"flat"`` on insufficient data.
    """
    if df is None or len(df) < slow_sma_window + 1:
        return "flat"
    close = df["Close"].astype(float)
    fast = float(ema(close, fast_ema_span).iloc[-1])
    slow = float(sma(close, slow_sma_window).iloc[-1])
    if pd.isna(slow):
        return "flat"
    last = float(close.iloc[-1])
    if fast < slow and last < fast and last < slow:
        return "down"
    if fast > slow and last > fast and last > slow:
        return "up"
    return "flat"


def pct_return(close: pd.Series, lookback: int) -> Optional[float]:
    """Trailing *lookback*-bar simple return, or ``None``."""
    if close is None or len(close) < lookback + 1:
        return None
    prev = float(close.iloc[-(lookback + 1)])
    if prev <= 0:
        return None
    return float(close.iloc[-1]) / prev - 1.0
