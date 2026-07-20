"""
Tax / realized-gains reporting API (feature P1f).

Serves the FIFO cost-basis report, short-term / long-term gains split, and
wash-sale flags under ``/api/tax``.  All computation runs in a threadpool since
it parses the trade journal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from config.settings import get_settings
from dashboard.auth import require_auth

router = APIRouter(prefix="/api/tax", tags=["Analytics"])


@router.get("/report")
async def tax_report(year: Optional[int] = None, _user: str = Depends(require_auth)):
    """Realized-gains report; pass ``?year=YYYY`` to scope to one tax year."""
    from analytics.tax import build_tax_report

    settings = get_settings()
    report = await run_in_threadpool(build_tax_report, settings.DATA_DIR, year)
    return report.to_dict()


@router.get("/years")
async def tax_years(_user: str = Depends(require_auth)):
    """The distinct tax years present in the journal (newest first for the UI)."""
    from analytics.tax import build_tax_report

    settings = get_settings()
    report = await run_in_threadpool(build_tax_report, settings.DATA_DIR, None)
    return {"years": sorted(report.available_years, reverse=True)}


# ---------------------------------------------------------------------------
# Tax-loss harvesting + IRS 8949 / Schedule D export (P1-7)
# ---------------------------------------------------------------------------


def _recent_buys_by_symbol(data_dir) -> dict:
    """Map each symbol to its buy-leg dates from the journal (wash-sale input)."""
    from analytics.performance import load_completed_trades
    from analytics.tax import events_from_trades

    trades = load_completed_trades(Path(data_dir) / "trades.csv")
    by_symbol: dict = {}
    for symbol, events in events_from_trades(trades).items():
        by_symbol[symbol] = [e.when for e in events if e.side == "buy"]
    return by_symbol


@router.get("/harvest")
async def tax_harvest(min_loss: float = 0.0, _user: str = Depends(require_auth)):
    """Scan open positions for tax-loss-harvesting opportunities (wash-sale aware)."""
    from analytics.tax_harvest import scan_harvest_opportunities
    from dashboard.app import _load_open_positions

    settings = get_settings()

    def _build():
        positions = _load_open_positions(Path(settings.DATA_DIR))
        prices: dict = {}
        try:
            from dashboard import quotes as _quotes

            syms = [str(p.get("symbol", "")) for p in positions if p.get("symbol")]
            for sym, q in _quotes.get_quotes(syms).items():
                if q.get("price") is not None:
                    prices[sym] = float(q["price"])
        except Exception:  # noqa: BLE001
            prices = {}
        recent = _recent_buys_by_symbol(settings.DATA_DIR)
        opps = scan_harvest_opportunities(
            positions, prices, recent, min_loss=min_loss
        )
        return {
            "opportunities": [o.to_dict() for o in opps],
            "count": len(opps),
            "total_harvestable_loss": round(
                sum(o.unrealized_loss for o in opps if not o.wash_sale_risk), 2
            ),
        }

    return await run_in_threadpool(_build)


@router.get("/schedule-d")
async def tax_schedule_d(year: Optional[int] = None, _user: str = Depends(require_auth)):
    """Schedule D short/long-term capital-gains summary."""
    from analytics.tax import realized_lots
    from analytics.tax_harvest import schedule_d_summary

    settings = get_settings()

    def _build():
        from analytics.performance import load_completed_trades

        trades = load_completed_trades(Path(settings.DATA_DIR) / "trades.csv")
        lots = realized_lots(trades)
        if year is not None:
            lots = [l for l in lots if l.tax_year == int(year)]
        return schedule_d_summary(lots)

    return await run_in_threadpool(_build)


@router.get("/8949")
async def tax_form_8949(
    year: Optional[int] = None,
    format: str = "json",
    _user: str = Depends(require_auth),
):
    """IRS Form 8949 rows; ``?format=csv`` returns a downloadable CSV file."""
    from analytics.tax import realized_lots
    from analytics.tax_harvest import form_8949_csv, form_8949_rows

    settings = get_settings()

    def _lots():
        from analytics.performance import load_completed_trades

        trades = load_completed_trades(Path(settings.DATA_DIR) / "trades.csv")
        lots = realized_lots(trades)
        if year is not None:
            lots = [l for l in lots if l.tax_year == int(year)]
        return lots

    lots = await run_in_threadpool(_lots)
    if format.lower() == "csv":
        csv_text = form_8949_csv(lots)
        fname = f"form_8949_{year or 'all'}.csv"
        return PlainTextResponse(
            csv_text,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    return form_8949_rows(lots)
