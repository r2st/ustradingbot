"""Guards for the per-section descriptions and Help reference.

Every dashboard section carries a one-line subtitle and a "?" link that deep-links
to a matching card in the Help section. These are all driven by
``dashboard.app._build_section_guides`` so the subtitle and the how-to card can
never drift apart. The tests below lock in that contract.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

import dashboard.app as dash
from config.settings import Settings


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch) -> None:
    monkeypatch.setattr(dash, "get_settings", lambda: Settings(DASHBOARD_AUTH_ENABLED=False))


# --- data-layer contract ----------------------------------------------------

REQUIRED_FIELDS = ("key", "title", "summary", "what", "how")


def test_guides_have_required_fields() -> None:
    guides = dash._build_section_guides()
    assert guides, "expected at least one section guide"
    for g in guides:
        for field in REQUIRED_FIELDS:
            assert g.get(field), f"guide {g.get('key')!r} missing {field!r}"
        assert isinstance(g["how"], list) and g["how"], f"{g['key']} needs how-to steps"
        # Keep subtitles short enough to read at a glance in the header.
        assert len(g["summary"]) <= 120, f"{g['key']} summary too long"


def test_guide_keys_are_unique() -> None:
    keys = [g["key"] for g in dash._build_section_guides()]
    assert len(keys) == len(set(keys)), f"duplicate guide keys: {keys}"


# The detailed-help fields every card now carries: a quick-start callout, a
# fuller "what" write-up, worked examples and common-mistake warnings.
DETAIL_LIST_FIELDS = ("examples", "mistakes")


def test_guides_have_detailed_help_fields() -> None:
    for g in dash._build_section_guides():
        key = g["key"]
        assert g.get("quickstart"), f"{key} missing quickstart callout"
        # The "what" description is a 2-3 sentence explanation, not a fragment.
        assert g.get("what") and g["what"].count(".") >= 2, f"{key} what too thin"
        for field in DETAIL_LIST_FIELDS:
            val = g.get(field)
            assert isinstance(val, list) and val, f"{key} needs {field!r} bullets"


# --- rendered-page contract -------------------------------------------------


def _html(client: TestClient) -> str:
    resp = client.get("/")
    assert resp.status_code == 200
    return resp.text


def test_every_guide_renders_a_subtitle_and_help_card(client: TestClient) -> None:
    html = _html(client)
    for g in dash._build_section_guides():
        key = g["key"]
        # The section header carries the "?" deep-link ...
        assert f'href="#help-{key}"' in html, f"missing help link for {key}"
        # ... which targets a card in the Help reference ...
        assert f'id="help-{key}"' in html, f"missing help card for {key}"
        # ... and the header shows the summary text as a subtitle (Jinja
        # HTML-escapes it, so compare against the escaped form).
        assert str(escape(g["summary"])) in html, f"summary for {key} not rendered"


def test_every_content_section_has_a_help_link(client: TestClient) -> None:
    html = _html(client)
    # Each guide corresponds to a nav-<key> content section; that section's
    # header must expose exactly one help link. (The Help section itself has no
    # guide/"?" — it is the destination.)
    keys = {g["key"] for g in dash._build_section_guides()}
    # Section ids are lowercase and may be hyphenated (e.g. nav-indicator-alerts).
    section_ids = set(re.findall(r'id="nav-([a-z][a-z-]*)"', html))
    # Every guide key is a real section on the page.
    assert keys <= section_ids, f"guides without a section: {keys - section_ids}"
    # The help link and subtitle counts line up with the number of guides.
    assert html.count('class="section-help-link"') == len(keys)
    # One subtitle per guide, plus the Help section's own subtitle.
    assert html.count('class="section-sub"') == len(keys) + 1


def test_help_reference_grid_present(client: TestClient) -> None:
    html = _html(client)
    assert 'class="help-ref-grid"' in html
    assert "Section reference" in html
    # The Help section keeps its paper-trading walkthrough too.
    assert "Paper-trading walkthrough" in html


def test_detailed_help_fields_render(client: TestClient) -> None:
    html = _html(client)
    # The quick-start callout, examples and mistakes blocks render once the
    # guide carries the data (every card does).
    assert html.count('class="help-guide-quickstart"') == len(dash._build_section_guides())
    assert 'class="help-guide-examples"' in html
    assert 'class="help-guide-mistakes"' in html
    # A representative quick-start string reaches the page (HTML-escaped).
    provider = next(g for g in dash._build_section_guides() if g["key"] == "provider")
    assert str(escape(provider["quickstart"])) in html


def test_no_unrendered_guide_placeholders(client: TestClient) -> None:
    html = _html(client)
    # A broken Jinja reference would leave the variable name in the output.
    assert "guides_map" not in html
    assert "{{" not in html.split("<script")[0]
