# API Reference

> Endpoint catalogue for the **US Trading Bot Dashboard** (v0.1.0), generated from the live OpenAPI schema. Interactive docs are served at `/docs` (Swagger UI) and `/redoc` when the dashboard is running.

## Authentication

- **HTTP Basic auth** guards every endpoint except `GET /health` and `GET /api/mode` (public, read-only). Credentials come from `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`. Set `DASHBOARD_AUTH_ENABLED=false` only for trusted local dev.
- **Admin password** — money-moving actions (manual trades, position stops, engine control, switching to live) additionally require the admin password in the JSON body as `admin_password`. It reads from `DASHBOARD_ADMIN_PASSWORD`, falling back to `DASHBOARD_PASSWORD` when unset (see [env-vars.md](env-vars.md)).
- **Versioned REST API** (`/api/v1/*`) authenticates with an API key minted from the dashboard, presented as a bearer token.

## Error shape

Every error response uses one consistent envelope:

```json
{ "ok": false, "error": { "code": "…", "message": "…", "status": 4xx }, "detail": "…", "error_code": "…", "request_id": "…" }
```

`detail` / `error_code` are preserved for backward compatibility. `request_id` echoes the `X-Request-ID` response header for log correlation. Unhandled errors return `500` with a generic message (no stack trace) — the detail is logged server-side against the request id.

## Endpoints (138 total)

| Area | Endpoints |
|---|---|
| [System](#system) | 3 |
| [Trading](#trading) | 6 |
| [Analytics](#analytics) | 29 |
| [Market Data](#market-data) | 6 |
| [Signals & TA](#signals--ta) | 3 |
| [AI](#ai) | 10 |
| [Configuration](#configuration) | 29 |
| [Journal](#journal) | 11 |
| [Backtesting](#backtesting) | 4 |
| [Users](#users) | 6 |
| [Notifications](#notifications) | 22 |
| [rest-api-v1](#rest-api-v1) | 9 |

### System

_Health, trading mode, and dashboard root._

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Dashboard |
| `GET` | `/api/mode` | Api Mode |
| `GET` | `/health` | Health |

### Trading

_Manual trades, position stops, and engine control — money-moving, admin-gated actions._

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/engine/control` | Engine Control Api |
| `GET` | `/api/engine/logs` | Engine Logs Api |
| `GET` | `/api/engine/status` | Engine Status Api |
| `POST` | `/api/manual-trade` | Place |
| `POST` | `/api/mode/switch` | Api Mode Switch |
| `POST` | `/api/positions/stop` | Stop Position |

### Analytics

_Performance, risk, history, and Monte-Carlo insight endpoints (read-only)._

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/analytics/by-strategy` | Analytics By Strategy |
| `GET` | `/api/analytics/by-symbol` | Analytics By Symbol |
| `GET` | `/api/analytics/equity-curve` | Analytics Equity Curve |
| `GET` | `/api/analytics/report` | Analytics Report |
| `GET` | `/api/analytics/summary` | Analytics Summary |
| `GET` | `/api/analytics/trades` | Analytics Trades |
| `GET` | `/api/autotune` | Autotune |
| `GET` | `/api/earnings` | Earnings |
| `GET` | `/api/history/filters` | History Filters |
| `GET` | `/api/history/stats` | History Stats |
| `GET` | `/api/history/trades` | History Trades |
| `GET` | `/api/history/win-rate-trend` | Win Rate Trend |
| `GET` | `/api/live/pnl` | Live Pnl |
| `GET` | `/api/live/pnl/intraday` | Live Pnl Intraday |
| `GET` | `/api/montecarlo` | Montecarlo |
| `GET` | `/api/paper/account` | Api Paper Account |
| `GET` | `/api/paper/positions` | Api Paper Positions |
| `GET` | `/api/paper/summary` | Api Paper Summary |
| `GET` | `/api/paper/trades` | Api Paper Trades |
| `GET` | `/api/premarket` | Premarket |
| `GET` | `/api/regime` | Regime |
| `GET` | `/api/risk/correlations` | Risk Correlations |
| `GET` | `/api/risk/drawdown` | Risk Drawdown |
| `GET` | `/api/risk/exposure` | Risk Exposure |
| `GET` | `/api/risk/pnl-breakdown` | Risk Pnl Breakdown |
| `GET` | `/api/risk/report` | Risk Report |
| `GET` | `/api/risk/sectors` | Risk Sectors |
| `GET` | `/api/sectors` | Sectors |
| `GET` | `/api/ws/token` | Ws Token |

### Market Data

_Provider selection/status, quotes, pre-market and sector scans._

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/earnings/contagion` | Earnings Contagion |
| `GET` | `/api/earnings/history/{symbol}` | Earnings History |
| `GET` | `/api/earnings/today` | Earnings Today |
| `GET` | `/api/providers` | Api Providers |
| `POST` | `/api/providers/keys` | Api Provider Keys |
| `POST` | `/api/providers/select` | Api Provider Select |

### Signals & TA

_Technical-analysis charts, rationale, and signal insights._

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/rationale` | Rationale List |
| `GET` | `/api/trade/{symbol}/indicators` | Live Indicators |
| `GET` | `/api/trade/{symbol}/ta-chart` | Ta Chart |

### AI

_Live analyst commentary and the AI memory / learnings layer._

| Method | Path | Description |
|---|---|---|
| `GET` | `/ai-dashboard` | Ai Dashboard Page |
| `GET` | `/api/ai/commentary` | Ai Commentary |
| `GET` | `/api/ai/market-overview` | Ai Market Overview |
| `POST` | `/api/ai/refresh` | Ai Refresh |
| `GET` | `/api/ai/status` | Ai Status |
| `GET` | `/api/memory/guard-decisions` | Api Memory Guard Decisions |
| `GET` | `/api/memory/learnings` | Api Memory Learnings |
| `GET` | `/api/memory/overview` | Api Memory Overview |
| `GET` | `/api/memory/reflections` | Api Memory Reflections |
| `GET` | `/api/memory/stats` | Api Memory Stats |

### Configuration

_Watchlists, universe, trade-selection, and alert-rule configuration._

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/trade-selection` | Get Selection |
| `POST` | `/api/trade-selection` | Set Selection |
| `GET` | `/api/universe/exchanges` | Get Exchanges |
| `GET` | `/api/universe/filters` | Get Filters |
| `POST` | `/api/universe/filters` | Set Filter |
| `GET` | `/api/universe/indices` | Get Indices |
| `GET` | `/api/universe/promotions` | Get Promotions |
| `POST` | `/api/universe/promotions/{ticker}/demote` | Demote Promotion |
| `GET` | `/api/universe/scan-pool` | Get Scan Pool |
| `GET` | `/api/universe/search` | Search Symbols |
| `GET` | `/api/universe/sectors` | Get Sectors |
| `POST` | `/api/universe/seed` | Seed Universe |
| `GET` | `/api/universe/seed/status/{job_id}` | Seed Status |
| `GET` | `/api/universe/stats` | Get Stats |
| `GET` | `/api/universe/symbols` | Get Symbols |
| `GET` | `/api/universe/tiers` | Get Tiers |
| `GET` | `/api/universe/watchlists` | Get Watchlists |
| `POST` | `/api/universe/watchlists` | Add To Watchlist |
| `DELETE` | `/api/universe/watchlists/{list_name}` | Delete Watchlist |
| `GET` | `/api/universe/watchlists/{list_name}` | Get Watchlist |
| `POST` | `/api/universe/watchlists/{list_name}/enabled` | Set Watchlist Enabled |
| `POST` | `/api/universe/watchlists/{list_name}/remove` | Remove From Watchlist |
| `GET` | `/api/watchlist` | Get Watchlists |
| `POST` | `/api/watchlist/lists` | Create List |
| `DELETE` | `/api/watchlist/lists/{name}` | Delete List |
| `POST` | `/api/watchlist/lists/{name}/enabled` | Set Enabled |
| `DELETE` | `/api/watchlist/lists/{name}/symbols/{symbol}` | Remove Symbol |
| `GET` | `/api/watchlist/monitor` | Watchlist Monitor |
| `POST` | `/api/watchlist/symbols` | Add Symbol |

### Journal

_Trade notes, activity log, and exports._

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/activity` | Activity Feed |
| `GET` | `/api/activity/cycles` | Activity Cycles |
| `GET` | `/api/export/analytics.csv` | Export Analytics Csv |
| `GET` | `/api/export/analytics.pdf` | Export Analytics Pdf |
| `GET` | `/api/export/backtest/{job_id}.csv` | Export Backtest Csv |
| `GET` | `/api/export/trades.csv` | Export Trades Csv |
| `GET` | `/api/export/trades.pdf` | Export Trades Pdf |
| `GET` | `/api/notes` | List Notes |
| `DELETE` | `/api/notes/{trade_id}` | Delete Note |
| `GET` | `/api/notes/{trade_id}` | Get Note |
| `POST` | `/api/notes/{trade_id}` | Set Note |

### Backtesting

_On-demand and scheduled strategy backtests._

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/backtest/latest` | Backtest Latest Api |
| `GET` | `/api/backtest/options` | Backtest Options Api |
| `POST` | `/api/backtest/run` | Backtest Run Api |
| `GET` | `/api/backtest/status/{job_id}` | Backtest Status Api |

### Users

_Multi-user registration, login, and profiles._

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/users` | List Users |
| `POST` | `/api/users/login` | Login |
| `POST` | `/api/users/logout` | Logout |
| `GET` | `/api/users/me` | Me |
| `POST` | `/api/users/me/profile` | Update Profile |
| `POST` | `/api/users/register` | Register |

### Notifications

_Alert channels, push subscriptions, and test dispatch._

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/alerts/channels` | Get Channels |
| `GET` | `/api/alerts/history` | Get History |
| `GET` | `/api/alerts/rules` | Get Rules |
| `PUT` | `/api/alerts/rules` | Put Rules |
| `POST` | `/api/alerts/test` | Test Channel |
| `GET` | `/api/notifications` | Notifications List |
| `GET` | `/api/notifications/preferences` | Notifications Get Prefs |
| `POST` | `/api/notifications/preferences` | Notifications Set Prefs |
| `POST` | `/api/notifications/read` | Notifications Read |
| `POST` | `/api/notifications/read-all` | Notifications Read All |
| `GET` | `/api/notifications/unread` | Notifications Unread |
| `GET` | `/api/push/poll` | Push Poll |
| `GET` | `/api/push/status` | Push Status |
| `POST` | `/api/push/subscribe` | Push Subscribe |
| `POST` | `/api/push/test` | Push Test |
| `POST` | `/api/push/unsubscribe` | Push Unsubscribe |
| `GET` | `/favicon.svg` | Favicon |
| `GET` | `/manifest.json` | Manifest |
| `GET` | `/manifest.webmanifest` | Manifest |
| `GET` | `/pwa/icon.svg` | Icon |
| `GET` | `/pwa/pwa.js` | Pwa Client |
| `GET` | `/sw.js` | Service Worker |

### rest-api-v1

_Versioned, API-key-authenticated JSON API under /api/v1 (external integrations)._

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1` | Api Index |
| `GET` | `/api/v1/analytics` | Api Analytics |
| `GET` | `/api/v1/keys` | List Keys |
| `POST` | `/api/v1/keys` | Create Key |
| `DELETE` | `/api/v1/keys/{key_id}` | Revoke Key |
| `GET` | `/api/v1/positions` | Api Positions |
| `GET` | `/api/v1/risk` | Api Risk |
| `GET` | `/api/v1/trades` | Api Trades |
| `GET` | `/api/v1/watchlist` | Api Watchlist |

---

_This file is generated from the app's OpenAPI schema. To regenerate after adding endpoints, run the dashboard and export `GET /openapi.json`, or re-run the generation snippet in the repo history._
