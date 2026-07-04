"""Verify the rendered dashboard includes the new feature sections (features 1-19).

Guards against template regressions: the new sections, dark-mode toggle, PWA
hooks, and mobile-friendly anchors must all be present in the HTML.
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
    resp = client.get("/")
    assert resp.status_code == 200
    return resp.text


@pytest.mark.parametrize("anchor", [
    "nav-watchlist", "nav-trade", "nav-scanner", "nav-earnings",
    "nav-montecarlo", "nav-notes", "nav-api",
])
def test_new_section_anchors_present(html, anchor):
    assert f'id="{anchor}"' in html


def test_nav_links_present(html):
    for label in ("Watchlist", "Trade", "Scanner", "Earnings", "Journal", "API"):
        assert f">{label}<" in html or f"{label}</a>" in html


def test_dark_mode_toggle_present(html):
    assert 'id="themeToggle"' in html
    assert 'data-theme="light"' in html          # light-theme CSS block
    assert "ustb_theme" in html                    # persisted preference


def test_pwa_hooks_present(html):
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert 'name="theme-color"' in html
    assert '/pwa/pwa.js' in html


def test_heatmap_and_correlation_matrix_present(html):
    assert 'id="riskHeatmap"' in html      # feature 6
    assert 'id="riskCorrMatrix"' in html   # feature 7


def test_feature_widgets_present(html):
    for element_id in ("wlLists", "mtResult", "scannerRows", "earningsRows",
                       "mcFan", "noteRows", "apiKeyList", "regimeBox", "autotuneBox"):
        assert f'id="{element_id}"' in html


def test_export_download_links(html):
    assert '/api/export/trades.csv' in html
    assert '/api/export/analytics.pdf' in html


def test_viewport_meta_still_present(html):
    # Mobile-friendliness invariant must survive the new sections.
    assert 'name="viewport"' in html
