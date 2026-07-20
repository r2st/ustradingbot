"""
Scheduled performance statements (P1-10).

Generates a monthly / quarterly account statement — headline metrics, sector /
strategy / factor attribution, and the period's realised P&L — as text and as a
minimal self-contained PDF (via :func:`dashboard.pdf_report.simple_pdf`), then
emails it (with the PDF attached) over the existing SMTP settings.

The scheduler (:func:`automation.scheduler.build_scheduler`) wires a monthly job
when ``STATEMENT_ENABLED`` is set; ``STATEMENT_PERIOD`` chooses monthly vs
quarterly delivery.  Everything is best-effort and never raises into the loop.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, List, Optional

import structlog

from config.settings import EASTERN

log = structlog.get_logger(__name__)


@dataclass
class Statement:
    """A rendered statement ready to email."""

    subject: str
    body: str
    pdf: bytes = b""
    period: str = "monthly"
    lines: List[str] = field(default_factory=list)


def _money(v: Optional[float]) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def build_statement(
    settings: Any, period: str = "monthly", now: Optional[datetime] = None
) -> Statement:
    """Build the statement for *period* (``monthly`` | ``quarterly``).

    Never raises — returns a minimal statement if the journal can't be read.
    """
    now = now or datetime.now(tz=EASTERN)
    period = (period or "monthly").lower()
    label = "Quarterly" if period == "quarterly" else "Monthly"
    data_dir = Path(settings.DATA_DIR)
    title = f"{label} Statement — {now:%Y-%m-%d}"

    try:
        from analytics.attribution import build_attribution_report
        from analytics.performance import analyze_journal
        from analytics.risk_dashboard import drawdown_tracking, pnl_breakdown
        from analytics.performance import load_completed_trades

        report = analyze_journal(data_dir / "trades.csv", settings.TOTAL_CAPITAL)
        trades = load_completed_trades(data_dir / "trades.csv")
        pnl = pnl_breakdown(trades, now=now)
        dd = drawdown_tracking(trades, settings.TOTAL_CAPITAL)
        attribution = build_attribution_report(
            str(data_dir), settings.TOTAL_CAPITAL
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("statement.build_failed", error=str(exc))
        return Statement(subject=title, body=f"Could not build statement: {exc}",
                         period=period)

    summary = report.summary
    window_key = "month" if period == "monthly" else "month"
    window_pnl = pnl.get(window_key, 0.0)

    lines: List[str] = [
        title,
        "=" * 52,
        "",
        f"Period P&L:          {_money(window_pnl)}",
        f"Account equity:      {_money(dd.get('current_equity'))}",
        f"Current drawdown:    {dd.get('current_drawdown_pct', 0.0) * 100:.1f}%",
        "",
        "All-time performance",
        "-" * 52,
        f"Total trades:        {summary.get('total_trades', 0)}",
        f"Win rate:            {summary.get('win_rate', 0.0) * 100:.1f}%",
        f"Profit factor:       {summary.get('profit_factor', 'n/a')}",
        f"Sharpe ratio:        {summary.get('sharpe_ratio', 'n/a')}",
        f"Total P&L:           {_money(summary.get('total_pnl'))}",
        "",
        "Sector attribution",
        "-" * 52,
    ]
    for row in attribution.get("by_sector", [])[:12]:
        lines.append(
            f"  {str(row.get('sector', '?')):<18} "
            f"P&L {_money(row.get('total_pnl')):>13}  "
            f"({row.get('contribution_pct', 0.0):+.1f}%)"
        )

    lines += ["", "Strategy attribution", "-" * 52]
    for row in attribution.get("by_strategy", [])[:12]:
        lines.append(
            f"  {str(row.get('strategy', '?')):<18} "
            f"P&L {_money(row.get('total_pnl')):>13}  "
            f"({row.get('contribution_pct', 0.0):+.1f}%)"
        )

    factor = attribution.get("factor", {})
    if factor.get("beta") is not None:
        lines += [
            "", "Factor attribution (vs SPY)", "-" * 52,
            f"  Portfolio beta:     {factor.get('beta')}",
            f"  Systematic P&L:     {_money(factor.get('systematic_pnl'))}",
            f"  Specific (alpha):   {_money(factor.get('specific_pnl'))}",
        ]

    lines += ["", f"Mode: {settings.TRADING_MODE} · Broker: {settings.BROKER}"]

    body = "\n".join(lines)
    pdf = b""
    try:
        from dashboard.pdf_report import simple_pdf

        pdf = simple_pdf(title, lines)
    except Exception as exc:  # noqa: BLE001
        log.warning("statement.pdf_failed", error=str(exc))

    return Statement(
        subject=f"{label} Statement {_money(window_pnl)} — {now:%Y-%m}",
        body=body, pdf=pdf, period=period, lines=lines,
    )


def _send_email_with_pdf(settings: Any, statement: Statement) -> bool:
    """Send the statement over SMTP with the PDF attached.  Returns success."""
    enabled = bool(
        getattr(settings, "EMAIL_ALERTS_ENABLED", False)
        and settings.SMTP_HOST and settings.EMAIL_FROM and settings.EMAIL_TO
    )
    if not enabled:
        log.info("statement.email_disabled")
        return False
    msg = EmailMessage()
    msg["Subject"] = statement.subject
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = settings.EMAIL_TO
    msg.set_content(statement.body)
    if statement.pdf:
        msg.add_attachment(
            statement.pdf, maintype="application", subtype="pdf",
            filename=f"statement_{statement.period}.pdf",
        )
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as srv:
            if settings.SMTP_USE_TLS:
                srv.starttls()
            if settings.SMTP_USERNAME:
                srv.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            srv.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("statement.email_failed", error=str(exc))
        return False


def send_statement(
    settings: Any, period: str = "monthly", now: Optional[datetime] = None
) -> bool:
    """Build and email the statement.  Returns whether an email was sent."""
    statement = build_statement(settings, period=period, now=now)
    sent = _send_email_with_pdf(settings, statement)
    log.info("statement.sent", period=period, ok=sent)
    return sent
