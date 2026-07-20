"""
Paper ⇄ live mode switching for the dashboard.

Switching modes must survive a process restart, so the desired broker is
persisted to the project ``.env`` file (the single source of truth read by
:class:`~config.settings.Settings` at start-up).  Because the dashboard and the
trading engine are separate processes, the switch also drops a *restart
sentinel* file that the running engine polls each loop; when it sees the
sentinel it re-execs itself and picks up the new ``.env``.

Guard rails:

* Switching **to live** requires the admin password (``DASHBOARD_ADMIN_PASSWORD``,
  falling back to ``DASHBOARD_PASSWORD``), compared in constant time.  If no
  admin password is configured, a live
  switch is refused outright — real money is never armed without a credential.
* ``ALLOW_MODE_SWITCH=false`` disables switching entirely.
* Switching to paper never needs a password (de-risking is always allowed).
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Tuple

import structlog

from config.settings import EASTERN, Settings

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: The two valid target modes.
PAPER = "paper"
LIVE = "live"

#: Gateway ports: 7496 is the IBKR *live* port, 7497 the paper port.
LIVE_PORT = 7496
PAPER_PORT = 7497

RESTART_SENTINEL = "restart.flag"


@dataclass
class SwitchResult:
    """Outcome of a mode-switch attempt."""

    ok: bool
    mode: str
    message: str
    restart_requested: bool = False


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def update_env_var(env_path: Path, updates: Dict[str, str]) -> None:
    """Apply ``KEY=value`` *updates* to *env_path*, preserving other lines.

    Existing keys are rewritten in place; missing keys are appended.  The file
    is created if it does not exist.  Comments and unrelated lines are left
    untouched so no other configuration is disturbed.
    """
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)

    for key, value in remaining.items():
        out.append(f"{key}={value}")

    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    # The .env holds broker + LLM secrets in plaintext (documented trust model,
    # see docs/security.md B-6); keep it owner-read/write only so a stray world
    # or group read can't lift live credentials.
    try:
        os.chmod(env_path, 0o600)
    except OSError:  # pragma: no cover — non-POSIX / permission quirk
        log.debug("mode_control.env_chmod_failed", env_path=str(env_path))


def request_restart(data_dir: Path, target_mode: str) -> None:
    """Drop the restart sentinel the engine polls to re-exec itself."""
    data_dir.mkdir(parents=True, exist_ok=True)
    sentinel = data_dir / RESTART_SENTINEL
    sentinel.write_text(
        f"{target_mode}\n{datetime.now(tz=EASTERN).isoformat()}\n", encoding="utf-8"
    )


RESTART_ACK = "restart_ack.json"


def consume_restart_request(data_dir: Path) -> bool:
    """Return ``True`` (and delete the sentinel) if a restart was requested.

    On consumption an ack file (:data:`RESTART_ACK`) is written so the dashboard
    can confirm the running engine actually picked up the request rather than
    the signal silently no-op-ing (audit B-6).
    """
    sentinel = Path(data_dir) / RESTART_SENTINEL
    if sentinel.exists():
        target = ""
        try:
            target = sentinel.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            pass
        try:
            sentinel.unlink()
        except OSError:
            pass
        try:
            ack = Path(data_dir) / RESTART_ACK
            ack.write_text(
                json.dumps({
                    "target": target,
                    "acked_at": datetime.now(tz=EASTERN).isoformat(),
                }),
                encoding="utf-8",
            )
        except OSError:
            log.debug("mode_control.restart_ack_write_failed")
        return True
    return False


def restart_status(data_dir: str | Path) -> Dict[str, str | bool | None]:
    """Report whether a requested restart is still pending or has been acked.

    ``pending`` is ``True`` while the sentinel is present (the engine has not yet
    re-execed); ``acked_at`` carries the timestamp of the last consumption.  A
    request that stays pending indefinitely means the engine isn't polling —
    exactly the silent config-drift the fire-and-forget signal risked (B-6).
    """
    data_dir = Path(data_dir)
    pending = (data_dir / RESTART_SENTINEL).exists()
    ack: Dict[str, str] = {}
    ack_path = data_dir / RESTART_ACK
    if ack_path.exists():
        try:
            loaded = json.loads(ack_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                ack = loaded
        except (ValueError, OSError):
            ack = {}
    return {
        "pending": pending,
        "acked_at": ack.get("acked_at"),
        "target": ack.get("target"),
    }


def _normalise(target: str) -> str:
    t = target.strip().lower()
    if t in ("paper", "sim", "simulated"):
        return PAPER
    if t in ("live", "real", "ibkr"):
        return LIVE
    raise ValueError(f"unknown target mode: {target!r}")


def switch_mode(
    target: str,
    admin_password: str,
    settings: Settings,
    env_path: Path | None = None,
) -> SwitchResult:
    """Switch the trading mode, persisting to ``.env`` and requesting a restart.

    Args:
        target: ``"paper"`` or ``"live"``.
        admin_password: The admin password (required only for a live switch).
        settings: The current settings (for the password + allow flag).
        env_path: Override the ``.env`` path (tests inject a temp file).

    Returns:
        A :class:`SwitchResult` describing what happened.
    """
    if not settings.ALLOW_MODE_SWITCH:
        return SwitchResult(False, settings.TRADING_MODE, "Mode switching is disabled.")

    try:
        mode = _normalise(target)
    except ValueError as exc:
        return SwitchResult(False, settings.TRADING_MODE, str(exc))

    if mode == LIVE:
        expected = settings.admin_password
        if not expected:
            return SwitchResult(
                False, settings.TRADING_MODE,
                "Cannot switch to live: no admin password configured.",
            )
        if not secrets.compare_digest(
            (admin_password or "").encode("utf-8"), expected.encode("utf-8")
        ):
            log.warning("mode_switch.bad_password")
            return SwitchResult(
                False, settings.TRADING_MODE, "Invalid admin password."
            )
        updates = {"BROKER": "ibkr", "IBKR_PORT": str(LIVE_PORT)}
        new_label = "LIVE"
    else:
        updates = {"BROKER": "paper"}
        new_label = "PAPER"

    env_path = env_path or (_project_root() / ".env")
    old_label = settings.TRADING_MODE
    update_env_var(env_path, updates)
    request_restart(Path(settings.DATA_DIR), mode)

    log.info(
        "mode_switch.applied",
        old_mode=old_label,
        new_mode=new_label,
        env_path=str(env_path),
    )
    return SwitchResult(
        ok=True,
        mode=new_label,
        message=(
            f"Switched to {new_label}. The engine will restart to apply the change."
        ),
        restart_requested=True,
    )


def known_targets() -> Iterable[Tuple[str, str]]:
    """Return ``(value, label)`` pairs for the two switch targets."""
    return (("paper", "Paper (simulated)"), ("live", "Live (real money)"))
