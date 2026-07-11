# Tiered Symbol Universe

> The scanner no longer looks at a fixed 40-symbol watchlist. It runs a
> three-tier universe built on the **S&P 500** and **NASDAQ-100**, scanning the
> hottest names every cycle, a liquid scan pool daily, and the full index
> universe weekly — and **auto-promotes** any symbol that produces a signal into
> the every-cycle tier for a while. This document explains the tier model,
> auto-promotion, universe seeding, configuration, and the **Universe Browser**
> dashboard tab.

---

## 1. The three tiers

| Tier | Name | Membership | Cadence | Workers |
|------|------|------------|---------|---------|
| **Tier 1** | Active Trading | user watchlist ∪ ETFs ∪ auto-promoted symbols | **every cycle** | `TIER1_WORKERS` (4) |
| **Tier 2** | Scan Pool | top-N S&P 500 by volume × market cap | **daily** | `TIER2_WORKERS` (8) |
| **Tier 3** | Universe | full S&P 500 ∪ NASDAQ-100 (pre-screened) | **weekly** | `TIER3_WORKERS` (16) |

The tier machinery lives in `config/universe.py` and reads from the universe
SQLite DB (`data_store/universe.db`) when present, falling back to the curated
static lists in `config/index_membership.py` so **every tier is populated even
with no DB** and stays deterministic in tests.

### Tier 1 — Active Trading (`engine.py`, entry phase)

Runs on **every** scan cycle. The set is assembled fresh each cycle:

1. Base symbols from `scan_symbols_for()` — the user's JSON watchlist
   (`data_store/watchlists.json`) if present, else the universe DB's Tier 1,
   else the built-in `ALL_SYMBOLS`.
2. **Expire stale promotions** (`expire_promotions()`), then fold the surviving
   promoted symbols in.
3. Filter by the operator's dashboard trade-selection.

The resulting set is remembered as `_tier1_symbol_set` so Tier 2/3 can skip
names already covered every cycle.

### Tier 2 — Scan Pool (`_run_tier2_scan`)

Runs **once per day**. Loads `get_scan_pool_symbols(limit=TIER2_SCAN_POOL_SIZE)`
— the most liquid slice of the S&P 500, ranked by `avg_volume × market_cap` when
the DB has that metadata (else the curated liquidity-priority order). Symbols
already in Tier 1 are skipped. Any signal it finds **auto-promotes** its symbol
into Tier 1.

### Tier 3 — Universe (`_run_tier3_scan`)

Runs **once per week** (ISO year-week). Loads `get_index_universe_symbols()` —
the full S&P 500 ∪ NASDAQ-100 — then runs a lightweight **pre-screen** to find
unusual movers (≥ `TIER3_PRESCREEN_PRICE_CHANGE_PCT` daily move **or**
≥ `TIER3_PRESCREEN_VOLUME_RATIO` × average volume). Only the qualifying movers
are full-scanned, keeping the weekly sweep cheap. Any signal auto-promotes its
symbol into Tier 1.

---

## 2. Auto-promotion (Tier 2/3 → Tier 1)

When a Tier 2 or Tier 3 scan produces a signal, `_promote_signals()` calls
`promote_to_tier1(symbol, source_tier, reason)` for each signalling symbol. The
promotion is persisted to the universe DB (so it survives restarts and is
visible to the dashboard) and the symbol joins the every-cycle Active-Trading
set for **`PROMOTION_TTL_HOURS`** (default 72h).

- On each cycle the engine calls `expire_promotions()` first, so lapsed
  promotions revert to their scan-pool cadence unless a fresh signal re-promotes
  them.
- Set `PROMOTION_TTL_HOURS` to a non-positive value to make promotions never
  expire.
- Promotion is **best-effort**: with no universe DB, `promote_to_tier1()`
  returns `False` and the promotion is simply not recorded — it never breaks a
  scan.

The reason recorded is the firing `strategy` and `grade` (e.g. `"momentum A"`),
which the dashboard shows alongside the TTL remaining.

---

## 3. Index membership & the universe DB

### `config/index_membership.py`

The single source that *narrows* the tradable universe to the two approved
indices. Two access paths, in priority order:

1. **Curated static lists** — `SP500` (~470 large/mid-cap constituents) and
   `NASDAQ_100` (~100), embedded so the bot works fully offline and tests are
   deterministic. Class-share tickers use the hyphenated form the data providers
   expect (`BRK-B`, `BF-B`). These are updated by hand as indices reconstitute.
2. **Network refresh** — `fetch_index_constituents()` best-effort-pulls the
   authoritative list from Wikipedia (free, no auth) for the seeder's refresh,
   always falling back to the static list so a network outage never empties a
   tier.

`index_universe()` returns the deduplicated S&P 500 ∪ NASDAQ-100 union.

### `data_store/universe_seeder.py`

Populates the universe SQLite DB (`data_store/universe.db`):

- fetches US equities from SEC EDGAR (free, no auth),
- adds curated Canadian stocks and major ETFs,
- optionally enriches with yfinance sector/price data,
- seeds S&P 500 / NASDAQ-100 index membership and the liquidity ranking used by
  the scan pool,
- migrates the existing watchlist into `user_watchlists`.

Run it standalone:

```bash
python -m data_store.universe_seeder                 # full seed (with enrichment)
python -m data_store.universe_seeder --skip-enrichment   # faster, no yfinance
```

Or trigger a background re-seed from the dashboard (see §5). When the DB is
absent, the tier functions in `config/universe.py` transparently fall back to
the static lists, so the bot still trades a sensible universe.

---

## 4. Configuration

All in `config/settings.py` / `.env`:

| Setting | Default | Purpose |
|---------|---------|---------|
| `TIERED_SCANNING_ENABLED` | `True` | Master switch for the tiered scanner. |
| `TIER1_WORKERS` | `4` | Parallel workers for the every-cycle Tier 1 scan. |
| `TIER2_ENABLED` | `True` | Enable the daily Scan Pool sweep. |
| `TIER2_WORKERS` | `8` | Parallel workers for Tier 2. |
| `TIER2_SCAN_POOL_SIZE` | `250` | Top-N S&P 500 names (by liquidity) in the scan pool. |
| `TIER2_INTERVAL_MINUTES` | `30` | Legacy sector-rotation throttle (unused by index tiers). |
| `TIER3_ENABLED` | `True` | Enable the weekly full-universe sweep. |
| `TIER3_WORKERS` | `16` | Parallel workers for Tier 3. |
| `TIER3_PRESCREEN_PRICE_CHANGE_PCT` | `0.03` | Pre-screen: min daily move (3%) to qualify. |
| `TIER3_PRESCREEN_VOLUME_RATIO` | `2.0` | Pre-screen: min volume vs average (2×) to qualify. |
| `PROMOTION_TTL_HOURS` | `72.0` | How long a promoted symbol stays in Tier 1 (≤0 = never expire). |
| `BATCH_DOWNLOAD_SIZE` | `50` | Batch size for fetching data across large symbol sets. |

> The `get_tier1/2/3_symbols()` functions in `config/universe.py` preserve the
> **original sector-rotation** tier semantics for back-compat; the
> **index-based** tiers described here are implemented by
> `get_scan_pool_symbols()`, `get_index_universe_symbols()`, and the promotion
> helpers.

---

## 5. The Universe Browser dashboard tab

The **Universe** tab (nav link "Universe" → `#nav-universe`) is backed by
`dashboard/universe_router.py`; all endpoints are prefixed `/api/universe` and
require dashboard auth.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/universe/tiers` | GET | Tier breakdown cards (Tier 1/2/3 counts) + echoes `TIER2_SCAN_POOL_SIZE` and `PROMOTION_TTL_HOURS`. |
| `/api/universe/indices` | GET | S&P 500 / NASDAQ-100 membership with a per-symbol index badge, per-index counts, and union/overlap size. |
| `/api/universe/scan-pool` | GET | Tier-2 Scan Pool ranked by `volume × market cap`; `?limit=` defaults to `TIER2_SCAN_POOL_SIZE`. |
| `/api/universe/promotions` | GET | Currently promoted symbols with source tier, reason, promoted-at, and `ttl_remaining_hours`. |
| `/api/universe/promotions/{ticker}/demote` | POST | Manually demote a symbol out of Tier 1. |
| `/api/universe/symbols` · `/search` · `/sectors` · `/exchanges` · `/stats` | GET | Browse / search the full seeded universe. |
| `/api/universe/watchlists` (+ CRUD) | GET/POST/DELETE | Manage user watchlists (Tier 1 membership). |
| `/api/universe/filters` | GET/POST | Universe filter settings. |
| `/api/universe/seed` | POST | Trigger a background re-seed; returns a `job_id`. Body: `{"skip_enrichment": true}` (optional). |
| `/api/universe/seed/status/{job_id}` | GET | Poll seed progress. |

The tab shows:

- **Tier breakdown cards** — Tier 1 / Tier 2 / Tier 3 counts and the settings
  that drive them.
- **Index membership view** — the merged S&P 500 / NASDAQ-100 list with a badge
  showing which index (or both) each symbol belongs to.
- **Scan pool ranked table** — the Tier-2 pool ordered by liquidity, with the
  `volume × market cap` ranking metric shown.
- **Auto-promotions** — the live promoted-into-Tier-1 symbols with their TTL
  remaining and a **Demote** control to remove a promotion by hand.

Endpoints that need the DB return an "unavailable" payload when
`data_store/universe.db` is absent, prompting the operator to seed it.

---

## 6. Files at a glance

| Path | Role |
|------|------|
| `config/index_membership.py` | S&P 500 / NASDAQ-100 static lists + Wikipedia refresh. |
| `config/universe.py` | Tier resolution, scan pool, index universe, promotion helpers. |
| `data_store/universe_seeder.py` | Seeds the universe DB (EDGAR + Canada + ETFs + index membership). |
| `data_store/universe.py` | Universe DB access (`get_scan_pool_ranked`, `promote_symbol`, `expire_promotions`, `demote_symbol`, …). |
| `data_store/universe.db` | The seeded universe (gitignored; auto-created by the seeder). |
| `dashboard/universe_router.py` | `/api/universe/*` endpoints + Universe Browser data. |
| `engine.py` | `_run_tier2_scan`, `_run_tier3_scan`, `_promote_signals`, Tier 1 assembly. |
