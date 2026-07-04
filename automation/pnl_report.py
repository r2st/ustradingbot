"""
Automated daily / weekly P&L email reports (feature 10).

Builds a plain-text summary of trading activity from the journal — headline
stats plus a per-strategy breakdown and the window's realised P&L — and emails
it through the existing :class:`~agent.alerts.EmailNotifier`.

:func:`build_report` is pure (reads the journal, no network) so it tests
without SMTP; :func:`send_report` wires it to email.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class ReportContent:
    """A rendered report ready to email."""

    subject: str
    body: str


def _fmt_money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.2f}"


def build_report(settings, period: str = "daily", now: Optional[datetime] = None) -> ReportContent:
    """Build the P&L report for *period* (``"daily"`` or ``"weekly"``).

    Reads ``DATA_DIR/trades.csv`` and composes a text summary.  Never raises —
    returns a minimal report if the journal cannot be read.
    """
    now = now or datetime.now()
    period = period.lower()
    label = "Weekly" if period == "weekly" else "Daily"
    data_dir = Path(settings.DATA_DIR)

    try:
        from analytics.performance import analyze_journal
        from analytics.risk_dashboard import pnl_breakdown, drawdown_tracking
        from analytics.performance import load_completed_trades

        report = analyze_journal(data_dir / "trades.csv", settings.TOTAL_CAPITAL)
        trades = load_completed_trades(data_dir / "trades.csv")
        pnl = pnl_breakdown(trades, now=now)
        dd = drawdown_tracking(trades, settings.TOTAL_CAPITAL)
    except Exception as exc:  # noqa: BLE001
        log.warning("pnl_report.build_failed", error=str(exc))
        return ReportContent(
            subject=f"{label} P&L Report — {now:%Y-%m-%d}",
            body=f"Could not build report: {exc}",
        )

    summary = report.summary
    window_pnl = pnl.get("week" if period == "weekly" else "today", 0.0)

    lines = [
        f"{label} Trading Report — {now:%Y-%m-%d %H:%M}",
        "=" * 44,
        "",
        f"{label} realised P&L:   {_fmt_money(window_pnl)}",
        f"Account equity:      {_fmt_money(dd.get('current_equity'))}",
        f"Current drawdown:    {dd.get('current_drawdown_pct', 0.0) * 100:.1f}%",
        "",
        "All-time",
        "-" * 44,
        f"Total trades:        {summary.get('total_trades', 0)}",
        f"Win rate:            {summary.get('win_rate', 0.0) * 100:.1f}%",
        f"Profit factor:       {summary.get('profit_factor') if summary.get('profit_factor') is not None else 'n/a'}",
        f"Expectancy:          {_fmt_money(summary.get('expectancy'))}",
        f"Total P&L:           {_fmt_money(summary.get('total_pnl'))}",
    ]

    by_strategy = report.by_strategy
    if by_strategy:
        lines += ["", "By strategy", "-" * 44]
        for row in by_strategy:
            lines.append(
                f"  {str(row.get('strategy', '?')):<16} "
                f"trades {row.get('total_trades', 0):>3}  "
                f"win {row.get('win_rate', 0.0) * 100:>5.1f}%  "
                f"P&L {_fmt_money(row.get('total_pnl'))}"
            )

    lines += ["", f"Mode: {settings.TRADING_MODE} · Broker: {settings.BROKER}"]
    return ReportContent(
        subject=f"{label} P&L {_fmt_money(window_pnl)} — {now:%Y-%m-%d}",
        body="\n".join(lines),
    )


def send_report(settings, period: str = "daily", notifier=None, now: Optional[datetime] = None) -> bool:
    """Build and email the P&L report.  Returns ``True`` when an email was sent."""
    content = build_report(settings, period=period, now=now)
    if notifier is None:
        from agent.alerts import EmailNotifier

        notifier = EmailNotifier(settings)
    if not getattr(notifier, "enabled", False):
        log.info("pnl_report.email_disabled", period=period)
        return False
    sent = notifier.send_sync(content.subject, content.body)
    log.info("pnl_report.sent", period=period, ok=sent)
    return bool(sent)
