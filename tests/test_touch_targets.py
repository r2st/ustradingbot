"""Mobile touch targets: every surface ships a >=48px coarse-pointer block."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings

client = TestClient(dash.app, raise_server_exceptions=False)

TOUCH_MEDIA = "@media (pointer: coarse), (max-width: 768px)"


def _touch_block(css: str) -> str:
    compact = css.replace("(pointer:coarse),(max-width:768px)", "(pointer: coarse), (max-width: 768px)")
    idx = compact.find(TOUCH_MEDIA)
    assert idx != -1, "missing coarse-pointer touch-target media query"
    return compact[idx: idx + 3000]


def test_public_css_touch_targets():
    css = client.get("/static/viral.css").text
    block = _touch_block(css)
    for selector in (".v-btn", ".v-share-btn", ".v-input", ".v-nav a"):
        assert selector in block
    assert re.search(r"min-height:\s*48px", block)


def test_landing_touch_targets():
    html = client.get("/").text
    block = _touch_block(html)
    assert ".tab, .btn, .field input { min-height: 48px; }" in block


@pytest.fixture
def no_auth(monkeypatch, tmp_path):
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=tmp_path)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)


def test_dashboard_touch_targets(no_auth):
    html = client.get("/dashboard").text
    block = _touch_block(html)
    for selector in (".btn-primary", ".btn-secondary", ".icon-btn", "select", ".section-nav a"):
        assert selector in block
    assert "min-height: 48px; min-width: 48px;" in block
    # Inputs at 16px stop iOS Safari zooming the page on focus.
    assert "select, textarea { min-height: 48px; font-size: 16px; }" in block
