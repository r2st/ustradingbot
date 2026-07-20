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

def test_sidebar_has_six_collapsible_categories(html) -> None:
    # "reports" was added alongside the Reports section (Tax, Attribution,
    # Statements, Walk-Forward, etc.), bringing the sidebar to six categories.
    for cat in ("trading", "analytics", "market", "ai", "reports", "settings"):
        assert f'data-cat="{cat}"' in html
    assert html.count('class="nav-cat"') == 6
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
    ("Data Provider", "nav-provider"), ("Positions &amp; P&amp;L", "nav-account"),
    ("Watchlist", "nav-watchlist"), ("Manual Trade", "nav-trade"),
    ("Scanner", "nav-scanner"), ("Earnings", "nav-earnings"),
    ("Strategies", "nav-strategy"), ("Risk", "nav-risk"),
    ("Journal", "nav-notes"), ("API", "nav-api"), ("Help", "nav-help"),
])
def test_nav_links_all_grouped(html, label, anchor) -> None:
    # Every anchor link survives the regrouping (labels updated in the overhaul).
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


# ───────────────── 5. Navigation overhaul (sidebar/palette/mobile) ─────────

def test_command_palette_scaffolding(html) -> None:
    # Cmd/Ctrl-K quick-jump overlay, input and wiring.
    assert 'id="cmdkOverlay"' in html
    assert 'id="cmdkInput"' in html
    assert "window.openCmdK" in html
    assert "window.closeCmdK" in html
    # Opens on the sidebar search affordance.
    assert 'class="nav-search"' in html


def test_command_palette_keyboard_shortcut_bound(html) -> None:
    # Cmd/Ctrl-K binding plus single-key shortcuts guarded against typing.
    assert "metaKey" in html and "ctrlKey" in html
    assert "function isTyping" in html
    assert "function scrollToSection" in html


def test_mobile_bottom_nav(html) -> None:
    assert 'id="bottomNav"' in html
    assert 'class="bottom-nav"' in html
    assert "window.bottomNavGo" in html
    # One tab per top-level category.
    for cat in ("trading", "analytics", "market", "ai", "settings"):
        assert f'class="bn-item" data-cat="{cat}"' in html


def test_persistent_sidebar_media_query(html) -> None:
    # The drawer promotes to a fixed rail on wide screens.
    assert "@media (min-width: 1080px)" in html
    assert "--sidebar-w" in html


def test_metric_tooltips_present(html) -> None:
    # Contextual info tooltips on key P&L metrics.
    assert 'class="info-tip"' in html
    assert "data-tip=" in html


# ───────────────── 6. Data-viz + performance layer ─────────────────────────

def test_allocation_donut_and_pnl_timeline_present(html) -> None:
    assert 'id="allocDonut"' in html
    assert "renderAllocationDonut" in html
    assert 'id="pnlTimeline"' in html
    assert "setPnlTimelineFreq" in html
    # Period toggle for the timeline.
    for freq in ("daily", "weekly", "monthly"):
        assert f'data-freq="{freq}"' in html


def test_risk_gauges_present(html) -> None:
    for gid in ("gaugeExposure", "gaugeDrawdown", "gaugeConcentration"):
        assert f'id="{gid}"' in html
    assert "renderRiskGauges" in html
    assert 'class="gauge-fill"' in html


def test_position_sparklines_wired(html) -> None:
    # Inline P&L sparkline helper + a Trend column in the table view.
    assert "window._spark" in html
    assert 'class="pos-spark"' in html
    assert ">Trend</th>" in html


def test_performance_layer_present(html) -> None:
    # Debounce + lazy-load helpers, global fetch-wrapping stale indicator.
    assert "window.debounce" in html
    assert "window.lazySection" in html
    assert "IntersectionObserver" in html
    assert "__fetchWrapped" in html
    assert 'class="updating-dot"' in html or "updating-dot" in html


def test_sticky_and_swipe_table_behaviour(html) -> None:
    assert "sticky-col" in html
    assert "can-scroll-right" in html
    assert "swipe-hint" in html
    assert "function enhanceTables" in html


def test_activity_filter_is_debounced(html) -> None:
    # Symbol filter no longer fetches on every keystroke.
    assert 'oninput="actFilterDebounced()"' in html
    assert "actFilterDebounced" in html


def test_charts_and_memory_lazy_loaded(html) -> None:
    # Heavy far-down sections load on scroll, not at boot.
    assert 'lazySection("nav-charts"' in html
    assert 'lazySection("nav-memory"' in html
