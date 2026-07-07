"""Tests for multi-timeframe analysis (signals/multi_timeframe.py)."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from config.settings import Settings
from signals.multi_timeframe import (
    resample_weekly,
    weekly_confirms,
    weekly_trend,
)


def _daily(prices) -> pd.DataFrame:
    prices = np.asarray(prices, dtype=float)
    n = len(prices)
    idx = pd.bdate_range(end=datetime.now(), periods=n)
    return pd.DataFrame(
        {
            "Open": prices,
            "High": prices * 1.01,
            "Low": prices * 0.99,
            "Close": prices,
            "Volume": np.ones(n) * 1_000_000,
        },
        index=idx,
    )


def _uptrend(n=400) -> pd.DataFrame:
    return _daily(100.0 * np.exp(np.cumsum(np.full(n, 0.002))))


def _downtrend(n=400) -> pd.DataFrame:
    return _daily(300.0 * np.exp(np.cumsum(np.full(n, -0.002))))


# --------------------------------------------------------------------------- #
# resample_weekly
# --------------------------------------------------------------------------- #


def test_resample_weekly_reduces_rows() -> None:
    daily = _uptrend(200)
    weekly = resample_weekly(daily)
    # ~200 business days -> ~40 weeks.
    assert 30 < len(weekly) < 50
    assert list(weekly.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_resample_weekly_ohlc_semantics() -> None:
    # Anchor to a fixed Friday so all 5 business days land in ONE calendar
    # week; ending at datetime.now() splits the week on any other weekday.
    prices = np.asarray([10, 11, 12, 13, 14], dtype=float)
    idx = pd.bdate_range(end=datetime(2026, 1, 9), periods=5)  # Mon..Fri
    daily = pd.DataFrame(
        {
            "Open": prices,
            "High": prices * 1.01,
            "Low": prices * 0.99,
            "Close": prices,
            "Volume": np.ones(5) * 1_000_000,
        },
        index=idx,
    )
    weekly = resample_weekly(daily)
    row = weekly.iloc[-1]
    assert row["Open"] == 10
    assert row["High"] == pytest.approx(14 * 1.01)
    assert row["Close"] == 14


def test_resample_weekly_empty() -> None:
    assert resample_weekly(pd.DataFrame()).empty


# --------------------------------------------------------------------------- #
# weekly_trend
# --------------------------------------------------------------------------- #


def test_weekly_trend_up() -> None:
    t = weekly_trend(_uptrend(), ema_period=30)
    assert t is not None
    assert t.direction == "up"
    assert t.is_up is True
    assert t.weekly_close > t.weekly_ema
    assert t.ema_rising is True


def test_weekly_trend_down() -> None:
    t = weekly_trend(_downtrend(), ema_period=30)
    assert t is not None
    assert t.direction == "down"
    assert t.is_up is False


def test_weekly_trend_insufficient_history() -> None:
    # 30 business days -> ~6 weekly bars < ema_period 30.
    assert weekly_trend(_uptrend(30), ema_period=30) is None


# --------------------------------------------------------------------------- #
# weekly_confirms gating
# --------------------------------------------------------------------------- #


def test_confirms_disabled_always_true() -> None:
    s = Settings(ENABLE_MULTI_TIMEFRAME=False)
    assert weekly_confirms(_downtrend(), s, "long") is True


def test_confirms_requires_uptrend_for_long() -> None:
    s = Settings(ENABLE_MULTI_TIMEFRAME=True, MTF_REQUIRE_WEEKLY_UPTREND=True)
    assert weekly_confirms(_uptrend(), s, "long") is True
    assert weekly_confirms(_downtrend(), s, "long") is False


def test_confirms_soft_filter_allows_neutral() -> None:
    # Soft filter: block only when explicitly against (down) for a long.
    s = Settings(ENABLE_MULTI_TIMEFRAME=True, MTF_REQUIRE_WEEKLY_UPTREND=False)
    assert weekly_confirms(_downtrend(), s, "long") is False
    assert weekly_confirms(_uptrend(), s, "long") is True


def test_confirms_insufficient_history_passes() -> None:
    s = Settings(ENABLE_MULTI_TIMEFRAME=True, MTF_REQUIRE_WEEKLY_UPTREND=True)
    # Not enough weekly bars -> don't block.
    assert weekly_confirms(_uptrend(30), s, "long") is True


# --------------------------------------------------------------------------- #
# screener integration
# --------------------------------------------------------------------------- #


def test_screener_weekly_veto(monkeypatch) -> None:
    """A qualifying daily signal is vetoed when the weekly trend disagrees."""
    import signals.screener as screener
    from signals.signal_types import Grade, Signal

    df = _downtrend()
    monkeypatch.setattr(screener, "fetch_ohlcv", lambda s, period=None: df)

    good_signal = Signal(symbol="AAPL", strategy="momentum", entry_price=100.0,
                         stop_price=95.0, target_price=115.0,
                         signal_strength=0.85, grade=Grade.A, direction="long")
    monkeypatch.setattr(screener, "score_symbol", lambda sym, strat, d: good_signal)
    monkeypatch.setattr(screener, "_DEDICATED_DETECTORS", {})

    s = Settings(ENABLE_MULTI_TIMEFRAME=True, MTF_REQUIRE_WEEKLY_UPTREND=True)
    monkeypatch.setattr(screener, "get_settings", lambda: s)

    # Weekly trend is down -> signal vetoed -> no result.
    assert screener._scan_symbol("AAPL", "B") is None


def test_screener_weekly_allows_when_disabled(monkeypatch) -> None:
    import signals.screener as screener
    from signals.signal_types import Grade, Signal

    df = _downtrend()
    monkeypatch.setattr(screener, "fetch_ohlcv", lambda s, period=None: df)
    good_signal = Signal(symbol="AAPL", strategy="momentum", entry_price=100.0,
                         stop_price=95.0, target_price=115.0,
                         signal_strength=0.85, grade=Grade.A, direction="long")
    monkeypatch.setattr(screener, "score_symbol", lambda sym, strat, d: good_signal)
    monkeypatch.setattr(screener, "_DEDICATED_DETECTORS", {})
    s = Settings(ENABLE_MULTI_TIMEFRAME=False)
    monkeypatch.setattr(screener, "get_settings", lambda: s)

    assert screener._scan_symbol("AAPL", "B") is good_signal
