# USTradingBot Production Readiness Audit

**Date:** July 4, 2026
**Scope:** Full codebase analysis, documentation review, security assessment, test coverage audit
**Risk Level:** This is a trading system where bugs directly translate to financial losses

---

## Executive Summary

The USTradingBot is a well-architected autonomous trading system with strong foundational design principles (exit-first processing, fail-closed AI layer, defense-in-depth risk management). The signal generation pipeline, risk management, and core data structures are solid and well-tested.

However, the system has **critical gaps that would cause real money losses in production**. The most dangerous findings are: the IBKR live broker has stub methods for exit detection and stop modification (meaning stop losses and take-profits would never fire on the live path), the dashboard exposes trading configuration with zero authentication, plaintext production credentials sit on disk, and the daily P&L safety limit never resets between days. These must be resolved before any live trading.

**Severity Summary:**

| Severity | Count | Examples |
|----------|-------|---------|
| Critical | 8 | IBKR exit polling is a stub, no dashboard auth, daily P&L never resets |
| High | 12 | No broker reconnection, trailing stops broken on IBKR, no retry logic |
| Medium | 14 | No data caching, unbounded exit history, entry appends not atomic |
| Low | 6 | AI cost not persisted, Telegram token in URL path |

---

## Part 1: Documentation vs. Implementation Gap Analysis

### 1.1 Documented but NOT Implemented

The project has two design documents: `SYSTEM_DESIGN.md` (865 lines, root) and `docs/US_Trading_Bot_Architecture_Guide.docx`. Both describe features that do not exist in code.

**Missing source files (listed in SYSTEM_DESIGN.md Section 15 but absent from the codebase):**

| Documented File | Purpose | Status |
|----------------|---------|--------|
| `signals/position_monitor.py` | Position health re-scoring | Functionality exists inside `exit_manager.py` instead |
| `signals/opportunity_comparator.py` | Signal comparison/ranking | Not implemented at all |
| `execution/ibkr_broker.py` | Separate IBKR broker module | Merged into `execution/broker.py` |
| `agent/expert.py` | Telegram reporting agent | Not implemented; `agent/notifier.py` provides basic alerts only |
| `agent/scheduler.py` | Scan loop scheduler | Not implemented; scheduling is in `engine.py` directly |
| `agent/telegram_bot.py` | Telegram bot commands | Not implemented at all |
| `tools/finviz_scanner.py` | Universe builder from Finviz | Not implemented |
| `tools/market_scanner.py` | Relative strength ranking | Not implemented |
| `tools/watchlist_screener.py` | Manual screening utility | Not implemented |
| `tests/test_fetcher.py` | Data fetcher tests | Not implemented |
| `README.md` | Quick-start guide | Does not exist |

**Missing features (documented in SYSTEM_DESIGN.md but not implemented):**

1. **Telegram bot commands** (Section 12.2): `/status`, `/positions`, `/pnl`, `/trades`, `/force_scan`, `/pause`, `/resume` are all documented but none exist. The current `notifier.py` only sends one-way alerts; it cannot receive or process commands.

2. **Monitoring & alerting metrics** (Section 10.2): The document specifies 8 metrics with alert thresholds (capture ratio < 0.15, win rate < 35%, AI reject rate > 80%, AI cost > $5/day, scan cycle duration > 10 min, IBKR disconnect alerts). None of these alerts are implemented. The metrics are partially calculable from the CSV, but no monitoring loop checks them.

3. **Daily summary Telegram notification** (Section 10.3): End-of-day P&L, positions count, trades taken, and AI cost summary are described but not implemented.

4. **Error alerts via Telegram** (Section 10.3): Connection failures, ghost positions, and daily loss limit hits should trigger alerts. None do.

5. **Backup strategy** (Section 13.3): Daily rsync of trades.csv, weekly rotation of rejected.jsonl are documented but not implemented. No cron jobs, no backup scripts.

6. **systemd service file** (Section 13.2): A complete systemd unit file is documented but not created anywhere in the project.

7. **Opportunity comparator** (referenced in Architecture Guide Section 1, step 7): The entry pipeline describes "opportunity comparison" as a gate before AI evaluation. No such comparison exists in `engine.py`'s `_process_signal()`.

**Documentation inaccuracies:**

1. **AI model mismatch**: SYSTEM_DESIGN.md and the Architecture Guide both reference "Claude Sonnet 4.5" as the AI model. The actual code (`ai/analyst.py` line ~250) uses `openai/gpt-oss-20b:free` via OpenRouter, not Claude at all.

2. **API key name mismatch**: SYSTEM_DESIGN.md Section 14.1 references `ANTHROPIC_API_KEY`. The code uses `OPENROUTER_API_KEY`. A vestigial `ANTHROPIC_API_KEY` field exists in settings.py but is marked "unused."

3. **Account ID**: The Architecture Guide hardcodes account `U15355701`. The actual code reads this from settings/environment.

4. **Grade B threshold**: The Architecture Guide notes a code inconsistency -- comments say 0.70 but code checks >= 0.65. The code uses 0.65.

5. **Watchlist size**: The Architecture Guide describes ~100 US + 11 CA stocks. The actual `universe.py` has 30 US + 11 CA = 41 total symbols.

### 1.2 Implemented but NOT Documented

| Feature | Location | Notes |
|---------|----------|-------|
| FastAPI web dashboard | `dashboard/app.py`, `dashboard/templates/` | Full dark-themed dashboard with system status, strategy configs, demo scores, risk rules. Not mentioned in SYSTEM_DESIGN.md at all |
| Static HTML preview | `dashboard/preview.html` | 813-line standalone preview |
| PaperBroker | `execution/broker.py` | Full paper trading simulation with persistent state, Yahoo Finance price polling for stop/target hits. Only briefly mentioned as a "mode toggle" in docs |
| Broker factory pattern | `execution/broker.py:make_broker()` | Returns PaperBroker or IBKRBroker based on settings |
| `logging_config.py` | Root level | structlog configuration with colored console dev mode and JSON production mode |
| `keys/` directory | Root level | Contains live production credentials in plaintext (Cloudflare, SendGrid, GitHub, Hetzner SSH keys, root password) |
| Rejected signal logger | `journal/btst_logger.py` | JSONL append logger for rejected signals |

---

## Part 2: Security Assessment

### 2.1 CRITICAL: Dashboard Has Zero Authentication

**File:** `dashboard/app.py`

The FastAPI dashboard has no authentication middleware, no login system, no session management, no JWT, no OAuth, and no basic auth. Any network visitor can see:

- IBKR connection details (host, port, client ID)
- Capital allocation ($9K USD, $3K CAD)
- All risk management thresholds and position limits
- Strategy configurations and scoring weights
- Data directory paths

The documented startup command uses `--host 0.0.0.0`, binding to all network interfaces. There is no CORS middleware, no rate limiting, and no HTTPS enforcement.

**Recommendation:** Add at minimum HTTP Basic Auth or API key authentication. Add CORS middleware. Bind to 127.0.0.1 by default. Put behind a reverse proxy (nginx/caddy) with TLS termination.

### 2.2 CRITICAL: Plaintext Production Credentials

**Directory:** `keys/`

Seven files containing live production secrets in plaintext:

- `Cloudfare_token.txt` -- Live Cloudflare API token
- `sendgrid.txt` -- Live SendGrid API key (SG.xxx format)
- `Git_token.txt` -- Live GitHub Personal Access Token (ghp_xxx format)
- `hetzner_vps_ip` -- Production server IP address
- `hetzner_ustradingbot` -- **Unencrypted SSH private key** (ed25519, no passphrase)
- `hetzner_ustradingbot.pub` -- SSH public key
- `hetzner_root_password` -- **Root password in plaintext**

The `keys/` directory is properly gitignored, but secrets are stored with no encryption at rest and no file permission restrictions documented. The SSH key lacks a passphrase (the file header contains `none...none` indicating no encryption).

**Recommendation:** Use a secrets manager (e.g., 1Password CLI, HashiCorp Vault, or at minimum `age`/`gpg` encryption). Add passphrase to SSH keys. Set `chmod 600` on all credential files. Remove the root password file entirely; use SSH key-only auth.

### 2.3 Positive Security Findings

- `.gitignore` is comprehensive and verified working (covers `.env`, `keys/`, `*.pem`, `*.key`, etc.)
- API keys are loaded from environment variables via pydantic-settings, never hardcoded
- AI layer fails closed on any error (defaults to REJECT, never APPROVE)
- Broker defaults to paper trading mode (port 7497)
- IBKR connection defaults to localhost (127.0.0.1)
- Telegram notifier degrades gracefully when unconfigured
- OpenRouter API calls use HTTPS

### 2.4 Medium Security Issues

- **No API key rotation mechanism**: `get_settings()` uses `@lru_cache(maxsize=1)`, caching secrets forever with no hot-reload capability
- **Vestigial `ANTHROPIC_API_KEY` field** in settings.py increases attack surface
- **IBKR connection is unencrypted TCP**: Acceptable on localhost, risky if `IBKR_HOST` is changed to a remote address
- **Telegram bot token appears in URL path**: Standard for Telegram Bot API but could leak in server-side access logs

---

## Part 3: Error Handling & Resilience

### 3.1 CRITICAL: IBKRBroker Has Stub Methods

**File:** `execution/broker.py`

The live IBKR broker path has three critical stub methods:

```
IBKRBroker.poll_exits() -> returns []  (line ~437)
IBKRBroker.modify_stop() -> returns False  (line ~442)
IBKRBroker.force_close() -> not implemented
```

**Impact:** When using the IBKR live broker:
- Stop-loss and take-profit fills are NEVER detected by the engine
- Positions that hit their stop or target remain as phantom open positions indefinitely
- Trailing stops are completely non-functional (modify_stop always returns False)
- The exit manager cannot force-close positions

This means the only thing protecting positions on the live path are the bracket orders placed with IBKR. The bot's internal tracking will drift from reality with every fill.

### 3.2 CRITICAL: No Broker Reconnection

**File:** `engine.py`

`self.broker.connect()` is called once in `__init__` (line ~91). The return value is never checked. If IBKR disconnects mid-day:

- The broad `except Exception` in the main loop (line ~145) catches the error
- The engine logs `engine.cycle_error` and sleeps until the next cycle
- There is no reconnection attempt, no heartbeat check, no escalating alert
- Every subsequent cycle fails silently until manual restart

### 3.3 CRITICAL: Daily P&L Limit Never Resets

**File:** `risk/manager.py`

`_daily_pnl` is an in-memory float (line ~99). Two problems:

1. `reset_daily_pnl()` exists but is **never called** by the engine. There is no day-boundary detection.
2. On restart mid-day, the accumulator resets to zero, potentially allowing more losses than the configured 1.5% daily limit.

After a losing day, the bot starts the next day with the previous day's negative P&L still accumulated, immediately hitting the daily loss limit and refusing all new entries.

### 3.4 HIGH: Bracket Order Partial Submission Risk

**File:** `execution/broker.py` (lines ~424-425)

The IBKR bracket order submits three separate orders in a loop. If the process crashes between placing the parent (entry) order and placing the stop-loss leg, the position will be open with no stop loss on the exchange. There is no reconciliation on restart to detect unprotected positions.

### 3.5 HIGH: No Retry Logic Anywhere

Every network call in the system makes exactly one attempt:

- `fetch_ohlcv()` / `fetch_current_price()` -- single Yahoo Finance call, no retry
- `_call_openrouter()` -- single API call, no retry (even on 429 rate limit or 503 transient errors)
- `place_bracket_order()` -- single attempt, no retry
- `TelegramNotifier.send()` -- single attempt, no retry

A transient network hiccup causes signals to be missed, prices to be unavailable, and AI evaluations to default to REJECT.

### 3.6 HIGH: Exit Manager Has No Exception Isolation

**File:** `execution/exit_manager.py` (lines ~90-97)

The four exit phases (`_check_broker_exits`, `_check_time_based_exits`, `_check_position_health`, `_check_trailing_stops`) run sequentially with no try/except around each phase. If `_check_broker_exits()` throws, the remaining three checks are all skipped for that cycle. A position that should be force-exited due to being held too long will remain open.

### 3.7 HIGH: Corrupted Position File = Silent Data Loss

**File:** `risk/manager.py` (lines ~540-543)

If `open_positions.json` is corrupted (disk-full, truncated write, manual edit error), `_load_positions()` returns an empty dict. All tracked positions are silently lost with no alert and no backup. The bot proceeds as if it has zero positions, potentially re-entering the same symbols.

### 3.8 MEDIUM: No Data Caching

**File:** `data/fetcher.py`

Every call to `fetch_ohlcv()` or `fetch_current_price()` makes a fresh HTTP request to Yahoo Finance. During a single cycle, the same symbol may be fetched 4+ times (screener scan, freshness check, exit manager health check, trailing stop check). With 41 symbols, this is 160+ unnecessary HTTP calls per cycle, increasing Yahoo Finance rate-limit risk.

### 3.9 MEDIUM: Engine Entry Pipeline Not Exception-Safe

**File:** `engine.py`, `_process_signal()` (lines ~257-382)

If any gate (AI call, broker order, risk check) raises an unexpected exception within `_process_signal`, it propagates up and kills the entire entry phase for all remaining signals in the batch. A single bad symbol crashes processing for all others.

---

## Part 4: Test Coverage Analysis

### 4.1 Test Suite Summary

The project has 8 test files with ~1,625 lines of test code. Tests are well-organized with shared fixtures and environment isolation.

**Test execution:** All existing tests pass (verified via pytest).

### 4.2 Modules With ZERO Test Coverage

| Module | Lines | Risk Level | Impact |
|--------|-------|------------|--------|
| `engine.py` | 546 | **Critical** | The main trading loop, 9-gate entry pipeline, market hours logic, freshness checks -- all untested |
| `signals/screener.py` | 196 | **Critical** | Strategy priority ordering, grade filtering, scan loop error handling |
| `data/fetcher.py` | 199 | **High** | Yahoo Finance wrapper with NaN handling, column validation, fallback logic |
| `data/earnings.py` | 306 | **High** | Earnings calendar, gap direction, volume ratio -- feeds AI Tier-1 and PEAD detector |
| `dashboard/app.py` | 318 | **Medium** | FastAPI routes, data builders |
| `agent/notifier.py` | 93 | **Low** | Telegram notifications |
| `logging_config.py` | 79 | **Low** | structlog setup |

### 4.3 Critical Untested Code Paths

**Exit Manager (3 of 4 exit paths untested):**
- `_check_time_based_exits()` -- 5 distinct code branches (zombie, loss, flat, moderate profit, strong runner), all untested
- `_check_position_health()` -- re-scoring and SETUP_BROKEN exit logic, untested
- `_check_trailing_stops()` -- ATR-based trailing stop ratchet, untested
- Only `_check_broker_exits()` has one integration test (target hit scenario)

**IBKRBroker (completely untested):** `connect()`, `get_positions()`, `place_bracket_order()`, `poll_exits()`, `modify_stop()` -- none have tests. The entire live trading path is unverified.

### 4.4 Tests That Pass Without Testing Anything

At least 6 tests contain conditional assertions that can pass trivially:

```python
# Pattern found in multiple tests:
if result is not None:
    assert result.grade in {...}  # Skipped if result is None
```

Affected tests:
- `test_ema_bear_regime_zeros_score` -- uses `if not state.is_above_ema200:` (random data may not trigger)
- `test_score_symbol_returns_signal` -- `if result is not None:` skips all assertions
- `test_score_symbol_bearish_data` -- same pattern
- `test_vcp_detects_constructed_breakout` -- `if sig is not None:` (comment acknowledges this)
- `test_canadian_stock_uses_swing_weights` -- same pattern
- `test_signal_price_levels_valid` -- same pattern

### 4.5 Missing Test Categories

- **No integration/E2E tests**: No test instantiates `TradingEngine` and exercises a full `run_cycle()`
- **No error-path tests**: Network failures, file corruption, partial data are never simulated
- **No edge-case tests**: NaN/Inf values in OHLCV data, empty DataFrames, concurrent file access, timezone/DST boundaries
- **No performance tests**: Large symbol universes, large CSV files, concurrent operations

### 4.6 Estimated Coverage

By module count: ~50% of source modules have some tests. By actual code paths: estimated 25-35%. The most complex and safety-critical components (engine main loop, exit manager time/health/trailing logic, IBKR broker) are entirely untested.

---

## Part 5: Production Readiness Checklist

### Authentication & Authorization

| Item | Status | Details |
|------|--------|---------|
| Dashboard authentication | **Missing** | Zero auth on FastAPI app |
| Dashboard CORS | **Missing** | No CORSMiddleware configured |
| Dashboard rate limiting | **Missing** | No throttling |
| IBKR auth | **OK** | TWS/Gateway handles login; bot connects via localhost |
| API key storage | **OK** | Loaded from .env via pydantic-settings |
| API key rotation | **Missing** | No hot-reload; requires restart |

### API Key Security

| Item | Status | Details |
|------|--------|---------|
| Keys in environment | **OK** | .env file, gitignored |
| Keys not hardcoded | **OK** | No credentials in source code |
| Keys encrypted at rest | **Missing** | `keys/` directory has plaintext tokens, SSH keys, root passwords |
| Key rotation support | **Missing** | Singleton settings cache prevents runtime rotation |
| Unused key cleanup | **Needed** | Vestigial `ANTHROPIC_API_KEY` field in settings |

### Error Handling & Recovery

| Item | Status | Details |
|------|--------|---------|
| Main loop crash protection | **Partial** | Broad except catches cycle errors, but no escalation or reconnection |
| Broker reconnection | **Missing** | No disconnect detection, no auto-reconnect |
| Order failure handling | **Partial** | Returns BracketResult(False) but no retry |
| Data fetch retries | **Missing** | Single attempt for all Yahoo/OpenRouter calls |
| Exit phase isolation | **Missing** | One failed check skips all subsequent checks |
| State corruption recovery | **Missing** | Corrupted JSON silently resets to empty |
| Partial order protection | **Missing** | No reconciliation for incomplete bracket submissions |

### Monitoring & Alerting

| Item | Status | Details |
|------|--------|---------|
| Structured logging | **OK** | structlog with JSON production mode |
| Health check endpoint | **OK** | `GET /health` on dashboard |
| Telegram entry/exit alerts | **OK** | Basic notifications implemented |
| Metric alerting | **Missing** | 8 metrics documented, none monitored |
| IBKR disconnect alerts | **Missing** | No detection or notification |
| Daily summary | **Missing** | Documented but not implemented |
| Error escalation | **Missing** | No consecutive-failure counter or escalation |
| Scan cycle timing | **Missing** | No duration tracking or alerts |

### Rate Limiting

| Item | Status | Details |
|------|--------|---------|
| Yahoo Finance throttling | **Missing** | Sequential fetches with no delay |
| OpenRouter rate limit handling | **Missing** | 429 responses treated as permanent failure |
| IBKR message rate | **Not applicable** | ib_insync handles internally |
| Dashboard request limiting | **Missing** | No middleware |

### Data Persistence

| Item | Status | Details |
|------|--------|---------|
| Trade journal | **OK** | 37-column CSV with atomic rewrites for exits |
| Open positions | **OK** | JSON with atomic writes |
| AI verdict cache | **OK** | JSON with TTL, atomic writes |
| Rejected signals | **OK** | Append-only JSONL |
| Daily P&L persistence | **Missing** | In-memory only; lost on restart |
| AI cost tracking persistence | **Missing** | In-memory only |
| Entry appends atomicity | **Missing** | CSV appends are not atomic |
| Backup strategy | **Missing** | No backups, no rotation |
| Exit history pruning | **Missing** | Grows unboundedly |

### Testing Coverage

| Item | Status | Details |
|------|--------|---------|
| Unit tests exist | **Partial** | Good coverage for signals, risk, journal; zero for engine, screener, data layer |
| Integration tests | **Minimal** | One exit manager integration test |
| End-to-end tests | **Missing** | No full-cycle tests |
| Error path tests | **Missing** | No failure simulation |
| Live broker tests | **Missing** | IBKRBroker completely untested |
| Conditional assertion problem | **Present** | 6+ tests can pass without asserting anything |

### Deployment Hardening

| Item | Status | Details |
|------|--------|---------|
| systemd service | **Missing** | Documented but not created |
| HTTPS/TLS | **Missing** | Dashboard runs plain HTTP |
| Firewall rules | **Missing** | No iptables/ufw configuration |
| Process monitoring | **Missing** | No watchdog, no supervisord |
| Log rotation | **Missing** | No logrotate configuration |
| File permissions | **Undocumented** | No chmod 600 enforcement for credentials |

### Risk Management

| Item | Status | Details |
|------|--------|---------|
| Per-trade risk limits | **OK** | 1.5% max position size enforced |
| Per-strategy position caps | **OK** | Momentum 18, Swing 15, PEAD 5, MeanRev 1 |
| Daily loss limit | **Broken** | Never auto-resets; not persisted across restarts |
| R:R minimum enforcement | **OK** | 1.8 minimum enforced in pre-check |
| Stop-loss on every entry | **OK** | Bracket orders ensure exchange-side stops |
| Trailing stop functionality | **Broken on IBKR** | `modify_stop()` returns False on live path |
| Position health monitoring | **OK on paper** | Re-scoring works but only with PaperBroker |
| Ghost position handling | **Partial** | Documented 48h deferral but only partial implementation |
| Re-entry cooldowns | **OK** | Exit-reason-aware (STOP_HIT=24h, TARGET_HIT=90min) |
| Order freshness check | **OK** | 1% max price drift, 15min max age |
| Cash verification | **OK** | Checked before order placement |

### Configuration Management

| Item | Status | Details |
|------|--------|---------|
| Centralized config | **OK** | Single `config/settings.py` via pydantic-settings |
| Environment variable loading | **OK** | .env file with python-dotenv |
| Safe defaults | **OK** | Paper mode, empty API keys, localhost broker |
| Secrets rotation | **Missing** | No runtime rotation capability |
| Config validation | **OK** | Pydantic validates types on load |

---

## Part 6: Priority Remediation Plan

### P0 -- Must Fix Before Any Live Trading

1. **Implement `IBKRBroker.poll_exits()`** -- Query `reqExecutions()` and match fills against open positions. Without this, the bot cannot detect when stops or targets are hit on the live broker. (`execution/broker.py`)

2. **Implement `IBKRBroker.modify_stop()`** -- Use `ib.cancelOrder()` + `ib.placeOrder()` to update stop-loss orders. Without this, trailing stops are non-functional. (`execution/broker.py`)

3. **Implement `IBKRBroker.force_close()`** -- Submit market sell orders for forced exits. (`execution/broker.py`)

4. **Fix daily P&L reset** -- Add day-boundary detection in `engine.py`'s main loop. Persist `_daily_pnl` to disk so it survives restarts. (`risk/manager.py`, `engine.py`)

5. **Check broker connect() return value** -- If `connect()` returns False, log an error and exit (or retry with backoff). (`engine.py` line ~91)

6. **Add broker reconnection logic** -- Detect disconnects (ib_insync has `disconnectedEvent`) and attempt reconnection with exponential backoff. (`engine.py`, `execution/broker.py`)

7. **Add exception isolation to exit manager** -- Wrap each exit phase in try/except so one failure doesn't skip the others. (`execution/exit_manager.py` lines ~90-97)

8. **Add exception isolation to entry pipeline** -- Wrap `_process_signal()` in try/except so one bad signal doesn't kill processing for all remaining signals. (`engine.py`)

### P1 -- Should Fix Before Production

9. **Add dashboard authentication** -- At minimum, HTTP Basic Auth or API key. Bind to 127.0.0.1. (`dashboard/app.py`)

10. **Encrypt credentials at rest** -- Add passphrase to SSH keys, use a secrets manager or encrypted vault for the `keys/` directory.

11. **Add retry logic** -- Implement retry with exponential backoff for Yahoo Finance, OpenRouter, and Telegram calls. (`data/fetcher.py`, `ai/analyst.py`, `agent/notifier.py`)

12. **Add data caching** -- Cache `fetch_ohlcv()` results per cycle (60-minute TTL). Cache `fetch_current_price()` for 1-5 minutes. (`data/fetcher.py`)

13. **Add state file backup** -- Before loading `open_positions.json`, copy to `open_positions.json.bak`. Alert on corrupted file load. (`risk/manager.py`)

14. **Make entry CSV appends atomic** -- Use the same tempfile+rename pattern used for exit rewrites. (`journal/trade_logger.py`)

15. **Add consecutive failure counter** -- If N cycles fail in a row, send a Telegram alert and optionally halt. (`engine.py`)

16. **Add IBKR tests** -- Write unit tests for IBKRBroker with mocked ib_insync. (`tests/test_execution.py`)

17. **Add engine tests** -- Test `is_market_open()`, `freshness_check()`, and `_process_signal()` gate logic. (`tests/test_engine.py`)

### P2 -- Should Fix Before Sustained Operation

18. **Add exit manager tests** -- Cover all 4 exit paths with realistic scenarios. (`tests/test_exit_manager.py`)

19. **Fix conditional-assertion tests** -- Replace `if result is not None:` patterns with deterministic fixtures. (`tests/test_signals.py`, `tests/test_strategies.py`)

20. **Add data layer tests** -- Test fetcher and earnings with mocked yfinance. (`tests/test_fetcher.py`, `tests/test_earnings.py`)

21. **Implement monitoring metrics** -- Track and alert on capture ratio, win rate, AI reject rate, scan duration. (`engine.py` or new `monitoring/` module)

22. **Add exit history pruning** -- Cap `_exit_history` to last N entries or last M days. (`risk/manager.py`)

23. **Persist AI cost tracking** -- Write cumulative cost to a file. Add daily cost cap alerting. (`ai/analyst.py`)

24. **Create systemd service file** -- As documented in SYSTEM_DESIGN.md Section 13.2. (`trading-bot.service`)

25. **Set up log rotation** -- Configure logrotate for structlog output and rejected.jsonl. 

26. **Add HTTPS** -- Put dashboard behind nginx/caddy with TLS. 

27. **Update documentation** -- Reconcile SYSTEM_DESIGN.md with actual codebase (AI model, file structure, feature status).

28. **Remove vestigial config** -- Delete `ANTHROPIC_API_KEY` from settings.py.

---

## Appendix A: File Inventory

### Source Files (17 modules, ~4,500 lines)

```
engine.py                          546 lines  (main loop)
logging_config.py                   79 lines  (structlog config)
config/settings.py                 199 lines  (pydantic-settings)
config/universe.py                  88 lines  (watchlist)
data/fetcher.py                    199 lines  (Yahoo Finance)
data/earnings.py                   306 lines  (earnings calendar)
signals/signal_types.py            244 lines  (dataclasses/enums)
signals/rsi_signals.py             332 lines  (RSI indicator)
signals/macd_signals.py            256 lines  (MACD indicator)
signals/ema_signals.py             316 lines  (EMA structure)
signals/volume_signals.py          230 lines  (volume/OBV/VWAP)
signals/ripster_cloud.py           266 lines  (Ripster clouds)
signals/combined_filter.py         356 lines  (weighted scoring)
signals/screener.py                196 lines  (scan orchestrator)
signals/vcp_signal.py              199 lines  (VCP detector)
signals/pead_signal.py             166 lines  (PEAD detector)
signals/mean_reversion_signal.py   174 lines  (mean reversion)
ai/analyst.py                      331 lines  (AI veto layer)
ai/cache.py                        143 lines  (verdict cache)
risk/manager.py                    683 lines  (risk management)
execution/broker.py                455 lines  (broker protocol)
execution/exit_manager.py          275 lines  (exit management)
journal/trade_logger.py            461 lines  (CSV journal)
journal/btst_logger.py             136 lines  (rejected logger)
agent/notifier.py                   93 lines  (Telegram alerts)
dashboard/app.py                   318 lines  (FastAPI dashboard)
```

### Test Files (8 files, ~1,625 lines)

```
tests/conftest.py                  217 lines
tests/test_config.py               137 lines  (24 tests)
tests/test_signals.py              334 lines  (~30 tests)
tests/test_strategies.py            93 lines  (~5 tests)
tests/test_ai.py                   105 lines  (~10 tests)
tests/test_execution.py            145 lines  (~12 tests)
tests/test_journal.py              164 lines  (~8 tests)
tests/test_risk_manager.py         432 lines  (~25 tests)
```

### Configuration Files

```
.env                    Environment variables (gitignored)
.env.example            Template with placeholders
pyproject.toml          PEP 621 project metadata
requirements.txt        12 runtime dependencies
.gitignore              Comprehensive secret exclusions
```

### Data Files

```
data_store/trades.csv          Header-only (37 columns, no trades)
dashboard/templates/dashboard.html  Jinja2 template (435 lines)
dashboard/preview.html              Static preview (813 lines)
docs/US_Trading_Bot_Architecture_Guide.docx  Architecture guide
SYSTEM_DESIGN.md                    System design document (865 lines)
```

---

## Appendix B: Dependency Analysis

### Runtime Dependencies (requirements.txt)

| Package | Version | Purpose | Risk |
|---------|---------|---------|------|
| yfinance | latest | OHLCV data | Rate limits, API changes |
| pandas | latest | Data manipulation | Stable |
| numpy | latest | Numerical ops | Stable |
| pydantic | latest | Data validation | Stable |
| pydantic-settings | latest | Config loading | Stable |
| python-dotenv | latest | .env file loading | Stable |
| structlog | latest | Structured logging | Stable |
| httpx | latest | HTTP client (AI, Telegram) | Stable |
| fastapi | latest | Dashboard web framework | Stable |
| uvicorn | latest | ASGI server | Stable |
| jinja2 | latest | Template rendering | Stable |

### Optional Dependencies

| Package | Group | Purpose |
|---------|-------|---------|
| ib_insync | `live` | Interactive Brokers connection |
| pytest | `dev` | Test framework |
| pytest-asyncio | `dev` | Async test support |
| pytest-cov | `dev` | Coverage reporting |

### Missing from requirements.txt

- `ib_insync` is only in optional deps but is critical for live trading
- No version pinning on any dependency (uses `latest` for all)
- No `requirements-lock.txt` or equivalent for reproducible builds

**Recommendation:** Pin all dependency versions. Create a lockfile. Add `ib_insync` to a `production` extras group.
