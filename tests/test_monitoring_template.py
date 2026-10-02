"""Template presence tests for the monitoring features (F1–F9).

Guards against template regressions: the new sections, nav anchors, live-P&L
widgets, and the self-hosted chart library must all be present in the HTML.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


@pytest.fixture
def html(client, monkeypatch, tmp_path) -> str:
    data_dir = tmp_path / "data_store"
    data_dir.mkdir()
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=data_dir)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    return resp.text


@pytest.mark.parametrize("anchor", [
    "nav-history", "nav-charts", "nav-activity", "nav-alerts",
])
def test_monitoring_section_anchors_present(html, anchor):
    assert f'id="{anchor}"' in html


def test_live_pnl_widgets_present(html):
    for element_id in ("paperUnrealized", "paperTodayTotal", "livePnlStatus",
                       "pnlIntradayChart"):
        assert f'id="{element_id}"' in html


def test_chartjs_self_hosted(html):
    assert '/static/chart.umd.min.js' in html
    assert "cdn." not in html.lower().replace("cdn.jsdelivr", "")  # no CDN scripts


def test_rationale_modal_present(html):
    assert 'id="rationaleModal"' in html
    assert "showRationale" in html
    assert "renderCandlesSVG" in html


def test_open_risk_widgets_present(html):
    for element_id in ("riskOpenTotal", "riskBudgetBar", "riskMtmBadge",
                       "riskOpenRows"):
        assert f'id="{element_id}"' in html


def test_activity_and_alerts_widgets_present(html):
    for element_id in ("actFeed", "actCycleRows", "alertRuleRows",
                       "alertHistFeed", "wlMonRows", "histRows",
                       "histWinRateChart", "chartEquity", "chartDrawdown"):
        assert f'id="{element_id}"' in html


def test_static_chart_asset_served(client, monkeypatch, tmp_path):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir()
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=data_dir)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    resp = client.get("/static/chart.umd.min.js")
    assert resp.status_code == 200
    assert len(resp.content) > 100_000  # the real Chart.js bundle, not a stub
