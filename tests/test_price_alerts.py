"""P2f — user-defined price alerts."""

from __future__ import annotations

import types

import pytest

from alerts import price_alerts
from alerts.price_alerts import (
    PriceAlertError,
    check_price_alerts,
    get_price_alert_store,
)


def _settings(tmp_path):
    return types.SimpleNamespace(DATA_DIR=tmp_path)


@pytest.fixture(autouse=True)
def _clear_store_cache():
    # The module memoises stores by data-dir path; clear between tests.
    price_alerts._stores.clear()
    yield
    price_alerts._stores.clear()


# ---------------------------------------------------------------------------
# CRUD + validation
# ---------------------------------------------------------------------------


def test_add_list_delete(tmp_path):
    store = get_price_alert_store(tmp_path)
    rule = store.add_alert("aapl", "above", 200)
    assert rule["symbol"] == "AAPL"
    assert rule["direction"] == "above"
    assert rule["threshold"] == 200.0
    assert rule["active"] is True and rule["triggered_at"] is None

    assert len(store.list_alerts()) == 1
    assert store.delete_alert(rule["id"]) is True
    assert store.list_alerts() == []
    assert store.delete_alert("nope") is False


@pytest.mark.parametrize("kwargs", [
    {"symbol": "", "direction": "above", "threshold": 10},
    {"symbol": "bad!sym", "direction": "above", "threshold": 10},
    {"symbol": "AAPL", "direction": "sideways", "threshold": 10},
    {"symbol": "AAPL", "direction": "above", "threshold": 0},
    {"symbol": "AAPL", "direction": "above", "threshold": -5},
    {"symbol": "AAPL", "direction": "above", "threshold": "abc"},
])
def test_validation_errors(tmp_path, kwargs):
    store = get_price_alert_store(tmp_path)
    with pytest.raises(PriceAlertError):
        store.add_alert(**kwargs)


def test_persistence_across_reloads(tmp_path):
    get_price_alert_store(tmp_path).add_alert("MSFT", "below", 300)
    price_alerts._stores.clear()  # force a fresh store reading from disk
    reloaded = get_price_alert_store(tmp_path)
    assert len(reloaded.list_alerts()) == 1
    assert reloaded.list_alerts()[0]["symbol"] == "MSFT"


# ---------------------------------------------------------------------------
# check_price_alerts
# ---------------------------------------------------------------------------


def test_above_cross_triggers_and_publishes(tmp_path, monkeypatch):
    published = []
    monkeypatch.setattr(
        "dashboard.push.publish",
        lambda *a, **k: published.append((a, k)),
    )
    store = get_price_alert_store(tmp_path)
    rule = store.add_alert("AAPL", "above", 200)

    triggered = check_price_alerts(
        _settings(tmp_path), price_fetcher=lambda syms: {"AAPL": 201.0}
    )
    assert len(triggered) == 1
    assert triggered[0]["id"] == rule["id"]
    assert published  # a notification was dispatched
    # Persisted as triggered.
    assert store.list_alerts()[0]["triggered_at"] is not None
    assert store.list_alerts()[0]["last_price"] == 201.0


def test_below_cross_triggers(tmp_path):
    store = get_price_alert_store(tmp_path)
    store.add_alert("MSFT", "below", 300)
    triggered = check_price_alerts(
        _settings(tmp_path), price_fetcher=lambda syms: {"MSFT": {"price": 299.5}}
    )
    assert len(triggered) == 1


def test_no_trigger_when_not_crossed(tmp_path):
    store = get_price_alert_store(tmp_path)
    store.add_alert("AAPL", "above", 200)
    triggered = check_price_alerts(
        _settings(tmp_path), price_fetcher=lambda syms: {"AAPL": 150.0}
    )
    assert triggered == []
    # last_price still recorded.
    assert store.list_alerts()[0]["last_price"] == 150.0


def test_triggered_alert_does_not_refire(tmp_path):
    store = get_price_alert_store(tmp_path)
    store.add_alert("AAPL", "above", 200)
    s = _settings(tmp_path)
    assert len(check_price_alerts(s, price_fetcher=lambda x: {"AAPL": 201})) == 1
    # Second check with the price still above must NOT re-trigger.
    assert check_price_alerts(s, price_fetcher=lambda x: {"AAPL": 205}) == []


def test_toggle_rearms(tmp_path):
    store = get_price_alert_store(tmp_path)
    rule = store.add_alert("AAPL", "above", 200)
    s = _settings(tmp_path)
    check_price_alerts(s, price_fetcher=lambda x: {"AAPL": 201})
    assert store.list_alerts()[0]["triggered_at"] is not None
    # Re-arm and it fires again.
    store.set_active(rule["id"], True)
    assert store.list_alerts()[0]["triggered_at"] is None
    assert len(check_price_alerts(s, price_fetcher=lambda x: {"AAPL": 201})) == 1


def test_inactive_alert_not_checked(tmp_path):
    store = get_price_alert_store(tmp_path)
    rule = store.add_alert("AAPL", "above", 200)
    store.set_active(rule["id"], False)
    assert check_price_alerts(_settings(tmp_path),
                              price_fetcher=lambda x: {"AAPL": 500}) == []


def test_none_price_skipped(tmp_path):
    store = get_price_alert_store(tmp_path)
    store.add_alert("AAPL", "above", 200)
    triggered = check_price_alerts(
        _settings(tmp_path), price_fetcher=lambda syms: {"AAPL": None}
    )
    assert triggered == []
    assert store.list_alerts()[0]["triggered_at"] is None


def test_fetcher_raising_is_safe(tmp_path):
    store = get_price_alert_store(tmp_path)
    store.add_alert("AAPL", "above", 200)

    def boom(syms):
        raise RuntimeError("provider down")

    assert check_price_alerts(_settings(tmp_path), price_fetcher=boom) == []
