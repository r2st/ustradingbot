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
    html = client.get("/dashboard").text
    assert 'name="viewport"' in html
    assert "width=device-width" in html
    # Exactly one viewport tag.
    assert html.count("width=device-width") == 1


def test_has_hamburger_nav(client) -> None:
    html = client.get("/dashboard").text
    assert 'class="nav-toggle"' in html
    assert 'id="sectionNav"' in html
    assert "function toggleNav" in html
    assert "function closeNav" in html


def test_section_anchor_ids_present(client) -> None:
    html = client.get("/dashboard").text
    for anchor in ("nav-provider", "nav-account", "nav-strategy",
                   "nav-risk", "nav-status", "nav-help"):
        assert f'id="{anchor}"' in html
        assert f'href="#{anchor}"' in html


def test_responsive_css_present(client) -> None:
    html = client.get("/dashboard").text
    assert "@media (max-width: 768px)" in html
    assert "@media (max-width: 480px)" in html
    # Tables get a horizontal-scroll wrapper.
    assert "table-wrap" in html
    # Sticky-header anchor offset.
    assert "scroll-margin-top" in html


def test_touch_target_sizing(client) -> None:
    html = client.get("/dashboard").text
    # Controls bumped to a >=44px minimum tap target on mobile.
    assert "min-height: 44px" in html


def test_breadcrumb_section_indicator(client) -> None:
    html = client.get("/dashboard").text
    # Sticky breadcrumb / current-section indicator with live crumb targets.
    assert 'id="sectionIndicator"' in html
    assert 'id="crumbCat"' in html
    assert 'id="crumbSection"' in html


def test_back_to_top_button(client) -> None:
    html = client.get("/dashboard").text
    assert 'id="backToTop"' in html
    assert 'class="back-to-top"' in html
    assert "function scrollToTop" in html or "window.scrollToTop" in html


def test_scroll_spy_wiring(client) -> None:
    html = client.get("/dashboard").text
    # The scroll-spy adds an active class to the matching nav link and keeps a
    # rAF-throttled scroll listener.
    assert ".section-nav a[href^=\"#\"].active" in html
    assert "requestAnimationFrame" in html
    assert 'addEventListener("scroll"' in html


def test_collapsible_sections(client) -> None:
    html = client.get("/dashboard").text
    # Collapsible section cards + their persisted-state key.
    assert "section-collapse-caret" in html
    assert "ustb_sections_collapsed" in html
    assert "data-collapsible" in html
