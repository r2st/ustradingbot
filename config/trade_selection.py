"""
User-controlled trade selection (feature 1).

Lets the operator constrain what the engine trades — which symbols, which
strategies, and the minimum setup grade — from the dashboard, typically after
reviewing backtest results.  The selection persists to
``DATA_DIR/trade_selection.json`` so the dashboard (writer) and the engine
(reader, separate process) share it through the filesystem, exactly like the
watchlist store.

Semantics:

* ``enabled = false`` (the default) — the engine behaves exactly as before:
  it scans the full watchlist, runs every strategy, and uses the built-in
  minimum grade.
* ``enabled = true`` — the engine only scans the selected symbols (empty =
  all watchlist symbols), only takes signals from the selected strategies
  (empty = all), and only trades setups at or above ``min_grade``.

The on-disk format::

    {
      "enabled": true,
      "symbols": ["NVDA", "AAPL"],
      "strategies": ["vcp_breakout", "momentum"],
      "min_grade": "A",
      "updated_at": "2026-07-06T09:00:00"
    }
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List

from config.settings import EASTERN

#: Long strategy keys the engine understands (mirrors signals.screener).
LONG_STRATEGIES = (
    "vcp_breakout",
    "momentum",
    "swing",
    "mean_reversion",
    "pead",
)

#: Short strategy keys (mirrors short_strategies.strategies.STRATEGY_PRIORITY).
SHORT_STRATEGIES = (
    "short_gap_fail",
    "short_earnings_pop_fade",
    "short_support_breakdown",
    "short_bear_flag",
    "short_buying_climax",
    "short_overbought_fade",
    "short_vwap_rejection",
    "short_ma_crossunder",
    "short_relative_weakness",
    "short_laggard_fade",
)

#: Highly selective strategy keys (mirrors selective_strategies.strategies).
SELECTIVE_STRATEGIES = (
    "hs_rsi2_reversal",
    "hs_triple_timeframe",
    "hs_bb_climax",
    "hs_pead_drift",
    "hs_gap_fill",
    "hs_turnaround_tuesday",
)

#: Every strategy key selectable from the dashboard (long + short + selective).
VALID_STRATEGIES = LONG_STRATEGIES + SHORT_STRATEGIES + SELECTIVE_STRATEGIES

#: Grades the engine will accept as a minimum (F would mean "trade anything").
VALID_MIN_GRADES = ("A", "B", "C")

_FILENAME = "trade_selection.json"


class TradeSelectionError(ValueError):
    """Raised when a trade-selection update is invalid."""


@dataclass
class TradeSelection:
    """The resolved trade-selection state the engine consumes."""

    enabled: bool = False
    symbols: List[str] = field(default_factory=list)
    strategies: List[str] = field(default_factory=list)
    min_grade: str = "B"
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "symbols": list(self.symbols),
            "strategies": list(self.strategies),
            "min_grade": self.min_grade,
            "updated_at": self.updated_at,
        }

    # ---------------------------------------------------------- engine hooks

    def filter_symbols(self, scan_symbols: List[str]) -> List[str]:
        """Restrict *scan_symbols* to the selected set (when enabled).

        An empty selection means "no symbol restriction".  Selected symbols
        that are not in the current watchlist are still scanned — an explicit
        pick from backtest results should win over watchlist membership.
        """
        if not self.enabled or not self.symbols:
            return scan_symbols
        selected = set(self.symbols)
        kept = [s for s in scan_symbols if s in selected]
        extra = sorted(selected.difference(scan_symbols))
        return kept + extra

    def allowed_strategies(self) -> List[str] | None:
        """Strategy whitelist for the screener, or ``None`` for no limit."""
        if not self.enabled or not self.strategies:
            return None
        return list(self.strategies)

    def effective_min_grade(self, default: str = "B") -> str:
        """The minimum grade the engine should trade."""
        return self.min_grade if self.enabled else default

    def allows_signal(self, signal: Any) -> tuple[bool, str]:
        """Hard gate: may *signal* be traded under this selection?

        The scan paths already filter on the selection, but this re-check in
        the entry pipeline guarantees that no signal source — long scan, short
        scan, or any future path — can place a trade outside the operator's
        selection (symbols, strategies, minimum grade).

        Returns:
            ``(True, "")`` when allowed, else ``(False, reason)`` with a
            human-readable reason for the rejection log.
        """
        if not self.enabled:
            return True, ""

        strategy = str(getattr(signal, "strategy", "") or "").lower()
        if self.strategies and strategy not in self.strategies:
            return False, (
                f"strategy {strategy!r} is not in the selected strategies "
                f"({', '.join(self.strategies)})"
            )

        symbol = str(getattr(signal, "symbol", "") or "").upper()
        if self.symbols and symbol not in self.symbols:
            return False, (
                f"{symbol} is not in the selected symbols "
                f"({len(self.symbols)} picked)"
            )

        grade = getattr(signal, "grade", None)
        grade_value = str(getattr(grade, "value", grade) or "").upper()
        rank = {"A": 0, "B": 1, "C": 2, "F": 3}
        if rank.get(grade_value, 99) > rank.get(self.min_grade.upper(), 99):
            return False, (
                f"grade {grade_value or '?'} is below the selected minimum "
                f"({self.min_grade})"
            )
        return True, ""


def _normalize(payload: Dict[str, Any], strict: bool = True) -> TradeSelection:
    """Validate and coerce a raw payload into a :class:`TradeSelection`.

    Args:
        payload: Raw dict (from the dashboard form or the persisted file).
        strict: When ``True`` (saves) any invalid entry raises.  When
            ``False`` (engine loads) the selection is preserved fail-CLOSED:
            unknown strategy ids are kept verbatim (they whitelist nothing,
            so the engine trades *less*, never more), unparseable symbols are
            kept upper-cased, and a bad min_grade falls back to ``"B"``.
            The engine must never silently drop an active selection — that
            was the "user selected A, engine traded B" inconsistency.

    Raises:
        TradeSelectionError: in strict mode, on invalid strategies, grades,
            or symbols.
    """
    from config.watchlist import WatchlistError, normalize_symbol

    enabled = bool(payload.get("enabled", False))

    raw_symbols = payload.get("symbols", []) or []
    if not isinstance(raw_symbols, list):
        if strict:
            raise TradeSelectionError("symbols must be a list.")
        raw_symbols = []
    symbols: List[str] = []
    seen: set[str] = set()
    for s in raw_symbols:
        try:
            sym = normalize_symbol(s)
        except WatchlistError as exc:
            if strict:
                raise TradeSelectionError(str(exc)) from exc
            sym = str(s).strip().upper()
            if not sym:
                continue
        if sym not in seen:
            seen.add(sym)
            symbols.append(sym)

    raw_strategies = payload.get("strategies", []) or []
    if not isinstance(raw_strategies, list):
        if strict:
            raise TradeSelectionError("strategies must be a list.")
        raw_strategies = []
    strategies: List[str] = []
    for s in raw_strategies:
        name = str(s).strip().lower()
        if name not in VALID_STRATEGIES:
            if strict:
                raise TradeSelectionError(f"Unknown strategy: {s!r}")
            if not name:
                continue
            # Keep the unknown id: it matches no scanner strategy, so the
            # selection stays restrictive (fail-closed) instead of vanishing.
        if name and name not in strategies:
            strategies.append(name)

    min_grade = str(payload.get("min_grade", "B")).strip().upper() or "B"
    if min_grade not in VALID_MIN_GRADES:
        if strict:
            raise TradeSelectionError(
                f"min_grade must be one of {', '.join(VALID_MIN_GRADES)}."
            )
        min_grade = "B"

    return TradeSelection(
        enabled=enabled,
        symbols=symbols,
        strategies=strategies,
        min_grade=min_grade,
        updated_at=datetime.now(tz=EASTERN).isoformat(timespec="seconds"),
    )


_LOCK = RLock()


def load_trade_selection(data_dir: str | Path) -> TradeSelection:
    """Read the persisted selection; a missing/corrupt file means disabled.

    Never raises — the engine must keep trading its defaults if the file is
    missing or unreadable.  A *readable* file is normalised tolerantly
    (fail-closed: unknown entries restrict, they never widen), so a version
    skew between the dashboard that wrote the file and the engine that reads
    it can never silently deactivate an operator's selection.
    """
    path = Path(data_dir) / _FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return TradeSelection()
        sel = _normalize(raw, strict=False)
        sel.updated_at = str(raw.get("updated_at", "") or "")
        return sel
    except (OSError, json.JSONDecodeError, TradeSelectionError):
        return TradeSelection()


def save_trade_selection(data_dir: str | Path, payload: Dict[str, Any]) -> TradeSelection:
    """Validate *payload* and persist it atomically.

    Returns the normalised :class:`TradeSelection` that was written.

    Raises:
        TradeSelectionError: when the payload is invalid (nothing is written).
    """
    selection = _normalize(payload)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / _FILENAME
    body = json.dumps(selection.to_dict(), indent=2)
    with _LOCK:
        fd, tmp = tempfile.mkstemp(
            dir=str(data_dir), prefix=".trade_sel_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            os.replace(tmp, str(target))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return selection
