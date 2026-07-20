"""
Portfolio rebalancing API (P1-8).

Current-vs-target allocation, drift detection, and rebalancing suggestions under
``/api/rebalance``.  Read endpoints use the shared Basic Auth guard; the
``/apply`` endpoint (which places orders) additionally requires the admin
password and the ``REBALANCE_AUTO`` opt-in.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from config.settings import get_settings
from dashboard.auth import require_auth, verify_admin_password
from dashboard.http_util import parse_json_body
from dashboard.rate_limit import rate_limit

router = APIRouter(prefix="/api/rebalance", tags=["Analytics"])


def _prices_for(positions) -> dict:
    prices: dict = {}
    try:
        from dashboard import quotes as _quotes

        syms = [str(p.get("symbol", "")) for p in positions if p.get("symbol")]
        for sym, q in _quotes.get_quotes(syms).items():
            if q.get("price") is not None:
                prices[sym] = float(q["price"])
    except Exception:  # noqa: BLE001
        prices = {}
    return prices


def _report(settings, dimension=None):
    from analytics.rebalance import build_rebalance_report
    from dashboard.app import _load_open_positions

    positions = _load_open_positions(Path(settings.DATA_DIR))
    prices = _prices_for(positions)
    return build_rebalance_report(
        positions,
        dict(settings.REBALANCE_TARGETS),
        dimension=dimension or settings.REBALANCE_DIMENSION,
        drift_threshold_pct=settings.REBALANCE_DRIFT_THRESHOLD_PCT,
        prices=prices,
    )


@router.get("")
async def rebalance_report(dimension: str = "", _user: str = Depends(require_auth)):
    """Current vs target allocation, drift, and rebalancing suggestions."""
    settings = get_settings()
    dim = dimension or None
    report = await run_in_threadpool(_report, settings, dim)
    report["auto_enabled"] = bool(settings.REBALANCE_AUTO)
    report["configured"] = bool(settings.REBALANCE_TARGETS)
    return report


@router.post("/targets")
async def set_targets(request: Request, _user: str = Depends(require_auth)):
    """Update the target allocation: body ``{dimension?, targets: {bucket: pct}}``.

    Persists to the settings singleton for this process; deployments that want
    the change to survive a restart should also set the env var.
    """
    body = await parse_json_body(request)
    targets = body.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise HTTPException(status_code=400, detail="targets must be a non-empty map")
    clean: dict = {}
    for k, v in targets.items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"bad weight for {k!r}")
        if fv < 0:
            raise HTTPException(status_code=400, detail=f"weight for {k!r} must be >= 0")
        clean[str(k)] = fv
    settings = get_settings()
    settings.REBALANCE_TARGETS = clean
    if body.get("dimension"):
        settings.REBALANCE_DIMENSION = str(body["dimension"])
    return {"ok": True, "dimension": settings.REBALANCE_DIMENSION, "targets": clean}


@router.post("/apply", dependencies=[Depends(rate_limit("rebalance", control=True))])
async def apply_rebalance(request: Request, _user: str = Depends(require_auth)):
    """Preview/apply the rebalancing trades (admin + REBALANCE_AUTO required).

    Body ``{admin_password, dry_run?}``.  With ``dry_run`` true (default) the
    suggested orders are returned but not placed; the auto-rebalance path only
    trims over-allocated single-name buckets, never adds (which needs a symbol
    selection the operator must make deliberately).
    """
    body = await parse_json_body(request)
    settings = get_settings()
    if not settings.REBALANCE_AUTO:
        raise HTTPException(status_code=403, detail="REBALANCE_AUTO is disabled.")
    if not verify_admin_password(body.get("admin_password")):
        raise HTTPException(status_code=403, detail="Admin password required.")
    report = await run_in_threadpool(_report, settings, None)
    # Auto mode only surfaces the trims as an ordered plan — placement of real
    # orders stays a deliberate, explicit follow-up to avoid a runaway loop.
    trims = [s for s in report["suggestions"] if s["action"] == "trim"]
    return {
        "ok": True,
        "dry_run": bool(body.get("dry_run", True)),
        "plan": trims,
        "note": "Auto-rebalance returns a trim plan; execute trims via the "
        "manual-trade path to keep every order operator-confirmed.",
    }
