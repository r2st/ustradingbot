"""Tests for broker reconnection with exponential backoff (P0 fix).

Exercises ``TradingEngine._ensure_broker_connected`` in isolation via a stub
broker/notifier, so no real broker, network, or full engine bootstrap is
needed.  Backoff delays are set to zero so the tests run instantly.
"""

from __future__ import annotations

from pathlib import Path

from config.settings import Settings
from engine import TradingEngine


class StubBroker:
    """Broker stub whose connect() results are scripted per call."""

    def __init__(self, connect_results, connected=False) -> None:
        self._results = list(connect_results)
        self._connected = connected
        self.connect_calls = 0
        self.disconnect_calls = 0

    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        self.connect_calls += 1
        result = self._results.pop(0) if self._results else False
        self._connected = bool(result)
        return result

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False


class StubNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


def _engine(tmp_data_dir: Path, broker: StubBroker, **overrides) -> TradingEngine:
    """Build a bare engine without running the heavy __init__."""
    params = {
        "RECONNECT_BASE_DELAY_SECONDS": 0.0,
        "RECONNECT_MAX_DELAY_SECONDS": 0.0,
        "RECONNECT_MAX_ATTEMPTS": 5,
    }
    params.update(overrides)
    eng = TradingEngine.__new__(TradingEngine)
    eng.settings = Settings(DATA_DIR=tmp_data_dir, **params)
    eng.broker = broker
    eng.notifier = StubNotifier()
    return eng


async def test_returns_immediately_when_connected(tmp_data_dir: Path) -> None:
    broker = StubBroker([], connected=True)
    eng = _engine(tmp_data_dir, broker)
    assert await eng._ensure_broker_connected() is True
    assert broker.connect_calls == 0  # no reconnect needed


async def test_reconnects_after_transient_failures(tmp_data_dir: Path) -> None:
    # Disconnected; connect fails twice, then succeeds on the third attempt.
    broker = StubBroker([False, False, True], connected=False)
    eng = _engine(tmp_data_dir, broker)

    assert await eng._ensure_broker_connected() is True
    assert broker.connect_calls == 3
    assert any("reconnected" in m.lower() for m in eng.notifier.messages)


async def test_gives_up_after_max_attempts(tmp_data_dir: Path) -> None:
    broker = StubBroker([False] * 5, connected=False)
    eng = _engine(tmp_data_dir, broker)

    assert await eng._ensure_broker_connected() is False
    assert broker.connect_calls == 5
    assert any("failed" in m.lower() for m in eng.notifier.messages)


async def test_connect_exception_is_treated_as_failure(tmp_data_dir: Path) -> None:
    class ExplodingBroker(StubBroker):
        def connect(self) -> bool:
            self.connect_calls += 1
            if self.connect_calls < 2:
                raise ConnectionError("boom")
            self._connected = True
            return True

    broker = ExplodingBroker([], connected=False)
    eng = _engine(tmp_data_dir, broker)
    assert await eng._ensure_broker_connected() is True
    assert broker.connect_calls == 2


async def test_backoff_delay_grows_exponentially(tmp_data_dir: Path, monkeypatch) -> None:
    """Verify the delay sequence is base * 2**(attempt-1), capped at max."""
    broker = StubBroker([False, False, False, True], connected=False)
    eng = _engine(
        tmp_data_dir,
        broker,
        RECONNECT_BASE_DELAY_SECONDS=1.0,
        RECONNECT_MAX_DELAY_SECONDS=100.0,
        RECONNECT_MAX_ATTEMPTS=5,
    )

    delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("engine.asyncio.sleep", _fake_sleep)
    assert await eng._ensure_broker_connected() is True
    # 3 failed attempts -> 3 sleeps: 1, 2, 4
    assert delays == [1.0, 2.0, 4.0]


async def test_backoff_delay_is_capped(tmp_data_dir: Path, monkeypatch) -> None:
    broker = StubBroker([False, False, False, True], connected=False)
    eng = _engine(
        tmp_data_dir,
        broker,
        RECONNECT_BASE_DELAY_SECONDS=10.0,
        RECONNECT_MAX_DELAY_SECONDS=15.0,
        RECONNECT_MAX_ATTEMPTS=5,
    )
    delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("engine.asyncio.sleep", _fake_sleep)
    assert await eng._ensure_broker_connected() is True
    # base 10 -> 10, 20(capped to 15), 40(capped to 15)
    assert delays == [10.0, 15.0, 15.0]
