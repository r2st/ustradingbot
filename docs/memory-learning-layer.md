# Memory & Learning Layer

> The bot writes itself a plain-English lesson after every trade closes, and
> consults those lessons — plus its own historical win rate — before it places
> the next entry. This document explains the full learning loop, the modules
> that implement it, its configuration, and the read-only **AI Memory**
> dashboard tab that surfaces it.

The layer ships as two operator-approved features:

| Feature | Name | What it adds |
|---------|------|--------------|
| **F1** | AI Trade Reflection (Learnings Engine) | After a close, an LLM writes one lesson to `learnings.jsonl`; before entries a guard applies matching lessons. |
| **F2** | Similar-Setup Guard | Before an entry, query `trades.csv` for historically similar setups and demote/skip when the win rate is poor. |

Everything in this layer is **fail-open**: a missing key, a timeout, a corrupt
ledger, or an unparseable lesson results in *no action* — memory can never take
down entries or halt trading.

---

## 1. The learning loop at a glance

```
                         ┌──────────────────────────────────────────┐
                         │              TRADE CLOSES                  │
                         │        (exit manager finalises an exit)    │
                         └───────────────────┬──────────────────────┘
                                             │
                    engine._reflect_on_exits │  (after exit mgmt, each cycle)
                                             ▼
                   ┌─────────────────────────────────────────────┐
                   │  F1 · ai/reflection.py  (ReflectionEngine)   │
                   │  • reads the closed trade's context          │
                   │  • asks an OpenRouter model for ONE lesson   │
                   │  • checks support + confidence gates         │
                   └───────────────────┬─────────────────────────┘
                                       │ writes one JSON line
                                       ▼
                   ┌─────────────────────────────────────────────┐
                   │  journal/learnings.py  →  learnings.jsonl    │
                   │  append-only lesson store (with expiry)      │
                   └───────────────────┬─────────────────────────┘
                                       │ read back on next entry
                                       ▼
   ┌───────────────────────────────────────────────────────────────────────┐
   │                      NEXT ENTRY — engine._process_signal                │
   │                                                                         │
   │   … AI veto …                                                           │
   │   (d1)   F2 · analytics/setup_similarity.py  → proceed / demote / block │
   │   (d1.5) F1 · analytics/learnings_guard.py   → allow / demote / reject  │
   │   … news / ratings / regime … → order placement                        │
   └───────────────────────────────────────────────────────────────────────┘
```

The loop is **fully automatic**: no operator action closes it. A trade closes →
a lesson is written → the lesson is applied on the next matching entry.

---

## 2. F1 — AI Trade Reflection (the writer)

**Module:** `ai/reflection.py` (`ReflectionEngine`)
**Store:** `journal/learnings.py` → `data_store/learnings.jsonl`
**Guard:** `analytics/learnings_guard.py` (`LearningsGuard`)

### 2.1 Reflection (writing a lesson)

When a position closes, the engine calls `_reflect_on_exits()` after exit
management (`engine.py`). For each *genuinely* closed trade (partial-takes are
skipped — the runner's final close is reflected on instead) it:

1. Reads the closed row from the just-updated journal (`trades.csv`).
2. Sends the trade's entry context (grade, RSI, volume ratio, MACD, strategy,
   direction) and its outcome to an OpenRouter model (`LEARNINGS_MODEL`).
3. Asks for **one** concise lesson as strict JSON:

   ```json
   {
     "lesson_text": "1-3 sentence lesson",
     "pattern_tags": ["tag", ...],
     "action": "avoid" | "require_confirm" | "prefer" | "observe",
     "conditions": {
       "rsi_above": 65, "rsi_below": null,
       "volume_ratio_below": 2.0, "volume_ratio_above": null,
       "grade_max": "B"
     },
     "confidence": 0.0-1.0
   }
   ```

The lesson is appended to `learnings.jsonl` as one JSON line.

### 2.2 The two guardrails against hallucinated lessons

A lesson only becomes **binding** (i.e. able to `avoid` / `require_confirm` /
`prefer`) when it clears both gates:

- **Support gate** — at least `LEARNINGS_MIN_TRADES_FOR_PATTERN` similar trades
  must already exist in the ledger. A one-off loss is stored as a non-binding
  `observe` note the operator can read, but the guard will not act on it.
- **Confidence gate** — the model must self-report confidence at or above
  `LEARNINGS_MIN_CONFIDENCE`.

Below either gate the lesson is downgraded to `observe`: recorded and visible,
but advisory only.

### 2.3 The learnings guard (applying lessons on entry)

Before placing an entry (gate **d1.5**, after the AI veto), `LearningsGuard`
selects lessons that apply to the incoming signal — same strategy and
direction, with the signal's RSI / volume / grade inside the lesson's
`conditions` — and turns the **most restrictive** applicable action into a
verdict:

| Lesson action | Guard verdict | Effect on the signal |
|---------------|---------------|----------------------|
| `avoid` | **reject** | Signal is dropped. |
| `require_confirm` | **demote** | Allowed only if grade A; everything else is dropped. |
| `prefer` | annotate | Never blocks — logged as a "prefer" note. |
| `observe` | — | Ignored by the guard (advisory only). |

The guard is pure and fail-open: any error reading the store yields `allow`.

---

## 3. F2 — Similar-Setup Guard (the bot's own track record)

**Module:** `analytics/setup_similarity.py` (`find_similar_setups`)

Before an entry (gate **d1**, right after the AI veto and *before* the learnings
guard), the bot asks: *"have I traded a setup like this before, and how did it
go?"* It answers **from its own ledger** (`trades.csv`) — no API call, no state
mutation.

It filters completed trades to those that look like the incoming signal:

- same strategy and direction,
- same grade,
- RSI within `SIMILAR_SETUP_RSI_TOLERANCE` (±10 points),
- volume ratio within `SIMILAR_SETUP_VOL_TOLERANCE` (±0.5),
- inside the `SIMILAR_SETUP_LOOKBACK_DAYS` window,

then computes the historical win rate and average R-multiple over the matches
and returns a decision:

| Condition | Result | Effect |
|-----------|--------|--------|
| Fewer than `SIMILAR_SETUP_MIN_MATCHES` matches | **proceed** | Not enough history to judge. |
| Win rate ≥ `SIMILAR_SETUP_MIN_WIN_RATE` | **proceed** | Track record acceptable. |
| Win rate < `SIMILAR_SETUP_MIN_WIN_RATE` | **demote** | Take the trade only if grade A. |
| Win rate < `SIMILAR_SETUP_BLOCK_WIN_RATE` over a solid sample | **block** | Skip entirely. |

Like the learnings guard, it is fail-open by construction — any read error
yields `proceed`, so a corrupt ledger can never halt trading.

---

## 4. Where the guards sit in the entry pipeline

Both guards run inside `engine._process_signal()`, *after* the paid AI veto so
that a cheap local check never runs before an expensive one is needed, and
before the news/ratings/regime vetoes:

```
a0)   Trade-selection hard gate
a1)   Earnings block/flag
a)    Risk manager pre-check
b)    Strategy capacity
c)    Pending-order guard
d)    AI evaluation (earnings + OpenRouter veto)
d1)   ── Similar-Setup Guard (F2) ──     proceed / demote / block
d1.5) ── Learnings Guard (F1) ──         allow / demote / reject
d2)   News-sentiment veto
d4)   Third-party ratings veto
d3)   Market-regime + auto-tune floor
e)    Build sized order → place bracket
```

A **demote** from either guard drops the signal unless it is grade A. A
**block**/**reject** always drops it. Every rejection is written to the
rejected-signal log (`rejected_signals.jsonl`) with the gate name
(`similar_setup` or `learnings_guard`) and reason — which is exactly what the
dashboard's guard-decisions view reads back.

---

## 5. Configuration

All settings live in `config/settings.py` and load from `.env`. Sensible
defaults ship enabled; the whole layer is safe to leave on.

### F1 — Reflection & learnings guard

| Setting | Default | Purpose |
|---------|---------|---------|
| `LEARNINGS_ENABLED` | `True` | Master switch for reflection + learnings guard. |
| `LEARNINGS_MODEL` | `openai/gpt-oss-20b:free` | OpenRouter model used to write lessons. |
| `LEARNINGS_MAX_AGE_DAYS` | `90` | Lessons expire (are pruned) after this many days. |
| `LEARNINGS_MAX_RELEVANT` | `5` | Max lessons weighed per entry. |
| `LEARNINGS_MIN_TRADES_FOR_PATTERN` | `3` | Similar trades required before a lesson can *bind*. |
| `LEARNINGS_MIN_CONFIDENCE` | `0.6` | Below this, a lesson is stored as advisory `observe`. |
| `LEARNINGS_MAX_TOKENS` | `400` | Completion-token cap for the reflection call. |

> **F1 needs a working `OPENROUTER_API_KEY`.** Without it (or on any API error)
> no lessons are written — the writer is fail-open, so trading continues
> unaffected, but the Memory tab stays empty. F2 does **not** need a key.

### F2 — Similar-setup guard

| Setting | Default | Purpose |
|---------|---------|---------|
| `SIMILAR_SETUP_ENABLED` | `True` | Master switch for the similar-setup guard. |
| `SIMILAR_SETUP_LOOKBACK_DAYS` | `90` | Ledger lookback window for matches. |
| `SIMILAR_SETUP_MIN_MATCHES` | `5` | Matches required before the guard acts. |
| `SIMILAR_SETUP_RSI_TOLERANCE` | `10.0` | RSI band (± points) for "similar". |
| `SIMILAR_SETUP_VOL_TOLERANCE` | `0.5` | Volume-ratio band (±) for "similar". |
| `SIMILAR_SETUP_MIN_WIN_RATE` | `0.35` | Below this win rate → demote to grade-A-only. |
| `SIMILAR_SETUP_BLOCK_WIN_RATE` | `0.15` | Below this (solid sample) → hard skip. |

---

## 6. The AI Memory dashboard tab

A read-only **Memory** tab (nav link "Memory" → `#nav-memory`) surfaces the
bot's self-written memory. Backed by `dashboard/memory_router.py`, all endpoints
are prefixed `/api/memory` and require dashboard auth.

| Endpoint | Method | Returns |
|----------|--------|---------|
| `/api/memory/overview` | GET | One-shot payload powering the whole tab: stats + learnings + reflections + guard decisions. |
| `/api/memory/learnings` | GET | Every stored lesson (expired flagged), newest first. |
| `/api/memory/reflections` | GET | Reflections timeline — lessons paired with the closing trade. |
| `/api/memory/guard-decisions` | GET | Recent block/demote decisions the guards actually applied (from `rejected_signals.jsonl`); `?limit=` 1–500 (default 100). |
| `/api/memory/stats` | GET | Headline stats: total lessons, active bindings by type, guard hit rate. |

The tab shows four sections:

- **Stats summary** — total lessons, active bindings by action type, guard hit rate.
- **Learnings list** — every lesson with its action, conditions, support count, confidence, and expiry.
- **Reflections timeline** — lessons newest-first, paired with the trade that produced them.
- **Guard decisions log** — the entry signals the memory guards recently blocked or demoted.

Everything here is read-only and fail-soft: a missing file reads as empty, a
malformed record is skipped.

---

## 7. Files & data at a glance

| Path | Role |
|------|------|
| `ai/reflection.py` | F1 writer — turns a closed trade into a lesson. |
| `ai/openrouter.py` | Thin OpenRouter chat client used by the reflection engine. |
| `journal/learnings.py` | Lesson store (`Learning`, `LearningStore`) — append/load/prune. |
| `analytics/learnings_guard.py` | F1 reader — applies lessons on entry. |
| `analytics/setup_similarity.py` | F2 — historical win-rate check on entry. |
| `data_store/learnings.jsonl` | Append-only lesson store (auto-created on first close; gitignored). |
| `data_store/trades.csv` | The ledger F2 queries and F1 reflects on. |
| `data_store/rejected_signals.jsonl` | Source for the guard-decisions view. |
| `dashboard/memory_router.py` | `/api/memory/*` endpoints + Memory tab data. |

> **Note.** `learnings.jsonl` does not exist until the first trade closes during
> market hours and F1 writes a lesson — an absent file (and an all-zeros
> `/api/memory/stats`) is the normal cold-start state, not an error.
