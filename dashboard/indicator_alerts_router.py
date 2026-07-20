"""
Custom indicator & portfolio alert API (P1-4).

CRUD for indicator/portfolio alert rules (RSI cross, MA crossover, volume spike,
drawdown, daily-loss) plus a manual ``/check`` trigger, under
``/api/indicator-alerts``.  Guarded by the shared HTTP Basic Auth dependency.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from alerts.indicator_alerts import (
    IndicatorAlertError,
    get_indicator_alert_store,
)
from config.settings import get_settings
from dashboard.auth import require_auth
from dashboard.http_util import parse_json_body

router = APIRouter(prefix="/api/indicator-alerts", tags=["Notifications"])


def _store():
    return get_indicator_alert_store(get_settings().DATA_DIR)


@router.get("")
async def list_alerts(_user: str = Depends(require_auth)):
    """Every indicator/portfolio alert rule (armed and triggered)."""
    return {"alerts": _store().list_alerts()}


@router.post("")
async def create_alert(request: Request, _user: str = Depends(require_auth)):
    """Create a rule: body ``{type, ...type-specific params}``.

    Types: ``rsi_cross`` (symbol, direction, threshold), ``ma_cross`` (symbol,
    kind=golden|death, fast, slow), ``volume_spike`` (symbol, pct_above_avg),
    ``drawdown`` (scope=portfolio|position, threshold_pct, symbol?),
    ``daily_loss`` (threshold_pct).
    """
    body = await parse_json_body(request)
    rule_type = str(body.get("type", ""))
    try:
        rule = _store().add_alert(rule_type, body)
    except IndicatorAlertError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return rule


@router.delete("/{alert_id}")
async def delete_alert(alert_id: str, _user: str = Depends(require_auth)):
    if not _store().delete_alert(alert_id):
        raise HTTPException(status_code=404, detail="Alert not found.")
    return {"ok": True, "deleted": alert_id}


@router.post("/{alert_id}/active")
async def set_active(
    alert_id: str, request: Request, _user: str = Depends(require_auth)
):
    """Enable/disable a rule: body ``{active: bool}`` (re-arming clears triggered)."""
    body = await parse_json_body(request)
    rule = _store().set_active(alert_id, bool(body.get("active", True)))
    if rule is None:
        raise HTTPException(status_code=404, detail="Alert not found.")
    return rule


@router.post("/check")
async def check_now(_user: str = Depends(require_auth)):
    """Evaluate every armed rule now and return the ones that fired."""
    from alerts.indicator_alerts import check_indicator_alerts

    settings = get_settings()
    ctx = await run_in_threadpool(_build_portfolio_ctx, settings)
    triggered = await run_in_threadpool(
        check_indicator_alerts, settings, None, ctx
    )
    return {"triggered": triggered, "count": len(triggered)}


def _build_portfolio_ctx(settings) -> dict:
    """Assemble portfolio drawdown / daily-loss context from the risk report."""
    ctx: dict = {}
    try:
        from analytics.risk_dashboard import build_risk_report

        report = build_risk_report(
            settings.DATA_DIR,
            dict(settings.CAPITAL_BY_CURRENCY),
            settings.TOTAL_CAPITAL,
            ohlcv_fetcher=lambda s: None,
            daily_loss_limit_pct=settings.DAILY_LOSS_LIMIT_PCT,
        )
        cur = (report.drawdown or {}).get("current_drawdown_pct")
        if cur is not None:
            ctx["drawdown_pct"] = abs(float(cur))
        budget = report.daily_loss_budget or {}
        today_pnl = budget.get("today_pnl")
        cap = float(settings.TOTAL_CAPITAL or 0.0)
        if today_pnl is not None and cap > 0:
            ctx["daily_loss_pct"] = max(0.0, -float(today_pnl) / cap * 100.0)
    except Exception:  # noqa: BLE001
        pass
    return ctx
