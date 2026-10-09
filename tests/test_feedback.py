"""Tests for the feedback widget and /api/feedback endpoint."""

from __future__ import annotations

import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard.app import app

client = TestClient(app, raise_server_exceptions=False)


class TestFeedbackEndpoint:
    def test_submit_feedback_success(self, tmp_path):
        with patch("dashboard.feedback_router._FEEDBACK_DIR", tmp_path):
            r = client.post(
                "/api/feedback",
                json={"message": "Great tool!", "category": "praise", "page": "/tools"},
            )
        assert r.status_code == 201
        assert r.json() == {"ok": True}
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["message"] == "Great tool!"
        assert data["category"] == "praise"
        assert data["page"] == "/tools"
        assert "ts" in data

    def test_submit_feedback_empty_message(self):
        r = client.post("/api/feedback", json={"message": ""})
        assert r.status_code == 400
        assert "required" in r.json()["error"]

    def test_submit_feedback_missing_message(self):
        r = client.post("/api/feedback", json={"category": "bug"})
        assert r.status_code == 400

    def test_submit_feedback_message_too_long(self):
        r = client.post("/api/feedback", json={"message": "x" * 2001})
        assert r.status_code == 400
        assert "too long" in r.json()["error"]

    def test_submit_feedback_defaults(self, tmp_path):
        with patch("dashboard.feedback_router._FEEDBACK_DIR", tmp_path):
            r = client.post("/api/feedback", json={"message": "hello"})
        assert r.status_code == 201
        data = json.loads(list(tmp_path.glob("*.json"))[0].read_text())
        assert data["category"] == "general"

    def test_feedback_no_auth_required(self):
        r = client.post("/api/feedback", json={"message": "test"})
        assert r.status_code != 401


class TestFeedbackWidget:
    def test_landing_contains_widget(self):
        r = client.get("/")
        assert r.status_code == 200
        assert "fb-widget" in r.text
        assert "/api/feedback" in r.text

    def test_tools_page_contains_widget(self):
        r = client.get("/tools")
        assert r.status_code == 200
        assert "fb-widget" in r.text


class TestUmamiAnalytics:
    def test_landing_has_umami_script(self):
        r = client.get("/")
        assert r.status_code == 200
        assert "analytics.doaide.com/script.js" in r.text
        assert "32893208-8775-4a86-937f-4542984e7a7e" in r.text

    def test_tools_page_has_umami_script(self):
        r = client.get("/tools")
        assert r.status_code == 200
        assert "analytics.doaide.com/script.js" in r.text

    def test_login_has_umami_script(self):
        r = client.get("/login")
        assert r.status_code == 200
        assert "analytics.doaide.com/script.js" in r.text
