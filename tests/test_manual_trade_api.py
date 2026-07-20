"""
HTTP integration tests for the manual-trade endpoint (audit item T1).

Exercises the full FastAPI stack for ``POST /api/manual-trade``:

* happy path — a valid, admin-authorised trade is placed,
* auth — 401 without dashboard credentials,
* admin gate — 403 without / with a wrong admin password,
* input validation — 422 for missing symbol, negative / zero quantity, and a
  malformed symbol, all rejected by :class:`dashboard.schemas.ManualTradeRequest`
  before any money-path code runs.

The broker/journal side effects are stubbed by monkeypatching
``execution.manual_trade.place_manual_trade`` (imported lazily inside the route),
so these tests stay hermetic and never touch a real broker.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class _FakeResult:
    """Stand-in for execution.manual_trade.ManualTradeResult."""

    def __init__(self, **payload):
        self._payload = {"ok": True, "message": "Order placed.", **payload}

    def to_dict(self):
        return self._payload


@pytest.fixture
def _stub_place(monkeypatch):
    """Replace the real broker call; record the params it was handed."""
    calls = []

    def _fake(params, settings, *a, **k):
        calls.append(params)
        return _FakeResult(symbol=params.get("symbol"), quantity=params.get("quantity"))

    import execution.manual_trade as mt

    monkeypatch.setattr(mt, "place_manual_trade", _fake)
    return calls


def _client(monkeypatch, *, tmp_path, auth=False, admin="adminpw"):
    """Build a TestClient with a hermetic Settings singleton."""
    data_dir = tmp_path / "data_store"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "True" if auth else "False")
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", admin)
    monkeypatch.setenv("TOTAL_CAPITAL", "10000")

    from config.settings import get_settings

    get_settings.cache_clear()
    import dashboard.app as dash

    return TestClient(dash.app, raise_server_exceptions=False)


_VALID = {
    "symbol": "AAPL",
    "quantity": 10,
    "entry_price": 100.0,
    "stop_price": 95.0,
    "target_price": 110.0,
    "admin_password": "adminpw",
}


def test_happy_path_places_trade(monkeypatch, tmp_path, _stub_place):
    client = _client(monkeypatch, auth=False, tmp_path=tmp_path)
    resp = client.post("/api/manual-trade", json=_VALID)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # The admin password is stripped before the trade is placed.
    assert _stub_place and "admin_password" not in _stub_place[0]
    assert _stub_place[0]["symbol"] == "AAPL"
    assert _stub_place[0]["quantity"] == 10


def test_auth_required_401(monkeypatch, tmp_path, _stub_place):
    client = _client(monkeypatch, auth=True, tmp_path=tmp_path)
    resp = client.post("/api/manual-trade", json=_VALID)  # no credentials
    assert resp.status_code == 401
    assert not _stub_place  # never reached the broker


def test_admin_password_required_403(monkeypatch, tmp_path, _stub_place):
    client = _client(monkeypatch, auth=False, tmp_path=tmp_path)
    body = dict(_VALID, admin_password="wrong")
    resp = client.post("/api/manual-trade", json=body)
    assert resp.status_code == 403
    assert not _stub_place


def test_admin_password_missing_403(monkeypatch, tmp_path, _stub_place):
    client = _client(monkeypatch, auth=False, tmp_path=tmp_path)
    body = {k: v for k, v in _VALID.items() if k != "admin_password"}
    resp = client.post("/api/manual-trade", json=body)
    assert resp.status_code == 403
    assert not _stub_place


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda b: b.pop("symbol"), id="missing-symbol"),
        pytest.param(lambda b: b.update(symbol="bad sym"), id="bad-symbol-format"),
        pytest.param(lambda b: b.update(quantity=-5), id="negative-qty"),
        pytest.param(lambda b: b.update(quantity=0), id="zero-qty"),
        pytest.param(lambda b: b.update(entry_price=-1), id="negative-price"),
        pytest.param(lambda b: b.update(entry_price="notanumber"), id="non-numeric-price"),
    ],
)
def test_input_validation_422(monkeypatch, tmp_path, _stub_place, mutate):
    client = _client(monkeypatch, auth=False, tmp_path=tmp_path)
    body = dict(_VALID)
    mutate(body)
    resp = client.post("/api/manual-trade", json=body)
    assert resp.status_code == 422, resp.text
    assert not _stub_place  # rejected before any broker call
