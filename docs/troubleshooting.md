# Operations Runbook & Troubleshooting

> Field guide for the two production services (`ustradingbot` dashboard,
> `ustradingbot-engine` scanner/trader). For deploy mechanics see
> [deployment.md](deployment.md); for every env var see [env-vars.md](env-vars.md);
> for the API surface see [api.md](api.md).

Production runs `BROKER=paper` — it does **not** move real money. Host, SSH, and
service layout are documented in [deployment.md §1](deployment.md).

---

## Quick triage

```bash
# Are both services up?
systemctl is-active ustradingbot ustradingbot-engine

# Public liveness (no auth) — expect {"status":"ok", ...}
curl -s http://127.0.0.1:8501/health

# Recent engine activity (or "market_closed" off-hours)
journalctl -u ustradingbot-engine -n 50 --no-pager

# Recent dashboard errors (structured JSON; grep by level or request id)
journalctl -u ustradingbot -n 100 --no-pager | grep -i '"level": "error"'
```

Every dashboard request is logged with a `request_id` and echoed back on the
`X-Request-ID` response header. When a user reports a failure, ask for that id
and `grep` the journal for it — the unhandled-error log line carries the real
exception detail that the client (deliberately) never sees.

---

## 1. Engine won't start

**Symptom:** `systemctl is-active ustradingbot-engine` reports `failed` or
`activating`; dashboard "Engine" panel shows *stopped*; `open_positions.json`
and the heartbeat never update.

**Diagnose:**
```bash
systemctl status ustradingbot-engine --no-pager
journalctl -u ustradingbot-engine -n 100 --no-pager
```

**Common causes & fixes:**

| Cause | Signal in logs | Fix |
|---|---|---|
| Python/dependency error after a deploy | `ModuleNotFoundError`, `ImportError` on start | `.venv/bin/pip install -r requirements.txt`, then `systemctl restart ustradingbot-engine` |
| Bad `.env` value (type error) | `pydantic … ValidationError` | Correct the offending var in the prod `.env` (see [env-vars.md](env-vars.md)); restart |
| Corrupt runtime state file | `JSONDecodeError` reading `data_store/*.json` | Move the named file aside (`mv data_store/x.json /tmp/`); the engine reseeds it |
| Crash-loop (starts then exits) | repeated start/stop, `Restart=always` churning | Read the traceback in the journal; fix root cause. `systemctl reset-failed ustradingbot-engine` to clear the failed state |
| Market closed (not a fault) | `engine.market_closed` then a long sleep | Nothing to do — the engine idles until 09:30 ET and writes no positions until the first live cycle |

> **"Static data" is usually the engine, not the dashboard.** The dashboard only
> renders snapshots from `data_store/`. If numbers never move, check the engine
> first — a stopped engine leaves the dashboard showing the last written state.

---

## 2. WebSocket won't connect

**Symptom:** live P&L doesn't stream; browser console shows the WebSocket
closing immediately; the "live" indicator stays grey.

**Diagnose:**
```bash
# The WS endpoint (/ws/pnl) needs a short-lived token first (GET /api/ws/token).
curl -s -u admin:$DASHBOARD_PASSWORD http://127.0.0.1:8501/api/ws/token
```

**Common causes & fixes:**

- **Auth token missing/expired.** The realtime P&L socket (`/ws/pnl`)
  authenticates with a short-lived token minted at `GET /api/ws/token` (HTTP
  Basic required), passed as the `?token=` query parameter. A 401 there means
  the browser session isn't authenticated — log in again. Tokens are
  short-lived by design; the client re-mints on reconnect.
- **Reverse proxy not upgrading the connection.** A proxy in front of the
  dashboard must forward the `Upgrade`/`Connection` headers for `wss://`. For
  nginx: `proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";`
- **CSP blocking the socket.** The dashboard's Content-Security-Policy allows
  `connect-src 'self' ws: wss:`. If you tightened the CSP (in
  `dashboard/middleware.py`), make sure the WebSocket scheme is still permitted —
  a CSP violation shows in the browser console, not the server log.
- **Engine not running.** No engine → no P&L updates to push. See §1.

---

## 3. OpenRouter 401 (AI features degraded)

**Symptom:** the Analyst / AI Commentary page renders deterministic template
prose instead of LLM narration; the Memory tab stays empty (no new lessons);
engine logs show `401 Unauthorized` from OpenRouter.

**This never affects trading** — every AI layer is fail-open (veto) or
fail-soft (commentary/reflection). It is a narration outage, not a trading one.

**Diagnose & fix:**
```bash
# Where is the key coming from? (loose file wins over a placeholder .env value)
cat keys/openrouter-key 2>/dev/null | head -c 12; echo

# Verify the key directly against OpenRouter
curl -s https://openrouter.ai/api/v1/models \
  -H "Authorization: Bearer $(cat keys/openrouter-key)" -o /dev/null -w '%{http_code}\n'
```

- The key resolves in this order: `OPENROUTER_API_KEY` env/`​.env` → loose
  `keys/openrouter-key` file → unset. A **placeholder** value in `.env` (e.g.
  `your_openrouter_key_here`) is treated as *unset* so the loose file can win —
  that behaviour is deliberate ([see settings loading order](env-vars.md)).
- Put a working key in `keys/openrouter-key` (or a real value in `.env`), then
  `systemctl restart ustradingbot` (dashboard) and, for reflections,
  `ustradingbot-engine`.
- Free models (`openai/gpt-oss-20b:free`) cost `$0`; a 401 is an auth problem,
  not a billing one.

---

## 4. Broker connection issues

Production is `BROKER=paper` (the built-in simulated broker — no gateway, no
keys), so broker connectivity is normally a non-issue. These apply only when
`BROKER=ibkr`.

**Symptom:** entries/exits don't fill; engine logs show reconnect attempts.

**Diagnose:**
```bash
journalctl -u ustradingbot-engine -n 100 --no-pager | grep -iE 'broker|reconnect|ibkr|gateway'
```

**Common causes & fixes:**

- **Gateway/TWS not running or wrong port.** `IBKR_PORT` must point at a live
  gateway (`7496`) for live or the paper gateway (`7497`). The engine retries
  with exponential backoff (`RECONNECT_*` settings) before giving up on a cycle;
  persistent failures mean the gateway is down or unreachable.
- **Client-id collision.** Two clients sharing `IBKR_CLIENT_ID` get disconnected
  by IBKR. Give each process a distinct id.
- **Accidentally live.** `IS_LIVE_TRADING` is true only when `BROKER=ibkr` **and**
  `IBKR_PORT=7496`. If you did not intend to trade real money, set
  `BROKER=paper` and restart the engine. Switching *to* live requires the admin
  password through the dashboard.
- **Market-data provider rate limits (separate from the broker).** Polygon's
  free tier caps ~5 req/min and returns `429`; the `FallbackProvider` trips after
  `PROVIDER_FALLBACK_TRIP_THRESHOLD` failures and routes to
  `MARKET_DATA_FALLBACK_PROVIDER` (default `yfinance`). Zero signals for a whole
  scan is the classic starved-provider symptom — check for `429` in the logs.

---

## 5. Dashboard returns errors

| Status | Meaning | Action |
|---|---|---|
| `401` on `/` or any API | Not authenticated (this is the healthy fail-closed state in prod) | Supply HTTP Basic creds (`DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`) |
| `403` on a trade/engine/mode action | Wrong or missing **admin** password in the request body | Send the correct `admin_password` (`DASHBOARD_ADMIN_PASSWORD`, falling back to `DASHBOARD_PASSWORD`) |
| `500` on every authenticated page | Auth enabled but `DASHBOARD_PASSWORD` unset (fail-closed) | Set `DASHBOARD_PASSWORD` in the prod `.env`; restart the dashboard |
| `500` with `{"error_code":"internal_error"}` | Unhandled exception | Grab the `request_id` from the body/`X-Request-ID` header; `grep` the journal for it to get the real traceback |
| `413` `payload_too_large` | Request body over 1 MiB | Legitimate guard — shrink the payload |
| `422` `validation_error` | Malformed JSON or bad field | Fix the request body; the `errors` array names the offending field |
| `429` | Rate-limited (login lockout or trade/control cap) | Back off for the advertised `Retry-After` (first login lockout is 60s, escalating to `RATE_LIMIT_LOGIN_LOCKOUT_MINUTES`); retrying early no longer extends it. To clear a lockout immediately, restart the dashboard — the state is in-process unless `RATE_LIMIT_REDIS_URL` is set, in which case `redis-cli del rll:<ip> rlf:<ip> rlr:<ip>`. See [env-vars.md](env-vars.md) `RATE_LIMIT_*` |

**Repeated `auth.failure` WARN logs** (`grep auth.failure`) show the source IP
and reason (`missing_credentials` / `invalid_credentials`) — a burst from one IP
is a brute-force attempt and will trip the login lockout automatically.

---

## 6. Common log events (glossary)

| Event | Where | Meaning |
|---|---|---|
| `engine.market_closed` | engine | Outside 09:30–16:00 ET; idling. Normal. |
| `request` | dashboard | One access-log line per request (method, path, status, `duration_ms`, `request_id`). |
| `unhandled_exception` | dashboard | A bug reached the global handler; carries the traceback + `request_id`. |
| `auth.failure` | dashboard | A rejected login (WARN), with `ip` and `reason`. |
| `engine_control.action` / `.action_failed` | dashboard | An operator start/stop/restart of the engine. |
| `provider.fallback_tripped` | engine | Primary data provider circuit-breaker opened; using the fallback. |

---

## Escalation checklist

1. `systemctl status` both units; capture the last 100 journal lines of each.
2. `curl /health` — is the process even up?
3. Identify the failing request's `request_id` and grep both journals for it.
4. If a deploy preceded the incident, confirm deps installed and `.env` didn't
   drift (see [deployment.md §4](deployment.md)); roll back to the newest
   `/opt/USTradingBot.bak.*` if needed.
5. Back up `data_store/` before mutating any runtime state file (see
   [`scripts/backup.sh`](../scripts/backup.sh)).
