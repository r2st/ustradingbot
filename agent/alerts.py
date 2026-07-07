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
from pathlib import Path
from typing import Dict, Optional, Sequence

import structlog

from agent import alert_config
from agent.notifier import TelegramNotifier
from config.settings import Settings
from signals.signal_types import ExitEvent, ExitReason, TradeOrder

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
        self._data_dir = Path(settings.DATA_DIR)
        # Alert rules (F6): mtime-cached so a dashboard save takes effect on
        # the next dispatch without an engine restart.
        self._rules_mtime: Optional[int] = None
        self._rules: Dict[str, Dict] = dict(alert_config.DEFAULT_RULES)
        # Threshold de-dup state.
        self._drawdown_alerted = False
        self._daily_loss_alerted_date: Optional[str] = None

    @property
    def any_channel_enabled(self) -> bool:
        return self.telegram.enabled or self.email.enabled

    # ------------------------------------------------------------- rule lookup

    def _rule(self, event_type: str) -> Dict:
        """Return the effective rule for *event_type* (mtime-cached reload)."""
        try:
            path = self._data_dir / alert_config.RULES_FILE
            mtime = path.stat().st_mtime_ns if path.exists() else None
            if mtime != self._rules_mtime:
                self._rules = alert_config.load_rules(self._data_dir)
                self._rules_mtime = mtime
        except Exception:  # noqa: BLE001 -- fall back to the cached rules
            pass
        return self._rules.get(event_type) or {"enabled": True,
                                               "channels": list(alert_config.CHANNELS),
                                               "threshold": None}

    def _threshold(self, event_type: str, default: float) -> float:
        """Return the rule threshold for *event_type*, or *default*."""
        t = self._rule(event_type).get("threshold")
        try:
            return float(t) if t is not None else float(default)
        except (TypeError, ValueError):
            return float(default)

    def _push(self, title: str, body: str) -> bool:
        """Enqueue a PWA push notification (best-effort)."""
        try:
            from dashboard.push import publish

            publish(title, body, self._data_dir)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _dispatch(
        self,
        event_type: str,
        text: str,
        subject: str,
        default_channels: Sequence[str] = alert_config.CHANNELS,
        telegram_coro=None,
    ) -> bool:
        """Route one alert through the rule set and record it in history.

        *default_channels* preserves legacy per-event channel behaviour when
        the user has not narrowed the rule (e.g. cycle summaries were always
        Telegram-only).  *telegram_coro* lets callers keep the richer
        Telegram-specific formatting (``notify_entry`` etc.).

        Returns whether the event was dispatched to at least one channel.
        """
        rule = self._rule(event_type)
        if not rule.get("enabled", True):
            if telegram_coro is not None:
                telegram_coro.close()  # never leave a coroutine un-awaited
            return False
        channels = [c for c in rule.get("channels", alert_config.CHANNELS)
                    if c in default_channels]
        results: Dict[str, bool] = {}
        if "telegram" in channels and self.telegram.enabled:
            if telegram_coro is not None:
                await telegram_coro
                telegram_coro = None
            else:
                await self.telegram.send(text)
            results["telegram"] = True
        if telegram_coro is not None:
            telegram_coro.close()  # telegram channel skipped for this event
        if "email" in channels and self.email.enabled:
            await self.email.send(subject, text)
            results["email"] = True
        if "push" in channels:
            if self._push(subject, text):
                results["push"] = True
        alert_config.append_history(self._data_dir, event_type, text, results)
        return bool(results)

    # ---------------------------------------------------------------- raw send

    async def send(self, text: str, subject: str = "Trading Bot Alert",
                   event_type: str = "generic") -> None:
        """Send *text* to every enabled channel (legacy raw-send surface)."""
        if event_type in alert_config.EVENT_TYPES:
            await self._dispatch(event_type, text, subject)
            return
        # Unclassified sends keep the original behaviour: all channels.
        await self.telegram.send(text)
        await self.email.send(subject, text)
        alert_config.append_history(
            self._data_dir, event_type, text,
            {"telegram": self.telegram.enabled, "email": self.email.enabled},
        )

    # ----------------------------------------------------------- entry / exit

    async def notify_entry(self, order: TradeOrder, fill_price: float) -> None:
        if not self._settings.ALERT_ON_ENTRY:
            return
        sig = order.signal
        body = (
            f"Bought {order.quantity} {sig.symbol} @ {fill_price:.2f} "
            f"{order.currency}\nStop {sig.stop_price:.2f} · "
            f"Target {sig.target_price:.2f}\nGrade {sig.grade.value} · "
            f"AI {order.ai_decision}"
        )
        await self._dispatch(
            "entry", body, f"ENTRY {sig.symbol} ({sig.strategy})",
            default_channels=("telegram", "email"),
            telegram_coro=self.telegram.notify_entry(order, fill_price),
        )

    @staticmethod
    def _exit_event_type(event: ExitEvent) -> str:
        """Map an exit reason to its specific alert event type."""
        mapping = {
            ExitReason.STOP_HIT: "stop_hit",
            ExitReason.TARGET_HIT: "target_hit",
            ExitReason.PARTIAL_TAKE: "partial_take",
        }
        return mapping.get(event.exit_reason, "exit")

    async def notify_exit(self, event: ExitEvent) -> None:
        if not self._settings.ALERT_ON_EXIT:
            return
        body = (
            f"Closed {event.symbol} @ {event.exit_price:.2f}\n"
            f"P&L {event.pnl_gross:+.2f}"
        )
        await self._dispatch(
            self._exit_event_type(event), body,
            f"EXIT {event.symbol} — {event.exit_reason.value}",
            default_channels=("telegram", "email"),
            telegram_coro=self.telegram.notify_exit(event),
        )

    async def notify_cycle(
        self,
        signals_found: int,
        trades_placed: int,
        exits: int,
        open_positions: int,
    ) -> None:
        # Cycle summaries go to Telegram only (email would be too noisy).
        body = (
            f"Cycle: {signals_found} signals, {trades_placed} placed, "
            f"{exits} exits, {open_positions} open"
        )
        await self._dispatch(
            "cycle_summary", body, "Cycle summary",
            default_channels=("telegram",),
            telegram_coro=self.telegram.notify_cycle(
                signals_found, trades_placed, exits, open_positions
            ),
        )

    # -------------------------------------------------- proximity (F3 / F6)

    async def notify_proximity(
        self, symbol: str, kind: str, price: float, level: float, day: str
    ) -> bool:
        """Alert that *symbol* is approaching its stop or target.

        *kind* is ``"approaching_stop"`` or ``"approaching_target"``.
        Debounced to once per symbol per *day* (persisted, so an engine
        restart never re-fires it).  Returns whether an alert was sent.
        """
        if kind not in ("approaching_stop", "approaching_target"):
            return False
        state = alert_config.load_state(self._data_dir)
        fired = state.setdefault(kind, {})
        if fired.get(symbol) == day:
            return False
        emoji = "🔻" if kind == "approaching_stop" else "🎯"
        which = "stop" if kind == "approaching_stop" else "target"
        sent = await self._dispatch(
            kind,
            f"{emoji} {symbol} @ {price:.2f} is approaching its {which} "
            f"({level:.2f}).",
            f"{symbol} approaching {which}",
        )
        if sent:
            fired[symbol] = day
            alert_config.save_state(self._data_dir, state)
        return sent

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
        await self.send(text, subject=f"Mode switch: {old_mode} -> {new_mode}",
                        event_type="mode_switch")

    # ------------------------------------------------------- threshold checks

    async def check_drawdown(self, drawdown_pct: float) -> bool:
        """Fire a drawdown alert when *drawdown_pct* first breaches the limit.

        *drawdown_pct* is a positive fraction (0.05 == 5%).  Re-arms once
        drawdown recovers back below the threshold.  Returns whether an alert
        was sent on this call.
        """
        limit = self._threshold("drawdown", self._settings.ALERT_DRAWDOWN_PCT)
        if drawdown_pct >= limit:
            if not self._drawdown_alerted:
                self._drawdown_alerted = True
                await self._dispatch(
                    "drawdown",
                    f"⚠️ *DRAWDOWN ALERT* — drawdown {drawdown_pct * 100:.1f}% "
                    f"exceeded limit {limit * 100:.1f}%.",
                    "Drawdown limit breached",
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
        limit_pct = self._threshold("daily_loss", self._settings.ALERT_DAILY_LOSS_PCT)
        limit_dollars = total_capital * limit_pct
        if daily_pnl <= -limit_dollars and limit_dollars > 0:
            if self._daily_loss_alerted_date != day:
                self._daily_loss_alerted_date = day
                await self._dispatch(
                    "daily_loss",
                    f"⚠️ *DAILY LOSS ALERT* — today's P&L {daily_pnl:+.2f} "
                    f"breached the -{limit_dollars:.2f} limit.",
                    "Daily loss limit breached",
                )
                return True
        return False
