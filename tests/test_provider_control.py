"""Tests for market-data provider selection + key management."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings
from dashboard.mode_control import consume_restart_request
from dashboard.provider_control import (
    provider_status,
    resolve_default_provider,
    save_api_keys,
    switch_provider,
)


# --------------------------------------------------------------------------- #
# provider_status
# --------------------------------------------------------------------------- #


def test_status_yfinance_default() -> None:
    st = provider_status(Settings())
    assert st["active"] == "yfinance"
    yf = next(p for p in st["providers"] if p["name"] == "yfinance")
    assert yf["active"] is True
    assert yf["connected"] is True
    assert yf["needs_key"] is False


def test_status_alpaca_needs_key() -> None:
    st = provider_status(Settings())
    alp = next(p for p in st["providers"] if p["name"] == "alpaca")
    assert alp["connected"] is False
    assert alp["needs_key"] is True
    # key fields reported, without values
    assert {kf["env"] for kf in alp["key_fields"]} == {
        "ALPACA_API_KEY", "ALPACA_API_SECRET"
    }
    assert all(kf["configured"] is False for kf in alp["key_fields"])


def test_status_alpaca_connected_when_keys_present() -> None:
    st = provider_status(Settings(ALPACA_API_KEY="k", ALPACA_API_SECRET="s"))
    alp = next(p for p in st["providers"] if p["name"] == "alpaca")
    assert alp["connected"] is True
    assert alp["needs_key"] is False
    assert all(kf["configured"] for kf in alp["key_fields"])


def test_status_polygon_needs_key() -> None:
    st = provider_status(Settings())
    pol = next(p for p in st["providers"] if p["name"] == "polygon")
    assert pol["connected"] is False
    assert pol["signup_url"] == "https://polygon.io"


# --------------------------------------------------------------------------- #
# switch_provider
# --------------------------------------------------------------------------- #


def test_switch_to_yfinance(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    s = Settings(DATA_DIR=tmp_path, MARKET_DATA_PROVIDER="yfinance")
    res = switch_provider("yfinance", s, env_path=env)
    assert res.ok is True
    assert "MARKET_DATA_PROVIDER=yfinance" in env.read_text()
    assert consume_restart_request(tmp_path) is True


def test_switch_to_alpaca_refused_without_keys(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    s = Settings(DATA_DIR=tmp_path)
    res = switch_provider("alpaca", s, env_path=env)
    assert res.ok is False
    assert "API key" in res.message
    assert not env.exists()


def test_switch_to_alpaca_with_keys(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    s = Settings(DATA_DIR=tmp_path, ALPACA_API_KEY="k", ALPACA_API_SECRET="s")
    res = switch_provider("alpaca", s, env_path=env)
    assert res.ok is True
    assert "MARKET_DATA_PROVIDER=alpaca" in env.read_text()


def test_switch_to_polygon_with_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    s = Settings(DATA_DIR=tmp_path, POLYGON_API_KEY="pk")
    res = switch_provider("polygon", s, env_path=env)
    assert res.ok is True
    assert "MARKET_DATA_PROVIDER=polygon" in env.read_text()


def test_switch_unknown_provider(tmp_path: Path) -> None:
    s = Settings(DATA_DIR=tmp_path)
    res = switch_provider("bloomberg", s, env_path=tmp_path / ".env")
    assert res.ok is False


# --------------------------------------------------------------------------- #
# save_api_keys
# --------------------------------------------------------------------------- #


def test_save_polygon_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    s = Settings(DATA_DIR=tmp_path)
    res = save_api_keys({"POLYGON_API_KEY": "pk123"}, s, env_path=env)
    assert res.ok is True
    assert "POLYGON_API_KEY=pk123" in env.read_text()
    # Polygon keys do NOT auto-switch the provider.
    assert "MARKET_DATA_PROVIDER=polygon" not in env.read_text()


def test_save_alpaca_keys_auto_selects_alpaca(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    s = Settings(DATA_DIR=tmp_path, MARKET_DATA_PROVIDER="yfinance")
    res = save_api_keys(
        {"ALPACA_API_KEY": "k", "ALPACA_API_SECRET": "s"}, s, env_path=env
    )
    assert res.ok is True
    assert res.active == "alpaca"
    text = env.read_text()
    assert "ALPACA_API_KEY=k" in text
    assert "MARKET_DATA_PROVIDER=alpaca" in text  # auto-switched


def test_save_partial_alpaca_key_does_not_switch(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    s = Settings(DATA_DIR=tmp_path)
    # Only the key, not the secret -> incomplete -> no auto-switch.
    res = save_api_keys({"ALPACA_API_KEY": "k"}, s, env_path=env)
    assert res.ok is True
    assert "MARKET_DATA_PROVIDER=alpaca" not in env.read_text()


def test_save_ignores_unknown_fields(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    s = Settings(DATA_DIR=tmp_path)
    res = save_api_keys({"HACKER_KEY": "x"}, s, env_path=env)
    assert res.ok is False


def test_no_auto_select_when_disabled(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    s = Settings(DATA_DIR=tmp_path)
    res = save_api_keys(
        {"ALPACA_API_KEY": "k", "ALPACA_API_SECRET": "s"}, s,
        env_path=env, auto_select=False,
    )
    assert res.ok is True
    assert "MARKET_DATA_PROVIDER=alpaca" not in env.read_text()


# --------------------------------------------------------------------------- #
# resolve_default_provider
# --------------------------------------------------------------------------- #


def test_resolve_default_prefers_alpaca_when_keys_present() -> None:
    s = Settings(ALPACA_API_KEY="k", ALPACA_API_SECRET="s")
    assert resolve_default_provider(s) == "alpaca"


def test_resolve_default_none_when_no_keys() -> None:
    assert resolve_default_provider(Settings()) is None


def test_resolve_default_none_when_already_set() -> None:
    s = Settings(MARKET_DATA_PROVIDER="polygon", ALPACA_API_KEY="k",
                 ALPACA_API_SECRET="s")
    assert resolve_default_provider(s) is None


# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


def _use(monkeypatch, tmp_path, **kw) -> None:
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=tmp_path, **kw)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    monkeypatch.setattr("dashboard.provider_control._project_root", lambda: tmp_path)


def test_api_list_providers(client, monkeypatch, tmp_path) -> None:
    _use(monkeypatch, tmp_path)
    resp = client.get("/api/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] == "yfinance"
    assert {p["name"] for p in body["providers"]} == {"yfinance", "alpaca", "polygon"}


def test_api_select_refused_without_key(client, monkeypatch, tmp_path) -> None:
    _use(monkeypatch, tmp_path)
    resp = client.post("/api/providers/select", json={"provider": "polygon"})
    assert resp.json()["ok"] is False


def test_api_select_yfinance_ok(client, monkeypatch, tmp_path) -> None:
    _use(monkeypatch, tmp_path, MARKET_DATA_PROVIDER="yfinance")
    resp = client.post("/api/providers/select", json={"provider": "yfinance"})
    assert resp.json()["ok"] is True


def test_api_save_keys(client, monkeypatch, tmp_path) -> None:
    _use(monkeypatch, tmp_path)
    resp = client.post("/api/providers/keys",
                       json={"keys": {"POLYGON_API_KEY": "abc"}})
    body = resp.json()
    assert body["ok"] is True
    assert "POLYGON_API_KEY=abc" in (tmp_path / ".env").read_text()


def test_dashboard_renders_provider_section(client, monkeypatch, tmp_path) -> None:
    _use(monkeypatch, tmp_path)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Market Data Provider" in resp.text
    assert "Yahoo Finance" in resp.text
    assert "Polygon.io" in resp.text
    assert "app.alpaca.markets" in resp.text  # help / signup link
