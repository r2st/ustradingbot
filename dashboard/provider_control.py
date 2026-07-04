"""
Market-data provider selection + API-key management for the dashboard.

Lets the user pick between Yahoo Finance, Alpaca, and Polygon.io — and enter the
API keys those providers need — without hand-editing ``.env``.  Like the
paper⇄live toggle it persists to the project ``.env`` (the single source of
truth) and drops the restart sentinel so the running engine re-execs and picks
up the new provider / keys.

Design notes:

* Switching to a provider that needs a key it does not yet have is refused, so
  the bot never silently ends up on a provider that cannot return data.
* When Alpaca credentials are saved and the provider is still on the default
  Yahoo backend, the provider is auto-switched to Alpaca (per product spec:
  "default to Alpaca when keys are present").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from config.settings import Settings
from dashboard.mode_control import request_restart, update_env_var

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ProviderSpec:
    """Static metadata describing one market-data provider."""

    name: str
    label: str
    description: str
    streaming: bool
    key_fields: List[Dict[str, str]] = field(default_factory=list)
    signup_url: str = ""


# Provider catalogue. ``key_fields`` lists the .env vars the provider needs,
# each with a human label and whether it is a secret (masked in the UI).
PROVIDERS: List[ProviderSpec] = [
    ProviderSpec(
        name="yfinance",
        label="Yahoo Finance",
        description="Free daily bars, no API key required. The default backend.",
        streaming=False,
        key_fields=[],
    ),
    ProviderSpec(
        name="alpaca",
        label="Alpaca",
        description="Historical bars + realtime websocket streaming for "
        "low-latency exits. Requires a free Alpaca account.",
        streaming=True,
        key_fields=[
            {"env": "ALPACA_API_KEY", "label": "API Key", "secret": "false"},
            {"env": "ALPACA_API_SECRET", "label": "API Secret", "secret": "true"},
        ],
        signup_url="https://app.alpaca.markets",
    ),
    ProviderSpec(
        name="polygon",
        label="Polygon.io",
        description="US-equity aggregates via the Polygon REST API. Requires a "
        "Polygon.io API key (a free tier is available).",
        streaming=False,
        key_fields=[{"env": "POLYGON_API_KEY", "label": "API Key", "secret": "true"}],
        signup_url="https://polygon.io",
    ),
]

_BY_NAME: Dict[str, ProviderSpec] = {p.name: p for p in PROVIDERS}


@dataclass
class ProviderResult:
    """Outcome of a provider switch or key-save operation."""

    ok: bool
    message: str
    active: str = ""
    restart_requested: bool = False


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def provider_connected(spec: ProviderSpec, settings: Settings) -> bool:
    """Return whether *spec* has everything it needs to fetch data."""
    if spec.name == "alpaca":
        return settings.alpaca_keys_present
    if spec.name == "polygon":
        return settings.polygon_key_present
    return True  # yfinance needs nothing


def provider_status(settings: Settings) -> Dict[str, Any]:
    """Return the full provider picture: active + per-provider status/config."""
    active = str(settings.MARKET_DATA_PROVIDER).lower()
    rows: List[Dict[str, Any]] = []
    for spec in PROVIDERS:
        connected = provider_connected(spec, settings)
        rows.append(
            {
                "name": spec.name,
                "label": spec.label,
                "description": spec.description,
                "streaming": spec.streaming,
                "active": spec.name == active,
                "connected": connected,
                "needs_key": bool(spec.key_fields) and not connected,
                "signup_url": spec.signup_url,
                # Report which key fields are configured (never the values).
                "key_fields": [
                    {
                        "env": kf["env"],
                        "label": kf["label"],
                        "secret": kf.get("secret", "false") == "true",
                        "configured": bool(getattr(settings, kf["env"], "")),
                    }
                    for kf in spec.key_fields
                ],
            }
        )
    active_spec = _BY_NAME.get(active)
    return {
        "active": active,
        "active_label": active_spec.label if active_spec else active,
        "providers": rows,
    }


def switch_provider(
    target: str,
    settings: Settings,
    env_path: Path | None = None,
) -> ProviderResult:
    """Switch the active market-data provider, persisting to ``.env``.

    Refuses to switch to a provider whose required keys are not configured.
    """
    name = str(target).strip().lower()
    spec = _BY_NAME.get(name)
    if spec is None:
        return ProviderResult(False, f"Unknown provider: {target!r}.",
                              active=settings.MARKET_DATA_PROVIDER)

    if not provider_connected(spec, settings):
        return ProviderResult(
            False,
            f"{spec.label} needs its API key(s) before it can be selected.",
            active=settings.MARKET_DATA_PROVIDER,
        )

    env_path = env_path or (_project_root() / ".env")
    update_env_var(env_path, {"MARKET_DATA_PROVIDER": name})
    request_restart(Path(settings.DATA_DIR), f"provider:{name}")
    log.info("provider.switched", provider=name)
    return ProviderResult(
        ok=True,
        message=f"Market data provider set to {spec.label}. "
        "The engine will restart to apply the change.",
        active=name,
        restart_requested=True,
    )


def save_api_keys(
    updates: Dict[str, str],
    settings: Settings,
    env_path: Path | None = None,
    auto_select: bool = True,
) -> ProviderResult:
    """Persist provider API keys to ``.env``.

    Only recognised key fields are written (unknown keys are ignored).  When
    *auto_select* is set and Alpaca credentials become complete while the
    provider is still on the default Yahoo backend, the provider is switched to
    Alpaca automatically (product spec: prefer Alpaca when keys are present).
    """
    valid_envs = {kf["env"] for spec in PROVIDERS for kf in spec.key_fields}
    to_write = {
        k: str(v) for k, v in updates.items() if k in valid_envs and v is not None
    }
    if not to_write:
        return ProviderResult(False, "No recognised API-key fields supplied.",
                              active=settings.MARKET_DATA_PROVIDER)

    env_path = env_path or (_project_root() / ".env")

    # Determine the resulting alpaca-key completeness after this write.
    merged_alpaca_key = to_write.get("ALPACA_API_KEY", settings.ALPACA_API_KEY)
    merged_alpaca_secret = to_write.get("ALPACA_API_SECRET", settings.ALPACA_API_SECRET)
    alpaca_now_complete = bool(merged_alpaca_key and merged_alpaca_secret)

    active = str(settings.MARKET_DATA_PROVIDER).lower()
    switched_to = None
    if (
        auto_select
        and alpaca_now_complete
        and active == "yfinance"
    ):
        to_write["MARKET_DATA_PROVIDER"] = "alpaca"
        switched_to = "alpaca"

    update_env_var(env_path, to_write)
    request_restart(Path(settings.DATA_DIR), "provider_keys")

    saved = [k for k in to_write if k != "MARKET_DATA_PROVIDER"]
    log.info("provider.keys_saved", fields=saved, auto_selected=switched_to)
    msg = f"Saved {len(saved)} key field(s)."
    if switched_to:
        msg += " Alpaca keys detected — provider switched to Alpaca."
    return ProviderResult(
        ok=True,
        message=msg + " The engine will restart to apply the change.",
        active=switched_to or active,
        restart_requested=True,
    )


def resolve_default_provider(settings: Settings) -> Optional[str]:
    """Return the provider that *should* be active by default.

    Prefers Alpaca when its keys are present and the provider is still the
    default Yahoo backend; otherwise ``None`` (no change needed).
    """
    if str(settings.MARKET_DATA_PROVIDER).lower() == "yfinance" and (
        settings.alpaca_keys_present
    ):
        return "alpaca"
    return None
