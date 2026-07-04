"""
Export trade history, analytics, and backtests to CSV / PDF (feature 18).

Serves download endpoints under ``/api/export`` with ``Content-Disposition:
attachment`` so the browser saves them.  CSV is produced with the stdlib ``csv``
module; PDF via the dependency-free :mod:`dashboard.pdf_report`.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from config.settings import get_settings
from dashboard.auth import require_auth
from dashboard.pdf_report import simple_pdf

router = APIRouter(prefix="/api/export", tags=["export"])


def _csv_response(rows: List[Dict[str, Any]], filename: str, columns: List[str] | None = None) -> Response:
    buf = io.StringIO()
    if rows:
        columns = columns or list(rows[0].keys())
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    elif columns:
        csv.DictWriter(buf, fieldnames=columns).writeheader()
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _pdf_response(title: str, lines: List[str], filename: str) -> Response:
    return Response(
        content=simple_pdf(title, lines),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Trade history
# ---------------------------------------------------------------------------


@router.get("/trades.csv")
async def export_trades_csv(_user: str = Depends(require_auth)):
    """Download the full trade journal CSV verbatim."""
    path = Path(get_settings().DATA_DIR) / "trades.csv"
    if not path.exists():
        return _csv_response([], "trades.csv")
    return Response(
        content=path.read_text(encoding="utf-8"),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="trades.csv"'},
    )


@router.get("/trades.pdf")
async def export_trades_pdf(_user: str = Depends(require_auth)):
    """Download recent completed trades as a formatted PDF."""
    from dashboard.app import _analytics_report

    trades = _analytics_report().recent_trades
    lines = [
        f"Generated {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"{'Symbol':<10}{'Strategy':<16}{'Exit':<12}{'P&L net':>12}{'R':>8}",
        "-" * 58,
    ]
    for t in trades:
        lines.append(
            f"{str(t.get('symbol', '')):<10}"
            f"{str(t.get('strategy', '')):<16}"
            f"{str(t.get('exit_reason', '') or ''):<12}"
            f"{_fmt(t.get('pnl_net')):>12}"
            f"{_fmt(t.get('r_multiple')):>8}"
        )
    if not trades:
        lines.append("No completed trades yet.")
    return _pdf_response("Trade History", lines, "trades.pdf")


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@router.get("/analytics.csv")
async def export_analytics_csv(_user: str = Depends(require_auth)):
    from dashboard.app import _analytics_report

    report = _analytics_report()
    rows = report.by_strategy or []
    return _csv_response(rows, "analytics_by_strategy.csv")


@router.get("/analytics.pdf")
async def export_analytics_pdf(_user: str = Depends(require_auth)):
    from dashboard.app import _analytics_report

    report = _analytics_report()
    s = report.summary
    lines = [
        f"Generated {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "Portfolio summary",
        "-" * 44,
        f"Total trades:   {s.get('total_trades', 0)}",
        f"Win rate:       {s.get('win_rate', 0.0) * 100:.1f}%",
        f"Profit factor:  {s.get('profit_factor')}",
        f"Expectancy:     {_fmt(s.get('expectancy'))}",
        f"Total P&L:      {_fmt(s.get('total_pnl'))}",
        "",
        "By strategy",
        "-" * 44,
    ]
    for row in report.by_strategy or []:
        lines.append(
            f"  {str(row.get('strategy', '?')):<16} "
            f"trades {row.get('total_trades', 0):>3}  "
            f"win {row.get('win_rate', 0.0) * 100:>5.1f}%  "
            f"P&L {_fmt(row.get('total_pnl'))}"
        )
    return _pdf_response("Analytics Report", lines, "analytics.pdf")


# ---------------------------------------------------------------------------
# Backtest results
# ---------------------------------------------------------------------------


@router.get("/backtest/{job_id}.csv")
async def export_backtest_csv(job_id: str, _user: str = Depends(require_auth)):
    from dashboard.backtest_control import get_job

    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired job id.")
    results = (job.get("results") or {}) if isinstance(job, dict) else {}
    rows = results.get("by_strategy") or []
    return _csv_response(rows, f"backtest_{job_id}.csv")


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)
