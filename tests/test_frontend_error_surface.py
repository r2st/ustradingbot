"""
Static checks for the frontend fetch-error surface (audit item F1).

The dashboard's ``jsend`` (mutations) now routes failures through
``reportApiError`` so a failed save/trade/control action is no longer swallowed
by a bare ``catch (e) {}``.  These assertions guard that wiring against
regressions without needing a browser.
"""

from __future__ import annotations

from pathlib import Path

_HTML = (
    Path(__file__).resolve().parent.parent
    / "dashboard"
    / "templates"
    / "dashboard.html"
).read_text(encoding="utf-8")


def test_report_api_error_helper_exists():
    assert "function reportApiError(" in _HTML
    assert "window.reportApiError = reportApiError" in _HTML
    # De-duplication so a flaky network / repeated action can't spam toasts.
    assert "_TOAST_DEDUP_MS" in _HTML


def test_jsend_surfaces_errors_by_default():
    # jsend must report on failure unless the caller opts out with {quiet:true}.
    idx = _HTML.find("async function jsend(method, url, body, opts)")
    assert idx != -1, "jsend signature not found / not extended with opts"
    body = _HTML[idx:idx + 700]
    assert "reportApiError(" in body
    assert "opts && opts.quiet" in body


def test_jget_is_quiet_by_default_for_pollers():
    idx = _HTML.find("async function jget(url, opts)")
    assert idx != -1
    # Window widened: jget now also carries the per-key AbortController logic
    # (F-4) between the signature and the report gate.
    body = _HTML[idx:idx + 1500]
    # Only reports when a caller explicitly opts in (user-initiated loads).
    assert "opts && opts.report" in body
