"""Tests for the viral public pages (tools, blog, embed, sitemap, robots)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dashboard.app import app

client = TestClient(app, raise_server_exceptions=False)


class TestToolPages:
    def test_tools_index_200(self):
        r = client.get("/tools")
        assert r.status_code == 200
        assert "Position Size Calculator" in r.text

    def test_position_size_calculator_200(self):
        r = client.get("/tools/position-size-calculator")
        assert r.status_code == 200
        assert "Account Size" in r.text

    def test_profit_loss_calculator_200(self):
        r = client.get("/tools/profit-loss-calculator")
        assert r.status_code == 200
        assert "Entry Price" in r.text

    def test_risk_reward_calculator_200(self):
        r = client.get("/tools/risk-reward-calculator")
        assert r.status_code == 200
        assert "Take Profit" in r.text

    def test_tools_no_auth_required(self):
        for path in ["/tools", "/tools/position-size-calculator", "/tools/profit-loss-calculator", "/tools/risk-reward-calculator"]:
            r = client.get(path)
            assert r.status_code != 401, f"{path} should not require auth"

    def test_tools_contain_json_ld(self):
        for path in ["/tools/position-size-calculator", "/tools/profit-loss-calculator", "/tools/risk-reward-calculator"]:
            r = client.get(path)
            assert "application/ld+json" in r.text, f"{path} missing JSON-LD"

    def test_tools_contain_share_buttons(self):
        for path in ["/tools/position-size-calculator", "/tools/profit-loss-calculator", "/tools/risk-reward-calculator"]:
            r = client.get(path)
            assert "WhatsApp" in r.text
            assert "Twitter/X" in r.text


class TestBlogPages:
    def test_blog_index_200(self):
        r = client.get("/blog")
        assert r.status_code == 200
        assert "Blog" in r.text

    def test_blog_index_lists_articles(self):
        r = client.get("/blog")
        assert "AI Trading Bots" in r.text
        assert "Risk Management" in r.text
        assert "Algorithmic Trading" in r.text

    def test_blog_post_ai_trading(self):
        r = client.get("/blog/ai-trading-bots-retail-investing")
        assert r.status_code == 200
        assert "How AI Trading Bots Are Changing Retail Investing" in r.text

    def test_blog_post_risk_management(self):
        r = client.get("/blog/risk-management-rules-traders")
        assert r.status_code == 200
        assert "5 Risk Management Rules" in r.text

    def test_blog_post_algo_guide(self):
        r = client.get("/blog/algorithmic-trading-beginners-guide")
        assert r.status_code == 200
        assert "Complete Guide to Algorithmic Trading" in r.text

    def test_blog_unknown_slug_404(self):
        r = client.get("/blog/nonexistent-slug")
        assert r.status_code == 404

    def test_blog_posts_contain_json_ld(self):
        r = client.get("/blog/ai-trading-bots-retail-investing")
        assert "application/ld+json" in r.text

    def test_blog_no_auth_required(self):
        r = client.get("/blog")
        assert r.status_code != 401


class TestEmbedPage:
    def test_embed_200(self):
        r = client.get("/embed")
        assert r.status_code == 200
        assert "Embed" in r.text

    def test_embed_no_auth(self):
        r = client.get("/embed")
        assert r.status_code != 401


class TestSEO:
    def test_sitemap_xml(self):
        r = client.get("/sitemap.xml")
        assert r.status_code == 200
        assert "application/xml" in r.headers.get("content-type", "")
        assert "<urlset" in r.text
        assert "trade.doaide.com/tools" in r.text

    def test_robots_txt(self):
        r = client.get("/robots.txt")
        assert r.status_code == 200
        assert "User-agent" in r.text
        assert "Sitemap:" in r.text
