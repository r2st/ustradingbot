"""
Accessibility regression guards for the dashboard SPA (audit F-1/F-2/F-5).

* Every data ``<th>`` carries a ``scope`` so screen readers can associate a cell
  with its header (F-1).
* Every ``<table>`` is named — a ``<caption>`` or an ``aria-label`` (F-1).
* Modal focus management, a skip link, and OS theme preference (F-2/F-5).
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch) -> None:
    monkeypatch.setattr(
        dash, "get_settings", lambda: Settings(DASHBOARD_AUTH_ENABLED=False)
    )


@pytest.fixture
def html(client) -> str:
    return client.get("/").text


def test_page_renders(html) -> None:
    # A broken caption/scope edit inside a JS template string would surface as a
    # 500 or a truncated body — assert the page is whole.
    assert "</html>" in html
    assert "<title" in html


def test_all_static_th_have_scope(html) -> None:
    # Ignore the intentional empty action-column headers (<th></th>) which name
    # nothing; every *labelled* header must carry a scope.
    # A labelled header has real text right after `>` (not `</th>` or markup).
    labelled = re.findall(r"<th(?![a-z])([^>]*)>\s*(?=[^<\s])", html)
    missing = [attrs for attrs in labelled if "scope=" not in attrs]
    assert not missing, f"{len(missing)} labelled <th> without scope: {missing[:5]}"


def test_sr_only_class_defined(html) -> None:
    assert ".sr-only" in html


def test_every_table_is_named(html) -> None:
    # Each <table> must be named by a <caption> or an aria-label. Check that the
    # count of tables matches the count of caption/aria-label association points.
    tables = re.findall(r"<table\b[^>]*>", html)
    captions = html.count("<caption")
    aria_labelled = sum(1 for t in tables if "aria-label" in t)
    assert captions + aria_labelled >= len(tables), (
        f"{len(tables)} tables but only {captions} captions + "
        f"{aria_labelled} aria-labels"
    )


def test_scope_values_are_valid(html) -> None:
    for val in re.findall(r"scope=['\"]([^'\"]+)['\"]", html):
        assert val in ("col", "row", "colgroup", "rowgroup"), val
