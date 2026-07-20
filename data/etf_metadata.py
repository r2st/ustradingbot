"""
ETF metadata — live asset-type detection and fund fundamentals via yfinance.

Two network-backed, TTL-cached, fail-open helpers the rest of the codebase
leans on:

* :func:`detect_quote_type` — the yfinance ``Ticker.info["quoteType"]`` lookup
  (``"ETF"`` vs ``"EQUITY"``) that lets :func:`config.etf_universe.is_etf`
  recognise arbitrary user-added funds (VTI, ARKK, SCHD, …) that aren't in the
  static list.  Cached for ``ETF_DETECT_TTL_MINUTES``.

* :func:`get_etf_info` — an :class:`ETFInfo` snapshot (expense ratio, NAV,
  category, fund family, top-10 holdings, leverage classification) surfaced in
  the analyst card's *ETF Info* section.  Cached for ``ETF_INFO_TTL_HOURS``.

Both are **best-effort**: any yfinance failure (rate limit, delisting, network)
is swallowed and returns ``None`` / an empty snapshot, so no caller ever breaks
because fund metadata was unavailable.  A ``yfinance`` ``Ticker`` factory and a
monotonic ``clock`` are injectable purely so the tests can run offline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Callable, Dict, List, Optional, Tuple

import structlog

from config.etf_classification import (
    REGULAR,
    classify_leverage,
    leverage_label,
)

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# yfinance Ticker factory (injectable for tests)
# ---------------------------------------------------------------------------


def _default_ticker(symbol: str) -> Any:
    """Return a live ``yfinance.Ticker`` (imported lazily so import stays cheap)."""
    import yfinance as yf

    return yf.Ticker(symbol)


#: Module-level factory the tests monkeypatch to avoid hitting the network.
_ticker_factory: Callable[[str], Any] = _default_ticker


def set_ticker_factory(factory: Callable[[str], Any]) -> None:
    """Override the ``yfinance.Ticker`` factory (test seam)."""
    global _ticker_factory
    _ticker_factory = factory


# ---------------------------------------------------------------------------
# ETF info snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ETFInfo:
    """Immutable fund-fundamentals snapshot for an ETF.

    Every field is optional — the snapshot is populated best-effort from
    whatever ``yfinance`` exposes, and missing data leaves a ``None`` / empty
    value rather than failing.
    """

    symbol: str
    expense_ratio: Optional[float] = None   # decimal, e.g. 0.0009 == 0.09 %
    nav_price: Optional[float] = None
    category: Optional[str] = None
    fund_family: Optional[str] = None
    top_holdings: List[Dict[str, Any]] = field(default_factory=list)
    leverage: str = REGULAR                 # config.etf_classification category
    leverage_label: str = ""

    @property
    def expense_ratio_pct(self) -> Optional[float]:
        """Expense ratio expressed as a percentage (0.09 for a 0.09 % fund)."""
        if self.expense_ratio is None:
            return None
        return round(self.expense_ratio * 100.0, 4)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable dict for the dashboard API / analyst card."""
        return {
            "symbol": self.symbol,
            "expense_ratio": self.expense_ratio,
            "expense_ratio_pct": self.expense_ratio_pct,
            "nav_price": self.nav_price,
            "category": self.category,
            "fund_family": self.fund_family,
            "top_holdings": list(self.top_holdings),
            "leverage": self.leverage,
            "leverage_label": self.leverage_label,
            "is_leveraged": self.leverage != REGULAR,
        }


# ---------------------------------------------------------------------------
# TTL caches
# ---------------------------------------------------------------------------

# quoteType cache: symbol -> (stored_at, quote_type|None)
_qt_cache: Dict[str, Tuple[float, Optional[str]]] = {}
_qt_lock = RLock()

# ETF-info cache: symbol -> (stored_at, ETFInfo|None)
_info_cache: Dict[str, Tuple[float, Optional[ETFInfo]]] = {}
_info_lock = RLock()


def clear_cache() -> None:
    """Drop all cached quoteType / ETFInfo entries (used by tests)."""
    with _qt_lock:
        _qt_cache.clear()
    with _info_lock:
        _info_cache.clear()


def _get_settings(settings: Any) -> Any:
    if settings is not None:
        return settings
    from config.settings import get_settings

    return get_settings()


# ---------------------------------------------------------------------------
# quoteType detection (Gap 1)
# ---------------------------------------------------------------------------


def detect_quote_type(
    symbol: str,
    settings: Any = None,
    clock: Callable[[], float] = time.monotonic,
) -> Optional[str]:
    """Return the yfinance ``quoteType`` for *symbol* (``"ETF"`` / ``"EQUITY"``).

    TTL-cached for ``ETF_DETECT_TTL_MINUTES`` and fail-open: any lookup error
    (or the feature being disabled) returns ``None``.  The result is upper-cased
    so callers can compare against ``"ETF"`` directly.

    Args:
        symbol: Ticker to look up.
        settings: Optional settings override (defaults to the process settings).
        clock: Injectable monotonic clock for cache-expiry tests.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return None

    s = _get_settings(settings)
    if not bool(getattr(s, "ETF_DYNAMIC_DETECTION", True)):
        return None

    ttl = float(getattr(s, "ETF_DETECT_TTL_MINUTES", 720.0)) * 60.0
    now = clock()
    with _qt_lock:
        entry = _qt_cache.get(sym)
        if entry is not None and now - entry[0] <= ttl:
            return entry[1]

    quote_type: Optional[str] = None
    try:
        info = _ticker_factory(sym).info or {}
        raw = info.get("quoteType")
        if raw:
            quote_type = str(raw).upper()
    except Exception as exc:  # noqa: BLE001 — fail-open
        log.debug("etf_metadata.quote_type_failed", symbol=sym, error=str(exc))
        quote_type = None

    with _qt_lock:
        _qt_cache[sym] = (now, quote_type)
    return quote_type


def is_etf_via_yfinance(
    symbol: str,
    settings: Any = None,
    clock: Callable[[], float] = time.monotonic,
) -> Optional[bool]:
    """Return ``True``/``False`` from the live quoteType, or ``None`` if unknown.

    ``None`` (lookup failed or disabled) lets :func:`config.etf_universe.is_etf`
    fall back to the static list rather than guessing.
    """
    qt = detect_quote_type(symbol, settings=settings, clock=clock)
    if qt is None:
        return None
    return qt == "ETF"


# ---------------------------------------------------------------------------
# Fund fundamentals (Gap 2)
# ---------------------------------------------------------------------------


def _extract_expense_ratio(info: Dict[str, Any]) -> Optional[float]:
    """Pull an expense ratio (as a decimal) from a yfinance info dict.

    yfinance exposes the figure under several keys depending on the fund and
    library version; try them in order of preference.  Values already look like
    decimals (0.0009), so they are returned as-is.
    """
    for key in ("annualReportExpenseRatio", "netExpenseRatio", "expenseRatio"):
        val = info.get(key)
        try:
            if val is not None:
                f = float(val)
                # Some feeds report percent (0.09) rather than decimal (0.0009);
                # normalise anything clearly >1 %-as-decimal down to a fraction.
                return f / 100.0 if f > 1.0 else f
        except (TypeError, ValueError):
            continue
    return None


def _extract_holdings(ticker: Any) -> List[Dict[str, Any]]:
    """Return up to the top-10 holdings for an ETF (best-effort, fail-open).

    Prefers the modern ``funds_data.top_holdings`` API (a DataFrame indexed by
    symbol with ``Name`` / ``Holding Percent`` columns) and falls back to the
    legacy ``get_institutional_holders()`` shape.  Any error yields an empty
    list.
    """
    # Modern path: Ticker.funds_data.top_holdings
    try:
        fd = getattr(ticker, "funds_data", None)
        top = getattr(fd, "top_holdings", None) if fd is not None else None
        rows = _holdings_from_frame(top)
        if rows:
            return rows[:10]
    except Exception as exc:  # noqa: BLE001
        log.debug("etf_metadata.top_holdings_failed", error=str(exc))

    # Legacy path: Ticker.get_institutional_holders()
    try:
        getter = getattr(ticker, "get_institutional_holders", None)
        frame = getter() if callable(getter) else None
        rows = _holdings_from_frame(frame)
        if rows:
            return rows[:10]
    except Exception as exc:  # noqa: BLE001
        log.debug("etf_metadata.institutional_holders_failed", error=str(exc))

    return []


def _holdings_from_frame(frame: Any) -> List[Dict[str, Any]]:
    """Normalise a holdings DataFrame (or list-of-dicts) into plain dicts.

    Accepts either a pandas DataFrame (from ``top_holdings`` /
    ``get_institutional_holders``) or an already-plain iterable of dicts (used
    by the tests), and returns ``[{"symbol", "name", "pct"}, ...]``.
    """
    if frame is None:
        return []

    # Already a list/iterable of dicts (test-friendly path).
    if isinstance(frame, (list, tuple)):
        out: List[Dict[str, Any]] = []
        for row in frame:
            if isinstance(row, dict):
                out.append(
                    {
                        "symbol": str(row.get("symbol") or row.get("Symbol") or ""),
                        "name": str(row.get("name") or row.get("Name") or ""),
                        "pct": _as_pct(
                            row.get("pct")
                            if row.get("pct") is not None
                            else row.get("Holding Percent")
                        ),
                    }
                )
        return out

    # pandas DataFrame path (duck-typed so pandas need not be imported here).
    try:
        if hasattr(frame, "empty") and frame.empty:
            return []
        out2: List[Dict[str, Any]] = []
        cols = {str(c).lower(): c for c in getattr(frame, "columns", [])}
        name_col = cols.get("name") or cols.get("holding")
        pct_col = cols.get("holding percent") or cols.get("% out") or cols.get("pctheld")
        for idx, row in frame.iterrows():
            symbol = str(idx)
            name = str(row[name_col]) if name_col is not None else ""
            pct = _as_pct(row[pct_col]) if pct_col is not None else None
            out2.append({"symbol": symbol, "name": name, "pct": pct})
        return out2
    except Exception as exc:  # noqa: BLE001
        log.debug("etf_metadata.holdings_frame_parse_failed", error=str(exc))
        return []


def _as_pct(val: Any) -> Optional[float]:
    """Coerce a holding weight to a percentage float (0-100), or ``None``."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    # yfinance reports weights as fractions (0.071); present as percent.
    return round(f * 100.0, 4) if f <= 1.0 else round(f, 4)


def get_etf_info(
    symbol: str,
    settings: Any = None,
    clock: Callable[[], float] = time.monotonic,
) -> Optional[ETFInfo]:
    """Return a TTL-cached :class:`ETFInfo` for *symbol*, or ``None`` (fail-open).

    Cached for ``ETF_INFO_TTL_HOURS``.  Returns ``None`` when yfinance yields no
    usable data at all, so callers can cleanly hide the *ETF Info* section.

    Args:
        symbol: ETF ticker.
        settings: Optional settings override.
        clock: Injectable monotonic clock for cache-expiry tests.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return None

    s = _get_settings(settings)
    ttl = float(getattr(s, "ETF_INFO_TTL_HOURS", 24.0)) * 3600.0
    now = clock()
    with _info_lock:
        entry = _info_cache.get(sym)
        if entry is not None and now - entry[0] <= ttl:
            return entry[1]

    info_obj = _build_etf_info(sym)

    with _info_lock:
        _info_cache[sym] = (now, info_obj)
    return info_obj


def _build_etf_info(sym: str) -> Optional[ETFInfo]:
    """Fetch and assemble an :class:`ETFInfo` (no caching); ``None`` on total failure."""
    try:
        ticker = _ticker_factory(sym)
        info = ticker.info or {}
    except Exception as exc:  # noqa: BLE001 — fail-open
        log.debug("etf_metadata.info_fetch_failed", symbol=sym, error=str(exc))
        return None

    expense = _extract_expense_ratio(info)
    nav = None
    for key in ("navPrice", "nav"):
        try:
            if info.get(key) is not None:
                nav = float(info[key])
                break
        except (TypeError, ValueError):
            continue
    category = info.get("category") or None
    family = info.get("fundFamily") or None
    holdings = _extract_holdings(ticker)
    leverage = classify_leverage(sym, info)

    # If we learned literally nothing, don't manufacture an empty card.
    if (
        expense is None
        and nav is None
        and not category
        and not family
        and not holdings
        and leverage == REGULAR
    ):
        return None

    return ETFInfo(
        symbol=sym,
        expense_ratio=expense,
        nav_price=nav,
        category=category,
        fund_family=family,
        top_holdings=holdings,
        leverage=leverage,
        leverage_label=leverage_label(leverage),
    )
