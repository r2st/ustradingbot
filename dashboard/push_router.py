"""
PWA + push-notification routes (feature 17).

Serves the web-app manifest, service worker, an icon, and the small client
script that turns the dashboard into an installable PWA and raises native
notifications for new trade alerts (polled from :mod:`dashboard.push`).
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from config.settings import get_settings
from dashboard.auth import require_auth
from dashboard.push import get_push_store

router = APIRouter(tags=["pwa"])


def _store():
    return get_push_store(get_settings().DATA_DIR)


# ---------------------------------------------------------------------------
# PWA assets (public — browsers fetch these without credentials)
# ---------------------------------------------------------------------------

_MANIFEST = {
    "name": "US Trading Bot",
    "short_name": "TradingBot",
    "description": "US/CA equity trading bot dashboard",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0F172A",
    "theme_color": "#0F172A",
    "icons": [
        {"src": "/favicon.svg", "sizes": "32x32", "type": "image/svg+xml", "purpose": "any"},
        {"src": "/pwa/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"},
    ],
}

# "Bull Circuit" mark — a minimalist bull head drawn from circuit / chart
# traces (horns in blue, an upward green chart line through the face, and
# green/red circuit nodes). Drawn on a 32-unit grid so it can be scaled to any
# icon size with a single <g transform>.
_BULL_PATHS = (
    '<path d="M7 13 C4 9 5 5.5 9 7" fill="none" stroke="#3B82F6" stroke-width="2" stroke-linecap="round"/>'
    '<path d="M25 13 C28 9 27 5.5 23 7" fill="none" stroke="#3B82F6" stroke-width="2" stroke-linecap="round"/>'
    '<path d="M9 7 C9 11 8.5 13 10 15.5 C11.6 18.2 14 20 16 20 C18 20 20.4 18.2 22 15.5 '
    'C23.5 13 23 11 23 7" fill="none" stroke="#3B82F6" stroke-width="2" stroke-linejoin="round"/>'
    '<polyline points="10.5 15 13.5 12.4 16.5 14 21 9" fill="none" stroke="#22C55E" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
    '<circle cx="9" cy="7" r="1.7" fill="#22C55E"/>'
    '<circle cx="23" cy="7" r="1.7" fill="#22C55E"/>'
    '<circle cx="21" cy="9" r="1.6" fill="#22C55E"/>'
    '<circle cx="10.5" cy="15" r="1.5" fill="#EF4444"/>'
    '<circle cx="13" cy="13" r="1" fill="#3B82F6"/>'
    '<circle cx="19" cy="13" r="1" fill="#3B82F6"/>'
)

# 32×32 favicon — the bull on a rounded navy tile so it reads on any tab colour.
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="#0F172A"/>'
    + _BULL_PATHS +
    '</svg>'
)

# 192×192 maskable PWA icon — same mark scaled 6× on a full-bleed navy tile.
_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">'
    '<rect width="192" height="192" rx="36" fill="#0F172A"/>'
    '<g transform="scale(6)">' + _BULL_PATHS + '</g></svg>'
)

_SERVICE_WORKER = """
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => self.clients.claim());
// Real Web Push (future): show any pushed payload as a notification.
self.addEventListener('push', (event) => {
  let data = { title: 'Trading Bot', body: 'Update' };
  try { if (event.data) data = event.data.json(); } catch (e) {}
  event.waitUntil(self.registration.showNotification(data.title, {
    body: data.body, icon: '/pwa/icon.svg', badge: '/pwa/icon.svg'
  }));
});
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(clients.matchAll({ type: 'window' }).then((cs) => {
    for (const c of cs) { if ('focus' in c) return c.focus(); }
    if (clients.openWindow) return clients.openWindow('/');
  }));
});
"""

_PWA_CLIENT = """
(function () {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(function () {});
  }
  function enable() {
    if (!('Notification' in window)) return;
    if (Notification.permission === 'default') Notification.requestPermission();
  }
  window.enablePushNotifications = enable;

  var KEY = 'ustb_push_last_id';
  function lastId() { return parseInt(localStorage.getItem(KEY) || '0', 10) || 0; }
  function setLast(id) { localStorage.setItem(KEY, String(id)); }

  function poll() {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    fetch('/api/push/poll?since=' + lastId(), { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.notifications) return;
        data.notifications.forEach(function (n) {
          try { new Notification(n.title, { body: n.body, icon: '/pwa/icon.svg' }); } catch (e) {}
          if (n.id > lastId()) setLast(n.id);
        });
      })
      .catch(function () {});
  }
  // Seed lastId so we don't replay history on first grant, then poll.
  fetch('/api/push/status', { credentials: 'same-origin' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) { if (d && lastId() === 0) setLast(d.latest_id || 0); });
  setInterval(poll, 20000);
})();
"""


@router.get("/manifest.webmanifest")
@router.get("/manifest.json")
async def manifest():
    return Response(content=__import__("json").dumps(_MANIFEST),
                    media_type="application/manifest+json")


@router.get("/sw.js")
async def service_worker():
    return Response(content=_SERVICE_WORKER, media_type="application/javascript")


@router.get("/favicon.svg")
async def favicon():
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


@router.get("/pwa/icon.svg")
async def icon():
    return Response(content=_ICON_SVG, media_type="image/svg+xml")


@router.get("/pwa/pwa.js")
async def pwa_client():
    return Response(content=_PWA_CLIENT, media_type="application/javascript")


# ---------------------------------------------------------------------------
# Push subscription + polling API (auth-guarded)
# ---------------------------------------------------------------------------


async def _body(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


@router.get("/api/push/status")
async def push_status(_user: str = Depends(require_auth)):
    store = _store()
    return {"subscriptions": store.subscription_count(), "latest_id": store.latest_id()}


@router.get("/api/push/poll")
async def push_poll(since: int = 0, _user: str = Depends(require_auth)):
    return {"notifications": _store().poll(since)}


@router.post("/api/push/subscribe")
async def push_subscribe(request: Request, _user: str = Depends(require_auth)):
    body = await _body(request)
    return {"ok": _store().subscribe(body)}


@router.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request, _user: str = Depends(require_auth)):
    body = await _body(request)
    return {"ok": _store().unsubscribe(str(body.get("endpoint", "")))}


@router.post("/api/push/test")
async def push_test(_user: str = Depends(require_auth)):
    notif = _store().publish("Trading Bot", "Test notification from the dashboard.")
    return {"ok": True, "notification": notif.to_dict()}
