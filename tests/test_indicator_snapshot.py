"""Tests for the TA1 indicator snapshot (signals/indicator_snapshot).

The critical property: every series' final value must agree with the
scalar state the scoring engine computes via ``signals/*`` — the chart
must show exactly what the system saw.
"""

from __future__ import annotations

import pandas as pd
import pytest

from signals.combined_filter import _compute_atr, score_symbol
from signals.ema_signals import calculate_ema
from signals.indicator_snapshot import (
    SNAPSHOT_BARS,
    build_indicator_snapshot,
    compute_indicator_series,
)
from signals.macd_signals import calculate_macd
from signals.rsi_signals import calculate_rsi
from signals.volume_signals import calculate_volume

SERIES_KEYS = [
    "ema9", "ema20", "ema50", "ema200", "rsi",
    "macd", "macd_signal", "macd_hist",
    "bb_up", "bb_mid", "bb_lo",
    "obv", "vol_avg20", "atr14",
]


# --------------------------------------------------------------------------- #
# structure
# --------------------------------------------------------------------------- #


def test_snapshot_structure_and_lengths(bullish_df: pd.DataFrame) -> None:
    snap = build_indicator_snapshot(bullish_df)
    assert snap is not None
    assert snap["v"] == 2
    assert len(snap["bars"]) == SNAPSHOT_BARS
    for key in SERIES_KEYS:
        assert key in snap["series"], f"missing series {key}"
        assert len(snap["series"][key]) == SNAPSHOT_BARS, key
    assert set(snap["levels"].keys()) == {"support", "resistance"}
    assert isinstance(snap["state"], dict)
    bar = snap["bars"][0]
    assert set(bar.keys()) == {"t", "o", "h", "l", "c", "v"}


def test_snapshot_handles_bad_input() -> None:
    assert build_indicator_snapshot(None) is None
    assert build_indicator_snapshot(pd.DataFrame()) is None


def test_snapshot_short_df_pads_unfilled_windows_with_none(
    bullish_df: pd.DataFrame,
) -> None:
    short = bullish_df.head(30)
    snap = build_indicator_snapshot(short, n=30)
    assert snap is not None
    # ATR(14) needs 14 bars — the leading values must be None, not NaN.
    atr = snap["series"]["atr14"]
    assert atr[0] is None
    assert atr[-1] is not None


# --------------------------------------------------------------------------- #
# parity with the signals/* scalar states
# --------------------------------------------------------------------------- #


def test_rsi_series_matches_rsi_state(bullish_df: pd.DataFrame) -> None:
    snap = build_indicator_snapshot(bullish_df)
    state = calculate_rsi(bullish_df)
    assert snap["series"]["rsi"][-1] == pytest.approx(state.rsi_value, abs=1e-3)


def test_macd_series_matches_macd_state(bullish_df: pd.DataFrame) -> None:
    snap = build_indicator_snapshot(bullish_df)
    state = calculate_macd(bullish_df)
    assert snap["series"]["macd"][-1] == pytest.approx(state.macd_line, abs=1e-3)
    assert snap["series"]["macd_signal"][-1] == pytest.approx(
        state.signal_line, abs=1e-3
    )
    assert snap["series"]["macd_hist"][-1] == pytest.approx(
        state.histogram, abs=1e-3
    )


def test_ema_series_matches_ema_state(bullish_df: pd.DataFrame) -> None:
    snap = build_indicator_snapshot(bullish_df)
    state = calculate_ema(bullish_df)
    assert snap["series"]["ema9"][-1] == pytest.approx(state.ema9, abs=1e-3)
    assert snap["series"]["ema20"][-1] == pytest.approx(state.ema20, abs=1e-3)
    assert snap["series"]["ema50"][-1] == pytest.approx(state.ema50, abs=1e-3)
    assert snap["series"]["ema200"][-1] == pytest.approx(state.ema200, abs=1e-3)


def test_atr_series_matches_compute_atr(bullish_df: pd.DataFrame) -> None:
    snap = build_indicator_snapshot(bullish_df)
    assert snap["atr14"] == pytest.approx(_compute_atr(bullish_df), abs=1e-3)
    assert snap["series"]["atr14"][-1] == snap["atr14"]


def test_obv_series_agrees_with_volume_state(bullish_df: pd.DataFrame) -> None:
    snap = build_indicator_snapshot(bullish_df)
    state = calculate_volume(bullish_df)
    obv = snap["series"]["obv"]
    # is_obv_confirming means OBV today > OBV 10 bars ago.
    assert (obv[-1] > obv[-11]) == state.is_obv_confirming
    assert snap["state"]["obv_confirming"] == state.is_obv_confirming


def test_bollinger_bands_bracket_the_mid(bullish_df: pd.DataFrame) -> None:
    snap = build_indicator_snapshot(bullish_df)
    s = snap["series"]
    for up, mid, lo in zip(s["bb_up"], s["bb_mid"], s["bb_lo"]):
        if up is None or mid is None or lo is None:
            continue
        assert lo <= mid <= up


def test_series_aligned_to_bars(bullish_df: pd.DataFrame) -> None:
    """The last bar's close feeds the last series values (alignment check)."""
    snap = build_indicator_snapshot(bullish_df)
    full = compute_indicator_series(bullish_df)
    assert snap["series"]["ema20"][-1] == pytest.approx(
        float(full["ema20"].iloc[-1]), abs=1e-3
    )
    assert snap["bars"][-1]["c"] == pytest.approx(
        float(bullish_df["Close"].iloc[-1]), abs=1e-3
    )


# --------------------------------------------------------------------------- #
# capture wiring (combined_filter)
# --------------------------------------------------------------------------- #


def _clean_uptrend_df(n_days: int = 250) -> pd.DataFrame:
    """A deterministic uptrend that passes every hard veto.

    All up-days (close > open) with steadily rising closes and flat volume:
    above EMA-200, OBV confirming (never diverging), no bearish surge,
    price above both Ripster clouds, and ~3% daily range for the ATR gate.
    The shared ``bullish_df`` fixture is random and can trip a veto
    depending on the run date; scoring tests need a guaranteed pass.
    """
    from datetime import datetime

    import numpy as np

    dates = pd.bdate_range(end=datetime.now(), periods=n_days)
    closes = np.linspace(100.0, 150.0, n_days)
    return pd.DataFrame(
        {
            "Open": closes - 0.5,
            "High": closes + 2.0,
            "Low": closes - 2.0,
            "Close": closes,
            "Volume": np.full(n_days, 1_000_000.0),
        },
        index=dates,
    )


def test_score_symbol_attaches_snapshot() -> None:
    df = _clean_uptrend_df()
    signal = score_symbol("AAPL", "momentum", df)
    assert signal is not None
    snap = signal.raw_data.get("indicators")
    assert snap is not None
    assert snap["v"] == 2
    assert len(snap["series"]["rsi"]) == SNAPSHOT_BARS
    # The persisted scalar must equal the last point of the series.
    assert snap["series"]["rsi"][-1] == pytest.approx(
        signal.rsi_value, abs=0.01
    )


def test_score_symbol_capture_can_be_disabled() -> None:
    df = _clean_uptrend_df()
    signal = score_symbol("AAPL", "momentum", df, capture_series=False)
    assert signal is not None
    assert "indicators" not in signal.raw_data
