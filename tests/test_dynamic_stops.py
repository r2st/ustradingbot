"""Tests for the dynamic stop-loss engine (execution/stops.py)."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from config.settings import Settings
from execution.stops import (
    StopConfig,
    breakeven_stop,
    compute_atr,
    compute_dynamic_stop,
    days_between,
    reached_r_multiple,
    resolve_stop_config,
    trailing_stop,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _flat_df(price: float, atr: float = 2.0, rows: int = 30) -> pd.DataFrame:
    """A frame whose last close is *price* and whose ATR is ~*atr*."""
    high = np.full(rows, price + atr / 2)
    low = np.full(rows, price - atr / 2)
    close = np.full(rows, price, dtype=float)
    open_ = np.full(rows, price, dtype=float)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close,
         "Volume": np.ones(rows)},
        index=pd.bdate_range(end=datetime.now(), periods=rows),
    )


def _pos(**kw):
    base = {
        "symbol": "AAPL",
        "strategy": "momentum",
        "entry_price": 100.0,
        "stop_price": 95.0,
        "original_stop_loss": 95.0,
        "entry_time": datetime.now().isoformat(),
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #


def test_trailing_stop_math() -> None:
    assert trailing_stop(110.0, 2.0, 2.0) == pytest.approx(106.0)


def test_breakeven_stop_adds_buffer() -> None:
    assert breakeven_stop(100.0, 0.001) == pytest.approx(100.1)


def test_reached_r_multiple() -> None:
    # entry 100, stop 95 -> 1R = 5 dollars. At 105 that's exactly 1R.
    assert reached_r_multiple(100.0, 105.0, 95.0, 1.0) is True
    assert reached_r_multiple(100.0, 104.0, 95.0, 1.0) is False
    # non-positive risk is never satisfied
    assert reached_r_multiple(100.0, 200.0, 100.0, 1.0) is False


def test_days_between_parses_iso_and_datetime() -> None:
    now = datetime(2026, 7, 10, 12, 0, 0)
    assert days_between((now - timedelta(days=3)).isoformat(), now) == pytest.approx(3.0)
    assert days_between(now - timedelta(days=2), now) == pytest.approx(2.0)
    assert days_between("", now) is None
    assert days_between("not-a-date", now) is None


def test_compute_atr_positive() -> None:
    df = _flat_df(100.0, atr=2.0)
    assert compute_atr(df, 14) == pytest.approx(2.0, abs=0.01)


def test_compute_atr_insufficient_rows() -> None:
    assert compute_atr(_flat_df(100.0, rows=5), 14) == 0.0


# --------------------------------------------------------------------------- #
# resolve_stop_config
# --------------------------------------------------------------------------- #


def test_resolve_stop_config_uses_global_defaults(settings: Settings) -> None:
    cfg = resolve_stop_config(settings, "momentum")
    assert cfg.trail_atr_multiplier == settings.TRAIL_ATR_MULTIPLIER
    assert cfg.enable_breakeven == settings.ENABLE_BREAKEVEN_STOP


def test_resolve_stop_config_applies_overrides(tmp_data_dir) -> None:
    s = Settings(
        DATA_DIR=tmp_data_dir,
        STOP_OVERRIDES_BY_STRATEGY={
            "mean_reversion": {
                "TRAIL_ATR_MULTIPLIER": 1.25,
                "enable_time_tighten": False,
            }
        },
    )
    cfg = resolve_stop_config(s, "mean_reversion")
    assert cfg.trail_atr_multiplier == 1.25  # settings-name alias
    assert cfg.enable_time_tighten is False  # field-name alias
    # unrelated strategy still uses defaults
    assert resolve_stop_config(s, "momentum").trail_atr_multiplier == 2.0


def test_resolve_stop_config_ignores_unknown_keys(tmp_data_dir) -> None:
    s = Settings(
        DATA_DIR=tmp_data_dir,
        STOP_OVERRIDES_BY_STRATEGY={"momentum": {"NONSENSE": 5}},
    )
    # Must not raise; unknown key silently ignored.
    assert resolve_stop_config(s, "momentum").trail_atr_multiplier == 2.0


# --------------------------------------------------------------------------- #
# compute_dynamic_stop — trailing
# --------------------------------------------------------------------------- #


def test_trailing_raises_stop_when_in_profit() -> None:
    cfg = StopConfig()
    # price up to 110, entry 100 -> +10% > 5% activation, ATR 2, trail 2x = 106
    df = _flat_df(110.0, atr=2.0)
    decision = compute_dynamic_stop(_pos(), df, cfg)
    assert decision is not None
    assert decision.new_stop == pytest.approx(106.0, abs=0.05)
    # breakeven (100.1) is also a candidate but trailing is higher, so it wins.
    assert decision.reason == "trailing"


def test_no_raise_when_not_in_profit() -> None:
    cfg = StopConfig()
    # price 101 -> +1% below 5% activation; not yet 1R either -> nothing.
    df = _flat_df(101.0, atr=2.0)
    assert compute_dynamic_stop(_pos(), df, cfg) is None


def test_never_lowers_existing_stop() -> None:
    cfg = StopConfig()
    df = _flat_df(110.0, atr=2.0)
    # current stop already at 108 -> proposed 106 must be rejected.
    assert compute_dynamic_stop(_pos(stop_price=108.0), df, cfg) is None


# --------------------------------------------------------------------------- #
# compute_dynamic_stop — breakeven
# --------------------------------------------------------------------------- #


def test_breakeven_when_1r_reached_but_trail_inactive() -> None:
    # Disable trailing so breakeven is the sole mechanism.
    cfg = StopConfig(enable_trailing=False, enable_time_tighten=False)
    # entry 100 stop 95 -> 1R at 105. price 106 = 1.2R, +6%.
    df = _flat_df(106.0, atr=2.0)
    decision = compute_dynamic_stop(_pos(), df, cfg)
    assert decision is not None
    assert decision.reason == "breakeven"
    assert decision.new_stop == pytest.approx(100.1, abs=0.01)


# --------------------------------------------------------------------------- #
# compute_dynamic_stop — time-based tightening
# --------------------------------------------------------------------------- #


def test_time_tighten_on_stale_stagnant_trade() -> None:
    cfg = StopConfig(
        enable_trailing=False,
        enable_breakeven=False,
        time_tighten_days=5,
        time_tighten_atr_multiplier=1.0,
        time_stagnant_profit_pct=0.02,
    )
    # Held 6 days, price 101 (+1% < 2% stagnant threshold), ATR 2 -> stop 101-2=99
    old_entry = (datetime.now() - timedelta(days=6)).isoformat()
    df = _flat_df(101.0, atr=2.0)
    decision = compute_dynamic_stop(_pos(entry_time=old_entry), df, cfg)
    assert decision is not None
    assert decision.reason == "time_tighten"
    assert decision.new_stop == pytest.approx(99.0, abs=0.05)


def test_time_tighten_skipped_when_recently_opened() -> None:
    cfg = StopConfig(enable_trailing=False, enable_breakeven=False)
    df = _flat_df(101.0, atr=2.0)
    # only 1 day old -> no tighten
    assert compute_dynamic_stop(_pos(), df, cfg) is None


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #


def test_invalid_entry_returns_none() -> None:
    assert compute_dynamic_stop(_pos(entry_price=0.0), _flat_df(110.0), StopConfig()) is None


def test_empty_df_returns_none() -> None:
    assert compute_dynamic_stop(_pos(), pd.DataFrame(), StopConfig()) is None


def test_volatility_stops_disabled_falls_back_to_no_atr() -> None:
    # With ATR sizing off, trailing/time mechanisms (which need ATR) do nothing,
    # but breakeven (no ATR needed) still works once 1R is reached.
    cfg = StopConfig(enable_volatility_stops=False)
    df = _flat_df(106.0, atr=2.0)
    decision = compute_dynamic_stop(_pos(), df, cfg)
    assert decision is not None
    assert decision.reason == "breakeven"
