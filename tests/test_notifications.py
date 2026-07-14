"""Tests for the Phase 2 notification center: categories, read-state, and
per-category preferences (``dashboard.push`` + ``dashboard.push_router``).

Auth is disabled and ``DATA_DIR`` points at a tmp dir so the file-backed
:class:`~dashboard.push.PushStore` is isolated per test.
"""

from __future__ import annotations

import json

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
    import dashboard.push as push

    push._STORES.clear()
    import dashboard.app as dash

    c = TestClient(dash.app, raise_server_exceptions=False)
    c._data_dir = data_dir  # type: ignore[attr-defined]
    yield c
    get_settings.cache_clear()
    push._STORES.clear()


# --------------------------------------------------------------- store layer


def test_store_publish_records_category_and_read_flag(tmp_path):
    from dashboard.push import PushStore

    store = PushStore(tmp_path)
    n = store.publish("Entry: AAPL", "Bought 10", category="trade_executed")
    assert n.category == "trade_executed"
    assert n.read is False
    recent = store.recent()
    assert recent[0]["id"] == n.id
    assert recent[0]["category"] == "trade_executed"
    assert store.unread_count() == 1


def test_store_unknown_category_normalized_to_general(tmp_path):
    from dashboard.push import PushStore

    store = PushStore(tmp_path)
    n = store.publish("x", "y", category="bogus")
    assert n.category == "general"


def test_store_mark_read_and_mark_all(tmp_path):
    from dashboard.push import PushStore

    store = PushStore(tmp_path)
    a = store.publish("a", "1", category="ai_alert")
    store.publish("b", "2", category="stop_hit")
    assert store.unread_count() == 2
    assert store.mark_read([a.id]) == 1
    assert store.unread_count() == 1
    # Re-marking the same id is a no-op.
    assert store.mark_read([a.id]) == 0
    assert store.mark_all_read() == 1
    assert store.unread_count() == 0
    assert store.mark_all_read() == 0


def test_store_preferences_roundtrip_and_persistence(tmp_path):
    from dashboard.push import DEFAULT_PREFERENCES, PushStore

    store = PushStore(tmp_path)
    assert store.get_preferences() == DEFAULT_PREFERENCES
    store.set_preferences({"stop_hit": False, "ai_alert": False})
    assert store.is_category_enabled("stop_hit") is False
    assert store.is_category_enabled("trade_executed") is True
    # A fresh store reads the persisted preferences back.
    reloaded = PushStore(tmp_path)
    assert reloaded.is_category_enabled("stop_hit") is False
    assert reloaded.is_category_enabled("ai_alert") is False


def test_store_recent_is_bounded_and_newest_first(tmp_path):
    from dashboard.push import PushStore

    store = PushStore(tmp_path)
    for i in range(5):
        store.publish(f"n{i}", "body", category="general")
    recent = store.recent(limit=3)
    assert [r["title"] for r in recent] == ["n4", "n3", "n2"]


def test_module_publish_respects_muted_category(tmp_path):
    from dashboard.push import get_push_store, publish

    store = get_push_store(tmp_path)
    store.set_preferences({"stop_hit": False})
    publish("Stop", "hit", tmp_path, category="stop_hit")
    assert store.unread_count() == 0  # dropped
    publish("Entry", "done", tmp_path, category="trade_executed")
    assert store.unread_count() == 1  # delivered


def test_legacy_state_without_category_loads(tmp_path):
    """A push_state.json written before Phase 2 must still load cleanly."""
    from dashboard.push import PushStore

    legacy = {
        "subscriptions": {},
        "notifications": [{"id": 1, "ts": 1.0, "title": "old", "body": "b"}],
        "next_id": 2,
    }
    (tmp_path / "push_state.json").write_text(json.dumps(legacy), encoding="utf-8")
    store = PushStore(tmp_path)
    recent = store.recent()
    assert recent[0]["category"] == "general"
    assert recent[0]["read"] is False
    assert store.unread_count() == 1


# --------------------------------------------------------------- HTTP layer


def test_notifications_list_and_unread(client):
    client.post("/api/push/test", json={"category": "trade_executed"})
    client.post("/api/push/test", json={"category": "target_reached"})
    d = client.get("/api/notifications").json()
    assert d["unread"] == 2
    assert len(d["notifications"]) == 2
    assert d["notifications"][0]["category"] == "target_reached"  # newest first


def test_notifications_mark_read_endpoint(client):
    r = client.post("/api/push/test", json={"category": "ai_alert"}).json()
    nid = r["notification"]["id"]
    assert r["notification"]["category"] == "ai_alert"
    d = client.post("/api/notifications/read", json={"ids": [nid]}).json()
    assert d["ok"] is True and d["changed"] == 1 and d["unread"] == 0


def test_notifications_mark_all_read_endpoint(client):
    client.post("/api/push/test", json={"category": "stop_hit"})
    client.post("/api/push/test", json={"category": "trade_executed"})
    assert client.get("/api/notifications/unread").json()["unread"] == 2
    d = client.post("/api/notifications/read-all").json()
    assert d["changed"] == 2 and d["unread"] == 0


def test_preferences_get_and_set_endpoint(client):
    d = client.get("/api/notifications/preferences").json()
    assert d["preferences"]["stop_hit"] is True
    assert "ai_alert" in d["categories"]
    d = client.post(
        "/api/notifications/preferences",
        json={"preferences": {"stop_hit": False}},
    ).json()
    assert d["ok"] is True and d["preferences"]["stop_hit"] is False
    # Persisted for the next read.
    assert client.get("/api/notifications/preferences").json()["preferences"]["stop_hit"] is False


def test_push_test_categories_have_distinct_copy(client):
    seen = {}
    for cat in ("trade_executed", "stop_hit", "target_reached", "ai_alert"):
        n = client.post("/api/push/test", json={"category": cat}).json()["notification"]
        assert n["category"] == cat
        seen[cat] = n["title"]
    assert len(set(seen.values())) == 4


def test_legacy_push_endpoints_still_work(client):
    """The pre-Phase-2 /api/push/* contract must remain intact."""
    assert client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/sw.js").status_code == 200
    r = client.post("/api/push/test")
    assert r.status_code == 200
    nid = r.json()["notification"]["id"]
    polled = client.get("/api/push/poll", params={"since": nid - 1}).json()
    assert any(n["id"] == nid for n in polled["notifications"])
    assert client.get("/api/push/status").json()["latest_id"] == nid
