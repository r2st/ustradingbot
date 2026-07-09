"""Tests for alert rules, history, and rule-gated dispatch (monitoring F6)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from agent import alert_config
from agent.alerts import AlertManager
from config.settings import Settings
from signals.signal_types import ExitEvent, ExitReason


# ---------------------------------------------------------------------------
# alert_config unit tests
# ---------------------------------------------------------------------------


def test_default_rules_when_file_absent(tmp_data_dir: Path):
    rules = alert_config.load_rules(tmp_data_dir)
    assert set(rules) == set(alert_config.EVENT_TYPES)
    for rule in rules.values():
        assert rule["enabled"] is True
        assert rule["channels"] == list(alert_config.CHANNELS)


def test_save_and_reload_rules(tmp_data_dir: Path):
    alert_config.save_rules(tmp_data_dir, {
        "cycle_summary": {"enabled": False, "channels": ["telegram"]},
        "drawdown": {"enabled": True, "channels": ["email"], "threshold": 0.08},
    })
    rules = alert_config.load_rules(tmp_data_dir)
    assert rules["cycle_summary"]["enabled"] is False
    assert rules["drawdown"]["channels"] == ["email"]
    assert rules["drawdown"]["threshold"] == 0.08
    # Untouched events keep their defaults.
    assert rules["entry"]["enabled"] is True


def test_save_rejects_unknown_keys(tmp_data_dir: Path):
    with pytest.raises(ValueError):
        alert_config.save_rules(tmp_data_dir, {"not_an_event": {"enabled": True}})
    with pytest.raises(ValueError):
        alert_config.save_rules(tmp_data_dir, {"entry": {"channels": ["fax"]}})


def test_corrupt_rules_file_falls_back_to_defaults(tmp_data_dir: Path):
    (Path(tmp_data_dir) / alert_config.RULES_FILE).write_text("{oops")
    rules = alert_config.load_rules(tmp_data_dir)
    assert rules["entry"]["enabled"] is True


def test_history_roundtrip_and_corruption(tmp_data_dir: Path):
    alert_config.append_history(tmp_data_dir, "entry", "bought X", {"telegram": True})
    alert_config.append_history(tmp_data_dir, "stop_hit", "stopped Y", {"email": False})
    path = Path(tmp_data_dir) / alert_config.HISTORY_FILE
    with open(path, "a", encoding="utf-8") as f:
        f.write("garbage\n")
    records = alert_config.read_history(tmp_data_dir)
    assert len(records) == 2
    assert records[0]["type"] == "stop_hit"  # newest first
    assert alert_config.read_history(tmp_data_dir, type_filter="entry")[0]["message"] == "bought X"


# ---------------------------------------------------------------------------
# AlertManager rule gating
# ---------------------------------------------------------------------------


class _FakeTelegram:
    def __init__(self):
        self.enabled = True
        self.sent = []

    async def send(self, text):
        self.sent.append(("send", text))
        return True

    async def notify_entry(self, order, fill_price):
        self.sent.append(("entry", order.signal.symbol))
        return True

    async def notify_exit(self, event):
        self.sent.append(("exit", event.symbol))
        return True

    async def notify_cycle(self, *args):
        self.sent.append(("cycle", args))
        return True


class _FakeEmail:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.sent = []

    async def send(self, subject, body):
        self.sent.append((subject, body))

    def send_sync(self, subject, body):
        self.sent.append((subject, body))
        return True


@pytest.fixture
def manager(settings: Settings):
    tg = _FakeTelegram()
    em = _FakeEmail()
    return AlertManager(settings, telegram=tg, email=em), tg, em


def test_no_rules_file_preserves_behaviour(manager):
    mgr, tg, _ = manager
    asyncio.run(mgr.notify_cycle(3, 1, 0, 2))
    assert tg.sent and tg.sent[0][0] == "cycle"


def test_disabling_cycle_summary_stops_telegram(manager, settings):
    mgr, tg, _ = manager
    alert_config.save_rules(settings.DATA_DIR, {
        "cycle_summary": {"enabled": False, "channels": ["telegram"]},
    })
    asyncio.run(mgr.notify_cycle(3, 1, 0, 2))
    assert tg.sent == []  # rule consulted without a restart (mtime cache)
    # Entries still alert.
    from signals.signal_types import Grade, Signal, TradeOrder

    sig = Signal(symbol="NVDA", strategy="momentum", entry_price=100,
                 stop_price=95, target_price=110, grade=Grade.A)
    order = TradeOrder(signal=sig, quantity=1, currency="USD")
    asyncio.run(mgr.notify_entry(order, 100.0))
    assert ("entry", "NVDA") in tg.sent


def test_stop_hit_maps_to_specific_event_type(manager, settings):
    mgr, tg, _ = manager
    event = ExitEvent(symbol="AAPL", exit_price=90.0,
                      exit_reason=ExitReason.STOP_HIT, pnl_gross=-50.0)
    asyncio.run(mgr.notify_exit(event))
    history = alert_config.read_history(settings.DATA_DIR)
    assert history[0]["type"] == "stop_hit"
    assert ("exit", "AAPL") in tg.sent


def test_dispatch_appends_history_with_channel_status(manager, settings):
    mgr, _, _ = manager
    asyncio.run(mgr.send("hello", event_type="engine_error"))
    rec = alert_config.read_history(settings.DATA_DIR)[0]
    assert rec["type"] == "engine_error"
    assert rec["channels"].get("telegram") is True


def test_proximity_debounce_once_per_day(manager, settings):
    mgr, tg, _ = manager
    sent1 = asyncio.run(mgr.notify_proximity("NVDA", "approaching_stop",
                                             100.0, 99.5, "2026-07-07"))
    sent2 = asyncio.run(mgr.notify_proximity("NVDA", "approaching_stop",
                                             100.1, 99.5, "2026-07-07"))
    assert sent1 is True and sent2 is False
    # Survives a "restart": a fresh manager reads the persisted state.
    mgr2 = AlertManager(settings, telegram=_FakeTelegram(), email=_FakeEmail())
    assert asyncio.run(mgr2.notify_proximity("NVDA", "approaching_stop",
                                             100.2, 99.5, "2026-07-07")) is False
    # A new day re-arms.
    assert asyncio.run(mgr2.notify_proximity("NVDA", "approaching_stop",
                                             100.2, 99.5, "2026-07-08")) is True


def test_threshold_override_from_rules(manager, settings):
    mgr, tg, _ = manager
    alert_config.save_rules(settings.DATA_DIR, {
        "daily_loss": {"enabled": True, "channels": ["telegram"], "threshold": 0.10},
    })
    # 5% loss < 10% rule threshold → no alert.
    assert asyncio.run(mgr.check_daily_loss(-600.0, 12_000.0, day="d1")) is False
    # 12% loss breaches it.
    assert asyncio.run(mgr.check_daily_loss(-1500.0, 12_000.0, day="d1")) is True


# ---------------------------------------------------------------------------
# Alerts router
# ---------------------------------------------------------------------------


@pytest.fixture
def client_env(monkeypatch, tmp_path):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir()
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=data_dir)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    return TestClient(dash.app, raise_server_exceptions=False), data_dir


def test_rules_endpoint_roundtrip(client_env):
    client, _ = client_env
    d = client.get("/api/alerts/rules").json()
    assert "cycle_summary" in d["rules"]
    resp = client.put("/api/alerts/rules", json={"rules": {
        "entry": {"enabled": False, "channels": ["email"]},
    }})
    assert resp.status_code == 200
    assert client.get("/api/alerts/rules").json()["rules"]["entry"]["enabled"] is False


def test_rules_endpoint_rejects_bad_payload(client_env):
    client, _ = client_env
    resp = client.put("/api/alerts/rules", json={"rules": {"bogus_event": {}}})
    assert resp.status_code == 400


def test_test_endpoint_reports_unconfigured_clearly(client_env):
    client, _ = client_env
    resp = client.post("/api/alerts/test", json={"channel": "telegram"})
    assert resp.status_code == 200  # never a 500
    d = resp.json()
    assert d["ok"] is False
    assert "not configured" in d["error"].lower()
    assert client.post("/api/alerts/test", json={"channel": "fax"}).status_code == 400


def test_channels_endpoint_no_secrets(client_env):
    client, _ = client_env
    d = client.get("/api/alerts/channels").json()
    assert d["channels"]["telegram"] is False
    assert d["channels"]["push"] is True
    assert "token" not in str(d).lower()
