"""Shared fixtures for the short-strategies test suite.

Frames are built bar-by-bar from explicit arrays so each test isolates
exactly the pattern (or anti-pattern) it exercises.  Detector tests pass a
``SharedFilterConfig`` with ``min_atr_pct=0.0`` so pattern assertions are
decoupled from the ATR-percent floor (which has its own dedicated tests).
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Sequence

import numpy as np
import pandas as pd
import pytest

from short_strategies.common.config import SharedFilterConfig, get_short_config


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch):
    """Hermetic settings + a fresh short-config per test."""
    monkeypatch.setenv("USTB_SKIP_KEY_FILES", "1")
    get_short_config.cache_clear()
    yield
    get_short_config.cache_clear()


def make_df(
    close: Sequence[float],
    open_: Optional[Sequence[float]] = None,
    high: Optional[Sequence[float]] = None,
    low: Optional[Sequence[float]] = None,
    volume: Optional[Sequence[float]] = None,
) -> pd.DataFrame:
    """Build an OHLCV frame from explicit arrays.

    Defaults derive plausible bars from the closes: open = prior close,
    high/low = a symmetric 2% envelope (comfortably above the 1.5% ATR
    floor), volume = 1,000,000.
    """
    close_arr = [float(c) for c in close]
    n = len(close_arr)
    if open_ is None:
        open_ = [close_arr[0]] + close_arr[:-1]
    if high is None:
        high = [max(o, c) * 1.01 for o, c in zip(open_, close_arr)]
    if low is None:
        low = [min(o, c) * 0.99 for o, c in zip(open_, close_arr)]
    if volume is None:
        volume = [1_000_000.0] * n
    dates = pd.bdate_range(end=datetime.now(), periods=n)
    return pd.DataFrame(
        {
            "Open": [float(x) for x in open_],
            "High": [float(x) for x in high],
            "Low": [float(x) for x in low],
            "Close": close_arr,
            "Volume": [float(x) for x in volume],
        },
        index=dates,
    )


def downtrend_closes(n: int = 250, start: float = 100.0, drift: float = -0.004,
                     seed: int = 7) -> List[float]:
    """A noisy but persistent downtrend series."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.008, n)
    return list(start * np.exp(np.cumsum(rets)))


def uptrend_closes(n: int = 250, start: float = 100.0, drift: float = 0.004,
                   seed: int = 7) -> List[float]:
    """A noisy but persistent uptrend series."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.008, n)
    return list(start * np.exp(np.cumsum(rets)))


@pytest.fixture
def loose_filters() -> SharedFilterConfig:
    """Filter config with the ATR%-floor disabled (pattern tests)."""
    return SharedFilterConfig(min_atr_pct=0.0)


@pytest.fixture
def downtrend_df() -> pd.DataFrame:
    return make_df(downtrend_closes())


@pytest.fixture
def uptrend_df() -> pd.DataFrame:
    return make_df(uptrend_closes())
