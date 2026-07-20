# Changelog

All notable changes to the US Trading Bot are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Production-readiness audit remediation (P1–P3 items).

### Added

- **Tax / realized-gains reporting** (`analytics/tax.py`, `/api/tax/*`): FIFO
  cost-basis, short-term vs long-term split, and wash-sale flagging from the
  trade journal.
- **User-defined price alerts** (`alerts/price_alerts.py`, `/api/price-alerts/*`):
  "notify when AAPL crosses $200" rules checked against live prices each engine
  cycle and delivered through the existing notification system.
- **Portfolio beta vs SPY** and market-relative drawdown in the Risk Dashboard
  (`analytics/risk_dashboard.py`, additive `beta` block on `/api/risk/report`).
- **Deeper trade journaling** (`journal/notes.py`): setup-type tags, mistake
  tags, structured post-mortem fields (what worked / went wrong / lesson /
  rating), and searchable/filterable notes with a `/api/notes/facets` endpoint.
- **Request body-size limit** (B5): requests over 1 MiB are rejected with `413`.
- **Shared response/error envelope** (B13): every error response uses one
  `{ok, error, detail}` shape via `dashboard/http_util.py` +
  `dashboard/middleware.py`.
- **Coverage tooling** (T4): `.coveragerc` + `scripts/coverage.sh`
  (term / HTML / XML reports; ~82% line coverage).
- **Tests** (T5): AI-layer failure/fallback paths and the P&L WebSocket
  error/close path, plus tests for every new feature above.
- `CONTRIBUTING.md` and this changelog (D5).

### Changed

- **Consistent bad-JSON handling** (B7): malformed request bodies return `422`
  instead of a `500`; unguarded `await request.json()` calls now route through a
  shared parser (or Pydantic schemas).
- **Narrower exception handling** (B11): fail-open `except Exception` blocks in
  the AI commentary, quotes, earnings, and insights layers now log at
  WARN/DEBUG so silent failures are visible.
- **Cached journal CSV reads** (B14): `analytics.performance.load_completed_trades`
  is memoised with an mtime+size guard, so the many per-poll analytics/risk/
  export reads no longer re-parse an unchanged `trades.csv`.
- **`SYSTEM_DESIGN.md` reconciled with the tree** (D4): removed references to
  files that were never built, added the dashboard/analytics/alerts subsystems,
  and corrected the AI model (OpenRouter, not Claude Sonnet) and API-key name
  (`OPENROUTER_API_KEY`, not `ANTHROPIC_API_KEY`).

### Frontend

- Loading states for polled panels, ARIA live regions and table semantics,
  modal focus traps + ESC-to-close, an `innerHTML`/XSS audit, timer cleanup on
  section change/unload, Chart.js empty states, and mobile-responsive scroll
  containers for dense tables (F3–F10).

---

_Earlier history predates this changelog; see the git log for prior feature
work (dashboard, memory & learning layer, tiered universe, earnings/ETF/AH
suite, real-time P&L WebSocket)._
