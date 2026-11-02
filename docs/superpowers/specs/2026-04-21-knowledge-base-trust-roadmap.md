# Knowledge-Base Trust — Roadmap (post-calibration)

**Status:** draft (revised 2026-04-21 after advisor review, memory-schema audit, and wallet_scout premise correction)
**Owner:** atodorov20
**Depends on:** `2026-04-20-calibration-loop-v1-design.md` (shipped 2026-04-21)

---

## Framing

Calibration Loop v1 gave us **self-report trust** — every `insider_copy` prediction now carries a verification query + envelope + author's at-time calibration snapshot. We can grade agents.

The next layer is **stored-knowledge trust** — when any agent reads memory or external research, it should know:
- Who wrote this and when
- What that author's calibration was at the time
- Whether it has been externally verified against real-world data
- Whether it's still fresh

The original vision ("before we copy, check our own research and knowledge") requires this layer. Calibration without provenance-tagged memory is self-reports with no store-of-record to check against.

**Key design principle:** do not build our own encyclopedia. Cache + annotate external sources (Wikidata for canonical entities, GDELT for real-time news/events, Polymarket for market-belief priors). Memory becomes a thin provenance + calibration layer on top.

---

## Current state — memory tables (audited 2026-04-21)

| Table | Columns relevant to provenance | Content type |
|---|---|---|
| `memories` | `id`, `type`, `name`, `description`, `content`, `source`, `created_at`, `updated_at` | Mixed — agent observations + system snapshots (brain_health_latest, convergence:latest-scan, conflict:latest-scan) + seeded reference |
| `strategy_agent_observations` | `id`, `strategy_type`, `obs_type`, `content`, `confidence`, `run_id (uuid, nullable)`, `created_at` | Pure agent observations via `agents/tools/obs_tool.py` |

**Key finding:** `strategy_agent_observations.run_id` already exists and (presumably) links to `agent_runs.id`. Provenance on that table is a **retrieval-helper problem**, not a schema problem. `memories` is where actual schema work is required.

---

## Wallet Scout premise correction (2026-04-21)

`wallet_scout` was originally queued as the next calibration rollout because it is a high-stakes writer. That logic no longer holds unchanged.

Its teardown half is still valid:
- audit existing `insider_copy_v1` strategies
- stop dead / dormant / reclassified wallets

Its creation half is not:
- discovering new wallets from `insider_score`
- creating new copy strategies from that screen
- activating those strategies downstream

Why: the working premise that higher `insider_score` identified better copy targets has been empirically inverted. Calibrating `wallet_scout` on predictions like "wallet will maintain insider_score >= 70" would measure consistency on a discredited signal, not usefulness for strategy creation.

Immediate implication for roadmap sequencing:
- narrow `wallet_scout` to audit + stop only
- route replacement candidate selection through an archetype-based agent / validator
- defer full `wallet_scout` calibration rollout until its prediction target is tied to a validated selection premise
- if temporarily enabled later, treat `wallet_scout` predictions as diagnostic only, not prescriptive

---

## Work that MUST NOT be wasted

Every phase below explicitly reuses existing infrastructure.

| Existing | Reused by |
|---|---|
| `CALIBRATION_ENABLED_AGENTS` allow-list | Phase 1 |
| `predict_tool.py start-run/finish-run/predict` lifecycle | Phases 1, 2a |
| `agent_runs.calibration_context` JSONB | Phase 2a retrieval helper joins here |
| `strategy_agent_observations.run_id` (existing FK) | Phase 2a.2 (retrieval helper) |
| `brain_index.py` schema tool | Phase 2a migration validation |
| `scripts/validate_schema_refs.py` CI check | Phase 2a column additions |
| `scripts/brain_health.py` check registry | Phase 2a adds KB1 |
| `CALIBRATION_LIVE_SUPABASE_URL/_KEY` integration-test pattern | All phases |

---

## Phase 0 — **Prototype reviewer (validate premise BEFORE spec'ing)**

**Size:** 1 day. No schema, no deploy. Pure discovery.

**Why this now:** advisor caught that Phase 2b/2c speculate on data sources that may not deliver actionable signal for Polymarket markets. Wikidata knows Trump the entity, but markets resolve on events ("Trump wins 2028") that have no Q-ID. GDELT is noisy with an English bias. Prototype first, spec after.

**Work:**
1. Throwaway script `scripts/prototype_copy_reviewer.py` (~200 LOC, not committed long-term)
2. Input: a wallet's new position (condition_id, outcome, wallet address, bid price)
3. Reads ungated (no provenance filter) from BOTH `memories` and `strategy_agent_observations`
4. Stubs Wikidata: hardcoded JSON blobs for 2-3 test entities (entity_lookup returns static)
5. Stubs GDELT: hardcoded JSON for 2-3 test queries (recent_events returns static)
6. Emits: approve/reject/defer + reasoning, plus **what-I-looked-at trace** (which memory rows read, which stub queries hit, which fields consulted)
7. Run on 10 current paper positions from `strategy_positions` WHERE state='open'

**Observations to log (the actual deliverable):**
- Which memory tables/rows were actually consulted
- Which memory fields would have been most load-bearing had they been present
- Whether stub-Wikidata entity lookups mapped to any real market question
- Whether stub-GDELT theme+actor queries produced anything relevant to the market's resolution condition
- Gap list: "if I had X I could have decided definitively"

**Output:** single markdown file `docs/prototypes/2026-04-XX-copy-reviewer-findings.md` with the above observations. This is what grounds Phase 2a schema AND either validates or kills Phase 2b/2c.

**Explicitly NOT in Phase 0:**
- Real Wikidata/GDELT API calls
- Any schema changes
- Any deploy
- Any commitment to the reviewer architecture

---

## Phase 1 — Prototype or defer `wallet_scout` calibration

**Size:** 1 afternoon. Independent of Phase 0/2a. Can run in parallel.

**Status change:** this is no longer a straightforward copy-paste rollout.

**Why:** calibration only adds value if the predicted target matters. `wallet_scout`'s current creation-side target does not. A well-calibrated prediction on "wallet stays above insider_score threshold" is still calibration on junk if that threshold is not a valid creation signal.

**Work plan (not a spec):**
1. Narrow `agents/wallet_scout_agent.py` to audit + stop only
2. Keep `wallet_scout` out of `CALIBRATION_ENABLED_AGENTS` for now
3. If we need interim telemetry, allow diagnostic-only predictions tied to teardown outcomes, not wallet selection
4. Revisit full calibration only after an archetype-based selector replaces the discredited premise

**Deliverable:** prompt-scope patch now; calibration wiring deferred.

**Removed from prior draft:** the assumption that `wallet_scout` is automatically the best next calibration candidate. That was true only when its selection premise was assumed valid.

---

## Phase 2a — Memory provenance + trusted retrieval

**Size:** 4-5 days (revised — initial 2-3 day estimate was optimistic per advisor). Spec deferred until Phase 0 observations land.

**Split into two sub-phases based on memory-table audit:**

### Phase 2a.1 — `memories` table provenance (schema work)
- Add columns: `author_run_id UUID` (FK → agent_runs.id), `author_calibration_snapshot JSONB`, `verified_at_ts TIMESTAMPTZ NULL`, `stale_at_ts TIMESTAMPTZ NULL`
- Backfill path for existing rows: `author_run_id = NULL` acceptable (legacy), exempt forever (same pattern as calibration v1 predictions)
- `obs_tool.py` / any writer helper: populate `author_run_id` + `author_calibration_snapshot` on new inserts
- Migration file + brain_index regeneration + validate_schema_refs update

### Phase 2a.2 — `strategy_agent_observations` trusted retrieval (helper work)
- No schema changes — `run_id` column already exists
- New helper: `get_observations_trusted(strategy_type, min_calibration_pct=0.5)` joins `strategy_agent_observations → agent_runs.calibration_context`, filters by accuracy_pct at time of write
- Read path in `obs_tool.py read` grows a `--trust-filter` flag
- Brain health: new check `KB1 — trusted memory coverage` (percent of recent observations whose author_run calibration ≥ threshold)

### Phase 2a shared:
- Integration test `tests/test_memory_provenance.py` — asserts new `memories` rows have author_run_id populated, trusted retrieval honors threshold, joins work
- Default `trust_filter` threshold: **propose 0.5 min calibration_pct AND ≥10 graded samples** (same gate as red-flag rule in calibration v1). Open for discussion.

**Explicitly NOT in v1:**
- Automatic staleness detection (stale_at_ts is nullable, set by periodic job later)
- Memory versioning / revision history (would bloat schema)
- Trust score ML — use `accuracy_pct` directly from calibration_context
- Write-path enforcement (a writer with NULL run_id still succeeds; CAL1-style warn-only)

---

## Phase 2b/2c — Wikidata / GDELT adapters

**Status:** **SPEC DEFERRED pending Phase 0 observations.** Advisor's validation gate: does Wikidata entity data change the copy decision on ≥ 3 of 10 prototype positions? Does GDELT theme-scored events discriminate win/loss for the strategy's historical paper trades? If yes to either, spec that one. If neither, skip both.

No size estimate until prototype findings land.

---

## Phase 3 — Second-opinion routing (pre-copy gate)

**Status:** spec deferred until 2a ships AND Phase 0/2b/2c direction is decided.

**Intent unchanged from previous draft:** when a copy-strategy is about to paper-copy a wallet's new position at ≥ threshold confidence, a reviewer agent consults trusted memory + (validated external sources if any) + market-implied probability. Returns approve/reject/defer with its own calibrated envelope.

---

## Phase 4 — Rollout remaining calibration agents

After Phase 2a ships: calibrate the rest per calibration v1 spec §7.2.
- v1.2 `strategy_reviewer` (cheap)
- v1.3 `world_analyst` (prompt-tighten first)
- v1.4 `finance_analyst` (last — finance is conflated with "is loop working")
- deferred `insider_analyst` (sample chronically < MIN_SAMPLE)

---

## Revised sequencing

```
Phase 0 (prototype, 1 day)  ←  priority, blocks Phase 2a spec
Phase 1 (wallet_scout scope-narrowing, 1 short pass)  ←  parallel-izable with Phase 0
                ↓
Phase 1b (optional wallet_scout diagnostic-only calibration, only if useful)
                ↓
Phase 2a (4-5 days, spec'd from Phase 0 findings)
                ↓
Phase 2b/2c (if validated by Phase 0) — concurrent
                ↓
Phase 3 (1 week)
                ↓
Phase 4 (remaining agents, parallelizable)
```

**Key change from prior draft:** Phase 0 prototype now blocks Phase 2a's spec (but not Phase 1, which is independent). This is the advisor's "don't spec speculatively" correction.

---

## Non-goals for this roadmap

- Knowledge graph canonicalization from scratch (use Wikidata IF validated)
- Real-time event streams beyond GDELT polling cadence
- Multi-language coverage
- ML-based trust scoring (use calibration_pct directly)
- Memory versioning / edit history

---

## Open questions — answers after Phase 0

1. ~~Phase 1 agent choice~~ → **re-opened: do not assume `wallet_scout` until its premise is fixed**
2. Phase 2a schema — is the proposed 4-column add to `memories` minimal, or do we need more (evidenced by Phase 0 gap list)?
3. Phase 2a default `trust_filter` threshold — 0.5 calibration_pct + 10 samples, or stricter based on Phase 0 observations?
4. Phase 2b/2c — do we spec them at all, or does Phase 0 kill the premise?
5. Phase 3 — advisory-only for how long before blocking (if we get there)?

---

## Changelog

- **2026-04-21 v2** — revised after advisor review:
  - Added Phase 0 prototype step (blocks Phase 2a spec, not Phase 1)
  - Phase 1 agent: `strategy_reviewer` → **`wallet_scout`**
  - Phase 2a size: 2-3 days → **4-5 days**
  - Phase 2a split into 2a.1 (`memories` schema) + 2a.2 (`strategy_agent_observations` retrieval)
  - Phase 2b/2c deferred pending Phase 0 validation (was: full specs coming soon)
  - Decoupled Phase 2a start from Phase 1 completion
- **2026-04-21 v3** — wallet_scout premise correction:
  - Added explicit note that `wallet_scout` creation-side selection is operating on a discredited `insider_score` premise
  - Changed Phase 1 from "extend calibration to wallet_scout" to "scope-narrow now, calibration later if still useful"
  - Kept teardown/audit value in scope; deferred prescriptive calibration until an archetype-based selector exists
- **2026-04-21 v1** — initial draft
