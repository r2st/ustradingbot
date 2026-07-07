# Technical Analysis Charts & Live AI Commentary — Specification

**Status:** Proposed (not implemented) — 2026-07-07
**Scope:** Two dashboard features: per-trade technical-analysis charts (TA1) and a live AI commentary dashboard (TA2)
**Audience:** Anyone implementing dashboard/engine/AI work in this repo
**Date:** 2026-07-07

This document specifies two features for USTradingBot. Each feature lists
what it does, where the data comes from in the existing codebase, the API
surface, the frontend work, acceptance criteria, and a complexity estimate
(S = hours, M = 1–2 days, L = 3+ days). It follows the conventions
established in `docs/monitoring-features-spec.md` (F1–F9, implemented).

- **TA1 — Technical Analysis Charts Per Trade:** replace the generic
  performance charts (F7) as the primary per-trade view with full
  candlestick + indicator charts showing exactly what the system saw when it
  entered, plus a plain-English explanation panel.
- **TA2 — Live AI Commentary Dashboard:** a full-page section where an
  OpenRouter-backed commentator narrates open positions, watchlist setups,
  and market conditions every few minutes during market hours — a "live
  analyst" over data the system already computes.

Both features are read-only views. Neither changes any trading decision.

---

## 0. Existing infrastructure (what we build on)

The dashboard is a single Jinja2 page (`dashboard/templates/dashboard.html`,
vanilla JS, section-based layout) served by FastAPI (`dashboard/app.py`),
guarded by HTTP Basic auth (`dashboard/auth.require_auth`). Feature routers
live in `dashboard/*_router.py` and are mounted at the bottom of `app.py`.
Chart.js is vendored at `dashboard/static/chart.umd.min.js` (line/bar charts
only — no candlestick support without a plugin). The F9 rationale modal
already renders a **dependency-free SVG candlestick chart** with
entry/stop/target level lines (`dashboard.html`, `renderRationaleChart`
around line 3633) — TA1 extends this renderer rather than adding a library.

### 0.1 Indicator data already computed at signal time

`signals/combined_filter.score_symbol(symbol, strategy, df)` is the scoring
engine. On every scan of every symbol it computes five full indicator
states from ~6 months of daily OHLCV (`MIN_OHLCV_ROWS = 200`):

| Indicator | Module / function | Parameters | State fields available |
|---|---|---|---|
| RSI | `signals/rsi_signals.calculate_rsi` → `RSIState` | RSI(14), Wilder smoothing | `rsi_value`, `is_momentum_zone` (55–70), `is_swing_recovery`, `is_overbought` (>70), `is_overbought_rollover`, `has_bearish_divergence`, `is_above_midline`, `is_rising`, `rsi_5_bars_ago` |
| MACD | `signals/macd_signals.calculate_macd` → `MACDState` | 12/26/9 | `macd_line`, `signal_line`, `histogram`, `has_bullish_crossover`, `is_confirmed_bullish`, `has_histogram_flip`, `is_momentum_intact`, `has_bullish_divergence` (20-bar trough comparison) |
| EMA structure | `signals/ema_signals.calculate_ema` → `EMAState` | EMA 9 / 20 / 50 / 200 | stack ordering, `is_above_ema200`, `slope_quality` |
| Volume | `signals/volume_signals.calculate_volume` → `VolumeState` | 20-day avg volume, OBV 10-bar lookback, 20-bar rolling VWAP | `volume_ratio`, `has_bullish_surge` (≥1.5×), `has_bullish_dryup` (≤0.5×), `is_obv_confirming`, `has_obv_divergence`, `is_above_vwap`, `has_bearish_surge` |
| Ripster EMA clouds | `signals/ripster_cloud.calculate_ripster` → `RipsterState` | fast cloud EMA 8/9, slow cloud EMA 34/35 | cloud colors, price-vs-cloud positions, `fast_above_slow`, fresh cross, `price_below_both` |

Plus ATR(14) (`combined_filter._compute_atr`), which sets the stop:

- `stop = entry − ATR_STOP_MULTIPLIER (1.5) × ATR(14)`
- `target = entry + (entry − stop) × RISK_REWARD_MIN (1.8)`

Strategy modules add pattern-level structure: `signals/vcp_signal.py`
computes the **pivot high** (90-day high), consolidation depth, and ATR
contraction for VCP breakouts; `signals/multi_timeframe.weekly_confirms`
checks price vs a rising 30-week EMA (`WEEKLY_TREND_EMA_PERIOD`).

Hard vetoes (all in `combined_filter.py`): bear regime (below EMA-200), OBV
divergence, bearish volume surge, Ripster cross below both clouds, low ATR%
(`MIN_ATR_PCT = 1.5%`).

**What is persisted today vs. discarded.** The `Signal` dataclass
(`signals/signal_types.py`) keeps only scalar summaries: `rsi_value`,
`rsi_score`, `macd_histogram`, `macd_score`, `ema_score`, `volume_ratio`,
`volume_score`, `ripster_score`, `obv_confirming`, plus entry/stop/target
and grade. The full indicator **series** (EMA lines, RSI curve, MACD lines,
OBV, cloud boundaries) and most state booleans (crossover flags, divergence
flags, VWAP position) are computed and then thrown away. **The single
biggest gap for TA1: indicator series are recomputed every scan but never
captured.** They must either be snapshotted at entry (preferred for
history) or recomputed on demand from cached bars (fine for open positions
and backfill).

### 0.2 Trade rationale (F9) — the persistence pattern to extend

`journal/rationale.py` already persists, per placed trade, to
`DATA_DIR/trade_rationale.jsonl` (size-rotated at 20 MB):

- 10 scored criteria (0–10 each with one-line explanations): setup grade,
  breakout pattern, volume confirmation, trend alignment, MACD momentum,
  RSI positioning, risk/reward, AI veto, regime fit, OBV confirmation
  (`build_trade_rationale`).
- A 90-bar daily OHLCV snapshot (`snapshot_bars`, served from the fetch
  cache so it is normally free).
- Entry/stop/target, quantity, grade, strategy, timestamps.

Served by `GET /api/rationale?symbol=&entry_time=&limit=`
(`dashboard/rationale_router.py`, `find_rationale` picks the record nearest
the entry time). TA1's snapshot is a **v2 of this record** — same file,
same store, new optional keys — so old records keep working.

### 0.3 The 9-gate entry pipeline (for gate-by-gate reasoning)

`engine.Engine._process_signal` (engine.py:534) rejects at, in order:
`pre_check` → `strategy_cap` → `pending_order_guard` → `ai_veto` →
`news_sentiment` → `regime_autotune` → `build_order` → `freshness_check` →
`cash_check`. Every rejection is logged with gate + human detail via
`journal/btst_logger.RejectedSignalLogger` (JSONL) and mirrored into the
activity feed. `GET /api/watchlist/monitor`
(`dashboard/watchlist_router.py:110`) already joins watchlist symbols
against the last scan's signals and the last 2 days of rejections, yielding
per-symbol status: `held` / `signal` / `near_entry` / `rejected` /
`excluded` / `idle`, with the last rejection's gate and detail. TA2's
watchlist panel builds directly on this payload.

### 0.4 Market data, quotes, regime, AI

- **Bars:** `data/fetcher.fetch_ohlcv(symbol, period="6mo")` — daily bars,
  TTL-cached, retried, over `data/providers.py` (yfinance / Alpaca /
  Polygon behind a `FallbackProvider`). Polygon free tier is 5 req/min;
  never fetch per-request from a router — always go through the cache.
- **Quotes:** `dashboard/quotes.get_quote/get_quotes` — shared 15 s-TTL
  quote service (`QUOTE_CACHE_TTL_SECONDS`), built for F1.
- **Live P&L:** `GET /api/live/pnl` (`dashboard/live_router.py`) already
  computes per-position current price, unrealized P&L, and effective
  stop/target levels (including multi-level exit ladders via
  `_effective_levels`).
- **Regime:** `analytics/regime.current_regime` classifies SPY (config:
  `REGIME_BENCHMARK`) into bull/bear/sideways via 50/200 SMA plus a
  volatility flag from 20-day realized vol (high ≥ 1.8 %/day,
  `REGIME_HIGH_VOL_PCT`), and emits per-strategy-family weight multipliers.
  Exposed at `GET /api/regime` (`dashboard/insights_router.py`). Note:
  **there is no VIX, QQQ, or sector data today** — the "VIX regime" in TA2
  is either the existing realized-vol proxy or a new `^VIX` fetch (yfinance
  supports it through the same `fetch_ohlcv` path).
- **AI:** `ai/analyst.AIAnalyst` is the existing OpenRouter integration —
  model `OPENROUTER_MODEL` (default `openai/gpt-oss-20b:free`, per the
  project rule that AI calls use OpenRouter free models), key from
  `OPENROUTER_API_KEY` env, `httpx` async client, strict-JSON prompting
  with tolerant parsing (`_parse_verdict`), per-symbol file cache
  (`ai/cache.AICache`, TTL `AI_CACHE_TTL_HOURS = 4`), cost estimation.
  The veto is fail-**closed**; TA2 commentary must be fail-**open** (see
  §2.6) because it is display-only.

### 0.5 Cross-cutting design decisions (carried over from F1–F9)

- **Polling, not websockets.** All new endpoints are polled JSON.
- **Engine → dashboard stays file-based** (append-only JSON/JSONL under
  `settings.DATA_DIR`, best-effort writes that never break the trade loop).
- **Auth:** every new endpoint uses `Depends(require_auth)`.
- **New routers, not more `app.py`.**
- **Server computes, client renders.** Indicator math lives in Python next
  to the code that already does it (`signals/`), never re-implemented in JS.

---

## 1. TA1 — Technical Analysis Charts Per Trade

### 1.1 What & why

For every open position and every historical trade, show the chart the
system effectively "looked at": a candlestick chart with the exact
indicators that drove the entry overlaid, plus entry/stop/target levels and
a plain-English explanation of why the trade was taken. This replaces the
generic performance charts (F7's equity/returns/drawdown stay, but the
per-trade view becomes the centerpiece) and upgrades the F9 rationale modal
from a bare candlestick into a full TA workstation view.

User value: today you can see *that* a trade scored 0.81 and *that* the
stop is \$142.30; after TA1 you can see the 20 EMA it bounced off, the
volume surge bar, the RSI at entry, and read "stop is 1.5× ATR(14) below
entry, under the consolidation low" — the difference between trusting the
bot and auditing it.

### 1.2 Chart contents

One main pane plus three subplots, all rendered from a single JSON payload:

**Main pane (candlestick, ~120 daily bars):**

- OHLC candles (extend the existing F9 SVG renderer).
- Moving averages: EMA 9, EMA 20, EMA 50, EMA 200 — the exact EMAs from
  `ema_signals` (note: the system uses EMA 20, not SMA 50; the chart shows
  what the system actually uses).
- Ripster clouds: fast cloud (EMA 8/9 band) and slow cloud (EMA 34/35
  band), shaded green/red by cloud color.
- Bollinger Bands (20, 2σ) and Keltner Channels (20, 1.5 × ATR(10)) — **new
  computation** (§1.4); rendered as toggleable bands, with squeeze bars
  (BB inside KC) marked along the bottom axis.
- Support/resistance levels — **new computation** (§1.4); horizontal
  segments with strength (touch count).
- Entry, stop, and target: horizontal lines (reuse F9's level-line
  rendering), plus an entry marker on the entry bar. For open positions
  with exit ladders, all effective levels from
  `live_router._effective_levels`.
- Pattern annotation: a label on the pattern region, named per strategy
  from `journal/rationale._STRATEGY_PATTERN` ("VCP volatility-contraction
  breakout", "momentum trend-continuation breakout", "oversold pullback in
  an uptrend", "extreme-oversold mean-reversion setup", "post-earnings
  announcement drift"). For VCP, the pivot-high line and consolidation box
  from `vcp_signal`'s `pivot_high` / consolidation window.

**Subplot 1 — Volume:** volume bars colored by up/down day, 20-day average
volume line, surge bars (≥1.5×) highlighted, plus the OBV line on a
secondary scale (OBV series recomputed in the snapshot module — the
existing `volume_signals` internals compute it already).

**Subplot 2 — RSI(14):** RSI curve, 30/50/70 guide lines, shaded
overbought/oversold zones, and a dot + value label on the entry bar
("RSI 61 at entry" — from `Signal.rsi_value`, already persisted).

**Subplot 3 — MACD(12,26,9):** MACD line, signal line, histogram bars;
crossover markers where `macd_line` crosses `signal_line`; entry bar
highlighted.

### 1.3 Explanation panel

A text panel beside (desktop) or below (mobile) the chart. **Generated
deterministically from data — no LLM call.** Every sentence is a template
filled from persisted values, so it is exact, free, and never hallucinates:

- **Entry triggers:** rendered from the F9 criteria list plus the snapshot
  state booleans, e.g. "Entry signal: combined score 0.81 (grade A).
  MACD bullish crossover confirmed above zero; RSI 61 in the momentum zone
  (55–70); volume 2.1× its 20-day average; price above all four EMAs;
  both Ripster clouds green."
- **Stop derivation:** always ATR-based in this system — "Stop \$142.30 =
  entry \$148.60 − 1.5 × ATR(14) \$4.20 (settings `ATR_STOP_MULTIPLIER`)."
  Shown with the actual ATR value from the snapshot.
- **Target derivation:** "Target \$159.94 = entry + 1.8 × risk per share
  (`RISK_REWARD_MIN`), R:R 1.8 : 1." If a resistance level from §1.4 sits
  between entry and target, note it: "nearest resistance \$155.20 is
  before the target."
- **Setup type:** the signal **grade (A/B/C/F** — thresholds 0.78 / 0.65 /
  0.38 from `signal_types.Grade`; note the system grades A/B/C/F, there is
  no "D") **plus the strategy pattern name** — e.g. "Grade A
  momentum trend-continuation breakout." Grade meaning is spelled out
  (A = full size, B = 75 % size).
- **Open positions only — current readings:** live RSI/MACD/EMA-distance
  recomputed from today's cached bars, plus the F1 live numbers: "Now:
  price \$151.20 (+1.7 %), RSI 58, MACD histogram still positive, 2.1 %
  above the 20 EMA. 0.62R gained; 5.9 % from stop, 5.8 % from target."
- **AI veto note:** the persisted `ai_decision`/`ai_reasoning` from the
  trade journal (one line).

### 1.4 Data sources & new computation

| Need | Source | Status |
|---|---|---|
| OHLCV bars | `data/fetcher.fetch_ohlcv` (open/backfill); `bars` snapshot in `trade_rationale.jsonl` (history) | exists |
| EMA / RSI / MACD / volume / Ripster series | same math as `signals/*` modules, exposed as series | **new module** `signals/indicator_snapshot.py` |
| ATR(14) | `combined_filter._compute_atr` | exists (promote to the snapshot module) |
| Bollinger / Keltner + squeeze | — | **new**, in `signals/indicator_snapshot.py` (BB 20/2σ, KC 20/1.5×ATR(10), squeeze = BB inside KC) |
| Support/resistance | — | **new module** `signals/support_resistance.py`: swing-high/low pivots (fractal, 2-bar wings) clustered within 0.5 × ATR, strength = touch count, top 3 above + below entry. (Do not name it `levels.py` — `execution/levels.py` is exit ladders.) |
| Entry/stop/target, grade, scores | `trade_rationale.jsonl` + `data_store/trades.csv` + `open_positions.json` | exists |
| Pattern name / VCP pivot | `journal/rationale._STRATEGY_PATTERN`; `vcp_signal` internals (expose `pivot_high`, consolidation window in `Signal.raw_data`) | exists / small change |
| Live readings (open positions) | `dashboard/quotes` + snapshot module over cached bars | exists + new |

**Snapshot capture (engine side).** Extend `Engine._build_rationale` /
`journal/rationale.py` with an `indicators` key computed by the new
snapshot module from the same `df` the scorer used: per-bar series aligned
to the existing 90-bar `bars` array (`ema9/20/50/200`, `rip_hi/lo` fast and
slow, `bb_up/mid/lo`, `kc_up/lo`, `squeeze`, `rsi`, `macd/signal/hist`,
`obv`, `vol_avg20`), plus scalars (`atr14`, S/R levels, state booleans,
pattern metadata). Rounded to 4 dp; ~25–40 KB per trade — acceptable
against the 20 MB rotation, but raise `MAX_RATIONALE_BYTES` to 50 MB.
Best-effort like everything else in the rationale path: a snapshot failure
must never block an entry.

**Backfill / fallback (dashboard side).** For trades that predate TA1 (and
for all open positions' *current* view), the endpoint recomputes the same
payload on demand from `fetch_ohlcv` — identical code path, `source:
"recomputed"` in the response so the UI can caption it "reconstructed from
current data" (bars may differ slightly from entry-time data after splits).

### 1.5 API

- `GET /api/ta/trade-chart?symbol=AAPL&entry_time=2026-07-01T10:30:00`
  (auth: `require_auth`) → one self-contained payload:

  ```json
  {
    "symbol": "AAPL", "source": "snapshot|recomputed",
    "trade": {"entry_price": 148.6, "stop_price": 142.3, "target_price": 159.94,
               "quantity": 33, "grade": "A", "strategy": "momentum",
               "entry_time": "...", "exit_time": null, "levels": [ ... ]},
    "bars": [{"t": "2026-03-02", "o": 0, "h": 0, "l": 0, "c": 0, "v": 0}, ...],
    "series": {"ema9": [...], "ema20": [...], "ema50": [...], "ema200": [...],
                "bb_up": [...], "bb_mid": [...], "bb_lo": [...],
                "kc_up": [...], "kc_lo": [...], "squeeze": [...],
                "rip_fast_hi": [...], "rip_fast_lo": [...],
                "rip_slow_hi": [...], "rip_slow_lo": [...],
                "rsi": [...], "macd": [...], "macd_signal": [...],
                "macd_hist": [...], "obv": [...], "vol_avg20": [...]},
    "levels": {"support": [{"price": 141.8, "touches": 4}],
                "resistance": [{"price": 155.2, "touches": 3}]},
    "annotations": {"pattern": "momentum trend-continuation breakout",
                     "entry_bar": 87, "atr14": 4.2, "squeeze_recent": false},
    "explanation": {"entry": "...", "stop": "...", "target": "...",
                     "setup": "...", "current": "... (open positions only)",
                     "ai_note": "..."}
  }
  ```

- `GET /api/ta/live-readings?symbols=AAPL,MSFT` — lightweight current
  indicator scalars for the open-positions explanation refresh (and reused
  by TA2), served from a per-symbol per-day cache.
- New router `dashboard/ta_router.py`, mounted like the others.

### 1.6 Frontend

- **Renderer:** extend the F9 dependency-free SVG candlestick renderer
  (`dashboard.html` ~line 3633) into a reusable `renderTAChart(el, payload)`
  with the three subplots sharing the x-axis. No new JS dependency —
  Chart.js cannot do candlesticks without a plugin, and the SVG renderer
  already handles candles + level lines. Line/band overlays are simple SVG
  paths/polygons. Crosshair + tooltip (bar values on hover) is the only
  interactive element in v1; zoom/pan is out of scope.
- **Placement:**
  - Open-positions section: each position row gets an expandable chart
    panel (accordion). Chart + explanation load **only on first expand**
    (lazy).
  - Trade-history section: same expandable row pattern; replaces the "Why
    this trade?" modal as the primary detail view (the modal remains as
    the compact fallback).
  - Layout: chart ≈ 65 % width, explanation panel ≈ 35 % on desktop;
    stacked on mobile (the dashboard already has responsive section CSS,
    see `test_dashboard_responsive.py`).
- Indicator toggles (checkboxes: EMAs / clouds / BB / KC / S&R) persisted
  in `localStorage`.

### 1.7 Performance considerations

This feature is computationally heavier than anything on the dashboard
today; it must be aggressively lazy:

- **Lazy loading:** nothing is computed or fetched until a row is expanded.
  Never render all charts on page load.
- **Server-side caching:** recomputed payloads cached per
  `(symbol, entry_time)` — for closed trades the payload is immutable, so
  cache-forever in a small on-disk cache (`DATA_DIR/ta_cache/`); for open
  positions, TTL = quote-cache-aligned 60 s for the `current` explanation
  block and one trading day for the bar/series block (daily bars only
  change once per day).
- **Snapshot-first:** history reads come from `trade_rationale.jsonl`
  (already on disk, no provider calls at all).
- **Bounded payloads:** 120 bars max, values rounded to 4 dp
  (~60–90 KB JSON, gzip-served by uvicorn).
- **Provider budget:** all recomputation goes through the existing
  `fetch_ohlcv` TTL cache; an expanded chart for a symbol the engine
  scanned this hour costs zero provider calls.

### 1.8 Acceptance criteria

1. Expanding any open position shows candles, EMA 9/20/50/200, Ripster
   clouds, BB + KC with squeeze markers, volume + OBV subplot, RSI subplot
   with entry-RSI marker, MACD subplot with crossover markers, S/R levels,
   and entry/stop/target lines — all from one request.
2. Expanding any historical trade placed after TA1 ships renders from the
   persisted snapshot (`source: "snapshot"`), byte-identical across
   reloads, with zero provider calls.
3. Historical trades from before TA1 render via recompute with a visible
   "reconstructed" caption.
4. The explanation panel states entry triggers, the stop formula with the
   actual ATR value, the target formula with the R:R, and the grade +
   pattern name; for open positions it also shows current readings and
   R-progress, refreshing with the F1 poll cycle.
5. All values in the explanation panel match the chart and
   `trade_rationale.jsonl` exactly (no re-derived numbers drifting apart).
6. Engine entry latency is unchanged (snapshot capture is best-effort and
   uses the already-fetched df); a snapshot failure still places the trade.
7. Dashboard initial page load time is unchanged (no eager chart work).
8. Unit tests: snapshot module series match `signals/*` scalar states on
   fixture data; S/R detector on a synthetic series; router returns
   snapshot vs recompute correctly; explanation templates render for all
   five strategies.

### 1.9 Complexity: **L**

Snapshot module + S/R detection + rationale v2 (M) and the SVG multi-pane
renderer + explanation templates (M) are independently medium; together
with backfill, caching, and tests this is a solid L (3–5 days). Suggested
split: ship engine-side snapshot capture first (history starts
accumulating immediately), then the renderer, then the explanation panel.

---

## 2. TA2 — Live AI Commentary Dashboard

### 2.1 What & why

A full-page dashboard section ("Analyst") that reads like a live market
analyst covering your book: per-position technical commentary with risk
math and suggested actions, per-watchlist-symbol setup summaries with
entry-readiness, and a market overview (SPY/QQQ trend, volatility regime,
sector snapshot) — refreshed every ~5 minutes during market hours.

User value: the dashboard currently answers "what happened"; TA2 answers
"what does it mean and what should I watch next," in prose, without the
user reading nine indicators themselves.

**Design principle — the numbers never come from the model.** Every fact
(prices, indicator readings, distances, R-multiples, gate results, scores)
is computed locally by existing code + the TA1 snapshot module and passed
to the LLM as structured context; the LLM only turns facts into prose. The
UI renders the computed numbers from the payload, not from the prose, so a
hallucinated figure can never be displayed as data. A deterministic
template fallback renders the same facts as terse bullet prose when the
LLM is unavailable — the page degrades, never empties.

### 2.2 Open Positions Panel

Per open position, a card with:

- **Computed header (deterministic, no LLM):** symbol, live price and day
  change (quote service), unrealized P&L and R-progress
  ("0.45R gained"), distance to stop and target in % ("2.3 % from stop,
  5.1 % from target") — all already computed by `GET /api/live/pnl`
  (`_position_row`, `_effective_levels`).
- **Sentiment chip (deterministic):** bullish / bearish / neutral with a
  confidence 0–1, derived by scoring the current indicator states with the
  existing `bullish_score` functions (reuse `combined_filter` scoring on
  fresh bars) — not asked of the LLM, so it is consistent scan-to-scan.
- **AI commentary (LLM):** 2–4 sentences narrating the current technicals,
  e.g. "Price is testing the 50 EMA at \$375 from above. RSI 62 and
  rising; volume 1.4× average suggests conviction; the MACD histogram is
  expanding. The fast Ripster cloud remains green." Input facts: TA1
  `/api/ta/live-readings` scalars + position row + nearest S/R levels.
- **Suggested action (LLM, advisory):** one sentence from a constrained
  set of themes (hold / tighten stop toward breakeven / watch for reversal
  near target / setup weakening) — clearly labeled "commentary, not
  advice"; the bot's actual exits remain 100 % rule-based and unaffected.

### 2.3 Watchlist Analysis Panel

Per symbol from `GET /api/watchlist/monitor` (§0.3), a card with:

- **Status + gate history (deterministic):** the existing
  `held/signal/near_entry/rejected/excluded/idle` status, and for
  rejected symbols the failing gate with its human detail ("rejected at
  `ai_veto`: earnings within 14 days"), straight from the monitor payload.
  For the scoring-level view, the last scan's per-indicator scores when a
  signal exists.
- **Entry-readiness score 0–100 (deterministic):** the combined weighted
  score (× 100) from the most recent scan when available; symbols that
  produced a `signal` show grade + proposed entry/stop/target; `near_entry`
  symbols get a "setup exists, price drifted" badge. No new scanning —
  readiness is read from what the engine already computed last cycle.
- **Key levels (deterministic):** top support/resistance from the TA1 S/R
  module + distance from current price.
- **AI setup summary (LLM):** 1–3 sentences, e.g. "AAPL is consolidating
  in a squeeze near \$198 — Bollinger Bands inside the Keltner Channel
  with contracting volume. RSI 55, neutral. A push above \$200 resistance
  on ≥1.5× volume would satisfy the volume gate."

### 2.4 Market Overview panel

- **SPY:** the existing `RegimeResult` verbatim — regime (bull / bear /
  sideways), trend, 50/200 MA values, realized vol, volatility flag, and
  strategy-family weight multipliers (`GET /api/regime`).
- **QQQ:** run `analytics/regime.detect_regime` on QQQ bars too (pure
  function, one extra cached fetch).
- **Volatility regime:** fetch `^VIX` daily close via `fetch_ohlcv` and
  bucket: `< 15` low / `15–20` normal / `20–30` elevated / `> 30` crisis;
  if `^VIX` is unavailable from the active provider, fall back to the
  existing realized-vol flag (labeled as such).
- **Sector snapshot (deterministic):** 1-day and 5-day % change for the 11
  SPDR sector ETFs (XLK, XLF, XLE, XLV, XLY, XLP, XLI, XLB, XLRE, XLU,
  XLC) via the batch quote service — rendered as a compact heat strip.
  11 extra symbols once per refresh cycle is within budget because they
  ride the shared quote cache.
- **AI market summary (LLM):** 2–3 sentences synthesizing the above:
  "Market is in a bullish regime with low volatility (VIX 13.8). Breadth
  favors tech and discretionary; energy lagging. Conditions favor long
  momentum setups — momentum family weight ×1.25."

### 2.5 AI Commentary Engine

New module `ai/commentator.py`, deliberately parallel to `ai/analyst.py`
(same OpenRouter call shape, headers, tolerant JSON parsing) but a separate
class — the veto path must stay untouched.

- **Model:** OpenRouter free models per project rules. New settings:

  | Setting | Default |
  |---|---|
  | `AI_COMMENTARY_ENABLED` | `False` (opt-in) |
  | `AI_COMMENTARY_MODEL` | `openai/gpt-oss-20b:free` |
  | `AI_COMMENTARY_INTERVAL_MINUTES` | `5` |
  | `AI_COMMENTARY_CACHE_TTL_MINUTES` | `5` |
  | `AI_COMMENTARY_MAX_CALLS_PER_DAY` | `150` |

- **Batched prompting — one call per panel, not per symbol.** Each refresh
  makes at most 3 LLM calls: (1) all open positions in one prompt →
  strict-JSON array of `{symbol, commentary, action}`; (2) top-N watchlist
  symbols (default 10, ranked by monitor status: signal > near_entry >
  rejected) in one prompt; (3) market overview. At the 5-minute cadence
  over a 6.5 h session that is ≤ 234 calls/day worst case, throttled by
  the daily budget counter (`AI_COMMENTARY_MAX_CALLS_PER_DAY`) — OpenRouter
  free-tier daily request caps are real and shared with the veto, which
  must always win; when the budget is exhausted, fall back to templates.
- **Refresh loop:** an `asyncio` background task in the **dashboard**
  process (started from `app.py` lifespan, like the intraday P&L sampler
  pattern) — not the engine; commentary must never compete with the trade
  loop. It runs only during regular market hours (reuse the engine's ET
  market-hours check), refreshes every `AI_COMMENTARY_INTERVAL_MINUTES`,
  and skips entirely when no client has polled the panel in the last 15
  minutes (no browser open → no API spend).
- **Caching & persistence:** latest commentary written to
  `DATA_DIR/commentary.json` (whole-payload, atomic replace) with
  `generated_at`, per-section `source: "llm" | "template" | "stale"`, and
  the input facts. Endpoints serve the file; a dashboard restart shows the
  last commentary marked stale rather than a blank page. Input-hash
  short-circuit: if a section's facts are unchanged since the last
  generation (e.g. market closed), skip the LLM call and bump the
  timestamp.
- **Fallback (fail-open):** on missing key, HTTP error, timeout, parse
  failure, or budget exhaustion, render the deterministic template
  commentary from the same facts ("RSI 62 rising; volume 1.4× avg; MACD
  histogram positive and expanding") and set `source: "template"`. Errors
  are logged, never surfaced as broken UI. This is the opposite of the
  veto's fail-closed rule, and correct: commentary influences no order.

### 2.6 API

New router `dashboard/commentary_router.py` (all `require_auth`):

- `GET /api/commentary` → the whole `commentary.json` payload:
  `{generated_at, market_open, budget: {used, max}, positions: [...],
  watchlist: [...], market: {...}}` — one poll drives the whole page.
- `POST /api/commentary/refresh` → force a refresh outside the timer
  (still budget-checked); returns 429 when budget-exhausted.
- `GET /api/commentary/status` → engine-style health: last run, last
  error, calls used today, model.

### 2.7 Frontend

- New top-level section/page "Analyst" in `dashboard.html` (the page
  already uses section-based navigation), polling `GET /api/commentary`
  every 60 s (the server refreshes at its own 5-min cadence; client
  polling just picks up the file).
- **Layout (rich, but consistent with the existing dashboard idiom):**
  - Top band: market overview — regime badge (color-coded bull/bear/
    sideways), VIX-regime chip, SPY/QQQ mini sparklines (Chart.js — line
    charts, no candles needed here), sector heat strip, AI market summary.
  - Left column: open-position cards (computed header + sentiment chip +
    commentary + action line), each with a "view chart" link that jumps to
    the TA1 chart for that position.
  - Right column: watchlist cards ordered by readiness, each showing the
    readiness meter (0–100 bar), status/gate line, key levels, and the AI
    setup summary.
  - Every AI-sourced block carries a small "AI commentary — not financial
    advice" caption and a `source` badge (llm/template/stale) with the
    generation timestamp.

### 2.8 Performance considerations

- LLM cost: ≤ 3 calls per 5-minute cycle, free-model pricing, hard daily
  budget, idle-browser suppression, input-hash skip when facts unchanged.
- Provider load: all facts come from existing caches (quotes 15 s TTL,
  bars daily TTL, monitor payload, live P&L); the only net-new symbols are
  QQQ, ^VIX, and 11 sector ETFs — batched through `fetch_multiple` /
  `get_quotes`, well inside Polygon's 5 req/min after caching.
- Dashboard load: the panel is one cached-file read per poll; commentary
  generation is fully async and never blocks a request handler.
- Payload: cap watchlist commentary at 10 symbols per cycle (configurable);
  remaining symbols show deterministic data only.

### 2.9 Acceptance criteria

1. With `AI_COMMENTARY_ENABLED=true` and a valid key, the Analyst page
   shows, within one interval of market open: per-position commentary with
   correct live risk math (matches `/api/live/pnl` to the cent),
   watchlist cards with readiness scores and last-scan gate results
   matching `/api/watchlist/monitor`, and a market overview whose regime
   values match `/api/regime`.
2. All displayed numbers are rendered from computed payload fields; prose
   is visually distinguished as AI commentary with source + timestamp.
3. Kill the API key (or exhaust the budget): the page renders template
   commentary for every section, badged `template`, with zero errors in
   the UI and no unhandled exceptions in logs.
4. No LLM or provider calls occur when the market is closed (beyond one
   startup check) or when no client has polled recently.
5. The engine process is untouched: trading latency, veto behavior, and
   AI-veto cache are identical with the feature on or off.
6. Daily OpenRouter usage never exceeds `AI_COMMENTARY_MAX_CALLS_PER_DAY`
   (verified by the status endpoint's counter).
7. Unit tests: commentator prompt-building from fixture facts, strict-JSON
   parsing with malformed model output → template fallback, budget
   counter, market-hours gating, sentiment derivation from indicator
   states, VIX bucketing.

### 2.10 Complexity: **L** (M if TA1 lands first)

The commentator + refresh loop + fallback machinery is M; the three-panel
frontend is M; the facts layer is mostly free **if TA1's
`indicator_snapshot` module and `/api/ta/live-readings` exist** — build
TA1 first and TA2 drops to M/L.

---

## 3. Suggested build order

1. **TA1 engine-side snapshot** (`signals/indicator_snapshot.py`,
   `signals/support_resistance.py`, rationale record v2) — start
   accumulating history immediately; smallest risk, no UI.
2. **TA1 API + chart renderer** (`dashboard/ta_router.py`, SVG multi-pane
   chart, lazy expand).
3. **TA1 explanation panel** (templates + live readings endpoint).
4. **TA2 facts layer + deterministic page** (panels rendering computed
   data with template prose — the page is already useful with zero LLM).
5. **TA2 commentator** (OpenRouter calls, budget, fallback wiring).

Both features are additive: no existing endpoint changes shape, the F9
modal keeps working against v1 rationale records, and F7's portfolio-level
charts remain until TA1's per-trade view has fully replaced the per-trade
chart needs.
