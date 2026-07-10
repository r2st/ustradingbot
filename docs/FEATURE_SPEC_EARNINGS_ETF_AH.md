# Earnings, ETFs, Extended-Hours & Ratings — Feature Specification

**Status:** Proposed — awaiting review/approval (no code written yet)
**Scope:** Five feature areas — earnings risk filtering, a daily earnings tracker, ETF support, after-hours/pre-market data, and a third-party ratings filter (Seeking Alpha or alternative)
**Audience:** Anyone implementing engine / signal / data / dashboard work in this repo
**Date:** 2026-07-10
**Complexity legend:** S = hours · M = 1–2 days · L = 3+ days · XL = 1+ week

---

## 0. Read this first — how much is already built

**Three of the five requested areas are partly implemented.** This spec is
written as a *delta* against what exists, so we don't rebuild working code.
Grounding, module by module:

| Requested feature | Already in repo | File(s) | Gap this spec fills |
|---|---|---|---|
| 1. Earnings calendar + risk filter | Next-earnings lookup; **entry blackout is live** | `data/earnings.py`, `data/earnings_calendar.py`, `ai/analyst.py` (Tier-1), `short_strategies/risk/filters.py` | Configurable N-day pre-earnings **block/flag**, earnings *results* (EPS/rev actual vs est), post-earnings gap-and-go signal |
| 2. Daily earnings tracker | Upcoming-earnings API + dashboard section | `dashboard/insights_router.py` (`/api/earnings`), `dashboard.html` (`#nav-earnings`) | "Reporting **today**" view, pre/post-market moves, beat/miss + surprise %, sector-contagion alerts, per-symbol history |
| 3. ETF support | Nothing ETF-aware | — | ETF universe, asset-type tag, ETF-specific sizing, sector-rotation strategy, breadth |
| 4. After-hours / pre-market data | Gap/volume scan **off daily bars** (proxy, not real EH) | `signals/premarket.py`, `/api/premarket`, `#nav-scanner` | True extended-hours quotes/volume, overnight-gap morning filter feeding the entry pipeline |
| 5. Seeking Alpha / ratings | Nothing external-ratings-aware (`dashboard/analyst_cards.py` is unrelated — it explains the bot's *own* reasoning) | — | Quant rating / factor grades / price-target ingestion + entry filter |

### 0.1 Architecture we build on

- **Data layer.** `data/fetcher.py` (`fetch_ohlcv`, `fetch_current_price`,
  `fetch_multiple`; TTL cache + retry) sits over `data/providers.py`, which
  defines the `MarketDataProvider` Protocol (`get_ohlcv`, `get_current_price`,
  `supports_streaming`) with `YFinanceProvider` (default), `AlpacaProvider`
  (REST + websocket), `PolygonProvider` (REST), and `FallbackProvider`
  (primary→secondary with a circuit breaker). `make_provider(settings)` is the
  factory. **All providers return daily bars only.** IBKR is a *broker*
  (`execution/broker.py`), **not** a data provider today.
- **Universe / watchlist.** `config/universe.py` holds `US_WATCHLIST` (30
  symbols), `CA_WATCHLIST` (11 TSX), `ALL_SYMBOLS`, and `SECTOR_BY_SYMBOL` +
  `get_sector()` / `is_canadian()` / `get_currency()`. The engine actually
  scans `config/watchlist.scan_symbols_for(settings)` → the user's JSON-backed
  `WatchlistStore` (named, enable-able lists) or the universe DB. **No
  asset-type concept exists — every symbol is treated as an individual stock.**
- **Signal pipeline.** `signals/combined_filter.score_symbol(symbol, strategy,
  df)` runs 5 indicators + hard vetoes → a `Signal` with a `Grade`. Scans live
  in `signals/screener.py`, `short_strategies/`, `selective_strategies/`, and
  are orchestrated by `engine.TradingEngine.run_cycle()`.
- **Entry gate pipeline.** `engine.TradingEngine._process_signal(sig)` runs
  every candidate through ordered gates, logging each rejection via
  `RejectedSignalLogger` with a `gate` name (surfaced to users through
  `dashboard/analyst_cards._GATE_EXPLANATIONS`):

  | Order | Gate name | Code |
  |---|---|---|
  | a0 | `trade_selection` | `selection.allows_signal(sig)` |
  | a | `pre_check` | `risk_manager.pre_check` |
  | b | `strategy_cap` | `risk_manager.check_strategy_cap` |
  | c | `pending_order_guard` | broker already holds symbol |
  | d | `ai_veto` | `ai_analyst.evaluate` (**Tier-1 = earnings blackout**, Tier-2 = OpenRouter) |
  | d2 | `news_sentiment` | `news_filter.check` |
  | d3 | `regime_autotune` | `_regime_autotune_gate` |
  | e | `build_order` | `risk_manager.build_order` (sizing) |
  | f | `freshness_check` | price drift / signal age |
  | g | `cash_check` | available cash |
  | h | — | bracket order placement |

  **This ordered list is where every new pre-entry filter hooks in.** New
  gates append a branch here + a `_GATE_EXPLANATIONS` entry + a settings flag.
- **Dashboard.** A single Jinja2 page (`dashboard/templates/dashboard.html`,
  ~5,300 lines, vanilla JS, `<div class="section" id="nav-X">` layout + nav
  anchors) served by FastAPI (`dashboard/app.py`), HTTP-Basic-guarded
  (`dashboard/auth.require_auth`). Feature routers are `dashboard/*_router.py`,
  each an `APIRouter`, mounted in the `for _r in (...)` loop at the bottom of
  `app.py`. Network-bound work runs via `run_in_threadpool`.
- **External-data filter precedent.** `data/news_sentiment.NewsSentimentFilter`
  (`.check(symbol) -> SentimentResult(approved, reason)`, Finnhub-backed,
  TTL-cached, **fail-open**, off by default) is the exact template for the
  ratings filter (Feature 5) and the results/gap filters. Copy its shape.
- **Config.** `config/settings.Settings` (env-overridable dataclass) already
  carries `TOTAL_CAPITAL=12000`, `MAX_POSITION_SIZE_PCT=0.015`,
  `AI_EARNINGS_BLACKOUT_DAYS=14`, provider keys (`POLYGON_API_KEY`,
  `ALPACA_*`, `FINNHUB_API_KEY`), and `PREMARKET_GAP_PCT`/`PREMARKET_VOLUME_RATIO`.
  All new tunables go here + `.env.example`.

### 0.2 Cross-cutting design rules (apply to every feature below)

1. **Fail-open, never crash the scan.** All new data lookups return
   `None`/`False`/empty on error and log; a broken external API must never stop
   the engine (mirrors `data/earnings.py` and `NewsSentimentFilter`).
2. **Cache aggressively.** Earnings/ratings change daily at most — reuse the
   TTL-cache pattern (`data/earnings_calendar.py` uses a 6 h in-process cache).
3. **Threadpool for network calls in routers.** Never block the event loop.
4. **Every new gate is off by default** behind a settings flag until validated
   in paper mode.
5. **Timezone.** Use `config.settings.EASTERN`; recent commits standardized all
   timestamps to Eastern.

---

## Feature 1 — Earnings Calendar + Risk Filter

### 1.1 Overview & purpose
Prevent the bot from opening a position right before an earnings report (binary
gap risk), and turn earnings into a *signal* rather than only a hazard: detect
post-earnings momentum (gap-and-go on a beat) and track actual results.

Three sub-capabilities:
- **(1a) Configurable pre-earnings block/flag** — block (or soft-flag) new
  entries 1–2 days before earnings. *Partly exists:* `ai/analyst.py` already
  hard-rejects any non-PEAD signal when `is_earnings_within_days(symbol,
  days=AI_EARNINGS_BLACKOUT_DAYS=14)`. Gap: that window is coarse (14 d, buried
  in the AI gate) and can't distinguish "flag" from "block."
- **(1b) Earnings results tracking** — beat/miss/inline, EPS actual vs
  estimate, revenue actual vs estimate. *New.*
- **(1c) Post-earnings momentum signal** — gap-and-go entries on a beat.
  *Partly exists:* `signals/pead_signal.py` + `data.earnings.get_recent_earnings`
  already compute post-earnings price move / volume ratio / gap direction. Gap:
  no *fundamental* beat/miss context feeding it.

### 1.2 Data sources (free/low-cost preferred)
| Need | Primary (free) | Fallback / paid |
|---|---|---|
| Next earnings date | **yfinance** `get_earnings_dates()` — already used | Financial Modeling Prep (FMP) `/earning_calendar`, Alpha Vantage `EARNINGS_CALENDAR` (CSV) |
| EPS/revenue actual vs estimate | **FMP** `/earnings-surprises/{sym}` & `/income-statement` (free tier ~250 req/day); **Alpha Vantage** `EARNINGS` (free, 25 req/day) | Finnhub `/stock/earnings` (already have `FINNHUB_API_KEY`) |
| Post-earnings OHLCV | existing `data/fetcher` daily bars | — |

**Recommendation:** add **Finnhub** as the results source — the key
(`FINNHUB_API_KEY`) and the fail-open filter pattern already exist. Keep
yfinance for dates. FMP as an optional richer source behind a new key.

### 1.3 Implementation approach
**New / modified files:**
- **`data/earnings.py`** (extend) — add `get_earnings_result(symbol) ->
  EarningsResult | None` with `EarningsResult(report_date, eps_actual,
  eps_estimate, eps_surprise_pct, rev_actual, rev_estimate, rev_surprise_pct,
  verdict: "beat"|"miss"|"inline")`. Finnhub-backed, TTL-cached, fail-open.
- **`signals/earnings_filter.py`** (new) — `EarningsEntryFilter.check(sig) ->
  (allowed: bool, mode: "block"|"flag"|"ok", reason)` reading
  `EARNINGS_BLOCK_DAYS` (default 2) and `EARNINGS_FILTER_MODE`
  (`block`|`flag`|`off`). Reuses `data.earnings_calendar.next_earnings_date`.
- **`signals/pead_signal.py`** (extend) — incorporate `EarningsResult.verdict`
  so gap-and-go only fires on a *beat* + upward gap + volume confirmation.
- **`config/settings.py`** — `EARNINGS_FILTER_MODE`, `EARNINGS_BLOCK_DAYS`,
  `EARNINGS_RESULTS_ENABLED`, optional `FMP_API_KEY`.
- **`engine.py`** — add gate **(a1)** `earnings_filter` right after
  `trade_selection` (before the paid AI call — cheap to reject early). In
  `flag` mode it does not reject; it annotates `sig.raw_data["earnings_flag"]`
  for display and lets the trade through.

### 1.4 Dashboard UI changes
- Extend the existing **Earnings Calendar** section (`#nav-earnings`) columns:
  add "Report time" (BMO/AMC), a colored "≤N days" flag badge, and (when
  available) last-quarter verdict.
- New rejection reason in `analyst_cards._GATE_EXPLANATIONS`:
  `"earnings_filter": "earnings report is within the blackout window, so the
  bot is holding off"`.

### 1.5 Pipeline integration
New gate a1 in `_process_signal`. PEAD strategy is exempt (as today). In
`flag` mode, the Analyst card shows an "earnings within N days" banner but the
trade proceeds.

### 1.6 Priority & complexity
**Priority: HIGH** (protects capital; mostly wiring existing pieces).
- 1a block/flag gate: **S** · 1b results tracking: **M** · 1c beat-aware PEAD: **S–M**.

### 1.7 Dependencies
Standalone. 1b's `EarningsResult` is consumed by Feature 2 (tracker) and
Feature 1c. Shares the Finnhub client with Feature 5 if Finnhub is chosen there.

---

## Feature 2 — Daily Earnings Tracker

### 2.1 Overview & purpose
A dashboard view answering "who reports today, and how did the market react?"
— pre-market and after-hours reporters, their pre/post-market move, beat/miss
with EPS surprise %, per-symbol history, and **sector-contagion alerts** (e.g.
NVDA beats → flag other watchlist semis).

### 2.2 Data sources
- **Today's reporters:** filter `data.earnings_calendar.upcoming_earnings(...)`
  to `days_until == 0`, plus BMO/AMC session tag (Finnhub/FMP provide it;
  yfinance's timestamp hour is a rough proxy).
- **Pre/post-market move:** requires extended-hours quotes → **depends on
  Feature 4.** Until F4 lands, degrade to the daily-bar gap proxy already in
  `signals/premarket.py`.
- **Beat/miss + surprise %:** `EarningsResult` from Feature 1b.
- **Sector map:** `config.universe.get_sector()` (already covers the watchlist).
- **History:** persist each reported result to
  `data_store/earnings_history.jsonl` (append-only, like the rejected-signal
  logger) keyed by `(symbol, report_date)`.

### 2.3 Implementation approach
**New files:**
- **`data/earnings_tracker.py`** — `todays_earnings(symbols) ->
  List[DailyEarnings]` combining calendar + result + (F4) extended-hours move;
  `sector_contagion(reported: DailyEarnings, watchlist) -> List[str]`
  (same-sector peers to flag when a bellwether beats/misses beyond a
  configurable surprise threshold); `record_result(...)` / `symbol_history(sym)`
  over the JSONL store.
- **`dashboard/earnings_router.py`** (new `APIRouter`, `prefix="/api"`) —
  `GET /api/earnings/today`, `GET /api/earnings/history/{symbol}`,
  `GET /api/earnings/contagion`. Mount in the `app.py` include loop.
  *(Alternatively extend `insights_router.py`, which already owns `/api/earnings`
  — a dedicated router is cleaner given the number of new endpoints.)*
- **`config/settings.py`** — `CONTAGION_SURPRISE_THRESHOLD` (default 5.0 %),
  `EARNINGS_HISTORY_ENABLED`.

### 2.4 Dashboard UI changes
Split the `#nav-earnings` section into two tabs/subsections:
- **Upcoming** (existing table, enriched per 1.4).
- **Reporting today** (new): table `Symbol | Session (BMO/AMC) | Est EPS |
  Actual | Surprise % | Pre/Post move | Sector` with beat=green / miss=red
  chips. A "Sector contagion" callout lists flagged peers with a one-click
  "add to today's watch" affordance. Vanilla JS `loadTodaysEarnings()` mirrors
  the existing `loadEarnings()`.

### 2.5 Pipeline integration
Read-mostly / informational. Contagion flags can *optionally* feed a soft-flag
on peer signals (annotation only), but the tracker does not gate entries
itself. Post-earnings drift on a same-sector peer remains PEAD's job.

### 2.6 Priority & complexity
**Priority: MEDIUM.** Core table + history: **M**. Contagion: **S**. Full
pre/post-market move column: **blocked on Feature 4** (until then, daily-gap
proxy).

### 2.7 Dependencies
Feature 1b (`EarningsResult`) **required** for beat/miss columns. Feature 4
**recommended** for accurate pre/post-market moves.

---

## Feature 3 — ETF Support

### 3.1 Overview & purpose
Add major and sector ETFs as first-class watchlist members, tag them as a
distinct **asset type**, size them differently from single names, use them for
**market-breadth** and a **sector-rotation** strategy.

- Broad: SPY, QQQ, IWM, DIA.
- Sector (SPDR): XLF, XLE, XLK, XLV, XLI, XLP, XLU, XLRE, XLC, XLB, XLY.

### 3.2 Data sources
No new provider — ETFs are ordinary tickers on yfinance/Alpaca/Polygon. The
GICS-sector→ETF mapping is a static table (below). Breadth uses existing daily
bars for the 11 sector ETFs.

### 3.3 Implementation approach
**New / modified files:**
- **`config/etf_universe.py`** (new) — `BROAD_MARKET_ETFS`, `SECTOR_ETFS`
  (dict: GICS sector → ETF, e.g. `"Technology": "XLK"`), `ALL_ETFS`,
  `is_etf(symbol) -> bool` (set membership), `sector_for_etf()` /
  `etf_for_sector()`.
- **`config/universe.py`** (extend) — merge ETF sector tags into
  `SECTOR_BY_SYMBOL` so the risk dashboard's concentration math already works;
  add an `asset_type(symbol) -> "etf"|"stock"` helper (delegates to `is_etf`).
- **`config/watchlist.py`** — seed a default **"ETFs"** named list in
  `_default_lists()` (enabled). `_SYMBOL_RE` already accepts these tickers.
- **`risk/manager.py` `build_order()`** — add an ETF branch to the existing
  strategy-modifier ladder (currently `mean_reversion=0.5`, `short_*` modifier,
  else `1.0`). ETFs are less volatile → allow a **larger** notional cap but the
  same risk budget: introduce `ETF_RISK_MODIFIER` (default ~1.3)
  and `ETF_NOTIONAL_CAP_PCT` (default 0.15 vs the 0.10 stock cap). Gate the
  branch on `config.etf_universe.is_etf(signal.symbol)`.
- **`signals/combined_filter.py`** — ETFs should skip the single-name
  `low_atr` veto tuning implicitly (their ATR% is lower); expose
  `MIN_ATR_PCT_ETF` so ETFs aren't vetoed purely for being calm. Small change
  in `_check_low_atr` caller path.
- **`signals/sector_rotation.py`** (new strategy/scanner) — rank the 11 sector
  ETFs by relative strength (e.g. 3-mo return + trend vs SPY), emit long
  signals on the top-N rotating leaders. Registered like existing scanners and
  invoked from `engine._run_*_scan`. New strategy id `"sector_rotation"` with a
  weights profile in `config/settings.weights_for_strategy` and a tag in
  `analyst_cards.STRATEGY_TAGS`.
- **`analytics/breadth.py`** (new, optional) — market-breadth indicator from
  sector-ETF participation (how many sector ETFs are above their 50-day MA);
  surfaced in the Risk / Market-Intelligence section.

### 3.4 Dashboard UI changes
- Watchlist UI: show an **ETF** badge next to `asset_type=="etf"` rows.
- New **Sector Rotation** panel (in `#nav-strategy` or a new `#nav-sectors`):
  ranked sector-ETF table with relative-strength score + current leaders.
- Optional breadth gauge in `#nav-risk` / `#nav-montecarlo`.
- Add `"sector_rotation"` to the Trade Selection UI
  (`trade_selection_router` + selection config) so users can enable/disable it.

### 3.5 Pipeline integration
ETFs flow through the *same* scan→score→gate→execute pipeline. Only two
touch-points change behavior: `build_order` sizing (ETF branch) and the ATR
veto threshold. Sector-rotation is a new scanner feeding `_process_signal`
normally. The earnings filter (F1) is naturally a **no-op** for ETFs
(no single-name earnings date) — verify `next_earnings_date` returns `None`
for ETFs and that this fails-open to "allowed."

### 3.6 Priority & complexity
**Priority: HIGH** (broadens the tradable universe; low risk, mostly additive).
- ETF universe + tag + watchlist seed: **S**.
- Sizing + ATR branch: **S**.
- Sector-rotation strategy: **M–L**.
- Breadth indicator: **M** (optional).

### 3.7 Dependencies
Standalone. Sector-rotation optionally consumes Feature 4's gap filter and
Feature 2's contagion flags but needs neither. `asset_type()` is reused by
Feature 4 (different gap thresholds for ETFs).

---

## Feature 4 — After-Hours / Pre-Market Data

### 4.1 Overview & purpose
Monitor *true* extended-hours price action, detect significant overnight gaps,
analyze pre-market volume, alert on unusual AH activity, and — most
importantly — use overnight moves as a **morning entry filter** (e.g. skip or
resize a long if the stock gapped down 5 %+ overnight).

**Key gap vs today:** `signals/premarket.py` computes gaps from **daily bars**
(treats the last daily bar as "latest") — a proxy that only works once a
regular-session bar exists. It cannot see genuine pre-open / post-close prints.

### 4.2 Data sources
| Source | Extended-hours support | Notes |
|---|---|---|
| **IBKR** (via `execution/broker.py`) | **Yes** — `reqMktData` / historical bars with `useRTH=False`; the broker connection already exists in live mode | Best option; the user explicitly noted "IBKR provides extended hours data." Requires a running TWS/Gateway. |
| **Alpaca** | Yes — IEX feed includes pre/post trades; latest-trade + minute bars | Already wired as a provider; free IEX feed. Good default when no IBKR. |
| **Polygon** | Yes on paid tiers | Free tier is delayed/limited. |
| yfinance | Partial `prepost=True` on intraday history | Unreliable; keep as last-resort proxy. |

**Recommendation:** add an **extended-hours capability to the provider
abstraction**, implemented for IBKR (primary in live mode) and Alpaca
(fallback). Extend the `MarketDataProvider` Protocol with an *optional*
`get_extended_hours_quote(symbol) -> ExtQuote | None` (pre/post price, session
tag, extended-hours volume), duck-typed with `getattr` so existing providers
that lack it simply return `None` (fail-open).

### 4.3 Implementation approach
**New / modified files:**
- **`data/providers.py`** — add optional `get_extended_hours_quote` to the
  Protocol; implement on `AlpacaProvider` (latest trade + today's minute bars
  with `feed=IEX`, partitioning by session) and, for IBKR, a thin
  `IBKRDataProvider` **or** a method on the existing broker wrapper (the broker
  already holds the `ib_insync` connection). `FallbackProvider._call` extended
  to pass this method through.
- **`data/extended_hours.py`** (new) — `ExtQuote` dataclass
  `(symbol, session: "pre"|"post"|"closed", last, prev_close, gap_pct,
  ext_volume, avg_ext_volume, unusual: bool)`; `overnight_gap(symbol) ->
  ExtQuote | None`; `scan_extended_hours(symbols) -> List[ExtQuote]`. Fail-open,
  TTL-cached (short TTL, ~60 s, since these move).
- **`signals/premarket.py`** — when a real `ExtQuote` is available, use it
  instead of the daily-bar proxy; keep the proxy as fallback. `scan()` gains an
  extended-hours path.
- **`signals/gap_filter.py`** (new) — `GapEntryFilter.check(sig) ->
  (allowed, action: "skip"|"resize"|"ok", reason)` using thresholds
  `GAP_DOWN_SKIP_PCT` (default −5 %) and `GAP_UP_CHASE_PCT`. For longs: skip on
  a large adverse gap-down; for shorts: mirror. `resize` can shrink size via a
  size modifier rather than a hard skip.
- **`config/settings.py`** — `EXTENDED_HOURS_ENABLED`, `GAP_FILTER_ENABLED`,
  `GAP_DOWN_SKIP_PCT`, `GAP_UP_CHASE_PCT`, `EXT_UNUSUAL_VOLUME_RATIO`,
  `EXT_HOURS_PROVIDER` (auto/ibkr/alpaca).
- **`engine.py`** — new gate **(f2)** `gap_filter` near the freshness check
  (both are "has the world moved since the setup?" checks). Alerts on unusual
  AH activity route through the existing `agent/alerts.AlertManager`.

### 4.4 Dashboard UI changes
- Rename/extend `#nav-scanner` ("Pre-Market Scanner") to show a **live/last-print
  price**, session tag (Pre/Post/Closed), extended-hours volume, and an
  "unusual" flag when `ext_volume/avg > EXT_UNUSUAL_VOLUME_RATIO`.
- Add a rejection reason `"gap_filter"` to `_GATE_EXPLANATIONS`
  ("price gapped sharply overnight, so the bot skipped/resized the entry").
- Optional: a compact "overnight movers" strip on the main dashboard.

### 4.5 Pipeline integration
Gate f2 in `_process_signal`. Because the engine's scan cadence and
order-cutoff logic already exist (`_is_past_order_cutoff`), the gap filter is
purely additive. In `resize` mode it feeds a size modifier into
`build_order` (there is already an `ai_size_modifier` hook set to `1.0` — reuse
that mechanism for a `gap_size_modifier`).

### 4.6 Priority & complexity
**Priority: MEDIUM–HIGH** (real morning-gap protection is valuable; the daily
proxy is a known weakness).
- Alpaca extended-hours quote + `ExtQuote` + gap filter: **M**.
- IBKR extended-hours provider: **M–L** (depends on live TWS/Gateway for
  testing).
- Full scanner UI upgrade: **S–M**.

### 4.7 Dependencies
Unblocks Feature 2's pre/post-market move column. Shares the provider
abstraction with the existing data layer. `asset_type()` (Feature 3) informs
ETF-specific gap thresholds (optional).

---

## Feature 5 — Seeking Alpha / Third-Party Ratings Filter

### 5.1 Overview & purpose
Use third-party equity ratings as an **entry filter and/or signal**: only trade
names with an acceptable Quant Rating (≥ "Hold"), read factor grades (Value,
Growth, Profitability, Momentum, EPS Revisions), analyst consensus / price
targets, and treat recent rating changes as signals.

### 5.2 Data sources — ⚠️ licensing reality
**Seeking Alpha has no official public API and its ToS prohibit scraping.**
Do **not** scrape SA. This spec therefore treats "Seeking Alpha Quant Rating"
as *one possible provider behind an abstraction* and defaults to a
license-clean alternative.

| Provider | Access | Data | License note |
|---|---|---|---|
| **Seeking Alpha** | Unofficial only (RapidAPI mirrors exist, legally grey) | Quant Rating, factor grades, price targets | **Avoid unless the user holds a commercial data license.** Document, don't implement by default. |
| **Finnhub** *(recommended default)* | Official API, key already in repo (`FINNHUB_API_KEY`) | Analyst recommendation trends, price targets, upgrade/downgrade events | Free tier; clean license. |
| **FMP** | Official API | Analyst estimates, price targets, upgrades/downgrades, a "rating" score | Free tier ~250 req/day. |
| **Zacks** | Rank via some data vendors / limited free | Zacks Rank (1–5) | Redistribution restricted; verify license. |
| **TipRanks** | Unofficial | Smart Score, analyst consensus | Same grey-area caution as SA. |

**Recommendation:** implement a **`RatingsProvider` abstraction** (mirroring
`MarketDataProvider`) with a **Finnhub** implementation as the default and an
optional FMP implementation. Model a normalized rating enum
(`STRONG_BUY > BUY > HOLD > SELL > STRONG_SELL`) so the "≥ Hold" filter is
provider-agnostic. A Seeking-Alpha adapter is a documented extension point the
user can supply *if* they have a licensed feed — not shipped.

### 5.3 Implementation approach
**New files (modeled directly on `data/news_sentiment.py`):**
- **`data/ratings.py`** — `Rating` enum + `RatingSnapshot(symbol, quant_rating,
  factor_grades: dict, consensus, price_target, recent_change, as_of)`;
  `RatingsProvider` Protocol; `FinnhubRatingsProvider` (+ optional
  `FMPRatingsProvider`); `make_ratings_provider(settings)` factory. TTL-cached
  (daily), fail-open.
- **`signals/ratings_filter.py`** — `RatingsFilter.check(sig) ->
  SentimentResult`-style `(approved, reason)`. Rejects when
  `quant_rating < RATINGS_MIN` (default HOLD). Fail-open when data is missing
  (documented, like the short-interest filter's `short_interest_fail_open`).
- **`config/settings.py`** — `RATINGS_FILTER_ENABLED` (default False),
  `RATINGS_PROVIDER` (`finnhub`/`fmp`), `RATINGS_MIN` (default `"hold"`),
  `RATINGS_FAIL_OPEN` (default True), optional `FMP_API_KEY`.
- **`engine.py`** — new gate **(d4)** `ratings_filter` right after
  `news_sentiment` (both are external-context vetoes; group them). Off by
  default.
- **Rating-change-as-signal (optional, phase 2):** a small scanner
  `signals/ratings_signal.py` that emits a watch/soft-flag when
  `recent_change` is a fresh upgrade — annotation only, not an auto-entry.

### 5.4 Dashboard UI changes
- New **Ratings** column/expander on watchlist Analyst cards: quant rating chip,
  factor-grade mini-grid, consensus + price target with upside %. Because
  `analyst_cards.py` builds structured card payloads, add a `ratings` block to
  `build_watchlist_card`/`build_position_card` (fail-open to "—" when absent).
- New rejection reason `"ratings_filter"` in `_GATE_EXPLANATIONS`
  ("a third-party quant rating for this stock is below the minimum you set").
- Provider/key management: reuse the existing API-keys UI
  (`dashboard/api_keys.py`, `provider_control.py`) to store the ratings key.

### 5.5 Pipeline integration
Gate d4 in `_process_signal`, off by default, fail-open. ETFs and any symbol
without coverage pass through untouched.

### 5.6 Priority & complexity
**Priority: MEDIUM** (strong filter value, but licensing makes the "Seeking
Alpha" ask specifically risky — deliver the *capability* via a clean provider).
- Finnhub ratings provider + filter + settings: **M**.
- Card UI block: **S–M**.
- FMP provider: **S**. Rating-change signal: **M** (phase 2).
- Seeking Alpha adapter: **not scheduled** — documented extension point only.

### 5.7 Dependencies
Standalone. Shares the Finnhub client and the fail-open external-filter pattern
with Features 1b/2. Independent of ETF/AH work.

---

## 6. Build order, dependency graph & rollout

### 6.1 Dependency graph
```
F3 ETF universe/tag ──► F3 sizing ──► F3 sector-rotation
        │
        └──(asset_type)──► F4 ETF gap thresholds (optional)

F1a earnings block/flag gate (standalone, mostly wiring)
F1b earnings results ──► F1c beat-aware PEAD
                    └──► F2 tracker (beat/miss columns)
F4 extended-hours ──────► F2 tracker (pre/post move columns)
F5 ratings (standalone)
```

### 6.2 Recommended sequencing
1. **F3 ETF universe + tag + sizing** (HIGH, low-risk, additive) — immediate
   universe expansion; unblocks nothing but broadens value fast.
2. **F1a earnings block/flag gate** (HIGH) — promotes the existing 14-day AI
   blackout into a first-class, configurable gate. Small, high-value.
3. **F1b earnings results** → **F1c beat-aware PEAD** (HIGH/MED).
4. **F4 extended-hours + gap filter** (MED-HIGH) — Alpaca path first (testable
   without TWS), IBKR path second.
5. **F2 daily earnings tracker** (MED) — lands cleanly once F1b + F4 exist.
6. **F5 ratings filter** (MED) — independent; schedule against user priority.
7. **F3 sector-rotation strategy + breadth** (MED-L) — last, largest new
   surface.

### 6.3 Rollout / safety
- Every new gate ships **off by default** behind a settings flag; enable in
  **paper mode** first (`PaperBroker`), watch `RejectedSignalLogger` output and
  the Analyst-card rejection explanations before enabling live.
- New external providers land **fail-open** so an outage never halts scanning.
- Add tests mirroring the existing suite (`tests/test_premarket.py`,
  `tests/test_providers.py`, `tests/test_analyst_cards.py`,
  `tests/test_feature_routers.py`) for each new module. Run with
  `.venv/bin/python -m pytest`.

### 6.4 New settings summary (all in `config/settings.py` + `.env.example`)
```
# F1
EARNINGS_FILTER_MODE = "off"          # off | flag | block
EARNINGS_BLOCK_DAYS = 2
EARNINGS_RESULTS_ENABLED = False
FMP_API_KEY = ""                      # optional, shared with F2/F5
# F2
CONTAGION_SURPRISE_THRESHOLD = 5.0
EARNINGS_HISTORY_ENABLED = True
# F3
ETF_RISK_MODIFIER = 1.3
ETF_NOTIONAL_CAP_PCT = 0.15
MIN_ATR_PCT_ETF = 0.008
# F4
EXTENDED_HOURS_ENABLED = False
EXT_HOURS_PROVIDER = "auto"           # auto | ibkr | alpaca
GAP_FILTER_ENABLED = False
GAP_DOWN_SKIP_PCT = -0.05
GAP_UP_CHASE_PCT = 0.08
EXT_UNUSUAL_VOLUME_RATIO = 3.0
# F5
RATINGS_FILTER_ENABLED = False
RATINGS_PROVIDER = "finnhub"          # finnhub | fmp
RATINGS_MIN = "hold"
RATINGS_FAIL_OPEN = True
```

### 6.5 Open questions for the reviewer
1. **Earnings filter default:** ship `flag` or `block` as the recommended
   mode? (Current AI-layer behavior is effectively a 14-day hard block.)
2. **Extended-hours provider:** is a live IBKR TWS/Gateway available for the
   engine, or should Alpaca-IEX be the primary EH source?
3. **Ratings provider:** confirm we default to **Finnhub** (clean license) and
   treat Seeking Alpha as a documented, user-supplied extension only.
4. **Sector-rotation:** a new *strategy* competing for capital, or an
   *informational* panel only, in the first cut?
5. **ETF sizing:** confirm the larger notional cap (15 %) / risk modifier (1.3)
   directions before implementation.
