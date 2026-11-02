# Flipper Inference Phase 1 — Per-Wallet Flipper Tag-Grain Persistence (Design)

**Date:** 2026-05-15
**Status:** Phase 1 of the flipper-tag-grain-inference roadmap (Phase 0: `2026-05-15-flipper-tag-grain-inference-design.md`, merged as PR #45). Design only. No schema applied. No persistence code shipped.
**Scope:** name what the analyzer needs to persist per-wallet so that a peer flip-ROI aggregate is computable later.

NOT A BUY OR SELL SIGNAL. This designs an evidence-persistence surface.

---

## 0. Doctrinal framing

Phase 0 designed the **inference path** for flipper-branch tag-grains: given a peer baseline `(mean_flip_roi, peer_count)`, run a bootstrap, return a real verdict instead of `inference_not_run`. That spec presumed the baseline data exists.

A data inventory done in advance of this Phase 1 spec proved it does not:

- `wallet_analysis_results` (Postgres) has **1 row, 1 wallet** total (as of 2026-05-15, post-PR-#44). The analyzer runs produce ProtocolReports but only a single one has ever been persisted to this table.
- `wallet_tag_pnl` (SQLite) has **150 wallets, 330 tags, 50 wallets in `leaf/geopolitics`** — real peer mass — but it stores held-only and combined metrics; no flipper-side fields.
- The 15-MB JSON ProtocolReports produced by `run_wallet_protocol.py --json` are not landing in any queryable store by default.

So Phase 1 is not "design the aggregator" — it's "design what each analyzer run writes about its flipper-branch tag grains so that the aggregator has something to read."

### Non-goals

- Not a wide re-analysis of historical wallets. The "no analyzer batches" guardrail stands.
- Not a Supabase schema change for `wallet_tag_pnl` (SQLite-only). Persistence target is decided in §2.
- Not the aggregator (Phase 2 of the roadmap).
- Not a backfill plan for the 150 wallets already in `wallet_tag_pnl`. They were populated by a legacy path that does not run the current flipper branch.

---

## 1. What needs to be persisted per analyzer run

Per `run_wallet_protocol.py` invocation, the analyzer's `flipper_branch.tag_grains[]` array contains, for each grain (`leaf` or `nav`) × tag:

```
n_at_grain          int     count of flips in this (grain, tag)
grain_total_pnl     float   sum of flip_pnl
grain_roi           float   weighted mean ROI in the grain
grain_win_rate      float   fraction of flips with flip_pnl > 0
grain_avg_win       float   mean of positive-pnl flips
grain_avg_loss      float   mean of negative-pnl flips
grain_max_realized_loss  float
peer_count          int     (currently 0 because no FH4 inference runs)
confidence_flag     str     "ok" / "low_population" / ...
verdict             str     "inference_not_run" (currently always)
```

Today these are computed in-memory and rendered into JSON output. Nothing persists.

**What we want persisted** for each (analyzer_run, wallet, grain, tag):

| Column | Source | Purpose |
|---|---|---|
| `wallet` | analyzer arg | peer key |
| `grain` | tag_grain | join key |
| `tag` | tag_grain | join key |
| `n_at_grain` | tag_grain | peer-set membership filter (>= MIN_PEER_FLIPS_IN_GRAIN = 30) |
| `grain_roi` | tag_grain | per-wallet contribution to peer mean (Phase 0 §3.2 option A: per-peer-mean) |
| `grain_win_rate` | tag_grain | secondary signal |
| `grain_total_pnl` | tag_grain | magnitude / triage |
| `grain_max_realized_loss` | tag_grain | risk caveat surface (downstream) |
| `flip_rate` (wallet-level) | report | mode classification |
| `walk_full_history` | report | data-completeness gate |
| `analyzed_at` | report | recency |
| `protocol_version` | report | guard against cross-version aggregation |
| `code_version` | report | same |
| `wallet_fixture_hash` | computed | re-aggregation key when fixture re-runs change a wallet's grains |

Not persisted (intentionally):
- `grain_avg_win`, `grain_avg_loss` — derivable from win_rate + ROI when needed.
- The full ProtocolReport JSONB — already supported via `wallet_analysis_results.report_json`, this is a parallel narrow surface.

---

## 2. Persistence target — decision

Three options considered.

### Option A: Extend `wallet_tag_pnl` (SQLite) with flipper-side columns

```sql
ALTER TABLE wallet_tag_pnl ADD COLUMN n_flips INTEGER NOT NULL DEFAULT 0;
ALTER TABLE wallet_tag_pnl ADD COLUMN mean_flip_roi REAL;
ALTER TABLE wallet_tag_pnl ADD COLUMN flip_win_rate REAL;
ALTER TABLE wallet_tag_pnl ADD COLUMN flip_total_pnl REAL;
-- + leaf/nav distinction (currently only one row per wallet × tag, no grain hierarchy)
```

Pros:
- 150 wallets / 330 tags already exist; immediate peer mass for tags also held.
- Single table for all per-wallet tag stats.

Cons:
- Conflates held and flipper populations. A wallet with 100 flips and 0 held positions in a tag has no row today; would need a row to land flipper metrics. Schema migration would have to make many fields nullable.
- SQLite — outside Supabase's peer ecosystem; can't be a Postgres view over it.
- The legacy 150 wallets were populated by `analyze_users.py` and similar legacy paths. Running the current flipper branch on those wallets is out of scope (no batches). So extending the schema doesn't backfill the data — the new columns would be null for all 150 legacy rows.

### Option B: New table `wallet_flipper_tag_grain` (Supabase Postgres)

```sql
CREATE TABLE public.wallet_flipper_tag_grain (
  wallet TEXT NOT NULL,
  grain TEXT NOT NULL CHECK (grain IN ('leaf', 'nav')),
  tag TEXT NOT NULL,
  n_at_grain INTEGER NOT NULL,
  grain_roi DOUBLE PRECISION NOT NULL,
  grain_win_rate DOUBLE PRECISION NOT NULL,
  grain_total_pnl DOUBLE PRECISION NOT NULL,
  grain_max_realized_loss DOUBLE PRECISION,
  flip_rate DOUBLE PRECISION NOT NULL,     -- wallet-level, copied per row for filter
  walk_full_history BOOLEAN NOT NULL,
  analyzed_at TIMESTAMPTZ NOT NULL,
  protocol_version TEXT NOT NULL,
  code_version TEXT NOT NULL,
  wallet_fixture_hash TEXT,                 -- supersession key
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'superseded')),
  superseded_by_fixture_hash TEXT,
  PRIMARY KEY (wallet, grain, tag, wallet_fixture_hash)
);

CREATE INDEX idx_wftg_grain_tag_active
  ON public.wallet_flipper_tag_grain (grain, tag)
  WHERE status = 'active';

CREATE INDEX idx_wftg_wallet
  ON public.wallet_flipper_tag_grain (wallet);
```

Pros:
- Co-located with `wallet_analysis_results` (same Supabase project, same RLS).
- Narrow surface, no conflation with held metrics.
- Phase 2 aggregator can be a Postgres view or a simple SELECT.
- Supersession by `wallet_fixture_hash` cleanly handles re-runs (same wallet, new fixture → new row, old row marked superseded).
- KSG-friendly: every row carries `protocol_version` and `code_version` for provenance.

Cons:
- New table to maintain.
- Population only starts when an analyzer run wires up the writer — no historical data.
- Requires a writer in `sniper/analysis/wallet_protocol/ledger/` or `scripts/run_wallet_protocol.py`.

### Option C: Defer — read from `wallet_analysis_results.report_json` at aggregate time

JSONB path queries into the existing column. No new table.

Pros:
- Zero schema change.
- The 1 row that exists already has the structure; new rows would extend the dataset.

Cons:
- Every aggregate query unpacks `flipper_branch.tag_grains[]` from JSONB at runtime — expensive and not cacheable.
- Schema validation lives in code, not in the DB.
- Requires `wallet_analysis_results` to actually receive analyzer-run writes (which it currently doesn't — only 1 row).
- Mixing the writer mechanism (`wallet_analysis_results.report_json` write) with the read mechanism (per-grain rows) means the Phase 2 aggregator can't proceed until the writer is solved separately.

**Recommendation: Option B.** A narrow, indexed, supersession-aware table beats both a conflated SQLite extension (A) and a runtime-unpacked JSONB query (C). The cost — a new writer in the analyzer run path — is the same regardless of which option, so we may as well pay it for the cleaner reader surface.

This is a **recommendation, not a decision**. Phase 2 of THIS spec (the migration) requires the same review gate as the Phase 3 migration in the Phase 0 spec did.

---

## 3. Writer design

A new function `persist_flipper_tag_grains(report: ProtocolReport, *, db_conn)` lives at `sniper/analysis/wallet_protocol/ledger/flipper_tag_grain_writer.py` (new file).

Invoked from `run_wallet_protocol.py` after the protocol completes — only when:

- `--persist` is passed (default: off — analyzer remains analysis-only)
- `report.flipper_branch.is_active` is True
- `wallet_fixture_hash` can be computed (per §3.1)
- `report.walk_full_history` is True (only fully-walked wallets contribute peer signal)

### 3.1 wallet_fixture_hash — deterministic for both modes

The `wallet_fixture_hash` must be **content-derived and deterministic**: two runs that produce identical flipper grains must produce identical hashes, otherwise supersession degenerates into infinite row accumulation. This is non-negotiable.

**Fixture mode** (`--from-fixture <path>`):
- `wallet_fixture_hash = sha256(<fixture file bytes>)`, truncated to 16 hex chars.

**Live mode** (no `--from-fixture`):
- `wallet_fixture_hash = sha256(canonical_content_fingerprint)` where the fingerprint is the JSON-serialized, key-sorted dict:
  ```
  {
    "wallet": <wallet lowercased>,
    "activity_row_ids": <sorted list of activity row IDs>,
    "n_closed_positions": int,
    "n_open_positions": int,
    "flip_count": int,
    "protocol_version": str,
  }
  ```
- Activity row IDs are the deterministic IDs Goldsky / REST return for each fetched row (e.g. `transaction_hash:log_index`). Sorted to remove order-dependence.
- `report.analyzed_at` is **explicitly NOT** part of the hash — wall-clock would defeat the determinism property.
- Two live runs of the same wallet at different times that captured identical activity will produce identical hashes (correctly, because the underlying data is identical). Two runs that captured DIFFERENT activity (some new activity appeared) will produce different hashes (correctly, because the underlying data moved).

### 3.2 Atomicity — supersede + insert are a single transaction

The writer's two-step operation (mark prior active rows superseded, then insert new active rows) must be atomic. If the supersede succeeds but the insert fails (network blip, JSON validation failure, RLS rejection), the wallet ends up with zero active rows — a worse state than not running.

Mechanism: PostgREST supports `Prefer: tx=rollback` for dry-runs and `Prefer: tx=...` for explicit transaction handling. Concretely:

- Both statements are submitted in a single `POST /rpc/persist_flipper_tag_grains` call backed by a Postgres function, OR
- Both statements are submitted in a single batched request body and the writer rejects partial success at the application layer.

The chosen mechanism is decided in Phase 1.2 (writer implementation) — the spec just requires the property.

### 3.3 Writer skips

The writer **does not write** if:

- Phase 1.1 schema migration has not been applied (schema probe — same pattern as the KSG upsert script's Phase-3 probe).
- The analyzer was run with `--from-fixture` AND the fixture file doesn't exist on disk (replay-only mode; fixture hash undefined → no supersession key).
- `report.walk_full_history` is False (partial fixtures contribute biased peer signal).
- `report.flipper_branch.is_active` is False (no flipper data to write).

Per-grain skip:

- Grains with `n_at_grain == 0` are skipped (no signal value; reduces noise rows).

---

## 4. Self-contamination, supersession, and cross-version safety

The peer-baseline-fn (Phase 0 §3.1) must exclude the wallet under analysis AND must not mix data from different protocol versions. Concretely:

```sql
SELECT AVG(grain_roi) AS mean_flip_roi, COUNT(*) AS peer_count
FROM wallet_flipper_tag_grain
WHERE grain = $1 AND tag = $2
  AND wallet != $3                       -- self-exclusion
  AND n_at_grain >= 30                   -- peer-set threshold (Phase 0 §3.2)
  AND walk_full_history = true           -- only fully-walked wallets contribute
  AND status = 'active'                  -- exclude superseded rows
  AND protocol_version = $4;             -- cross-version safety
```

**`protocol_version = $4` is mandatory, not optional.** The flipper-branch tag-grain semantics differ across protocol versions: v1 used unweighted ROI, v2 (current) uses pair-resampled ratio bootstrap, a future v3 might change the unit. Aggregating across versions produces a silently-wrong peer mean. The caller passes the running analyzer's own `report.protocol_version` so the peer set is matched.

Supersession via `wallet_fixture_hash` rather than `valid_to_commit` because flipper tag grains are tied to which set of activity rows produced them, not which code version analyzed them. A code change that doesn't move fixture content shouldn't invalidate prior grains. (A change that DOES touch flipper math bumps `protocol_version`; the `protocol_version=$4` filter then partitions peer sets cleanly.)

### Invariants enforced at the writer

- `status='active'` rows MUST have `superseded_by_fixture_hash IS NULL`.
- `status='superseded'` rows MUST have `superseded_by_fixture_hash IS NOT NULL`.
- These invariants are check-constraint candidates for the migration (Phase 1.1).

---

## 5. Population strategy

Phase 1 does not include a wide re-analysis. The table starts empty and populates as:

1. New analyzer runs in the normal workflow (e.g. continued calibration work, future per-wallet analyses) write through `persist_flipper_tag_grains`.
2. The v3 calibration set's 10 wallets are obvious early candidates — they're already running through the protocol and their flipper-branch tag grains are observable in test fixtures. Running them through `--persist` (one operator-authorized batch) would seed ~30 wallets-worth of grain rows.
3. Population grows organically. The aggregator (Phase 2) returns `None` for any (grain, tag) where `peer_count < 10` — fails closed, no false positives.

The peer-population threshold `MIN_PEERS_FOR_INFERENCE = 10` (Phase 0 §6 risk #1) means we need at least 10 distinct wallets analyzed in a given (grain, tag) before that grain can produce a real FH4 verdict. For `leaf/geopolitics`, where `wallet_tag_pnl` shows 50 wallets historically have positions, this is reachable inside ~10–20 deliberate analyzer runs.

---

## 6. Phasing within this Phase 1

| Step | Deliverable | Risk |
|---|---|---|
| 0 | This spec | none |
| 1 | Reviewed Supabase migration creating `wallet_flipper_tag_grain` + rollback. **Written but not applied** until §2 recommendation is ratified. | medium — new table; additive but DDL is destructive on rollback |
| 2 | `persist_flipper_tag_grains` writer + unit tests. Wire `--persist` flag through `run_wallet_protocol.py`. **No automatic invocation** — default off. | low — additive code path |
| 3 | Operator-authorized seed: re-run the v3 calibration set's 10 wallets with `--persist`. Expect ~200-500 grain rows. | medium — first real DB writes from analyzer |
| 4 | Population check: `SELECT grain, tag, COUNT(*) FROM wallet_flipper_tag_grain WHERE status='active' GROUP BY grain, tag HAVING COUNT(*) >= 10 ORDER BY 3 DESC` — report which (grain, tag) pairs have crossed the peer-count floor. | low — read-only |

Step 4 produces the gate for Phase 2 (aggregator). If no (grain, tag) crosses 10 peers, Phase 2 design must factor that in — possibly with a different population strategy or a lower threshold.

---

## 7. Risks and open questions

### Risks

1. **Re-running 10 wallets ≠ "no analyzer batches" guardrail.** This is a 10-wallet authorized seed, not a batch. Operator explicit invocation per run. Documented as such.
2. **Schema-migration freshness.** Adding the table before any writer exists is fine, but if a future migration touches `wallet_flipper_tag_grain` we need to remember to update the writer.
3. **Goldsky activity coverage.** Wallets with truncated activity (`walk_full_history = false`) contribute biased flip ROI estimates. The writer skips them; the read-side WHERE clause double-defends.
4. **Recency drift.** A wallet analyzed 6 months ago and never refreshed pollutes the peer mean with stale behavior. Mitigation: `analyzed_at` is recorded; the Phase 2 aggregator can add a recency filter (e.g. last 180 days) without schema change.

### Open questions (deferred to Phase 2)

1. Whether to add a `wallet_fixture_hash` index for fast supersession lookups (depends on how often a wallet gets re-analyzed in practice; index is cheap regardless).
2. Whether `wallet_tag_pnl` (SQLite, 150 wallets) should eventually move to Supabase and merge with this table. Currently they serve different consumers; cross-platform consolidation is out of scope.
3. Whether to also persist held-branch tag grains in a parallel table for symmetry. Held grains DO get an inference verdict today via `held_analyst.py`; persisting them would let downstream consumers cite specific grain rows. Probably yes eventually, but Phase 1 is flipper-only because flipper is what's broken.

---

## 8. Cross-references

- Phase 0 (parent): `docs/superpowers/specs/2026-05-15-flipper-tag-grain-inference-design.md` (PR #45, merged `e61adbb`)
- Triggering audit: `docs/research/wallet_intelligence_workflow/2026-05-15-category-truth-audit-eatpraylove-hwr22e4.md`
- Existing analyzer-output table: `migrations/supabase_016_wallet_analysis_results.sql`
- Existing held/combined per-tag table: `wallet_tag_pnl` (SQLite — see `scripts/brain_index.py schema wallet_tag_pnl`)
- Code site for the writer: `sniper/analysis/wallet_protocol/ledger/`
- Inference site (downstream consumer): `sniper/analysis/wallet_protocol/flipper.py:_build_fh4_grain`
- KSG discipline: `docs/superpowers/specs/2026-05-15-branch-close-knowledge-sync-design.md`
