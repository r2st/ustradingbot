"""
User-defined price alerts (feature P2f).

Lets a user set arbitrary price-cross alerts ("notify when AAPL crosses $200")
that are checked against live prices and delivered through the existing
notification system.

* Rules persist to ``DATA_DIR/price_alerts.json`` with atomic writes and input
  validation (mirroring :mod:`config.watchlist`).
* :func:`check_price_alerts` fetches current prices for every armed rule, fires
  a push notification when a rule's threshold is crossed, and marks the rule
  triggered so it never re-fires until the user re-arms it.

The checker is safe to call from the engine's scan cycle (or a manual
``POST /api/price-alerts/check``): it never raises and degrades gracefully when
a quote is unavailable.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, List, Optional

import structlog

from config.settings import EASTERN

log = structlog.get_logger(__name__)

_FILENAME = "price_alerts.json"
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,6}(\.[A-Z]{1,3})?$")
_DIRECTIONS = ("above", "below")
_MAX_NOTE = 200


class PriceAlertError(ValueError):
    """Raised when a price-alert operation gets invalid input."""


def _now_iso() -> str:
    return datetime.now(tz=EASTERN).isoformat(timespec="seconds")


def _normalize_symbol(symbol: str) -> str:
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise PriceAlertError("Symbol must not be empty.")
    if not _SYMBOL_RE.match(sym):
        raise PriceAlertError(f"Invalid symbol: {symbol!r}")
    return sym


class PriceAlertStore:
    """Thread-safe JSON-backed store of price-alert rules."""

    def __init__(self, data_dir: str | Path) -> None:
        self._dir = Path(data_dir)
        self._path = self._dir / _FILENAME
        self._lock = RLock()

    # -- persistence -------------------------------------------------------
    def _load(self) -> List[Dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("price_alerts.load_failed", error=str(exc))
            return []
        alerts = data.get("alerts") if isinstance(data, dict) else data
        return list(alerts) if isinstance(alerts, list) else []

    def _save(self, alerts: List[Dict[str, Any]]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"alerts": alerts}, f, indent=2)
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    # -- reads -------------------------------------------------------------
    def list_alerts(self) -> List[Dict[str, Any]]:
        with self._lock:
            return self._load()

    def get(self, alert_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for a in self._load():
                if a.get("id") == alert_id:
                    return a
        return None

    # -- mutations ---------------------------------------------------------
    def add_alert(
        self, symbol: str, direction: str, threshold: Any, note: str = ""
    ) -> Dict[str, Any]:
        sym = _normalize_symbol(symbol)
        d = str(direction or "").strip().lower()
        if d not in _DIRECTIONS:
            raise PriceAlertError(f"direction must be one of {list(_DIRECTIONS)}")
        try:
            thr = float(threshold)
        except (TypeError, ValueError):
            raise PriceAlertError("threshold must be a number.")
        if thr <= 0:
            raise PriceAlertError("threshold must be positive.")
        rule = {
            "id": uuid.uuid4().hex,
            "symbol": sym,
            "direction": d,
            "threshold": round(thr, 4),
            "note": str(note or "")[:_MAX_NOTE],
            "active": True,
            "created_at": _now_iso(),
            "triggered_at": None,
            "last_price": None,
        }
        with self._lock:
            alerts = self._load()
            alerts.append(rule)
            self._save(alerts)
        return rule

    def delete_alert(self, alert_id: str) -> bool:
        with self._lock:
            alerts = self._load()
            kept = [a for a in alerts if a.get("id") != alert_id]
            if len(kept) == len(alerts):
                return False
            self._save(kept)
            return True

    def set_active(self, alert_id: str, active: bool) -> Optional[Dict[str, Any]]:
        """Enable/disable a rule.  Re-activating clears its triggered state."""
        with self._lock:
            alerts = self._load()
            found = None
            for a in alerts:
                if a.get("id") == alert_id:
                    a["active"] = bool(active)
                    if active:
                        a["triggered_at"] = None  # re-arm
                    found = a
                    break
            if found is None:
                return None
            self._save(alerts)
            return found

    def _persist_all(self, alerts: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._save(alerts)


_stores: Dict[str, PriceAlertStore] = {}
_stores_lock = RLock()


def get_price_alert_store(data_dir: str | Path) -> PriceAlertStore:
    key = str(Path(data_dir))
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = PriceAlertStore(data_dir)
            _stores[key] = store
        return store


def _crossed(direction: str, price: float, threshold: float) -> bool:
    if direction == "above":
        return price >= threshold
    return price <= threshold


def check_price_alerts(
    settings: Any,
    price_fetcher: Optional[Callable[[List[str]], Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Check every armed rule against live prices; fire + mark those crossed.

    Args:
        settings: app settings (needs ``DATA_DIR``).
        price_fetcher: ``symbols -> {symbol: price|{"price": price}}``.  Defaults
            to :func:`dashboard.quotes.get_quotes`.  Injectable for tests.

    Returns the list of rules that triggered this call.  Never raises.
    """
    store = get_price_alert_store(settings.DATA_DIR)
    try:
        alerts = store.list_alerts()
    except Exception as exc:  # noqa: BLE001
        log.warning("price_alerts.check_load_failed", error=str(exc))
        return []

    armed = [a for a in alerts if a.get("active") and not a.get("triggered_at")]
    if not armed:
        return []

    symbols = sorted({str(a.get("symbol", "")) for a in armed if a.get("symbol")})
    fetch = price_fetcher
    if fetch is None:
        from dashboard import quotes

        def fetch(syms: List[str]) -> Dict[str, Any]:
            return quotes.get_quotes(syms)

    try:
        quote_map = fetch(symbols) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("price_alerts.fetch_failed", error=str(exc))
        return []

    def _price_of(sym: str) -> Optional[float]:
        q = quote_map.get(sym)
        if q is None:
            return None
        if isinstance(q, dict):
            q = q.get("price")
        try:
            return float(q) if q is not None else None
        except (TypeError, ValueError):
            return None

    triggered: List[Dict[str, Any]] = []
    changed = False
    for rule in alerts:
        if not rule.get("active") or rule.get("triggered_at"):
            continue
        price = _price_of(str(rule.get("symbol", "")))
        if price is None:
            continue
        rule["last_price"] = round(price, 4)
        if _crossed(str(rule.get("direction")), price, float(rule.get("threshold", 0))):
            rule["triggered_at"] = _now_iso()
            changed = True
            triggered.append(dict(rule))
            _publish(settings, rule, price)
        else:
            changed = True  # last_price updated

    if changed:
        try:
            store._persist_all(alerts)
        except Exception as exc:  # noqa: BLE001
            log.warning("price_alerts.persist_failed", error=str(exc))

    if triggered:
        log.info("price_alerts.triggered", count=len(triggered),
                 symbols=[t["symbol"] for t in triggered])
    return triggered


def _publish(settings: Any, rule: Dict[str, Any], price: float) -> None:
    """Best-effort push notification for a triggered rule."""
    try:
        from dashboard.push import publish

        arrow = "above" if rule.get("direction") == "above" else "below"
        title = f"{rule['symbol']} price alert"
        body = (
            f"{rule['symbol']} crossed {arrow} "
            f"${float(rule['threshold']):,.2f} (now ${price:,.2f})."
        )
        if rule.get("note"):
            body += f" — {rule['note']}"
        publish(title, body, settings.DATA_DIR, category="ai_alert")
    except Exception as exc:  # noqa: BLE001 -- never fail the check on delivery
        log.warning("price_alerts.publish_failed", error=str(exc))
