"""Phase 1 UI polish — regression guards for the four dashboard improvements:

1. "Bull Circuit" logo + favicon (template head, header wordmark, PWA routes).
2. Dark-mode default using the Slate-Navy palette (kept toggle).
3. Sidebar navigation grouped into 5 collapsible categories.
4. Open-positions card view with a table toggle and a cookie preference.
"""

from __future__ import annotations

import json

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


@pytest.fixture
def html(client) -> str:
    resp = client.get("/")
    assert resp.status_code == 200
    return resp.text


def _seed_position(data_dir) -> None:
    (data_dir / "open_positions.json").write_text(
        json.dumps(
            {
                "AAPL": {
                    "symbol": "AAPL", "strategy": "momentum", "entry_price": 195.5,
                    "stop_price": 190.0, "target_price": 208.0, "quantity": 10,
                    "currency": "USD", "risk_amount": 55.0, "grade": "A",
                    "entry_time": "2026-07-04T10:00:00",
                }
            }
        )
    )


# ─────────────────────────── 1. Logo + favicon ───────────────────────────

def test_favicon_link_and_route(client, html) -> None:
    assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg">' in html
    resp = client.get("/favicon.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    body = resp.text
    assert "<svg" in body and 'viewBox="0 0 32 32"' in body
    # Uses the four brand colours.
    for colour in ("#0F172A", "#3B82F6", "#22C55E", "#EF4444"):
        assert colour in body


def test_header_wordmark_and_logo(html) -> None:
    assert 'class="brand-logo"' in html            # inline Bull Circuit mark
    assert 'class="wm-us"' in html and ">US<" in html
    assert 'class="wm-bot"' in html and ">TradingBot<" in html


def test_pwa_icon_and_manifest_use_brand(client) -> None:
    icon = client.get("/pwa/icon.svg")
    assert icon.status_code == 200
    assert "#0F172A" in icon.text and "#3B82F6" in icon.text
    for path in ("/manifest.webmanifest", "/manifest.json"):
        m = client.get(path)
        assert m.status_code == 200
        data = json.loads(m.text)
        assert data["theme_color"] == "#0F172A"
        assert data["background_color"] == "#0F172A"


# ─────────────────────────── 2. Dark palette ─────────────────────────────

def test_dark_palette_is_default(html) -> None:
    # The default :root block carries the Slate-Navy values.
    assert "--bg: #0F172A;" in html
    assert "--surface: #1E293B;" in html
    assert "--text: #F8FAFC;" in html
    assert "--text-muted: #94A3B8;" in html
    assert "--green: #22C55E;" in html
    assert "--red: #EF4444;" in html
    # Background is the navy→slate gradient, applied to the body.
    assert "linear-gradient(160deg, #0F172A 0%, #1E293B 100%)" in html
    assert "background: var(--bg-grad" in html
    assert '<meta name="theme-color" content="#0F172A">' in html


def test_theme_toggle_still_present(html) -> None:
    # Dark is default but the light-mode toggle must remain.
    assert 'id="themeToggle"' in html
    assert 'data-theme="light"' in html
    assert "ustb_theme" in html


# ───────────────────── 3. Collapsible sidebar nav ────────────────────────

def test_sidebar_has_five_collapsible_categories(html) -> None:
    for cat in ("trading", "analysis", "strategy", "activity", "system"):
        assert f'data-cat="{cat}"' in html
    assert html.count('class="nav-cat"') == 5
    assert "function toggleNavCat" in html
    # Category headers rendered as toggle buttons.
    assert 'class="nav-cat-toggle"' in html


def test_sidebar_preserves_hamburger_and_helpers(html) -> None:
    assert 'class="nav-toggle"' in html
    assert 'id="sectionNav"' in html
    assert "function toggleNav" in html
    assert "function closeNav" in html
    assert 'id="navBackdrop"' in html


@pytest.mark.parametrize("label,anchor", [
    ("Data", "nav-provider"), ("Account", "nav-account"),
    ("Watchlist", "nav-watchlist"), ("Trade", "nav-trade"),
    ("Scanner", "nav-scanner"), ("Earnings", "nav-earnings"),
    ("Strategies", "nav-strategy"), ("Risk", "nav-risk"),
    ("Journal", "nav-notes"), ("API", "nav-api"), ("Help", "nav-help"),
])
def test_nav_links_all_grouped(html, label, anchor) -> None:
    # Every legacy anchor link survives the regrouping.
    assert f'href="#{anchor}"' in html
    assert f">{label}</a>" in html


# ────────────────────── 4. Position card view ────────────────────────────

def _positions_region(html: str) -> str:
    """The server-rendered open-positions block only (excludes the later
    JavaScript that mentions the same class names)."""
    start = html.index('id="paperPositionsWrap"')
    end = html.index("Recent Trade History", start)
    return html[start:end]


def test_positions_default_to_cards(client, monkeypatch, tmp_path) -> None:
    _seed_position(tmp_path)
    monkeypatch.setattr(
        dash, "get_settings",
        lambda: Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=tmp_path),
    )
    html = client.get("/").text
    region = _positions_region(html)
    assert 'class="pos-cards"' in region
    assert 'class="pos-card"' in region
    assert 'class="pos-ticker">AAPL' in region
    assert "grade grade-a" in region                # grade badge
    assert "Shares" in region and "Value" in region  # card metrics
    assert 'class="pos-progress"' in region         # progress bar
    assert "score-table" not in region              # not the table layout
    for btn in ("Why?", "TA Chart", "Stop"):
        assert btn in region
    # Cards toggle is the active one by default.
    assert 'data-view="cards" aria-pressed="true"' in html


def test_positions_table_view_via_cookie(client, monkeypatch, tmp_path) -> None:
    _seed_position(tmp_path)
    monkeypatch.setattr(
        dash, "get_settings",
        lambda: Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=tmp_path),
    )
    client.cookies.set("ustb_pos_view", "table")
    html = client.get("/").text
    region = _positions_region(html)
    assert "score-table" in region
    assert 'class="pos-cards"' not in region
    assert 'data-view="table" aria-pressed="true"' in html


def test_positions_view_toggle_and_js(html) -> None:
    assert 'id="posViewToggle"' in html
    assert 'data-view="cards"' in html and 'data-view="table"' in html
    assert "function setPosView" in html
    assert "ustb_pos_view" in html                  # cookie preference


def test_positions_empty_state_preserved(client, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        dash, "get_settings",
        lambda: Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=tmp_path),
    )
    html = client.get("/").text
    assert "No open positions yet" in html
