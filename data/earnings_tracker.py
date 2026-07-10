"""
Daily earnings tracker (Feature 2).

Answers "who reports today, and how did the market react?" for the watchlist:
which symbols report today (with a BMO/AMC session tag), their pre/post-market
move, beat/miss with EPS surprise %, per-symbol history, and same-sector
*contagion* alerts (e.g. NVDA beats → flag other watchlist semis).

Composition of existing pieces:

* **Today's reporters** — :func:`data.earnings_calendar.upcoming_earnings`
  filtered to ``days_until == 0``.
* **Beat/miss + surprise %** — :func:`data.earnings.get_earnings_result` (F1b).
* **Pre/post-market move** — :func:`data.extended_hours.overnight_gap` (F4);
  degrades to ``None`` until extended hours are enabled.
* **Sector map** — :func:`config.universe.get_sector`.
* **History** — an append-only JSONL store (``DATA_DIR/earnings_history.jsonl``)
  keyed by ``(symbol, report_date)``.

Every public function is **fail-open** — a broken feed yields empty results,
never an exception — so the tracker can't take down the dashboard.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, List, Optional, Sequence

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_HISTORY_FILENAME = "earnings_history.jsonl"
_history_lock = RLock()


@dataclass
class DailyEarnings:
    """One symbol reporting today, enriched with result + market reaction."""

    symbol: str
    sector: str
    session: str = "unknown"        # "bmo" | "amc" | "unknown"
    earnings_date: Optional[date] = None
    eps_estimate: Optional[float] = None
    eps_actual: Optional[float] = None
    surprise_pct: Optional[float] = None
    verdict: Optional[str] = None   # "beat" | "miss" | "inline"
    move_pct: Optional[float] = None  # pre/post-market move (fraction)
    move_session: Optional[str] = None  # "pre" | "post" | ...
    factors: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "sector": self.sector,
            "session": self.session,
            "earnings_date": self.earnings_date.isoformat() if self.earnings_date else None,
            "eps_estimate": self.eps_estimate,
            "eps_actual": self.eps_actual,
            "surprise_pct": self.surprise_pct,
            "verdict": self.verdict,
            "move_pct": round(self.move_pct, 4) if self.move_pct is not None else None,
            "move_session": self.move_session,
        }


# ---------------------------------------------------------------------------
# Today's reporters
# ---------------------------------------------------------------------------


def todays_earnings(
    symbols: Sequence[str],
    settings: Any = None,
    *,
    calendar_fetcher: Optional[Callable[[Sequence[str]], List[Any]]] = None,
    result_fetcher: Optional[Callable[[str], Any]] = None,
    ext_fetcher: Optional[Callable[[str], Any]] = None,
    today: Optional[date] = None,
) -> List[DailyEarnings]:
    """Return the watchlist symbols reporting *today*, enriched.

    Args:
        symbols: Watchlist tickers.
        settings: Application settings (for the result/ext-hours lookups).
        calendar_fetcher: ``fetcher(symbols) -> List[EarningsEntry]``; defaults
            to :func:`data.earnings_calendar.upcoming_earnings`.
        result_fetcher: ``fetcher(symbol) -> EarningsResult | None``.
        ext_fetcher: ``fetcher(symbol) -> ExtQuote | None``.
        today: Reference date for tests.
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    today = today or date.today()

    try:
        entries = _resolve_calendar(symbols, calendar_fetcher)
    except Exception as exc:  # noqa: BLE001 -- fail-open
        log.warning("earnings_tracker.calendar_failed", error=str(exc))
        return []

    reporters = [e for e in entries if getattr(e, "days_until", None) == 0]
    out: List[DailyEarnings] = []
    for entry in reporters:
        symbol = str(getattr(entry, "symbol", "") or "").upper()
        de = DailyEarnings(
            symbol=symbol,
            sector=_sector(symbol),
            earnings_date=getattr(entry, "earnings_date", None) or today,
        )
        _enrich_result(de, symbol, settings, result_fetcher)
        _enrich_move(de, symbol, settings, ext_fetcher)
        out.append(de)

    # Beaten/most-surprising first, then alphabetical.
    out.sort(key=lambda d: (-(abs(d.surprise_pct or 0.0)), d.symbol))
    return out


def _resolve_calendar(symbols, calendar_fetcher) -> List[Any]:
    if calendar_fetcher is not None:
        return calendar_fetcher(symbols)
    from data.earnings_calendar import upcoming_earnings

    return upcoming_earnings(symbols)


def _enrich_result(de: DailyEarnings, symbol: str, settings, result_fetcher) -> None:
    try:
        if result_fetcher is not None:
            result = result_fetcher(symbol)
        else:
            from data.earnings import get_earnings_result

            result = get_earnings_result(symbol, settings)
    except Exception:  # noqa: BLE001 -- fail-open
        result = None
    if result is None:
        return
    de.eps_estimate = getattr(result, "eps_estimate", None)
    de.eps_actual = getattr(result, "eps_actual", None)
    de.surprise_pct = getattr(result, "eps_surprise_pct", None)
    de.verdict = getattr(result, "verdict", None)


def _enrich_move(de: DailyEarnings, symbol: str, settings, ext_fetcher) -> None:
    try:
        if ext_fetcher is not None:
            quote = ext_fetcher(symbol)
        else:
            from data.extended_hours import overnight_gap

            quote = overnight_gap(symbol, settings)
    except Exception:  # noqa: BLE001 -- fail-open
        quote = None
    if quote is None:
        return
    de.move_pct = getattr(quote, "gap_pct", None)
    de.move_session = getattr(quote, "session", None)


def _sector(symbol: str) -> str:
    try:
        from config.universe import get_sector

        return get_sector(symbol)
    except Exception:  # noqa: BLE001
        return "Unknown"


# ---------------------------------------------------------------------------
# Sector contagion
# ---------------------------------------------------------------------------


def sector_contagion(
    reported: DailyEarnings,
    watchlist: Sequence[str],
    settings: Any = None,
) -> List[str]:
    """Return same-sector peers to flag when *reported* is a big surprise.

    A bellwether reporting an EPS surprise whose magnitude exceeds
    ``CONTAGION_SURPRISE_THRESHOLD`` (percent) tends to move its whole sector;
    this returns the other watchlist symbols in the same sector so the operator
    can add them to today's watch.  Empty when the surprise is small, the sector
    is unknown, or there are no peers.
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    threshold = float(getattr(settings, "CONTAGION_SURPRISE_THRESHOLD", 5.0))
    surprise = reported.surprise_pct
    if surprise is None or abs(surprise) < threshold:
        return []
    if not reported.sector or reported.sector == "Unknown":
        return []
    peers = [
        s.upper()
        for s in watchlist
        if s.upper() != reported.symbol and _sector(s) == reported.sector
    ]
    return sorted(dict.fromkeys(peers))


# ---------------------------------------------------------------------------
# Per-symbol history (append-only JSONL)
# ---------------------------------------------------------------------------


def _history_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / _HISTORY_FILENAME


def record_result(data_dir: str | Path, entry: DailyEarnings) -> None:
    """Append *entry* to the earnings-history store (best-effort, deduped).

    Keyed by ``(symbol, earnings_date)`` — a repeat report for the same key is
    skipped so re-scans don't duplicate rows.  Never raises.
    """
    try:
        path = _history_path(data_dir)
        key = (entry.symbol, entry.earnings_date.isoformat() if entry.earnings_date else "")
        with _history_lock:
            for row in _read_rows(path):
                if (row.get("symbol"), row.get("earnings_date")) == key:
                    return  # already recorded
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry.to_dict()) + "\n")
    except Exception as exc:  # noqa: BLE001 -- history must never break a scan
        log.warning("earnings_tracker.record_failed", symbol=entry.symbol, error=str(exc))


def symbol_history(data_dir: str | Path, symbol: str) -> List[Dict[str, Any]]:
    """Return recorded results for *symbol*, newest first.  Never raises."""
    symbol = str(symbol or "").upper()
    rows = [r for r in _read_rows(_history_path(data_dir)) if r.get("symbol") == symbol]
    rows.sort(key=lambda r: str(r.get("earnings_date") or ""), reverse=True)
    return rows


def _read_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out
