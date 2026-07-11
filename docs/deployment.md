# Deployment

> How USTradingBot runs in production on a Hetzner VPS, how to ship new code to
> it, and the gotchas that have bitten past deploys. Production runs
> **`BROKER=paper`** — it does not trade real money.

---

## 1. Production topology

| | |
|---|---|
| **Host** | Hetzner VPS `ubuntu-4gb-hel1-3` at `204.168.241.124` |
| **SSH** | user `root`, key `keys/hetzner_ustradingbot` |
| **App dir** | `/opt/USTradingBot` (a plain file copy — **not** a git repo) |
| **Runtime** | project virtualenv at `/opt/USTradingBot/.venv` |
| **Broker** | `BROKER=paper` (simulated money) |

The app runs as **two** systemd services — deliberately separate processes.

### `ustradingbot.service` — the dashboard

```
ExecStart = uvicorn dashboard.app:app --host 0.0.0.0 --port 8501
WorkingDirectory = /opt/USTradingBot
Restart = always
```

The dashboard is **read-only**: it renders JSON/CSV snapshots from
`data_store/`. It does not scan or trade.

### `ustradingbot-engine.service` — the trading engine

```
ExecStart = .venv/bin/python engine.py
WorkingDirectory = /opt/USTradingBot
Restart = always
TimeoutStopSec = 20
```

The engine is the scanner + trade loop. It writes the snapshots the dashboard
reads. **Without the engine running, dashboard data never moves** — that is the
classic "static data" symptom, not a dashboard bug.

### Why they are separate

On a provider/mode switch the dashboard drops `data_store/restart.flag` and the
engine self-restarts (`os.execv`) to pick it up. A mode switch must **not** kill
the dashboard, so the two must stay separate processes — do not merge them.

The engine respects US market hours: outside 09:30–16:00 ET on weekdays it logs
`engine.market_closed` and sleeps `SCAN_INTERVAL_MINUTES` (prod = 60), writing no
`open_positions.json` until the first live cycle. It writes
`data_store/engine_status.json` (heartbeat: phase, `next_scan_at`, open
positions, pid) each loop for the dashboard's Engine Control panel, and uses an
interruptible between-cycle sleep so `systemctl stop/restart` returns fast.

---

## 2. Shipping code to production

`/opt/USTradingBot` is **not** a git repo (code was deployed as a file copy), so
`git pull` is impossible there. Ship tracked files with `git archive` over SSH —
this preserves the server's untracked runtime state (`.env`, `keys/`, `.venv/`,
`data_store/`).

```bash
# From the repo root, on the commit you want to ship (e.g. main @ HEAD):

# 1. Back up the current app first (backups are ~243 MB — prune old ones).
ssh -i keys/hetzner_ustradingbot root@204.168.241.124 \
  'cp -a /opt/USTradingBot /opt/USTradingBot.bak.$(date +%s)'

# 2. Ship tracked files (only tracked files travel; runtime state is preserved).
git archive --format=tar HEAD \
  | ssh -i keys/hetzner_ustradingbot root@204.168.241.124 \
        'tar -x -C /opt/USTradingBot'

# 3. If the deploy adds pip deps, install them into the server venv.
ssh -i keys/hetzner_ustradingbot root@204.168.241.124 \
  '/opt/USTradingBot/.venv/bin/pip install -r /opt/USTradingBot/requirements.txt'

# 4. Restart BOTH services.
ssh -i keys/hetzner_ustradingbot root@204.168.241.124 \
  'systemctl restart ustradingbot ustradingbot-engine'
```

Keep only the 3 newest `/opt/USTradingBot.bak.*` backups.

> **Engine-read config changes need the engine restarted.** Anything the engine
> reads each cycle (watchlists, trade selection, settings) only takes effect
> after `systemctl restart ustradingbot-engine`. A dashboard-only change needs
> only `ustradingbot`.

---

## 3. Post-deploy verification

```bash
# Public health (no auth) — expect 200 JSON {status:ok, trading_mode:PAPER}
curl -s http://204.168.241.124:8501/health

# Authenticated dashboard root — expect 200 (401 if unauthenticated is HEALTHY)
curl -s -u admin:$DASHBOARD_PASSWORD http://204.168.241.124:8501/ -o /dev/null -w '%{http_code}\n'

# Feature endpoints (memory + universe), authenticated — expect 200
curl -s -u admin:$DASHBOARD_PASSWORD http://204.168.241.124:8501/api/memory/overview -o /dev/null -w '%{http_code}\n'
curl -s -u admin:$DASHBOARD_PASSWORD http://204.168.241.124:8501/api/universe/tiers  -o /dev/null -w '%{http_code}\n'

# Both services active
ssh -i keys/hetzner_ustradingbot root@204.168.241.124 \
  'systemctl is-active ustradingbot ustradingbot-engine'

# Tail engine logs to confirm a cycle ran (or market_closed off-hours)
ssh -i keys/hetzner_ustradingbot root@204.168.241.124 \
  'journalctl -u ustradingbot-engine -n 50 --no-pager'
```

Because `DASHBOARD_PASSWORD` is set in prod, `/` returns **HTTP 401** (Basic
auth) when unauthenticated — this is the healthy fail-closed state, *not* the
old HTTP 500.

---

## 4. Gotchas that have bitten deploys

- **`.env` config drift.** The server `.env` predates recent settings additions.
  When a deploy adds a **required** env var, update the prod `.env` too, or the
  dashboard can fail closed (HTTP 500/401). Most new features default safely
  (`Settings` uses `extra="ignore"`), so a code deploy usually needs no `.env`
  edit — but verify. The memory layer and tiered universe added **no required**
  env vars (all `LEARNINGS_*` / `SIMILAR_SETUP_*` / `TIER*` / `PROMOTION_*`
  default safely).

- **`OPENROUTER_API_KEY` for AI features.** F1 trade reflection and the AI
  commentary/analyst pages need a *working* OpenRouter key. A rejected key (401)
  means no lessons get written and the analyst degrades to deterministic
  template prose — trading is unaffected (all fail-open/degraded), but the
  Memory tab stays empty. F2 (similar-setup guard) is pure stats and needs no
  key. Refresh the key in the prod `.env` and restart the dashboard to restore
  AI narration.

- **`SCHEMA_COLUMNS` changes are data migrations.** `csv.DictWriter` appends by
  field *name* regardless of the on-disk header, so adding a column to
  `journal/trade_logger.SCHEMA_COLUMNS` can make the first post-deploy trade
  write more fields than the old header declares, breaking `pd.read_csv` and
  500-ing the dashboard. `TradeLogger` now self-heals the header on the engine's
  next start, and `load_completed_trades` skips bad lines — but **verify the
  prod `trades.csv` field count** after such deploys.

- **Runtime state is preserved, not shipped.** `git archive` ships only tracked
  files. Server-only runtime files in `data_store/` (e.g. `trade_selection.json`,
  `watchlists.json`, `users.json`, `api_keys.json`, `learnings.jsonl`,
  `universe.db`) are gitignored and survive deploys. Edits made live to those
  files persist across deploys — but are **not** in version control.

- **Polygon free tier.** Aggregates (`/v2/aggs`) work; the real-time last-trade
  endpoint returns 403 on the free plan, so `PolygonProvider.get_current_price`
  falls back to the previous-day close. The `FallbackProvider` wraps any
  non-yfinance primary and falls back to `MARKET_DATA_FALLBACK_PROVIDER`
  (default `yfinance`) with a 3-strike circuit breaker.

---

## 5. Local development

```bash
# Install (dev extras)
python -m pip install -e ".[dev]"

# Engine (simulated broker, no gateway needed)
python engine.py

# Dashboard (separate terminal)
uvicorn dashboard.app:app --host 127.0.0.1 --port 8501

# Tests — use the project venv (system/anaconda pythons lack deps)
.venv/bin/python -m pytest tests/ short_strategies/tests/ -q
```

For browser testing without auth, the `.claude/launch.json` "dashboard-preview"
config runs uvicorn with `DASHBOARD_AUTH_ENABLED=false`.
