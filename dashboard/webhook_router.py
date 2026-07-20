"""
Inbound webhooks + writable control API (P0-3).

POST endpoints that let an external system (a TradingView alert, a custom
screener, an ops script) submit or veto trades:

* ``POST /api/webhooks/trade`` — submit a buy/sell with symbol, qty, stop,
  target.  Routed through the same :func:`execution.manual_trade.place_manual_trade`
  path the dashboard's manual-trade button uses, so it inherits every risk gate.
* ``POST /api/webhooks/veto`` — block new automated entries for a symbol.
* ``POST /api/webhooks/tradingview`` — accept a TradingView alert JSON and route
  it to the trade or veto path.

Security (defence in depth):

1. ``WEBHOOKS_ENABLED`` must be true (off by default — this is a money path).
2. API-key auth (reusing the REST-API key store) via ``Authorization: Bearer``
   or ``X-API-Key``.
3. Optional HMAC-SHA256 body signature (``X-Signature: sha256=<hex>``) verified
   against ``WEBHOOK_HMAC_SECRET`` when that secret is configured.
4. Per-IP rate limiting on every write endpoint.
5. ``WEBHOOK_ALLOW_TRADES`` gates real order placement; when false the trade is
   validated and audited but not executed (dry-run).
"""

from __future__ import annotations

import hmac
import json
from hashlib import sha256
from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from config.settings import get_settings
from dashboard.api_keys import get_api_key_store
from dashboard.rate_limit import rate_limit

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["Webhooks"])


def _client_ip(request: Request | None) -> str:
    if request is None:
        return "unknown"
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def require_webhook_key(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> str:
    """Authenticate a webhook via Bearer token or ``X-API-Key`` header.

    Independent of the read-only REST API's gate: webhooks have their own
    ``WEBHOOKS_ENABLED`` master switch but reuse the same API-key store so keys
    are managed in one place.
    """
    settings = get_settings()
    if not getattr(settings, "WEBHOOKS_ENABLED", False):
        raise HTTPException(status_code=404, detail="Webhooks disabled")

    key = ""
    if authorization and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    elif x_api_key:
        key = x_api_key.strip()

    if not key or not get_api_key_store(settings.DATA_DIR).verify(key):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return key


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 verification of a raw request body.

    Accepts the signature with or without a ``sha256=`` prefix (the GitHub /
    TradingView convention).  Returns ``True`` when *secret* is empty (signature
    checking disabled) so the caller can treat "no secret configured" as
    "signatures not required".
    """
    if not secret:
        return True
    if not signature:
        return False
    presented = signature.strip()
    if presented.startswith("sha256="):
        presented = presented[len("sha256="):]
    expected = hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()
    return hmac.compare_digest(expected, presented)


async def _read_verified_body(
    request: Request, x_signature: Optional[str]
) -> Dict[str, Any]:
    """Read the raw body, verify its HMAC signature, and parse JSON."""
    settings = get_settings()
    raw = await request.body()
    secret = getattr(settings, "WEBHOOK_HMAC_SECRET", "") or ""
    if not verify_signature(raw, x_signature or "", secret):
        log.warning("webhook.bad_signature", ip=_client_ip(request))
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=422, detail="Body must be valid JSON.")
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="Body must be a JSON object.")
    return data


# ---------------------------------------------------------------------------
# TradingView alert parsing (pure — unit-tested without HTTP)
# ---------------------------------------------------------------------------


def parse_tradingview_alert(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a TradingView alert JSON into an intent envelope.

    Handles both a flat custom convention and TradingView's native
    ``{"strategy": {"order_action": ...}}`` shape.  Returns a dict with:

    * ``intent`` — ``"trade"`` or ``"veto"``.
    * for a trade: ``symbol``, ``side`` (buy/sell), ``quantity``, ``stop_price``,
      ``target_price``, ``entry_price`` (any of the price fields may be ``None``).
    * for a veto: ``symbol``, ``strategy``, ``ttl_minutes``, ``note``.

    Raises ``ValueError`` when the payload has no recognisable symbol/action.
    """
    strat_block = payload.get("strategy")
    strat_block = strat_block if isinstance(strat_block, dict) else {}

    symbol = str(
        payload.get("ticker")
        or payload.get("symbol")
        or strat_block.get("ticker")
        or ""
    ).strip().upper()
    if not symbol:
        raise ValueError("TradingView alert missing a ticker/symbol.")

    action = str(
        payload.get("action")
        or payload.get("order_action")
        or strat_block.get("order_action")
        or ""
    ).strip().lower()

    if action in {"veto", "block"}:
        return {
            "intent": "veto",
            "symbol": symbol,
            "strategy": str(payload.get("strategy_name", "") or "").strip().lower(),
            "ttl_minutes": payload.get("ttl_minutes"),
            "note": str(payload.get("comment") or payload.get("note") or "")[:200],
        }

    if action not in {"buy", "sell", "long", "short"}:
        raise ValueError(f"Unrecognised TradingView action: {action!r}")
    side = "buy" if action in {"buy", "long"} else "sell"

    def _num(*keys: str) -> Optional[float]:
        for k in keys:
            v = payload.get(k)
            if v is None and k in strat_block:
                v = strat_block.get(k)
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    qty = _num("quantity", "contracts", "order_contracts", "position_size")
    return {
        "intent": "trade",
        "symbol": symbol,
        "side": side,
        "quantity": int(qty) if qty else None,
        "entry_price": _num("entry", "entry_price", "price", "close"),
        "stop_price": _num("stop", "stop_price", "stop_loss", "sl"),
        "target_price": _num("target", "target_price", "take_profit", "tp"),
    }


# ---------------------------------------------------------------------------
# Trade execution helper
# ---------------------------------------------------------------------------


async def _submit_trade(params: Dict[str, Any], ip: str, source: str) -> Dict[str, Any]:
    """Route a normalised trade envelope to the manual-trade path (or dry-run)."""
    settings = get_settings()
    body = {
        "symbol": params.get("symbol"),
        "side": params.get("side", "buy"),
        "quantity": params.get("quantity"),
        "entry_price": params.get("entry_price"),
        "stop_price": params.get("stop_price"),
        "target_price": params.get("target_price"),
    }
    body = {k: v for k, v in body.items() if v is not None}

    if not getattr(settings, "WEBHOOK_ALLOW_TRADES", False):
        log.info("webhook.trade_dryrun", source=source, ip=ip, **body)
        return {
            "ok": True,
            "dry_run": True,
            "message": "Webhook trades are disabled (WEBHOOK_ALLOW_TRADES=False); "
            "validated but not placed.",
            "trade": body,
        }

    from execution.manual_trade import place_manual_trade

    result = await run_in_threadpool(place_manual_trade, body, settings)
    ok = bool(getattr(result, "ok", True))
    (log.info if ok else log.warning)(
        "webhook.trade_placed" if ok else "webhook.trade_rejected",
        source=source,
        ip=ip,
        symbol=getattr(result, "symbol", "") or body.get("symbol"),
        ok=ok,
        message=getattr(result, "message", ""),
    )
    from dashboard import metrics

    metrics.inc(
        "ustb_webhook_trades_total",
        labels={"result": "placed" if ok else "rejected", "source": source},
        help_text="Inbound webhook trade submissions by outcome.",
    )
    return result.to_dict()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/trade", dependencies=[Depends(rate_limit("webhook", control=True))])
async def webhook_trade(
    request: Request,
    _key: str = Depends(require_webhook_key),
    x_signature: Optional[str] = Header(default=None),
):
    """Submit a trade via webhook: ``{symbol, side, quantity, stop_price,
    target_price, entry_price?}``."""
    data = await _read_verified_body(request, x_signature)
    if not data.get("symbol"):
        raise HTTPException(status_code=422, detail="symbol is required.")
    return await _submit_trade(data, _client_ip(request), source="direct")


@router.post("/veto", dependencies=[Depends(rate_limit("webhook", control=True))])
async def webhook_veto(
    request: Request,
    _key: str = Depends(require_webhook_key),
    x_signature: Optional[str] = Header(default=None),
):
    """Veto new entries for a symbol: ``{symbol, strategy?, ttl_minutes?, note?}``."""
    data = await _read_verified_body(request, x_signature)
    from execution.veto import get_veto_store, VetoError

    settings = get_settings()
    try:
        rule = get_veto_store(settings.DATA_DIR).add(
            symbol=str(data.get("symbol", "")),
            strategy=str(data.get("strategy", "") or ""),
            ttl_minutes=data.get("ttl_minutes"),
            note=str(data.get("note", "") or ""),
        )
    except VetoError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    log.info("webhook.veto_added", ip=_client_ip(request), symbol=rule["symbol"])
    return {"ok": True, "veto": rule}


@router.get("/veto")
async def webhook_list_vetoes(_key: str = Depends(require_webhook_key)):
    """List active vetoes."""
    from execution.veto import get_veto_store

    settings = get_settings()
    return {"vetoes": get_veto_store(settings.DATA_DIR).list_active()}


@router.delete("/veto/{veto_id}")
async def webhook_remove_veto(
    veto_id: str, _key: str = Depends(require_webhook_key)
):
    """Remove a veto by id."""
    from execution.veto import get_veto_store

    settings = get_settings()
    removed = get_veto_store(settings.DATA_DIR).remove(veto_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Veto not found.")
    return {"ok": True, "removed": veto_id}


@router.post(
    "/tradingview", dependencies=[Depends(rate_limit("webhook", control=True))]
)
async def webhook_tradingview(
    request: Request,
    _key: str = Depends(require_webhook_key),
    x_signature: Optional[str] = Header(default=None),
):
    """Accept a TradingView alert JSON and route it to the trade or veto path."""
    data = await _read_verified_body(request, x_signature)
    try:
        envelope = parse_tradingview_alert(data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if envelope["intent"] == "veto":
        from execution.veto import get_veto_store, VetoError

        settings = get_settings()
        try:
            rule = get_veto_store(settings.DATA_DIR).add(
                symbol=envelope["symbol"],
                strategy=envelope.get("strategy", ""),
                ttl_minutes=envelope.get("ttl_minutes"),
                note=envelope.get("note", ""),
            )
        except VetoError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"ok": True, "intent": "veto", "veto": rule}

    return await _submit_trade(
        envelope, _client_ip(request), source="tradingview"
    )
