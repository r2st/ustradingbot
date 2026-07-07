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

#: Strategy keys the engine understands (mirrors signals.screener).
VALID_STRATEGIES = (
    "vcp_breakout",
    "momentum",
    "swing",
    "mean_reversion",
    "pead",
)

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


def _normalize(payload: Dict[str, Any]) -> TradeSelection:
    """Validate and coerce a raw payload into a :class:`TradeSelection`.

    Raises:
        TradeSelectionError: on invalid strategies, grades, or symbols.
    """
    from config.watchlist import WatchlistError, normalize_symbol

    enabled = bool(payload.get("enabled", False))

    raw_symbols = payload.get("symbols", []) or []
    if not isinstance(raw_symbols, list):
        raise TradeSelectionError("symbols must be a list.")
    symbols: List[str] = []
    seen: set[str] = set()
    for s in raw_symbols:
        try:
            sym = normalize_symbol(s)
        except WatchlistError as exc:
            raise TradeSelectionError(str(exc)) from exc
        if sym not in seen:
            seen.add(sym)
            symbols.append(sym)

    raw_strategies = payload.get("strategies", []) or []
    if not isinstance(raw_strategies, list):
        raise TradeSelectionError("strategies must be a list.")
    strategies: List[str] = []
    for s in raw_strategies:
        name = str(s).strip().lower()
        if name not in VALID_STRATEGIES:
            raise TradeSelectionError(f"Unknown strategy: {s!r}")
        if name not in strategies:
            strategies.append(name)

    min_grade = str(payload.get("min_grade", "B")).strip().upper() or "B"
    if min_grade not in VALID_MIN_GRADES:
        raise TradeSelectionError(
            f"min_grade must be one of {', '.join(VALID_MIN_GRADES)}."
        )

    return TradeSelection(
        enabled=enabled,
        symbols=symbols,
        strategies=strategies,
        min_grade=min_grade,
        updated_at=datetime.now().isoformat(timespec="seconds"),
    )


_LOCK = RLock()


def load_trade_selection(data_dir: str | Path) -> TradeSelection:
    """Read the persisted selection; a missing/corrupt file means disabled.

    Never raises — the engine must keep trading its defaults if the file is
    bad, so any read problem degrades to ``enabled=False``.
    """
    path = Path(data_dir) / _FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return TradeSelection()
        sel = _normalize(raw)
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
