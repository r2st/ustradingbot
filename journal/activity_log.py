"""
Engine activity event stream (monitoring feature 4) + last-scan snapshot (F8).

The engine appends one JSON line per event to ``DATA_DIR/engine_activity.jsonl``
so the dashboard can show a structured, human-readable feed of what happened
each cycle (scan started, signals found, per-gate rejections, trades placed,
exits, cycle summary) without scraping journald.

Design rules (matching the heartbeat pattern in ``dashboard.engine_control``):

* **Best-effort writes** — :meth:`ActivityLogger.log` never raises; a
  telemetry failure must never break the trading loop.
* **Size-capped** — the JSONL rotates at ~10 MB (one ``.1`` backup kept), so
  the file never grows unbounded.

The module also owns ``DATA_DIR/last_scan.json`` — the latest scan's signal
list, persisted for the watchlist monitor (feature 8).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

log = structlog.get_logger(__name__)

ACTIVITY_FILE = "engine_activity.jsonl"
LAST_SCAN_FILE = "last_scan.json"

#: Rotate the activity JSONL when it exceeds this size (~10 MB).
MAX_ACTIVITY_BYTES = 10 * 1024 * 1024


class ActivityLogger:
    """Append-only, size-capped JSONL writer for engine activity events.

    Every public method is best-effort: exceptions are swallowed (and logged
    at debug level) so instrumentation can be sprinkled through the engine
    without any risk to the trading loop.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._path = Path(data_dir) / ACTIVITY_FILE
        self._log = log.bind(component="ActivityLogger")

    @property
    def path(self) -> Path:
        return self._path

    def log(self, event: str, **data: Any) -> None:
        """Append one event line. Never raises."""
        try:
            record: Dict[str, Any] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": str(event),
            }
            record.update(data)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._maybe_rotate()
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception:  # noqa: BLE001 -- telemetry must never break trading
            self._log.debug("activity.write_failed", activity_event=event,
                            exc_info=True)

    def _maybe_rotate(self) -> None:
        try:
            if self._path.exists() and self._path.stat().st_size >= MAX_ACTIVITY_BYTES:
                backup = self._path.with_suffix(self._path.suffix + ".1")
                os.replace(self._path, backup)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Reading (dashboard side)
# ---------------------------------------------------------------------------


def read_activity(
    data_dir: str | Path,
    limit: int = 200,
    since_ts: Optional[str] = None,
    event: Optional[str] = None,
    symbol: Optional[str] = None,
    cycle_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return activity events newest-first, with optional filters.

    Malformed lines are skipped (a partially written trailing line must never
    error the feed).  ``since_ts`` returns only events strictly newer than the
    given ISO timestamp, enabling incremental polling.
    """
    path = Path(data_dir) / ACTIVITY_FILE
    records: List[Dict[str, Any]] = []
    for p in (path.with_suffix(path.suffix + ".1"), path):
        if not p.exists():
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict):
                        records.append(rec)
        except OSError:
            continue

    if since_ts:
        records = [r for r in records if str(r.get("ts", "")) > since_ts]
    if event:
        records = [r for r in records if r.get("event") == event]
    if symbol:
        sym = symbol.upper()
        records = [r for r in records if str(r.get("symbol", "")).upper() == sym]
    if cycle_id:
        records = [r for r in records if str(r.get("cycle_id", "")) == str(cycle_id)]

    records.reverse()  # newest first
    return records[: max(1, int(limit))]


def cycles_summary(data_dir: str | Path, limit: int = 20) -> List[Dict[str, Any]]:
    """Roll the event stream up into one row per cycle, newest first."""
    events = read_activity(data_dir, limit=100_000)
    cycles: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for rec in reversed(events):  # oldest first for aggregation
        cid = str(rec.get("cycle_id", "") or "")
        if not cid:
            continue
        if cid not in cycles:
            cycles[cid] = {
                "cycle_id": cid,
                "started_at": rec.get("ts"),
                "elapsed_s": None,
                "signals_found": 0,
                "rejected_by_gate": {},
                "trades_placed": 0,
                "exits": 0,
                "errors": 0,
            }
            order.append(cid)
        c = cycles[cid]
        ev = rec.get("event")
        if ev == "cycle_start":
            c["started_at"] = rec.get("ts")
        elif ev == "scan_complete":
            c["signals_found"] = int(rec.get("signals_found", 0) or 0)
        elif ev == "signal_rejected":
            gate = str(rec.get("gate", "unknown"))
            c["rejected_by_gate"][gate] = c["rejected_by_gate"].get(gate, 0) + 1
        elif ev in ("trade_placed", "entry_filled"):
            c["trades_placed"] += 1
        elif ev == "exit":
            c["exits"] += 1
        elif ev == "error":
            c["errors"] += 1
        elif ev == "cycle_complete":
            c["elapsed_s"] = rec.get("elapsed_seconds")
            # Trust the engine's own totals when present.
            if rec.get("signals_found") is not None:
                c["signals_found"] = int(rec.get("signals_found", 0) or 0)
    rows = [cycles[cid] for cid in reversed(order)]
    return rows[: max(1, int(limit))]


# ---------------------------------------------------------------------------
# Last-scan snapshot (feature 8)
# ---------------------------------------------------------------------------


def write_last_scan(
    data_dir: str | Path,
    cycle_id: str,
    signals: List[Any],
) -> None:
    """Persist the latest scan's signal list for the watchlist monitor.

    *signals* are :class:`~signals.signal_types.Signal` objects (or dicts with
    the same fields).  Best-effort; never raises.
    """
    def _field(s: Any, obj_attr: str, dict_key: str, default: Any = None) -> Any:
        if isinstance(s, dict):
            return s.get(dict_key, default)
        return getattr(s, obj_attr, default)

    def _num(value: Any) -> Optional[float]:
        try:
            return round(float(value), 4)
        except (TypeError, ValueError):
            return None

    try:
        rows: List[Dict[str, Any]] = []
        for s in signals:
            grade = _field(s, "grade", "grade")
            rows.append({
                "symbol": _field(s, "symbol", "symbol"),
                "strategy": _field(s, "strategy", "strategy"),
                "grade": getattr(grade, "value", grade),
                "signal_strength": _num(
                    _field(s, "signal_strength", "signal_strength", 0.0)
                ) or 0.0,
                "entry": _num(_field(s, "entry_price", "entry")),
                "stop": _num(_field(s, "stop_price", "stop")),
                "target": _num(_field(s, "target_price", "target")),
            })
        payload = {
            "cycle_id": cycle_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "signals": rows,
        }
        path = Path(data_dir) / LAST_SCAN_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001 -- best-effort telemetry
        log.debug("last_scan.write_failed", exc_info=True)


def read_last_scan(data_dir: str | Path) -> Dict[str, Any]:
    """Return the persisted last-scan snapshot, or ``{}`` when absent."""
    path = Path(data_dir) / LAST_SCAN_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}
