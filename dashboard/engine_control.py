"""
Trading-engine process control + status for the dashboard.

The engine (``engine.py``) runs as its own systemd unit
(``ustradingbot-engine.service``), separate from the dashboard.  This module
lets the dashboard:

* **report** the engine's status — the systemd unit state merged with a
  heartbeat the engine writes to ``data_store/engine_status.json`` each loop
  (current phase, last scan, next scan, open positions);
* **control** the engine (start / stop / restart), guarded by the admin
  password (``DASHBOARD_PASSWORD``, constant-time compared); and
* **tail** recent engine logs from journald.

Everything degrades gracefully off the production host: when systemd or the
unit is absent (e.g. local dev) status falls back to the heartbeat file and
control actions return a clear "unavailable" message instead of raising.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import structlog

from config.settings import Settings

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: The systemd unit that runs the trading engine on the production host.
SERVICE_NAME = "ustradingbot-engine.service"

#: Heartbeat file the engine refreshes each loop, read by the dashboard.
STATUS_FILE = "engine_status.json"

#: Control actions the dashboard may request.
VALID_ACTIONS = ("start", "stop", "restart")


# ---------------------------------------------------------------------------
# Heartbeat — written by the engine, read by the dashboard
# ---------------------------------------------------------------------------


def status_path(data_dir: str | Path) -> Path:
    """Return the path to the heartbeat file under *data_dir*."""
    return Path(data_dir) / STATUS_FILE


def write_heartbeat(data_dir: str | Path, **fields: Any) -> None:
    """Persist the engine's live state to the heartbeat file (best-effort).

    Called by the engine at key points in its loop so the dashboard can show
    activity without parsing logs.  A ``updated_at`` UTC stamp is always added.
    The write is atomic (temp file + replace) and never raises — telemetry must
    never disrupt the trading loop.
    """
    try:
        path = status_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(fields)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001 -- heartbeat must never break the engine
        log.debug("engine_control.heartbeat_write_failed", exc_info=True)


def read_heartbeat(data_dir: str | Path) -> Dict[str, Any]:
    """Return the parsed heartbeat, or ``{}`` when missing/unreadable."""
    path = status_path(data_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _heartbeat_fresh(hb: Dict[str, Any], settings: Settings) -> bool:
    """Return whether the heartbeat was updated recently enough to trust.

    The engine only refreshes the heartbeat once per loop, so the allowance is
    scan-interval aware: during a long between-scan sleep the heartbeat is still
    considered fresh (it is not stale, the engine is just waiting).
    """
    ts = hb.get("updated_at")
    if not ts:
        return False
    try:
        updated = datetime.fromisoformat(str(ts))
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    interval = hb.get("scan_interval_min") or settings.SCAN_INTERVAL_MINUTES or 60
    allowance = (float(interval) + 2.0) * 60.0
    age = (datetime.now(timezone.utc) - updated).total_seconds()
    return age <= allowance


# ---------------------------------------------------------------------------
# systemd interaction
# ---------------------------------------------------------------------------


def _systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def _run(cmd: List[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    """Run *cmd* capturing output, never raising on non-zero exit."""
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def _service_state() -> Dict[str, Any]:
    """Return the systemd unit state, or ``{'available': False}`` off-host."""
    if not _systemctl_available():
        return {"available": False}
    try:
        proc = _run(
            [
                "systemctl", "show", SERVICE_NAME,
                "--property=ActiveState,SubState,MainPID,"
                "ExecMainStartTimestamp,LoadState",
            ]
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False}

    props: Dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key] = value

    if props.get("LoadState", "not-found") in ("not-found", ""):
        return {"available": True, "loaded": False}

    return {
        "available": True,
        "loaded": True,
        "active_state": props.get("ActiveState", "unknown"),
        "sub_state": props.get("SubState", ""),
        "main_pid": props.get("MainPID", "0"),
        "since": props.get("ExecMainStartTimestamp", ""),
    }


def engine_status(settings: Settings) -> Dict[str, Any]:
    """Return the combined engine status: systemd state + activity heartbeat."""
    svc = _service_state()
    hb = read_heartbeat(settings.DATA_DIR)
    managed = bool(svc.get("available") and svc.get("loaded"))

    if managed:
        active = svc.get("active_state")
        if active == "active":
            state = "running"
        elif active == "failed":
            state = "error"
        else:
            state = "stopped"
    elif svc.get("available"):
        # systemd present but the unit is not installed on this host.
        state = "unknown"
    else:
        # No systemd (local dev) — infer from heartbeat freshness.
        state = "running" if _heartbeat_fresh(hb, settings) else "stopped"

    return {
        "state": state,
        "controllable": managed,
        "service": {
            "name": SERVICE_NAME,
            "managed": managed,
            "active_state": svc.get("active_state"),
            "sub_state": svc.get("sub_state"),
            "main_pid": svc.get("main_pid"),
            "since": svc.get("since"),
        },
        "activity": {
            "phase": hb.get("phase"),
            "market_open": hb.get("market_open"),
            "last_cycle_at": hb.get("last_cycle_at"),
            "next_scan_at": hb.get("next_scan_at"),
            "scan_interval_min": hb.get("scan_interval_min"),
            "open_positions": hb.get("open_positions"),
            "updated_at": hb.get("updated_at"),
            "stale": bool(hb) and not _heartbeat_fresh(hb, settings),
        },
    }


def control_engine(
    action: str, admin_password: str, settings: Settings
) -> Dict[str, Any]:
    """Start / stop / restart the engine service, gated by the admin password.

    Returns ``{"ok": bool, "message": str}``.  Refuses the action when no admin
    password is configured, the password is wrong, systemd is unavailable, or
    ``systemctl`` reports failure.
    """
    action = (action or "").strip().lower()
    if action not in VALID_ACTIONS:
        return {"ok": False, "message": f"Unknown action: {action!r}."}

    expected = settings.DASHBOARD_PASSWORD or ""
    if not expected:
        return {
            "ok": False,
            "message": "Engine control disabled: no admin password configured.",
        }
    if not secrets.compare_digest(
        (admin_password or "").encode("utf-8"), expected.encode("utf-8")
    ):
        log.warning("engine_control.bad_password", action=action)
        return {"ok": False, "message": "Invalid admin password."}

    if not _systemctl_available():
        return {
            "ok": False,
            "message": "Engine control unavailable: systemd is not present "
            "on this host.",
        }

    try:
        proc = _run(["systemctl", action, SERVICE_NAME], timeout=45.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "message": f"systemctl {action} failed: {exc}"}

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        log.warning(
            "engine_control.action_failed", action=action,
            rc=proc.returncode, detail=detail,
        )
        return {"ok": False, "message": f"systemctl {action} failed: {detail}"}

    log.info("engine_control.action", action=action)
    return {"ok": True, "message": f"Engine {action} requested successfully."}


def _format_log_line(raw: str) -> str:
    """Render a structlog JSON log line as a compact human-readable string."""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if not isinstance(obj, dict) or "event" not in obj:
        return raw
    ts = str(obj.get("timestamp", ""))
    tshort = ts[11:19] if len(ts) >= 19 else ts
    level = str(obj.get("level", "")).upper()
    event = obj.get("event", "")
    extras = " ".join(
        f"{k}={v}"
        for k, v in obj.items()
        if k not in ("timestamp", "level", "event", "logger")
    )
    return f"{tshort} {level:<7} {event} {extras}".rstrip()


def engine_logs(lines: int = 200) -> Dict[str, Any]:
    """Return the last *lines* of engine logs from journald (human-formatted)."""
    lines = max(1, min(int(lines or 200), 1000))
    if shutil.which("journalctl") is None:
        return {
            "available": False,
            "lines": [],
            "message": "journalctl is not available on this host.",
        }
    try:
        proc = _run(
            ["journalctl", "-u", SERVICE_NAME, "-n", str(lines),
             "--no-pager", "-o", "cat"],
            timeout=20.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "lines": [], "message": f"log read failed: {exc}"}

    formatted = [
        _format_log_line(ln) for ln in proc.stdout.splitlines() if ln.strip()
    ]
    return {"available": True, "lines": formatted, "message": ""}
