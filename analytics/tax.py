"""
Tax / realized-gains reporting for the trade journal (feature P1f).

Builds a US-style realized-gains report from the completed-trades journal
(``trades.csv``):

* **Cost-basis (FIFO).**  Each journal row is a fully realised round trip, so
  it already pairs an entry lot with an exit.  :func:`fifo_match` still runs a
  genuine first-in-first-out matcher over a reconstructed per-symbol BUY/SELL
  event stream so partial fills and overlapping lots are handled correctly; for
  the one-row-per-round-trip journal it reduces to the per-row result.
* **Short-term vs long-term split.**  A lot held **more than one year**
  (``holding_days > 365``) is long-term; otherwise short-term.  Short sales are
  always short-term regardless of holding period.
* **Wash-sale detection.**  A realised loss is flagged when the same symbol was
  re-bought within ±30 calendar days of the loss sale (excluding the lot's own
  matched entry).  The disallowed loss is reported for review — this is a
  flagging aid, not tax advice.

All maths is pure and unit-testable; :func:`build_tax_report` is the only
function that touches the filesystem.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import structlog

log = structlog.get_logger(__name__)

WASH_SALE_WINDOW_DAYS = 30
LONG_TERM_MIN_DAYS = 365  # held strictly more than one year → long-term


# ---------------------------------------------------------------------------
# Events + lots
# ---------------------------------------------------------------------------


@dataclass
class _Event:
    """A reconstructed buy or sell leg of a journal round-trip."""

    symbol: str
    side: str  # "buy" or "sell"
    when: datetime
    quantity: float
    price: float
    commission: float
    direction: str  # original position direction ("long"/"short")


@dataclass
class RealizedLot:
    """One realised tax lot (a matched buy→sell)."""

    symbol: str
    quantity: float
    acquired: str        # ISO date the lot was opened
    disposed: str        # ISO date the lot was closed
    holding_days: int
    term: str            # "short" or "long"
    cost_basis: float
    proceeds: float
    gain: float
    tax_year: int
    direction: str = "long"
    wash_sale: bool = False
    disallowed_loss: float = 0.0
    replacement_date: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TaxReport:
    year: Any = "all"
    available_years: List[int] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    wash_sales: List[Dict[str, Any]] = field(default_factory=list)
    wash_sale_count: int = 0
    total_disallowed_loss: float = 0.0
    lots: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "year": self.year,
            "available_years": self.available_years,
            "summary": self.summary,
            "wash_sales": self.wash_sales,
            "wash_sale_count": self.wash_sale_count,
            "total_disallowed_loss": self.total_disallowed_loss,
            "lots": self.lots,
        }


# ---------------------------------------------------------------------------
# Parsing the journal into events
# ---------------------------------------------------------------------------


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def _dt(value: Any) -> Optional[datetime]:
    ts = pd.to_datetime(value, errors="coerce")
    if ts is None or pd.isna(ts):
        return None
    return ts.to_pydatetime()


def events_from_trades(trades: pd.DataFrame) -> Dict[str, List[_Event]]:
    """Reconstruct a per-symbol chronological BUY/SELL event stream.

    A long round-trip becomes BUY@entry then SELL@exit; a short becomes
    SELL@entry then BUY@exit (cover).  Rows without a usable entry/exit time or
    a positive quantity are skipped (and logged at debug).
    """
    by_symbol: Dict[str, List[_Event]] = {}
    if trades is None or trades.empty:
        return by_symbol

    for _, row in trades.iterrows():
        symbol = str(row.get("symbol", "") or "").upper()
        if not symbol:
            continue
        entry_t = _dt(row.get("entry_time"))
        exit_t = _dt(row.get("exit_time"))
        qty = _f(row.get("quantity"))
        if entry_t is None or exit_t is None or qty <= 0:
            log.debug("tax.skip_row", symbol=symbol, reason="missing time/qty")
            continue
        direction = str(row.get("direction", "long") or "long").lower()
        entry_px = _f(row.get("entry_fill_price"))
        exit_px = _f(row.get("exit_price"))
        entry_comm = _f(row.get("entry_commission"))
        exit_comm = _f(row.get("exit_commission"))

        open_side, close_side = ("buy", "sell") if direction != "short" else ("sell", "buy")
        events = by_symbol.setdefault(symbol, [])
        events.append(_Event(symbol, open_side, entry_t, qty, entry_px, entry_comm, direction))
        events.append(_Event(symbol, close_side, exit_t, qty, exit_px, exit_comm, direction))

    for symbol in by_symbol:
        by_symbol[symbol].sort(key=lambda e: (e.when, 0 if e.side in ("buy", "sell") else 1))
    return by_symbol


# ---------------------------------------------------------------------------
# FIFO matching
# ---------------------------------------------------------------------------


def fifo_match(events: List[_Event]) -> List[RealizedLot]:
    """FIFO-match a single symbol's BUY/SELL events into realised lots.

    Opening legs (buys for longs, sells for shorts) accumulate as open lots;
    each closing leg is matched against the oldest open lots first, splitting a
    lot when a close only partially fills it.  Each realised lot carries its own
    cost basis, proceeds, holding period, and term.
    """
    lots: List[RealizedLot] = []
    if not events:
        return lots

    direction = events[0].direction
    is_short = direction == "short"
    open_side = "sell" if is_short else "buy"

    # Open lots: list of mutable dicts {qty, price, commission, when}.
    open_lots: List[Dict[str, Any]] = []

    for ev in events:
        if ev.side == open_side:
            open_lots.append({
                "qty": ev.quantity,
                "price": ev.price,
                # per-share commission so partial matches split it fairly
                "comm_per_share": (ev.commission / ev.quantity) if ev.quantity else 0.0,
                "when": ev.when,
            })
            continue

        # Closing leg — match FIFO against open lots.
        remaining = ev.quantity
        close_comm_per_share = (ev.commission / ev.quantity) if ev.quantity else 0.0
        while remaining > 1e-9 and open_lots:
            lot = open_lots[0]
            matched = min(remaining, lot["qty"])
            open_comm = lot["comm_per_share"] * matched
            close_comm = close_comm_per_share * matched
            if is_short:
                # Opened by selling (proceeds), closed by buying (cost).
                proceeds = lot["price"] * matched - open_comm
                cost_basis = ev.price * matched + close_comm
                acquired, disposed = ev.when, lot["when"]
            else:
                cost_basis = lot["price"] * matched + open_comm
                proceeds = ev.price * matched - close_comm
                acquired, disposed = lot["when"], ev.when

            holding_days = (ev.when - lot["when"]).days
            term = "long" if (not is_short and holding_days > LONG_TERM_MIN_DAYS) else "short"
            gain = proceeds - cost_basis
            lots.append(RealizedLot(
                symbol=ev.symbol,
                quantity=round(matched, 6),
                acquired=acquired.isoformat(),
                disposed=disposed.isoformat(),
                holding_days=int(holding_days),
                term=term,
                cost_basis=round(cost_basis, 2),
                proceeds=round(proceeds, 2),
                gain=round(gain, 2),
                tax_year=int(ev.when.year),
                direction=direction,
            ))
            lot["qty"] -= matched
            remaining -= matched
            if lot["qty"] <= 1e-9:
                open_lots.pop(0)
    return lots


# ---------------------------------------------------------------------------
# Wash-sale detection
# ---------------------------------------------------------------------------


def detect_wash_sales(
    lots: List[RealizedLot],
    events_by_symbol: Dict[str, List[_Event]],
    window_days: int = WASH_SALE_WINDOW_DAYS,
) -> List[RealizedLot]:
    """Flag realised losses with a same-symbol repurchase within ±window days.

    Mutates the flagged lots in place (sets ``wash_sale``, ``disallowed_loss``,
    ``replacement_date``) and returns the list of flagged lots.
    """
    flagged: List[RealizedLot] = []
    for lot in lots:
        if lot.gain >= 0:
            continue
        buys = [
            e for e in events_by_symbol.get(lot.symbol, [])
            if e.side == "buy"
        ]
        sale_date = pd.to_datetime(lot.disposed).to_pydatetime()
        own_entry = pd.to_datetime(lot.acquired).to_pydatetime()
        for buy in buys:
            # Exclude the lot's own opening purchase.
            if abs((buy.when - own_entry).total_seconds()) < 1.0:
                continue
            if abs((buy.when - sale_date).days) <= window_days:
                lot.wash_sale = True
                lot.disallowed_loss = round(-lot.gain, 2)
                lot.replacement_date = buy.when.isoformat()
                flagged.append(lot)
                break
    return flagged


# ---------------------------------------------------------------------------
# Summaries + top-level report
# ---------------------------------------------------------------------------


def summarize(lots: List[RealizedLot]) -> Dict[str, Any]:
    """Short-term / long-term proceeds, cost basis, and gain totals."""
    buckets = {
        "short": {"proceeds": 0.0, "cost_basis": 0.0, "gain": 0.0, "count": 0},
        "long": {"proceeds": 0.0, "cost_basis": 0.0, "gain": 0.0, "count": 0},
    }
    for lot in lots:
        b = buckets["long" if lot.term == "long" else "short"]
        b["proceeds"] += lot.proceeds
        b["cost_basis"] += lot.cost_basis
        b["gain"] += lot.gain
        b["count"] += 1
    for b in buckets.values():
        b["proceeds"] = round(b["proceeds"], 2)
        b["cost_basis"] = round(b["cost_basis"], 2)
        b["gain"] = round(b["gain"], 2)
    total_gain = round(buckets["short"]["gain"] + buckets["long"]["gain"], 2)
    total_proceeds = round(
        buckets["short"]["proceeds"] + buckets["long"]["proceeds"], 2
    )
    return {
        "short_term": buckets["short"],
        "long_term": buckets["long"],
        "total_gain": total_gain,
        "total_proceeds": total_proceeds,
    }


def realized_lots(trades: pd.DataFrame) -> List[RealizedLot]:
    """All realised tax lots across every symbol (FIFO), with wash-sale flags."""
    events_by_symbol = events_from_trades(trades)
    lots: List[RealizedLot] = []
    for symbol, events in events_by_symbol.items():
        lots.extend(fifo_match(events))
    detect_wash_sales(lots, events_by_symbol)
    lots.sort(key=lambda lot: lot.disposed, reverse=True)
    return lots


def build_tax_report(
    data_dir: str | Path, year: Optional[int] = None
) -> TaxReport:
    """Assemble a :class:`TaxReport` from ``trades.csv`` (optionally one year)."""
    from analytics.performance import load_completed_trades

    trades = load_completed_trades(Path(data_dir) / "trades.csv")
    all_lots = realized_lots(trades)
    available_years = sorted({lot.tax_year for lot in all_lots})

    if year is not None:
        lots = [lot for lot in all_lots if lot.tax_year == int(year)]
        report_year: Any = int(year)
    else:
        lots = all_lots
        report_year = "all"

    wash = [lot for lot in lots if lot.wash_sale]
    return TaxReport(
        year=report_year,
        available_years=available_years,
        summary=summarize(lots),
        wash_sales=[lot.to_dict() for lot in wash],
        wash_sale_count=len(wash),
        total_disallowed_loss=round(sum(lot.disallowed_loss for lot in wash), 2),
        lots=[lot.to_dict() for lot in lots],
    )
