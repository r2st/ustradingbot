"""Tests for the unified alert system (agent/alerts.py)."""

from __future__ import annotations

from datetime import datetime

import pytest

from agent.alerts import AlertManager, EmailNotifier
from agent.notifier import TelegramNotifier
from config.settings import Settings
from signals.signal_types import ExitEvent, ExitReason, Grade, Signal, TradeOrder


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class SpyTelegram(TelegramNotifier):
    """Telegram notifier that records messages instead of sending them."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._enabled = True  # force enabled for the spy
        self.sent: list = []
        self.entries: list = []
        self.exits: list = []
        self.cycles: list = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def notify_entry(self, order, fill_price) -> None:
        self.entries.append((order, fill_price))

    async def notify_exit(self, event) -> None:
        self.exits.append(event)

    async def notify_cycle(self, signals_found, trades_placed, exits, open_positions):
        self.cycles.append((signals_found, trades_placed, exits, open_positions))


class SpyEmail(EmailNotifier):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._enabled = True
        self.sent: list = []

    async def send(self, subject: str, body: str) -> None:
        self.sent.append((subject, body))

    def send_sync(self, subject: str, body: str) -> bool:
        self.sent.append((subject, body))
        return True


def _mgr(**kw) -> AlertManager:
    settings = Settings(**kw)
    return AlertManager(settings, telegram=SpyTelegram(settings), email=SpyEmail(settings))


def _order() -> TradeOrder:
    sig = Signal(symbol="AAPL", strategy="momentum", entry_price=100.0,
                 stop_price=95.0, target_price=115.0, signal_strength=0.8,
                 grade=Grade.A)
    return TradeOrder(signal=sig, quantity=10, currency="USD",
                      ai_decision="APPROVE", ai_reasoning="ok")


# --------------------------------------------------------------------------- #
# EmailNotifier
# --------------------------------------------------------------------------- #


def test_email_disabled_by_default() -> None:
    assert EmailNotifier(Settings()).enabled is False


def test_email_enabled_when_configured() -> None:
    s = Settings(EMAIL_ALERTS_ENABLED=True, SMTP_HOST="smtp.test",
                 EMAIL_FROM="a@test", EMAIL_TO="b@test")
    assert EmailNotifier(s).enabled is True


def test_email_send_sync_uses_smtp(monkeypatch) -> None:
    s = Settings(EMAIL_ALERTS_ENABLED=True, SMTP_HOST="smtp.test",
                 SMTP_USERNAME="u", SMTP_PASSWORD="p",
                 EMAIL_FROM="a@test", EMAIL_TO="b@test")

    class FakeSMTP:
        def __init__(self, host, port, timeout=15):
            self.actions = []
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): calls.append("starttls")
        def login(self, u, p): calls.append(("login", u, p))
        def send_message(self, msg): calls.append(("send", msg["Subject"]))

    calls: list = []
    monkeypatch.setattr("agent.alerts.smtplib.SMTP", FakeSMTP)
    assert EmailNotifier(s).send_sync("Subj", "Body") is True
    assert "starttls" in calls
    assert ("login", "u", "p") in calls
    assert ("send", "Subj") in calls


def test_email_send_sync_swallows_errors(monkeypatch) -> None:
    s = Settings(EMAIL_ALERTS_ENABLED=True, SMTP_HOST="smtp.test",
                 EMAIL_FROM="a@test", EMAIL_TO="b@test")

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr("agent.alerts.smtplib.SMTP", boom)
    # Must not raise.
    assert EmailNotifier(s).send_sync("S", "B") is False


# --------------------------------------------------------------------------- #
# AlertManager routing
# --------------------------------------------------------------------------- #


async def test_send_fans_out_to_all_channels() -> None:
    mgr = _mgr()
    await mgr.send("hello")
    assert mgr.telegram.sent == ["hello"]
    assert len(mgr.email.sent) == 1


async def test_notify_entry_respects_flag() -> None:
    mgr = _mgr(ALERT_ON_ENTRY=True)
    await mgr.notify_entry(_order(), 100.0)
    assert len(mgr.telegram.entries) == 1
    assert len(mgr.email.sent) == 1

    off = _mgr(ALERT_ON_ENTRY=False)
    await off.notify_entry(_order(), 100.0)
    assert off.telegram.entries == []


async def test_notify_exit_respects_flag() -> None:
    ev = ExitEvent(symbol="AAPL", exit_price=115.0, exit_reason=ExitReason.TARGET_HIT,
                   pnl_gross=150.0)
    mgr = _mgr(ALERT_ON_EXIT=True)
    await mgr.notify_exit(ev)
    assert mgr.telegram.exits == [ev]

    off = _mgr(ALERT_ON_EXIT=False)
    await off.notify_exit(ev)
    assert off.telegram.exits == []


async def test_mode_switch_alert() -> None:
    mgr = _mgr(ALERT_ON_MODE_SWITCH=True)
    await mgr.notify_mode_switch("PAPER", "LIVE", "admin")
    assert any("LIVE" in m for m in mgr.telegram.sent)
    assert any("REAL money" in m for m in mgr.telegram.sent)

    off = _mgr(ALERT_ON_MODE_SWITCH=False)
    await off.notify_mode_switch("PAPER", "LIVE", "admin")
    assert off.telegram.sent == []


# --------------------------------------------------------------------------- #
# Threshold monitors
# --------------------------------------------------------------------------- #


async def test_drawdown_alert_fires_once_then_rearms() -> None:
    mgr = _mgr(ALERT_DRAWDOWN_PCT=0.05)
    assert await mgr.check_drawdown(0.06) is True   # breach -> fire
    assert await mgr.check_drawdown(0.07) is False  # still breached -> silent
    assert await mgr.check_drawdown(0.02) is False  # recovered -> re-arm
    assert await mgr.check_drawdown(0.06) is True   # breach again -> fire
    assert len([m for m in mgr.telegram.sent if "DRAWDOWN" in m]) == 2


async def test_daily_loss_alert_dedups_per_day() -> None:
    mgr = _mgr(ALERT_DAILY_LOSS_PCT=0.01)  # 1% of 12000 = -120
    assert await mgr.check_daily_loss(-150.0, 12000.0, day="2026-07-04") is True
    assert await mgr.check_daily_loss(-200.0, 12000.0, day="2026-07-04") is False
    # New day re-arms.
    assert await mgr.check_daily_loss(-150.0, 12000.0, day="2026-07-05") is True


async def test_daily_loss_no_alert_when_within_limit() -> None:
    mgr = _mgr(ALERT_DAILY_LOSS_PCT=0.01)
    assert await mgr.check_daily_loss(-50.0, 12000.0, day="2026-07-04") is False


async def test_any_channel_enabled() -> None:
    mgr = _mgr()
    assert mgr.any_channel_enabled is True
