"""Tests for the engine's realtime (fast) exit-polling loop (Feature 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from config.settings import Settings
from engine import TradingEngine


class StubExitManager:
    def __init__(self) -> None:
        self.calls = 0

    def manage_exits(self):
        self.calls += 1
        return None


class StubBroker:
    def __init__(self, connected=True) -> None:
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


def _engine(tmp_data_dir: Path, **overrides) -> TradingEngine:
    eng = TradingEngine.__new__(TradingEngine)
    eng.settings = Settings(DATA_DIR=tmp_data_dir, **overrides)
    eng.running = True
    eng.broker = StubBroker()
    eng.exit_manager = StubExitManager()
    return eng


# ------------------------------------------------------------ capability flag


def test_realtime_disabled_for_yfinance(tmp_data_dir) -> None:
    eng = _engine(tmp_data_dir, MARKET_DATA_PROVIDER="yfinance")
    assert eng._realtime_exits_enabled() is False


def test_realtime_enabled_for_alpaca(tmp_data_dir) -> None:
    eng = _engine(tmp_data_dir, MARKET_DATA_PROVIDER="alpaca", ENABLE_REALTIME_EXITS=True)
    assert eng._realtime_exits_enabled() is True


def test_realtime_disabled_when_flag_off(tmp_data_dir) -> None:
    eng = _engine(tmp_data_dir, MARKET_DATA_PROVIDER="alpaca", ENABLE_REALTIME_EXITS=False)
    assert eng._realtime_exits_enabled() is False


# ------------------------------------------------------------ sleep behaviour


async def test_non_realtime_does_single_sleep(tmp_data_dir, monkeypatch) -> None:
    eng = _engine(tmp_data_dir, MARKET_DATA_PROVIDER="yfinance")
    sleeps = []
    monkeypatch.setattr(asyncio, "sleep", lambda s: sleeps.append(s) or _noop())
    await eng._sleep_between_cycles(90.0)
    assert sleeps == [90.0]
    assert eng.exit_manager.calls == 0


async def test_realtime_polls_exits_between_cycles(tmp_data_dir, monkeypatch) -> None:
    eng = _engine(
        tmp_data_dir,
        MARKET_DATA_PROVIDER="alpaca",
        ENABLE_REALTIME_EXITS=True,
        REALTIME_EXIT_POLL_SECONDS=30.0,
    )
    monkeypatch.setattr(eng, "is_market_open", lambda: True)
    monkeypatch.setattr(asyncio, "sleep", lambda s: _noop())
    await eng._sleep_between_cycles(90.0)
    # 90s window / 30s poll -> exits managed ~3 times.
    assert eng.exit_manager.calls == 3


async def test_realtime_skips_exits_when_market_closed(tmp_data_dir, monkeypatch) -> None:
    eng = _engine(
        tmp_data_dir,
        MARKET_DATA_PROVIDER="alpaca",
        ENABLE_REALTIME_EXITS=True,
        REALTIME_EXIT_POLL_SECONDS=30.0,
    )
    monkeypatch.setattr(eng, "is_market_open", lambda: False)
    monkeypatch.setattr(asyncio, "sleep", lambda s: _noop())
    await eng._sleep_between_cycles(90.0)
    assert eng.exit_manager.calls == 0


async def _noop() -> None:
    return None
