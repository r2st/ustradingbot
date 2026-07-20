"""
Tax-loss harvesting scanner + IRS Form 8949 / Schedule D export (P1-7).

Builds on the realised-lot / wash-sale machinery in :mod:`analytics.tax`:

* :func:`scan_harvest_opportunities` — scans open positions for unrealised
  losses worth harvesting, and flags where selling would trigger the wash-sale
  rule (a same-symbol purchase within ±30 days), so a suggestion never quietly
  recommends a trade whose loss the IRS would disallow.
* :func:`form_8949_rows` / :func:`form_8949_csv` — render realised lots into the
  IRS Form 8949 layout (Part I short-term, Part II long-term), including the
  wash-sale code ``W`` and disallowed-loss adjustment column.
* :func:`schedule_d_summary` — the Schedule D short/long-term roll-up.

The scanner accepts plain data (positions, a price map, recent-purchase dates)
so it unit-tests without any network or journal access.
"""

from __future__ import annotations

import csv
import io
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from analytics.tax import RealizedLot

WASH_SALE_WINDOW_DAYS = 30


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:19]).date()
    except (ValueError, TypeError):
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Harvesting scanner
# ---------------------------------------------------------------------------


@dataclass
class HarvestOpportunity:
    """A candidate tax-loss-harvest sale of an open position."""

    symbol: str
    quantity: float
    entry_price: float
    current_price: float
    unrealized_loss: float          # positive magnitude of the loss
    holding_days: int
    term: str                       # "short" | "long"
    wash_sale_risk: bool
    wash_sale_reason: str
    earliest_rebuy_date: Optional[str]  # 31 days after the sale to stay clean
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def scan_harvest_opportunities(
    positions: List[Dict[str, Any]],
    prices: Dict[str, float],
    recent_buys_by_symbol: Optional[Dict[str, List[Any]]] = None,
    min_loss: float = 0.0,
    wash_window_days: int = WASH_SALE_WINDOW_DAYS,
    as_of: Any = None,
    long_term_days: int = 365,
) -> List[HarvestOpportunity]:
    """Return harvestable unrealised losses in *positions*, largest loss first.

    Args:
        positions: open-position dicts (``symbol``, ``quantity``, ``entry_price``,
            ``entry_time``/``entry_date``, optional ``direction``).
        prices: ``{symbol: current_price}``.
        recent_buys_by_symbol: ``{symbol: [purchase dates]}`` used for wash-sale
            risk — a purchase within *wash_window_days* before the sale would
            disallow the harvested loss.
        min_loss: only report opportunities whose loss magnitude is at least this.
        as_of: reference "today" (defaults to ``date.today()``).
    """
    ref = _as_date(as_of) or date.today()
    recent = recent_buys_by_symbol or {}
    out: List[HarvestOpportunity] = []

    for pos in positions:
        symbol = str(pos.get("symbol", "")).upper()
        if not symbol:
            continue
        direction = str(pos.get("direction", "long") or "long").lower()
        if direction == "short":
            continue  # harvesting shorts is out of scope for this scanner
        qty = float(pos.get("quantity", 0) or 0)
        entry = float(pos.get("entry_price", 0) or 0)
        price = prices.get(symbol)
        if qty <= 0 or entry <= 0 or price is None:
            continue
        price = float(price)
        if price >= entry:
            continue  # not at a loss
        loss = round((entry - price) * qty, 2)
        if loss < float(min_loss):
            continue

        entry_date = _as_date(pos.get("entry_time") or pos.get("entry_date"))
        holding_days = (ref - entry_date).days if entry_date else 0
        term = "long" if holding_days >= long_term_days else "short"

        # Wash-sale risk: a same-symbol purchase within the window *before* the
        # sale (today) would disallow the loss.  The position's own entry only
        # counts if it is itself inside the window (a very recent buy).
        wash_risk = False
        reason = ""
        for buy in recent.get(symbol, []):
            bd = _as_date(buy)
            if bd is None:
                continue
            if 0 <= (ref - bd).days <= wash_window_days:
                wash_risk = True
                reason = (
                    f"purchase on {bd.isoformat()} is within {wash_window_days}d — "
                    "harvested loss would be disallowed"
                )
                break

        from datetime import timedelta

        earliest_rebuy = (ref + timedelta(days=wash_window_days + 1)).isoformat()
        note = (
            "Wash-sale risk — waiting to sell or avoid the recent lot preserves "
            "the deduction." if wash_risk
            else f"Harvest the loss; wait until {earliest_rebuy} to rebuy."
        )
        out.append(
            HarvestOpportunity(
                symbol=symbol,
                quantity=qty,
                entry_price=round(entry, 4),
                current_price=round(price, 4),
                unrealized_loss=loss,
                holding_days=holding_days,
                term=term,
                wash_sale_risk=wash_risk,
                wash_sale_reason=reason,
                earliest_rebuy_date=earliest_rebuy,
                note=note,
            )
        )

    out.sort(key=lambda o: o.unrealized_loss, reverse=True)
    return out


# ---------------------------------------------------------------------------
# IRS Form 8949
# ---------------------------------------------------------------------------


def _fmt_date(iso: str) -> str:
    """Format an ISO date as MM/DD/YYYY (the IRS 8949 convention)."""
    d = _as_date(iso)
    return d.strftime("%m/%d/%Y") if d else str(iso)


def form_8949_row(lot: RealizedLot) -> Dict[str, Any]:
    """Render one realised lot as an IRS Form 8949 row.

    Column (f) carries ``W`` for a wash sale; column (g) the disallowed-loss
    adjustment (a positive number that reduces the loss); column (h) the final
    gain/loss after the adjustment.
    """
    code = "W" if lot.wash_sale else ""
    adjustment = round(lot.disallowed_loss, 2) if lot.wash_sale else 0.0
    gain_after = round(lot.gain + adjustment, 2)
    return {
        "description": f"{lot.quantity:g} sh {lot.symbol}",
        "date_acquired": _fmt_date(lot.acquired),
        "date_sold": _fmt_date(lot.disposed),
        "proceeds": round(lot.proceeds, 2),
        "cost_basis": round(lot.cost_basis, 2),
        "code": code,
        "adjustment": adjustment,
        "gain_loss": gain_after,
        "term": lot.term,
    }


def form_8949_rows(lots: List[RealizedLot]) -> Dict[str, List[Dict[str, Any]]]:
    """Split lots into Part I (short-term) and Part II (long-term) 8949 rows."""
    short_rows = [form_8949_row(l) for l in lots if l.term != "long"]
    long_rows = [form_8949_row(l) for l in lots if l.term == "long"]
    return {"short_term": short_rows, "long_term": long_rows}


_CSV_HEADER = [
    "Description of property (a)",
    "Date acquired (b)",
    "Date sold (c)",
    "Proceeds (d)",
    "Cost basis (e)",
    "Code (f)",
    "Adjustment (g)",
    "Gain or loss (h)",
]


def form_8949_csv(lots: List[RealizedLot]) -> str:
    """Return a full Form 8949 CSV (both parts, with part headers + subtotals)."""
    parts = form_8949_rows(lots)
    buf = io.StringIO()
    writer = csv.writer(buf)

    for part_name, key in (("Part I — Short-Term", "short_term"),
                           ("Part II — Long-Term", "long_term")):
        writer.writerow([part_name])
        writer.writerow(_CSV_HEADER)
        proceeds = cost = adj = gain = 0.0
        for r in parts[key]:
            writer.writerow([
                r["description"], r["date_acquired"], r["date_sold"],
                f"{r['proceeds']:.2f}", f"{r['cost_basis']:.2f}", r["code"],
                f"{r['adjustment']:.2f}", f"{r['gain_loss']:.2f}",
            ])
            proceeds += r["proceeds"]
            cost += r["cost_basis"]
            adj += r["adjustment"]
            gain += r["gain_loss"]
        writer.writerow([
            "Totals", "", "", f"{proceeds:.2f}", f"{cost:.2f}", "",
            f"{adj:.2f}", f"{gain:.2f}",
        ])
        writer.writerow([])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Schedule D
# ---------------------------------------------------------------------------


def schedule_d_summary(lots: List[RealizedLot]) -> Dict[str, Any]:
    """Schedule D roll-up: short/long-term proceeds, basis, adjustments, gain."""
    def _bucket(term_lots: List[RealizedLot]) -> Dict[str, Any]:
        proceeds = sum(l.proceeds for l in term_lots)
        cost = sum(l.cost_basis for l in term_lots)
        adjustment = sum(l.disallowed_loss for l in term_lots if l.wash_sale)
        gain = sum(l.gain for l in term_lots) + adjustment
        return {
            "proceeds": round(proceeds, 2),
            "cost_basis": round(cost, 2),
            "adjustments": round(adjustment, 2),
            "gain_loss": round(gain, 2),
            "count": len(term_lots),
        }

    short = _bucket([l for l in lots if l.term != "long"])
    long = _bucket([l for l in lots if l.term == "long"])
    return {
        "short_term": short,
        "long_term": long,
        "net_gain_loss": round(short["gain_loss"] + long["gain_loss"], 2),
    }
