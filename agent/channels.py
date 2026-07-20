"""
Additional alert channels: Slack, Discord, and SMS (Twilio) — P1-9.

Each notifier mirrors :class:`agent.notifier.TelegramNotifier`: it is a no-op
unless configured, exposes an ``enabled`` flag and an async ``send`` that runs
network I/O off the event loop, and never raises into the trading loop.

* **Slack** / **Discord** — incoming-webhook POST (just a URL to configure).
* **SMS (Twilio)** — reserved for *critical* alerts (daily-loss limit, broker
  disconnect); the AlertManager gates which event types may reach it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class SlackNotifier:
    """Best-effort Slack incoming-webhook notifier."""

    def __init__(self, settings: Any) -> None:
        self._url = str(getattr(settings, "SLACK_WEBHOOK_URL", "") or "")
        self._enabled = bool(self._url)
        self._log = log.bind(component="SlackNotifier")
        if not self._enabled:
            self._log.info("slack.disabled", reason="no webhook url")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send_sync(self, text: str) -> bool:
        if not self._enabled:
            return False
        try:
            resp = httpx.post(self._url, json={"text": text}, timeout=15.0)
            if resp.status_code >= 300:
                self._log.warning("slack.send_http_error", status=resp.status_code)
                return False
            return True
        except Exception as exc:  # noqa: BLE001 -- never break the loop
            self._log.warning("slack.send_failed", error=str(exc))
            return False

    async def send(self, text: str) -> bool:
        if not self._enabled:
            return False
        return await asyncio.to_thread(self.send_sync, text)


class DiscordNotifier:
    """Best-effort Discord incoming-webhook notifier."""

    def __init__(self, settings: Any) -> None:
        self._url = str(getattr(settings, "DISCORD_WEBHOOK_URL", "") or "")
        self._enabled = bool(self._url)
        self._log = log.bind(component="DiscordNotifier")
        if not self._enabled:
            self._log.info("discord.disabled", reason="no webhook url")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send_sync(self, text: str) -> bool:
        if not self._enabled:
            return False
        try:
            # Discord caps content at 2000 chars.
            resp = httpx.post(
                self._url, json={"content": text[:2000]}, timeout=15.0
            )
            if resp.status_code >= 300:
                self._log.warning("discord.send_http_error", status=resp.status_code)
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            self._log.warning("discord.send_failed", error=str(exc))
            return False

    async def send(self, text: str) -> bool:
        if not self._enabled:
            return False
        return await asyncio.to_thread(self.send_sync, text)


class SMSNotifier:
    """Best-effort Twilio SMS notifier (reserved for critical alerts)."""

    def __init__(self, settings: Any) -> None:
        self._sid = str(getattr(settings, "TWILIO_ACCOUNT_SID", "") or "")
        self._token = str(getattr(settings, "TWILIO_AUTH_TOKEN", "") or "")
        self._from = str(getattr(settings, "TWILIO_FROM_NUMBER", "") or "")
        self._to = str(getattr(settings, "TWILIO_TO_NUMBER", "") or "")
        self._enabled = bool(self._sid and self._token and self._from and self._to)
        self._log = log.bind(component="SMSNotifier")
        if not self._enabled:
            self._log.info("sms.disabled", reason="twilio not configured")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send_sync(self, text: str) -> bool:
        if not self._enabled:
            return False
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self._sid}/Messages.json"
        try:
            resp = httpx.post(
                url,
                data={"From": self._from, "To": self._to, "Body": text[:1500]},
                auth=(self._sid, self._token),
                timeout=15.0,
            )
            if resp.status_code >= 300:
                self._log.warning(
                    "sms.send_http_error", status=resp.status_code,
                    body=resp.text[:200],
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            self._log.warning("sms.send_failed", error=str(exc))
            return False

    async def send(self, text: str) -> bool:
        if not self._enabled:
            return False
        return await asyncio.to_thread(self.send_sync, text)
