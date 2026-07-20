"""
Portfolio rebalancing / target allocation (P1-8).

Compares the open book's *current* allocation — sliced by sector, strategy, or
asset-type — against operator-defined *target* weights, flags buckets that have
drifted past a threshold, and emits the buy/sell suggestions that would restore
the targets.

All functions are pure over plain data (a positions list, a price map, a target
dict) so they unit-test without any network or filesystem access.  The engine /
dashboard wire them to the live book; an optional auto-rebalance mode can turn
the suggestions into orders through the existing manual-trade path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from config.universe import get_sector

DIMENSIONS = ("sector", "strategy", "asset_type")


def _bucket_of(pos: Dict[str, Any], dimension: str) -> str:
    """Classify a position into a bucket for the chosen dimension."""
    symbol = str(pos.get("symbol", ""))
    if dimension == "sector":
        return get_sector(symbol)
    if dimension == "strategy":
        return str(pos.get("strategy", "") or "unknown").lower()
    if dimension == "asset_type":
        try:
            from config.etf_universe import is_etf

            return "etf" if is_etf(symbol) else "stock"
        except Exception:  # noqa: BLE001
            return "stock"
    raise ValueError(f"dimension must be one of {DIMENSIONS}")


def _position_value(pos: Dict[str, Any], prices: Optional[Dict[str, float]]) -> float:
    """Market value of a position (marked to *prices* when available, else cost)."""
    symbol = str(pos.get("symbol", ""))
    try:
        qty = int(float(pos.get("quantity", 0) or 0))
    except (ValueError, TypeError):
        return 0.0
    px = None
    if prices:
        px = prices.get(symbol)
    if px is None:
        px = pos.get("entry_price", 0)
    try:
        return float(px) * qty
    except (ValueError, TypeError):
        return 0.0


def current_allocation(
    positions: List[Dict[str, Any]],
    dimension: str = "sector",
    prices: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Return the current allocation of the open book by *dimension*.

    Result: ``{"total": float, "buckets": {name: {"value": v, "pct": p}}}``
    where ``pct`` is a fraction of total book value (0..1).
    """
    buckets: Dict[str, float] = {}
    total = 0.0
    for pos in positions:
        value = _position_value(pos, prices)
        if value <= 0:
            continue
        bucket = _bucket_of(pos, dimension)
        buckets[bucket] = buckets.get(bucket, 0.0) + value
        total += value
    out = {
        name: {"value": round(v, 2),
               "pct": round(v / total, 4) if total > 0 else 0.0}
        for name, v in buckets.items()
    }
    return {"total": round(total, 2), "buckets": out}


def _normalize_targets(targets: Dict[str, float]) -> Dict[str, float]:
    """Coerce target weights to fractions summing to 1 (accepts % or fractions)."""
    clean = {str(k): float(v) for k, v in targets.items() if float(v) > 0}
    total = sum(clean.values())
    if total <= 0:
        return {}
    # If the caller gave percentages (sum ~100), normalise; fractions (~1) too.
    return {k: v / total for k, v in clean.items()}


def compute_drift(
    allocation: Dict[str, Any],
    targets: Dict[str, float],
) -> List[Dict[str, Any]]:
    """Return per-bucket drift rows (current vs target), largest |drift| first.

    ``drift_pct`` is ``current_pct - target_pct`` in **percentage points**.
    Buckets present in either the book or the targets are included, so a target
    with no current holding shows a negative drift (under-allocated).
    """
    tgt = _normalize_targets(targets)
    buckets = allocation.get("buckets", {})
    names = set(buckets) | set(tgt)
    rows: List[Dict[str, Any]] = []
    for name in names:
        cur = float(buckets.get(name, {}).get("pct", 0.0))
        target = float(tgt.get(name, 0.0))
        rows.append({
            "bucket": name,
            "current_pct": round(cur * 100, 2),
            "target_pct": round(target * 100, 2),
            "drift_pct": round((cur - target) * 100, 2),
        })
    rows.sort(key=lambda r: abs(r["drift_pct"]), reverse=True)
    return rows


@dataclass
class RebalanceAction:
    """A single suggested buy/sell to move a bucket toward its target."""

    bucket: str
    action: str          # "trim" | "add"
    drift_pct: float
    current_value: float
    target_value: float
    delta_value: float    # +ve to buy, -ve to sell

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def rebalance_suggestions(
    allocation: Dict[str, Any],
    targets: Dict[str, float],
    drift_threshold_pct: float = 5.0,
) -> List[RebalanceAction]:
    """Return the trim/add actions for buckets drifted past the threshold.

    ``delta_value`` is the dollar amount to buy (+) or sell (-) in each bucket to
    hit its target weight of the current total book value.  Only buckets whose
    absolute drift exceeds ``drift_threshold_pct`` are returned.
    """
    total = float(allocation.get("total", 0.0))
    tgt = _normalize_targets(targets)
    buckets = allocation.get("buckets", {})
    actions: List[RebalanceAction] = []
    names = set(buckets) | set(tgt)
    for name in names:
        cur_pct = float(buckets.get(name, {}).get("pct", 0.0))
        cur_val = float(buckets.get(name, {}).get("value", 0.0))
        target_pct = float(tgt.get(name, 0.0))
        drift_pp = (cur_pct - target_pct) * 100
        if abs(drift_pp) < float(drift_threshold_pct):
            continue
        target_val = round(target_pct * total, 2)
        delta = round(target_val - cur_val, 2)
        actions.append(RebalanceAction(
            bucket=name,
            action="trim" if delta < 0 else "add",
            drift_pct=round(drift_pp, 2),
            current_value=round(cur_val, 2),
            target_value=target_val,
            delta_value=delta,
        ))
    actions.sort(key=lambda a: abs(a.drift_pct), reverse=True)
    return actions


def build_rebalance_report(
    positions: List[Dict[str, Any]],
    targets: Dict[str, float],
    dimension: str = "sector",
    drift_threshold_pct: float = 5.0,
    prices: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Assemble the full current-vs-target + drift + suggestions payload."""
    allocation = current_allocation(positions, dimension, prices)
    drift = compute_drift(allocation, targets)
    actions = rebalance_suggestions(allocation, targets, drift_threshold_pct)
    needs_rebalance = any(abs(r["drift_pct"]) >= drift_threshold_pct for r in drift)
    return {
        "dimension": dimension,
        "drift_threshold_pct": drift_threshold_pct,
        "total_value": allocation["total"],
        "allocation": allocation["buckets"],
        "drift": drift,
        "suggestions": [a.to_dict() for a in actions],
        "needs_rebalance": needs_rebalance,
    }
