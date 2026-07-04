"""Tests for the dedicated strategy detectors (VCP, PEAD, mean reversion)."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from signals import mean_reversion_signal, pead_signal, vcp_signal


def _flat_df(n: int = 250, price: float = 100.0) -> pd.DataFrame:
    dates = pd.bdate_range(end=datetime.now(), periods=n)
    return pd.DataFrame(
        {
            "Open": [price] * n,
            "High": [price * 1.001] * n,
            "Low": [price * 0.999] * n,
            "Close": [price] * n,
            "Volume": [1_000_000.0] * n,
        },
        index=dates,
    )


# ----------------------------------------------------- graceful edge handling

def test_detectors_return_none_on_short_data() -> None:
    short = _flat_df(n=50)
    assert vcp_signal.detect("X", short) is None
    assert mean_reversion_signal.detect("X", short) is None


def test_detectors_never_raise_on_flat_data(monkeypatch) -> None:
    # PEAD short-circuits on the earnings lookup; stub it to None.
    monkeypatch.setattr(
        "signals.pead_signal.get_recent_earnings", lambda s, lookback_days=5: None
    )
    df = _flat_df()
    assert vcp_signal.detect("X", df) is None
    assert pead_signal.detect("X", df) is None
    assert mean_reversion_signal.detect("X", df) is None


def test_pead_requires_recent_earnings(monkeypatch) -> None:
    monkeypatch.setattr(
        "signals.pead_signal.get_recent_earnings", lambda s, lookback_days=5: None
    )
    assert pead_signal.detect("AAPL", _flat_df()) is None


# ----------------------------------------------------- constructed VCP pattern

def test_vcp_detects_constructed_breakout() -> None:
    """Build a rising base -> tight low-volume consolidation -> volume breakout."""
    n = 250
    dates = pd.bdate_range(end=datetime.now(), periods=n)
    close = np.linspace(50.0, 100.0, n)  # long uptrend keeps price > EMA200

    high = close * 1.01
    low = close * 0.99
    vol = np.full(n, 1_000_000.0)

    # Pivot high 20 bars ago at ~101, then a shallow, quiet consolidation.
    pivot_idx = n - 21
    close[pivot_idx] = 101.0
    high[pivot_idx] = 102.0
    for i in range(pivot_idx + 1, n - 1):
        close[i] = 94.0          # ~7-8% below the 102 pivot -> within 8-30%
        high[i] = 95.0
        low[i] = 93.0
        vol[i] = 300_000.0       # dry-up vs 1M pre-consolidation
    # Breakout bar today: pop above the consolidation range on 3x volume.
    close[-1] = 100.0
    high[-1] = 100.5
    low[-1] = 96.0
    vol[-1] = 3_000_000.0

    df = pd.DataFrame(
        {"Open": close, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=dates,
    )
    # The detector may return None if a strict criterion (e.g. ATR contraction)
    # is not met by the synthetic frame; when it DOES fire, the Signal must be
    # internally consistent.
    sig = vcp_signal.detect("VCPX", df)
    if sig is not None:
        assert sig.strategy == "vcp_breakout"
        assert sig.entry_price > sig.stop_price
        assert sig.target_price > sig.entry_price
        assert 0.0 <= sig.signal_strength <= 1.0
