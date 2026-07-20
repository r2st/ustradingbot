"""Tests for hard portfolio-risk limits (P0-1): sector, correlation, VaR/CVaR."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from config.settings import Settings
from risk.limits import (
    conditional_var,
    correlation_cap_check,
    historical_var,
    max_correlation_for_symbol,
    parametric_var,
    portfolio_returns,
    portfolio_var_cvar,
    projected_sector_pct,
    sector_cap_check,
)
from risk.manager import RiskManager
from signals.signal_types import Grade, Signal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pos(symbol: str, price: float, qty: int) -> dict:
    return {"symbol": symbol, "entry_price": price, "quantity": qty}


def _make_signal(symbol: str = "AAPL", entry: float = 100.0) -> Signal:
    return Signal(
        symbol=symbol,
        strategy="momentum",
        entry_price=entry,
        stop_price=round(entry * 0.95, 2),
        target_price=round(entry * 1.12, 2),
        signal_strength=0.8,
        grade=Grade.A,
        timestamp=datetime.now(),
    )


def _returns(seed: int, n: int = 60, scale: float = 0.02) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=datetime.now(), periods=n)
    return pd.Series(rng.normal(0.0005, scale, n), index=idx)


# ---------------------------------------------------------------------------
# Sector concentration
# ---------------------------------------------------------------------------


class TestSectorConcentration:
    def test_projected_pct_empty_book(self) -> None:
        # Only the new position exists -> its sector is 100% of the book.
        pct = projected_sector_pct([], "AAPL", 1000.0)
        assert pct == pytest.approx(1.0)

    def test_projected_pct_diversified(self) -> None:
        # AAPL/MSFT are Technology; JPM is Financials.  Adding a small Tech
        # position to a book already split across sectors stays moderate.
        book = [_pos("JPM", 100.0, 50), _pos("XOM", 100.0, 50)]
        pct = projected_sector_pct(book, "AAPL", 1000.0)
        # tech = 1000 / (5000 + 5000 + 1000) = 1000/11000
        assert pct == pytest.approx(1000.0 / 11000.0, rel=1e-6)

    def test_cap_check_rejects_over_concentration(self) -> None:
        book = [_pos("MSFT", 100.0, 100)]  # 10_000 Technology
        # Adding 10_000 more Technology -> tech = 20000/20000 = 100% > 30%.
        ok, pct = sector_cap_check(book, "AAPL", 10_000.0, 0.30)
        assert ok is False
        assert pct == pytest.approx(1.0)

    def test_cap_check_allows_within_limit(self) -> None:
        book = [_pos("JPM", 100.0, 100), _pos("XOM", 100.0, 100)]  # 20k non-tech
        ok, pct = sector_cap_check(book, "AAPL", 1_000.0, 0.30)
        assert ok is True
        assert pct < 0.30


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


class TestCorrelation:
    def test_identical_series_is_perfectly_correlated(self) -> None:
        base = _returns(1)
        rbs = {"AAPL": base, "MSFT": base.copy()}
        worst = max_correlation_for_symbol("AAPL", ["MSFT"], rbs)
        assert worst is not None
        assert worst[0] == "MSFT"
        assert worst[1] == pytest.approx(1.0, abs=1e-9)

    def test_cap_check_rejects_high_correlation(self) -> None:
        base = _returns(2)
        rbs = {"AAPL": base, "MSFT": base * 1.01}  # near-identical
        ok, worst = correlation_cap_check("AAPL", ["MSFT"], rbs, max_corr=0.85)
        assert ok is False
        assert worst is not None and worst[1] > 0.85

    def test_cap_check_passes_uncorrelated(self) -> None:
        rbs = {"AAPL": _returns(3), "MSFT": _returns(999)}
        ok, worst = correlation_cap_check("AAPL", ["MSFT"], rbs, max_corr=0.85)
        assert ok is True

    def test_insufficient_data_fails_open(self) -> None:
        # No returns for the new symbol -> None -> gate fails open (ok=True).
        ok, worst = correlation_cap_check("AAPL", ["MSFT"], {"MSFT": _returns(4)}, 0.85)
        assert ok is True
        assert worst is None

    def test_negative_correlation_not_rejected(self) -> None:
        base = _returns(5)
        rbs = {"AAPL": base, "SH": -base}  # perfectly inverse
        ok, worst = correlation_cap_check("AAPL", ["SH"], rbs, max_corr=0.85)
        assert ok is True
        assert worst is not None and worst[1] == pytest.approx(-1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# VaR / CVaR
# ---------------------------------------------------------------------------


class TestVaR:
    def test_parametric_var_positive_for_volatile_series(self) -> None:
        r = _returns(6, scale=0.03)
        var = parametric_var(r, confidence=0.95)
        assert var > 0

    def test_historical_var_matches_quantile(self) -> None:
        arr = np.linspace(-0.10, 0.10, 101)  # symmetric
        var = historical_var(arr, confidence=0.95)
        # 5th percentile of [-0.1,0.1] linspace ~ -0.09 -> VaR ~ 0.09
        assert var == pytest.approx(0.09, abs=0.005)

    def test_cvar_at_least_var(self) -> None:
        r = _returns(7, scale=0.03)
        var = historical_var(r, 0.95)
        cvar = conditional_var(r, 0.95)
        assert cvar >= var - 1e-9

    def test_empty_returns_zero(self) -> None:
        assert parametric_var([], 0.95) == 0.0
        assert historical_var([1.0], 0.95) == 0.0
        assert conditional_var([], 0.95) == 0.0

    def test_portfolio_returns_weighted(self) -> None:
        a = _returns(8)
        b = _returns(9)
        port = portfolio_returns({"A": a, "B": b}, {"A": 1.0, "B": 1.0})
        assert port is not None
        # Equal-weight portfolio return equals mean of the two aligned series.
        joined = pd.concat([a, b], axis=1, join="inner").dropna()
        expected = joined.mean(axis=1)
        assert np.allclose(port.to_numpy(), expected.to_numpy())

    def test_portfolio_var_cvar_payload(self) -> None:
        rbs = {"A": _returns(10, scale=0.03), "B": _returns(11, scale=0.03)}
        out = portfolio_var_cvar(rbs, {"A": 1.0, "B": 1.0}, 0.95)
        assert out["observations"] > 0
        assert out["historical_var"] >= 0
        assert out["parametric_var"] >= 0
        assert out["cvar"] >= 0
        assert out["confidence"] == 0.95

    def test_portfolio_var_cvar_no_data(self) -> None:
        out = portfolio_var_cvar({}, {}, 0.95)
        assert out["observations"] == 0
        assert out["historical_var"] == 0.0


# ---------------------------------------------------------------------------
# pre_check integration — the hard gates
# ---------------------------------------------------------------------------


def _register(rm: RiskManager, symbol: str, price: float, qty: int) -> None:
    """Inject an open position directly into the manager's book."""
    rm._positions[symbol] = _pos(symbol, price, qty)


class TestPreCheckGates:
    def test_sector_gate_blocks_concentration(self, settings: Settings) -> None:
        settings.MAX_SECTOR_CONCENTRATION_PCT = 0.30
        # Big USD pool so a single stock can be sized up to a large notional.
        rm = RiskManager(settings)
        # Fill the book with Technology names (AAPL, MSFT both Tech).
        _register(rm, "MSFT", 100.0, 200)  # 20k tech
        sig = _make_signal("AAPL", entry=100.0)
        ok, reason = rm.pre_check(sig)
        assert ok is False
        assert reason.startswith("sector_concentration:")

    def test_sector_gate_off_when_disabled(self, settings: Settings) -> None:
        settings.ENFORCE_SECTOR_LIMIT = False
        rm = RiskManager(settings)
        _register(rm, "MSFT", 100.0, 200)
        sig = _make_signal("AAPL", entry=100.0)
        ok, reason = rm.pre_check(sig)
        assert ok is True

    def test_correlation_gate_blocks(self, settings: Settings) -> None:
        settings.ENFORCE_SECTOR_LIMIT = False  # isolate the correlation gate
        settings.ENFORCE_CORRELATION_LIMIT = True
        settings.MAX_POSITION_CORRELATION = 0.85
        rm = RiskManager(settings)
        _register(rm, "MSFT", 100.0, 10)
        base = _returns(42)

        def provider(symbols):
            return {s: base for s in symbols}  # everything identical -> corr 1.0

        rm.returns_provider = provider
        sig = _make_signal("AAPL", entry=100.0)
        ok, reason = rm.pre_check(sig)
        assert ok is False
        assert reason.startswith("correlation_too_high:")

    def test_correlation_gate_fails_open_without_provider(
        self, settings: Settings
    ) -> None:
        settings.ENFORCE_SECTOR_LIMIT = False
        settings.ENFORCE_CORRELATION_LIMIT = True
        rm = RiskManager(settings)
        _register(rm, "MSFT", 100.0, 10)
        sig = _make_signal("AAPL", entry=100.0)
        ok, reason = rm.pre_check(sig)  # no provider -> gate skipped
        assert ok is True

    def test_portfolio_var_gate_blocks(self, settings: Settings) -> None:
        settings.ENFORCE_SECTOR_LIMIT = False
        settings.ENFORCE_CORRELATION_LIMIT = False
        settings.ENFORCE_PORTFOLIO_VAR_LIMIT = True
        settings.PORTFOLIO_VAR_LIMIT_PCT = 0.001  # absurdly tight -> always trips
        rm = RiskManager(settings)
        _register(rm, "MSFT", 100.0, 10)

        def provider(symbols):
            return {s: _returns(hash(s) % 1000, scale=0.05) for s in symbols}

        rm.returns_provider = provider
        sig = _make_signal("AAPL", entry=100.0)
        ok, reason = rm.pre_check(sig)
        assert ok is False
        assert reason.startswith("portfolio_var_exceeded:")

    def test_daily_loss_halt_blocks(self, settings: Settings) -> None:
        settings.ENFORCE_SECTOR_LIMIT = False
        settings.HALT_NEW_ENTRIES_ON_DAILY_LOSS = True
        settings.DAILY_LOSS_LIMIT_PCT = 0.015
        rm = RiskManager(settings)
        # Drive the daily accumulator below the halt threshold.
        rm.record_daily_pnl(-settings.TOTAL_CAPITAL * 0.02)
        sig = _make_signal("AAPL", entry=100.0)
        ok, reason = rm.pre_check(sig)
        assert ok is False
        assert reason.startswith("daily_loss_halt:") or reason.startswith(
            "daily_loss_limit_reached:"
        )
