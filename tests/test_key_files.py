"""Tests for loading API keys from loose keys/ files into Settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import Settings
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
