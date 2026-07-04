"""Tests for the paper/live trading-mode resolution in settings."""

from __future__ import annotations

from config.settings import Settings


def test_default_is_paper() -> None:
    s = Settings()
    assert s.BROKER == "paper"
    assert s.IS_LIVE_TRADING is False
    assert s.TRADING_MODE == "PAPER"


def test_paper_broker_is_never_live_regardless_of_port() -> None:
    # Even pointed at the live port, the simulated broker is still paper.
    s = Settings(BROKER="paper", IBKR_PORT=7496)
    assert s.IS_LIVE_TRADING is False
    assert s.TRADING_MODE == "PAPER"


def test_ibkr_paper_gateway_is_paper() -> None:
    s = Settings(BROKER="ibkr", IBKR_PORT=7497)
    assert s.IS_LIVE_TRADING is False
    assert s.TRADING_MODE == "PAPER"


def test_ibkr_live_gateway_is_live() -> None:
    s = Settings(BROKER="ibkr", IBKR_PORT=7496)
    assert s.IS_LIVE_TRADING is True
    assert s.TRADING_MODE == "LIVE"


def test_ibkr_broker_is_case_insensitive() -> None:
    assert Settings(BROKER="IBKR", IBKR_PORT=7496).IS_LIVE_TRADING is True


def test_paper_broker_label() -> None:
    assert "simulated" in Settings(BROKER="paper").paper_broker_label.lower()
    assert "7497" in Settings(BROKER="ibkr").paper_broker_label
