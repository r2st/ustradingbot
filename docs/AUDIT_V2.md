# USTradingBot — Audit V2 (Remaining Improvements)

**Date:** 2026-07-20
**Scope:** Second-pass audit of the dashboard backend (`dashboard/`, `engine.py`)
and the single-page frontend (`dashboard/templates/dashboard.html`, ~7,300 lines).

This is a **findings-only** report — nothing here has been implemented.

## Context

The first audit (see [`IMPROVEMENTS.md`](IMPROVEMENTS.md)) already shipped, and
this report deliberately does **not** re-raise any of it:

- Auth-gated `/docs` / `/redoc` / `/openapi.json`
- Rate limiting on money/control paths + per-IP login lockout
- Pydantic request models on every mutating endpoint
- Request body size limit (413), consistent bad-JSON→422, standard `{ok, error}` envelope
- Security-headers middleware (CSP, HSTS, X-Frame-Options, nosniff, referrer policy)
- Per-request correlation IDs + structured request logging (`structlog`)
- Constant-time auth comparison (`secrets.compare_digest`), fail-closed on missing password
- Frontend fetch-error toast surface (`reportApiError`), WebSocket auth + reconnect,
  visibility-aware polling, dark/light theme toggle, mobile hamburger nav
- CI/CD (GitHub Actions: pytest + ruff + coverage ~82%), ~110 test files

**Bottom line:** the app is genuinely well-hardened. There are **no remaining
true P0s**. The highest-value remaining work is a handful of P1 items
(LLM-endpoint rate limiting, a money-path audit trail, a real readiness probe,
and table accessibility). Everything else is P2/P3 polish.

---

## Priority summary

| ID | Pri | Area | Item |
|----|-----|------|------|
| B-1 | P1 | Backend / Security | AI/LLM endpoints (`/api/ai/refresh`, commentary) are unthrottled — cost-abuse & DoS vector |
| B-2 | P1 | Backend / Observability | No audit-trail log for the manual-trade money path |
| B-3 | P1 | Backend / Ops | `/health` is liveness-only; no readiness probe, `/metrics`, or `/version` |
| F-1 | P1 | Frontend / A11y | 31 data tables (221 `<th>`) have no `scope=`/`<caption>` — unusable with screen readers |
| B-4 | P2 | Backend / Security | Rate limiter & lockout are in-process only — silently ineffective under >1 worker |
| B-5 | P2 | Backend / Security | No CSRF defense for cookie-less-but-browser-auto-sent HTTP Basic on state-changing GET-less endpoints |
| B-6 | P2 | Backend / Reliability | Provider/LLM keys persisted to `.env` in plaintext; engine restart is fire-and-forget |
| B-7 | P2 | Backend / Testing | No tests for the WS lifecycle disconnect path, AI commentary rendering, or multi-worker limiter |
| F-2 | P2 | Frontend / A11y | `role="dialog"` modals have no focus trap / focus restore / `aria-modal`; no skip-to-content link |
| F-3 | P2 | Frontend / UX | Loading uses bare "Loading…" text — no skeletons; some sections lack an explicit empty state |
| F-4 | P2 | Frontend / Perf | 353 KB monolithic HTML, no minify/split/service-worker; only 1 `AbortController` → stale-response races |
| B-8 | P3 | Backend / API | Response-envelope migration is half-done — new `{ok,data}` vs legacy bare dicts is inconsistent |
| B-9 | P3 | Backend / Logging | No log rotation guidance; `except Exception: pass` swallows a handful of paths silently |
| F-5 | P3 | Frontend / A11y | Theme ignores OS `prefers-color-scheme` on first load; 27 `onclick` on non-focusable elements |
| F-6 | P3 | Frontend / Data-viz | Missing dashboard views: sector/strategy exposure, per-strategy P&L, correlation, trade-timing |
| G-1 | P3 | General | Feature gaps a trading dashboard typically has (see §"Feature gaps") |

---

# BACKEND

## B-1 — [P1] AI/LLM endpoints are not rate-limited (cost + DoS)

`dashboard/ai_router.py:92` exposes `POST /api/ai/refresh`, which drives
OpenRouter LLM calls (`dashboard/ai_commentary.py:1157`, real paid API). The
route is gated by `require_auth` but carries **no `rate_limit` dependency**,
unlike the trade/control paths. `GET /api/ai/commentary` and
`/api/ai/market-overview` similarly trigger refreshes.

- **Why it matters:** a single authenticated session (or a leaked/shared basic
  credential) can hammer refresh and run up an unbounded OpenRouter bill, or
  saturate the event loop with slow outbound LLM calls. This is exactly the
  class of endpoint the existing limiter was built for, but it was not applied
  here.
- **Where:** `dashboard/ai_router.py:60,76,92` (compare with the `Depends(rate_limit(...))`
  wiring in `dashboard/manual_trade_router.py:22`).
- **Fix direction:** add a `rate_limit("ai_refresh", control=True)` (or a
  dedicated low per-minute cap) to the refresh/commentary routes; consider a
  short server-side cache TTL so repeated GETs don't each hit the LLM.

## B-2 — [P1] No audit log on the manual-trade money path

`dashboard/engine_control.py:275` (`engine_control.action`) and
`dashboard/mode_control.py:168` both emit a structured audit line for their
money/control actions. **`dashboard/manual_trade_router.py:23` (`place`) does
not** — it validates, checks the admin password, and places a live bracket
order with **no log of who placed what, for which symbol/quantity, from which
IP**. The only trace is the generic request-log line from the middleware, which
does not include the trade parameters.

- **Why it matters:** the single most sensitive action in the system (placing a
  real order) has the weakest audit trail. Post-incident ("who fired this
  trade?") there is nothing to reconstruct from.
- **Fix direction:** emit `log.info("manual_trade.placed", symbol=..., qty=...,
  side=..., ip=..., user=...)` on success and a WARN on the bad-password 403
  (mirroring `engine_control.bad_password`). Redact nothing sensitive but do log
  the trade envelope.

## B-3 — [P1] `/health` is liveness-only — no readiness / metrics / version

`dashboard/app.py:1679` `/health` returns `{status: "ok", trading_mode, broker,
timestamp}` unconditionally — it returns `ok` even if the broker is
unreachable, the data provider key is invalid, or the engine loop has died.
There is **no** `/readyz` readiness probe, **no** `/metrics` endpoint
(no Prometheus counters/histograms anywhere — confirmed by grep), and **no**
`/version` (build SHA / release) endpoint.

- **Why it matters:** an orchestrator or uptime monitor pointed at `/health` will
  report green while the bot is silently disconnected from the market. There is
  no operational visibility into cycle latency, error rates, order counts, or
  LLM spend.
- **Fix direction:** (a) a `/readyz` that actually pings broker/provider and
  reports engine-loop liveness (last-cycle timestamp); (b) a `/metrics` endpoint
  (even a lightweight hand-rolled counter set — cycles, orders, errors, LLM
  calls, provider latency); (c) `/version` returning the git SHA baked in at
  deploy time.

## B-4 — [P2] Rate limiter & login lockout are in-process only

`dashboard/rate_limit.py:37-41` keeps all counters in module-level dicts guarded
by a `threading.Lock`. The docstring correctly notes this assumes "a
single-process FastAPI app." If the deployment ever runs `uvicorn --workers N`
(or gunicorn with multiple workers), each worker keeps its own counters, so the
effective limit is `N×` the configured cap and the brute-force lockout can be
sidestepped by landing on a different worker.

- **Why it matters:** a scaling change (a natural next step for a "production"
  deploy) silently weakens two security controls with no error or warning.
- **Fix direction:** either (a) document + assert single-worker in the deploy
  unit (the systemd unit under `deploy/systemd/`), or (b) move the limiter state
  to a shared store (Redis) keyed per client. At minimum, log a startup WARN if
  `WEB_CONCURRENCY`/`--workers` > 1.

## B-5 — [P2] No CSRF defense for browser-auto-sent HTTP Basic

Auth is HTTP Basic (`dashboard/auth.py`). Browsers cache Basic credentials and
**auto-attach them to cross-site requests**, so the usual "no cookie ⇒ no CSRF"
reasoning does not fully hold. The main thing saving the app today is that the
mutating endpoints require a JSON body (Pydantic models) — a cross-site HTML
form can only send `application/x-www-form-urlencoded`/`multipart`, which those
models reject with 422. That is an *incidental* defense, not a deliberate one.

- **Why it matters:** any endpoint that accepts form-encoded input, or is
  loosened later to accept it, becomes CSRF-exploitable. The money path is
  additionally protected by the admin password, but config mutations
  (watchlist/universe/alerts add & delete — `dashboard/universe_router.py:196,
  217,247`, `dashboard/alerts_router.py:34`) are protected only by
  `require_auth`.
- **Fix direction:** add an explicit `Origin`/`Referer` same-origin check
  middleware for state-changing methods, or a double-submit CSRF token. Document
  that Basic-auth-in-browser is a deliberate CSRF-relevant choice.

## B-6 — [P2] Secrets on disk in plaintext; restart is fire-and-forget

- Provider/LLM keys are written to `.env` in plaintext by
  `dashboard/provider_control.py:220` (`update_env_var`). REST API keys are
  correctly stored **hashed** (`dashboard/api_keys.py`), so the inconsistency is
  notable: broker/LLM secrets are the higher-value ones and get the weaker
  treatment.
- After writing keys, `save_api_keys` calls `request_restart(...)` and returns
  "the engine will restart" (`provider_control.py:221`) — but this is a
  fire-and-forget signal file; nothing verifies the restart happened or reports
  failure back to the user.
- **Why it matters:** a stolen `.env` yields live broker + LLM credentials. The
  restart handshake can silently no-op, leaving the UI claiming a key change
  took effect when it didn't (this matches the `.env` config-drift gotcha
  already noted in project memory).
- **Fix direction:** at minimum document the plaintext-`.env` trust model and
  file permissions (0600); ideally encrypt at rest or read from a secrets
  manager. Surface restart success/failure back to the client.

## B-7 — [P2] Test-coverage gaps in a few high-value paths

Coverage is broad (~110 test files, ~82%). The routers that lack a *dedicated*
test file are all still exercised indirectly, so the real gaps are behavioral,
not module-level:

- **WebSocket lifecycle** (`dashboard/ws_pnl.py`): the unauthorized-close
  (`_WS_CLOSE_POLICY`, line 142) and mid-stream `WebSocketDisconnect` (line 160)
  paths — verify a rejected/expired token closes with 1008 and that a disconnect
  mid-stream is handled cleanly.
- **AI commentary rendering** (`dashboard/ai_commentary.py`, 1,223 lines;
  `analyst_cards.py`, 940 lines): the LLM-failure *fail-open* branch
  (`ai_commentary.py:1171`) — assert a 401/403 from OpenRouter degrades
  gracefully rather than 500-ing the page.
- **Multi-worker limiter** (B-4): no test asserts the single-process assumption.
- **No load/concurrency test** for the engine cycle or the WS broadcast fan-out.

## B-8 — [P3] Response-envelope migration is half-finished

`dashboard/http_util.py:156` introduced `ok()`/`fail()`/`error_body()` and the
error side is fully standardized via the exception handlers. But the docstring
explicitly says success envelopes are "opt-in for new endpoints; existing
endpoints that return bare dicts keep their shape." The result is a mixed API:
some routes return `{ok:true,data:...}`, most return bare dicts
(e.g. `/health`, `/api/mode`). This is a deliberate back-compat choice, but it
leaves the client guessing per-endpoint.

- **Fix direction:** either commit to migrating reads behind a version bump
  (`/api/v2`) or document the split so it's intentional, not accidental drift.

## B-9 — [P3] Logging polish

- A handful of `except Exception: pass` swallow errors with no log line:
  `dashboard/app.py:1743`, `backtest_control.py:106`, `push.py:276`,
  `live_router.py:258`, `ta_router.py:254`, `ws_pnl.py:166`. Most are
  intentional best-effort cleanups, but a couple (backtest/live/ta) hide real
  failures. Add a `log.debug`/`log.warning` in each so they're diagnosable.
- No log-rotation guidance in `logging_config.py` or the deploy docs — a
  long-running bot will grow logs unbounded unless the host handles it (journald
  does, but a file sink wouldn't).

---

# FRONTEND (`dashboard/templates/dashboard.html`)

The SPA is more mature than a typical single-file dashboard: viewport meta,
`<html lang>`, dark/light theme, WS reconnect, visibility-aware polling,
`aria-label`s (33), reduced-motion support, mobile hamburger nav, `reportApiError`
toast with `aria-live="polite"`, and 29 `overflow-x` table wrappers. The gaps
below are refinements, not foundational holes.

## F-1 — [P1] Data tables are not screen-reader accessible

There are 31 `<table>`s and **221 `<th>` elements but zero `scope=` attributes
and zero `<caption>`** (confirmed by grep). Screen readers cannot associate a
data cell with its column/row header, which makes the core content of a data-
dense trading dashboard effectively unusable non-visually.

- **Fix direction:** add `scope="col"`/`scope="row"` to every `<th>` and a
  `<caption>` (or `aria-label` on the table) naming each table. Cheap, high-impact.

## F-2 — [P2] Modal focus management + no skip link

`role="dialog"` appears once (line 1301) but there is **no `aria-modal`, no focus
trap, and no focus restore** (only a single `.focus()` call in the whole file,
at line 6824). Keyboard/screen-reader users can tab out of the open dialog into
the page behind it, and focus isn't returned to the trigger on close. There is
also **no skip-to-content link** for keyboard users to bypass the nav.

- **Fix direction:** trap Tab within the open dialog, set `aria-modal="true"`,
  move focus to the dialog on open and back to the invoking control on close,
  and close on `Esc`. Add a visually-hidden "Skip to main content" link.

## F-3 — [P2] Loading and empty states are inconsistent

- Loading is bare `Loading…` text (~27 occurrences) — no skeleton placeholders,
  so panels visibly jump/reflow when data arrives. `skeleton` appears 0 times.
- Empty states exist in the hot paths (114 references, e.g. "No trades match the
  filters" at line 5764) but are ad-hoc per render function; some panels render
  nothing (blank) when empty rather than an explicit "nothing yet" message.
- **Fix direction:** a shared skeleton component for table/card loads and a
  single `renderEmpty(container, message)` helper applied uniformly.

## F-4 — [P2] Monolithic payload + stale-response races

- The template is **353 KB in one file** with 12 inline `<script>` blocks and all
  CSS inline — every load parses the whole app; there's no minification, code
  splitting, or service-worker/offline caching (`serviceWorker` appears 0 times).
- Only **one `AbortController`** in the file: rapidly switching sections or
  re-filtering can let a slow earlier `fetch` resolve after a newer one and
  overwrite fresh data with stale data.
- **Fix direction:** wire an `AbortController` into the shared `jget`/`jsend`
  helpers keyed per section; consider a minified build artifact and a
  cache-busting hash so the 353 KB isn't re-downloaded each visit.

## F-5 — [P3] Theme + interaction-affordance a11y

- The theme reads only from `localStorage` and defaults to dark
  (`dashboard.html:16`); it ignores the OS `prefers-color-scheme` on first visit
  (0 occurrences). A light-mode-preferring user gets a dark flash-and-stay.
- 27 `onclick` handlers sit on non-button elements (`div`/`span`/`a`/`tr`/`td`).
  Unless each also has `role="button"` + `tabindex="0"` + a key handler (they
  don't appear to), those actions are mouse-only and invisible to keyboard/AT
  users.
- **Fix direction:** honor `prefers-color-scheme` when no stored preference
  exists; convert click-handling non-buttons to real `<button>`s or add the
  role/tabindex/keydown trio.

## F-6 — [P3] Missing data-visualization views

Existing charts are solid: intraday P&L, equity curve, returns, drawdown, win-
rate (canvases at `dashboard.html:1591,2058,2077,2086,2092,2096`). For a trading
dashboard, notable **missing** visualizations:

- **Exposure/allocation** — current holdings by sector / by strategy (pie or
  bar); gross vs net exposure over time.
- **Per-strategy P&L attribution** — which strategy (momentum / swing / short /
  selective) is making or losing money.
- **Correlation / concentration** heatmap across open positions (risk clustering).
- **Trade-timing / calendar** heatmap (P&L by hour-of-day / day-of-week).
- **Rolling risk** — rolling Sharpe / volatility / beta line (beta is computed
  backend-side already per the prior audit but isn't charted over time).

---

# Feature gaps a trading-bot dashboard typically has (G-1, P3)

Not defects — product-completeness observations, roughly ranked:

1. **Kill-switch / global halt** with a prominent always-visible control and a
   confirmation — distinct from per-position stops and engine pause.
2. **Order/fill blotter** — a live, filterable log of every order lifecycle event
   (submitted → filled/partial/rejected/canceled), separate from the trade journal.
3. **Alert delivery status** — surface whether the last push/email/webhook alert
   actually delivered (the backend has channels; the UI doesn't confirm delivery).
4. **Reconciliation view** — broker positions vs. internal state, flagging drift
   (ties to the `.env`/restart drift risk in B-6).
5. **Backtest-vs-live comparison** — overlay realized live performance against the
   strategy's backtested expectation.
6. **Per-user activity / RBAC** — multi-user support exists (`users_router.py`) but
   there's no role separation (viewer vs trader vs admin) or per-user audit view.
7. **Session timeout / re-auth** for the admin password before money actions after
   an idle period.

---

## How these were assessed

- Backend: read `auth.py`, `middleware.py`, `rate_limit.py`, `http_util.py`,
  `manual_trade_router.py`, `provider_control.py`, `api_keys.py`, `ai_router.py`,
  and grepped `dashboard/` + `engine.py` for CORS/CSRF/timeouts/bare-excepts/
  secret-handling/metrics.
- Frontend: statistical + targeted grep over the 7,311-line
  `dashboard/templates/dashboard.html` (ARIA, roles, `scope`, `@media`,
  `AbortController`, `visibilitychange`, theme, charts, empty/loading states,
  service worker).
- Cross-referenced every finding against `docs/IMPROVEMENTS.md` to avoid
  re-reporting already-shipped work.

No code was modified.
