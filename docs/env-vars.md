# Environment Variable Reference

> Every configuration value read by the bot, grouped as in `config/settings.py`. All are optional unless noted — pydantic supplies the default shown. Set them in `.env` (project root) or as real environment variables (env vars win). See `.env.example` for a template.

**Loading order:** environment variable → `.env` → loose `keys/` file (for `POLYGON_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`) → default. Unfilled `.env.example` placeholders (e.g. `your_key_here`) are treated as unset.

_212 variables across 50 groups._

## Interactive Brokers connection

| Variable | Default | Required | Description |
|---|---|---|---|
| `IBKR_HOST` | `"127.0.0.1"` | No | Ibkr host |
| `IBKR_PORT` | `7497` | No | Ibkr port |
| `IBKR_CLIENT_ID` | `1` | No | Ibkr client id |
| `IBKR_ACCOUNT_ID` | `""` | No | Ibkr account id |

## Capital allocation

| Variable | Default | Required | Description |
|---|---|---|---|
| `CAPITAL_BY_CURRENCY` | `{"USD": 9000.0, "CAD": 3000.0}` | No | Capital by currency |
| `TOTAL_CAPITAL` | `12_000.0` | No | Total capital |

## Position / risk limits

| Variable | Default | Required | Description |
|---|---|---|---|
| `MAX_POSITION_SIZE_PCT` | `0.015` | No | Max position size pct |
| `MAX_OPEN_POSITIONS` | `25` | No | Max open positions |
| `MAX_MOMENTUM_POSITIONS` | `18` | No | Max momentum positions |
| `MAX_SWING_POSITIONS` | `15` | No | Max swing positions |
| `MAX_PEAD_POSITIONS` | `5` | No | Max pead positions |
| `MAX_SELECTIVE_POSITIONS` | `3` | No | Max selective positions |
| `DAILY_LOSS_LIMIT_PCT` | `0.015` | No | Daily loss limit pct |
| `ATR_STOP_MULTIPLIER` | `1.5` | No | Atr stop multiplier |
| `RISK_REWARD_MIN` | `1.8` | No | Risk reward min |

## Scanning & signal freshness

| Variable | Default | Required | Description |
|---|---|---|---|
| `SCAN_INTERVAL_MINUTES` | `60` | No | Scan interval minutes |
| `SIGNAL_FRESHNESS_TOLERANCE_PCT` | `0.01` | No | Signal freshness tolerance pct |
| `SIGNAL_MAX_AGE_MINUTES` | `15` | No | Signal max age minutes |

## Broker reconnection (exponential backoff)

| Variable | Default | Required | Description |
|---|---|---|---|
| `RECONNECT_MAX_ATTEMPTS` | `5` | No | When the broker session drops, the engine retries connect() with exponential backoff: delay = BASE * 2**(attempt-1), ca… |
| `RECONNECT_BASE_DELAY_SECONDS` | `2.0` | No | Reconnect base delay seconds |
| `RECONNECT_MAX_DELAY_SECONDS` | `60.0` | No | Reconnect max delay seconds |

## Position management

| Variable | Default | Required | Description |
|---|---|---|---|
| `HOLD_MAX_DAYS` | `20` | No | Hold max days |
| `REENTRY_COOLDOWN_MINUTES` | `90` | No | Reentry cooldown minutes |
| `ORDER_CUTOFF_MINUTES_BEFORE_CLOSE` | `5` | No | Order cutoff minutes before close |
| `GHOST_POSITION_MAX_DEFER_HOURS` | `48` | No | Ghost position max defer hours |

## Dynamic stop management

| Variable | Default | Required | Description |
|---|---|---|---|
| `ENABLE_DYNAMIC_STOPS` | `True` | No | name kept for backward compatibility; new deployments should use ``ENABLE_DYNAMIC_STOPS`` instead. If either is explici… |
| `ENABLE_PARTIAL_TAKE_TRAIL` | `True` | No | legacy alias — prefer ENABLE_DYNAMIC_STOPS |

## Market hours (Eastern Time)

| Variable | Default | Required | Description |
|---|---|---|---|
| `MARKET_OPEN_HOUR` | `9` | No | Market open hour |
| `MARKET_OPEN_MINUTE` | `30` | No | Market open minute |
| `MARKET_CLOSE_HOUR` | `16` | No | Market close hour |
| `MARKET_CLOSE_MINUTE` | `0` | No | Market close minute |

## AI veto layer (OpenRouter)

| Variable | Default | Required | Description |
|---|---|---|---|
| `OPENROUTER_API_KEY` | `""` | No | The AI news-veto layer uses OpenRouter (https://openrouter.ai). Only the API key is read from the environment (OPENROUT… |
| `OPENROUTER_BASE_URL` | `"https://openrouter.ai/api/v1"` | No | Openrouter base url |
| `OPENROUTER_MODEL` | `"openai/gpt-oss-20b:free"` | No | Openrouter model |
| `OPENROUTER_TIMEOUT_SECONDS` | `45.0` | No | Openrouter timeout seconds |
| `OPENROUTER_INPUT_COST_PER_1M` | `0.0` | No | Pricing per 1M tokens (free models are 0.0). Used for cost tracking only. |
| `OPENROUTER_OUTPUT_COST_PER_1M` | `0.0` | No | Openrouter output cost per 1m |
| `AI_VETO_ENABLED` | `True` | No | Enable/disable the paid Tier-2 LLM call. When False, only the free Tier-1 earnings filter runs and everything else is a… |
| `AI_CACHE_TTL_HOURS` | `4.0` | No | How long (hours) an AI verdict is cached per symbol+strategy. |
| `AI_EARNINGS_BLACKOUT_DAYS` | `14` | No | Reject signals whose earnings fall within this many days (Tier-1 filter). |
| `AI_FAIL_OPEN_ON_PROVIDER_ERROR` | `True` | No | When every provider is unavailable (all rate-limited / 5xx / timed out), skip the veto instead of rejecting. A 429 is n… |

## LLM fallback providers (ai.llm_router)

Every LLM consumer — the AI veto, trade reflection, and the dashboard's live
commentary — goes through `ai/llm_router.py`, which tries **OpenRouter →
Gemini → Groq** and stops at the first provider that answers. All three speak
the OpenAI `/chat/completions` dialect, so they differ only in base URL, key
and model name; a provider with no key is skipped rather than tried and failed.

Keys load in the usual order (env → `.env` → loose `keys/gemini_api_key` /
`keys/groq_api_key` file → default).

| Variable | Default | Required | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | `""` | No | Google AI Studio key for the first fallback. Empty = provider skipped. |
| `GEMINI_BASE_URL` | `"https://generativelanguage.googleapis.com/v1beta/openai"` | No | Gemini's OpenAI-compatibility endpoint. |
| `GEMINI_MODEL` | `"gemini-flash-latest"` | No | Use the `-latest` alias: `gemini-2.0-flash` has zero free-tier quota and 429s on the first call. |
| `GROQ_API_KEY` | `""` | No | Groq key for the second fallback. Empty = provider skipped. |
| `GROQ_BASE_URL` | `"https://api.groq.com/openai/v1"` | No | Groq base url |
| `GROQ_MODEL` | `"llama-3.3-70b-versatile"` | No | Groq model |
| `LLM_FALLBACK_ENABLED` | `True` | No | When False only OpenRouter is tried — the quickest way to isolate a misbehaving fallback. |
| `LLM_BREAKER_THRESHOLD` | `3` | No | Consecutive failures before a provider is taken out of rotation. |
| `LLM_BREAKER_COOLDOWN_SECONDS` | `300.0` | No | How long a tripped provider stays skipped. |
| `LLM_DEFAULT_RETRY_AFTER_SECONDS` | `60.0` | No | Backoff used when a 429 carries no `Retry-After` header. |
| `LLM_MAX_RETRY_AFTER_SECONDS` | `3600.0` | No | Upper clamp on a provider-supplied `Retry-After`, so nothing can park a feature indefinitely. |

## Memory & learning layer (F1 + F2)

| Variable | Default | Required | Description |
|---|---|---|---|
| `LEARNINGS_ENABLED` | `True` | No | OpenRouter call writes a plain-English lesson to ``learnings.jsonl``; before new entries the learnings guard consults m… |
| `LEARNINGS_MODEL` | `"openai/gpt-oss-20b:free"` | No | Learnings model |
| `LEARNINGS_MAX_AGE_DAYS` | `90` | No | lessons expire after 90 days |
| `LEARNINGS_MAX_RELEVANT` | `5` | No | max lessons weighed per entry |
| `LEARNINGS_MIN_TRADES_FOR_PATTERN` | `3` | No | need 3 similar trades to bind |
| `LEARNINGS_MIN_CONFIDENCE` | `0.6` | No | below this a lesson is advisory |
| `LEARNINGS_MAX_TOKENS` | `400` | No | reflection completion cap |
| `SIMILAR_SETUP_ENABLED` | `True` | No | F2 — Similar-Setup Guard: before an entry, query the ledger for setups like this one (same strategy/grade, RSI & volume… |
| `SIMILAR_SETUP_LOOKBACK_DAYS` | `90` | No | Similar setup lookback days |
| `SIMILAR_SETUP_MIN_MATCHES` | `5` | No | need 5 matches before acting |
| `SIMILAR_SETUP_RSI_TOLERANCE` | `10.0` | No | ±10 RSI points |
| `SIMILAR_SETUP_VOL_TOLERANCE` | `0.5` | No | ±0.5 volume ratio |
| `SIMILAR_SETUP_MIN_WIN_RATE` | `0.35` | No | below this → demote to A-only |
| `SIMILAR_SETUP_BLOCK_WIN_RATE` | `0.15` | No | below this → hard skip |

## AI commentary dashboard (TA2)

| Variable | Default | Required | Description |
|---|---|---|---|
| `AI_COMMENTARY_ENABLED` | `True` | No | from the same computed facts, because commentary influences no order. Uses OpenRouter free models; at most 3 LLM calls … |
| `AI_COMMENTARY_MODEL` | `"openai/gpt-oss-20b:free"` | No | Ai commentary model |
| `AI_COMMENTARY_INTERVAL_MINUTES` | `5` | No | Ai commentary interval minutes |
| `AI_COMMENTARY_MAX_CALLS_PER_DAY` | `150` | No | Ai commentary max calls per day |
| `AI_COMMENTARY_WATCHLIST_LIMIT` | `10` | No | Only the top-N watchlist symbols (ranked signal > near_entry > rejected) get indicator recomputation + LLM prose; the r… |
| `AI_COMMENTARY_IDLE_SUPPRESS_MINUTES` | `15` | No | Skip scheduled refreshes when no client has polled within this window (no browser open -> no provider/LLM spend). |

## Legacy Anthropic key (unused; kept for backward compat)

| Variable | Default | Required | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | `""` | No | Anthropic api key |

## Telegram notifications

| Variable | Default | Required | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `""` | No | Telegram bot token |
| `TELEGRAM_CHAT_ID` | `""` | No | Telegram chat id |

## Dashboard (FastAPI)

| Variable | Default | Required | Description |
|---|---|---|---|
| `DASHBOARD_AUTH_ENABLED` | `True` | No | password is configured the app refuses to start (fail-closed) so the dashboard is never accidentally exposed without cr… |
| `DASHBOARD_USERNAME` | `"admin"` | No | Dashboard username |
| `DASHBOARD_PASSWORD` | `""` | Yes* (auth enabled) | Dashboard password |
| `DASHBOARD_ADMIN_PASSWORD` | `""` | No (→ DASHBOARD_PASSWORD) | from DASHBOARD_PASSWORD so read access and trade-execution authority can be handed out separately. When empty it falls … |
| `DASHBOARD_HOST` | `"127.0.0.1"` | No | Default bind host for the dashboard (documented for the run command). |
| `DASHBOARD_PORT` | `8501` | No | Dashboard port |

## Rate limiting & brute-force lockout (dashboard)

| Variable | Default | Required | Description |
|---|---|---|---|
| `RATE_LIMIT_ENABLED` | `True` | No | In-process protection for money-moving / control endpoints and the login (HTTP Basic) path. Disabled automatically unde… |
| `RATE_LIMIT_TRADE_PER_MIN` | `20` | No | Max requests per client IP per minute for the money path (manual trades, position stops) — kept deliberately low; a hum… |
| `RATE_LIMIT_CONTROL_PER_MIN` | `30` | No | Max requests per client IP per minute for control endpoints (engine start/stop, mode switch, provider switch, backtest … |
| `RATE_LIMIT_LOGIN_MAX_FAILURES` | `5` | No | Failed-login attempts (per client IP) allowed before a temporary lockout. Only *presented and wrong* credentials count — an anonymous request (e.g. the polls a page fires after its session expires) is answered with the sign-in page and never spends budget. |
| `RATE_LIMIT_LOGIN_LOCKOUT_SECONDS` | `60` | No | Length of the **first** lockout once the budget is spent. Consecutive lockouts double it (60s → 2m → 4m → 8m) up to the ceiling below; retrying during a lockout does not extend it. |
| `RATE_LIMIT_LOGIN_LOCKOUT_MINUTES` | `15` | No | Ceiling (minutes) for the escalating lockout above. A client that stays quiet for one window decays back to the short first lockout. |

## Broker selection

| Variable | Default | Required | Description |
|---|---|---|---|
| `BROKER` | `"paper"` | No | dashboard and start paper trading immediately with zero extra setup. Switch to real-money trading ONLY by explicitly se… |

## Paper-broker fill realism (slippage + commission)

| Variable | Default | Required | Description |
|---|---|---|---|
| `PAPER_SLIPPAGE_BPS` | `5.0` | No | the (worse) bar open instead of the stop price. * Commission — a per-share charge on both the entry and the exit; paper… |
| `PAPER_COMMISSION_PER_SHARE` | `0.005` | No | Paper commission per share |

## Market-data provider selection

| Variable | Default | Required | Description |
|---|---|---|---|
| `MARKET_DATA_PROVIDER` | `"yfinance"` | No | "polygon" uses the Polygon.io REST API (requires POLYGON_API_KEY). The provider can be switched at runtime from the das… |
| `ALPACA_API_KEY` | `""` | No | Alpaca api key |
| `ALPACA_API_SECRET` | `""` | No | Alpaca api secret |
| `ALPACA_DATA_FEED` | `"iex"` | No | "iex" (free) or "sip" (paid) |
| `POLYGON_API_KEY` | `""` | No | Polygon api key |

## Fallback market-data provider

| Variable | Default | Required | Description |
|---|---|---|---|
| `MARKET_DATA_FALLBACK_PROVIDER` | `"yfinance"` | No | (and therefore zero trades). Yahoo Finance has no such per-minute cap, so it makes a reliable free fallback. Set empty … |
| `PROVIDER_FALLBACK_TRIP_THRESHOLD` | `3` | No | After this many *consecutive* primary failures the circuit breaker trips and routes every fetch straight to the fallbac… |
| `PROVIDER_FALLBACK_COOLDOWN_SECONDS` | `300.0` | No | 5 minutes |

## Data cache TTLs (seconds)

| Variable | Default | Required | Description |
|---|---|---|---|
| `PRICE_CACHE_TTL_SECONDS` | `300.0` | No | 5 minutes |
| `OHLCV_CACHE_TTL_SECONDS` | `300.0` | No | 5 minutes (was 1 hour; reduced so stop-loss checks see fresh bar data) |
| `DATA_CACHE_ENABLED` | `True` | No | Data cache enabled |

## Dashboard quote service (monitoring F1)

| Variable | Default | Required | Description |
|---|---|---|---|
| `QUOTE_CACHE_TTL_SECONDS` | `15.0` | No | The dashboard's shared quote service serves current price / prev-close / day-change from its own short-TTL cache so liv… |

## Position proximity alerts (monitoring F3/F6)

| Variable | Default | Required | Description |
|---|---|---|---|
| `POSITION_PROXIMITY_ALERT_PCT` | `1.0` | No | Warn (UI highlight + optional alert) when the current price is within this percentage of a position's stop or target (1… |

## Fetch retry (exponential backoff)

| Variable | Default | Required | Description |
|---|---|---|---|
| `FETCH_MAX_RETRIES` | `3` | No | Transient Yahoo/Alpaca failures are retried with exponential backoff: delay = BASE * 2**(attempt-1), capped at MAX_DELA… |
| `FETCH_RETRY_BASE_DELAY_SECONDS` | `0.5` | No | Fetch retry base delay seconds |
| `FETCH_RETRY_MAX_DELAY_SECONDS` | `8.0` | No | Fetch retry max delay seconds |

## Realtime exit polling

| Variable | Default | Required | Description |
|---|---|---|---|
| `REALTIME_EXIT_POLL_SECONDS` | `30.0` | No | cadence. Between full scan cycles it wakes every REALTIME_EXIT_POLL_SECONDS to run exit management only. Ignored for th… |
| `ENABLE_REALTIME_EXITS` | `True` | No | Enable realtime exits |

## Dynamic stop-loss configuration

| Variable | Default | Required | Description |
|---|---|---|---|
| `ENABLE_TRAILING_STOP` | `True` | No | * Volatility-adjusted — the trail distance is a multiple of ATR rather than a fixed percentage, so it widens in volatil… |
| `TRAIL_ATR_MULTIPLIER` | `2.0` | No | Trail atr multiplier |
| `TRAIL_ACTIVATION_PROFIT_PCT` | `0.05` | No | Trail activation profit pct |
| `ENABLE_BREAKEVEN_STOP` | `True` | No | Enable breakeven stop |
| `BREAKEVEN_TRIGGER_R` | `1.0` | No | Breakeven trigger r |
| `BREAKEVEN_BUFFER_PCT` | `0.001` | No | Breakeven buffer pct |
| `ENABLE_TIME_STOP_TIGHTENING` | `True` | No | Enable time stop tightening |
| `TIME_STOP_TIGHTEN_DAYS` | `5` | No | Time stop tighten days |
| `TIME_STOP_TIGHTEN_ATR_MULTIPLIER` | `1.0` | No | Time stop tighten atr multiplier |
| `TIME_STOP_STAGNANT_PROFIT_PCT` | `0.02` | No | Time stop stagnant profit pct |
| `ENABLE_VOLATILITY_STOPS` | `True` | No | Enable volatility stops |
| `STOP_ATR_PERIOD` | `14` | No | Stop atr period |
| `STOP_OVERRIDES_BY_STRATEGY` | `{}` | No | {"mean_reversion": {"TRAIL_ATR_MULTIPLIER": 1.5, "ENABLE_TIME_STOP_TIGHTENING": True, "TIME_STOP_TIGHTEN_DAYS": 3}} |

## Advanced order execution

| Variable | Default | Required | Description |
|---|---|---|---|
| `LIMIT_ORDER_EXPIRY_HOURS` | `4.0` | No | run under the dynamic trailing stop. * Market-on-close — submit the entry as a MOC order so it fills at the closing auc… |
| `ENABLE_SCALE_IN` | `False` | No | Enable scale in |
| `SCALE_IN_TRANCHES` | `3` | No | Scale in tranches |
| `SCALE_IN_STEP_PCT` | `0.01` | No | Scale in step pct |
| `ENABLE_PARTIAL_TAKE` | `True` | No | Enable partial take |
| `PARTIAL_TAKE_PCT` | `0.5` | No | Partial take pct |
| `PARTIAL_TAKE_TARGET_R` | `1.0` | No | Partial take target r |
| `ENABLE_MOC_ENTRIES` | `False` | No | When enabled, signals that arrive inside the order-cutoff window (too close to the close for a clean intraday entry) ar… |

## Mode switching (paper ⇄ live)

| Variable | Default | Required | Description |
|---|---|---|---|
| `ALLOW_MODE_SWITCH` | `True` | No | The dashboard can flip BROKER/IBKR_PORT and ask the engine to restart. Switching *to live* requires the admin password … |

## Email alerts (SMTP)

| Variable | Default | Required | Description |
|---|---|---|---|
| `EMAIL_ALERTS_ENABLED` | `False` | No | Email alerts enabled |
| `SMTP_HOST` | `""` | No | Smtp host |
| `SMTP_PORT` | `587` | No | Smtp port |
| `SMTP_USERNAME` | `""` | No | Smtp username |
| `SMTP_PASSWORD` | `""` | No | Smtp password |
| `SMTP_USE_TLS` | `True` | No | Smtp use tls |
| `EMAIL_FROM` | `""` | No | Email from |
| `EMAIL_TO` | `""` | No | Email to |

## Alert routing & thresholds

| Variable | Default | Required | Description |
|---|---|---|---|
| `ALERT_ON_ENTRY` | `True` | No | Alert on entry |
| `ALERT_ON_EXIT` | `True` | No | Alert on exit |
| `ALERT_ON_MODE_SWITCH` | `True` | No | Alert on mode switch |
| `ALERT_DRAWDOWN_PCT` | `0.05` | No | Fire a drawdown alert when peak-to-trough equity drawdown exceeds this fraction; fire a daily-loss alert when the day's… |
| `ALERT_DAILY_LOSS_PCT` | `0.01` | No | Alert daily loss pct |

## Multi-timeframe analysis

| Variable | Default | Required | Description |
|---|---|---|---|
| `ENABLE_MULTI_TIMEFRAME` | `True` | No | Confirm each daily signal against the weekly trend. When enabled, a daily long signal is only taken if the weekly trend… |
| `WEEKLY_TREND_EMA_PERIOD` | `30` | No | Weekly trend ema period |
| `MTF_REQUIRE_WEEKLY_UPTREND` | `True` | No | Mtf require weekly uptrend |

## Logging

| Variable | Default | Required | Description |
|---|---|---|---|
| `LOG_LEVEL` | `"INFO"` | No | Log level |

## Data persistence

| Variable | Default | Required | Description |
|---|---|---|---|
| `DATA_DIR` | `Path("data_store")` | No | Data dir |

## Grade thresholds

| Variable | Default | Required | Description |
|---|---|---|---|
| `GRADE_A_THRESHOLD` | `0.78` | No | Grade a threshold |
| `GRADE_B_THRESHOLD` | `0.65` | No | Grade b threshold |
| `GRADE_C_THRESHOLD` | `0.38` | No | Grade c threshold |

## Hard veto thresholds

| Variable | Default | Required | Description |
|---|---|---|---|
| `MIN_ATR_PCT` | `0.015` | No | 1.5 % — below this ATR% the stock is untradeable |

## ETF support (Feature 3)

| Variable | Default | Required | Description |
|---|---|---|---|
| `ETF_RISK_MODIFIER` | `1.3` | No | risk-budget multiplier vs a stock |
| `ETF_NOTIONAL_CAP_PCT` | `0.15` | No | max notional per ETF (vs 0.10 stock) |
| `STOCK_NOTIONAL_CAP_PCT` | `0.10` | No | explicit single-name notional cap |
| `MIN_ATR_PCT_ETF` | `0.008` | No | 0.8 % ATR floor for ETFs |
| `SECTOR_ROTATION_ENABLED` | `False` | No | SPY and go long the top-N rotating leaders. Off by default (a new strategy competing for capital); also selectable from… |
| `SECTOR_ROTATION_TOP_N` | `3` | No | Sector rotation top n |
| `SECTOR_ROTATION_LOOKBACK_DAYS` | `63` | No | ~3 trading months |

## Minimum OHLCV rows for indicator calculation

| Variable | Default | Required | Description |
|---|---|---|---|
| `MIN_OHLCV_ROWS` | `200` | No | Min ohlcv rows |

## OHLCV look-back window fetched per symbol

| Variable | Default | Required | Description |
|---|---|---|---|
| `OHLCV_FETCH_PERIOD` | `"2y"` | No | 200-row minimum — which silently rejected *every* symbol at the row check and produced zero signals (and therefore zero… |

## Watchlist management

| Variable | Default | Required | Description |
|---|---|---|---|
| `USE_WATCHLIST_FILE` | `True` | No | "energy") instead of the hard-coded universe. If the file is missing the store seeds itself from the built-in US/CA uni… |

## Tiered scanning (Full Stock Universe)

| Variable | Default | Required | Description |
|---|---|---|---|
| `TIERED_SCANNING_ENABLED` | `True` | No | uses a three-tier scanning architecture to cover thousands of symbols efficiently. Tier 1 (active watchlist) runs every… |
| `TIER1_WORKERS` | `4` | No | Tier 1 (Active Trading): user watchlist ∪ ETFs ∪ auto-promoted symbols — full strategy evaluation every scan cycle. |
| `TIER2_ENABLED` | `True` | No | Tier 2 (Scan Pool): the top-N S&P 500 names by volume × market cap, scanned once per day. A signal here auto-promotes t… |
| `TIER2_WORKERS` | `8` | No | Tier2 workers |
| `TIER2_SCAN_POOL_SIZE` | `250` | No | top-N S&P 500 by liquidity |
| `TIER2_INTERVAL_MINUTES` | `30` | No | legacy sector-rotation throttle (unused by index tiers) |
| `TIER3_ENABLED` | `True` | No | Tier 3 (Universe): the full S&P 500 ∪ NASDAQ-100, swept once per week for discovery. A lightweight pre-screen finds unu… |
| `TIER3_WORKERS` | `16` | No | Tier3 workers |
| `TIER3_PRESCREEN_PRICE_CHANGE_PCT` | `0.03` | No | 3% daily move |
| `TIER3_PRESCREEN_VOLUME_RATIO` | `2.0` | No | 2x avg volume |
| `PROMOTION_TTL_HOURS` | `72.0` | No | Auto-promotion: how long (hours) a symbol stays in Tier 1 after a Tier 2/3 signal promotes it. It reverts to its scan-p… |
| `BATCH_DOWNLOAD_SIZE` | `50` | No | Batch data fetching for large symbol sets. |

## News sentiment filter (Finnhub)

| Variable | Default | Required | Description |
|---|---|---|---|
| `NEWS_SENTIMENT_ENABLED` | `False` | No | the built-in headline scorer rejects an entry when the average sentiment over the lookback window is below NEWS_SENTIME… |
| `FINNHUB_API_KEY` | `""` | No | Finnhub api key |
| `NEWS_LOOKBACK_DAYS` | `3` | No | News lookback days |
| `NEWS_SENTIMENT_MIN_SCORE` | `-0.15` | No | News sentiment min score |
| `NEWS_MIN_ARTICLES` | `2` | No | News min articles |
| `NEWS_CACHE_TTL_MINUTES` | `30.0` | No | News cache ttl minutes |

## Earnings filter + results (Features 1, 2)

| Variable | Default | Required | Description |
|---|---|---|---|
| `EARNINGS_FILTER_MODE` | `"off"` | No | off \| flag \| block |
| `EARNINGS_BLOCK_DAYS` | `2` | No | Earnings block days |
| `EARNINGS_RESULTS_ENABLED` | `False` | No | Earnings *results* (beat/miss, EPS/revenue surprise) from Finnhub, used by the beat-aware PEAD signal and the daily ear… |
| `EARNINGS_RESULTS_CACHE_TTL_MINUTES` | `360.0` | No | 6 h |
| `FMP_API_KEY` | `""` | No | Optional Financial Modeling Prep key (richer earnings/ratings source). |
| `CONTAGION_SURPRISE_THRESHOLD` | `5.0` | No | Daily earnings tracker (Feature 2): same-sector contagion alert fires when a bellwether's EPS surprise exceeds this mag… |
| `EARNINGS_HISTORY_ENABLED` | `True` | No | Earnings history enabled |

## Third-party ratings filter (Feature 5)

| Variable | Default | Required | Description |
|---|---|---|---|
| `RATINGS_FILTER_ENABLED` | `False` | No | repo), FMP optionally. Seeking Alpha is a documented, user-supplied extension only (its ToS prohibit scraping) and is n… |
| `RATINGS_PROVIDER` | `"finnhub"` | No | finnhub \| fmp |
| `RATINGS_MIN` | `"hold"` | No | strong_sell \| sell \| hold \| buy \| strong_buy |
| `RATINGS_FAIL_OPEN` | `True` | No | Ratings fail open |
| `RATINGS_CACHE_TTL_MINUTES` | `720.0` | No | 12 h (ratings change daily at most) |

## Market regime detection

| Variable | Default | Required | Description |
|---|---|---|---|
| `REGIME_DETECTION_ENABLED` | `True` | No | moving-average structure and realised volatility, then scale each strategy family's weight (momentum favoured in bull r… |
| `REGIME_BENCHMARK` | `"SPY"` | No | Regime benchmark |
| `REGIME_FAST_MA` | `50` | No | Regime fast ma |
| `REGIME_SLOW_MA` | `200` | No | Regime slow ma |
| `REGIME_VOL_WINDOW` | `20` | No | Regime vol window |
| `REGIME_HIGH_VOL_PCT` | `0.018` | No | Regime high vol pct |

## Strategy auto-tuning

| Variable | Default | Required | Description |
|---|---|---|---|
| `AUTOTUNE_ENABLED` | `False` | No | last AUTOTUNE_LOOKBACK_TRADES closed trades: a cold streak raises the bar (fewer, higher-quality entries); a hot streak… |
| `AUTOTUNE_LOOKBACK_TRADES` | `30` | No | Autotune lookback trades |
| `AUTOTUNE_MIN_TRADES` | `15` | No | Autotune min trades |
| `AUTOTUNE_MAX_GRADE_ADJUST` | `0.08` | No | Autotune max grade adjust |

## Scheduler + automated reports

| Variable | Default | Required | Description |
|---|---|---|---|
| `SCHEDULER_ENABLED` | `False` | No | A lightweight in-process scheduler (no external deps) that fires daily and weekly P&L email reports and nightly backtes… |
| `PNL_REPORT_ENABLED` | `False` | No | Pnl report enabled |
| `PNL_REPORT_DAILY_TIME` | `"17:00"` | No | Pnl report daily time |
| `PNL_REPORT_WEEKLY_ENABLED` | `True` | No | Pnl report weekly enabled |
| `PNL_REPORT_WEEKLY_DAY` | `"FRI"` | No | Pnl report weekly day |
| `SCHEDULED_BACKTEST_ENABLED` | `False` | No | Scheduled backtest enabled |
| `SCHEDULED_BACKTEST_TIME` | `"02:00"` | No | Scheduled backtest time |
| `SCHEDULED_BACKTEST_LOOKBACK_DAYS` | `180` | No | Scheduled backtest lookback days |

## Pre-market scanner

| Variable | Default | Required | Description |
|---|---|---|---|
| `PREMARKET_GAP_PCT` | `0.02` | No | Flag symbols gapping more than PREMARKET_GAP_PCT off the prior close or trading at more than PREMARKET_VOLUME_RATIO tim… |
| `PREMARKET_VOLUME_RATIO` | `1.5` | No | Premarket volume ratio |

## Extended-hours data + overnight-gap filter (Feature 4)

| Variable | Default | Required | Description |
|---|---|---|---|
| `EXTENDED_HOURS_ENABLED` | `False` | No | proxy. The gap filter then skips or resizes a morning entry when the stock gapped sharply against the trade overnight. … |
| `EXT_HOURS_PROVIDER` | `"auto"` | No | auto \| ibkr \| alpaca |
| `EXT_HOURS_CACHE_TTL_SECONDS` | `60.0` | No | Ext hours cache ttl seconds |
| `GAP_FILTER_ENABLED` | `False` | No | Gap filter enabled |
| `GAP_DOWN_SKIP_PCT` | `-0.05` | No | skip longs gapping <= -5% overnight |
| `GAP_UP_CHASE_PCT` | `0.08` | No | skip longs already gapped up >= +8% |
| `GAP_RESIZE_PCT` | `0.03` | No | resize (not skip) beyond this gap |
| `GAP_RESIZE_MODIFIER` | `0.5` | No | size multiplier when resizing |
| `EXT_UNUSUAL_VOLUME_RATIO` | `3.0` | No | Ext unusual volume ratio |

## Monte Carlo projection

| Variable | Default | Required | Description |
|---|---|---|---|
| `MONTE_CARLO_RUNS` | `1000` | No | Monte carlo runs |
| `MONTE_CARLO_HORIZON` | `50` | No | Monte carlo horizon |

## Multi-user support

| Variable | Default | Required | Description |
|---|---|---|---|
| `MULTI_USER_ENABLED` | `False` | No | When enabled the dashboard exposes registration/login and stores per-user accounts (with their own strategies, capital,… |

## REST API

| Variable | Default | Required | Description |
|---|---|---|---|
| `REST_API_ENABLED` | `True` | No | A documented, API-key-authenticated JSON API under /api/v1. Keys are minted from the dashboard and stored (hashed) in `… |

\* `DASHBOARD_PASSWORD` is required when `DASHBOARD_AUTH_ENABLED=true` (the default); the dashboard fails closed (HTTP 500) without it.

