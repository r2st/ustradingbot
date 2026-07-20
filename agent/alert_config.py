"""
User-configurable alert rules, dispatch history, and debounce state (F6).

* **Rules** — ``DATA_DIR/alert_rules.json`` maps event types to
  ``{"enabled": bool, "channels": ["telegram", "email", "push"],
  "threshold": float | null}``.  When the file is absent, every event type
  falls back to :data:`DEFAULT_RULES` (which preserves the pre-feature
  behaviour exactly: everything enabled on all channels).
* **History** — every dispatch appends one line to
  ``DATA_DIR/alerts_history.jsonl`` (rotated) so a missed Telegram message is
  reviewable from the dashboard.
* **State** — ``DATA_DIR/alerts_state.json`` holds the per-symbol-per-day
  debounce for proximity alerts so an engine restart does not re-fire them.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from config.settings import EASTERN

log = structlog.get_logger(__name__)

RULES_FILE = "alert_rules.json"
HISTORY_FILE = "alerts_history.jsonl"
STATE_FILE = "alerts_state.json"

MAX_HISTORY_BYTES = 5 * 1024 * 1024

CHANNELS = ("telegram", "email", "push", "slack", "discord", "sms")

#: Event types the SMS channel is allowed to fire on (critical only).  Every
#: other event silently skips SMS even if the rule lists it, so a chatty rule
#: cannot rack up Twilio charges.
SMS_CRITICAL_EVENTS = frozenset(
    {"daily_loss", "broker_disconnect", "drawdown", "engine_error"}
)

EVENT_TYPES = (
    "entry",
    "exit",
    "stop_hit",
    "target_hit",
    "partial_take",
    "approaching_stop",
    "approaching_target",
    "engine_error",
    "broker_disconnect",
    "drawdown",
    "daily_loss",
    "cycle_summary",
    "mode_switch",
)

#: Default rule: enabled on every channel (matches pre-feature behaviour).
def _default_rule() -> Dict[str, Any]:
    return {"enabled": True, "channels": list(CHANNELS), "threshold": None}


DEFAULT_RULES: Dict[str, Dict[str, Any]] = {ev: _default_rule() for ev in EVENT_TYPES}


def load_rules(data_dir: str | Path) -> Dict[str, Dict[str, Any]]:
    """Return the effective rule set (defaults merged under user overrides)."""
    rules = {ev: _default_rule() for ev in EVENT_TYPES}
    path = Path(data_dir) / RULES_FILE
    if not path.exists():
        return rules
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return rules
    if not isinstance(data, dict):
        return rules
    for ev, rule in data.items():
        if ev not in rules or not isinstance(rule, dict):
            continue
        merged = rules[ev]
        if "enabled" in rule:
            merged["enabled"] = bool(rule["enabled"])
        if isinstance(rule.get("channels"), list):
            merged["channels"] = [c for c in rule["channels"] if c in CHANNELS]
        if "threshold" in rule:
            try:
                merged["threshold"] = (
                    None if rule["threshold"] is None else float(rule["threshold"])
                )
            except (TypeError, ValueError):
                pass
    return rules


def save_rules(data_dir: str | Path, rules: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Validate and persist *rules*; returns the effective merged rule set.

    Raises ``ValueError`` on unknown event types or channels.
    """
    if not isinstance(rules, dict):
        raise ValueError("Rules payload must be an object keyed by event type.")
    clean: Dict[str, Dict[str, Any]] = {}
    for ev, rule in rules.items():
        if ev not in EVENT_TYPES:
            raise ValueError(f"Unknown event type: {ev!r}")
        if not isinstance(rule, dict):
            raise ValueError(f"Rule for {ev!r} must be an object.")
        channels = rule.get("channels", list(CHANNELS))
        if not isinstance(channels, list):
            raise ValueError(f"channels for {ev!r} must be a list.")
        for c in channels:
            if c not in CHANNELS:
                raise ValueError(f"Unknown channel {c!r} for {ev!r}.")
        threshold = rule.get("threshold")
        if threshold is not None:
            try:
                threshold = float(threshold)
            except (TypeError, ValueError):
                raise ValueError(f"threshold for {ev!r} must be numeric or null.")
        clean[ev] = {
            "enabled": bool(rule.get("enabled", True)),
            "channels": channels,
            "threshold": threshold,
        }

    path = Path(data_dir) / RULES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    tmp.replace(path)
    return load_rules(data_dir)


# ---------------------------------------------------------------------------
# Dispatch history
# ---------------------------------------------------------------------------


def append_history(
    data_dir: str | Path,
    event_type: str,
    message: str,
    channels: Dict[str, bool],
) -> None:
    """Append one dispatch record (best-effort; never raises).

    *channels* maps channel name → whether the send succeeded (a disabled /
    unconfigured channel simply is not present in the map).
    """
    try:
        path = Path(data_dir) / HISTORY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists() and path.stat().st_size >= MAX_HISTORY_BYTES:
                os.replace(path, path.with_suffix(path.suffix + ".1"))
        except OSError:
            pass
        record = {
            "ts": datetime.now(EASTERN).isoformat(),
            "type": str(event_type),
            "message": str(message)[:500],
            "channels": {str(k): bool(v) for k, v in (channels or {}).items()},
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:  # noqa: BLE001 -- history must never break alerting
        log.debug("alert_history.write_failed", exc_info=True)


def read_history(
    data_dir: str | Path,
    limit: int = 100,
    type_filter: Optional[str] = None,
    since_ts: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return dispatch history newest-first; malformed lines are skipped."""
    path = Path(data_dir) / HISTORY_FILE
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
    if type_filter:
        records = [r for r in records if r.get("type") == type_filter]
    if since_ts:
        records = [r for r in records if str(r.get("ts", "")) > since_ts]
    records.reverse()
    return records[: max(1, int(limit))]


# ---------------------------------------------------------------------------
# Debounce state (proximity alerts fire once per symbol per day)
# ---------------------------------------------------------------------------


def load_state(data_dir: str | Path) -> Dict[str, Any]:
    path = Path(data_dir) / STATE_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(data_dir: str | Path, state: Dict[str, Any]) -> None:
    try:
        path = Path(data_dir) / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        log.debug("alert_state.write_failed", exc_info=True)
