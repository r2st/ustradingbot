"""HTTP-level tests for the new dashboard feature routers.

Auth is disabled (``DASHBOARD_AUTH_ENABLED=False``) and ``DATA_DIR`` is pointed
at a temp dir via env so the shared ``get_settings()`` singleton — used by every
router — resolves to an isolated, hermetic configuration.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "False")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "adminpw")
    monkeypatch.setenv("TOTAL_CAPITAL", "10000")
    from config.settings import get_settings

    get_settings.cache_clear()
    # Reset per-data_dir singleton caches so stores rebuild against tmp.
    import config.watchlist as wl
    import journal.notes as notes
    import dashboard.api_keys as apik
    import dashboard.push as push
    import users.accounts as accts
    wl._STORE.clear()
    notes._STORES.clear()
    apik._STORES.clear()
    push._STORES.clear()
    accts._STORES.clear()

    import dashboard.app as dash
    c = TestClient(dash.app, raise_server_exceptions=False)
    c._data_dir = data_dir  # type: ignore[attr-defined]
    yield c
    get_settings.cache_clear()


# ------------------------------------------------------------------ watchlist


def test_watchlist_crud(client):
    r = client.get("/api/watchlist")
    assert r.status_code == 200
    assert r.json()["scan_count"] > 0

    assert client.post("/api/watchlist/lists", json={"name": "Faves"}).status_code == 200
    r = client.post("/api/watchlist/symbols", json={"list": "Faves", "symbol": "tsla"})
    assert r.status_code == 200
    faves = next(l for l in r.json()["lists"] if l["name"] == "Faves")
    assert "TSLA" in faves["symbols"]

    r = client.delete("/api/watchlist/lists/Faves/symbols/TSLA")
    faves = next(l for l in r.json()["lists"] if l["name"] == "Faves")
    assert "TSLA" not in faves["symbols"]

    assert client.post("/api/watchlist/symbols", json={"list": "Faves", "symbol": "bad sym"}).status_code == 400


# ---------------------------------------------------------------------- notes


def test_notes_crud_and_search(client):
    assert client.post("/api/notes/10", json={"note": "gap up winner", "tags": ["win"]}).status_code == 200
    client.post("/api/notes/11", json={"note": "chased it", "tags": ["mistake"]})

    r = client.get("/api/notes", params={"q": "gap"})
    assert [n["trade_id"] for n in r.json()["notes"]] == ["10"]

    r = client.get("/api/notes", params={"tag": "mistake"})
    assert [n["trade_id"] for n in r.json()["notes"]] == ["11"]

    assert client.delete("/api/notes/10").json()["ok"] is True


# --------------------------------------------------------------- manual trade


def test_manual_trade_requires_admin(client):
    r = client.post("/api/manual-trade", json={
        "symbol": "AAPL", "entry_price": 100, "stop_price": 95,
        "target_price": 110, "quantity": 5, "admin_password": "wrong",
    })
    assert r.status_code == 403


def test_manual_trade_places_with_paper_broker(client):
    r = client.post("/api/manual-trade", json={
        "symbol": "AAPL", "entry_price": 100, "stop_price": 95,
        "target_price": 110, "quantity": 5, "admin_password": "adminpw",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["symbol"] == "AAPL" and body["quantity"] == 5


# ------------------------------------------------------------------- insights


def test_montecarlo_empty_journal(client):
    r = client.get("/api/montecarlo", params={"runs": 100, "horizon": 10})
    assert r.status_code == 200
    assert r.json()["trades_sampled"] == 0


def test_regime_disabled_is_neutral(client, monkeypatch):
    monkeypatch.setenv("REGIME_DETECTION_ENABLED", "False")
    from config.settings import get_settings

    get_settings.cache_clear()
    r = client.get("/api/regime")
    assert r.status_code == 200
    assert r.json()["weight_multipliers"] == {"momentum": 1.0, "swing": 1.0}


def test_autotune_preview(client):
    r = client.get("/api/autotune")
    assert r.status_code == 200
    assert "thresholds" in r.json()


def test_earnings_and_premarket_offline(client):
    # Empty the watchlist so the scans return immediately without network.
    import config.watchlist as wl

    store = wl.get_watchlist_store(client._data_dir)
    for name in store.list_names():
        store.delete_list(name)
    assert client.get("/api/earnings").json()["earnings"] == []
    assert client.get("/api/premarket").json()["hits"] == []


# -------------------------------------------------------------------- export


def test_export_csv_and_pdf(client):
    r = client.get("/api/export/trades.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]

    r = client.get("/api/export/trades.pdf")
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"

    r = client.get("/api/export/analytics.pdf")
    assert r.content[:4] == b"%PDF"


# ---------------------------------------------------------------------- users


def test_users_disabled_by_default(client):
    r = client.post("/api/users/register", json={"username": "alice", "password": "password1"})
    assert r.status_code == 404  # MULTI_USER_ENABLED is False


def test_users_register_login_profile(client, monkeypatch):
    monkeypatch.setenv("MULTI_USER_ENABLED", "True")
    from config.settings import get_settings

    get_settings.cache_clear()

    r = client.post("/api/users/register", json={"username": "alice", "password": "password1"})
    assert r.status_code == 200, r.text
    r = client.post("/api/users/login", json={"username": "alice", "password": "password1"})
    token = r.json()["token"]
    r = client.get("/api/users/me", headers={"X-User-Token": token})
    assert r.json()["username"] == "alice"
    r = client.post("/api/users/me/profile", headers={"X-User-Token": token},
                    json={"capital": 5000, "strategies": ["swing"], "watchlist": ["nvda"]})
    assert r.json()["profile"]["capital"] == 5000


# ----------------------------------------------------------------- REST API v1


def test_api_v1_requires_key(client):
    assert client.get("/api/v1/positions").status_code == 401
    # Index is public.
    assert client.get("/api/v1").status_code == 200


def test_api_v1_key_lifecycle(client):
    r = client.post("/api/v1/keys", json={"name": "test"})
    assert r.status_code == 200
    raw = r.json()["key"]
    assert raw.startswith("ustb_")

    r = client.get("/api/v1/positions", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200
    r = client.get("/api/v1/watchlist", headers={"X-API-Key": raw})
    assert r.status_code == 200 and "scan_symbols" in r.json()


# --------------------------------------------------------------------- push


def test_push_assets_and_flow(client):
    assert client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/sw.js").status_code == 200
    assert client.get("/pwa/icon.svg").status_code == 200

    r = client.post("/api/push/test")
    assert r.status_code == 200
    nid = r.json()["notification"]["id"]
    r = client.get("/api/push/poll", params={"since": nid - 1})
    assert any(n["id"] == nid for n in r.json()["notifications"])
