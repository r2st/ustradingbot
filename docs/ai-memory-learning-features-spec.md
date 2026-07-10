# Feature Spec: AI Memory, Learning & Self-Improvement System

**Version:** 1.0
**Date:** July 10, 2026
**Author:** Suman (CTO) — auto-generated from video analysis
**Status:** PROPOSAL — awaiting approval before implementation
**Source:** Analysis of ["How to Actually Build an AI Trading Bot (Full Guide)"](https://www.youtube.com/watch?v=PBBSMSyU674) by Miles Deutscher Finance

---

## 1. Motivation

The YouTube video identifies four reasons trading bots fail:

1. **No memory** — the bot repeats the same losing trade because it never learns.
2. **No goals/objectives** — no feedback loop to optimize against.
3. **No future-condition awareness** — trained on past data without forecasting.
4. **Crowded strategy** — too many people running the same edge.

USTradingBot already addresses problems 2-4 well: it has configurable objectives (grade thresholds, R:R minimums), market-regime detection for forward-looking context, and custom-built strategies. But **problem 1 — memory — is the biggest gap**. The bot logs every trade in `trades.csv` but never reads those lessons back to influence future entry decisions. The AI veto layer evaluates each signal in isolation, unaware that the same setup lost money three times last month.

The video proposes a "two-file memory system" (ledger + learnings) where the bot writes itself plain-English lessons after each closed trade and consults them before every new entry. This spec adapts that concept into six concrete features tailored to USTradingBot's architecture.

---

## 2. Feature Overview

| # | Feature | Priority | Effort | Video Concept |
|---|---------|----------|--------|---------------|
| F1 | AI Trade Reflection (Learnings Engine) | P0 | Medium | "Learnings file" — bot writes itself one lesson per closed trade |
| F2 | Similar-Setup Guard | P0 | Medium | Bot checks its record before entering, skips setups it already lost on |
| F3 | Strategy Performance Decay Detector | P1 | Small | "Strategy loses its edge" — detect degradation automatically |
| F4 | Multi-Model AI Consensus | P1 | Small | "Testing Claude vs GPT vs others to see which is best" |
| F5 | Cloud Memory Backend | P2 | Large | "Supabase or Firebase for thousands of trades" |
| F6 | Natural Language Strategy Builder | P2 | Large | "Go back and forth with the AI about your trading style" |

---

## 3. Feature Specifications

### F1: AI Trade Reflection (Learnings Engine)

**What:** After every trade closes (stop hit, target hit, time exit, setup broken), an AI call analyzes the trade and produces a structured lesson stored in `learnings.jsonl`. Before every new entry, the AI veto layer reads recent relevant learnings and incorporates them into its decision.

**Why (from video):** "When a trade closes, the bot writes itself one plain English lesson. So, it lost on this setup, don't take it again without confirmation of XYZ. This really matters because every single time it trades, it's going to read the ledger and the learnings file."

**Architecture:**

```
Trade Closes (exit_manager)
       │
       ▼
┌──────────────────────────────┐
│  Reflection Worker           │
│                              │
│  Input:                      │
│  • Closed trade from journal │
│  • Entry indicators snapshot │
│  • Market regime at entry    │
│  • Exit reason + P&L         │
│                              │
│  Output (AI via OpenRouter): │
│  • lesson_text (plain eng.)  │
│  • pattern_tags []           │
│  • confidence: 0.0-1.0       │
│  • action: avoid | prefer |  │
│           require_confirm    │
└──────────────┬───────────────┘
               │
               ▼
        learnings.jsonl
```

**Data schema — `learnings.jsonl`:**

```json
{
  "id": "lrn_20260710_001",
  "trade_id": "uuid-of-closed-trade",
  "created_at": "2026-07-10T16:05:00-04:00",
  "symbol": "TSLA",
  "strategy": "momentum",
  "direction": "long",
  "grade": "B",
  "entry_indicators": {
    "rsi": 68.2,
    "macd_histogram": 0.45,
    "ema_score": 0.72,
    "volume_ratio": 1.8,
    "regime": "bull"
  },
  "outcome": {
    "pnl_net": -142.30,
    "realized_r": -0.85,
    "days_held": 3,
    "exit_reason": "STOP_HIT"
  },
  "lesson_text": "Momentum B-grade entries on TSLA with RSI above 65 and volume ratio below 2.0 have hit stops 3 of the last 4 times. The setups fire during low-conviction drift rather than genuine breakouts. Require volume ratio >= 2.5 or skip.",
  "pattern_tags": ["high_rsi_weak_volume", "momentum_b_grade", "mega_cap_tech"],
  "action": "require_confirm",
  "conditions": {
    "strategy": "momentum",
    "grade_max": "B",
    "rsi_above": 65,
    "volume_ratio_below": 2.0
  },
  "confidence": 0.78,
  "expires_at": "2026-10-10T00:00:00-04:00"
}
```

**Integration into entry pipeline:** Insert as gate (d1.5) between the existing AI veto (d) and news sentiment (d2):

```
(d)  AI veto (OpenRouter) ✅
(d1.5) Learnings guard ← NEW
       │
       ├─ Load learnings matching strategy + pattern_tags
       ├─ If action="avoid" and conditions match → REJECT
       ├─ If action="require_confirm" → raise grade threshold to A
       └─ If action="prefer" → boost confidence annotation
(d2) News sentiment ✅
```

**AI model:** `openai/gpt-oss-20b:free` via OpenRouter (matches existing veto layer).

**Settings additions:**

```python
# learnings.jsonl reflection system
LEARNINGS_ENABLED: bool = True
LEARNINGS_MODEL: str = "openai/gpt-oss-20b:free"
LEARNINGS_MAX_AGE_DAYS: int = 90          # lessons expire after 90 days
LEARNINGS_MAX_RELEVANT: int = 5           # max lessons injected into veto prompt
LEARNINGS_MIN_TRADES_FOR_PATTERN: int = 3 # need 3 similar trades before creating a rule
```

**Fail-open behavior:** If the learnings file is missing, unreadable, or the reflection AI call fails, the entry pipeline proceeds as today. Learnings influence decisions but never hard-block without the AI veto's independent agreement.

---

### F2: Similar-Setup Guard

**What:** Before placing a new entry, query the trade journal for historically similar setups (same strategy, similar RSI range, similar volume ratio, same grade) and compute a win rate. If the historical win rate is below a threshold, the signal is demoted or skipped.

**Why (from video):** "The losing signal shows up, but this time the AI actually checks its own record. It sees it already lost on a similar setup to this, so it decides to skip it. This is the key."

**Architecture:**

```
New Signal (screener output)
       │
       ▼
┌──────────────────────────────┐
│  Similar-Setup Matcher       │
│                              │
│  Query trades.csv for:       │
│  • Same strategy             │
│  • RSI within ±10 of signal  │
│  • Volume ratio within ±0.5  │
│  • Same grade (A or B)       │
│  • Same regime (bull/bear)   │
│  • Last 90 days              │
│                              │
│  Compute:                    │
│  • match_count               │
│  • win_rate                  │
│  • avg_realized_r            │
│  • avg_days_held             │
└──────────────┬───────────────┘
               │
      ┌────────┴────────┐
      │                 │
  win_rate >= 35%   win_rate < 35%
      │              AND matches >= 5
      ▼                 │
   PROCEED              ▼
                   DEMOTE or SKIP
                   (grade B → require A)
```

**Implementation:** A new module `analytics/setup_similarity.py` that:

1. Loads closed trades from `trades.csv` as a pandas DataFrame.
2. Filters by strategy, grade, and a sliding window on RSI/volume/regime.
3. Computes win rate and average R-multiple.
4. Returns a `SimilarSetupResult` dataclass consumed by the engine's entry pipeline.

**Settings:**

```python
SIMILAR_SETUP_ENABLED: bool = True
SIMILAR_SETUP_LOOKBACK_DAYS: int = 90
SIMILAR_SETUP_MIN_MATCHES: int = 5        # need 5 historical matches to act
SIMILAR_SETUP_RSI_TOLERANCE: float = 10.0  # ±10 RSI points
SIMILAR_SETUP_VOL_TOLERANCE: float = 0.5   # ±0.5 volume ratio
SIMILAR_SETUP_MIN_WIN_RATE: float = 0.35   # below this → demote to A-only
SIMILAR_SETUP_BLOCK_WIN_RATE: float = 0.15 # below this → hard skip
```

**Performance:** The trades.csv file is loaded once per cycle (not per signal) and held in memory. With the current MAX_OPEN_POSITIONS=25 and ~5 signals per cycle, this adds negligible overhead.

---

### F3: Strategy Performance Decay Detector

**What:** A rolling-window monitor that detects when a strategy's edge is degrading — win rate dropping, average R declining, or loss streaks lengthening. When decay is detected, the strategy's allocation is automatically reduced (fewer max positions, higher grade threshold) until performance recovers.

**Why (from video):** "A lot of the pre-packaged bots simply too many people are using. So, they actually lose their edge in the market." Even custom strategies can decay as market conditions shift.

**Architecture:**

```
analytics/strategy_health.py
       │
       ▼
┌──────────────────────────────────────┐
│  Per-Strategy Rolling Metrics        │
│                                      │
│  For each strategy (last 30 trades): │
│  • win_rate                          │
│  • avg_realized_r                    │
│  • max_consecutive_losses            │
│  • profit_factor                     │
│  • trend (improving/stable/decaying) │
│                                      │
│  Decay triggers:                     │
│  • win_rate < 30% (was >40%)         │
│  • 5+ consecutive losses             │
│  • avg_R declining 3 periods in row  │
└──────────────┬───────────────────────┘
               │
      ┌────────┴────────┐
      │                 │
   healthy           decaying
      │                 │
      ▼                 ▼
  normal caps      reduced caps:
  normal grades    max_positions × 0.5
                   min_grade → A only
                   alert sent
```

**Integration:** Called in `_refresh_regime_and_autotune()` each cycle. Decay modifiers are applied alongside the existing regime multipliers. Logged to the activity feed and surfaced on the dashboard.

**Recovery:** When the rolling window shows 3 consecutive wins or win rate climbs above 40%, allocations restore to normal. The entire decay/recovery history is logged to `strategy_health.jsonl` for the operator's review.

**Settings:**

```python
STRATEGY_DECAY_ENABLED: bool = True
STRATEGY_DECAY_LOOKBACK_TRADES: int = 30
STRATEGY_DECAY_WIN_RATE_FLOOR: float = 0.30
STRATEGY_DECAY_MAX_CONSECUTIVE_LOSSES: int = 5
STRATEGY_DECAY_POSITION_MODIFIER: float = 0.5  # halve max positions
STRATEGY_DECAY_RECOVERY_WINS: int = 3
```

---

### F4: Multi-Model AI Consensus

**What:** Run the AI veto call through 2-3 free OpenRouter models simultaneously, then take a majority vote. Track per-model accuracy over time and weight the vote accordingly. When models disagree, default to the most conservative (reject-leaning) model.

**Why (from video):** "I'm even doing a test right now where I've put Claude and GPT and other models up against each other to see which one is the best."

**Architecture:**

```
ai/multi_model.py
       │
       ▼
┌─────────────────────────────────────────┐
│  Multi-Model Evaluator                  │
│                                         │
│  Models (all :free via OpenRouter):     │
│  • openai/gpt-oss-20b:free (primary)   │
│  • google/gemini-flash:free             │
│  • meta-llama/llama-4-scout:free        │
│                                         │
│  Execution: asyncio.gather() parallel   │
│  Vote: majority wins; tie → REJECT      │
│  Tracking: model_accuracy.json          │
│  • per-model approve/reject accuracy    │
│  • measured against actual trade P&L    │
└──────────────┬──────────────────────────┘
               │
        Consensus Decision
        + per-model reasoning
```

**Backward compatibility:** When `MULTI_MODEL_ENABLED=False` (default), behavior is identical to today — single model. When enabled, the primary model's response is still used for reasoning text and cost tracking; the vote only modifies the approve/reject decision.

**Model accuracy tracking:** After each trade closes, a background job checks whether each model's verdict was correct (approved a winner = correct; approved a loser = incorrect; rejected what would have been a winner = incorrect). Accuracy scores are stored in `model_accuracy.json` and surfaced on the dashboard.

**Settings:**

```python
MULTI_MODEL_ENABLED: bool = False
MULTI_MODEL_LIST: list[str] = [
    "openai/gpt-oss-20b:free",
    "google/gemini-flash:free",
    "meta-llama/llama-4-scout:free",
]
MULTI_MODEL_VOTE_THRESHOLD: int = 2       # need 2/3 approvals
MULTI_MODEL_TIMEOUT_SECONDS: float = 30.0
MULTI_MODEL_WEIGHT_BY_ACCURACY: bool = True
```

---

### F5: Cloud Memory Backend (SQLite → PostgreSQL Migration Path)

**What:** Migrate the file-based state (`trades.csv`, `open_positions.json`, `rejected.jsonl`, `learnings.jsonl`) to a SQLite database first, then provide a configuration option to connect to an external PostgreSQL/Supabase instance for cloud persistence.

**Why (from video):** "Two files is okay to start... but you may want to consider building a cloud memory system on something like Supabase or Firebase where your memory is actually stored on the cloud. It can hold thousands of trades and a much bigger ledger."

**Phase 1 — SQLite (local, zero config):**

```
data_store/trading.db
├── trades          (replaces trades.csv)
├── positions       (replaces open_positions.json)
├── rejections      (replaces rejected.jsonl)
├── learnings       (new — F1)
├── model_accuracy  (new — F4)
└── strategy_health (new — F3)
```

Benefits: ACID transactions (no more "write-tmp-rename"), SQL queries for analytics, single-file backup, concurrent dashboard + engine access via WAL mode.

**Phase 2 — PostgreSQL/Supabase (optional cloud):**

Add a `DATABASE_URL` setting. When set, SQLAlchemy connects to the external database instead of local SQLite. The schema is identical. This enables: multi-device access (laptop + VPS both see the same trades), cloud backup without rsync, and Supabase's built-in Row Level Security for future multi-user scaling.

**Migration:** A one-time `migrate_to_db.py` script reads existing CSV/JSON files and imports them into the database. The old files are renamed `.bak` but not deleted.

**Settings:**

```python
DATABASE_BACKEND: str = "file"        # file | sqlite | postgres
DATABASE_URL: str = ""                # only for postgres backend
SQLITE_WAL_MODE: bool = True
```

---

### F6: Natural Language Strategy Builder

**What:** A conversational interface (via the dashboard or a CLI command) where the user describes a trading strategy in plain English, and the AI generates executable Python code conforming to USTradingBot's strategy interface.

**Why (from video):** "You can just go back and forth with the AI about your trading style, your investment philosophy, what strategy you want to follow."

**Flow:**

```
User: "I want a strategy that buys when a stock drops 5% in one day
       but is still above its 200-day EMA, with a stop 2% below the
       entry and a target at the 20-day EMA."

       │
       ▼
┌──────────────────────────────────┐
│  Strategy Builder (AI)           │
│                                  │
│  1. Parse intent → strategy spec │
│  2. Generate Python module       │
│  3. Auto-run backtest            │
│  4. Present results + code       │
│  5. Iterate on feedback          │
└──────────────────────────────────┘
       │
       ▼
  selective_strategies/strategies/user_drop_bounce.py
  (conforms to the BaseStrategy interface)
```

**Safety:** Generated strategies are always created as `selective_strategies` with `MAX_SELECTIVE_POSITIONS=3` and are auto-enrolled in paper trading mode for their first 30 days, regardless of the engine's trading mode. The generated code is saved to disk for human review before live deployment.

**AI model:** Uses OpenRouter free models. The prompt includes the existing strategy interface code (`selective_strategies/strategies/rsi2_reversal.py` as a template), the signal types dataclass, and the combined filter API.

**Settings:**

```python
STRATEGY_BUILDER_ENABLED: bool = False
STRATEGY_BUILDER_MODEL: str = "openai/gpt-oss-20b:free"
STRATEGY_BUILDER_AUTO_BACKTEST: bool = True
STRATEGY_BUILDER_PAPER_ONLY_DAYS: int = 30
```

---

## 4. Implementation Priority & Dependencies

```
Phase 1 (Immediate — next sprint):
  F1 (Learnings Engine) ← highest impact, directly addresses the video's core thesis
  F2 (Similar-Setup Guard) ← depends on F1's pattern_tags for best results,
                              but works standalone with trades.csv

Phase 2 (Next 2 weeks):
  F3 (Strategy Decay Detector) ← extends existing autotune infrastructure
  F4 (Multi-Model Consensus) ← extends existing ai/analyst.py

Phase 3 (Future):
  F5 (Cloud Memory Backend) ← infrastructure change, non-urgent while trade volume is low
  F6 (Strategy Builder) ← highest effort, needs stable strategy interface first
```

---

## 5. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| AI reflection hallucinations create bad lessons | Bot avoids good setups | Require `LEARNINGS_MIN_TRADES_FOR_PATTERN=3` — no lesson from a single trade; lessons expire after 90 days |
| Similar-setup matcher is too aggressive | Kills valid signals | Require 5+ historical matches before acting; demote (don't block) by default |
| Multi-model adds latency | Slower entry pipeline | All models called in parallel via `asyncio.gather()`; 30s timeout per model |
| Cloud database adds a dependency | Downtime blocks trading | SQLite is the default; cloud is opt-in; file backend remains available as fallback |
| Generated strategies have bugs | Bad trades | Auto-paper-trade for 30 days; code saved to disk for review; `MAX_SELECTIVE_POSITIONS=3` hard cap |

---

## 6. Alignment with USTradingBot Architecture

All features follow the existing design principles:

- **Exit-first processing** — unchanged; memory features run in the entry pipeline only.
- **Defense-in-depth** — F1 and F2 add new gates that compound with existing gates, never replace them.
- **Fail-safe defaults** — every new feature is fail-OPEN (not fail-closed): a broken learnings file or a timed-out multi-model call never blocks an otherwise-valid trade.
- **OpenRouter free models** — all AI features use `openai/gpt-oss-20b:free` or equivalent, incurring $0.00 per call.
- **Stateless restarts** — all new state persists to disk (JSONL/JSON/SQLite); the engine recovers cleanly.
- **Single source of truth** — all new settings go into `config/settings.py` with sensible defaults; existing behavior is unchanged until features are explicitly enabled.

---

## 7. Key Video Quotes Mapped to Features

> "Every single time it trades, it's going to read the ledger and the learnings file, and then if it makes a mistake again, it's going to add to it."
→ **F1 (Learnings Engine)**

> "The losing signal shows up, but this time the AI actually checks its own record. It sees it already lost on a similar setup to this, so it decides to skip it."
→ **F2 (Similar-Setup Guard)**

> "A lot of the pre-packaged bots simply too many people are using. So, they actually lose their edge in the market."
→ **F3 (Strategy Decay Detector)**

> "I'm even doing a test right now where I've put Claude and GPT and other models up against each other to see which one is the best."
→ **F4 (Multi-Model Consensus)**

> "You may want to consider building a cloud memory system on something like Supabase or Firebase where your memory is actually stored on the cloud."
→ **F5 (Cloud Memory Backend)**

> "You can just go back and forth with the AI about your trading style, your investment philosophy, what strategy you want to follow."
→ **F6 (Natural Language Strategy Builder)**
