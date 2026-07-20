"""
Pydantic request models for every mutating dashboard endpoint.

Centralising the request bodies here gives three things the previous
``await request.json()`` pattern could not:

* **Type checking & bounds enforcement** at the edge — a negative quantity or a
  malformed symbol is rejected with a 422 *before* any money-path code runs.
* **Auto-generated OpenAPI schemas** — the (now auth-gated) ``/docs`` page
  documents each endpoint's real body.
* **A single, reviewable place** describing exactly what each endpoint accepts.

Design notes:

* The money-path models (:class:`ManualTradeRequest`, :class:`PositionStopRequest`)
  are strict: required fields and hard bounds, so bad input is a 422.
* Models that wrap an endpoint which historically consumed the *whole* JSON body
  (alert rules, push subscriptions, notification preferences, trade selection,
  backtests, user profiles) use ``extra="allow"`` and are re-serialised with
  :meth:`model_dump`, so previously-accepted free-form payloads keep working
  while the known fields gain validation and documentation.  The downstream
  validators that already return 400 for domain errors are intentionally left
  in place; the models only add structural/type/bounds checks on top.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# Ticker: 1–15 chars, letters/digits with optional ``.`` (e.g. ``SHOP.TO``) and
# ``-``.  Rejects whitespace and other punctuation up-front as a 422.
_SYMBOL_PATTERN = r"^[A-Za-z][A-Za-z0-9.\-]{0,14}$"


# ---------------------------------------------------------------------------
# Money path — strict validation
# ---------------------------------------------------------------------------


class ManualTradeRequest(BaseModel):
    """Body for ``POST /api/manual-trade`` (admin-gated bracket order).

    Scalar type/bounds checks live here (→ 422); the richer cross-field rules
    (stop below entry for a long, ladder consistency, symbol normalisation) stay
    in :func:`execution.manual_trade.validate_params`.
    """

    model_config = ConfigDict(extra="ignore")

    symbol: str = Field(..., pattern=_SYMBOL_PATTERN, description="Ticker, e.g. AAPL")
    side: str = Field("buy", description="'buy' (long) or 'sell' (short)")
    quantity: int = Field(..., gt=0, le=1_000_000, description="Whole shares, > 0")
    entry_price: float = Field(..., gt=0, le=1_000_000)
    stop_price: Optional[float] = Field(None, gt=0)
    target_price: Optional[float] = Field(None, gt=0)
    # Multi-rung exit ladders (each item: {"price"|"percent": ..., "pct": ...}).
    stops: Optional[List[Dict[str, Any]]] = None
    targets: Optional[List[Dict[str, Any]]] = None
    strategy: str = Field("manual", max_length=40)
    order_type: Optional[str] = Field(None, description="'limit' (default) or 'market'")
    admin_password: str = ""


class PositionStopRequest(BaseModel):
    """Body for ``POST /api/positions/stop`` (admin-gated close-at-market)."""

    model_config = ConfigDict(extra="ignore")

    symbol: str = Field(..., pattern=_SYMBOL_PATTERN)
    admin_password: str = ""


# ---------------------------------------------------------------------------
# Engine / mode / provider / backtest control (app.py)
# ---------------------------------------------------------------------------


class ModeSwitchRequest(BaseModel):
    """Body for ``POST /api/mode/switch``."""

    model_config = ConfigDict(extra="ignore")

    target: str = Field(..., description="'paper' or 'live'")
    admin_password: str = ""


class ProviderSelectRequest(BaseModel):
    """Body for ``POST /api/providers/select``."""

    model_config = ConfigDict(extra="ignore")

    provider: str = Field(..., min_length=1)


class ProviderKeysRequest(BaseModel):
    """Body for ``POST /api/providers/keys``.

    Accepts either ``{"keys": {...}}`` or a flat ``{"KEY": "value", ...}`` map;
    ``extra="allow"`` preserves the flat form.
    """

    model_config = ConfigDict(extra="allow")

    keys: Optional[Dict[str, Any]] = None


class EngineControlRequest(BaseModel):
    """Body for ``POST /api/engine/control``."""

    model_config = ConfigDict(extra="ignore")

    action: str = Field(..., description="start | stop | restart")
    admin_password: str = ""


class BacktestRunRequest(BaseModel):
    """Body for ``POST /api/backtest/run`` (validated further downstream)."""

    model_config = ConfigDict(extra="allow")

    symbols: List[str] = Field(default_factory=list)
    strategies: List[str] = Field(default_factory=list)
    start: Optional[str] = None
    end: Optional[str] = None
    min_grade: Optional[str] = None
    starting_capital: Optional[float] = Field(None, gt=0)


# ---------------------------------------------------------------------------
# Trade selection
# ---------------------------------------------------------------------------


class TradeSelectionRequest(BaseModel):
    """Body for ``POST /api/trade-selection`` (admin-gated).

    The domain validation (valid strategies / grades) lives in
    :func:`config.trade_selection.save_trade_selection`; kept as-is.
    """

    model_config = ConfigDict(extra="allow")

    enabled: Optional[bool] = None
    symbols: Optional[List[str]] = None
    strategies: Optional[List[str]] = None
    min_grade: Optional[str] = None
    admin_password: str = ""


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


class NoteRequest(BaseModel):
    """Body for ``POST /api/notes/{trade_id}``.

    ``extra="allow"`` + a ``model_dump(exclude_unset=True)`` in the endpoint
    preserve the ``rating`` sentinel (absent ≠ explicit ``null``) and any
    forward-compatible journal fields; ``tags`` / ``mistake_tags`` type checks
    (400) stay in the endpoint so their existing error contract is unchanged.
    """

    model_config = ConfigDict(extra="allow")

    note: Optional[str] = None
    tags: Optional[List[Any]] = None
    mistake_tags: Optional[List[Any]] = None
    setup_type: Optional[str] = None
    what_worked: Optional[str] = None
    what_went_wrong: Optional[str] = None
    lesson: Optional[str] = None
    rating: Optional[Any] = None


# ---------------------------------------------------------------------------
# Watchlist (config-file backed)
# ---------------------------------------------------------------------------


class CreateListRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""


class ListEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = True


class AddSymbolRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    list: str = ""
    symbol: str = ""


# ---------------------------------------------------------------------------
# Universe (DB backed)
# ---------------------------------------------------------------------------


class UniverseAddWatchlistRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    list_name: str = ""
    tickers: List[str] = Field(default_factory=list)


class UniverseRemoveWatchlistRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tickers: List[str] = Field(default_factory=list)


class UniverseWatchlistEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = True


class UniverseFilterRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    filter_name: str = ""
    filter_value: Optional[Any] = None
    enabled: bool = True


class UniverseSeedRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    skip_enrichment: bool = False


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


class AlertRulesRequest(BaseModel):
    """Body for ``PUT /api/alerts/rules`` — ``{"rules": {...}}`` or a flat map."""

    model_config = ConfigDict(extra="allow")
    rules: Optional[Dict[str, Any]] = None


class AlertTestRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    channel: str = ""


# ---------------------------------------------------------------------------
# Push / notifications
# ---------------------------------------------------------------------------


class PushSubscribeRequest(BaseModel):
    """A browser PushSubscription object (free-form; passed through verbatim)."""

    model_config = ConfigDict(extra="allow")
    endpoint: Optional[str] = None


class PushUnsubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    endpoint: str = ""


class PushTestRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    category: str = "general"


class NotificationsReadRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ids: List[Any] = Field(default_factory=list)


class NotificationsPrefsRequest(BaseModel):
    """``{"preferences": {...}}`` or a flat category→bool map."""

    model_config = ConfigDict(extra="allow")
    preferences: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Users (multi-user)
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    username: str = ""
    password: str = ""
    profile: Optional[Dict[str, Any]] = None


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    username: str = ""
    password: str = ""


class ProfileUpdateRequest(BaseModel):
    """A user profile document (free-form; validated by users.accounts)."""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# REST API v1 key management
# ---------------------------------------------------------------------------


class CreateApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = "api-key"
