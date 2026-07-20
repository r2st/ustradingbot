# Audit V2 — implementation status

Tracks what was implemented from [`AUDIT_V2.md`](AUDIT_V2.md). Every P1 and P2
finding is shipped with tests; the P3 set is largely shipped, with the
remaining items being greenfield **product features** (not defects) that are
called out below as tracked follow-ups.

Status: ✅ done · 🟡 partial (feasible subset shipped) · 📋 tracked (deferred)

| ID | Pri | Item | Status | Where |
|----|-----|------|--------|-------|
| B-1 | P1 | Rate-limit AI/LLM endpoints | ✅ | `rate_limit.py`, `ai_router.py`; `test_ai_rate_limit.py` |
| B-2 | P1 | Manual-trade audit log | ✅ | `manual_trade_router.py`; `test_manual_trade_audit.py` |
| B-3 | P1 | `/readyz` + `/metrics` + `/version` | ✅ | `ops.py`, `metrics.py`, `app.py`; `test_ops_endpoints.py` |
| F-1 | P1 | Table `scope`/`caption` a11y | ✅ | `dashboard.html`; `test_dashboard_a11y.py` |
| B-4 | P2 | Multi-worker rate limiter (Redis/shared) | ✅ | `rate_limit.py`; `test_rate_limit_shared.py` |
| B-5 | P2 | Explicit CSRF (same-origin) defence | ✅ | `middleware.py`; `test_csrf.py` |
| B-6 | P2 | `.env` 0600 + restart ack + trust-model docs | ✅ | `mode_control.py`, `docs/security.md`; `test_env_perms_restart.py` |
| B-7 | P2 | WS disconnect + AI fail-open tests | ✅ | `test_ws_pnl.py`, `test_ai_commentary_failopen.py` |
| F-2 | P2 | Modal focus trap/restore + skip link | ✅ | `dashboard.html`; `test_dashboard_a11y.py` |
| F-3 | P2 | Skeletons + shared empty-state helper | ✅ | `dashboard.html` (`renderSkeleton`/`renderEmpty`) |
| F-4 | P2 | AbortController for stale-response races | ✅ | `dashboard.html` (`jget` keyed abort) |
| B-8 | P3 | Response-envelope split documented | ✅ | `http_util.py`, `docs/api-conventions.md` |
| B-9 | P3 | Silent-except logging + rotation guidance | ✅ | `backtest/push/live/ta/ws_pnl`, `logging_config.py` |
| F-5 | P3 | OS theme preference + onclick a11y | ✅ | `dashboard.html` |
| F-6 | P3 | Missing data-viz | 🟡 | per-strategy P&L + trade-timing heatmap shipped (`/api/history/attribution`); see below |
| G-1 | P3 | Product completeness gaps | 🟡 | kill-switch shipped; see below |

## F-6 — data-viz: shipped vs remaining

**Shipped** (`/api/history/attribution` + Performance Charts panels):

- **Per-strategy P&L attribution** — diverging bars showing which strategy
  (momentum / swing / short / selective) makes or loses money.
- **Trade-timing heatmap** — realized P&L by exit day-of-week.

**Already present before this audit** (not re-built): correlation heatmap
(`#riskCorrMatrix`), sector concentration (`#riskSectors`), exposure by currency
(`#riskExposure`), equity curve, drawdown, returns, rolling win-rate.

**📋 Tracked follow-ups** (need new backend series):

- Gross-vs-net **exposure over time** and by-strategy allocation (positions carry
  strategy; needs a time-series exposure endpoint).
- **Rolling Sharpe / volatility / beta** line (beta is computed backend-side but
  not stored as a series — needs a rolling-risk endpoint).

## G-1 — product completeness: shipped vs remaining

**Shipped:**

- **Kill-switch / global halt** — always-visible header control with confirmation
  and admin password, halting the engine via the existing engine-control path.

**📋 Tracked follow-ups** (each a standalone feature requiring new backend +
engine/broker work, out of scope for a hardening pass — deliberately *not*
stubbed):

1. **Order/fill blotter** — a live order-lifecycle log (submitted → filled /
   partial / rejected / canceled). Needs the engine to emit per-order lifecycle
   events; today only completed trades are journaled.
2. **Alert delivery status** — confirm the last push/email/webhook actually
   delivered. Needs the alert channels to record and expose delivery receipts.
3. **Reconciliation view** — broker positions vs. internal state with drift
   flagging. Needs a broker-position fetch to diff against `open_positions.json`.
4. **Backtest-vs-live overlay** — realized live performance against the
   strategy's backtested expectation. Needs a joined data series.
5. **Per-user RBAC** — viewer / trader / admin role separation over the existing
   multi-user support, plus a per-user audit view.
6. **Session timeout / idle re-auth** before money actions. The admin password is
   already required per money action; a full idle-timeout layer over HTTP Basic
   is a separate UX/auth change.

These were left as documented backlog rather than half-implemented so the
codebase never carries a control that *looks* present but isn't wired end-to-end
(e.g. a reconciliation view that can't actually reach the broker).
