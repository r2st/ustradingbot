"""
Alert rules, history, and channel management API (monitoring feature 6).

* ``GET/PUT /api/alerts/rules`` — read/update which event types alert, on
  which channels, at what thresholds (validated; unknown keys rejected).
* ``GET /api/alerts/history`` — the persistent, reviewable dispatch log.
* ``POST /api/alerts/test`` — send a test message on one channel and report
  success/failure (surfaces bad Telegram tokens immediately, never a 500).
* ``GET /api/alerts/channels`` — which channels are configured (no secrets).
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from agent import alert_config
from dashboard.auth import get_settings, require_auth
from dashboard.http_util import parse_json_body

router = APIRouter(prefix="/api/alerts", tags=["Notifications"])


async def _body(request: Request) -> Dict[str, Any]:
    """Parse a JSON object body, 422 on malformed JSON (see B7)."""
    return await parse_json_body(request)


@router.get("/rules")
async def get_rules(_user: str = Depends(require_auth)):
    """The effective alert rule set (defaults merged under user overrides)."""
    settings = get_settings()
    return {
        "rules": alert_config.load_rules(settings.DATA_DIR),
        "event_types": list(alert_config.EVENT_TYPES),
        "channels": list(alert_config.CHANNELS),
    }


@router.put("/rules")
async def put_rules(request: Request, _user: str = Depends(require_auth)):
    """Validate and persist an updated rule set."""
    settings = get_settings()
    body = await _body(request)
    rules = body.get("rules", body)
    try:
        effective = alert_config.save_rules(settings.DATA_DIR, rules)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "rules": effective}


@router.get("/history")
async def get_history(
    limit: int = 100,
    type: str = "",
    since_ts: str = "",
    _user: str = Depends(require_auth),
):
    """Reviewable alert dispatch log, newest first."""
    settings = get_settings()
    records = alert_config.read_history(
        settings.DATA_DIR,
        limit=max(1, min(int(limit), 1000)),
        type_filter=type or None,
        since_ts=since_ts or None,
    )
    return {"alerts": records, "count": len(records)}


@router.get("/channels")
async def get_channels(_user: str = Depends(require_auth)):
    """Which delivery channels are configured/enabled (no secrets)."""
    settings = get_settings()
    return {
        "channels": {
            "telegram": bool(settings.TELEGRAM_BOT_TOKEN
                             and settings.TELEGRAM_CHAT_ID),
            "email": bool(settings.EMAIL_ALERTS_ENABLED and settings.SMTP_HOST
                          and settings.EMAIL_FROM and settings.EMAIL_TO),
            "push": True,  # in-app/PWA push needs no external configuration
        }
    }


@router.post("/test")
async def test_channel(request: Request, _user: str = Depends(require_auth)):
    """Send a test alert on one channel; report per-channel success clearly."""
    from agent.alerts import AlertManager

    settings = get_settings()
    body = await _body(request)
    channel = str(body.get("channel", "")).strip().lower()
    if channel not in alert_config.CHANNELS:
        raise HTTPException(
            status_code=400,
            detail=f"channel must be one of {list(alert_config.CHANNELS)}",
        )

    manager = AlertManager(settings)
    ok, error = False, ""
    text = "✅ USTradingBot test alert — your channel is working."
    try:
        if channel == "telegram":
            if not manager.telegram.enabled:
                error = "Telegram is not configured (token / chat id missing)."
            else:
                await manager.telegram.send(text)
                ok = True
        elif channel == "email":
            if not manager.email.enabled:
                error = "Email is not configured (SMTP host / from / to missing)."
            else:
                ok = manager.email.send_sync("USTradingBot test alert", text)
                if not ok:
                    error = "SMTP send failed — check host/credentials."
        elif channel == "push":
            from dashboard.push import publish

            publish("Test alert", text, settings.DATA_DIR)
            ok = True
    except Exception as exc:  # noqa: BLE001 -- report, never 500
        error = str(exc)

    alert_config.append_history(settings.DATA_DIR, "test", text, {channel: ok})
    return {"ok": ok, "channel": channel, "error": error}
