"""
User-defined price-alert API (feature P2f).

CRUD for price-cross alert rules plus a manual ``/check`` trigger, under
``/api/price-alerts``.  Every route is guarded by the shared HTTP Basic Auth
dependency; malformed bodies come back as 422 via :func:`parse_json_body`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from alerts.price_alerts import PriceAlertError, get_price_alert_store
from config.settings import get_settings
from dashboard.auth import require_auth
from dashboard.http_util import parse_json_body

router = APIRouter(prefix="/api/price-alerts", tags=["Notifications"])


def _store():
    return get_price_alert_store(get_settings().DATA_DIR)


@router.get("")
async def list_alerts(_user: str = Depends(require_auth)):
    """Every price-alert rule (armed and triggered)."""
    return {"alerts": _store().list_alerts()}


@router.post("")
async def create_alert(request: Request, _user: str = Depends(require_auth)):
    """Create a rule: body ``{symbol, direction: above|below, threshold, note?}``."""
    body = await parse_json_body(request)
    try:
        rule = _store().add_alert(
            symbol=str(body.get("symbol", "")),
            direction=str(body.get("direction", "")),
            threshold=body.get("threshold"),
            note=str(body.get("note", "")),
        )
    except PriceAlertError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return rule


@router.delete("/{alert_id}")
async def delete_alert(alert_id: str, _user: str = Depends(require_auth)):
    if not _store().delete_alert(alert_id):
        raise HTTPException(status_code=404, detail="Unknown alert id.")
    return {"ok": True}


@router.post("/{alert_id}/toggle")
async def toggle_alert(alert_id: str, request: Request,
                       _user: str = Depends(require_auth)):
    """Enable/disable a rule; re-enabling re-arms it. Body ``{active: bool}``."""
    body = await parse_json_body(request)
    active = bool(body.get("active", True))
    rule = _store().set_active(alert_id, active)
    if rule is None:
        raise HTTPException(status_code=404, detail="Unknown alert id.")
    return rule


@router.post("/check")
async def check_alerts(_user: str = Depends(require_auth)):
    """Run one check against live prices now; return the rules that fired."""
    from alerts.price_alerts import check_price_alerts

    settings = get_settings()
    triggered = await run_in_threadpool(check_price_alerts, settings)
    return {"triggered": triggered, "count": len(triggered)}
