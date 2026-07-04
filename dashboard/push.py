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

_FILENAME = "push_state.json"
_MAX_NOTIFICATIONS = 100


@dataclass
class Notification:
    id: int
    ts: float
    title: str
    body: str

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "ts": self.ts, "title": self.title, "body": self.body}


class PushStore:
    """File-backed store of push subscriptions and a bounded notification queue."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / _FILENAME
        self._lock = RLock()
        self._subs: Dict[str, Dict[str, Any]] = {}
        self._notifications: List[Dict[str, Any]] = []
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
            self._notifications = raw.get("notifications", []) or []
            self._next_id = int(raw.get("next_id", 1) or 1)

    def _save(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "subscriptions": self._subs,
            "notifications": self._notifications,
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

    def publish(self, title: str, body: str, now: float | None = None) -> Notification:
        """Enqueue a notification for connected clients to pick up on the next poll."""
        with self._lock:
            notif = Notification(
                id=self._next_id,
                ts=now if now is not None else time.time(),
                title=str(title),
                body=str(body),
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


def publish(title: str, body: str, data_dir: str | Path) -> None:
    """Convenience: enqueue a notification.  Best-effort; never raises."""
    try:
        get_push_store(data_dir).publish(title, body)
    except Exception:  # noqa: BLE001
        pass
