"""Shared fixtures for the US Trading Bot test suite."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Generator

import numpy as np
import pandas as pd
import pytest

from config.settings import Settings
from signals.signal_types import ExitEvent, ExitReason, Grade, Signal, TradeOrder


# ---------------------------------------------------------------------------
# Environment: prevent real .env loading during tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests don't load the real .env file or keys/ credential files."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    # Keep Settings independent of the developer's loose keys/ files (e.g.
    # keys/polygon_api_key) so provider tests are hermetic.
    monkeypatch.setenv("USTB_SKIP_KEY_FILES", "1")
    # Dashboard rate limiting / login lockout (B2) are off by default under the
    # test-suite so hermetic router tests are never throttled; the dedicated
    # rate-limit tests opt back in explicitly.
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "False")


@pytest.fixture(autouse=True)
def _reset_rate_limits() -> None:
    """Clear in-process rate-limit / lockout state before every test.

    Defensive belt-and-suspenders alongside ``RATE_LIMIT_ENABLED=False``: even a
    test that turns limiting back on starts from a clean slate, so throttling
    can never leak from one test into the next.
    """
    try:
        from dashboard.rate_limit import reset

        reset()
    except Exception:  # pragma: no cover - dashboard optional in some suites
        pass


# ---------------------------------------------------------------------------
# Temporary data directory
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory for data files."""
    data_dir = tmp_path / "data_store"
    data_dir.mkdir()
    return data_dir


@pytest.fixture
def settings(tmp_data_dir: Path) -> Settings:
    """Provide a Settings instance with a temporary DATA_DIR."""
    return Settings(DATA_DIR=tmp_data_dir)


# ---------------------------------------------------------------------------
# Sample OHLCV data
# ---------------------------------------------------------------------------

def _make_bullish_ohlcv(n_days: int = 250) -> pd.DataFrame:
    """Generate synthetic bullish OHLCV data for testing.

    Creates a steadily rising price series with realistic volume,
    suitable for testing indicator calculations. The data is designed
    to produce bullish signals on most indicators.
    """
    np.random.seed(42)
    dates = pd.bdate_range(end=datetime.now(), periods=n_days)

    # Start at $100 with slight upward drift
    returns = np.random.normal(0.001, 0.015, n_days)
    prices = 100.0 * np.exp(np.cumsum(returns))

    high = prices * (1 + np.abs(np.random.normal(0, 0.01, n_days)))
    low = prices * (1 - np.abs(np.random.normal(0, 0.01, n_days)))
    open_prices = prices * (1 + np.random.normal(0, 0.005, n_days))

    # Volume with occasional spikes
    base_volume = np.random.randint(500_000, 2_000_000, n_days).astype(float)
    # Add a volume surge on the last day (for bullish volume tests)
    base_volume[-1] = base_volume[-20:].mean() * 2.0

    df = pd.DataFrame(
        {
            "Open": open_prices,
            "High": high,
            "Low": low,
            "Close": prices,
            "Volume": base_volume,
        },
        index=dates,
    )
    return df


def _make_bearish_ohlcv(n_days: int = 250) -> pd.DataFrame:
    """Generate synthetic bearish OHLCV data for testing.

    Creates a declining price series that will be below EMA200,
    triggering the bear regime hard veto.
    """
    np.random.seed(99)
    dates = pd.bdate_range(end=datetime.now(), periods=n_days)

    # Start at $200 with downward drift
    returns = np.random.normal(-0.002, 0.015, n_days)
    prices = 200.0 * np.exp(np.cumsum(returns))

    high = prices * (1 + np.abs(np.random.normal(0, 0.01, n_days)))
    low = prices * (1 - np.abs(np.random.normal(0, 0.01, n_days)))
    open_prices = prices * (1 + np.random.normal(0, 0.005, n_days))
    volume = np.random.randint(500_000, 2_000_000, n_days).astype(float)

    df = pd.DataFrame(
        {
            "Open": open_prices,
            "High": high,
            "Low": low,
            "Close": prices,
            "Volume": volume,
        },
        index=dates,
    )
    return df


@pytest.fixture
def bullish_df() -> pd.DataFrame:
    """Bullish OHLCV data (250 trading days, rising trend)."""
    return _make_bullish_ohlcv()


@pytest.fixture
def bearish_df() -> pd.DataFrame:
    """Bearish OHLCV data (250 trading days, falling trend)."""
    return _make_bearish_ohlcv()


@pytest.fixture
def short_df() -> pd.DataFrame:
    """Too-short OHLCV data (50 trading days — below MIN_OHLCV_ROWS)."""
    return _make_bullish_ohlcv(n_days=50)


# ---------------------------------------------------------------------------
# Sample Signal / TradeOrder / ExitEvent
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_signal() -> Signal:
    """A well-formed Grade-A momentum signal."""
    return Signal(
        symbol="AAPL",
        strategy="momentum",
        direction="long",
        entry_price=195.50,
        stop_price=190.20,
        target_price=208.45,
        signal_strength=0.82,
        grade=Grade.A,
        rsi_value=62.3,
        rsi_score=0.80,
        macd_histogram=0.45,
        macd_score=0.85,
        ema_score=0.90,
        volume_ratio=2.1,
        volume_score=0.75,
        ripster_score=0.80,
        obv_confirming=True,
        timestamp=datetime.now(),
    )


@pytest.fixture
def sample_signal_grade_b() -> Signal:
    """A Grade-B swing signal."""
    return Signal(
        symbol="MSFT",
        strategy="swing",
        direction="long",
        entry_price=420.00,
        stop_price=410.00,
        target_price=438.00,
        signal_strength=0.70,
        grade=Grade.B,
        rsi_value=55.0,
        rsi_score=0.65,
        macd_histogram=0.20,
        macd_score=0.60,
        ema_score=0.75,
        volume_ratio=1.2,
        volume_score=0.60,
        ripster_score=0.70,
        obv_confirming=True,
        timestamp=datetime.now(),
    )


@pytest.fixture
def sample_trade_order(sample_signal: Signal) -> TradeOrder:
    """A fully sized trade order based on sample_signal."""
    return TradeOrder(
        signal=sample_signal,
        quantity=15,
        risk_amount=79.50,
        max_risk_dollars=135.00,
        currency="USD",
        ai_decision="APPROVE",
        ai_reasoning="No negative news found",
        ai_cost_usd=0.023,
    )


@pytest.fixture
def sample_exit_event() -> ExitEvent:
    """An exit event for a stop-hit scenario."""
    return ExitEvent(
        symbol="AAPL",
        exit_price=190.20,
        exit_reason=ExitReason.STOP_HIT,
        exit_date=datetime.now(),
        pnl_gross=-79.50,
    )
