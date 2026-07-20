"""
Tests for the shared (multi-worker) rate-limit / lockout backend (audit B-4).

Uses a minimal in-memory fake that implements only the Redis command surface
the limiter touches (``incr``/``expire``/``ttl``/``set``/``exists``/``delete``/
``scan_iter``), so no real Redis is required.  The point is to prove the limiter
and lockout consult the *shared* store — the same keys every worker would hit —
rather than process-local dicts.
"""

from __future__ import annotations

import time

import pytest
import structlog

from dashboard import rate_limit


class FakeRedis:
    """A tiny, single-process stand-in for redis-py (string ops + TTL)."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.expiry: dict[str, float] = {}

    def _expired(self, key):
        exp = self.expiry.get(key)
        if exp is not None and time.time() >= exp:
            self.store.pop(key, None)
            self.expiry.pop(key, None)
            return True
        return False

    def incr(self, key):
        self._expired(key)
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    def expire(self, key, seconds):
        if key in self.store:
            self.expiry[key] = time.time() + seconds

    def ttl(self, key):
        if self._expired(key) or key not in self.store:
            return -2
        exp = self.expiry.get(key)
        return int(exp - time.time()) if exp else -1

    def set(self, key, value, ex=None):
        self.store[key] = str(value)
        if ex:
            self.expiry[key] = time.time() + ex

    def exists(self, key):
        return 0 if self._expired(key) else int(key in self.store)

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)
            self.expiry.pop(k, None)

    def scan_iter(self, match="*"):
        import fnmatch

        return [k for k in list(self.store) if fnmatch.fnmatch(k, match)]


@pytest.fixture
def fake_redis(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "True")
    from config.settings import get_settings

    get_settings.cache_clear()
    client = FakeRedis()
    rate_limit.set_redis_client(client)
    yield client
    rate_limit.set_redis_client(None)
    rate_limit.reset()


def test_bucket_uses_shared_store(fake_redis):
    # 3 hits allowed, 4th over the fixed-window cap.
    results = [rate_limit._check_bucket("b", "1.2.3.4", 3, 60.0) for _ in range(4)]
    allowed = [r[0] for r in results]
    assert allowed == [True, True, True, False]
    assert results[-1][1] >= 1  # Retry-After from the key TTL
    # The counter lives in the shared store, not the in-process dict.
    assert any(k.startswith("rl:b:1.2.3.4:") for k in fake_redis.store)
    assert not rate_limit._hits  # in-process path untouched


def test_lockout_uses_shared_store(fake_redis, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_LOGIN_MAX_FAILURES", "3")
    monkeypatch.setenv("RATE_LIMIT_LOGIN_LOCKOUT_MINUTES", "15")
    from config.settings import get_settings

    get_settings.cache_clear()

    key = "9.9.9.9"
    # Not locked initially.
    rate_limit.LoginGuard.check_locked(key)
    for _ in range(3):
        rate_limit.LoginGuard.record_failure(key)
    # Now locked → check_locked raises 429.
    with pytest.raises(Exception) as exc:
        rate_limit.LoginGuard.check_locked(key)
    assert getattr(exc.value, "status_code", None) == 429
    assert f"rll:{key}" in fake_redis.store
    # A success clears both shared keys.
    rate_limit.LoginGuard.record_success(key)
    assert f"rll:{key}" not in fake_redis.store
    rate_limit.LoginGuard.check_locked(key)  # no longer raises


def test_reset_clears_shared_keys(fake_redis):
    rate_limit._check_bucket("b", "1.1.1.1", 5, 60.0)
    assert fake_redis.store
    rate_limit.reset()
    assert not fake_redis.store


def test_check_fails_open_on_backend_error(monkeypatch):
    class Broken:
        def incr(self, *a):
            raise RuntimeError("redis down")

    monkeypatch.setenv("RATE_LIMIT_ENABLED", "True")
    from config.settings import get_settings

    get_settings.cache_clear()
    rate_limit.set_redis_client(Broken())
    try:
        allowed, retry = rate_limit._check_bucket("b", "k", 1, 60.0)
        assert allowed is True and retry == 0  # fail open, never 500
    finally:
        rate_limit.set_redis_client(None)


def test_warn_if_multiworker(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    rate_limit.set_redis_client(None)
    with structlog.testing.capture_logs() as logs:
        rate_limit.warn_if_multiworker()
    events = {e.get("event") for e in logs}
    assert "rate_limit.multiworker_without_shared_store" in events


def test_no_warn_single_worker(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    rate_limit.set_redis_client(None)
    with structlog.testing.capture_logs() as logs:
        rate_limit.warn_if_multiworker()
    events = {e.get("event") for e in logs}
    assert "rate_limit.multiworker_without_shared_store" not in events
