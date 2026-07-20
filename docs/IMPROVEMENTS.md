# USTradingBot — Security & Reliability Improvements

Tracking doc for the hardening audit. This session implemented **all P0 items**;
each is verified by tests and shipped to production. Related B-series items
(B5/B7/B13) are being implemented in parallel and are noted for context.

Status legend: ✅ Done · 🚧 In progress · ⬜ Planned

---

## P0 — Critical (this release)

| ID | Item | Status |
|----|------|--------|
| **B1** | Gate `/docs`, `/redoc`, `/openapi.json` behind auth | ✅ Done |
| **B2** | Rate limiting on money/control paths + login lockout | ✅ Done |
| **B6** | Pydantic request models for all mutating endpoints | ✅ Done |
| **F1** | Surface frontend fetch errors (no more silent `catch {}`) | ✅ Done |
| **T1** | Manual-trade integration tests | ✅ Done |
| **O1** | CI/CD with GitHub Actions (pytest + lint + coverage) | ✅ Done |

### B1 — Auth-gated API documentation
FastAPI's interactive docs and the raw OpenAPI schema leaked the entire API
surface to anonymous callers. The built-in routes are now disabled
(`docs_url=None, redoc_url=None, openapi_url=None`) and re-served behind the same
`require_auth` HTTP Basic guard as every other page.

- Code: `dashboard/app.py` (`/openapi.json`, `/docs`, `/redoc` handlers).
- Tests: `tests/test_docs_gating.py` — 401 without creds when auth is on, 200
  with creds, schema still complete.

### B2 — Rate limiting + brute-force lockout
In-process, per-client-IP protection (no external store needed for a single
uvicorn process behind a proxy).

- **Rate limiter** (`dashboard/rate_limit.py`): sliding-window cap applied to the
  money path (`/api/manual-trade`, `/api/positions/stop`) and control endpoints
  (`/api/engine/control`, `/api/mode/switch`, `/api/providers/*`,
  `/api/backtest/run`, `/api/trade-selection`). Over-limit → `429` + `Retry-After`.
- **Login lockout**: failed HTTP Basic auth attempts are counted per IP in
  `require_auth`; after `RATE_LIMIT_LOGIN_MAX_FAILURES` (default 5) the IP is
  locked for `RATE_LIMIT_LOGIN_LOCKOUT_MINUTES` (default 15). A success clears
  the counter.
- Tunable via settings; disabled under the test-suite via `RATE_LIMIT_ENABLED`.
- Tests: `tests/test_rate_limit.py`.

### B6 — Pydantic request models
Every mutating POST/PUT endpoint now declares a typed `BaseModel` body instead of
raw `await request.json()`, giving edge type-checking, bounds enforcement, and
auto-generated OpenAPI schemas. Models live in `dashboard/schemas.py`.

- **Money path is strict**: `ManualTradeRequest` enforces a valid symbol pattern,
  `quantity > 0`, `entry_price > 0`, etc. → `422` before any broker code runs.
  `PositionStopRequest` likewise.
- **Control endpoints**: `ModeSwitchRequest`, `EngineControlRequest`,
  `ProviderSelectRequest`, `ProviderKeysRequest`, `BacktestRunRequest`,
  `TradeSelectionRequest`.
- **CRUD routers**: watchlist, universe, alerts, notes, push/notifications,
  users, and REST-API-v1 key management. Downstream domain validation (which
  returns `400`) is preserved; the models add structure/type/bounds on top.
- Tests: `tests/test_manual_trade_api.py` plus the existing router suites, all
  green.

### F1 — Surface frontend fetch errors
The dashboard's shared `jsend()` (mutations) now routes failures through a new
`reportApiError()` toast surface, so a failed save/trade/control action gives the
user visible, de-duplicated feedback instead of a silent `catch (e) {}`. Polling
reads (`jget`) stay quiet by default (transient blips self-heal) but can opt in
with `{report:true}`.

- Code: `dashboard/templates/dashboard.html`.
- Tests: `tests/test_frontend_error_surface.py`.

### T1 — Manual-trade integration tests
`tests/test_manual_trade_api.py` exercises the endpoint end-to-end: happy path,
`401` without auth, `403` without/with a wrong admin password, and `422` for
missing symbol, malformed symbol, and negative/zero quantity.

### O1 — CI/CD
`.github/workflows/ci.yml` runs on push to `main` and PRs across Python 3.11/3.12:
installs deps into a venv, runs a focused `ruff` lint (correctness rules), and
runs `pytest` with coverage (`--cov`, XML artifact uploaded).

---

## Related B-series items (in progress, parallel work)

| ID | Item | Status |
|----|------|--------|
| **B5** | Request body size limit (413 on oversized bodies) | 🚧 In progress |
| **B7** | Consistent bad-JSON handling (422 not 500) | 🚧 In progress |
| **B13** | Standard response envelope / error shape | 🚧 In progress |

These are being implemented alongside the P0 work (`dashboard/http_util.py`,
`dashboard/middleware.py`).

---

## Verification

- Full suite: `.venv/bin/python -m pytest tests/` → **all green**.
- Lint: `.venv/bin/ruff check .` → clean.
- New coverage: docs gating, rate limiting + lockout, manual-trade HTTP
  validation, frontend error surface.
