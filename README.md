# US Trading Bot

Autonomous multi-signal trading system for US & Canadian equities. It scans a
**tiered universe** built on the S&P 500 and NASDAQ-100, scores names with five
strategies (Momentum, Swing, VCP Breakout, PEAD, Mean Reversion), applies an AI
news veto and risk checks, and places bracket orders — all with a full audit
trail. It also **learns from its own trades**: after each close it writes itself
a plain-English lesson and consults that memory (plus its historical win rate)
before the next entry.

> **Paper trading is the default.** Out of the box the bot trades with
> **simulated money** through a built-in broker. No brokerage account, no API
> keys, no gateway. You can clone, run, and watch it trade in minutes — nothing
> you do risks real capital until you *explicitly* switch to live.

---

## Quick start (paper trading — zero setup)

```bash
# 1. Install
python -m pip install -e ".[dev]"

# 2. (optional) create a .env — the defaults already run paper trading
cp .env.example .env

# 3. Start the trading engine (simulated broker, no gateway needed)
python engine.py

# 4. In another terminal, open the dashboard
uvicorn dashboard.app:app --host 127.0.0.1 --port 8501
#    then browse to http://127.0.0.1:8501
```

The dashboard opens with a green **PAPER TRADING** banner. As the engine runs
during US market hours, the **Paper Trading Account** section fills in with your
simulated positions, P&L, cash balances, and trade history.

Fills are simulated **realistically** — entries take slippage, stops model
gap-throughs, and every trade is charged a per-share commission — so paper
results are a fair preview of live behaviour rather than an optimistic one.

### The only thing you might set

The dashboard is protected by HTTP Basic Auth and **fails closed**. For local
use either set a password or disable auth:

```bash
# .env
DASHBOARD_PASSWORD=some_strong_password      # then log in as admin / <password>
# — or, for trusted local dev only —
DASHBOARD_AUTH_ENABLED=False
```

---

## Paper vs. live: how the mode is decided

| `BROKER` | `IBKR_PORT` | Mode | Real money? |
|----------|-------------|------|-------------|
| `paper` (default) | *(any)* | **PAPER** | No — built-in simulator |
| `ibkr` | `7497` | **PAPER** | No — IBKR paper gateway |
| `ibkr` | `7496` | **LIVE** | **Yes** |

The mode is surfaced everywhere so you always know where you stand:

- A prominent banner at the top of the dashboard (green = paper, red = live).
- The badge in the dashboard header and the **Trading Mode** row in System Status.
- `GET /api/mode` → `{"trading_mode": "PAPER", "is_paper": true, ...}`
- `GET /health` → includes `"trading_mode"` (public, for monitors).

### Switch to live trading (real money)

1. Open and fund an Interactive Brokers account; run TWS or IB Gateway.
2. In `.env`:
   ```bash
   BROKER=ibkr
   IBKR_PORT=7496          # 7496 = LIVE, 7497 = IBKR paper
   IBKR_HOST=127.0.0.1
   IBKR_ACCOUNT_ID=Uxxxxxxx
   ```
   and install the live extra: `pip install -e ".[live]"`.
3. Restart the bot. The dashboard banner turns **red / LIVE**.

### Switch back to paper

Set `BROKER=paper` (or just delete the `BROKER` line — paper is the default) and
restart. No keys required.

> A one-click live/paper toggle is deliberately **not** exposed on the
> dashboard: flipping to real-money trading from a web page is too easy to do by
> accident. The switch is an explicit config change.

---

## Dashboard reference

| Route | Auth | What it shows |
|-------|------|----------------|
| `/` | yes | Full dashboard: mode banner, paper account, strategies, risk rules, help |
| `/health` | no | Liveness + current trading mode |
| `/api/mode` | no | Current mode / broker (read-only) |
| `/api/paper/summary` | yes | Balance, realized & today's P&L, headline stats |
| `/api/paper/positions` | yes | Open paper positions |
| `/api/paper/trades` | yes | Recent completed paper trades |
| `/api/analytics/summary` | yes | Win rate, profit factor, Sharpe/Sortino, max drawdown … |
| `/api/analytics/by-strategy` · `/by-symbol` | yes | Performance breakdowns |
| `/api/analytics/equity-curve` | yes | Cumulative equity curve |
| `/api/memory/overview` | yes | **AI Memory** tab — learnings, reflections, guard decisions, stats |
| `/api/universe/tiers` · `/indices` · `/scan-pool` · `/promotions` | yes | **Universe Browser** — tier cards, index membership, scan pool, auto-promotions |

The dashboard has two feature tabs covered by their own docs:

- **AI Memory** — the lessons the bot wrote itself, a reflections timeline, and
  the block/demote decisions the memory guards applied. See
  [`docs/memory-learning-layer.md`](docs/memory-learning-layer.md).
- **Universe** — S&P 500 / NASDAQ-100 membership, the ranked scan pool, live
  auto-promotions (with TTL and a demote control), and tier breakdown cards. See
  [`docs/tiered-universe.md`](docs/tiered-universe.md).

---

## Learning & memory

The bot closes the loop on its own trade ledger with two features that run
automatically:

- **AI Trade Reflection (F1)** — after a trade closes, an OpenRouter model
  writes one plain-English lesson to `data_store/learnings.jsonl`. Before the
  next matching entry a guard applies binding lessons (`avoid` → reject,
  `require_confirm` → grade-A-only, `prefer` → annotate). Two guardrails
  (support + confidence gates) stop one-off or low-confidence lessons from
  steering the bot.
- **Similar-Setup Guard (F2)** — before each entry the bot queries `trades.csv`
  for historically similar setups (same strategy/grade, RSI & volume within
  tolerance) and computes the win rate → **proceed / demote / block**. Pure,
  local, no API call.

Both run as entry gates *after* the AI veto and are **fail-open** — a missing
key or corrupt ledger never blocks a trade. Full explanation, config, and the
learning-loop diagram: [`docs/memory-learning-layer.md`](docs/memory-learning-layer.md).

## Tiered symbol universe

Scanning is organised into three tiers over the S&P 500 and NASDAQ-100:

- **Tier 1 (Active Trading)** — watchlist ∪ ETFs ∪ auto-promoted symbols,
  scanned **every cycle**.
- **Tier 2 (Scan Pool)** — top-N S&P 500 by volume × market cap, scanned
  **daily**.
- **Tier 3 (Universe)** — full S&P 500 ∪ NASDAQ-100, pre-screened for movers
  **weekly**.

Any Tier 2/3 signal **auto-promotes** its symbol into Tier 1 for
`PROMOTION_TTL_HOURS`. The universe is seeded into a SQLite DB
(`python -m data_store.universe_seeder`) with a static offline fallback so every
tier is populated even with no DB. Full details:
[`docs/tiered-universe.md`](docs/tiered-universe.md).

---

## Backtesting

Replay the same strategies over historical data using the same fill model as the
paper broker:

```bash
python -m backtest --symbols AAPL,MSFT,NVDA --start 2023-01-01 --end 2024-01-01
# add --output bt_out to write summary.json / equity_curve.csv / trades.csv
```

The dashboard's backtest form offers the **full tradeable universe**: its symbol
picker (`dashboard/backtest_control._full_symbol_set()`) merges the built-in
equities (`ALL_SYMBOLS`), all ETFs (`ALL_ETFS`), and any symbols the operator
added from the dashboard watchlist — so names like `MU`, `QCOM`, or `SOXL` show
up automatically. A single run accepts up to `_MAX_SYMBOLS` (100) symbols.

---

## Configuration

All settings live in `config/settings.py` and load from environment variables /
`.env`. See [`.env.example`](.env.example) for every option with comments. The
most relevant for paper trading:

| Setting | Default | Purpose |
|---------|---------|---------|
| `BROKER` | `paper` | `paper` (simulated) or `ibkr` (live/IBKR-paper) |
| `PAPER_SLIPPAGE_BPS` | `5.0` | Simulated slippage on entries/stops (basis points) |
| `PAPER_COMMISSION_PER_SHARE` | `0.005` | Simulated per-share commission |
| `TOTAL_CAPITAL` | `12000` | Paper account starting capital |
| `MARKET_DATA_PROVIDER` | `yfinance` | Data backend (`yfinance` or `alpaca`) |
| `LEARNINGS_ENABLED` | `True` | AI trade reflection + learnings guard (needs `OPENROUTER_API_KEY`) |
| `SIMILAR_SETUP_ENABLED` | `True` | Similar-setup win-rate guard (no API key needed) |
| `TIERED_SCANNING_ENABLED` | `True` | Three-tier S&P 500 / NASDAQ-100 scanning |
| `TIER2_SCAN_POOL_SIZE` | `250` | Top-N S&P 500 names in the daily scan pool |
| `PROMOTION_TTL_HOURS` | `72.0` | How long a Tier 2/3 signal keeps a symbol in Tier 1 |

See the feature docs for the full setting lists:
[memory & learning](docs/memory-learning-layer.md#5-configuration) ·
[tiered universe](docs/tiered-universe.md#4-configuration).

---

## Documentation

| Doc | Covers |
|-----|--------|
| [`SYSTEM_DESIGN.md`](SYSTEM_DESIGN.md) | Full system design — architecture, strategies, risk, execution, AI veto, schema. |
| [`docs/memory-learning-layer.md`](docs/memory-learning-layer.md) | AI trade reflection, learnings guard, similar-setup guard, the learning loop, AI Memory tab. |
| [`docs/tiered-universe.md`](docs/tiered-universe.md) | Three-tier universe, auto-promotion, universe seeding, Universe Browser tab. |
| [`docs/deployment.md`](docs/deployment.md) | Hetzner production deploy, the two systemd services, verification, gotchas. |
| [`docs/`](docs/) | Feature specs (monitoring, short selling, TA dashboard, earnings/ETF/after-hours, AI memory). |

---

## Deployment

Production runs on a Hetzner VPS as two systemd services (`ustradingbot`
dashboard + `ustradingbot-engine` trading loop). Code ships via `git archive`
over SSH because the server is not a git checkout. See
[`docs/deployment.md`](docs/deployment.md) for the full procedure, verification
steps, and gotchas.

---

## Testing

Use the project virtualenv — system/anaconda pythons lack the dependencies:

```bash
.venv/bin/python -m pytest tests/ short_strategies/tests/ -q
```
