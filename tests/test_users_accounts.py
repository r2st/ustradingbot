"""Unit tests for user accounts and API-key stores (features 16 & 19)."""

from __future__ import annotations

import time

import pytest

from dashboard.api_keys import ApiKeyStore
from users.accounts import (
    AccountError,
    UserProfile,
    UserStore,
    validate_password,
    validate_username,
)


# ------------------------------------------------------------------- accounts


def test_register_and_verify(tmp_data_dir):
    store = UserStore(tmp_data_dir)
    store.register("alice", "password1", UserProfile(capital=5000, strategies=["swing"]))
    assert store.verify_password("alice", "password1")
    assert not store.verify_password("alice", "wrong")
    assert not store.verify_password("nobody", "password1")


def test_password_is_hashed_on_disk(tmp_data_dir):
    UserStore(tmp_data_dir).register("bob", "supersecret")
    raw = (tmp_data_dir / "users.json").read_text()
    assert "supersecret" not in raw  # only the PBKDF2 hash is stored


@pytest.mark.parametrize("bad", ["ab", "has space", "x" * 40, "no@sign"])
def test_username_validation(bad):
    with pytest.raises(AccountError):
        validate_username(bad)


def test_password_min_length():
    with pytest.raises(AccountError):
        validate_password("short")


def test_duplicate_registration(tmp_data_dir):
    store = UserStore(tmp_data_dir)
    store.register("carol", "password1")
    with pytest.raises(AccountError):
        store.register("carol", "password2")


def test_login_token_and_expiry(tmp_data_dir, monkeypatch):
    store = UserStore(tmp_data_dir)
    store.register("dave", "password1")
    token = store.login("dave", "password1")
    assert store.validate_token(token) == "dave"

    # Expire the session by advancing the monotonic clock.
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + 999_999)
    assert store.validate_token(token) is None


def test_login_bad_credentials(tmp_data_dir):
    store = UserStore(tmp_data_dir)
    store.register("erin", "password1")
    with pytest.raises(AccountError):
        store.login("erin", "nope")


def test_profile_roundtrip(tmp_data_dir):
    store = UserStore(tmp_data_dir)
    store.register("frank", "password1")
    store.update_profile("frank", UserProfile(capital=7777, strategies=["momentum"], watchlist=["nvda"]))
    prof = store.get_profile("frank")
    assert prof.capital == 7777 and prof.strategies == ["momentum"] and prof.watchlist == ["NVDA"]


def test_profile_drops_invalid_strategies(tmp_data_dir):
    prof = UserProfile.from_dict({"strategies": ["swing", "not_a_strategy"], "capital": 1})
    assert prof.strategies == ["swing"]


# ------------------------------------------------------------------- api keys


def test_api_key_create_verify_revoke(tmp_data_dir):
    store = ApiKeyStore(tmp_data_dir)
    raw, info = store.create("primary")
    assert raw.startswith("ustb_")
    assert store.verify(raw)
    assert not store.verify("ustb_wrongkey")

    raw_on_disk = (tmp_data_dir / "api_keys.json").read_text()
    assert raw not in raw_on_disk  # only the hash is stored

    assert store.revoke(info.key_id)
    assert not store.verify(raw)
    assert not store.revoke("missing")


def test_api_key_list(tmp_data_dir):
    store = ApiKeyStore(tmp_data_dir)
    store.create("a")
    store.create("b")
    assert len(store.list_keys()) == 2
