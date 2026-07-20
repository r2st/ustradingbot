"""Tests for the Slack / Discord / SMS alert channels (P1-9)."""

from __future__ import annotations

import asyncio

import pytest

from agent import alert_config
from agent.alerts import AlertManager
from agent.channels import DiscordNotifier, SlackNotifier, SMSNotifier
from config.settings import Settings


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Notifier enablement
# ---------------------------------------------------------------------------


class TestEnablement:
    def test_slack_disabled_without_url(self) -> None:
        assert SlackNotifier(Settings()).enabled is False

    def test_slack_enabled_with_url(self) -> None:
        s = Settings(SLACK_WEBHOOK_URL="https://hooks.slack.com/services/x")
        assert SlackNotifier(s).enabled is True

    def test_discord_enabled_with_url(self) -> None:
        s = Settings(DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/x")
        assert DiscordNotifier(s).enabled is True

    def test_sms_needs_all_twilio_fields(self) -> None:
        assert SMSNotifier(Settings(TWILIO_ACCOUNT_SID="AC")).enabled is False
        s = Settings(
            TWILIO_ACCOUNT_SID="AC", TWILIO_AUTH_TOKEN="tok",
            TWILIO_FROM_NUMBER="+1", TWILIO_TO_NUMBER="+2",
        )
        assert SMSNotifier(s).enabled is True

    def test_disabled_send_is_noop(self) -> None:
        assert _run(SlackNotifier(Settings()).send("hi")) is False


# ---------------------------------------------------------------------------
# Fake channels wired into AlertManager
# ---------------------------------------------------------------------------


class _Fake:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


def _manager(tmp_path, **channels):
    s = Settings(DATA_DIR=tmp_path)
    # Silence real telegram/email.
    return AlertManager(s, **channels)


class TestDispatchRouting:
    def test_slack_and_discord_receive_alert(self, tmp_path) -> None:
        slack, discord = _Fake(), _Fake()
        mgr = _manager(tmp_path, slack=slack, discord=discord)
        # daily_loss rule includes all channels by default.
        _run(mgr._dispatch("daily_loss", "loss!", "Daily loss"))
        assert slack.sent == ["loss!"]
        assert discord.sent == ["loss!"]

    def test_sms_only_on_critical_events(self, tmp_path) -> None:
        sms = _Fake()
        mgr = _manager(tmp_path, sms=sms)
        # Non-critical event -> SMS skipped.
        _run(mgr._dispatch("entry", "bought", "Entry",
                           default_channels=alert_config.CHANNELS))
        assert sms.sent == []
        # Critical event -> SMS fires.
        _run(mgr._dispatch("daily_loss", "big loss", "Daily loss"))
        assert sms.sent == ["big loss"]

    def test_channel_selection_respected(self, tmp_path) -> None:
        # A rule that only lists slack should not reach discord.
        alert_config.save_rules(
            tmp_path,
            {"daily_loss": {"enabled": True, "channels": ["slack"], "threshold": None}},
        )
        slack, discord = _Fake(), _Fake()
        mgr = _manager(tmp_path, slack=slack, discord=discord)
        _run(mgr._dispatch("daily_loss", "loss!", "Daily loss"))
        assert slack.sent == ["loss!"]
        assert discord.sent == []

    def test_disabled_channel_skipped(self, tmp_path) -> None:
        slack = _Fake(enabled=False)
        mgr = _manager(tmp_path, slack=slack)
        _run(mgr._dispatch("daily_loss", "loss!", "Daily loss"))
        assert slack.sent == []


def test_sms_critical_events_defined() -> None:
    assert "daily_loss" in alert_config.SMS_CRITICAL_EVENTS
    assert "broker_disconnect" in alert_config.SMS_CRITICAL_EVENTS
