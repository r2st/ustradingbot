"""First-run onboarding guide on the dashboard."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings

client = TestClient(dash.app, raise_server_exceptions=False)


@pytest.fixture
def html(monkeypatch, tmp_path):
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=tmp_path)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    r = client.get("/dashboard")
    assert r.status_code == 200
    return r.text


def test_onboarding_modal_rendered(html):
    assert 'id="onboardingModal"' in html
    assert html.count('class="onb-step"') == 5
    assert 'aria-label="Getting started guide"' in html


def test_onboarding_shown_once_and_reopenable(html):
    assert 'localStorage.getItem(KEY) === "1"' in html
    assert '"doaide_onboarded"' in html
    assert "data-open-onboarding" in html
    assert "Take the tour" in html


def test_onboarding_reflects_paper_mode(html):
    assert "Everything runs on simulated money" in html


def test_onboarding_links_to_real_sections(html):
    for anchor in ("#nav-overview", "#nav-watchlist", "#nav-engine"):
        assert f'href="{anchor}" data-onb-close' in html
        assert f'id="{anchor[1:]}"' in html


def test_onboarding_uses_shared_modal_overlay(html):
    # anyModalOpen() keys off .modal-overlay so shortcuts stay quiet while open.
    assert 'class="modal-overlay" id="onboardingModal"' in html
