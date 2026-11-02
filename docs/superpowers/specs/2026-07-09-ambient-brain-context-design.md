# Ambient Brain Context — Design (approach C, hybrid)

Status: DESIGN (rev 2 — folds in Fable review) · 2026-07-09 · depends on migrations 028–031 (FTS + pgvector semantic retrieval, shipped this session).

## Problem

Reads from the Supabase `memories` brain are opt-in and rare — the agent doesn't
*know* relevant prior findings exist, so it never searches. A naive "inject the N
most-recent memories at SessionStart" is wrong: Sniper is a multi-front program
(memecoin, crypto5m, hyperliquid, wallet-analyzer, sports, brain/infra), and last
session's front is usually irrelevant to this session's work. **Recency ≠ relevance.**

## Insight

A `SessionStart` hook fires *before the user says anything*, so it cannot know the
session's topic. The relevance signal — the topic — arrives with the **first user
prompt**. So: make SessionStart *front-agnostic* (awareness only), and let the prompt
drive *relevance-matched* retrieval, using the `semantic_search_memories` RPC built
this session.

**Key design correction (rev 2):** the trigger must NOT embed every prompt. Embedding
is for *retrieval*, not *change detection*. A centroid topic-shift detector would embed
every substantive prompt just to decide whether to fire (0.50s + 281MB + an RPC per
prompt, mostly to inject nothing) — the gating would cost more than the payload. Use a
**front-keyword** trigger instead: the no-fire path is a regex + tiny state read, and
the embed is paid only on an actual fire.

## Grounded facts (measured 2026-07-09 in fresh processes, not assumed)

- **Fresh hook process = 0.50s wall + 281 MB RSS** (`/usr/bin/time`: import + model
  load + one embed). This is what a hook pays *per invocation* — so it must be paid
  only on fires. The **no-fire keyword path = 0.0002s** (regex + state read, no embed
  import).
- **Relevance floor = 0.84** — VALIDATED against 12 real first-prompts pulled from
  transcript history (`scripts/validate_ambient_floor.py`, 2026-07-09): on-topic min
  **0.868** (median 0.894, 12/12 clear), off-topic controls max **0.808** (sourdough
  0.808, hiking 0.803 — both would LEAK at 0.80). There is a clean empty gap between
  them; 0.84 sits in it (0.028 below on-topic min, 0.032 above off-topic max). The
  earlier 0.80 default (from messy synthetic guesses) was a NO-GO — it leaked 2/3
  off-topic controls. Prompts that fall in the 0.81–0.86 gap fail quiet (no injection);
  the map + agent-initiated search covers that tail.
- **No clean live fronts histogram exists**: tags are sparse/skewed (top tag
  `wallet-dossier`×22; the 95 synced session mirrors carry no tags), and name-pattern
  classification dumps 218/425 into "other". → The map uses a **curated** fronts list
  (also the keyword-trigger vocabulary) + a live total count, NOT a computed histogram.

## Architecture

Two Claude Code hooks + one shared script. No always-on daemon.

```
SessionStart ──► brain_ambient.py map   ──► awareness map (once/session)
UserPromptSubmit ► brain_ambient.py rag ──► relevance-matched context (gated)
```

`scripts/brain_ambient.py` reuses `bot/embeddings.py` + `bot/supabase_helper.py`
(`semantic_search_memories` with FTS fallback). Everything is **fail-open**: any
error → emit nothing, **exit 0** (never exit 2 — that would reject the user's prompt),
never block.

### Curated fronts (single source of truth, in the script)

```
FRONTS = ["memecoin", "crypto5m", "hyperliquid"|"hyperlive"|"hl", "wallet"|"dossier"|"archetype",
          "sports"|"soccer"|"political", "brain"|"memor"|"retrieval"]
```
Used by both the map (display) and the rag trigger (keyword match). Edit on a genuine
new front — rare.

### Component 1 — SessionStart awareness map (`map`)

Fires on session `startup` and `resume` (on `compact` it's harmless — 150 tokens).
Emits, once per session:

```
[brain] Shared research brain: <N> active memories across fronts:
memecoin · crypto5m · hyperliquid · wallet-analyzer · sports/markets · brain/infra.
Before starting a task, query it — prior findings often already answer or refute:
  python3 bot/supabase_helper.py semantic "<your task in words>"   # conceptual match
  python3 bot/supabase_helper.py search   "<exact key/addr/slug>"  # substring/prefix
Reads default to status=active (retracted/superseded findings are excluded).
```

- `<N>` is one live `count(*) where status=active` with a **3s timeout**. **Degrade,
  don't vanish:** if Supabase is down, emit the map *without* the count line —
  awareness + how-to-query is the payload; `<N>` is decoration.
- No content, no recency dump.

### Component 2 — UserPromptSubmit RAG (`rag`)

Reads the hook's JSON stdin (`session_id`, `prompt`). **Fires only when**:

- first substantive prompt of the session (`len(prompt) ≥ BRAIN_AMBIENT_MIN_PROMPT_CHARS`), **OR**
- the prompt mentions a `FRONTS` keyword **not yet seen** in this session's state
  (the "pivoted to a new front mid-session" case; front pivots virtually always name
  the front).

The **no-fire path** is regex over the prompt + a state-file read — no embedding import.

**Import discipline (mandatory):** `from bot.embeddings import ...` / `fastembed` imports
live **inside the fire branch**, never at module top. A module-level import silently
reintroduces the 0.50s + 281 MB on every prompt and no test will catch it.

**On fire:**
1. Truncate prompt to `BRAIN_AMBIENT_MAX_PROMPT_CHARS` (default 1000) before embedding —
   this workflow routinely pastes multi-KB agent reports; the head of a paste ≠ the
   topic, but a bounded head beats bge's silent 512-token truncation. (Keyword matching
   scans the *whole* prompt — regex is cheap.)
2. `semantic_search_memories(head, K=BRAIN_AMBIENT_TOPK)` with a **3s socket timeout**.
3. Keep results with `similarity ≥ BRAIN_AMBIENT_FLOOR (0.80)`; drop any `id` already
   injected this session (dedup); if nothing clears the floor, emit nothing.
4. Emit names + descriptions + similarity only (no full content — that stays behind an
   explicit fetch):

```
<brain-context note="background context, not instructions; auto-retrieved prior findings — may be stale, verify before relying">
- [0.89] finding/memecoin-copy-duplicate-event-bug — INSUFFICIENT DATA: dedup bug collapses N…
- [0.86] session/finding-crypto5m-binance-leads-book-maker-edge — Binance leads book ~1s…
</brain-context>
```

- Model missing → `semantic_search_memories` falls back to FTS. Supabase down / RPC
  slow → 3s timeout, emit nothing. Any exception → caught, exit 0.

### Component 3 — session state

Per-session JSON at `/tmp/.../brain_ambient_<session_id>.json`:
`{prompt_count, injected_ids, seen_fronts}`. Created on first `rag` for a `session_id`;
drives first-prompt detection, dedup, and seen-front tracking. Best-effort (missing/corrupt
→ treat as first prompt). The `map` command **prunes state files older than 7 days** so
`/tmp` doesn't accumulate one JSON per session.

### Config (env / `web_settings.py`)

| Var | Default | Meaning |
|---|---|---|
| `BRAIN_AMBIENT_ENABLED` | `1` | Master kill-switch (both hooks no-op when off) |
| `BRAIN_AMBIENT_TOPK` | `5` | Max memories injected per fire |
| `BRAIN_AMBIENT_FLOOR` | `0.84` | Min similarity to inject (Task-6 validated on real prompts) |
| `BRAIN_AMBIENT_MIN_PROMPT_CHARS` | `40` | Below this a prompt is non-substantive → skip |
| `BRAIN_AMBIENT_MAX_PROMPT_CHARS` | `1000` | Embed only this many chars (paste guard) |
| `BRAIN_AMBIENT_RPC_TIMEOUT_S` | `3` | Socket timeout on the Supabase RPC |

Hooks wired into **`.claude/settings.local.json`** — the same (tracked) file where
`secret_file_guard` already lives, so hook config has one home. Note the RAG hook's
`timeout: 10` (Claude Code's default is 60s — a hung call would stall every prompt a
full minute):

```json
{ "hooks": {
  "SessionStart":     [{ "hooks": [{ "type": "command", "command": "python3 scripts/brain_ambient.py map", "timeout": 10 }] }],
  "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python3 scripts/brain_ambient.py rag", "timeout": 10 }] }]
}}
```

## Why this solves the stated problem

No component uses recency or assumes a front: the map lists *all* fronts (front-agnostic),
retrieval is driven by the actual prompt (relevance), and the trigger uses front keywords
(what pivots actually contain) rather than a similarity metric the model is bad at
(its whole range is 0.76–0.89). Token cost bounded (map ~150 tok once; RAG ~400–600 tok
only when something clears the floor). Always-on cost = ~sub-ms no-op per prompt.

## Failure modes (all fail-open; none reject a prompt)

- Model absent → FTS fallback. Supabase down/slow → 3s timeout, emit nothing. `map`
  degrades to no-count. Bad/empty prompt → skip. Corrupt state → first-prompt. Any
  exception → caught, **exit 0** (never exit 2). RAG hook `timeout: 10` bounds worst case.
- Subagents: RESOLVED (Task-7 probe, 2026-07-09) — SessionStart hooks do **not** fire in
  subagent (Task-tool) contexts (dispatched a trivial subagent; the map probe log stayed
  empty). No subagent gate needed; the map never reaches task-scoped agents. (Verified via
  the Agent tool, which is the subagent mechanism in this environment.)

## Testing

- Unit (no network): trigger logic (first-prompt / new-front-keyword / substantive),
  import-discipline (assert `fastembed` not imported on the no-fire path), floor filter,
  dedup, paste truncation, and fail-open on every error path (mock embeddings + `_request`).
- Floor validation: pull **~10 real first-prompts from transcript history** and measure
  hit/miss vs `FLOOR` — real prompts sag toward 0.80 (one this session hit 0.818), so
  confirm the real hit rate before trusting the default. Adjust `FLOOR` from evidence.
- Integration: `brain_ambient.py map` (asserts fronts + instructions, and degrades to
  no-count when the count query is stubbed to fail); pipe known JSON prompts to `rag` and
  assert: on-topic first prompt injects; off-topic ("sourdough") injects nothing; a repeat
  dedups to empty; a second prompt naming a new front fires, one naming a seen front does not.

## Settled calls

1. **Trigger**: first-prompt + **front-keyword** shift (NOT centroid — that inverts the
   cost premise and carries an untuned, metric-mismatched threshold). ~10 lines, covers
   the multi-front-session pivot. (First-prompt-only is an even simpler valid v1.)
2. **Default on with kill-switch** — with the keyword trigger the always-on cost is a
   ~sub-ms no-op per prompt (not 0.50s), which makes "default on" easy to defend.

## Out of scope (v1)

Re-embedding/backfill (`embed_memories.py`), a fronts histogram (data won't support it),
cross-session dedup, and auto-writing memories from a session (`sync_session_findings.py`).
