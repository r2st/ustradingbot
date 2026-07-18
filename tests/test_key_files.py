"""Tests for loading API keys from loose keys/ files into Settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import Settings, _is_placeholder
from dashboard.provider_control import provider_status, switch_provider


def _with_key_file(monkeypatch, tmp_path: Path, value: str) -> Path:
    """Create keys/polygon_api_key with *value* and point Settings at it."""
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "polygon_api_key").write_text(value)
    monkeypatch.setattr("config.settings._KEYS_DIR", keys)
    monkeypatch.delenv("USTB_SKIP_KEY_FILES", raising=False)
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    return keys


def test_polygon_key_loaded_from_file(monkeypatch, tmp_path) -> None:
    _with_key_file(monkeypatch, tmp_path, "POLYKEY-FROM-FILE\n")
    s = Settings(DATA_DIR=tmp_path)
    assert s.POLYGON_API_KEY == "POLYKEY-FROM-FILE"
    assert s.polygon_key_present is True


def test_explicit_value_beats_file(monkeypatch, tmp_path) -> None:
    _with_key_file(monkeypatch, tmp_path, "FILEKEY")
    s = Settings(DATA_DIR=tmp_path, POLYGON_API_KEY="EXPLICIT")
    assert s.POLYGON_API_KEY == "EXPLICIT"


def test_env_var_beats_file(monkeypatch, tmp_path) -> None:
    _with_key_file(monkeypatch, tmp_path, "FILEKEY")
    monkeypatch.setenv("POLYGON_API_KEY", "FROM-ENV")
    s = Settings(DATA_DIR=tmp_path)
    assert s.POLYGON_API_KEY == "FROM-ENV"


def test_skip_flag_disables_file_loading(monkeypatch, tmp_path) -> None:
    _with_key_file(monkeypatch, tmp_path, "FILEKEY")
    monkeypatch.setenv("USTB_SKIP_KEY_FILES", "1")
    s = Settings(DATA_DIR=tmp_path)
    assert s.POLYGON_API_KEY == ""


def test_missing_file_leaves_key_empty(monkeypatch, tmp_path) -> None:
    keys = tmp_path / "keys"
    keys.mkdir()  # exists, but no polygon_api_key inside
    monkeypatch.setattr("config.settings._KEYS_DIR", keys)
    monkeypatch.delenv("USTB_SKIP_KEY_FILES", raising=False)
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    s = Settings(DATA_DIR=tmp_path)
    assert s.POLYGON_API_KEY == ""


def test_empty_file_leaves_key_empty(monkeypatch, tmp_path) -> None:
    _with_key_file(monkeypatch, tmp_path, "   \n")
    s = Settings(DATA_DIR=tmp_path)
    assert s.POLYGON_API_KEY == ""


# --------------------------------------------------------------------------- #
# End-to-end: the file-loaded key makes Polygon selectable, Alpaca still needs
# a key.
# --------------------------------------------------------------------------- #


def test_provider_dropdown_reflects_file_key(monkeypatch, tmp_path) -> None:
    _with_key_file(monkeypatch, tmp_path, "POLYKEY")
    s = Settings(DATA_DIR=tmp_path)

    st = provider_status(s)
    polygon = next(p for p in st["providers"] if p["name"] == "polygon")
    alpaca = next(p for p in st["providers"] if p["name"] == "alpaca")
    assert polygon["connected"] is True
    assert polygon["needs_key"] is False
    # No Alpaca keys -> still needs a key.
    assert alpaca["connected"] is False
    assert alpaca["needs_key"] is True


def test_switch_to_polygon_allowed_with_file_key(monkeypatch, tmp_path) -> None:
    _with_key_file(monkeypatch, tmp_path, "POLYKEY")
    s = Settings(DATA_DIR=tmp_path, MARKET_DATA_PROVIDER="yfinance")
    res = switch_provider("polygon", s, env_path=tmp_path / ".env")
    assert res.ok is True
    assert "MARKET_DATA_PROVIDER=polygon" in (tmp_path / ".env").read_text()


# --------------------------------------------------------------------------- #
# OpenRouter key: loaded from keys/openrouter-key, and a .env placeholder must
# never win (that was the Analyst-page "401 Unauthorized" bug).
# --------------------------------------------------------------------------- #


def _with_openrouter_file(monkeypatch, tmp_path: Path, value: str) -> Path:
    """Create keys/openrouter-key with *value* and point Settings at it."""
    keys = tmp_path / "keys"
    keys.mkdir(exist_ok=True)
    (keys / "openrouter-key").write_text(value)
    monkeypatch.setattr("config.settings._KEYS_DIR", keys)
    monkeypatch.delenv("USTB_SKIP_KEY_FILES", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return keys


def test_openrouter_key_loaded_from_file(monkeypatch, tmp_path) -> None:
    _with_openrouter_file(monkeypatch, tmp_path, "sk-or-v1-FROM-FILE\n")
    s = Settings(DATA_DIR=tmp_path)
    assert s.OPENROUTER_API_KEY == "sk-or-v1-FROM-FILE"


def test_openrouter_placeholder_env_scrubbed_and_file_wins(monkeypatch, tmp_path) -> None:
    """A .env.example placeholder must not shadow the real key file (the 401 bug)."""
    _with_openrouter_file(monkeypatch, tmp_path, "sk-or-v1-REAL")
    s = Settings(DATA_DIR=tmp_path, OPENROUTER_API_KEY="your_openrouter_key_here")
    assert s.OPENROUTER_API_KEY == "sk-or-v1-REAL"


def test_openrouter_placeholder_scrubbed_when_no_file(monkeypatch, tmp_path) -> None:
    """With no usable key file, a placeholder is scrubbed to empty, not sent."""
    keys = tmp_path / "keys"
    keys.mkdir()  # exists, but no openrouter-key inside
    monkeypatch.setattr("config.settings._KEYS_DIR", keys)
    monkeypatch.delenv("USTB_SKIP_KEY_FILES", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    s = Settings(DATA_DIR=tmp_path, OPENROUTER_API_KEY="your_openrouter_key_here")
    assert s.OPENROUTER_API_KEY == ""


def test_openrouter_real_env_value_beats_file(monkeypatch, tmp_path) -> None:
    """A real (non-placeholder) env/.env key still wins over the file."""
    _with_openrouter_file(monkeypatch, tmp_path, "sk-or-v1-FROM-FILE")
    s = Settings(DATA_DIR=tmp_path, OPENROUTER_API_KEY="sk-or-v1-EXPLICIT")
    assert s.OPENROUTER_API_KEY == "sk-or-v1-EXPLICIT"


def test_placeholder_file_ignored(monkeypatch, tmp_path) -> None:
    """A placeholder sitting in the key file itself is ignored too."""
    _with_openrouter_file(monkeypatch, tmp_path, "your_openrouter_key_here")
    s = Settings(DATA_DIR=tmp_path)
    assert s.OPENROUTER_API_KEY == ""


@pytest.mark.parametrize(
    "value,expected",
    [
        ("", True),
        ("   ", True),
        ("your_openrouter_key_here", True),
        ("YOUR_API_KEY_HERE", True),
        ("changeme", True),
        ("placeholder", True),
        ("sk-or-v1-08ef8realkey", False),
        ("POLYKEY-123", False),
    ],
)
def test_is_placeholder(value, expected) -> None:
    assert _is_placeholder(value) is expected
