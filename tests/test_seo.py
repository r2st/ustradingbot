"""SEO: per-page meta, generated sitemap, robots.txt, and no fake ratings."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings
from dashboard import public_market_router
from dashboard.viral_router import ARTICLES

client = TestClient(dash.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _setup(monkeypatch, tmp_path):
    settings = Settings(DASHBOARD_AUTH_ENABLED=True, DASHBOARD_PASSWORD="pw", DATA_DIR=tmp_path)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    monkeypatch.setattr(public_market_router, "_fetch_ohlcv", lambda s: None)
    public_market_router.clear_cache()
    yield
    public_market_router.clear_cache()


def _sitemap_paths() -> list:
    xml = client.get("/sitemap.xml").text
    return [urlparse(u).path for u in re.findall(r"<loc>(.*?)</loc>", xml)]


def _meta(html: str, attr: str, name: str) -> str:
    m = re.search(rf'<meta {attr}="{re.escape(name)}" content="([^"]*)"', html)
    return m.group(1) if m else ""


def test_sitemap_lists_every_article_and_public_page():
    paths = _sitemap_paths()
    for article in ARTICLES:
        assert f"/blog/{article['slug']}" in paths
    for p in ("/", "/market", "/screener", "/tools", "/blog"):
        assert p in paths
    assert len(paths) == len(set(paths)), "duplicate sitemap entries"
    assert "/dashboard" not in paths


def test_every_sitemap_url_resolves(monkeypatch):
    # The /stock pages answer 503 when market data is unreachable, as it is
    # here; give them bars so this asserts the route exists, not the network.
    import numpy as np
    import pandas as pd

    from dashboard import public_market_router, stock_insight

    close = np.linspace(100.0, 130.0, 60)
    bars = pd.DataFrame({"Close": close, "Volume": 1_000_000},
                        index=pd.date_range("2026-01-01", periods=60, freq="B"))
    monkeypatch.setattr(public_market_router, "_fetch_ohlcv", lambda symbol: bars)
    monkeypatch.setattr(stock_insight, "_call_gemini", lambda settings, prompt: None)
    public_market_router.clear_cache()
    try:
        for path in _sitemap_paths():
            assert client.get(path).status_code == 200, path
    finally:
        public_market_router.clear_cache()
        stock_insight.clear_cache()


def test_sitemap_is_valid_xml_with_lastmod():
    import xml.etree.ElementTree as ET

    r = client.get("/sitemap.xml")
    assert r.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(r.text)
    assert root.tag.endswith("urlset")
    assert "<lastmod>" in r.text


def test_robots_blocks_private_areas():
    text = client.get("/robots.txt").text
    for path in ("/dashboard", "/api/", "/login", "/docs"):
        assert f"Disallow: {path}" in text
    assert "Sitemap: https://trade.doaide.com/sitemap.xml" in text


def test_each_public_page_has_unique_title_description_and_canonical():
    titles, descriptions = {}, {}
    for path in _sitemap_paths():
        html = client.get(path).text
        title = re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()
        desc = _meta(html, "name", "description")
        canonical = re.search(r'<link rel="canonical" href="([^"]+)"', html)
        assert title, path
        assert len(desc) >= 50, f"{path} description too short"
        assert canonical and urlparse(canonical.group(1)).path == path, path
        assert _meta(html, "property", "og:title"), path
        assert _meta(html, "property", "og:description"), path
        assert title not in titles, f"{path} duplicates title of {titles.get(title)}"
        assert desc not in descriptions, f"{path} duplicates description of {descriptions.get(desc)}"
        titles[title] = path
        descriptions[desc] = path


def test_no_fake_ratings_on_public_pages():
    for path in _sitemap_paths():
        html = client.get(path).text
        for marker in ("aggregateRating", "ratingValue", "reviewCount", '"@type":"Review"'):
            assert marker not in html, f"{path} contains {marker}"


def test_private_pages_are_noindex(monkeypatch, tmp_path):
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=tmp_path)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    html = client.get("/dashboard").text
    assert '<meta name="robots" content="noindex, nofollow">' in html
