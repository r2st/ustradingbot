"""
Dividend tracking & total-return accounting (P1-6).

Tracks ex-dividend dates and per-share amounts, attributes accrued dividend
income to open positions, folds that income into total-return P&L, and detects
ex-dividend gap-downs so a mechanical stop / health exit is not tripped by a
price drop that is really just the stock going ex-dividend.

The pure helpers accept plain data (a share count, an entry date, a list of
``{ex_date, amount}`` records) so they unit-test with no network.
:func:`fetch_dividends` wraps yfinance (6 h cached) for live use.
"""

from __future__ import annotations

import threading
import time as _time
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

import structlog

log = structlog.get_logger(__name__)

_CACHE_TTL_SECONDS = 6 * 3600
_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


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
# Fetch
# ---------------------------------------------------------------------------


def fetch_dividends(
    symbol: str, fetcher: Optional[Callable[[str], Any]] = None
) -> List[Dict[str, Any]]:
    """Return ``[{ex_date: 'YYYY-MM-DD', amount: float}, ...]`` for *symbol*.

    Cached for 6 h.  ``fetcher`` is injectable for tests; the default reads
    ``yfinance.Ticker(symbol).dividends`` (a Series indexed by ex-date).  Any
    error yields an empty list (fail-soft — dividends are additive accounting).
    """
    now = _time.monotonic()
    with _cache_lock:
        entry = _cache.get(symbol)
        if entry is not None and now - entry[0] <= _CACHE_TTL_SECONDS:
            return entry[1]
    result = (fetcher or _yf_dividends)(symbol)
    records = _normalize(result)
    with _cache_lock:
        _cache[symbol] = (now, records)
    return records


def _normalize(raw: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if raw is None:
        return out
    # Accept a pandas Series (yfinance), a dict, or a list of records.
    items: Any
    if hasattr(raw, "items"):
        items = raw.items()
    else:
        items = raw
    for entry in items:
        if isinstance(entry, dict):
            ex_date = _as_date(entry.get("ex_date") or entry.get("date"))
            amount = entry.get("amount")
        else:
            try:
                ex_date, amount = entry
            except (TypeError, ValueError):
                continue
            ex_date = _as_date(ex_date)
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            continue
        if ex_date is None or amt <= 0:
            continue
        out.append({"ex_date": ex_date.isoformat(), "amount": round(amt, 6)})
    out.sort(key=lambda r: r["ex_date"])
    return out


def _yf_dividends(symbol: str) -> Any:
    try:
        import yfinance as yf

        return yf.Ticker(symbol).dividends
    except Exception as exc:  # noqa: BLE001
        log.warning("dividends.fetch_failed", symbol=symbol, error=str(exc))
        return None


# ---------------------------------------------------------------------------
# Income attribution
# ---------------------------------------------------------------------------


def dividends_between(
    dividends: List[Dict[str, Any]],
    start: Any,
    end: Any,
) -> List[Dict[str, Any]]:
    """Return dividend records with ``start < ex_date <= end``.

    Holding a share on the ex-date entitles you to the dividend, so an entry on
    the ex-date itself does *not* accrue (start is exclusive); an exit/as-of on
    the ex-date does (end is inclusive).
    """
    s = _as_date(start)
    e = _as_date(end)
    out: List[Dict[str, Any]] = []
    for rec in dividends:
        d = _as_date(rec.get("ex_date"))
        if d is None:
            continue
        if s is not None and d <= s:
            continue
        if e is not None and d > e:
            continue
        out.append(rec)
    return out


def position_dividend_income(
    shares: float,
    entry_date: Any,
    dividends: List[Dict[str, Any]],
    as_of: Any = None,
) -> float:
    """Dividend income accrued to a *shares* position opened at *entry_date*."""
    as_of = as_of or date.today()
    total = sum(
        float(r["amount"]) for r in dividends_between(dividends, entry_date, as_of)
    )
    return round(float(shares) * total, 2)


def portfolio_dividend_income(
    positions: List[Dict[str, Any]],
    dividends_by_symbol: Dict[str, List[Dict[str, Any]]],
    as_of: Any = None,
) -> Dict[str, Any]:
    """Total + per-position accrued dividend income for the open book."""
    rows: List[Dict[str, Any]] = []
    total = 0.0
    for pos in positions:
        symbol = str(pos.get("symbol", ""))
        shares = float(pos.get("quantity", 0) or 0)
        entry = pos.get("entry_time") or pos.get("entry_date")
        divs = dividends_by_symbol.get(symbol, [])
        income = position_dividend_income(shares, entry, divs, as_of)
        if income:
            rows.append({"symbol": symbol, "shares": shares,
                         "dividend_income": income})
            total += income
    rows.sort(key=lambda r: r["dividend_income"], reverse=True)
    return {"total": round(total, 2), "by_position": rows}


def total_return(realized_pnl: float, dividend_income: float) -> Dict[str, Any]:
    """Combine capital P&L and dividend income into a total-return figure."""
    return {
        "realized_pnl": round(float(realized_pnl), 2),
        "dividend_income": round(float(dividend_income), 2),
        "total_return": round(float(realized_pnl) + float(dividend_income), 2),
    }


def upcoming_ex_dividends(
    positions: List[Dict[str, Any]],
    dividends_by_symbol: Dict[str, List[Dict[str, Any]]],
    as_of: Any = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Future ex-dividend dates for the open book, soonest first.

    Each row: ``{symbol, ex_date, amount, shares, est_payment}`` where
    ``est_payment`` is ``shares * amount``.  Only ex-dates on/after *as_of* are
    included (already-passed dividends are accrued income, not upcoming events).
    """
    today = _as_date(as_of) or date.today()
    rows: List[Dict[str, Any]] = []
    for pos in positions:
        symbol = str(pos.get("symbol", ""))
        shares = float(pos.get("quantity", 0) or 0)
        for rec in dividends_by_symbol.get(symbol, []):
            d = _as_date(rec.get("ex_date"))
            if d is None or d < today:
                continue
            amt = float(rec.get("amount", 0.0))
            rows.append({
                "symbol": symbol,
                "ex_date": d.isoformat(),
                "amount": round(amt, 4),
                "shares": shares,
                "est_payment": round(shares * amt, 2),
            })
    rows.sort(key=lambda r: (r["ex_date"], r["symbol"]))
    return rows[: max(0, int(limit))]


def monthly_dividend_income(
    positions: List[Dict[str, Any]],
    dividends_by_symbol: Dict[str, List[Dict[str, Any]]],
    months: int = 12,
    as_of: Any = None,
) -> List[Dict[str, Any]]:
    """Trailing per-month dividend income for the open book.

    Returns ``months`` rows ``{month: "YYYY-MM", income: float}`` ending at the
    *as_of* month (oldest first), summing ``shares * amount`` for every ex-date
    that fell in each month while the position was held.
    """
    end = _as_date(as_of) or date.today()
    # Build the ordered list of trailing "YYYY-MM" buckets ending at `end`.
    buckets: List[str] = []
    y, m = end.year, end.month
    for _ in range(max(1, int(months))):
        buckets.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    buckets.reverse()
    index = {b: 0.0 for b in buckets}
    for pos in positions:
        symbol = str(pos.get("symbol", ""))
        shares = float(pos.get("quantity", 0) or 0)
        entry = pos.get("entry_time") or pos.get("entry_date")
        for rec in dividends_between(dividends_by_symbol.get(symbol, []), entry, end):
            d = _as_date(rec.get("ex_date"))
            if d is None:
                continue
            key = f"{d.year:04d}-{d.month:02d}"
            if key in index:
                index[key] += shares * float(rec.get("amount", 0.0))
    return [{"month": b, "income": round(index[b], 2)} for b in buckets]


# ---------------------------------------------------------------------------
# Ex-dividend gap handling
# ---------------------------------------------------------------------------


def dividend_on_date(dividends: List[Dict[str, Any]], d: Any) -> float:
    """Return the dividend amount with an ex-date on *d* (0.0 if none)."""
    target = _as_date(d)
    if target is None:
        return 0.0
    for rec in dividends:
        if _as_date(rec.get("ex_date")) == target:
            return float(rec.get("amount", 0.0))
    return 0.0


def recent_dividend(
    dividends: List[Dict[str, Any]], as_of: Any = None, window_days: int = 3
) -> float:
    """Sum dividends whose ex-date is within *window_days* before *as_of*.

    Used to add back a just-passed dividend when deciding whether a price drop
    is a genuine breakdown or merely the ex-dividend adjustment.
    """
    ref = _as_date(as_of) or date.today()
    total = 0.0
    for rec in dividends:
        d = _as_date(rec.get("ex_date"))
        if d is None:
            continue
        delta = (ref - d).days
        if 0 <= delta <= window_days:
            total += float(rec.get("amount", 0.0))
    return round(total, 6)


def is_dividend_gap(
    prev_close: float,
    current_price: float,
    dividend_amount: float,
    tolerance: float = 0.5,
) -> bool:
    """Return True when a price drop is explained by going ex-dividend.

    The drop ``prev_close - current_price`` counts as a dividend gap when it is
    positive and within ``tolerance`` (fractional) of *dividend_amount* — i.e.
    the stock fell by roughly the dividend, not on fresh bad news.
    """
    if dividend_amount <= 0:
        return False
    drop = float(prev_close) - float(current_price)
    if drop <= 0:
        return False
    lo = dividend_amount * (1.0 - tolerance)
    hi = dividend_amount * (1.0 + tolerance)
    return lo <= drop <= hi


def dividend_adjusted_price(
    current_price: float,
    dividends: List[Dict[str, Any]],
    as_of: Any = None,
    window_days: int = 3,
) -> float:
    """Add back any just-passed dividend to *current_price* for stop comparison.

    A stop / health check should measure the *total-return* price path, so a
    share that just went ex-dividend is compared as if it still carried the
    dividend — preventing the ex-div gap from tripping the stop.
    """
    return round(float(current_price) + recent_dividend(dividends, as_of, window_days), 4)
