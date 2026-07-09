"""
Telegram reporting agent.

Sends concise trade and cycle notifications to a Telegram chat via the Bot
API.  It is entirely optional: if ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``
are not configured, every method becomes a silent no-op so the engine runs
unchanged.

All network calls are best-effort and never raise into the trading loop.
"""

from __future__ import annotations

import httpx
import structlog

from config.settings import Settings
from signals.signal_types import ExitEvent, TradeOrder

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class TelegramNotifier:
    """Best-effort Telegram notifier.

    Args:
        settings: Application settings (provides bot token + chat id).
    """

    def __init__(self, settings: Settings) -> None:
        self._token = settings.TELEGRAM_BOT_TOKEN
        self._chat_id = settings.TELEGRAM_CHAT_ID
        self._enabled = bool(self._token and self._chat_id)
        self._log = log.bind(component="TelegramNotifier")
        if not self._enabled:
            self._log.info("telegram.disabled", reason="token/chat_id not set")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(self, text: str) -> bool:
        """Send a raw message (Markdown).  No-op when disabled.

        Returns ``True`` if the message was delivered successfully,
        ``False`` on any failure (including disabled state).
        """
        if not self._enabled:
            return False
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    url,
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True,
                    },
                )
            if resp.status_code != 200:
                self._log.warning(
                    "telegram.send_http_error",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
                return False
            data = resp.json()
            if not data.get("ok"):
                self._log.warning(
                    "telegram.send_api_error",
                    description=data.get("description", "unknown"),
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001 -- never break the loop
            self._log.warning("telegram.send_failed", error=str(exc))
            return False

    async def notify_entry(self, order: TradeOrder, fill_price: float) -> bool:
        """Announce a new position.  Returns ``True`` on success."""
        sig = order.signal
        return await self.send(
            f"🟢 *ENTRY* `{sig.symbol}` ({sig.strategy})\n"
            f"Grade {sig.grade.value} · score {sig.signal_strength:.2f}\n"
            f"Qty {order.quantity} @ {fill_price:.2f} {order.currency}\n"
            f"Stop {sig.stop_price:.2f} · Target {sig.target_price:.2f}\n"
            f"AI: {order.ai_decision}"
        )

    async def notify_exit(self, event: ExitEvent) -> bool:
        """Announce a closed position.  Returns ``True`` on success."""
        emoji = "✅" if event.pnl_gross >= 0 else "🔴"
        return await self.send(
            f"{emoji} *EXIT* `{event.symbol}` — {event.exit_reason.value}\n"
            f"@ {event.exit_price:.2f} · P&L {event.pnl_gross:+.2f}"
        )

    async def notify_cycle(
        self,
        signals_found: int,
        trades_placed: int,
        exits: int,
        open_positions: int,
    ) -> None:
        """Post a one-line cycle summary."""
        await self.send(
            f"📊 Cycle done · {signals_found} signals · "
            f"{trades_placed} entries · {exits} exits · "
            f"{open_positions} open"
        )
