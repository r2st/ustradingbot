# Short Selling Strategy Module — Specification

Status: implemented (see `short_strategies/`)
Scope: signal generation + shared short-side risk filters, integrated with the
existing engine, risk manager, and paper broker.

## 1. Goal

Add a modular short-selling capability to USTradingBot. Eleven strategies are
implemented as independent signal generators that plug into the existing
execution and risk layers. Short signals flow through the *same* entry
pipeline as long signals (risk pre-check → strategy cap → AI veto → sizing →
freshness → cash → bracket order), with direction-aware validation at every
gate.

## 2. Architecture

`short_strategies/` is an independent signal-provider module:

```
short_strategies/
├── __init__.py            # public API: run_short_scan, get_short_config
├── scanner.py             # orchestrator — mirrors signals/screener.py
├── common/
│   ├── config.py          # all tunable parameters (dataclasses, documented defaults)
│   ├── indicators.py      # ATR, RSI, EMA/SMA, ADX(+DI/−DI), rolling VWAP, trend_state
│   └── signal.py          # ShortSignal dataclass + conversion to core Signal
├── strategies/            # one file per strategy, uniform detect() API
├── risk/
│   ├── filters.py         # shared pre-entry filters (section 7)
│   └── sizing.py          # ATR stop placement + ATR position sizing
├── backtests/
│   └── runner.py          # daily-bar walk-forward harness per strategy
└── tests/                 # unit tests (collected by pytest)
```

### 2.1 Signal object

Every strategy emits a standardized `ShortSignal`:

| field            | type      | meaning                                        |
|------------------|-----------|------------------------------------------------|
| `strategy_id`    | str       | e.g. `"short_support_breakdown"`               |
| `symbol`         | str       | ticker                                         |
| `timestamp`      | datetime  | signal generation time                         |
| `side`           | str       | always `"SHORT"`                               |
| `signal_strength`| float     | 0.0–1.0 composite quality score                |
| `trigger_price`  | float     | proposed short-entry price (last close)        |
| `stop_price`     | float     | buy-stop above entry (ATR-based)               |
| `target_price`   | float     | cover target below entry                       |
| `filters_passed` | list[str] | names of shared filters that approved it       |
| `metadata`       | dict      | strategy-specific diagnostics                  |

`ShortSignal.to_core_signal()` converts to the pipeline-native
`signals.signal_types.Signal` with `direction="short"`; grade derives from
`signal_strength` via the existing thresholds.

### 2.2 Detector API

Each strategy module exposes:

```python
def detect(symbol: str, df: pd.DataFrame, config=None, ctx=None) -> ShortSignal | None
```

`df` is a daily OHLCV frame (same shape the long screener uses). `ctx` is an
optional `MarketContext` (benchmark frame, cross-sectional returns, sector
map) required only by the relative-weakness strategies. Detectors are pure,
never raise, and are independently testable.

## 3. Trend-following strategies

### 3.1 Support Breakdown with Volume Confirmation (`short_support_breakdown`)
Short when the daily close breaks below a clustered support level (pivot
clustering reused from `signals.support_resistance`) on above-average volume.
Parameters: `break_pct` (close ≥ this % below support), `volume_ratio_min`
(today / 20-day avg), `min_touches` (level significance).

### 3.2 Moving Average Crossunder (`short_ma_crossunder`)
Fast EMA crosses below slow SMA within the last `cross_within_bars` bars and
price closes below both. The underlying `trend_state()` utility (up / down /
flat from the same MA pair) lives in `common/indicators.py` and is reused by
other strategies (VWAP rejection requires an established downtrend).

### 3.3 Bear Flag Continuation (`short_bear_flag`)
Flagpole: decline ≥ `pole_min_drop_pct` within ≤ `pole_max_bars`. Flag:
`flag_min_bars`–`flag_max_bars` of drifting-up/sideways consolidation whose
range contracts and which retraces < `flag_max_retrace` of the pole. Entry on
close below the flag low.

### 3.4 ADX Trend-Strength Confirmation (`short_adx_filter`)
Wilder ADX(14) with −DI > +DI and ADX ≥ `adx_min` (default 20). Used two
ways: as a confirmation *filter* the scanner applies to the other
trend-following strategies (configurable), and as a standalone weak-trend
short when combined with a close below the slow MA.

## 4. Relative-weakness strategies

### 4.1 Sector/Peer Relative Strength Ranking (`short_relative_weakness`)
Rank every scanned symbol by trailing `rank_lookback_days` return within its
sector (`config.universe.SECTOR_BY_SYMBOL`; falls back to the whole scan
universe when a sector has < `min_peers` members). Short candidates in the
bottom `bottom_decile` fraction that are also below their 50-day MA.

### 4.2 Laggard Fade on Market Pullback Days (`short_laggard_fade`)
On days the benchmark (SPY) falls ≥ `market_down_pct`, short the symbols
whose day return underperforms the benchmark by ≥ `underperform_pct` and that
close in the bottom of their day range.

## 5. Reversal / exhaustion strategies

### 5.1 Overbought Momentum Fade (`short_overbought_fade`)
RSI(14) ≥ `rsi_min` (default 75) plus a rejection candle (upper wick ≥
`wick_body_ratio` × body, close in lower half of range) at/near a clustered
resistance level.

### 5.2 Failed Gap-Up / "Gap and Crap" (`short_gap_fail`)
Open gaps up ≥ `gap_min_pct` over the prior close, then the day closes below
the open (and optionally below the prior close with `require_below_prior_close`),
i.e. the opening range failed to hold.

### 5.3 Post-Earnings Pop Fade (`short_earnings_pop_fade`)
An earnings gap-up within the last `days_after_earnings` days that lacks
follow-through: the pop day (or the day after) closes below its open and
gives back ≥ `fade_min_pct` of the gap. Earnings dates come from
`data.earnings_calendar` (injectable for tests).

### 5.4 VWAP Rejection Short (`short_vwap_rejection`)
In an established downtrend (`trend_state == "down"`), a bounce whose high
tags the rolling `vwap_window`-day VWAP but closes back below it by
≥ `reject_close_pct`. Note: with daily bars the VWAP is a rolling
typical-price×volume approximation, not intraday VWAP — documented limitation.

### 5.5 Buying Climax / Distribution Day Reversal (`short_buying_climax`)
After an advance ≥ `advance_min_pct` over `advance_lookback` days, a climax
day (volume ≥ `climax_volume_ratio` × 20-day avg, wide range, new high)
followed by a reversal day that closes below the climax bar's midpoint.

## 6. Priority & scoring

The scanner tries strategies per symbol in priority order (first qualifying
signal wins, mirroring the long screener):

```
gap_fail → earnings_pop_fade → support_breakdown → bear_flag → buying_climax
→ overbought_fade → vwap_rejection → ma_crossunder → relative_weakness → laggard_fade
```

Event-driven setups outrank slower trend setups because their edge decays
fastest. `short_adx_filter` participates as a confirmation filter rather than
in the priority list (standalone mode is off by default). Each detector maps
its evidence to `signal_strength` ∈ [0, 1]; the existing Grade thresholds
(A ≥ 0.78, B ≥ 0.65) apply downstream unchanged.

## 7. Shared risk / execution filters

Applied by the scanner to every candidate, in order (all fail-closed unless
noted; each records its name in `filters_passed`):

1. **Borrow/Locate Check** — pluggable `BorrowProvider`; the default
   `StaticBorrowProvider` allows the scan universe minus an optional
   deny-list file (`data_store/hard_to_borrow.json`). Real locate feeds can
   implement the same protocol later.
2. **Short Interest / Float Filter** — reject when short % of float exceeds
   `max_short_pct_float` (squeeze risk) via an injectable lookup (yfinance
   `shortPercentOfFloat` by default). *Fail-open* when data is missing
   (configurable), because free data coverage is patchy.
3. **Market Regime Filter** — reuses `analytics.regime` SPY trend detection;
   shorts allowed only when the benchmark is **not** in a bull regime
   (i.e. below-trend or mixed). Configurable to require a strict bear regime.
4. **ATR-based stop placement** — stop = entry + `stop_atr_mult` × ATR(14)
   (buy-stop above entry); target = entry − `target_rr` × risk. Signals whose
   detector produced a tighter structural stop keep it only if within the ATR
   envelope.
5. **ATR-based position sizing** — 0.5–1 % account risk per trade. In engine
   mode this is enforced through `RiskManager.build_order` with a short-side
   risk modifier (`risk_modifier`, default 0.65 ≈ 1 % of the pool at the
   default `MAX_POSITION_SIZE_PCT`); `risk/sizing.py` provides the standalone
   maths for backtests.
6. **Earnings/Catalyst Blackout** — no *new* short entries within
   `earnings_blackout_days` (default 2) of a scheduled upcoming earnings
   report. Does not conflict with the post-earnings fade (its catalyst is in
   the past).
7. **Portfolio short-exposure cap** — total short notional (open shorts +
   candidates accepted this scan, strongest first) ≤ `max_short_exposure_pct`
   of total capital, plus a `max_short_positions` count cap enforced by the
   risk manager's strategy-family logic.

## 8. Engine integration

- `engine.run_cycle` runs `short_strategies.scanner.run_short_scan` after the
  long scan (when `SHORT_STRATEGIES_ENABLED`, default on) and feeds the
  resulting core `Signal`s through the identical `_process_signal` pipeline.
- `RiskManager.pre_check` / `build_order` and the `Signal`
  risk/reward properties are direction-aware (short: stop above entry,
  target below entry).
- `PaperBroker.place_bracket_order` accepts `side="short"`: short entries
  fill with downward slippage; stop/target/partial-take checks mirror; the
  stop ratchet (`modify_stop`) only ever moves a short stop *down*.
  IBKR short brackets are not yet supported (returns `unsupported`, same
  precedent as manual multi-level orders).
- `ExitManager`: time-based exits are direction-aware; position-health
  re-scoring and the long-only dynamic-stop engine skip short positions
  (their protection is the ATR buy-stop + time exits) — future work.
- The journal (`trades.csv`) P&L/R maths were already direction-aware.

## 9. Configuration

All parameters live in `short_strategies/common/config.py` as dataclasses
with documented defaults. Top-level operational knobs are environment-
overridable (read from the process env / `.env` like the main settings):

| env var | default | meaning |
|---|---|---|
| `SHORT_STRATEGIES_ENABLED` | `true` | master switch for the short scan |
| `SHORT_MAX_POSITIONS` | `5` | strategy-family position cap |
| `SHORT_MAX_EXPOSURE_PCT` | `0.25` | short notional cap vs TOTAL_CAPITAL |
| `SHORT_RISK_MODIFIER` | `0.65` | scales the per-trade risk budget for shorts |
| `SHORT_STOP_ATR_MULT` | `1.5` | ATR multiple for the buy-stop |
| `SHORT_TARGET_RR` | `2.0` | reward:risk multiple for the cover target |
| `SHORT_EARNINGS_BLACKOUT_DAYS` | `2` | upcoming-earnings blackout window |
| `SHORT_REGIME_FILTER_ENABLED` | `true` | require benchmark below trend |
| `SHORT_ADX_CONFIRM_ENABLED` | `true` | ADX gate on trend-following shorts |

No new *required* env vars: everything defaults safely, so prod `.env` needs
no edits (config-drift rule from the deploy runbook).

## 10. Testing

- Per-strategy unit tests with synthetic OHLCV frames that isolate each
  pattern (and its negative cases) in `short_strategies/tests/`.
- Filter, sizing, scanner, and backtest-runner tests alongside.
- Core direction-awareness tests in `tests/test_short_pipeline.py`
  (risk manager, paper broker short brackets, exit manager, engine wiring).
