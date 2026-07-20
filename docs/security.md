# Security notes

Operational security model for the dashboard + engine. Complements the hardening
already tracked in [`IMPROVEMENTS.md`](IMPROVEMENTS.md) and the second-pass
findings in [`AUDIT_V2.md`](AUDIT_V2.md).

## Secrets at rest — the `.env` trust model (audit B-6)

Broker and LLM/provider credentials are stored **in plaintext** in the project
`.env` file. This is a deliberate, documented trade-off, not an oversight:

- `.env` is the single source of truth that `config.settings.Settings` reads at
  start-up, and the dashboard writes to it when you switch mode or save provider
  keys (`dashboard/mode_control.py`, `dashboard/provider_control.py`).
- REST **API keys** (the external `/api/v1` surface) are stored **hashed**
  (`dashboard/api_keys.py`) because they are verified, not replayed. Broker/LLM
  secrets must be sent verbatim to the upstream, so they cannot be hashed.

**Controls in place:**

1. **File permissions.** Every write to `.env` re-applies mode `0600`
   (owner read/write only) via `update_env_var`, so a stray group/world read
   cannot lift live credentials. Verify with `ls -l .env`.
2. **Host trust.** The dashboard is meant to run behind a TLS-terminating
   reverse proxy on a single-tenant host (see `docs/deployment.md`). Anyone with
   filesystem access to `.env` already has the machine.
3. **Do not commit `.env`.** It is git-ignored; keep it that way.

**If you need stronger at-rest protection**, read the values from a secrets
manager (systemd `LoadCredential=`, Vault, cloud secret store) and inject them as
environment variables instead of persisting to `.env`. The settings layer already
prefers real environment variables over the file.

## Restart handshake (audit B-6)

A mode/provider/key change persists to `.env` and drops a **restart sentinel**
(`data_store/restart.flag`) that the running engine polls each loop; when it sees
the sentinel it re-execs and picks up the new config. This is intentionally
decoupled (dashboard and engine are separate processes).

To make the handshake observable rather than fire-and-forget, the engine writes
an **ack** (`data_store/restart_ack.json`) when it consumes the sentinel, and the
dashboard exposes `GET /api/engine/restart-status`:

```json
{ "pending": false, "acked_at": "2026-07-20T09:31:02-04:00", "target": "live" }
```

`pending: true` that never clears means the engine isn't running or isn't polling
— surface that to the operator instead of assuming the change took effect.

## CSRF (audit B-5)

Auth is HTTP Basic, which browsers cache and auto-attach cross-site, so the
dashboard adds an explicit same-origin `Origin`/`Referer` check on state-changing
methods (`dashboard/middleware.py`). Set `CSRF_TRUSTED_ORIGINS` to your public
URL when running behind a proxy that rewrites `Host`.

## Rate limiting under multiple workers (audit B-4)

The limiter and login lockout are process-local by default. If you scale to
`uvicorn --workers N` (or `WEB_CONCURRENCY > 1`), set `RATE_LIMIT_REDIS_URL` to a
shared Redis so the caps stay correct cluster-wide; otherwise the app logs a loud
startup WARN and the effective cap becomes ~`N×`.
