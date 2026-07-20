"""
Tests for the operational endpoints (audit item B-3):

* ``/health`` stays an unconditional liveness probe;
* ``/readyz`` reflects broker/provider/engine readiness with a 200/503;
* ``/version`` reports the build identity;
* ``/metrics`` exposes Prometheus-format counters/gauges/histograms and reflects
  live request/trade activity.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path, **env):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "False")
    monkeypatch.setenv("TOTAL_CAPITAL", "10000")
    monkeypatch.setenv("BROKER", "paper")
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "yfinance")
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))

    from config.settings import get_settings

    get_settings.cache_clear()
    from dashboard import metrics

    metrics.reset()
    import dashboard.app as dash

    return TestClient(dash.app, raise_server_exceptions=False), data_dir


def test_health_is_liveness_only(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readyz_ready_on_paper_yfinance(monkeypatch, tmp_path):
    # Paper broker + yfinance (no key needed) + no heartbeat → ready.
    client, _ = _client(monkeypatch, tmp_path)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"]["provider"]["ok"] is True
    assert body["checks"]["broker"]["ok"] is True
    assert body["checks"]["engine"]["present"] is False


def test_readyz_not_ready_when_provider_missing_key(monkeypatch, tmp_path):
    # Polygon requires a key that isn't configured → provider check fails → 503.
    client, _ = _client(monkeypatch, tmp_path, MARKET_DATA_PROVIDER="polygon")
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["ready"] is False
    assert resp.json()["checks"]["provider"]["ok"] is False


def test_readyz_stale_heartbeat_fails(monkeypatch, tmp_path):
    client, data_dir = _client(monkeypatch, tmp_path)
    # Write an old heartbeat → engine present but stale → 503.
    hb = {"updated_at": "2000-01-01T00:00:00+00:00", "scan_interval_min": 5}
    (data_dir / "engine_status.json").write_text(json.dumps(hb), encoding="utf-8")
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["engine"]["ok"] is False
    assert resp.json()["checks"]["engine"]["present"] is True


def test_version_reports_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_SHA", "abc123def456789")
    client, _ = _client(monkeypatch, tmp_path)
    # ops.version_info caches; reset the cache so the env var is honoured.
    import dashboard.ops as ops

    ops._version_cache = None
    resp = client.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["git_sha"] == "abc123def456789"
    assert body["git_sha_short"] == "abc123def456"
    assert "version" in body


def test_metrics_endpoint_prometheus_format(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    # Generate some traffic so counters/histograms are populated.
    client.get("/health")
    client.get("/api/mode")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    text = resp.text
    assert "# TYPE ustb_http_requests_total counter" in text
    assert "ustb_http_requests_total{" in text
    assert "# TYPE ustb_http_request_duration_ms histogram" in text
    assert "ustb_http_request_duration_ms_bucket{" in text
    assert 'le="+Inf"' in text
    # Engine gauge published at scrape time.
    assert "ustb_engine_up" in text


def test_metrics_counts_manual_trade(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    trade = {
        "symbol": "AAPL", "quantity": 1, "entry_price": 100.0,
        "stop_price": 95.0, "target_price": 110.0, "admin_password": "wrong",
    }
    client.post("/api/manual-trade", json=trade)  # 403 bad password
    text = client.get("/metrics").text
    assert 'ustb_manual_trades_total{result="bad_password"}' in text
