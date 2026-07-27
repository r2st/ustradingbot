# US Trading Bot — System Design Document

**Version:** 1.0  
**Date:** July 2, 2026  
**Status:** Implementation Ready  

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Architecture Overview](#2-architecture-overview)
3. [Tech Stack](#3-tech-stack)
4. [Data Pipeline](#4-data-pipeline)
5. [Trading Strategy Engine](#5-trading-strategy-engine)
6. [Risk Management](#6-risk-management)
7. [Execution Layer](#7-execution-layer)
8. [AI Veto Layer](#8-ai-veto-layer)
9. [Paper Trading vs Live Trading](#9-paper-trading-vs-live-trading)
10. [Monitoring and Alerting](#10-monitoring-and-alerting)
11. [Database Schema](#11-database-schema)
12. [API Design](#12-api-design)
13. [Deployment Architecture](#13-deployment-architecture)
14. [Security Considerations](#14-security-considerations)
15. [Project Structure](#15-project-structure)
16. [Memory & Learning Layer](#16-memory--learning-layer)
17. [Tiered Symbol Universe](#17-tiered-symbol-universe)

> **Note:** Sections 16–17 document features added after the v1.0 design.
> Sections 1–15 describe the original IBKR/Claude architecture; the AI veto now
> also runs via OpenRouter and paper trading uses the built-in simulator by
> default. See [`README.md`](README.md) for current operational defaults and the
> [`docs/`](docs/) feature specs for later additions.

---

## 1. Executive Summary

The US Trading Bot is an autonomous algorithmic trading system that connects to Interactive Brokers via the `ib_insync` library, scans a configurable universe of US and Canadian equities, detects trade signals using a multi-indicator scoring engine (RSI, MACD, EMA structure, Volume analysis, Ripster EMA Clouds), passes candidates through a Claude AI veto layer for news-based risk filtering, and executes bracket orders with automated exit management.

The system operates on a 60-minute scan interval during market hours (9:30 AM – 4:00 PM ET), processes exits before entries each cycle, and enforces strict risk controls including per-trade risk limits (1.5% of capital), daily loss limits, per-strategy position caps, and a multi-gate signal validation pipeline.

### Key Design Principles

- **Exit-first processing**: Every cycle manages existing positions before scanning for new entries, ensuring no stale positions accumulate.
- **Defense-in-depth risk management**: Multiple independent layers (hard vetoes, scoring thresholds, risk pre-checks, AI veto, freshness checks, cash verification) each capable of blocking a trade.
- **Broker as source of truth**: The internal journal tracks intent; all exit decisions confirm against IBKR's live position state using server-side execution history.
- **Fail-safe defaults**: The AI layer defaults to REJECT on any error. Position sizing returns zero (skip) rather than a 1-share floor. Bracket orders ensure every entry has a stop and target on the exchange.
- **Stateless restarts**: All position state persists to disk (JSON/CSV) so the system can recover from crashes, TWS restarts, and network interruptions.

---

## 2. Architecture Overview

### 2.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           TRADING ENGINE (engine.py)                        │
│                     Main Orchestrator — 60-min Scan Loop                    │
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐   │
│  │ Exit Manager  │  │  Screener    │  │ Risk Manager │  │ Order Builder │   │
│  │              │  │              │  │              │  │               │   │
│  │ • broker_exit│  │ • VCP        │  │ • pre_check  │  │ • bracket     │   │
│  │ • time_exit  │  │ • PEAD       │  │ • sizing     │  │ • limit entry │   │
│  │ • health_chk │  │ • Momentum   │  │ • stop valid │  │ • stop loss   │   │
│  │ • trail_stop │  │ • Swing      │  │ • caps check │  │ • take profit │   │
│  └──────┬───────┘  │ • MeanRev    │  └──────┬───────┘  └───────┬───────┘   │
│         │          └──────┬───────┘         │                  │            │
│         │                 │                 │                  │            │
└─────────┼─────────────────┼─────────────────┼──────────────────┼────────────┘
          │                 │                 │                  │
          ▼                 ▼                 │                  ▼
┌─────────────────┐ ┌──────────────────┐     │        ┌──────────────────┐
│   IBKR Broker   │ │  Signal Scoring  │     │        │   IBKR Broker    │
│   (Execution)   │ │    Engine        │     │        │ (Order Placement)│
│                 │ │                  │     │        │                  │
│ • reqExecutions │ │ ┌──────────────┐ │     │        │ • bracketOrder() │
│ • positions()   │ │ │ RSI Signals  │ │     │        │ • placeOrder()   │
│ • cancelOrder() │ │ │ MACD Signals │ │     │        │ • reqOpenOrders()│
│ • placeOrder()  │ │ │ EMA Signals  │ │     │        └──────────────────┘
└─────────────────┘ │ │ Volume Sigs  │ │     │
                    │ │ Ripster Cloud│ │     │
                    │ └──────────────┘ │     │
                    │                  │     │
                    │ Combined Filter  │     │
                    │ (weighted score) │     │
                    └────────┬─────────┘     │
                             │               │
                             ▼               │
                    ┌──────────────────┐      │
                    │   AI Veto Layer  │      │
                    │                  │◄─────┘
                    │ Tier 1: Earnings │
                    │ Tier 2: Claude   │
                    │   News Analysis  │
                    │ (w/ web_search)  │
                    └──────────────────┘
                             │
          ┌──────────────────┼──────────────────┐
          ▼                  ▼                  ▼
┌─────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│  Data Layer     │ │  Trade Journal   │ │  State Store     │
│                 │ │                  │ │                  │
│ • yfinance OHLCV│ │ • trades.csv     │ │ • open_positions │
│ • earnings cal  │ │ • rejected.jsonl │ │   .json          │
│ • universe.py   │ │ • 37-col schema  │ │ • AI cache       │
└─────────────────┘ └──────────────────┘ └──────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                         REPORTING & MONITORING                              │
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐   │
│  │ Telegram Bot │  │  Scheduler   │  │   Logging    │  │  Health       │   │
│  │              │  │              │  │  (structured)│  │  Metrics      │   │
│  │ • daily P&L  │  │ • cron jobs  │  │              │  │              │   │
│  │ • alerts     │  │ • market hrs │  │ • per-module │  │ • capture_R  │   │
│  │ • commands   │  │ • intervals  │  │ • JSON fmt   │  │ • win rate   │   │
│  └──────────────┘  └──────────────┘  └──────────────┘  └───────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Data Flow Per Cycle

```
START CYCLE
    │
    ├─► 1. check_broker_exits()     ── reqExecutions() → reconcile journal vs IBKR
    ├─► 2. check_time_based_exits() ── tiered smart exit (zombie/loss/partial/trail)
    ├─► 3. check_position_health()  ── re-score open positions, exit if SETUP_BROKEN
    ├─► 4. check_trailing_stops()   ── ratchet stops on partial-fill positions
    │
    ├─► 5. Order cutoff check       ── if < 5 min to close → skip entries
    │
    ├─► 4b. _reflect_on_exits()     ── F1: write one learnings.jsonl lesson per closed trade
    │
    ├─► 5. Order cutoff check       ── if < 5 min to close → skip entries
    │
    ├─► 6. Tiered scan              ── Tier 1 (every cycle) + Tier 2 (daily) + Tier 3 (weekly)
    │       │                          Tier 2/3 signals auto-promote symbols into Tier 1
    │       └─► For each signal (sorted by strength desc):
    │           ├─► trade-selection gate   (operator's pinned symbols/strategies/grade)
    │           ├─► earnings block/flag     (skip earnings-proximate entries)
    │           ├─► pre_check()            (risk manager structural gates)
    │           ├─► strategy position cap   (per-strategy limits)
    │           ├─► pending order guard     (no duplicate orders)
    │           ├─► AI evaluation           (Tier 1 earnings + Tier 2 OpenRouter veto)
    │           ├─► similar-setup guard (F2) (own win rate → proceed/demote/block)
    │           ├─► learnings guard (F1)     (apply lessons → allow/demote/reject)
    │           ├─► news-sentiment veto      (reject on strong negative news)
    │           ├─► ratings veto             (reject below quant-rating floor)
    │           ├─► regime + auto-tune gate  (raise the bar in adverse regimes)
    │           ├─► build_order()           (risk manager sizing)
    │           ├─► freshness check          (drift < 1%, age < 15 min)
    │           ├─► cash check               (sufficient buying power)
    │           └─► place_bracket_order()    (entry + stop + target)
    │
    └─► 7. Log trade, update open_positions.json, deduct cash
END CYCLE (sleep until next interval)
```

The similar-setup and learnings guards (steps after the AI veto) and the tiered
scan are covered in dedicated sections — see
[16. Memory & Learning Layer](#16-memory--learning-layer) and
[17. Tiered Symbol Universe](#17-tiered-symbol-universe).

---

## 3. Tech Stack

### 3.1 Core Language & Runtime

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Language | Python 3.11+ | Dominant language for quantitative finance; rich ecosystem for data analysis, ML, and broker APIs |
| Async Framework | asyncio | ib_insync is built on asyncio; enables non-blocking I/O for market data and order management |
| Package Manager | pip + pyproject.toml | Standard Python packaging with PEP 621 metadata |

### 3.2 Key Libraries

| Library | Version | Purpose |
|---------|---------|---------|
| `ib_insync` | >=0.9.86 | Interactive Brokers API wrapper (sync/async) |
| `yfinance` | >=0.2.36 | Market data (OHLCV, earnings calendar) |
| `pandas` | >=2.2 | DataFrame operations for indicator calculations |
| `numpy` | >=1.26 | Numerical computations for scoring engine |
| `anthropic` | >=0.50 | Claude API for AI veto layer |
| `pydantic` | >=2.6 | Configuration validation, data models |
| `python-dotenv` | >=1.0 | Environment variable management |
| `structlog` | >=24.1 | Structured JSON logging |
| `pytest` | >=8.0 | Testing framework |
| `pytest-asyncio` | >=0.23 | Async test support |
| `ta` | >=0.11 | Technical analysis indicators (Bollinger Bands) |
| `schedule` | >=1.2 | Cron-like scheduling for scan loops |
| `python-telegram-bot` | >=21.0 | Telegram reporting integration |
| `aiofiles` | >=23.2 | Async file I/O for journal writes |

### 3.3 External Services

| Service | Purpose | Auth Method |
|---------|---------|-------------|
| Interactive Brokers TWS/Gateway | Order execution, position management | TCP localhost (port 7496/7497) |
| Claude API (Sonnet 4.5) | News veto with web search | API key (env var) |
| yfinance (Yahoo Finance) | Historical OHLCV data, earnings calendar | Public API (no auth) |
| Telegram Bot API | Alerts and daily reports | Bot token (env var) |

---

## 4. Data Pipeline

### 4.1 Market Data Ingestion

```
Yahoo Finance API (yfinance)
        │
        ▼
┌─────────────────────────┐
│    fetch_ohlcv()        │
│                         │
│ • Symbol + period (6mo) │
│ • Returns: DataFrame    │
│   [Open,High,Low,Close, │
│    Volume] w/ DateIndex  │
│ • NaN rows dropped      │
│ • ~126 trading days     │
└────────────┬────────────┘
             │
     ┌───────┼───────┐
     ▼       ▼       ▼
  ┌─────┐ ┌─────┐ ┌─────┐
  │ RSI │ │MACD │ │ EMA │  ... (all indicators)
  └─────┘ └─────┘ └─────┘
```

Data integrity rules:
- Every indicator module calls `fetch_ohlcv()` with the same parameters, ensuring consistency across all scoring paths.
- Minimum 200 rows required (for EMA200 calculation); signals are rejected if insufficient history.
- OHLCV data is fetched fresh each scan cycle — no persistent caching of market data to avoid stale prices.
- The fetcher returns `None` on errors (network failure, delisted symbol) and the caller skips that symbol.

### 4.2 Earnings Calendar

The `data/earnings.py` module queries yfinance's earnings calendar for each symbol. This feeds two subsystems:

1. **AI Tier 1 filter**: If earnings are within 14 days, the signal is rejected pre-AI-call (saves API cost).
2. **PEAD detector**: Identifies stocks with earnings in the last 1–5 trading days for post-earnings drift signals.

### 4.3 Data Storage

| Data Type | Format | Location | Retention |
|-----------|--------|----------|-----------|
| OHLCV market data | In-memory DataFrame | RAM | Per-cycle (fetched fresh) |
| Open positions | JSON | `open_positions.json` | Until position closed |
| Trade journal | CSV (37 columns) | `trades.csv` | Permanent (append-only) |
| Rejected signals | JSONL | `rejected.jsonl` | Permanent (append-only) |
| AI cache | In-memory dict | RAM | 4-hour TTL per symbol+strategy |
| Configuration | Python module | `config/settings.py` | Permanent |

---

## 5. Trading Strategy Engine

### 5.1 Strategy Pipeline

The screener evaluates every symbol against five strategies in strict priority order. First match wins — a symbol cannot produce signals for multiple strategies in the same cycle.

| Priority | Strategy | Description | Stop Logic | Position Size |
|----------|----------|-------------|------------|---------------|
| 1 | VCP Breakout | Volatility contraction pattern breakout on volume surge | Below consolidation low | 100% |
| 2 | PEAD | Post-earnings announcement drift (1–5 days post-earnings) | 1% below earnings-day low | 100% |
| 3 | Momentum | Trending breakout via 5-indicator combined scoring | 1.5x ATR below entry | 100% |
| 4 | Swing | Pullback to EMA20 support in uptrend | 1.5x ATR below entry | 100% |
| 5 | Mean Reversion | Sharp drop recovery in stocks above EMA200 | Below 3-day low - 0.5x ATR | 50% |

### 5.2 Combined Scoring Engine

Five technical indicators are each scored 0.0–1.0 independently, then combined with strategy-specific weights:

**Momentum Weights:** EMA 25% | MACD 25% | Ripster 20% | Volume 20% | RSI 10%

**Swing Weights:** RSI 25% | EMA 25% | Ripster 20% | Volume 20% | MACD 10%

### 5.3 Hard Vetoes

Binary kill switches checked before any scoring. If any fires, the signal gets Grade F / score 0.0 regardless of other indicator strength:

1. **Bear Regime** — Price below EMA200
2. **OBV Divergence** — Price up but OBV down over 10 days
3. **Bearish Volume Surge** — Down day with volume > 1.5x 20-day average
4. **Ripster Cloud Cross Below** — Fast cloud crossed below slow cloud
5. **Low ATR%** — ATR(14)/Price < 1.5%

### 5.4 Grade Thresholds

| Grade | Score Range | Action | Size Modifier |
|-------|------------|--------|---------------|
| A | >= 0.78 | Trade | 100% |
| B | >= 0.65 | Trade | 75% |
| C | 0.38–0.64 | Skip | — |
| F | < 0.38 | Block | — |

### 5.5 Backtesting Framework

The backtesting module replays historical data through the same scoring engine used in live trading:

- **Data source**: Historical OHLCV from yfinance (up to 5 years).
- **Walk-forward**: Train on N months, test on next M months, slide forward.
- **Metrics**: Win rate, profit factor, Sharpe ratio, max drawdown, average R-multiple, capture ratio distribution.
- **Constraint**: Uses the same `combined_filter`, `risk_manager`, and grade thresholds as live — no separate backtest-only logic.

---

## 6. Risk Management

### 6.1 Position Sizing

```
max_risk_dollars = capital_pool × MAX_POSITION_SIZE_PCT(1.5%)
                   × AI_size_modifier(1.0)
                   × strategy_modifier(0.5 for MR, else 1.0)

risk_per_share = entry_price - stop_price

shares = max_risk_dollars / risk_per_share

cap = capital_pool × 10% / entry_price

final_shares = min(shares, cap)
```

If `final_shares` rounds to 0, the signal is skipped (no 1-share floor).

### 6.2 Pre-Check Gate

Before any AI API call (cost savings), the risk manager validates:

| Check | Condition | Cooldown |
|-------|-----------|----------|
| Duplicate position | Already holding same symbol | — |
| Re-entry cooldown | STOP_HIT/SETUP_BROKEN: 24h; TARGET_HIT: 90min | Exit-reason-aware |
| Daily loss limit | Cumulative day P&L >= -$180 (1.5% of $12k) | Until next trading day |
| Max positions | Total open >= 25 | Until a position closes |
| Invalid stop | stop >= entry | — |
| Invalid target | target <= entry | — |
| R:R ratio | reward/risk < 1.8 | — |
| Position too small | risk_per_share too high for capital | — |

### 6.3 Per-Strategy Position Caps

| Strategy | Max Positions | Notes |
|----------|---------------|-------|
| Momentum + VCP | 18 (shared bucket) | VCP counts against momentum cap |
| Swing | 15 | Historically best quality |
| PEAD | 5 | Conservative — newer strategy |
| Mean Reversion | 1 | Highest risk |

### 6.4 Stop Validation

Every stop price passes through `_validate_stop()`:

1. Stop must be below entry price — if violated, clamped to `entry × 0.93`.
2. If current price is known, stop must be below current price — if violated, clamped to `current × 0.985`.

This catches the inverted-stop bug where trailing stop updates push stops above market price.

### 6.5 Circuit Breakers

| Breaker | Trigger | Action |
|---------|---------|--------|
| Daily loss limit | Cumulative P&L <= -1.5% of total capital | Halt all new entries for the day |
| Order cutoff | Within 5 minutes of market close | Skip new entries (exits still run) |
| Capture ratio warning | Rolling 20-trade median < 0.15 | Log WARNING (manual review) |
| Ghost position limit | IBKR=0 shares, no fills for 48h | Force reconcile as PHANTOM |
| Zombie cleanup | Position held >= 2x max_hold_days | Force full exit |

---

## 7. Execution Layer

### 7.1 IBKR Connection

```python
# Connection parameters
HOST = "127.0.0.1"
PORT = 7496  # live (7497 for paper)
CLIENT_ID = 1
ACCOUNT_ID = "U15355701"  # explicit to prevent Error 435

# On connect:
# 1. ib.connect(host, port, clientId)
# 2. reqOpenOrders() — sync orders from prior sessions
# 3. Verify account ID matches
```

### 7.2 Bracket Order Structure

Every trade is placed as a 3-leg bracket order:

```
┌──────────────────────────────────────────────┐
│  Parent: LIMIT BUY                           │
│  Price: ask + 0.2% buffer                    │
│  TIF: GTC  |  outsideRth: False              │
├──────────────────────────────────────────────┤
│  Child 1: LIMIT SELL (Take Profit)           │
│  Price: target_price                         │
│  TIF: GTC  |  outsideRth: False              │
├──────────────────────────────────────────────┤
│  Child 2: STOP SELL (Stop Loss)              │
│  Price: stop_price                           │
│  TIF: GTC  |  outsideRth: False              │
└──────────────────────────────────────────────┘
```

Critical: Uses `ib.bracketOrder()` which pre-allocates sequential order IDs. Manual `parentId` assignment fails because IDs are zero at assignment time.

### 7.3 Fill Detection

Uses `reqExecutions()` with `ExecutionFilter` to query IBKR's server-side execution history (past 5 days). This is essential because `ib.fills()` only returns fills from the current TWS session — bracket stops that fired during a TWS restart are invisible otherwise.

---

## 8. AI Veto Layer

### 8.1 Two-Tier Evaluation

```
Signal passes technical scoring (Grade A or B)
        │
        ▼
┌──────────────────────────────┐
│ TIER 1: Earnings Calendar    │  ← FREE (no API call)
│ Reject if earnings < 14 days │
│ (skip for PEAD signals)      │
└──────────────┬───────────────┘
               │ PASS
               ▼
┌──────────────────────────────┐
│ TIER 2: OpenRouter LLM      │  ← default: openai/gpt-oss-20b:free
│ + web_search tool            │
│                              │
│ Strategy-aware prompts:      │
│ • VCP/Mom/Swing: news veto   │
│ • PEAD: post-earnings check  │
│ • MeanRev: fundamental vs    │
│   panic analysis             │
└──────────────┬───────────────┘
               │
        ┌──────┴──────┐
        ▼             ▼
    APPROVE        REJECT
   (proceed)    (log reason)
```

### 8.2 Safety Defaults

- On any error (empty response, invalid JSON, timeout, API error): **default to REJECT**.
- Cache results for 4 hours per symbol+strategy combination.
- Cache key includes strategy name (PEAD approval ≠ momentum approval).
- Track API cost per call: `$3/M input + $15/M output + $0.01/search`.

---

## 9. Paper Trading vs Live Trading

### 9.1 Mode Selection

The trading mode is determined by a single configuration parameter:

```python
IBKR_PORT = 7496  # Live trading
IBKR_PORT = 7497  # Paper trading

IS_PAPER_TRADING = (IBKR_PORT == 7497)  # Derived flag
```

### 9.2 Behavioral Differences

| Aspect | Paper Mode | Live Mode |
|--------|-----------|-----------|
| IBKR Port | 7497 | 7496 |
| Capital pools | Same configuration | Same configuration |
| Order execution | Simulated fills | Real market execution |
| Commission tracking | Estimated | Actual from IBKR |
| AI veto layer | Active (for testing) | Active |
| Risk limits | Same thresholds | Same thresholds |
| Journal logging | Full (separate file recommended) | Full |
| Telegram alerts | Optional | Recommended |

### 9.3 Transition Checklist

Before switching from paper to live:

1. Run paper trading for minimum 30 trading days.
2. Verify capture ratio median > 0.15 over last 20 trades.
3. Confirm all exit paths work (stop-hit, target-hit, time-exit, setup-broken).
4. Validate ghost position reconciliation handles TWS restarts.
5. Review rejected signals log — ensure AI veto is not too aggressive or too permissive.
6. Confirm daily loss limit triggers correctly.
7. Change `IBKR_PORT` to 7496 and verify account ID.

---

## 10. Monitoring and Alerting

### 10.1 Logging Architecture

```python
# Structured JSON logging via structlog
{
    "timestamp": "2026-07-02T10:30:00-04:00",
    "level": "info",
    "module": "engine",
    "event": "signal_detected",
    "symbol": "AAPL",
    "strategy": "momentum",
    "score": 0.82,
    "grade": "A",
    "rsi": 62.3,
    "macd_crossover": true,
    "volume_ratio": 2.1
}
```

### 10.2 Key Metrics

| Metric | Source | Alert Threshold |
|--------|--------|----------------|
| Capture ratio (20-trade rolling) | trades.csv | < 0.15 → WARNING |
| Daily P&L | trades.csv | <= -1.5% → HALT entries |
| Win rate (rolling 50) | trades.csv | < 35% → WARNING |
| AI reject rate | rejected.jsonl | > 80% → Review prompts |
| AI cost per day | trades.csv | > $5 → WARNING |
| Ghost positions | open_positions.json | Any > 48h → Force reconcile |
| IBKR connection | ib_insync | Disconnect → Telegram alert |
| Scan cycle duration | engine.py | > 10 min → WARNING |

### 10.3 Telegram Integration

The Telegram bot provides real-time reporting:

- **Trade alerts**: Entry/exit notifications with symbol, strategy, price, P&L.
- **Daily summary**: End-of-day P&L, open positions count, trades taken, AI cost.
- **Error alerts**: Connection failures, ghost positions, daily loss limit hit.
- **Commands**: `/status`, `/positions`, `/pnl`, `/force_scan`.

---

## 11. Database Schema

### 11.1 trades.csv Schema (37 columns)

```
ENTRY COLUMNS:
  trade_id            string    Unique trade identifier (UUID)
  symbol              string    Ticker symbol (e.g., "AAPL", "SHOP.TO")
  strategy            string    Strategy name (momentum/swing/vcp_breakout/pead/mean_reversion)
  direction           string    Always "long" in current system
  grade               string    A/B/C/F
  signal_strength     float     Combined weighted score (0.0-1.0)
  entry_price         float     Limit order fill price
  stop_loss           float     Initial stop loss price
  original_stop_loss  float     Immutable copy of initial stop
  target_price        float     Take-profit target price
  quantity            int       Number of shares
  risk_amount         float     Dollar risk (entry - stop) × quantity
  entry_date          datetime  Entry timestamp (ET)
  entry_commission    float     IBKR commission on entry

INDICATOR BREAKDOWN:
  rsi_value           float     RSI(14) at signal time
  rsi_score           float     RSI bullish score (0.0-1.0)
  macd_histogram      float     MACD histogram value
  macd_score          float     MACD bullish score (0.0-1.0)
  ema_score           float     EMA structure score (0.0-1.0)
  volume_ratio        float     Today's volume / 20-day average
  volume_score        float     Volume bullish score (0.0-1.0)
  ripster_score       float     Ripster cloud score (0.0-1.0)
  obv_confirming      bool      OBV trend confirmation

AI EVALUATION:
  ai_decision         string    APPROVE/REJECT
  ai_reasoning        string    Claude's analysis text
  ai_cost_usd         float     API call cost
  ai_cached           bool      Whether result was from cache

EXIT COLUMNS (filled on exit):
  exit_price          float     Exit fill price
  exit_date           datetime  Exit timestamp (ET)
  exit_reason         string    STOP_HIT/TARGET_HIT/TIME_EXIT_*/SETUP_BROKEN/PHANTOM
  exit_commission     float     IBKR commission on exit
  pnl_gross           float     (exit - entry) × quantity
  pnl_net             float     pnl_gross - commissions - ai_cost
  days_held           int       Trading days from entry to exit
  intended_r          float     target_distance / stop_distance
  realized_r          float     actual_gain / stop_distance
  capture_ratio       float     realized_r / intended_r
```

### 11.2 open_positions.json Schema

```json
{
  "AAPL": {
    "symbol": "AAPL",
    "strategy": "momentum",
    "grade": "A",
    "score": 0.82,
    "entry_price": 195.50,
    "stop_loss": 190.20,
    "original_stop_loss": 190.20,
    "target_price": 208.45,
    "quantity": 15,
    "risk_amount": 79.50,
    "entry_date": "2026-07-01T10:30:00-04:00",
    "entry_time": "2026-07-01T10:30:00-04:00",
    "ai_reasoning": "No negative news found for AAPL...",
    "ai_cost_usd": 0.023,
    "partial_fill": false,
    "parent_order_id": 1234,
    "currency": "USD"
  }
}
```

### 11.3 rejected.jsonl Schema

```json
{
  "timestamp": "2026-07-02T11:00:00-04:00",
  "symbol": "TSLA",
  "strategy": "momentum",
  "score": 0.75,
  "grade": "B",
  "rejection_reason": "AI_REJECT",
  "rejection_detail": "Recent lawsuit filing poses downside risk",
  "entry_price": 265.00,
  "stop_loss": 258.10,
  "target_price": 277.42
}
```

---

## 12. API Design

### 12.1 Internal Module API

The system uses a clean internal API between modules. No external REST API is exposed — all interaction is via the Telegram bot or direct configuration changes.

```python
# Data Layer
data.fetcher.fetch_ohlcv(symbol: str, period: str = "6mo") -> Optional[pd.DataFrame]
data.earnings.get_earnings_date(symbol: str) -> Optional[datetime]

# Signal Layer
signals.screener.run_full_scan(symbols: List[str], min_grade: str = "B") -> List[Signal]
signals.combined_filter.score_symbol(symbol: str, strategy: str, df: pd.DataFrame) -> Signal
signals.rsi_signals.calculate_rsi(df: pd.DataFrame, period: int = 14) -> RSIState
signals.rsi_signals.bullish_score(state: RSIState, strategy: str) -> float

# Risk Layer
risk.manager.pre_check(signal: Signal) -> Tuple[bool, str]
risk.manager.build_order(signal: Signal) -> Optional[TradeOrder]
risk.manager.validate_stop(stop: float, entry: float, current: Optional[float]) -> float

# Execution Layer
execution.broker.IBKRBroker.connect() -> bool
execution.broker.IBKRBroker.place_bracket_order(order: TradeOrder) -> Optional[Trade]
execution.broker.IBKRBroker.get_positions() -> Dict[str, Position]
execution.exit_manager.check_broker_exits() -> List[ExitEvent]
execution.exit_manager.check_time_based_exits() -> List[ExitEvent]

# AI Layer
ai.analyst.evaluate(signal: Signal) -> AIDecision
ai.cache.get(symbol: str, strategy: str) -> Optional[AIDecision]

# Journal Layer
journal.trade_logger.log_entry(trade: TradeOrder, fill: Fill) -> None
journal.trade_logger.log_exit(symbol: str, exit_event: ExitEvent) -> None
```

### 12.2 Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/status` | Current bot state, connection status, open positions count |
| `/positions` | List all open positions with P&L |
| `/pnl` | Today's P&L, weekly P&L, total P&L |
| `/trades [N]` | Last N trades with outcomes |
| `/force_scan` | Trigger an immediate scan cycle |
| `/pause` | Pause new entries (exits still run) |
| `/resume` | Resume normal operation |

---

## 13. Deployment Architecture

### 13.1 Single-Server Deployment

```
┌─────────────────────────────────────────────┐
│              Trading Server                  │
│         (Linux VPS or local machine)         │
│                                              │
│  ┌────────────────┐  ┌──────────────────┐   │
│  │  IB Gateway    │  │  Trading Bot     │   │
│  │  (headless)    │◄─┤  (Python)        │   │
│  │  Port 7496     │  │                  │   │
│  │                │  │  • engine.py     │   │
│  │  OR            │  │  • systemd svc   │   │
│  │  TWS           │  │  • auto-restart  │   │
│  │  Port 7497     │  │                  │   │
│  └────────────────┘  └──────────────────┘   │
│                                              │
│  ┌────────────────┐  ┌──────────────────┐   │
│  │  File Store    │  │  Log Aggregation │   │
│  │                │  │                  │   │
│  │  • trades.csv  │  │  • structlog     │   │
│  │  • positions   │  │  • journald      │   │
│  │  • rejected    │  │  • log rotation  │   │
│  └────────────────┘  └──────────────────┘   │
└─────────────────────────────────────────────┘
         │                      │
         ▼                      ▼
    Telegram API          Claude API
    (alerts)              (AI veto)
```

### 13.2 Process Management

```ini
# /etc/systemd/system/trading-bot.service
[Unit]
Description=US Trading Bot
After=network.target

[Service]
Type=simple
User=trader
WorkingDirectory=/opt/trading-bot
ExecStart=/opt/trading-bot/.venv/bin/python -m engine
Restart=on-failure
RestartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### 13.3 Backup Strategy

| Asset | Frequency | Method |
|-------|-----------|--------|
| `trades.csv` | After each trade | Append-only; daily rsync to backup |
| `open_positions.json` | After each change | Written atomically (write-tmp-rename) |
| `config/settings.py` | On change | Git-tracked |
| `rejected.jsonl` | Daily | Rotated weekly |
| Bot source code | On change | Git repository |

---

## 14. Security Considerations

### 14.1 Secrets Management

| Secret | Storage | Access |
|--------|---------|--------|
| IBKR account credentials | TWS/Gateway login (not in code) | Manual login or IB Gateway auto-login |
| OpenRouter API key | `.env` or `keys/` file (never committed) | `OPENROUTER_API_KEY` env var |
| Gemini API key (LLM fallback) | `.env` or `keys/gemini_api_key` (never committed) | `GEMINI_API_KEY` env var |
| Groq API key (LLM fallback) | `.env` or `keys/groq_api_key` (never committed) | `GROQ_API_KEY` env var |
| Telegram bot token | `.env` file (never committed) | `TELEGRAM_BOT_TOKEN` env var |
| Telegram chat ID | `.env` file | `TELEGRAM_CHAT_ID` env var |
| IBKR account ID | `config/settings.py` | Explicit in code (non-secret, needed for Error 435 prevention) |

### 14.2 Network Security

- IBKR connection is localhost-only (127.0.0.1). No remote API exposure.
- Claude API calls use HTTPS with API key authentication.
- Telegram bot uses HTTPS webhook/polling.
- No inbound ports need to be open on the trading server.
- Firewall rules should restrict outbound traffic to IBKR servers, Anthropic API, Telegram API, and Yahoo Finance.

### 14.3 File Security

- `.env` file permissions: `chmod 600`.
- `open_positions.json` written atomically (temp file + rename) to prevent corruption.
- `trades.csv` append-only — no code path deletes or overwrites historical trades.
- All state files owned by dedicated `trader` user with restricted home directory.

### 14.4 Operational Security

- Pattern Day Trader (PDT) rule awareness: the bot's 60-minute cycle and GTC bracket orders naturally avoid rapid day-trading patterns, but operators should monitor round-trip counts.
- The bot does not modify system or IBKR account settings.
- All trades are logged with full audit trail (entry conditions, AI reasoning, exit reason).
- No external API endpoints are exposed — the bot is a pull-only system.

### 14.5 Regulatory Compliance

- Retail automated traders using licensed brokers (Interactive Brokers) are subject to standard market conduct rules including prohibitions on manipulation and wash trading.
- The bot's re-entry cooldown system (90min–24h) prevents wash-sale-like patterns.
- All trades are journaled with timestamps, prices, and reasoning for audit purposes.
- SEC Rule 15c3-5 (Market Access Rule) compliance is handled by IBKR's risk controls as the executing broker.

---

## 15. Project Structure

> **Kept in sync with the tree.** This section was reconciled against the
> actual repository (audit item D4). Files the original design speculated about
> but that were never built (`signals/position_monitor.py`,
> `signals/opportunity_comparator.py`, `execution/ibkr_broker.py`,
> `agent/expert.py`, `agent/scheduler.py`, `agent/telegram_bot.py`, the
> `tools/*_scanner.py` utilities, `tests/test_fetcher.py`) have been removed;
> where the functionality exists elsewhere it is noted inline. New subsystems
> shipped since the first draft — the FastAPI dashboard, analytics, the paper
> broker, the alerts package — are now listed.

```
us_trading_bot/
├── pyproject.toml              # Project metadata, dependencies
├── .env.example                # Template for environment variables
├── .coveragerc                 # Coverage config (see scripts/coverage.sh)
├── .gitignore                  # Excludes .env, __pycache__, data files
├── README.md                   # Quick-start guide
├── SYSTEM_DESIGN.md            # This document
├── CONTRIBUTING.md             # Contributor guide
├── CHANGELOG.md                # Release notes
├── logging_config.py           # structlog setup (JSON prod / colour dev)
├── engine.py                   # Main trading loop orchestrator
│
├── config/
│   ├── settings.py             # All tunable parameters (single source of truth)
│   ├── universe.py             # Base watchlist symbols (US + Canadian)
│   ├── etf_universe.py         # ETF universe
│   ├── index_membership.py     # S&P 500 / NASDAQ-100 tiering
│   ├── trade_selection.py      # Operator strategy/symbol/grade whitelist
│   └── watchlist.py            # User-managed named watchlists (JSON store)
│
├── data/
│   ├── fetcher.py              # OHLCV retrieval (multi-provider, cached)
│   ├── providers.py            # Provider abstraction + fallback chain
│   ├── earnings.py             # Earnings calendar queries
│   ├── earnings_calendar.py    # Upcoming-earnings lookups
│   ├── earnings_tracker.py     # "Reporting today" + results/surprise
│   ├── extended_hours.py       # Pre/post-market pricing
│   ├── news_sentiment.py       # Headline sentiment
│   └── ratings.py              # Analyst ratings
│
├── signals/                    # Indicator modules + strategy detectors
│   ├── signal_types.py         # Signal dataclass (universal output format)
│   ├── screener.py             # Orchestrator: run_full_scan()
│   ├── combined_filter.py      # Weighted scoring engine
│   ├── rsi_signals.py  macd_signals.py  ema_signals.py  volume_signals.py
│   ├── ripster_cloud.py  vcp_signal.py  pead_signal.py  mean_reversion_signal.py
│   ├── sector_rotation.py  premarket.py  gap_filter.py  earnings_filter.py
│   ├── ratings_filter.py  multi_timeframe.py  support_resistance.py
│   └── indicator_snapshot.py
│       # NB: position health re-scoring lives in execution/exit_manager.py;
│       #     signal ranking is inline in engine.py (no separate comparator).
│
├── ai/
│   ├── llm_router.py           # Provider chain: OpenRouter → Gemini → Groq
│   ├── analyst.py              # AI news veto — fail-closed (strategy-aware)
│   ├── cache.py                # TTL cache per symbol+strategy
│   ├── openrouter.py           # Thin OpenRouter chat client
│   └── reflection.py           # Post-trade lesson writer (memory layer F1)
│
├── analytics/
│   ├── performance.py          # Journal metrics (mtime-cached CSV read)
│   ├── risk_dashboard.py       # Exposure, correlation, drawdown, beta vs SPY
│   ├── tax.py                  # Realized-gains / FIFO / wash-sale (P1f)
│   ├── montecarlo.py  regime.py  breadth.py
│   ├── setup_similarity.py     # Similar-setup guard (memory layer F2)
│   └── learnings_guard.py      # Applies learned lessons at entry (F1)
│
├── risk/
│   └── manager.py              # Position sizing, pre-checks, stop validation
│
├── execution/
│   ├── broker.py               # PaperBroker + IBKRBroker + make_broker()
│   ├── exit_manager.py         # All exit paths (broker/time/health/trailing)
│   ├── advanced_orders.py  levels.py  manual_trade.py  stop_trade.py  stops.py
│
├── journal/
│   ├── trade_logger.py         # CSV trade journal (38-column schema)
│   ├── notes.py                # Per-trade notes, tags, post-mortem (P6f)
│   ├── learnings.py            # Append-only lesson store (JSONL)
│   ├── rationale.py  activity_log.py  btst_logger.py
│
├── agent/
│   ├── notifier.py             # One-way alert dispatch (Telegram/email/push)
│   ├── alerts.py               # AlertManager (channels)
│   └── alert_config.py         # Alert rules + history
│       # NB: no telegram_bot / expert / scheduler — scheduling is in engine.py.
│
├── alerts/
│   └── price_alerts.py         # User-defined price-cross alerts (P2f)
│
├── dashboard/                  # FastAPI web dashboard + JSON API
│   ├── app.py                  # App assembly, root routes, router includes
│   ├── auth.py                 # HTTP Basic auth (fail-closed)
│   ├── middleware.py           # Security headers, request logging, error shape
│   ├── http_util.py            # Body-size limit, JSON parsing, error envelope
│   ├── rate_limit.py           # Per-IP rate limiting + login lockout
│   ├── schemas.py              # Pydantic request models
│   ├── ws_pnl.py               # Real-time P&L WebSocket
│   ├── quotes.py               # Shared TTL quote service
│   ├── ai_commentary.py analyst_cards.py            # AI analyst panel
│   ├── *_router.py             # Feature routers (watchlist, universe, notes,
│   │                           #   tax, price_alerts, earnings, history, …)
│   ├── templates/  static/     # Dashboard HTML + self-hosted Chart.js
│   └── push.py  pdf_report.py  backtest_control.py  provider_control.py
│
├── backtest/                   # On-demand strategy backtester
│   └── engine.py  data.py  __main__.py
│
├── selective_strategies/  short_strategies/   # Optional strategy packs
│
├── deploy/                     # systemd units for prod
├── docs/                       # api.md, env-vars.md, troubleshooting.md, specs
├── scripts/                    # coverage.sh, backup.sh, healthcheck.sh
│
├── tests/                      # ~1650 tests (pytest); see scripts/coverage.sh
│
└── data_store/                 # Runtime data (gitignored)
    ├── trades.csv  open_positions.json  rejected_signals.jsonl
    ├── learnings.jsonl  price_alerts.json  trade_notes.json  watchlists.json
```

---

## 16. Memory & Learning Layer

> **Full reference:** [`docs/memory-learning-layer.md`](docs/memory-learning-layer.md).

The bot closes the loop on its own `trades.csv` ledger: it writes itself a
plain-English lesson after each close and consults that memory — plus its
historical win rate — before the next entry. Two features implement this, both
**fail-open** (a missing key, a corrupt ledger, or an unparseable lesson never
blocks a trade).

### 16.1 F1 — AI Trade Reflection (Learnings Engine)

| Component | Module | Role |
|-----------|--------|------|
| Reflection writer | `ai/reflection.py` (`ReflectionEngine`) | After a close, asks an OpenRouter model for one lesson. |
| OpenRouter client | `ai/openrouter.py` | Thin chat client. |
| Lesson store | `journal/learnings.py` → `data_store/learnings.jsonl` | Append-only, expiring lesson store. |
| Learnings guard | `analytics/learnings_guard.py` (`LearningsGuard`) | Applies lessons on entry. |

**Writing.** `engine._reflect_on_exits()` runs after exit management each cycle.
For each genuinely-closed trade (partial-takes skipped) it sends the entry
context (grade, RSI, volume, MACD, strategy, direction) and outcome to the model
and stores one structured lesson: `lesson_text`, `pattern_tags`, `conditions`
(numeric bounds), `action`, and `confidence`.

**Two guardrails.** A lesson only *binds* when it clears both a **support gate**
(≥ `LEARNINGS_MIN_TRADES_FOR_PATTERN` similar historical trades) and a
**confidence gate** (≥ `LEARNINGS_MIN_CONFIDENCE`). Otherwise it is stored as a
non-binding `observe` note.

**Applying (entry gate d1.5).** `LearningsGuard.evaluate(signal)` selects
lessons matching the signal (same strategy/direction, RSI/volume/grade inside
`conditions`) and applies the most restrictive action: `avoid` → **reject**,
`require_confirm` → **demote** (grade-A only), `prefer` → annotate only,
`observe` → ignored.

### 16.2 F2 — Similar-Setup Guard

**Module:** `analytics/setup_similarity.py` (`find_similar_setups`). Pure, local,
no API call. At **entry gate d1** (right after the AI veto, before the learnings
guard) it filters `trades.csv` to setups like the incoming signal (same
strategy/grade, RSI within `SIMILAR_SETUP_RSI_TOLERANCE`, volume within
`SIMILAR_SETUP_VOL_TOLERANCE`, inside `SIMILAR_SETUP_LOOKBACK_DAYS`) and computes
the win rate over the matches:

- fewer than `SIMILAR_SETUP_MIN_MATCHES` matches, or win rate ≥
  `SIMILAR_SETUP_MIN_WIN_RATE` → **proceed**;
- win rate < `SIMILAR_SETUP_MIN_WIN_RATE` → **demote** (grade-A only);
- win rate < `SIMILAR_SETUP_BLOCK_WIN_RATE` over a solid sample → **block**.

### 16.3 Dashboard — AI Memory tab

Read-only, backed by `dashboard/memory_router.py` (`/api/memory/*`): learnings
list, reflections timeline, guard-decisions log (from `rejected_signals.jsonl`),
and a stats summary. See the full reference for endpoints and config tables.

---

## 17. Tiered Symbol Universe

> **Full reference:** [`docs/tiered-universe.md`](docs/tiered-universe.md).

Scanning runs over a three-tier universe built on the S&P 500 and NASDAQ-100
rather than a fixed watchlist. The tier machinery (`config/universe.py`) reads
`data_store/universe.db` when present and falls back to the curated static lists
in `config/index_membership.py` so every tier is populated even with no DB.

| Tier | Membership | Cadence | Engine method |
|------|------------|---------|---------------|
| **Tier 1 — Active Trading** | watchlist ∪ ETFs ∪ auto-promoted | every cycle | entry phase in `engine.py` |
| **Tier 2 — Scan Pool** | top-N S&P 500 by volume × market cap (`TIER2_SCAN_POOL_SIZE`) | daily | `_run_tier2_scan` |
| **Tier 3 — Universe** | full S&P 500 ∪ NASDAQ-100, pre-screened for movers | weekly | `_run_tier3_scan` |

**Auto-promotion.** Any Tier 2/3 signal calls `promote_to_tier1()`
(`_promote_signals`), moving the symbol into the every-cycle Tier 1 for
`PROMOTION_TTL_HOURS` (default 72h). The engine calls `expire_promotions()` each
cycle to revert lapsed promotions; promotions persist to the universe DB so they
survive restarts and appear on the dashboard.

**Seeding.** `data_store/universe_seeder.py` populates the DB from SEC EDGAR
(US), curated Canadian stocks, ETFs, and S&P 500 / NASDAQ-100 index membership
(+ liquidity ranking). Run `python -m data_store.universe_seeder`
(`--skip-enrichment` for a faster yfinance-free run) or trigger a background
re-seed from the dashboard.

**Dashboard — Universe Browser.** Backed by `dashboard/universe_router.py`
(`/api/universe/*`): tier breakdown cards, index membership with S&P/NASDAQ
badges, the liquidity-ranked scan pool, and live auto-promotions with TTL and a
manual **Demote** control.
