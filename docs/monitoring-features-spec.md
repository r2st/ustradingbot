# Monitoring Features — Specification

**Status:** Implemented (F1–F9) — 2026-07-07
**Scope:** Trading-activity monitoring features for the web dashboard
**Audience:** Anyone implementing dashboard/engine work in this repo
**Date:** 2026-07-07

This document specifies nine monitoring features for USTradingBot. Each
feature lists what it does, where the data comes from in the existing
codebase, the API surface, the frontend work, acceptance criteria, and a
complexity estimate (S = hours, M = 1–2 days, L = 3+ days).

**Headline features:** F1 (real-time P&L — live unrealized profit/loss on
every open position, updating without a reload) and F9 (trade rationale —
*why* the system took each trade, as scored criteria plus a breakout-pattern
chart). Everything else supports or extends those two views.

---

## 0. Existing infrastructure (what we build on)

The dashboard is a single Jinja2 page (`dashboard/templates/dashboard.html`,
vanilla JS, section-based layout) served by FastAPI (`dashboard/app.py`),
guarded by HTTP Basic auth (`dashboard/auth.require_auth`). Feature routers
live in `dashboard/*_router.py` and are mounted at the bottom of `app.py`.

Data already available:

| Source | File / module | Contents |
|---|---|---|
| Trade journal | `data_store/trades.csv` via `journal/trade_logger.py` (`SCHEMA_COLUMNS`) | entry/exit fills + timestamps, `pnl_gross/net/pct`, `r_multiple`, `hold_duration_hours`, `capture_ratio`, signal scores, AI decision, per-trade commissions |
| Open positions | `data_store/open_positions.json` (written by `PaperBroker` / risk manager) | symbol, quantity, entry/stop/target, currency, `opened_at`, partial-take state, side, multi-level exit ladders |
| Rejected signals | JSONL via `journal/btst_logger.RejectedSignalLogger` | timestamp, symbol, strategy, gate (`reason`), human detail, signal metadata |
| Engine heartbeat | `dashboard/engine_control.write_heartbeat` / `read_heartbeat` | phase, pid, market_open, open-position count, `last_cycle_at`, `next_scan_at` |
| Engine logs | journald via `dashboard/engine_control.engine_logs` | raw structlog lines (`engine.cycle_start`, `engine.rejected`, `engine.trade_placed`, …) |
| Performance analytics | `analytics/performance.analyze_journal` | summary (win rate, profit factor, expectancy, Sharpe, Sortino, max DD), by-strategy, by-symbol, equity curve, recent trades |
| Risk analytics | `analytics/risk_dashboard.build_risk_report` | exposure per currency, sector concentration, pairwise position correlations, drawdown tracking, daily/weekly/monthly P&L breakdown |
| Prices | `data/fetcher.fetch_current_price` / `fetch_ohlcv` / `fetch_multiple` (TTL-cached) over `data/providers.py` (yfinance / Alpaca / Polygon, `FallbackProvider`) | current price, OHLCV bars; Alpaca supports websocket streaming |
| Watchlists | `config/watchlist.WatchlistStore` + `dashboard/watchlist_router.py` | named lists, enable/disable, `scan_symbols()` |
| Alerts | `agent/alerts.AlertManager` (Telegram + email) and `dashboard/push.PushStore` (PWA push + poll) | entry/exit/cycle/mode-switch notifications, drawdown + daily-loss threshold monitors |

Existing API endpoints already serve much of the raw material:
`/api/paper/{summary,positions,trades}`, `/api/analytics/{summary,by-strategy,by-symbol,equity-curve,trades,report}`,
`/api/risk/{report,exposure,sectors,correlations,drawdown,pnl-breakdown}`,
`/api/engine/{status,logs}`, `/api/watchlist`, `/api/push/{status,poll}`,
`/api/{regime,premarket,earnings,montecarlo}`, and API-key-gated `/api/v1/*`.

**The single biggest gap across all nine features: no endpoint returns a
current market price.** The dashboard is entirely entry-time data; everything
"live" below hangs off one new quote endpoint (Feature 1).

### Cross-cutting design decisions

- **Transport: polling, not websockets.** The dashboard already polls
  (`/api/push/poll`, backtest status). Live features should poll JSON
  endpoints on a 10–30 s interval, matching provider rate limits
  (yfinance is unofficial; Polygon free tier is 5 req/min — batch quotes
  through `fetch_multiple` and a server-side quote cache, never one HTTP call
  per symbol). Server-Sent Events are a possible later upgrade; do not build
  them in v1.
- **One quote service, shared by all features.** A small module (suggested:
  `dashboard/quotes.py`) that takes a list of symbols, serves from a
  short-TTL cache (default 15 s), and fetches misses via
  `data/fetcher.fetch_multiple`. Every feature below that says "current
  price" means this service — never a direct provider call from a router.
- **Engine → dashboard channel stays file-based.** Engine and dashboard are
  separate processes (systemd service + uvicorn). All new engine-emitted data
  follows the established pattern: append-only JSONL / JSON files under
  `settings.DATA_DIR`, written best-effort (a telemetry failure must never
  break the trading loop — see `_emit_heartbeat` for the pattern).
- **Auth:** every new endpoint uses `Depends(require_auth)`; read-only
  mirrors may be added to `/api/v1` (API-key auth) where noted.
- **New routers, not more `app.py`.** Each feature ships as its own
  `APIRouter` per the convention at the bottom of `dashboard/app.py`.

### Suggested build order

1. **F1 Real-time P&L** (S/M) — unlocks the quote service everything else uses.
2. **F3 Position monitoring** (S) — thin extension of F1.
3. **F9 Trade rationale & scoring** (M) — engine-side capture must start early
   so history accumulates; the dashboard view can land later.
4. **F4 Engine activity log** (M) — highest "what is my bot doing?" value.
5. **F8 Watchlist monitor** (M) — reuses quote service + rejection log.
6. **F2 Trade history & analytics** (M).
7. **F7 Performance charts** (M) — needs a charting approach picked once.
8. **F6 Alerts & notifications** (M/L).
9. **F5 Risk dashboard enhancements** (M) — mostly upgrades an existing section.

---

## 1. Real-time P&L tracking

### What & why

Live unrealized P&L per open position (current price vs. entry fill), a
portfolio-level total (unrealized + realized), and a today-P&L figure that
includes open positions — plus an intraday chart of today's total P&L.
Today the account section only shows realized P&L from closed trades; while
a position is open the dashboard is blind to whether it is up or down, which
is the most basic monitoring question there is.

### Data sources

- Open positions: `open_positions.json` (already read by
  `_load_open_positions` in `dashboard/app.py`) — entry price, quantity,
  currency, `side` (short manual trades must compute inverted P&L, see
  `execution/broker.position_pnl`).
- Current prices: new shared quote service over `data/fetcher.fetch_multiple`.
- Realized P&L: `analytics/performance.analyze_journal` summary (exists);
  today's realized P&L: `_paper_today_pnl` in `dashboard/app.py` (exists).
- Intraday P&L history: a new sampler — the dashboard process appends a
  `{ts, unrealized, realized_today, total}` point to
  `DATA_DIR/pnl_intraday.jsonl` each time the quote endpoint is hit (throttled
  to ≥ 1 point/minute), reset logic keyed on US-Eastern date. No engine change.

### API

- `GET /api/live/pnl` →
  ```json
  {
    "as_of": "...", "quote_age_seconds": 12,
    "positions": [{"symbol": "NVDA", "side": "long", "quantity": 10,
                   "entry_price": 142.5, "current_price": 149.1,
                   "unrealized_pnl": 66.0, "unrealized_pct": 4.63,
                   "currency": "USD"}],
    "totals": {"unrealized": 66.0, "realized_today": -12.4,
               "today_total": 53.6, "realized_all_time": 812.9}
  }
  ```
  Symbols whose quote fetch fails return `current_price: null` and are
  excluded from totals, with a `stale: true` flag — never fail the whole
  response for one bad symbol.
- `GET /api/live/pnl/intraday?date=YYYY-MM-DD` → list of sampled points
  (defaults to today).

### Frontend

- Extend the existing open-positions table in the **Trading Account** section
  with Current Price, Unrealized P&L ($ and %), colored green/red.
- Add stat tiles: Unrealized P&L, Today's P&L (open + closed), Account Equity
  (starting capital + realized + unrealized).
- Small intraday line chart (today's total P&L) under the tiles.
- Poll every 15 s during market hours (gate on `/api/engine/status`
  `market_open` to stop polling nights/weekends); show "as of HH:MM:SS" and a
  stale badge when quote age > 60 s.

### Acceptance criteria

- With ≥ 1 open position and the market open, the positions table shows a
  current price and unrealized P&L that updates without a page reload.
- Short positions (manual sell trades) show correct sign: price drop ⇒
  positive unrealized P&L.
- A symbol with no available quote renders "—" and does not zero out or
  poison the portfolio totals.
- Totals reconcile: account equity = `TOTAL_CAPITAL` + realized (journal) +
  unrealized (live), within rounding.
- Quote fetching for N positions performs one batched fetch, not N provider
  calls; repeated polls within the cache TTL hit no provider at all.
- Intraday chart survives a dashboard restart (file-backed) and resets at the
  next US-Eastern trading day.

**Complexity: M** (S once the quote service exists — the service itself is
most of the work).

---

## 2. Trade history & analytics

### What & why

A full, filterable trade log (every row of `trades.csv`, not just the last
15) with per-trade P&L, R-multiple, and hold time, plus derived analytics:
rolling win rate over time, average hold duration, and best/worst trades.
This is the trust-building view — "show me every decision and how it worked
out" — and the raw material for deciding which strategies to keep.

### Data sources

- Everything comes from `trades.csv` via
  `analytics/performance.load_completed_trades` (already parses and
  type-coerces). Relevant columns: `entry_time`, `exit_time`, `pnl_net`,
  `pnl_pct`, `r_multiple`, `hold_duration_hours`, `strategy`, `grade`,
  `exit_reason`, `capture_ratio`, `ai_decision`.
- No engine changes; this is read-only over the journal.

### API

New router (suggested: `dashboard/history_router.py`):

- `GET /api/history/trades?limit=50&offset=0&strategy=&symbol=&exit_reason=&from=&to=&sort=exit_time&order=desc`
  → `{total, trades: [...]}` — server-side pagination and filtering (the
  journal can grow to thousands of rows; do not ship the whole CSV to the
  browser).
- `GET /api/history/stats` →
  `{avg_hold_hours, median_hold_hours, best: {...trade}, worst: {...trade},
    longest_win_streak, longest_loss_streak, by_exit_reason: {...}}`.
- `GET /api/history/win-rate-trend?window=20` → rolling win rate / avg R over
  the last N trades, one point per completed trade
  (`[{trade_id, exit_time, win_rate, avg_r}]`).

### Frontend

- New **Trade History** section: paginated table (symbol, strategy, grade,
  entry→exit time, hold duration, entry/exit price, P&L $, P&L %, R, exit
  reason), filter controls (strategy dropdown, symbol search, date range,
  exit reason), column sort.
- Row expand: full journal row (signal scores, AI reasoning, commissions).
- Stat strip above the table: avg/median hold time, best trade, worst trade,
  current streak.
- Rolling win-rate line chart (window selectable 10/20/50).
- CSV export of the *filtered* view can reuse/extend the existing
  `dashboard/export_router.py`.

### Acceptance criteria

- With a 1,000-row journal, the table endpoint responds < 500 ms and returns
  only the requested page; filters combine (strategy + date range) correctly.
- Open (not yet exited) journal rows are excluded, matching
  `load_completed_trades` semantics.
- Rolling win rate over the last 20 trades matches a hand calculation on the
  CSV; best/worst trade match `max/min(pnl_net)`.
- Blank/unparseable numeric cells (see `_num` in `dashboard/app.py`) render
  as "—", never NaN, and don't crash aggregation.

**Complexity: M**

---

## 3. Position monitoring dashboard

### What & why

Per-position situational awareness on top of F1's live prices: how far price
is from the stop and target (as % and R-progress), how long the trade has
been open vs. `HOLD_MAX_DAYS`, partial-take status, and a visual warning when
price is within a configurable proximity of the stop or target. Answers "do I
need to look at this position right now?" at a glance.

### Data sources

- `open_positions.json`: entry/stop/target, `opened_at`, `partial_take_*`,
  `side`, `levels` (multi-level manual-trade ladders — show nearest
  untriggered rung, mirroring `_PaperPosition` semantics).
- Current prices: shared quote service (F1).
- Config: `Settings.HOLD_MAX_DAYS`; new setting
  `POSITION_PROXIMITY_ALERT_PCT` (default 1.0 = warn when price is within 1%
  of stop or target).
- Trailing-stop moves already rewrite `stop_price` in the positions file
  (`RiskManager.update_stop`), so distance-to-stop automatically reflects
  trails.

### API

- Extend `GET /api/live/pnl` positions (F1) with:
  `stop_price`, `target_price`, `distance_to_stop_pct`,
  `distance_to_target_pct`, `r_progress` ((current−entry)/(entry−stop),
  sign-adjusted for shorts), `time_in_trade_hours`, `max_hold_hours`,
  `partial_taken`, `proximity: "near_stop" | "near_target" | null`,
  `next_level` (for laddered manual trades).
  One endpoint serves F1 and F3 — the frontend decides how much to render.

### Frontend

- Upgrade the open-positions table: a horizontal stop ◄─── price ───► target
  progress bar per row (entry marked), % to stop / % to target columns,
  time-in-trade with amber styling when > 80% of `HOLD_MAX_DAYS`.
- Row turns amber when `proximity == "near_target"`, red when `"near_stop"`.
- Sort default: nearest-to-stop first (most urgent on top).
- Reuses the F1 poll — no additional requests.

### Acceptance criteria

- For a long with entry 100, stop 95, target 110, price 104:
  distance-to-stop ≈ 8.65%, distance-to-target ≈ 5.77%, R-progress = 0.8;
  short positions produce mirrored, correctly signed values.
- A position whose price is within `POSITION_PROXIMITY_ALERT_PCT` of its stop
  is visually flagged within one poll interval.
- Trailing-stop updates from the engine are reflected on the next poll
  without a dashboard restart.
- Laddered manual trades show the nearest untriggered level as the effective
  stop/target rather than the display mirror being wrong.

**Complexity: S** (data plumbing rides on F1; the progress-bar UI is the bulk).

---

## 4. Engine activity log

### What & why

A scrollable, human-readable feed of what the engine did each cycle: scan
started, N signals found, each rejection with its gate and reason (risk
pre-check, strategy cap, AI veto, news sentiment, regime/autotune, freshness,
cash, broker), trades placed, exits taken, cycle summary. Today this story is
split between raw journald text (`/api/engine/logs` — noisy, unstructured for
the UI) and the rejection JSONL (gate rejections only). A structured event
feed is the single best answer to "why didn't the bot trade today?".

### Data sources

- **New engine-side event stream** — the one feature that needs an engine
  change. Add a small `journal/activity_log.py` (`ActivityLogger`) that
  appends JSON lines to `DATA_DIR/engine_activity.jsonl`:
  `{ts, cycle_id, event, symbol?, strategy?, gate?, reason?, data?}`.
  Emit from the exact points that already log via structlog in `engine.py`:
  `cycle_start`, `regime_refresh`, `scan_complete` (symbol + signal counts),
  `signal_rejected` (piggyback on every `rejected_logger.log_rejection` call
  so gates stay in lockstep), `ai_veto` (decision + reasoning + cost),
  `trade_placed`, `entry_pending/filled/expired`, `exit` (per
  `exit_summary.events`), `trail_updated`, `cycle_complete` (totals +
  elapsed), `broker_reconnect`, `error`.
  Best-effort writes; size-capped by rotating at ~10 MB (keep 1 backup).
- Existing structlog/journald output is unchanged (ops-level); the raw-log
  viewer (`/api/engine/logs`) remains for debugging.

### API

- `GET /api/activity?limit=200&since_ts=&event=&symbol=&cycle_id=` — newest
  first, filterable; `since_ts` enables incremental polling (only new events).
- `GET /api/activity/cycles?limit=20` — one row per cycle rolled up from
  events: `{cycle_id, started_at, elapsed_s, signals_found, rejected_by_gate:
  {ai_veto: 2, cash_check: 1, ...}, trades_placed, exits}`.

### Frontend

- New **Engine Activity** section: reverse-chronological feed grouped by
  cycle (collapsible cycle header showing the summary line; expand to see
  per-signal events). Color coding: green = trade placed, red = error/exit
  at loss, gray = rejection, blue = info.
- Filter chips: All / Trades / Rejections / Exits / Errors; symbol search.
- Incremental poll (15–30 s) appends new events; auto-scroll pinned to top
  with a "N new events" affordance when the user has scrolled.

### Acceptance criteria

- After one market-hours cycle, the feed shows cycle start → scan result →
  one event per signal (placed or rejected with gate + human-readable
  reason, including AI-veto reasoning text) → cycle summary matching the
  `engine.cycle_complete` structlog counts.
- Rejection events agree 1:1 with `RejectedSignalLogger` entries for the same
  cycle.
- An engine crash/restart is visible in the feed (error event and/or gap +
  new `engine.init`), and the dashboard renders fine while the engine is
  stopped (feed is just static).
- A telemetry write failure (e.g. read-only disk) is swallowed — engine
  keeps trading (unit test: `ActivityLogger` raising is non-fatal).
- The JSONL rotates: file never exceeds ~2× the cap.

**Complexity: M**

---

## 5. Risk dashboard (enhancements)

### What & why

A **Risk Dashboard** section already exists (`analytics/risk_dashboard.py` +
`/api/risk/*`): exposure per currency, sector concentration, pairwise
correlations, drawdown, and P&L breakdown. Two things make it materially
better for monitoring:

1. **Mark to market.** Exposure and drawdown currently value the book at
   entry prices (cost basis). With live quotes (F1), exposure should reflect
   current market value and the equity/drawdown numbers should include
   unrealized P&L.
2. **Open risk.** The most actionable number a swing trader checks daily:
   for each position, `(current_price − stop) × quantity` — the amount lost
   if every stop is hit right now — individually, in aggregate, and as % of
   capital, next to the `DAILY_LOSS_LIMIT_PCT` budget and today's realized
   P&L (`RiskManager.daily_pnl` is engine-side; the journal-derived
   `_paper_today_pnl` is the dashboard-side equivalent).

### Data sources

- `analytics/risk_dashboard.build_risk_report` (extend, don't replace).
- Live quotes from F1's quote service (pass a `prices: dict[str, float]`
  into the report builder; keep the entry-price path as fallback when quotes
  are unavailable so the report never errors).
- `open_positions.json` for stops/quantities; `Settings` for
  `DAILY_LOSS_LIMIT_PCT`, `TOTAL_CAPITAL`, `MAX_POSITION_SIZE_PCT`.

### API

- Extend `GET /api/risk/report` payload with:
  `open_risk: {per_position: [{symbol, risk_if_stopped, pct_of_capital}],
  total, pct_of_capital}`, `marked_to_market: true|false`, and
  `daily_loss_budget: {limit_pct, limit_usd, used_today, remaining}`.
- `GET /api/risk/exposure` gains `market_value` alongside `cost_basis` per
  currency and per position.

### Frontend

- In the existing Risk Dashboard section: an "Open Risk" tile row (total $
  at risk to stops, % of capital, daily-loss budget bar showing
  used/remaining), and a per-position sizing breakdown bar (each position's
  market value as % of capital, with a marker at `MAX_POSITION_SIZE_PCT`).
- Correlation matrix display upgrade: color-scaled table (exists as data via
  `/api/risk/correlations`; today's UI shows only max correlation).
- "Marked to market" vs "cost basis" badge so the user knows which valuation
  they're looking at.

### Acceptance criteria

- With live quotes available, exposure = Σ quantity × current price (per
  currency), and drawdown/current-equity figures include unrealized P&L;
  with quotes unavailable, the report still renders, valued at cost, with
  the badge flipped.
- Open risk for a long with a trailed stop *above* entry shows as negative
  risk (locked-in profit) rather than clamping to zero silently — or is
  explicitly rendered "locked +$X"; either way the aggregate math is
  documented and consistent.
- Daily-loss budget bar: with `DAILY_LOSS_LIMIT_PCT=0.03` and capital
  $100k, a −$1,200 day shows 40% of budget used.
- Existing `/api/risk/*` consumers (current template JS, `/api/v1/risk`)
  keep working — additions only, no renamed/removed fields.

**Complexity: M**

---

## 6. Alerts & notifications

### What & why

The delivery machinery exists (Telegram + email in `agent/alerts.py`, PWA
push in `dashboard/push.py`, engine already calls `notify_entry/exit/cycle`,
`check_drawdown`, `check_daily_loss`). What's missing is **user control and
history**: which events alert, on which channels, at what thresholds — and a
persistent, reviewable alert log in the dashboard (today a missed Telegram
message is simply gone). Plus one genuinely new alert source: *position
approaching stop/target* (needs live prices, which only F1 provides).

### Data sources

- Config: new `DATA_DIR/alert_rules.json` —
  `{event_type: {enabled, channels: ["telegram","email","push"],
  threshold?}}` for event types: `entry`, `exit`, `stop_hit`, `target_hit`,
  `partial_take`, `approaching_stop`, `approaching_target`, `engine_error`,
  `broker_disconnect`, `drawdown`, `daily_loss`, `cycle_summary`.
  (Stop/target hits are distinguishable from generic exits via
  `ExitEvent.exit_reason`.)
- History: new `DATA_DIR/alerts_history.jsonl`, appended by `AlertManager`
  on every dispatch (event type, message, channels attempted, per-channel
  success), rotation-capped like F4.
- Proximity source: the engine's realtime exit poller
  (`_sleep_between_cycles` → `exit_manager.manage_exits`) for realtime
  providers, or per scan cycle otherwise — evaluated engine-side so alerts
  fire even when no browser is open. Debounced: one `approaching_stop` per
  symbol per day (persisted next to the rules so a restart doesn't re-fire).

### API

New `dashboard/alerts_router.py`:

- `GET /api/alerts/rules` / `PUT /api/alerts/rules` — read/update the rule
  set (validated event types/channels; unknown keys rejected).
- `GET /api/alerts/history?limit=100&type=&since_ts=` — reviewable log.
- `POST /api/alerts/test` — `{channel}` → sends a test message and reports
  per-channel success/failure (surfaces bad Telegram tokens immediately).
- `GET /api/alerts/channels` — which channels are configured/enabled
  (derived from settings, no secrets).

### Frontend

- New **Alerts** section: channel status cards (Telegram / Email / Push —
  configured? test button), a rules table (event type × channel checkboxes,
  threshold inputs for drawdown/daily-loss/proximity), Save.
- Alert history feed (reuses the F4 feed component styling), filterable by
  type.
- A bell icon in the sticky header showing count of alerts since page load
  (piggybacks on the existing `/api/push/poll` mechanism).

### Engine changes

- `AlertManager` reads `alert_rules.json` (mtime-cached) and consults it
  before dispatching; absence of the file preserves current behavior
  (backward compatible). All dispatches append to history.
- New proximity check in the exit-management path using
  `POSITION_PROXIMITY_ALERT_PCT` (shared with F3).

### Acceptance criteria

- Disabling `cycle_summary` in the UI stops Telegram cycle messages on the
  next cycle without an engine restart; entries/exits still alert.
- A stop-loss exit produces a `stop_hit` alert (not just generic `exit`)
  on every enabled channel, and appears in `/api/alerts/history` with
  per-channel delivery status.
- `approaching_stop` fires at most once per symbol per day, fires with no
  browser open, and respects the configured proximity threshold.
- Test button returns a clear failure (not a 500) when Telegram credentials
  are wrong.
- With no `alert_rules.json`, behavior is identical to today (all existing
  notifications fire).

**Complexity: M/L** (touches engine, alert manager, new router, new UI
section; the rules/history plumbing is straightforward but wide).

---

## 7. Performance charts

### What & why

The analytics numbers exist; the *shapes* don't. Four charts turn the journal
into an at-a-glance health check: equity curve (with drawdown shading),
periodic returns (daily/weekly/monthly bars), drawdown-over-time, and rolling
win-rate trend. Charts answer "is performance degrading?" faster than any
table.

### Data sources

All journal-derived, no engine changes:

- Equity curve: `/api/analytics/equity-curve` (exists — one point per closed
  trade from `build_equity_curve`).
- Periodic returns: `analytics/risk_dashboard.pnl_breakdown` (exists —
  daily/weekly/monthly realized P&L) via `/api/risk/pnl-breakdown`.
- Drawdown series: computable from the equity curve client-side or add a
  `drawdown_curve` to the analytics report (running peak → % below peak per
  point; `analytics/performance.max_drawdown` already computes the scalar).
- Win-rate trend: F2's `/api/history/win-rate-trend`.

### API

- Mostly reuse. One addition:
  `GET /api/analytics/equity-curve?granularity=trade|daily` — daily
  granularity buckets closed trades by exit date (a 500-trade journal
  shouldn't render 500 x-axis points), and includes
  `drawdown_pct` per point so all charts share one payload.

### Frontend

- **Charting library decision (RESOLVED): Chart.js, self-hosted** from
  `dashboard/static/chart.umd.min.js` (repo precedent: PWA assets are served
  locally, and the Hetzner deploy has no build step; no CDN — the dashboard
  must work on a locked-down server). All line/bar charts (F1 intraday,
  F2 win-rate, F7's four) use Chart.js; the F9 candlestick chart is
  dependency-free inline SVG.
- New **Performance Charts** section: 2×2 grid (stacks on mobile per the
  existing responsive breakpoints): equity curve with drawdown shading;
  returns bar chart with Daily/Weekly/Monthly toggle (green/red bars);
  drawdown area chart annotated with max-DD; rolling win-rate line with a
  50% guide line.
- Refresh on section open + every 5 min (journal only changes on trade
  close; no need for fast polling).

### Acceptance criteria

- Equity curve matches `/api/analytics/equity-curve` data and its final
  value equals starting capital + total realized P&L.
- Max drawdown annotated on the chart equals `summary.max_drawdown` from
  `/api/analytics/summary`.
- Monthly toggle: a month with net −$500 renders a red bar of the correct
  magnitude; empty periods render as zero, not gaps that skew the axis.
- Empty journal renders friendly empty states ("No closed trades yet"), no
  JS errors.
- Everything is served same-origin (no CDN — the dashboard must work on a
  locked-down server); the charting dependency is a single self-hosted file
  (~207 kB raw, ~70 kB gzipped) shared by every chart on the page.

**Complexity: M** (data is nearly free; the work is the charting foundation —
which F1/F2 also consume, so build it here once).

---

## 8. Watchlist monitor

### What & why

Watchlist CRUD exists (`config/watchlist.py`, watchlist section in the UI),
but it's a static list of symbols. The monitor makes it live: current price
and day-change for every watchlist symbol, which ones produced a signal in
the latest scan (and its grade), which were rejected and why (gate), and
which are "approaching entry" — i.e. recently rejected on freshness/price
drift, meaning the setup exists but price moved. This is the pre-trade
counterpart to F3: what *might* the bot do next?

### Data sources

- Symbols: `WatchlistStore.scan_symbols()` /
  `config.watchlist.scan_symbols_for` (same universe the engine scans,
  including trade-selection filtering via `config/trade_selection.py` —
  show pinned-out symbols as "excluded by trade selection").
- Prices + day change: shared quote service; day change needs previous close
  (extend the quote service to return `{price, prev_close, change_pct}` —
  available from provider OHLCV; cache prev-close for the whole day).
- Latest scan signals: **new** — the engine currently discards the scan's
  signal list after processing. Persist it: at the end of each scan, write
  `DATA_DIR/last_scan.json` — `{cycle_id, ts, signals: [{symbol, strategy,
  grade, signal_strength, entry, stop, target}]}` (emitted alongside the F4
  `scan_complete` activity event; trivially co-implemented with F4).
- Rejections per symbol: `RejectedSignalLogger.get_recent_rejections`
  (exists) — join latest rejection per symbol; `freshness_check` /
  `price_drifted` rejections mark "near entry".

### API

New endpoints on the existing watchlist router:

- `GET /api/watchlist/monitor` →
  ```json
  {"as_of": "...", "last_scan_at": "...",
   "symbols": [{"symbol": "NVDA", "lists": ["default"],
                "price": 149.1, "change_pct": 1.8,
                "signal": {"strategy": "momentum", "grade": "A",
                           "entry": 148.9, "strength": 0.83},
                "last_rejection": {"gate": "cash_check", "ts": "...",
                                    "detail": "..."},
                "status": "signal" | "near_entry" | "rejected" |
                          "held" | "excluded" | "idle"}]}
  ```
  `held` cross-references `open_positions.json` so the user sees which
  watchlist names are already in the book.
- Batched quotes; for large watchlists (> ~50 symbols) fetch in one
  `fetch_multiple` call and let the TTL cache absorb the poll rate. On the
  Polygon free tier, degrade gracefully: fill what the rate budget allows
  and mark the rest stale, never block the endpoint.

### Frontend

- Upgrade the **Watchlist Management** section (or a sibling **Watchlist
  Monitor** subsection): table of symbol, list, price, day % (colored),
  status chip (`SIGNAL A` green / `NEAR ENTRY` amber / `HELD` blue /
  `REJECTED: gate` gray / `EXCLUDED` muted), last-scan timestamp header.
- Click a signal row → shows entry/stop/target from the last scan (and, if
  Manual Trade Entry is relevant, a "prefill manual trade" convenience that
  populates the existing manual-trade form).
- Sort: signals first, then near-entry, then by day-change. Poll every 30 s
  market-hours only.

### Acceptance criteria

- Every enabled watchlist symbol shows a price and day-change within one
  poll of page load (or an explicit stale marker on rate-limit).
- After a scan cycle where NVDA scored grade A but was rejected at
  `cash_check`, NVDA shows both the signal chip and the rejection reason.
- A symbol held in `open_positions.json` shows `HELD`, not a fresh signal
  chip.
- Symbols filtered out by Engine Trade Selection are visibly `EXCLUDED`
  (consistent with what the engine actually scanned — same code path as
  `scan_symbols_for` + `selection.filter_symbols`).
- A 60-symbol watchlist on the Polygon free tier does not hammer the API:
  one batch per TTL window, and the page never spins forever.

**Complexity: M** (quote batching + the `last_scan.json` engine touchpoint;
UI is a table upgrade).

---

## 9. Trade Rationale & Scoring Dashboard

### What & why

Every trade the engine takes has already survived a 9-gate pipeline (risk
pre-check, strategy cap, pending-order guard, AI veto, news sentiment,
regime/auto-tune, sizing, freshness, cash) on top of a 5-indicator weighted
score — but none of that reasoning is visible afterwards. This feature
answers **"why did the system take this trade?"** with two artifacts,
captured **at trade entry time** and **persisted**, shown both on the open
positions view and in trade history:

1. **Scored rationale criteria** — 5–10 named criteria, each with a score
   (0–10 scale), and a one-line explanation. The criteria set:

   | Criterion | Source | Example explanation |
   |---|---|---|
   | Setup grade quality | `signal_strength` → grade | "Combined weighted score 0.82 → grade A" |
   | Breakout pattern strength | strategy-aware blend of `ema_score` + `ripster_score` | "VCP volatility-contraction breakout structure" |
   | Volume confirmation | `volume_score` / `volume_ratio` | "Volume 2.1× its 20-day average" |
   | Trend alignment | `ema_score` | "Price above rising EMA stack" |
   | Momentum | `macd_score` / `macd_histogram` | "MACD histogram positive and rising" |
   | RSI positioning | `rsi_score` / `rsi_value` | "RSI 62 — bullish, not overbought" |
   | Risk/reward ratio | entry/stop/target | "R:R 2.4:1 vs 1.8 required" |
   | AI veto score | `AIDecision` (approve + reasoning) | "APPROVE — no adverse news in window" |
   | Volatility-regime fit | regime multiplier for the strategy family | "Bull regime favours momentum ×1.10" |
   | Relative strength / OBV | `obv_confirming` | "OBV confirms the price trend" |

2. **Breakout pattern chart** — a candlestick chart of the daily bars
   around the entry (~90 bars ending at entry), with the **entry, stop and
   target levels marked** as horizontal lines. The bars are snapshotted into
   the rationale record at entry time so the chart still shows the *setup as
   it looked then*, even months later in trade history.

### Data sources

- Everything above is already in memory in `engine._process_signal` at the
  moment the trade is placed: the `Signal` (all indicator scores), the
  `AIDecision`, the news check result, the cycle's `RegimeResult` /
  auto-tune state, and the sized `TradeOrder`.
- Bars: `data/fetcher.fetch_ohlcv` (cache-hot at that moment — the screener
  just fetched them; snapshotting is free).
- **New module `journal/rationale.py`**: `build_trade_rationale(...)` (pure —
  signal + context → criteria list) and a `RationaleStore` appending one JSON
  line per trade to `DATA_DIR/trade_rationale.jsonl` (rotated like F4;
  best-effort writes — a rationale failure must never block an entry).
- Records are keyed by `symbol` + `entry_time`; open positions match on the
  latest un-exited record, history rows on nearest entry time.
- Pending entries (scale-in / MOC): rationale is built at submit time and
  persisted when the fill is reconciled.

### API

New `dashboard/rationale_router.py`:

- `GET /api/rationale?symbol=&entry_time=&limit=50` → newest-first list of
  rationale records (`entry_time` narrows to the record nearest that entry;
  `symbol` filters). Each record:
  ```json
  {"symbol": "NVDA", "strategy": "vcp_breakout", "grade": "A",
   "entry_time": "...", "entry_price": 142.5, "stop_price": 136.2,
   "target_price": 153.8, "overall_score": 8.2,
   "criteria": [{"key": "volume_confirmation", "name": "Volume confirmation",
                 "score": 7.8, "explanation": "Volume 2.1× its 20-day average"}],
   "bars": [{"t": "2026-07-01", "o": 140.1, "h": 143.2, "l": 139.8,
             "c": 142.9, "v": 51200000}]}
  ```

### Frontend

- **Open positions table**: a "Why?" button per row opens a rationale modal.
- **Trade history (F2) rows**: same "Why?" affordance per completed trade.
- The modal shows: header (symbol, strategy, grade, overall score), the
  criteria table (name, 0–10 score bar, explanation), and the candlestick
  chart with entry (blue), stop (red) and target (green) level lines. The
  candlestick chart is rendered as dependency-free inline SVG (matching the
  existing hand-rolled equity-curve SVG); line/bar charts elsewhere use the
  shared chart library (F7).
- Trades placed before this feature shipped have no record; the modal says
  "No rationale captured for this trade" rather than erroring.

### Acceptance criteria

- Placing a paper trade writes a rationale record with ≥ 5 criteria, every
  score within [0, 10], and non-empty explanations; the record includes the
  AI decision text actually returned for that trade.
- The open-positions view shows the rationale for a position opened this
  session without a dashboard restart; after the position closes, the same
  record is reachable from trade history.
- The candlestick chart marks entry/stop/target at the recorded (entry-time)
  levels even after the live stop has been trailed.
- A rationale write failure (e.g. read-only disk) never prevents the trade —
  entry, journaling, and registration all proceed (unit-tested).
- Short manual trades render with the stop above and target below entry.

**Complexity: M** (engine capture is simple; the modal + candlestick SVG is
the bulk).

---

## Appendix A — new files summary

| File | Feature | Purpose |
|---|---|---|
| `dashboard/quotes.py` | F1 (shared) | Batched, TTL-cached current-price service |
| `dashboard/live_router.py` | F1, F3 | `/api/live/pnl`, intraday P&L |
| `dashboard/history_router.py` | F2 | Paginated trade history + stats |
| `dashboard/activity_router.py` | F4 | Activity feed endpoints |
| `dashboard/alerts_router.py` | F6 | Alert rules, history, test |
| `dashboard/rationale_router.py` | F9 | Trade rationale records API |
| `journal/activity_log.py` | F4, F8 | Engine-side JSONL event writer + `last_scan.json` |
| `journal/rationale.py` | F9 | Rationale builder + JSONL store |
| `agent/alert_config.py` | F6 | Alert rules load/save + history writer |
| `DATA_DIR/engine_activity.jsonl` | F4 | Event stream (rotated) |
| `DATA_DIR/pnl_intraday.jsonl` | F1 | Intraday P&L samples |
| `DATA_DIR/alert_rules.json` | F6 | User alert configuration |
| `DATA_DIR/alerts_history.jsonl` | F6 | Dispatch log (rotated) |
| `DATA_DIR/alerts_state.json` | F6 | Proximity-alert per-day debounce state |
| `DATA_DIR/last_scan.json` | F8 | Latest scan's signal list |
| `DATA_DIR/trade_rationale.jsonl` | F9 | Per-trade rationale + bar snapshot (rotated) |
| `dashboard/static/chart.umd.min.js` | F7 (shared) | Self-hosted Chart.js (chosen library) |

## Appendix B — testing conventions

Follow the existing patterns: router tests like `tests/test_dashboard_*.py`
(FastAPI `TestClient` + tmp `DATA_DIR` fixtures from `tests/conftest.py`),
engine-side writers tested like `tests/test_journal.py`. Every feature above
should land with: (1) endpoint happy-path + empty-state tests, (2) a
malformed-file resilience test (corrupt JSONL line, missing CSV column —
matching the defensive style used throughout `dashboard/app.py`), and (3)
for engine-touching features (F4, F6, F8), a test that a writer exception
does not propagate into the trading loop.
