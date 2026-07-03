"""Tests for configuration and settings."""

from __future__ import annotations

from config.settings import Settings, get_settings, weights_for_strategy
from config.universe import (
    ALL_SYMBOLS,
    CA_WATCHLIST,
    US_WATCHLIST,
    get_currency,
    is_canadian,
)


class TestSettings:
    """Tests for the Settings class."""

    def test_default_values(self, settings: Settings) -> None:
        """Verify key defaults match the architecture document."""
        assert settings.IBKR_PORT == 7497
        assert settings.TOTAL_CAPITAL == 12_000.0
        assert settings.MAX_POSITION_SIZE_PCT == 0.015
        assert settings.MAX_OPEN_POSITIONS == 25
        assert settings.MAX_MOMENTUM_POSITIONS == 18
        assert settings.MAX_SWING_POSITIONS == 15
        assert settings.MAX_PEAD_POSITIONS == 5
        assert settings.DAILY_LOSS_LIMIT_PCT == 0.015
        assert settings.ATR_STOP_MULTIPLIER == 1.5
        assert settings.RISK_REWARD_MIN == 1.8
        assert settings.SCAN_INTERVAL_MINUTES == 60
        assert settings.HOLD_MAX_DAYS == 20
        assert settings.REENTRY_COOLDOWN_MINUTES == 90
        assert settings.ORDER_CUTOFF_MINUTES_BEFORE_CLOSE == 5
        assert settings.GHOST_POSITION_MAX_DEFER_HOURS == 48

    def test_paper_trading_derived_from_port(self) -> None:
        """IS_PAPER_TRADING should be True for port 7497, False for 7496."""
        paper = Settings(IBKR_PORT=7497)
        assert paper.IS_PAPER_TRADING is True

        live = Settings(IBKR_PORT=7496)
        assert live.IS_PAPER_TRADING is False

    def test_capital_by_currency(self, settings: Settings) -> None:
        """Verify capital allocation per currency."""
        assert settings.get_capital_for_currency("USD") == 9_000.0
        assert settings.get_capital_for_currency("CAD") == 3_000.0
        assert settings.get_capital_for_currency("EUR") == 0.0

    def test_capital_case_insensitive(self, settings: Settings) -> None:
        """Currency lookup should be case-insensitive."""
        assert settings.get_capital_for_currency("usd") == 9_000.0
        assert settings.get_capital_for_currency("cad") == 3_000.0

    def test_momentum_weights_sum_to_one(self) -> None:
        """Momentum strategy weights must sum to 1.0."""
        total = sum(Settings.MOMENTUM_WEIGHTS.values())
        assert abs(total - 1.0) < 1e-9

    def test_swing_weights_sum_to_one(self) -> None:
        """Swing strategy weights must sum to 1.0."""
        total = sum(Settings.SWING_WEIGHTS.values())
        assert abs(total - 1.0) < 1e-9

    def test_momentum_weights_keys(self) -> None:
        """Both weight dicts should have the same five indicator keys."""
        expected = {"rsi", "macd", "ema", "volume", "ripster"}
        assert set(Settings.MOMENTUM_WEIGHTS.keys()) == expected
        assert set(Settings.SWING_WEIGHTS.keys()) == expected

    def test_grade_thresholds(self, settings: Settings) -> None:
        """Grade thresholds must match architecture document."""
        assert settings.GRADE_A_THRESHOLD == 0.78
        assert settings.GRADE_B_THRESHOLD == 0.65
        assert settings.GRADE_C_THRESHOLD == 0.38


class TestWeightsForStrategy:
    """Tests for the weights_for_strategy helper."""

    def test_momentum_strategies(self) -> None:
        """Momentum-family strategies use momentum weights."""
        for strategy in ("momentum", "vcp_breakout", "pead"):
            weights = weights_for_strategy(strategy)
            assert weights["ema"] == 0.25
            assert weights["macd"] == 0.25
            assert weights["rsi"] == 0.10

    def test_swing_strategies(self) -> None:
        """Swing-family strategies use swing weights."""
        for strategy in ("swing", "mean_reversion"):
            weights = weights_for_strategy(strategy)
            assert weights["rsi"] == 0.25
            assert weights["ema"] == 0.25
            assert weights["macd"] == 0.10

    def test_unknown_strategy_raises(self) -> None:
        """Unknown strategy names should raise ValueError."""
        import pytest

        with pytest.raises(ValueError, match="Unknown strategy"):
            weights_for_strategy("turbo_scalp")


class TestUniverse:
    """Tests for the watchlist universe."""

    def test_us_watchlist_not_empty(self) -> None:
        assert len(US_WATCHLIST) > 0

    def test_ca_watchlist_all_to_suffix(self) -> None:
        """All Canadian symbols should end with .TO."""
        for sym in CA_WATCHLIST:
            assert sym.endswith(".TO"), f"{sym} missing .TO suffix"

    def test_all_symbols_is_deduplicated(self) -> None:
        """ALL_SYMBOLS should have no duplicates."""
        assert len(ALL_SYMBOLS) == len(set(ALL_SYMBOLS))

    def test_all_symbols_contains_both_lists(self) -> None:
        """ALL_SYMBOLS should contain every US and CA symbol."""
        for sym in US_WATCHLIST:
            assert sym in ALL_SYMBOLS
        for sym in CA_WATCHLIST:
            assert sym in ALL_SYMBOLS

    def test_is_canadian(self) -> None:
        assert is_canadian("SHOP.TO") is True
        assert is_canadian("AAPL") is False
        assert is_canadian("RY.TO") is True

    def test_get_currency(self) -> None:
        assert get_currency("AAPL") == "USD"
        assert get_currency("SHOP.TO") == "CAD"
        assert get_currency("NVDA") == "USD"
        assert get_currency("TD.TO") == "CAD"
