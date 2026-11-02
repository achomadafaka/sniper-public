# Memecoin X Source Graph Phase-1 Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Repair the four proven Phase-1 review findings before any browser adapter or correlator
is built.

**Architecture:** Keep `memecoin_x_forward_posts` as the canonical-post read model, but add an
immutable `memecoin_x_card_observations` journal for every captured delivery. Relationships and
coin mentions point to the exact observation that produced them, so later complete cards cannot
borrow an earlier timestamp and every normalized fact has replayable raw evidence. Address and
relationship validation fail closed at normalization.

**Tech Stack:** Python 3.13, sqlite3, pytest, repo-local brain index.

## Global Constraints

- Display-only. No browser, network, trade, sizing, veto, service, or funded-runner integration.
- TDD for each behavioral correction: failing test, observed failure, minimal fix, focused pass.
- Forward evidence always uses the observation that exposed the exact normalized fact.
- `posted_at` remains metadata and never substitutes for our observation timestamp.
- Existing seven tables retain their names; the immutable observation journal is the eighth.
- One logical change per commit.

---

### Task 1: Fail-closed identity normalization

**Files:**
- Modify: `sniper/analysis/memecoin_x/normalize.py`
- Modify: `tests/test_memecoin_x_normalize.py`

**Interfaces:**
- Preserve `extract_coin_mentions(text, urls) -> list[dict]` and `normalize_card(card) -> dict`.
- EVM text matches require a complete standalone `0x` plus 40-hex token.
- Solana `CA:` labels are case-insensitive, but the Base58 address class is case-sensitive and
  every match passes `is_solana_address()`.
- A `direct` relationship is eligible only when canonical author equals feed actor after handle
  normalization.

- [ ] **Step 1: Add failing regression tests**

Add tests proving that 41-hex, embedded EVM substrings, and Base58 strings containing `I`, `O`, or
`l` produce no mint; add a test proving `direct` with different actor/author is diagnostic-only.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest tests/test_memecoin_x_normalize.py -k \
  'rejects_non_exact_evm or rejects_forbidden_base58 or direct_requires_actor_author_match' -q
```

Expected: all new tests fail on the reviewed implementation.

- [ ] **Step 3: Implement the minimal validation fix**

Use alphanumeric boundaries around `_EVM_RE`, scope case-insensitivity to the `CA|contract`
label only, validate every Solana match through `is_solana_address`, and add the direct-identity
invariant to `normalize_card`'s eligibility condition.

- [ ] **Step 4: Run GREEN and the normalize suite**

```bash
python3 -m pytest tests/test_memecoin_x_normalize.py -q
```

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/memecoin_x/normalize.py tests/test_memecoin_x_normalize.py
git commit -m "fix(memecoin-x): reject invalid address and direct-card identities"
```

---

### Task 2: Immutable observation-backed persistence

**Files:**
- Modify: `sniper/persistence/db_memecoin_x.py`
- Modify: `tests/test_memecoin_x_store.py`
- Modify: `docs/superpowers/specs/2026-07-13-memecoin-x-source-graph-design.md`
- Modify: `docs/superpowers/plans/2026-07-13-memecoin-x-source-graph-phase1-core.md`

**Interfaces:**
- Add `memecoin_x_card_observations`, unique by
  `(capture_run_id, canonical_post_id, actor_handle, relationship)`.
- Each observation stores author, actor, relationship, observed time/domain, parser version,
  text/completeness, posted metadata, extracted URLs, and raw-card JSON.
- Add nullable `source_observation_id` to relationships and mentions for compatibility with any
  pre-existing local Phase-1 database.
- `persist_card(...)` returns `new_observation` in addition to existing flags.
- New relationships and mentions reference the observation that produced them.
- When a higher parser version is persisted for a canonical post, replace lower-version mention
  state before inserting the higher-version result; lower parser input cannot overwrite newer
  state.

- [ ] **Step 1: Add failing schema and replay tests**

Cover table creation/idempotency, two observations for truncated then complete delivery, mention
raw evidence and observed timestamp from the complete delivery, second-actor relationship evidence,
and higher-parser correction from eligible to ineligible.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest tests/test_memecoin_x_store.py -k \
  'card_observation or complete_redelivery or second_actor or parser_correction' -q
```

Expected: missing table/column assertions and replay-integrity assertions fail.

- [ ] **Step 3: Implement schema and persistence**

Create the observation table and indexes in `ensure_memecoin_x_tables()`. Add a small idempotent
column-migration helper for `source_observation_id`. In `persist_card()`, persist/get the immutable
observation before relationship/mention writes, attach its ID to new facts, and apply the parser
version replacement rule transactionally.

- [ ] **Step 4: Run GREEN and all Phase-1 tests**

```bash
python3 -m pytest tests/test_memecoin_x_store.py tests/test_memecoin_x_normalize.py \
  tests/test_memecoin_x_clock_health.py tests/test_memecoin_x_roster.py -q
```

- [ ] **Step 5: Update the approved design and Phase-1 plan**

Document the eighth table, fact-level observation authority, parser replacement rule, and corrected
table/test counts. Remove claims that one canonical-post raw card is sufficient to replay every
relationship or mention.

- [ ] **Step 6: Commit**

```bash
git add sniper/persistence/db_memecoin_x.py tests/test_memecoin_x_store.py \
  docs/superpowers/specs/2026-07-13-memecoin-x-source-graph-design.md \
  docs/superpowers/plans/2026-07-13-memecoin-x-source-graph-phase1-core.md
git commit -m "fix(memecoin-x): persist immutable card observations"
```

---

### Task 3: Engineering-index truth and remaining-phase contract

**Files:**
- Modify: `index/schema_registry.json`
- Modify: `index/dependency_index.json`
- Create: `docs/superpowers/plans/2026-07-13-memecoin-x-source-graph-remaining-phases-guidelines.md`

**Interfaces:**
- Generated schema and dependency artifacts must equal fresh in-memory generation.
- Phase 2 reads only and writes capture runs/observations through Phase-1 APIs.
- Phase 3 correlates from eligible mention observation timestamps, never canonical-post first-seen
  time, and selects current parser state.
- Phase 4 remains display-only with no automatic following or trading effects.

- [ ] **Step 1: Regenerate indexes**

```bash
python3 scripts/brain_index.py generate
```

- [ ] **Step 2: Write remaining-phase guidelines**

Specify Phase 2 browser boundaries and coverage diagnostics, Phase 3 fact-level temporal joins and
evaluation gates, and Phase 4 display-only surfaces/promotion prohibitions.

- [ ] **Step 3: Verify**

```bash
python3 -m pytest tests/test_memecoin_x_store.py tests/test_memecoin_x_normalize.py \
  tests/test_memecoin_x_clock_health.py tests/test_memecoin_x_roster.py tests/test_brain_index.py -q
python3 scripts/validate_schema_refs.py --files sniper/persistence/db_memecoin_x.py \
  sniper/analysis/memecoin_x/normalize.py sniper/analysis/memecoin_x/roster.py scripts/brain_index.py
python3 scripts/brain_index.py schema memecoin_x_card_observations
git diff --check
```

- [ ] **Step 4: Commit**

```bash
git add index/schema_registry.json index/dependency_index.json \
  docs/superpowers/plans/2026-07-13-memecoin-x-source-graph-remaining-phases-guidelines.md
git commit -m "docs(memecoin-x): define remaining phase gates"
```

## Self-Review

- Spec coverage: all four review findings map to Tasks 1–3.
- Placeholder scan: no implementation placeholders or unowned decisions.
- Type consistency: relationships and mentions reference the same immutable observation ID;
  Phase 3 is explicitly required to use mention-level observation time.
- Scope: browser, correlator, output, and trading behavior remain unimplemented.
