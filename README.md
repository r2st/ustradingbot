# US Trading Bot

Autonomous multi-signal trading system for US & Canadian equities. It scans a
universe of stocks, scores them with five strategies (Momentum, Swing, VCP
Breakout, PEAD, Mean Reversion), applies an AI news veto and risk checks, and
places bracket orders — all with a full audit trail.

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

---

## Backtesting

Replay the same strategies over historical data using the same fill model as the
paper broker:

```bash
python -m backtest --symbols AAPL,MSFT,NVDA --start 2023-01-01 --end 2024-01-01
# add --output bt_out to write summary.json / equity_curve.csv / trades.csv
```

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

---

## Testing

```bash
python -m pytest -q
```
