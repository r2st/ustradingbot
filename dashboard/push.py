"""
PWA push notifications (feature 17).

Full Web Push with VAPID needs a server-side crypto dependency (pywebpush) that
this project deliberately avoids.  Instead we implement a dependency-free,
poll-based notification bridge that behaves like push from the user's point of
view:

* the browser installs the PWA (manifest + service worker) and is granted
  Notification permission;
* the page polls :func:`poll` for notifications newer than the last id it saw
  and raises a native ``Notification`` for each;
* the engine (or the dashboard) calls :meth:`PushStore.publish` — e.g. from the
  alert manager on a trade entry/exit — to enqueue one.

Subscriptions (browser ``PushSubscription`` objects) are also recorded so a real
VAPID sender can be added later without changing the client contract.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_FILENAME = "push_state.json"
_MAX_NOTIFICATIONS = 100

# Notification categories the dashboard understands.  ``general`` is the
# catch-all used for ad-hoc / test notifications; the rest map to the Phase 2
# alert types (trade executed, stop hit, target reached, AI alert).
CATEGORIES = ("trade_executed", "stop_hit", "target_reached", "ai_alert", "general")
DEFAULT_PREFERENCES: Dict[str, bool] = {c: True for c in CATEGORIES}


def _normalize_category(category: str) -> str:
    cat = str(category or "general").strip().lower()
    return cat if cat in CATEGORIES else "general"


@dataclass
class Notification:
    id: int
    ts: float
    title: str
    body: str
    category: str = "general"
    read: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "title": self.title,
            "body": self.body,
            "category": self.category,
            "read": self.read,
        }


class PushStore:
    """File-backed store of push subscriptions and a bounded notification queue."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / _FILENAME
        self._lock = RLock()
        self._subs: Dict[str, Dict[str, Any]] = {}
        self._notifications: List[Dict[str, Any]] = []
        self._preferences: Dict[str, bool] = dict(DEFAULT_PREFERENCES)
        self._next_id = 1
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self._path.exists():
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return
            self._subs = raw.get("subscriptions", {}) or {}
            # Backfill category/read on notifications written before Phase 2 so
            # older state files keep loading cleanly.
            loaded: List[Dict[str, Any]] = []
            for n in raw.get("notifications", []) or []:
                if not isinstance(n, dict):
                    continue
                n.setdefault("category", "general")
                n.setdefault("read", False)
                n["category"] = _normalize_category(n["category"])
                loaded.append(n)
            self._notifications = loaded
            prefs = raw.get("preferences", {})
            if isinstance(prefs, dict):
                for cat in CATEGORIES:
                    self._preferences[cat] = bool(prefs.get(cat, True))
            self._next_id = int(raw.get("next_id", 1) or 1)

    def _save(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "subscriptions": self._subs,
            "notifications": self._notifications,
            "preferences": self._preferences,
            "next_id": self._next_id,
        })
        fd, tmp = tempfile.mkstemp(dir=str(self._data_dir), prefix=".push_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, str(self._path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------------------------------------------------------- subscriptions

    def subscribe(self, subscription: Dict[str, Any]) -> bool:
        """Record a browser PushSubscription (keyed by its endpoint)."""
        endpoint = str(subscription.get("endpoint", "")).strip()
        if not endpoint:
            return False
        with self._lock:
            self._subs[endpoint] = subscription
            self._save()
            return True

    def unsubscribe(self, endpoint: str) -> bool:
        with self._lock:
            if endpoint in self._subs:
                del self._subs[endpoint]
                self._save()
                return True
            return False

    def subscription_count(self) -> int:
        with self._lock:
            return len(self._subs)

    # ---------------------------------------------------------- notifications

    def publish(
        self,
        title: str,
        body: str,
        category: str = "general",
        now: float | None = None,
    ) -> Notification:
        """Enqueue a notification for connected clients to pick up on the next poll."""
        with self._lock:
            notif = Notification(
                id=self._next_id,
                ts=now if now is not None else time.time(),
                title=str(title),
                body=str(body),
                category=_normalize_category(category),
                read=False,
            )
            self._next_id += 1
            self._notifications.append(notif.to_dict())
            if len(self._notifications) > _MAX_NOTIFICATIONS:
                self._notifications = self._notifications[-_MAX_NOTIFICATIONS:]
            self._save()
            return notif

    def poll(self, since_id: int = 0) -> List[Dict[str, Any]]:
        """Return notifications with ``id > since_id`` (oldest first)."""
        with self._lock:
            return [n for n in self._notifications if int(n.get("id", 0)) > since_id]

    def latest_id(self) -> int:
        with self._lock:
            return self._next_id - 1

    # ------------------------------------------------ notification-center reads

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the most recent notifications, newest first (bounded)."""
        with self._lock:
            limit = max(1, min(int(limit or 50), _MAX_NOTIFICATIONS))
            return list(reversed(self._notifications[-limit:]))

    def unread_count(self) -> int:
        with self._lock:
            return sum(1 for n in self._notifications if not n.get("read"))

    def mark_read(self, ids: List[int]) -> int:
        """Mark the given notification ids as read; return how many changed."""
        want = {int(i) for i in ids if str(i).lstrip("-").isdigit()}
        if not want:
            return 0
        changed = 0
        with self._lock:
            for n in self._notifications:
                if int(n.get("id", 0)) in want and not n.get("read"):
                    n["read"] = True
                    changed += 1
            if changed:
                self._save()
            return changed

    def mark_all_read(self) -> int:
        """Mark every notification read; return how many changed."""
        changed = 0
        with self._lock:
            for n in self._notifications:
                if not n.get("read"):
                    n["read"] = True
                    changed += 1
            if changed:
                self._save()
            return changed

    # ------------------------------------------------------------- preferences

    def get_preferences(self) -> Dict[str, bool]:
        with self._lock:
            return dict(self._preferences)

    def set_preferences(self, prefs: Dict[str, Any]) -> Dict[str, bool]:
        """Merge *prefs* (any subset of the known categories) and persist."""
        with self._lock:
            if isinstance(prefs, dict):
                for cat in CATEGORIES:
                    if cat in prefs:
                        self._preferences[cat] = bool(prefs[cat])
                self._save()
            return dict(self._preferences)

    def is_category_enabled(self, category: str) -> bool:
        with self._lock:
            return bool(self._preferences.get(_normalize_category(category), True))


_STORES: Dict[str, PushStore] = {}
_STORES_LOCK = RLock()


def get_push_store(data_dir: str | Path) -> PushStore:
    """Return a per-``data_dir`` cached :class:`PushStore` singleton."""
    key = str(Path(data_dir).resolve())
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = PushStore(data_dir)
            _STORES[key] = store
        return store


def publish(
    title: str,
    body: str,
    data_dir: str | Path,
    category: str = "general",
) -> None:
    """Convenience: enqueue a notification.  Best-effort; never raises.

    Honours the stored per-category preferences: when the user has muted
    *category* the notification is dropped so it never reaches the browser.
    """
    try:
        store = get_push_store(data_dir)
        if not store.is_category_enabled(category):
            return
        store.publish(title, body, category=category)
    except Exception:  # noqa: BLE001
        # Best-effort fan-out to the in-app feed; log so a broken push store is
        # diagnosable rather than silently swallowing notifications (B-9).
        log.debug("push.publish_failed", category=category, exc_info=True)
