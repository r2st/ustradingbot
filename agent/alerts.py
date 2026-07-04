"""
Unified alert system: Telegram + email with configurable thresholds.

The engine talks to a single :class:`AlertManager`, which fans every alert out
to whichever channels are configured:

* **Telegram** — via the existing :class:`~agent.notifier.TelegramNotifier`.
* **Email** — via :class:`EmailNotifier` (best-effort SMTP).

On top of the raw entry/exit/mode-switch notifications it also owns two
threshold monitors that the engine feeds each cycle:

* **Drawdown** — fires once when peak-to-trough equity drawdown first exceeds
  ``ALERT_DRAWDOWN_PCT`` and re-arms only after equity recovers below it.
* **Daily loss** — fires once per trading day when the day's loss first exceeds
  ``ALERT_DAILY_LOSS_PCT`` of total capital.

Every network call is best-effort and never raises into the trading loop, so a
mail-server outage can never stop the bot from trading.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from typing import Optional

import structlog

from agent.notifier import TelegramNotifier
from config.settings import Settings
from signals.signal_types import ExitEvent, TradeOrder

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class EmailNotifier:
    """Best-effort SMTP email notifier.

    A no-op unless ``EMAIL_ALERTS_ENABLED`` is set and the SMTP host plus
    from/to addresses are configured.  Sending runs on a worker thread so the
    async trading loop is never blocked by a slow mail server.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._enabled = bool(
            settings.EMAIL_ALERTS_ENABLED
            and settings.SMTP_HOST
            and settings.EMAIL_FROM
            and settings.EMAIL_TO
        )
        self._log = log.bind(component="EmailNotifier")
        if not self._enabled:
            self._log.info("email.disabled", reason="not configured")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send_sync(self, subject: str, body: str) -> bool:
        """Send an email synchronously.  Returns ``True`` on success."""
        if not self._enabled:
            return False
        s = self._settings
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = s.EMAIL_FROM
        msg["To"] = s.EMAIL_TO
        msg.set_content(body)
        try:
            with smtplib.SMTP(s.SMTP_HOST, s.SMTP_PORT, timeout=15) as server:
                if s.SMTP_USE_TLS:
                    server.starttls()
                if s.SMTP_USERNAME:
                    server.login(s.SMTP_USERNAME, s.SMTP_PASSWORD)
                server.send_message(msg)
            return True
        except Exception as exc:  # noqa: BLE001 -- never break the loop
            self._log.warning("email.send_failed", error=str(exc))
            return False

    async def send(self, subject: str, body: str) -> None:
        """Send an email without blocking the event loop."""
        if not self._enabled:
            return
        await asyncio.to_thread(self.send_sync, subject, body)


class AlertManager:
    """Route alerts to Telegram + email and own the threshold monitors.

    Exposes the same ``notify_entry`` / ``notify_cycle`` / ``send`` surface as
    :class:`TelegramNotifier` (so it is a drop-in for the engine) plus
    ``notify_exit``, ``notify_mode_switch``, ``check_drawdown`` and
    ``check_daily_loss``.
    """

    def __init__(
        self,
        settings: Settings,
        telegram: Optional[TelegramNotifier] = None,
        email: Optional[EmailNotifier] = None,
    ) -> None:
        self._settings = settings
        self.telegram = telegram or TelegramNotifier(settings)
        self.email = email or EmailNotifier(settings)
        self._log = log.bind(component="AlertManager")
        # Threshold de-dup state.
        self._drawdown_alerted = False
        self._daily_loss_alerted_date: Optional[str] = None

    @property
    def any_channel_enabled(self) -> bool:
        return self.telegram.enabled or self.email.enabled

    # ---------------------------------------------------------------- raw send

    async def send(self, text: str, subject: str = "Trading Bot Alert") -> None:
        """Send *text* to every enabled channel."""
        await self.telegram.send(text)
        await self.email.send(subject, text)

    # ----------------------------------------------------------- entry / exit

    async def notify_entry(self, order: TradeOrder, fill_price: float) -> None:
        if not self._settings.ALERT_ON_ENTRY:
            return
        await self.telegram.notify_entry(order, fill_price)
        sig = order.signal
        await self.email.send(
            f"ENTRY {sig.symbol} ({sig.strategy})",
            f"Bought {order.quantity} {sig.symbol} @ {fill_price:.2f} "
            f"{order.currency}\nStop {sig.stop_price:.2f} · "
            f"Target {sig.target_price:.2f}\nGrade {sig.grade.value} · "
            f"AI {order.ai_decision}",
        )

    async def notify_exit(self, event: ExitEvent) -> None:
        if not self._settings.ALERT_ON_EXIT:
            return
        await self.telegram.notify_exit(event)
        await self.email.send(
            f"EXIT {event.symbol} — {event.exit_reason.value}",
            f"Closed {event.symbol} @ {event.exit_price:.2f}\n"
            f"P&L {event.pnl_gross:+.2f}",
        )

    async def notify_cycle(
        self,
        signals_found: int,
        trades_placed: int,
        exits: int,
        open_positions: int,
    ) -> None:
        # Cycle summaries go to Telegram only (email would be too noisy).
        await self.telegram.notify_cycle(
            signals_found, trades_placed, exits, open_positions
        )

    # --------------------------------------------------------- mode switch

    async def notify_mode_switch(self, old_mode: str, new_mode: str, actor: str) -> None:
        """Announce a paper⇄live mode switch on every channel."""
        if not self._settings.ALERT_ON_MODE_SWITCH:
            return
        emoji = "🔴" if new_mode.upper() == "LIVE" else "🟢"
        text = (
            f"{emoji} *MODE SWITCH* {old_mode} → *{new_mode}* by {actor}.\n"
            + (
                "REAL money is now at risk."
                if new_mode.upper() == "LIVE"
                else "Back to simulated paper trading."
            )
        )
        await self.send(text, subject=f"Mode switch: {old_mode} -> {new_mode}")

    # ------------------------------------------------------- threshold checks

    async def check_drawdown(self, drawdown_pct: float) -> bool:
        """Fire a drawdown alert when *drawdown_pct* first breaches the limit.

        *drawdown_pct* is a positive fraction (0.05 == 5%).  Re-arms once
        drawdown recovers back below the threshold.  Returns whether an alert
        was sent on this call.
        """
        limit = self._settings.ALERT_DRAWDOWN_PCT
        if drawdown_pct >= limit:
            if not self._drawdown_alerted:
                self._drawdown_alerted = True
                await self.send(
                    f"⚠️ *DRAWDOWN ALERT* — drawdown {drawdown_pct * 100:.1f}% "
                    f"exceeded limit {limit * 100:.1f}%.",
                    subject="Drawdown limit breached",
                )
                return True
        else:
            self._drawdown_alerted = False
        return False

    async def check_daily_loss(
        self, daily_pnl: float, total_capital: float, day: Optional[str] = None
    ) -> bool:
        """Fire a daily-loss alert when the day's loss first breaches the limit.

        De-duplicated per *day* (an ISO date string); a new day re-arms it.
        Returns whether an alert was sent on this call.
        """
        limit_dollars = total_capital * self._settings.ALERT_DAILY_LOSS_PCT
        if daily_pnl <= -limit_dollars and limit_dollars > 0:
            if self._daily_loss_alerted_date != day:
                self._daily_loss_alerted_date = day
                await self.send(
                    f"⚠️ *DAILY LOSS ALERT* — today's P&L {daily_pnl:+.2f} "
                    f"breached the -{limit_dollars:.2f} limit.",
                    subject="Daily loss limit breached",
                )
                return True
        return False
