"""Regression guards for the dashboard's responsive/mobile scaffolding."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch) -> None:
    monkeypatch.setattr(dash, "get_settings", lambda: Settings(DASHBOARD_AUTH_ENABLED=False))


def test_has_viewport_meta(client) -> None:
    html = client.get("/").text
    assert 'name="viewport"' in html
    assert "width=device-width" in html
    # Exactly one viewport tag.
    assert html.count("width=device-width") == 1


def test_has_hamburger_nav(client) -> None:
    html = client.get("/").text
    assert 'class="nav-toggle"' in html
    assert 'id="sectionNav"' in html
    assert "function toggleNav" in html
    assert "function closeNav" in html


def test_section_anchor_ids_present(client) -> None:
    html = client.get("/").text
    for anchor in ("nav-provider", "nav-account", "nav-strategy",
                   "nav-risk", "nav-status", "nav-help"):
        assert f'id="{anchor}"' in html
        assert f'href="#{anchor}"' in html


def test_responsive_css_present(client) -> None:
    html = client.get("/").text
    assert "@media (max-width: 768px)" in html
    assert "@media (max-width: 480px)" in html
    # Tables get a horizontal-scroll wrapper.
    assert "table-wrap" in html
    # Sticky-header anchor offset.
    assert "scroll-margin-top" in html


def test_touch_target_sizing(client) -> None:
    html = client.get("/").text
    # Controls bumped to a >=44px minimum tap target on mobile.
    assert "min-height: 44px" in html
