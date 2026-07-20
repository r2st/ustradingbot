"""
Operational probes for the dashboard: readiness, version, and metric gauges
derived from the engine heartbeat (audit B-3).

``/health`` (liveness) answers "is the process up?"; these helpers answer the
harder operational questions:

* :func:`readiness_report` — "can the bot actually trade *right now*?"  It pings
  the configured market-data provider and broker and reports engine-loop
  liveness from the heartbeat, so an orchestrator pointed here goes red when the
  bot is silently disconnected from the market (rather than green because the
  web process happens to be alive).
* :func:`version_info` — the build/release identity (git SHA + version), read
  from a deploy-time env var or a ``VERSION`` file so a running instance can be
  pinned to a commit.
* :func:`refresh_engine_gauges` — publishes engine liveness / open-position /
  last-cycle-age gauges into :mod:`dashboard.metrics` at scrape time.
"""

from __future__ import annotations

import os
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import structlog

from config.settings import Settings
from dashboard import metrics

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Version / build identity
# ---------------------------------------------------------------------------

_version_cache: Dict[str, Any] | None = None


def _read_git_sha() -> str:
    """Best-effort git SHA: deploy-time env var, VERSION file, or `git`."""
    for env in ("GIT_SHA", "BUILD_SHA", "RELEASE_SHA", "SOURCE_COMMIT"):
        val = os.environ.get(env)
        if val:
            return val.strip()
    version_file = _PROJECT_ROOT / "VERSION"
    if version_file.exists():
        try:
            txt = version_file.read_text(encoding="utf-8").strip()
            if txt:
                return txt
        except OSError:
            pass
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def version_info(app_version: str = "0.1.0") -> Dict[str, Any]:
    """Return the build identity: ``{version, git_sha, git_sha_short, built_at}``.

    Cached after the first call — the SHA does not change within a process.
    """
    global _version_cache
    if _version_cache is None:
        sha = _read_git_sha()
        _version_cache = {
            "version": app_version,
            "git_sha": sha,
            "git_sha_short": sha[:12] if sha != "unknown" else sha,
            "built_at": os.environ.get("BUILD_TIMESTAMP", ""),
        }
    return dict(_version_cache)


# ---------------------------------------------------------------------------
# Readiness checks
# ---------------------------------------------------------------------------


def _check_provider(settings: Settings) -> Dict[str, Any]:
    """The active market-data provider has the credentials it needs to fetch."""
    try:
        from dashboard.provider_control import PROVIDERS, provider_connected

        active = str(settings.MARKET_DATA_PROVIDER).lower()
        spec = next((p for p in PROVIDERS if p.name == active), None)
        if spec is None:
            return {"ok": False, "detail": f"unknown provider {active!r}"}
        connected = provider_connected(spec, settings)
        return {
            "ok": bool(connected),
            "provider": active,
            "detail": "connected" if connected else "missing API key(s)",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"provider check failed: {exc}"}


def _check_broker(settings: Settings) -> Dict[str, Any]:
    """Paper broker is always ready; a live IBKR broker is TCP-pinged."""
    broker = str(getattr(settings, "BROKER", "paper")).lower()
    if broker != "ibkr":
        return {"ok": True, "broker": broker, "detail": "paper backend"}
    host = getattr(settings, "IBKR_HOST", "127.0.0.1")
    port = int(getattr(settings, "IBKR_PORT", 7497) or 7497)
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return {"ok": True, "broker": broker, "detail": f"reachable at {host}:{port}"}
    except OSError as exc:
        return {
            "ok": False,
            "broker": broker,
            "detail": f"unreachable at {host}:{port} ({exc.__class__.__name__})",
        }


def _heartbeat_age_seconds(settings: Settings) -> float | None:
    """Seconds since the engine last refreshed its heartbeat, or ``None``."""
    from dashboard.engine_control import read_heartbeat

    hb = read_heartbeat(settings.DATA_DIR)
    ts = hb.get("updated_at")
    if not ts:
        return None
    try:
        updated = datetime.fromisoformat(str(ts))
    except (ValueError, TypeError):
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated).total_seconds()


def _check_engine(settings: Settings) -> Dict[str, Any]:
    """Engine-loop liveness from the heartbeat freshness.

    A *missing* heartbeat is reported but not treated as a hard failure (the
    engine may simply not be running in a dev/paper setup); a *stale* heartbeat
    — the engine wrote one and then stopped refreshing it — is a hard failure,
    because that is the "engine died silently" case an orchestrator must catch.
    """
    from dashboard.engine_control import _heartbeat_fresh, read_heartbeat

    hb = read_heartbeat(settings.DATA_DIR)
    if not hb.get("updated_at"):
        return {"ok": True, "detail": "no heartbeat (engine not running)",
                "present": False}
    fresh = _heartbeat_fresh(hb, settings)
    age = _heartbeat_age_seconds(settings)
    return {
        "ok": bool(fresh),
        "present": True,
        "last_cycle_age_s": round(age, 1) if age is not None else None,
        "phase": hb.get("phase"),
        "detail": "fresh" if fresh else "stale — engine may have stopped",
    }


def readiness_report(settings: Settings) -> Dict[str, Any]:
    """Assemble the readiness picture; ``ready`` is the AND of all checks."""
    provider = _check_provider(settings)
    broker = _check_broker(settings)
    engine = _check_engine(settings)
    ready = bool(provider["ok"] and broker["ok"] and engine["ok"])
    return {
        "ready": ready,
        "checks": {"provider": provider, "broker": broker, "engine": engine},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Engine-derived metric gauges (published at scrape time)
# ---------------------------------------------------------------------------


def refresh_engine_gauges(settings: Settings) -> None:
    """Publish engine liveness / positions / cycle-age gauges into metrics."""
    try:
        from dashboard.engine_control import _heartbeat_fresh, read_heartbeat

        hb = read_heartbeat(settings.DATA_DIR)
        present = bool(hb.get("updated_at"))
        fresh = _heartbeat_fresh(hb, settings) if present else False
        metrics.set_gauge(
            "ustb_engine_up", 1 if fresh else 0,
            help_text="1 when the engine heartbeat is fresh, else 0",
        )
        age = _heartbeat_age_seconds(settings)
        if age is not None:
            metrics.set_gauge(
                "ustb_engine_last_cycle_age_seconds", age,
                help_text="Seconds since the engine last refreshed its heartbeat",
            )
        open_positions = hb.get("open_positions")
        if isinstance(open_positions, (int, float)):
            metrics.set_gauge(
                "ustb_engine_open_positions", float(open_positions),
                help_text="Open positions reported by the engine heartbeat",
            )
    except Exception:  # noqa: BLE001 — never let a gauge refresh break /metrics
        log.debug("ops.gauge_refresh_failed", exc_info=True)
