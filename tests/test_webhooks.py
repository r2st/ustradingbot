"""Tests for inbound webhooks + veto store (P0-3)."""

from __future__ import annotations

import hmac
import json
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from dashboard.webhook_router import parse_tradingview_alert, verify_signature
from execution.veto import VetoStore


# ---------------------------------------------------------------------------
# Veto store (pure)
# ---------------------------------------------------------------------------


class TestVetoStore:
    def test_add_and_is_vetoed(self, tmp_path) -> None:
        store = VetoStore(tmp_path)
        store.add("NVDA")
        assert store.is_vetoed("NVDA") is True
        assert store.is_vetoed("nvda") is True  # case-insensitive
        assert store.is_vetoed("AAPL") is False

    def test_strategy_scoped_veto(self, tmp_path) -> None:
        store = VetoStore(tmp_path)
        store.add("NVDA", strategy="momentum")
        assert store.is_vetoed("NVDA", "momentum") is True
        assert store.is_vetoed("NVDA", "swing") is False
        # A symbol-wide veto blocks any strategy.
        store.add("TSLA")
        assert store.is_vetoed("TSLA", "swing") is True

    def test_expiry(self, tmp_path) -> None:
        store = VetoStore(tmp_path)
        rule = store.add("NVDA", ttl_minutes=0.0001)  # ~6ms
        import time

        time.sleep(0.05)
        assert store.is_vetoed("NVDA") is False
        # Expired veto is pruned from disk on read.
        assert all(v["id"] != rule["id"] for v in store.list_active())

    def test_remove(self, tmp_path) -> None:
        store = VetoStore(tmp_path)
        rule = store.add("NVDA")
        assert store.remove(rule["id"]) is True
        assert store.is_vetoed("NVDA") is False
        assert store.remove("nonexistent") is False

    def test_invalid_symbol_rejected(self, tmp_path) -> None:
        from execution.veto import VetoError

        store = VetoStore(tmp_path)
        with pytest.raises(VetoError):
            store.add("not a symbol!!")


# ---------------------------------------------------------------------------
# HMAC signature verification (pure)
# ---------------------------------------------------------------------------


class TestSignature:
    def test_valid_signature(self) -> None:
        body = b'{"a": 1}'
        secret = "s3cret"
        sig = hmac.new(secret.encode(), body, sha256).hexdigest()
        assert verify_signature(body, sig, secret) is True
        assert verify_signature(body, f"sha256={sig}", secret) is True

    def test_invalid_signature(self) -> None:
        assert verify_signature(b"body", "deadbeef", "secret") is False

    def test_no_secret_skips_check(self) -> None:
        # Empty secret means signatures aren't required.
        assert verify_signature(b"body", "", "") is True

    def test_missing_signature_with_secret_fails(self) -> None:
        assert verify_signature(b"body", "", "secret") is False


# ---------------------------------------------------------------------------
# TradingView alert parsing (pure)
# ---------------------------------------------------------------------------


class TestTradingViewParse:
    def test_flat_buy(self) -> None:
        env = parse_tradingview_alert(
            {"ticker": "AAPL", "action": "buy", "quantity": 10,
             "stop": 95, "target": 110, "price": 100}
        )
        assert env["intent"] == "trade"
        assert env["symbol"] == "AAPL"
        assert env["side"] == "buy"
        assert env["quantity"] == 10
        assert env["stop_price"] == 95
        assert env["target_price"] == 110

    def test_native_strategy_block(self) -> None:
        env = parse_tradingview_alert(
            {"ticker": "MSFT",
             "strategy": {"order_action": "sell", "order_contracts": 5}}
        )
        assert env["side"] == "sell"
        assert env["quantity"] == 5

    def test_long_short_map_to_buy_sell(self) -> None:
        assert parse_tradingview_alert({"symbol": "X", "action": "long"})["side"] == "buy"
        assert parse_tradingview_alert({"symbol": "X", "action": "short"})["side"] == "sell"

    def test_veto_intent(self) -> None:
        env = parse_tradingview_alert(
            {"ticker": "NVDA", "action": "veto", "comment": "news risk"}
        )
        assert env["intent"] == "veto"
        assert env["symbol"] == "NVDA"
        assert env["note"] == "news risk"

    def test_missing_symbol_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_tradingview_alert({"action": "buy"})

    def test_unknown_action_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_tradingview_alert({"symbol": "AAPL", "action": "dance"})


# ---------------------------------------------------------------------------
# HTTP integration
# ---------------------------------------------------------------------------


def _client(monkeypatch, tmp_path, *, enabled=True, secret="", allow_trades=False):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "False")
    monkeypatch.setenv("WEBHOOKS_ENABLED", "True" if enabled else "False")
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", secret)
    monkeypatch.setenv("WEBHOOK_ALLOW_TRADES", "True" if allow_trades else "False")
    monkeypatch.setenv("TOTAL_CAPITAL", "10000")

    from config.settings import get_settings

    get_settings.cache_clear()
    # Mint an API key in this data dir.
    from dashboard.api_keys import get_api_key_store

    raw, _info = get_api_key_store(str(data_dir)).create("test")

    import dashboard.app as dash

    return TestClient(dash.app, raise_server_exceptions=False), raw


def test_webhook_requires_enabled(monkeypatch, tmp_path):
    client, key = _client(monkeypatch, tmp_path, enabled=False)
    resp = client.post("/api/webhooks/veto", json={"symbol": "NVDA"},
                       headers={"X-API-Key": key})
    assert resp.status_code == 404


def test_webhook_requires_key(monkeypatch, tmp_path):
    client, key = _client(monkeypatch, tmp_path)
    resp = client.post("/api/webhooks/veto", json={"symbol": "NVDA"})  # no key
    assert resp.status_code == 401


def test_webhook_veto_happy_path(monkeypatch, tmp_path):
    client, key = _client(monkeypatch, tmp_path)
    resp = client.post("/api/webhooks/veto", json={"symbol": "NVDA"},
                       headers={"X-API-Key": key})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # Listed as active.
    lst = client.get("/api/webhooks/veto", headers={"X-API-Key": key})
    assert any(v["symbol"] == "NVDA" for v in lst.json()["vetoes"])


def test_webhook_trade_dry_run(monkeypatch, tmp_path):
    client, key = _client(monkeypatch, tmp_path, allow_trades=False)
    resp = client.post(
        "/api/webhooks/trade",
        json={"symbol": "AAPL", "side": "buy", "quantity": 5,
              "stop_price": 95, "target_price": 110, "entry_price": 100},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["dry_run"] is True


def test_webhook_hmac_enforced(monkeypatch, tmp_path):
    secret = "topsecret"
    client, key = _client(monkeypatch, tmp_path, secret=secret)
    payload = {"symbol": "NVDA"}
    raw = json.dumps(payload).encode()
    good = hmac.new(secret.encode(), raw, sha256).hexdigest()

    # Wrong signature -> 401.
    bad = client.post("/api/webhooks/veto", data=raw,
                      headers={"X-API-Key": key, "X-Signature": "sha256=bad",
                               "Content-Type": "application/json"})
    assert bad.status_code == 401

    # Correct signature -> 200.
    ok = client.post("/api/webhooks/veto", data=raw,
                     headers={"X-API-Key": key, "X-Signature": f"sha256={good}",
                              "Content-Type": "application/json"})
    assert ok.status_code == 200


def test_webhook_tradingview_routes_veto(monkeypatch, tmp_path):
    client, key = _client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/webhooks/tradingview",
        json={"ticker": "NVDA", "action": "veto"},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 200
    assert resp.json()["intent"] == "veto"


# ---------------------------------------------------------------------------
# Dashboard-authenticated admin surface (Webhooks Manager panel)
# ---------------------------------------------------------------------------


def test_admin_status_masks_secret(monkeypatch, tmp_path):
    client, _key = _client(monkeypatch, tmp_path, enabled=True,
                           secret="supersecretvalue123", allow_trades=True)
    resp = client.get("/api/webhooks/admin/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["allow_trades"] is True
    assert body["has_secret"] is True
    # Masked, never the raw secret.
    assert body["secret_masked"] != "supersecretvalue123"
    assert "•" in body["secret_masked"]
    assert body["trade_url"].endswith("/api/webhooks/trade")


def test_admin_secret_reveal(monkeypatch, tmp_path):
    client, _key = _client(monkeypatch, tmp_path, secret="revealme12345")
    resp = client.get("/api/webhooks/admin/secret")
    assert resp.status_code == 200
    assert resp.json()["secret"] == "revealme12345"


def test_admin_veto_crud_and_deliveries(monkeypatch, tmp_path):
    client, _key = _client(monkeypatch, tmp_path)
    # Add
    resp = client.post("/api/webhooks/admin/veto",
                       json={"symbol": "NVDA", "note": "cool off"})
    assert resp.status_code == 200
    vid = resp.json()["veto"]["id"]
    # List
    resp = client.get("/api/webhooks/admin/vetoes")
    assert any(v["id"] == vid for v in resp.json()["vetoes"])
    # Delete
    resp = client.delete(f"/api/webhooks/admin/veto/{vid}")
    assert resp.status_code == 200
    # Delete again -> 404
    assert client.delete(f"/api/webhooks/admin/veto/{vid}").status_code == 404
    # Deliveries endpoint returns a list.
    resp = client.get("/api/webhooks/admin/deliveries")
    assert resp.status_code == 200
    assert isinstance(resp.json()["deliveries"], list)


def test_record_delivery_ring_buffer():
    from dashboard import webhook_router as wr

    before = len(wr.recent_deliveries())
    wr.record_delivery("tradingview", "trade", "AAPL", True, "dry-run")
    after = wr.recent_deliveries()
    assert len(after) == before + 1
    assert after[0]["symbol"] == "AAPL"
    assert after[0]["source"] == "tradingview"
