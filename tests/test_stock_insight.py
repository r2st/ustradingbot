"""The plain-English stock read and its Gemini layer."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from dashboard import stock_insight

ROW = {
    "symbol": "AAPL",
    "sector": "Technology",
    "price": 200.0,
    "change_pct": 1.25,
    "month_pct": -3.5,
    "rsi": 75.2,
    "above_sma50": True,
    "off_high_pct": -0.5,
}

SETTINGS = SimpleNamespace(
    GEMINI_API_KEY="test-key",
    GEMINI_BASE_URL="https://gemini.test/v1beta/openai",
    GEMINI_MODEL="gemini-flash-latest",
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    stock_insight.clear_cache()
    yield
    stock_insight.clear_cache()


class TestTechnicalSummary:
    def test_describes_every_reading(self):
        text = " ".join(stock_insight.technical_summary(ROW))
        assert "up 1.25% on the day at $200.00" in text
        assert "-3.50%" in text
        assert "overbought" in text
        assert "above its 50-day" in text
        assert "near its 6-month high" in text

    def test_oversold_and_downtrend(self):
        row = {**ROW, "change_pct": -2.0, "rsi": 22.0, "above_sma50": False, "off_high_pct": -18.0}
        text = " ".join(stock_insight.technical_summary(row))
        assert "down 2.00%" in text
        assert "oversold" in text
        assert "below its 50-day" in text
        assert "18.0% below its 6-month high" in text

    def test_missing_readings_are_left_out(self):
        row = {"symbol": "X", "price": 1.0, "change_pct": None, "month_pct": None,
               "rsi": None, "above_sma50": None, "off_high_pct": None}
        assert stock_insight.technical_summary(row) == []


class _Resp:
    def __init__(self, content="  A calm   read. ", status=200):
        self._content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class TestAiSummary:
    def test_calls_gemini_server_side_and_caches(self, monkeypatch):
        calls = []

        def fake_post(url, headers, json, timeout):
            calls.append((url, headers, json))
            return _Resp()

        monkeypatch.setattr(stock_insight.httpx, "post", fake_post)
        assert stock_insight.ai_summary(ROW, SETTINGS) == "A calm read."
        assert stock_insight.ai_summary(ROW, SETTINGS) == "A calm read."
        assert len(calls) == 1
        url, headers, body = calls[0]
        assert url == "https://gemini.test/v1beta/openai/chat/completions"
        assert headers["Authorization"] == "Bearer test-key"
        assert body["model"] == "gemini-flash-latest"
        # The prompt carries the deterministic facts, so the model cannot drift.
        assert "200.00" in body["messages"][0]["content"]
        assert stock_insight.cached_ai_summary("AAPL") == "A calm read."

    def test_overloaded_model_falls_back_to_lite(self, monkeypatch):
        models = []

        def fake_post(url, headers, json, timeout):
            models.append(json["model"])
            return _Resp(status=503) if json["model"] == "gemini-flash-latest" else _Resp("Lite.")

        monkeypatch.setattr(stock_insight.httpx, "post", fake_post)
        assert stock_insight.ai_summary(ROW, SETTINGS) == "Lite."
        assert models == ["gemini-flash-latest", "gemini-flash-lite-latest"]

    def test_no_key_means_no_call(self, monkeypatch):
        monkeypatch.setattr(stock_insight.httpx, "post", pytest.fail)
        settings = SimpleNamespace(GEMINI_API_KEY="", GEMINI_BASE_URL="", GEMINI_MODEL="")
        assert stock_insight.ai_summary(ROW, settings) is None

    def test_failure_falls_back_and_is_briefly_remembered(self, monkeypatch):
        calls = []

        def failing(*a, **k):
            calls.append(1)
            return _Resp(status=400)

        monkeypatch.setattr(stock_insight.httpx, "post", failing)
        assert stock_insight.ai_summary(ROW, SETTINGS) is None
        assert stock_insight.ai_summary(ROW, SETTINGS) is None
        assert len(calls) == 1

    def test_cache_peek_never_calls(self, monkeypatch):
        monkeypatch.setattr(stock_insight.httpx, "post", pytest.fail)
        assert stock_insight.cached_ai_summary("AAPL") is None
