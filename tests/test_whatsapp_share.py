"""Tests for WhatsApp sharing wiring (portfolio, watchlist, public pages)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings
from dashboard import public_market_router

SHARE_JS = Path(__file__).resolve().parent.parent / "dashboard" / "static" / "share.js"

client = TestClient(dash.app, raise_server_exceptions=False)


@pytest.fixture
def no_auth(monkeypatch, tmp_path):
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=tmp_path)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)


@pytest.fixture
def offline_market(monkeypatch):
    monkeypatch.setattr(public_market_router, "_fetch_ohlcv", lambda s: None)
    public_market_router.clear_cache()
    yield
    public_market_router.clear_cache()


def test_share_js_served():
    r = client.get("/static/share.js")
    assert r.status_code == 200
    assert "DoAideShare" in r.text
    assert "https://wa.me/?text=" in r.text


def test_dashboard_has_portfolio_and_watchlist_share(no_auth):
    html = client.get("/dashboard").text
    assert 'id="ovShareWa"' in html
    assert 'id="ovShareAmounts"' in html
    assert 'id="wlShareWa"' in html
    assert '/static/share.js' in html


@pytest.mark.parametrize("path,btn", [("/market", "wa-market"), ("/screener", "wa-screener")])
def test_public_pages_have_whatsapp(offline_market, path, btn):
    html = client.get(path).text
    assert f'id="{btn}"' in html
    assert "wa.me" in html
    assert "/static/share.js" in html


NODE_SCRIPT = r"""
global.window = {};
require(process.argv[1]);
const S = window.DoAideShare;
const d = {account_equity: 9900, day_pnl: -100, day_pnl_pct: -1,
           holdings: [{symbol: "BBB", weight_pct: 18.18, unrealized_pct: -10}]};
const hidden = S.portfolioText(d);
const shown = S.portfolioText(d, {includeAmounts: true});
const wl = S.watchlistText("WL", [{symbol: "NVDA", price: 120.5, change_pct: 2.1},
                                   {symbol: "X", price: null, change_pct: null}]);
console.log(JSON.stringify({hidden, shown, wl, url: S.waUrl("a b&c")}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_share_text_builders():
    import json

    out = subprocess.run(["node", "-e", NODE_SCRIPT, str(SHARE_JS)],
                         capture_output=True, text=True, check=True).stdout
    res = json.loads(out)
    assert "$9,900" not in res["hidden"]          # amounts are opt-in
    assert "-1.00%" in res["hidden"]
    assert "BBB · 18.2% of book · -10.00%" in res["hidden"]
    assert "Value: $9,900.00" in res["shown"]
    assert "Day's P&L: -$100.00 (-1.00%)" in res["shown"]
    assert "• NVDA $120.50 (+2.10%)" in res["wl"]
    assert "• X —" in res["wl"] and "(—%)" not in res["wl"]
    assert res["url"] == "https://wa.me/?text=a%20b%26c"
