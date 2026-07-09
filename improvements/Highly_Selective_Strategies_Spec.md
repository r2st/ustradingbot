# Highly Selective Strategies — Feature Spec

## Overview

A new strategy module (`selective_strategies/`) that adds six low-frequency, high-selectivity strategies to the US Trading Bot. These strategies stack multiple independent filters so each setup fires rarely but with cleaner statistical edge than a single-indicator system.

## Strategy Inventory

| ID | Name | Direction | Trigger Type |
|----|------|-----------|-------------|
| `hs_rsi2_reversal` | RSI-2 Extreme Mean Reversion at Structural Support/Resistance | Long/Short | Technical (daily) |
| `hs_triple_timeframe` | Triple-Timeframe Trend Confluence Breakout | Long/Short | Technical (multi-TF) |
| `hs_bb_climax` | Bollinger Band Climax Reversal with Volume Exhaustion | Long/Short | Technical (daily) |
| `hs_pead_drift` | Post-Earnings Volume-Confirmed Drift Continuation | Long | Event (earnings) |
| `hs_gap_fill` | Opening Range Gap-Fill (Statistical Gap Fade) | Short (gap-up) / Long (gap-down) | Intraday/daily |
| `hs_turnaround_tuesday` | Turnaround Tuesday (Day-of-Week Seasonal) | Long | Calendar (Monday close) |

### Strategy A — `hs_rsi2_reversal`

RSI(2) extreme + structural support/resistance confluence + volume confirmation. Enters when RSI(2) < 5 (long) or > 95 (short), price is within 0.5% of a tested support/resistance level (≥2 touches in 60 days), volume ≥1.2x 20-day average, and the 200-SMA trend filter is met.

Exit: 5-day SMA crossback or RSI-2 > 65 (long), hard stop at 1.5x ATR(14), 6-day time stop.

### Strategy B — `hs_triple_timeframe`

Three nested timeframes (daily macro trend, simulated 4H structure, simulated 1H trigger) must align. Daily: price above rising 50-SMA and 200-SMA. 4H proxy: consolidation ≥15 bars with contracting ATR. 1H proxy: breakout bar with volume ≥1.5x average. No upcoming macro/earnings event within 2 days.

Exit: Chandelier trailing stop (ATR(22) x 3 from highest high). Invalidation if close re-enters broken range.

### Strategy C — `hs_bb_climax`

Close outside the Bollinger Band (20, 2σ), volume in the top 5th percentile of trailing 100 days, recognized reversal candle pattern (hammer, engulfing, morning star), and 3-day accelerating move into the extreme.

Exit: Middle Bollinger Band (20-SMA) target, signal-day low minus 0.25x ATR stop, 5-day time stop.

### Strategy D — `hs_pead_drift`

Post-earnings gap ≥3%, first-hour hold above the open, full-day volume ≥2x average, and close in the top third of the day's range. This is event-driven and only fires around earnings dates.

Exit: 10-day drift hold, invalidation if close below pre-earnings close. Stop at 2x ATR(14).

### Strategy E — `hs_gap_fill`

Small-to-moderate overnight gap (0.3%–1.0%) on liquid instruments (no macro event day), with an indecisive first 15-min candle (body < 30% of range). Fades the gap toward the prior close.

Exit: Gap-fill target (prior close), stop above the first candle's high + 0.1x ATR, end-of-day time stop.

### Strategy F — `hs_turnaround_tuesday`

Calendar-driven: Monday close at least 1% below Friday's close, Internal Bar Strength (IBS) < 0.2, price above 200-SMA. Enters long at Monday's close, exits at Tuesday's close.

Hard stop at 2% below entry.

## How These Differ from Existing Strategies

| Aspect | Existing (Momentum, Swing, VCP, etc.) | Highly Selective |
|--------|---------------------------------------|-----------------|
| Frequency | Fires on most scan cycles for some symbols | Fires rarely — all filters must align simultaneously |
| Filter count | 1–3 conditions | 4–6 independent conditions per strategy |
| Trigger type | Mostly technical | Mix of technical, event, calendar, and seasonal |
| Position sizing | Standard risk budget | Reduced risk budget (0.5–1% per trade), own family cap |
| Correlation | Moderate overlap between momentum/swing | Largely uncorrelated with each other and existing strategies |

## Integration Points

### 1. Scanner (`selective_strategies/scanner.py`)

New `run_selective_scan()` function, modeled on the short-strategies scanner pattern. Called by `engine.py:_run_selective_scan()` alongside the existing long and short scans. Uses its own `STRATEGY_PRIORITY` and `DETECTORS` registry.

### 2. Engine (`engine.py`)

New `_run_selective_scan(scan_symbols, selection)` method in `TradingEngine.run_cycle()`, called between the long and short scans. Best-effort: failures never break the existing scan. Signals join the same pipeline (`_process_signal`).

### 3. Trade Selection (`config/trade_selection.py`)

New `SELECTIVE_STRATEGIES` tuple with all six `hs_*` strategy keys, appended to `VALID_STRATEGIES`. Dashboard shows them as a separate "Highly Selective" group.

### 4. Risk Manager (`risk/manager.py`)

New `"selective"` family in `_strategy_family()`. Position cap via `MAX_SELECTIVE_POSITIONS` setting (default 3). All six strategies share one cap since they fire rarely.

### 5. Backtester (`backtest/engine.py`)

Strategies added to `_DEDICATED_DETECTORS`, `_MAX_HOLD_DAYS`, and `_family()`. Opt-in (not in `DEFAULT_STRATEGIES`), like PEAD — must be explicitly selected in the backtest form.

### 6. Dashboard (`dashboard/app.py`)

New entries in `_build_strategies()` under the `"selective"` family.

### 7. Analyst Cards (`dashboard/analyst_cards.py`)

New `STRATEGY_TAGS` entries for each `hs_*` strategy.

### 8. AI Analyst (`ai/analyst.py`)

New branch in `_strategy_guidance()` for `hs_*` strategy-specific veto logic.

## Macro/Calendar Event Detection

Strategies B, D, E, and F depend on macro/calendar awareness:

- **Earnings dates** (D): Reuses the existing `signals.pead_signal.get_recent_earnings()` infrastructure.
- **Macro event calendar** (B, E): New `selective_strategies/events.py` module that checks for FOMC, CPI, NFP, and other high-impact releases. Initial implementation uses a static monthly calendar with manual overrides; can later integrate with an API.
- **Day-of-week check** (F): Simple `datetime.weekday()` check against Eastern timezone. Implemented as a scheduled rule, not a continuously evaluated signal.

## Config Architecture

All thresholds are configurable via `SelectiveConfig` dataclass (one per-strategy sub-config), with env-variable overrides via `SELECTIVE_*` prefix. No hardcoded thresholds.
