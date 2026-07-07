"""
Trade rationale API (monitoring feature 9).

Serves the per-trade rationale records the engine captures at entry time
(scored criteria + bar snapshot for the breakout-pattern chart).  See
:mod:`journal.rationale`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from dashboard.auth import get_settings, require_auth
from journal.rationale import find_rationale, read_rationales

router = APIRouter(prefix="/api/rationale", tags=["rationale"])


@router.get("")
async def rationale_list(
    symbol: str = "",
    entry_time: str = "",
    limit: int = 50,
    include_bars: bool = True,
    _user: str = Depends(require_auth),
):
    """Rationale records, newest first.

    With ``symbol`` (and optionally ``entry_time``) the single best-matching
    record is returned as ``record`` (``null`` when nothing was captured for
    that trade); otherwise a newest-first list is returned as ``records``.
    ``include_bars=false`` drops the bar snapshots for lightweight listings.
    """
    settings = get_settings()
    # The F9 modal never renders the v2 indicator series — that is the TA
    # chart's job (``/api/trade/{symbol}/ta-chart``) — so drop the heavy
    # ``indicators`` key from every response here.
    drop = {"indicators"} if include_bars else {"indicators", "bars"}
    if symbol:
        record = find_rationale(settings.DATA_DIR, symbol, entry_time or None)
        if record is not None:
            record = {k: v for k, v in record.items() if k not in drop}
        return {"record": record}
    records = read_rationales(
        settings.DATA_DIR, limit=max(1, min(int(limit), 500))
    )
    records = [
        {k: v for k, v in r.items() if k not in drop} for r in records
    ]
    return {"records": records, "count": len(records)}
