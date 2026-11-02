# Wallet Taxonomy Migration — From Hardcoded Keywords to Opaque Gamma Tags

**Date:** 2026-04-23
**Status:** spec draft, pre-implementation
**Parent principle:** [v2 execution profile spec §3.1 — opaque tags, no hardcoded taxonomies](./2026-04-23-wallet-execution-profile-v2-drift-to-resolution.md)
**Motivating audit:** [hardcoded taxonomy audit](../../research/scopes/2026-04-23-hardcoded-taxonomy-audit.md)

---

## 1. Goal

Replace every place in the wallet-analysis path where a market's "kind" is inferred from hardcoded keyword matching with a data-driven lookup against a new `market_tags` table (raw gamma tag slugs, treated as opaque strings).

The target end state:
- No code in the wallet-analysis path branches on specific tag values
- No `"sports"` / `"political"` / `"crypto"` / `"other"` labels survive as first-class fields
- Classification rules use distribution properties (concentration, dominance, n) of whatever tags are present — never named tags
- Polymarket's taxonomy can evolve without code changes

## 2. Scope (in and out)

**In scope:**
- `scripts/wallet_archetype_extractor.py` — every function that calls `_classify_title` or reads `primary_domain`
- `scripts/wallet_profiler.py` — legacy profiler that imports the same KEYWORDS
- `scripts/gate0_audit.py` — separate consumer of wallet_profiler
- `tests/test_wallet_archetype_extractor.py` — ~18 assertions that bake in the current vocabulary
- `docs/research/wallet_dossiers/*.md`, `knowledge/wallets/*.md`, the research memos I wrote this session — text references to the old names
- Supabase `reference/vocabulary/wallet-mechanism-buckets` entry — will need a pass to ensure no rot

**Out of scope:**
- `scripts/reference_price.py:_CRYPTO_KEYWORDS` — different purpose (pick a price feed for BTC/ETH markets), different fix (per-market feed metadata). Separate spec.
- `sim/market_type.py:_TYPE_TO_GROUP` — backtest/paper-trading categorisation. Different data path. Separate spec.
- `sniper/settings/paper.py` PAPER_*_KEYWORDS — strategy-level logic. Different fix (per-strategy filter objects). Separate spec.
- `news_intel.py`, `feeds/news.py`, `scripts/brain_health.py` keyword lists — intel/health pipelines. Different fix. Separate spec.

The taxonomy audit lists all of these as backlog. This migration targets the wallet-analysis path only so we have a completed instance to reference when tackling the others.

## 3. Current-state inventory

### 3.1 Producers (where classification originates)

| File:line | Function | What it produces |
|-----------|----------|------------------|
| `wallet_profiler.py:390` | `SPORT_KEYWORDS` (dict) | sport-name → keyword list mapping |
| `wallet_profiler.py:406` | `POLITICAL_KEYWORDS` (list) | generic political words |
| `wallet_profiler.py:410` | `CRYPTO_KEYWORDS` (list) | generic crypto words |
| `wallet_archetype_extractor.py:212` | `_classify_title(title)` | `"sports"` / `"political"` / `"crypto"` / `"other"` |
| `wallet_archetype_extractor.py:226` | `_classify_title_subcategory(title)` | sport-slug / `"sports_other"` / `"political"` / `"crypto"` / `"other"` |

### 3.2 Intermediate consumers (compute aggregates from the producers)

| File:line | Function | Uses |
|-----------|----------|------|
| `wallet_archetype_extractor.py:395` | `_domain_metrics(rows)` | → `{primary_domain, distribution, concentration, distinct_titles}` |
| `wallet_archetype_extractor.py:415` | `_horizon_view_from_counts(...)` | same envelope for per-horizon slices |
| `wallet_archetype_extractor.py:428` | `_count_domain_rows(rows)` | per-row classification counts |
| `wallet_archetype_extractor.py:443` | `_realized_pnl_horizon_view(resolved)` | PnL-weighted domain view |
| `wallet_archetype_extractor.py:~496` | `_domain_horizon_metrics(...)` | wrapper combining the three horizons |

### 3.3 Rule-level consumers (classification / edge hypotheses branch on domain values)

| File:line | Rule | Branch condition |
|-----------|------|------------------|
| `wallet_archetype_extractor.py:729` | `sports_specialist_accumulator` Layer 1 classifier | `primary_domain == "sports" AND concentration >= 0.45 AND trade_pairs < 0.15 AND sell_ratio < 0.15` |
| `wallet_archetype_extractor.py:763` | Layer 1 fallback `mixed_unresolved` rationale | Includes `primary_domain` string in output |
| `wallet_archetype_extractor.py:789-800` | `classify_archetype_layer2` | Reads `distinct_titles`, `concentration` from `domain` (no equality check, but reads the struct) |
| `wallet_archetype_extractor.py:968-976` | `_build_edge_hypotheses` | Reads `concentration`, `distinct_titles` from `domain` |
| `wallet_archetype_extractor.py:895` | `_OBSERVATION_BLOCKS` | Includes `"domain_horizons"` in the block list |
| `wallet_archetype_extractor.py:1397` | `_build_outcome_attribution` per-position | Stamps `"category"` field with `_classify_title(title)` result |
| `wallet_archetype_extractor.py:1414` | `_build_outcome_attribution` grouping | Groups realized PnL by `_classify_title` category |

### 3.4 Legacy callers (outside the archetype extractor)

| File | Path | Usage |
|------|------|-------|
| `scripts/wallet_profiler.py:434` | `analyze_domain` | Self-use in the legacy profiler |
| `scripts/wallet_profiler.py:554` | `analyze_performance` | Per-category PnL breakdown |
| `scripts/wallet_profiler.py:731` | `save_to_brain` | Writes category-keyed rows |
| `scripts/gate0_audit.py` | imports wallet_profiler | Runs the audit pipeline using its classifier |

### 3.5 Tests

- `tests/test_wallet_archetype_extractor.py` — ~18 assertions that bake in `"sports"` / `"political"` / `"crypto"` / `"other"` strings. Sample: `assert horizons["recent_trade"]["primary_domain"] == "sports"` (line 875).

### 3.6 Docs / knowledge

- `knowledge/wallets/0xdbade4c82fb72780a0db9a38f821d8671aba9c95.md` (SwissMiss — narrates with the old vocabulary, already partially annotated)
- `docs/research/wallet_dossiers/2026-04-22-swissmiss.md`
- `docs/research/wallet_dossiers/2026-04-22-kch123.md`
- `docs/research/WALLET_ARCHETYPE_DEEP_READ_2026-04-21.md`
- `docs/research/prior_art/2026-04-22-polymarket-wallet-research-landscape.md`
- `docs/research/scopes/2026-04-22-execution-mve-arb-control.md` (already has a retraction box for directional claim, may need another for domain-vocabulary)
- `docs/research/scopes/2026-04-23-execution-profile-rollout.md` (refers to SwissMiss's `sports_specialist_accumulator` classification — that classification may change post-migration)

### 3.7 Supabase

- `reference/vocabulary/wallet-mechanism-buckets` — names buckets; doesn't hardcode tags but references them indirectly. Needs audit.
- `patterns/swissmiss-long-horizon-selector-2026-04-23` — references SwissMiss's classification; may need update if she re-classifies.

## 4. Target-state data model

### 4.1 New table: `market_tags`

```sql
CREATE TABLE IF NOT EXISTS market_tags (
    condition_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    tag_label TEXT,             -- optional human-readable label from gamma
    ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (condition_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_market_tags_tag ON market_tags(tag);
```

- `tag` = gamma `tags[].slug` verbatim. No normalisation beyond lowercasing.
- `tag_label` = optional `tags[].label` for UI / debugging; never used in logic.
- Composite primary key allows natural multi-tag membership.

### 4.2 Extractor output shape — new fields

Replace `features["domain"]` and `features["domain_horizons"]` with a tag-based equivalent:

```json
"domain_tags": {
  "tag_fill_counts": {"<slug>": 42, "<slug>": 17, ...},   // count per distinct tag across wallet's fills
  "primary_tag": "<slug>",                                  // modal tag by count
  "primary_tag_share": 0.38,                                // fraction of fills carrying primary_tag
  "distinct_tag_count": 23,                                 // total distinct tags observed
  "distinct_titles": 87,                                    // (preserved)
  "unclassified_share": 0.07,                               // fraction of fills on markets with zero tags
  "top_tags": [                                             // top-5 by fill count, for UI/quick-read
    {"tag": "<slug>", "share": 0.38, "count": 42},
    ...
  ]
}
```

And per-horizon slices that mirror the old three-horizon split without introducing coarse buckets:

```json
"domain_tags_horizons": {
  "recent_trade":  {... same shape as domain_tags ...},
  "live_book":     {... same shape as domain_tags ...},
  "realized_pnl":  {... same shape, but share is |PnL|-weighted ...},
  "alignment": {
     // computed on primary_tag AND on primary_tag_share_overlap (Jaccard on top_tags sets)
     "recent_live_primary_match": true,
     "recent_realized_primary_match": false,
     "top_tag_overlap_jaccard": 0.62
  },
  "rotation_signal": false    // true iff recent ≠ live AND overlap < 0.3
}
```

### 4.3 Layer 1 classifier rule rewrites

The `sports_specialist_accumulator` rule is the only Layer 1 branch that equality-checks a domain value. Rewrite into data-driven form:

```python
# OLD
if primary_domain == "sports" and concentration >= 0.45 and trade_pairs < 0.15 and sell_ratio < 0.15:
    → sports_specialist_accumulator

# NEW
if (
    primary_tag_share >= 0.45                # wallet is concentrated on ONE tag
    and distinct_tag_count <= 8              # and that tag isn't just "one-of-many"
    and trade_pairs < 0.15
    and sell_ratio < 0.15
    and unclassified_share < 0.15            # tags are reliable for this wallet's markets
):
    → tag_specialist_accumulator   # renamed; agnostic to which tag
```

Rationale: the *shape* of the "sports specialist" signal was "wallet concentrates in ONE narrow domain and buys-and-holds." That shape is data-observable without knowing the tag name. A wallet concentrated in `nfl` or `ukraine` or `fed-rates` or whatever future tag exists reads the same way.

The new archetype name `tag_specialist_accumulator` is distribution-shape-based. The rationale list still surfaces `primary_tag` so humans can see *which* tag the wallet specializes in — that's informative output, not code-level logic.

### 4.4 Layer 4 outcome attribution

`_build_outcome_attribution` currently stamps each resolved position with `"category": _classify_title(title)`. Replace with:

```python
{
  "primary_tag": <slug or null>,   // the market's single most-specific gamma tag by some priority rule
  "tags": [<slug>, <slug>, ...]    // full tag set
}
```

Aggregation then reports per-tag realized PnL (one position's PnL contributes to every tag it carries). Dominant-tag selection for `dominant_tags` list uses PnL share, as already specified in the v2 execution profile spec.

**"most-specific tag" selection.** We cannot hardcode a tag hierarchy. Instead use a data-driven proxy: the tag that appears in the FEWEST markets globally (rarer → more specific) among the market's tags. If ties, deterministic alphabetical. This ranks narrow tags (specific events) above broad ones (e.g. `politics`), without assuming what's broad vs narrow.

### 4.5 Output schema compatibility

**Hard break.** `features["domain"]` and `features["domain_horizons"]` are removed; `features["domain_tags"]` and `features["domain_tags_horizons"]` replace them. Any external consumer reading `primary_domain` gets `None`.

Shadow period (Stage 3, see §5) emits BOTH the old and new fields so consumers can migrate. After Stage 5, only the new fields remain.

## 5. Migration stages

Each stage is one commit, independently shippable, with pytest green before merge. No stage depends on a later stage to avoid regression.

### Stage 1 — `market_tags` table + backfill

**What lands:**
- Migration file creating `market_tags` table.
- Extend `polymarket_api._fetch_event_category_by_condition` to ALSO write raw tag slugs into `market_tags` alongside its current collapsed-category write (no deletion yet).
- New script `scripts/backfill_market_tags.py`: paginate gamma `/events`, insert tags for every `conditionId` into `market_tags`. Idempotent (use `INSERT OR IGNORE`).
- Tests:
  - `market_tags` table creation
  - gamma-response-fixture → tags extracted and inserted
  - duplicate insert is a no-op
  - backfill covers all `market_metadata` rows (spot-check counts)

**Risk:** Additive. Zero existing-caller impact.

**Success criteria:**
- `SELECT COUNT(DISTINCT condition_id) FROM market_tags` approaches `SELECT COUNT(*) FROM market_metadata WHERE end_date_iso IS NOT NULL` after backfill.
- At least 80% of wallets in `followed_wallets ∪ watchlist_wallets` have tags available for every conditionId they hold (`unclassified_share < 0.2` per wallet).

### Stage 2 — Extractor emits `domain_tags` alongside legacy `domain`

**What lands:**
- New function `_classify_fill_tags(condition_id) → set[str]`: SQL lookup from `market_tags`. Returns `frozenset()` on miss (caller → unclassified).
- New function `_domain_tags_metrics(rows) → dict`: per-fill tag membership → envelope of §4.2.
- New function `_domain_tags_horizon_metrics(trades, positions, resolved)` mirror of existing `_domain_horizon_metrics`.
- `extract_wallet_archetype` emits BOTH `domain` (legacy) and `domain_tags` (new).
- Tests:
  - `_classify_fill_tags` happy path + miss path
  - `_domain_tags_metrics` distribution shape
  - horizon alignment + rotation signal boolean
  - primary_tag_share boundary cases
  - unclassified_share accounting
- **No consumer reads `domain_tags` yet.** Legacy rules still use `domain`.

**Risk:** Additive. Legacy consumers untouched.

**Success criteria:**
- All existing tests green.
- `features["domain_tags"]` populated on a live wallet run (SwissMiss reference run).
- On the SwissMiss rollout fixtures, `domain_tags.primary_tag` is a real gamma slug (illustrative — whatever dominates her fills), `primary_tag_share > 0`, `unclassified_share < 0.2`.

### Stage 3 — Layer 1 rule rewrite (`tag_specialist_accumulator`) + shadow cutover

**What lands:**
- New rule `tag_specialist_accumulator` (spec §4.3) reading from `domain_tags`. Added to classifier alongside the existing `sports_specialist_accumulator` rule. Both evaluated; the new rule's output attached under `classification_shadow` (second classification, not replacing the live one).
- `_build_edge_hypotheses` reads `domain_tags.distinct_tag_count` and `.primary_tag_share` alongside (not replacing) `domain.distinct_titles` and `.concentration`.
- Tests:
  - Wallet with ≥45% fills on one tag + low trade_pairs + low sell_ratio + low unclassified → `tag_specialist_accumulator` fires
  - Wallet with diffuse tags → rule doesn't fire
  - Wallet with >15% unclassified → rule doesn't fire (tag data not reliable for this wallet)
  - SwissMiss fixture: shadow classification matches or differs from live, document which

**Risk:** Medium. New rule is live but in shadow field; misfires don't affect downstream.

**Success criteria:**
- Shadow classifications emitted for all extractor runs.
- Rollout on `knowledge/wallets/` shortlist produces a shadow-vs-live comparison table for human review.

### Stage 4 — Cutover: shadow → primary, legacy → deprecation warning

**What lands:**
- `classification` (primary) now comes from the tag-based rules.
- `classification_legacy` preserves the KEYWORDS-based rules for one commit's rollback window.
- Layer 4 outcome attribution switches to tag-based per-position grouping.
- Tests: migrate all `primary_domain == "X"` assertions to their tag-based equivalents using the same fixtures. Any test that can't be migrated without semantic change is flagged (likely 2-3 of the 18).
- Pre-cutover validation: run the shadow comparison from Stage 3 on the 18-wallet shortlist; require human sign-off on the classification delta before merging the cutover.
- Documentation pass: update `knowledge/wallets/*.md` SwissMiss + any other wallet whose primary classification changed. Update 2 dossiers. Annotate research memos.

**Risk:** High. This is where classification meaning changes. Consumers of `features["classification"]["archetype"]` that read `"sports_specialist_accumulator"` will either get `"tag_specialist_accumulator"` or something else.

**Success criteria:**
- Every extractor test passes under the new field names.
- Shadow-comparison delta is documented per wallet in the rollout memo.
- Supabase `reference/vocabulary/wallet-mechanism-buckets` entry updated to rename the bucket and explain the data-driven rule.

### Stage 5 — Remove legacy

**What lands:**
- Delete `_classify_title`, `_classify_title_subcategory`, `_domain_metrics`, `_horizon_view_from_counts`, `_count_domain_rows`, `_realized_pnl_horizon_view`, `_domain_horizon_metrics` in the extractor.
- Delete `SPORT_KEYWORDS`, `POLITICAL_KEYWORDS`, `CRYPTO_KEYWORDS` from `wallet_profiler.py`, along with the `analyze_domain` and `analyze_performance` per-category functions. `wallet_profiler.py` stays but loses the legacy classifier path.
- `scripts/gate0_audit.py` migrates to read from `domain_tags` (same pattern as extractor Stage 2) OR is deleted if its last use was the now-removed legacy path.
- Delete `features["domain"]` and `features["domain_horizons"]` from extractor output.
- Delete `classification_legacy` (preserved one commit for rollback).
- Tests: remove any remaining legacy assertions.
- Supabase: final pass on `reference/vocabulary/wallet-mechanism-buckets` and `patterns/swissmiss-long-horizon-selector-2026-04-23` to ensure they describe the tag-based rules.

**Risk:** Medium. Once legacy is deleted, rollback requires a revert commit.

**Success criteria:**
- No grep hit for `SPORT_KEYWORDS`, `POLITICAL_KEYWORDS`, `CRYPTO_KEYWORDS`, `_classify_title`, `primary_domain` in non-test non-doc Python.
- All tests green.
- `python3 scripts/wallet_archetype_extractor.py <any wallet>` runs without reference to the old fields.

## 6. Pre-cutover validation (Stage 3 → Stage 4 gate)

Before Stage 4 is allowed to merge, run the Stage 3 shadow comparison on a defined wallet set:

```bash
# For each wallet in followed_wallets ∪ watchlist_wallets ∪ knowledge/wallets/*
python3 scripts/wallet_archetype_extractor.py <wallet> --emit-shadow
# Compare:
#   classification.archetype (legacy, from KEYWORDS)
#   classification_shadow.archetype (new, from gamma tags)
# Write a per-wallet delta row to data/taxonomy_migration_shadow_diff.csv
```

Human review: eyeball the delta. Wallets whose classification changes need:
- A note in the migration rollout memo
- Possible update to their knowledge page
- Acceptance of the new label

Approved shadow report is a required artifact for Stage 4. Not a human-judgement-free automation.

## 7. Handling the wallet_profiler legacy users

`wallet_profiler.py` is imported by both `wallet_archetype_extractor.py` and `scripts/gate0_audit.py`. Stage 1-5 remove the KEYWORDS only; the rest of `wallet_profiler.py` (profile fetching, gate0 checks, etc.) stays unchanged.

If `gate0_audit.py` reads per-category results, Stage 4 or 5 migrates it the same way the extractor does. If it doesn't, the unused category paths in `wallet_profiler.py` are deleted in Stage 5 cleanup.

Grep before Stage 5:
```bash
grep -rn "analyze_domain\|analyze_performance" --include="*.py" .
```

Any caller still reading per-category output gets migrated; otherwise deleted.

## 8. Rollback strategy

Each stage is one commit. Rollback = `git revert <sha>`. Specifically:
- Stage 1: revert drops the `market_tags` table (migration file). Backfill data is lost but re-runnable.
- Stage 2: revert drops `domain_tags` field. Legacy `domain` still populated.
- Stage 3: revert drops `classification_shadow`. No user impact.
- Stage 4: revert re-enables the legacy classifier as primary. `classification_legacy` is the same field renamed; revert renames it back.
- Stage 5: revert requires re-introducing the deleted code. Hardest to roll back; this is why the pre-cutover validation in §6 is a required artifact.

## 9. Known open questions

1. **What if `domain_tags.unclassified_share > 0.2` on the shadow comparison for many wallets?** Means gamma tag coverage is incomplete and the new rule will often decline to fire. Stage 1 success criterion requires this to be < 20% for the shortlist; if we miss that, backfill needs more aggressive retry (pagination, fallback to `/markets?conditionId=` single lookup).
2. **Tag specificity ranking — is "rarer global frequency ⇒ more specific" a valid proxy?** Empirically plausible (e.g. `ukraine` appears in fewer markets than `politics`), but not rigorously tested. If it produces nonsense `primary_tag` selections in shadow review, alternatives: (a) use tag depth in Polymarket's category hierarchy if gamma exposes it, (b) fall back to alphabetical without the rarity weight. Investigate during Stage 3.
3. **Archetype name `tag_specialist_accumulator` acceptable?** Semantically correct ("wallet specializes in one tag"); operationally reads as generic. An agent seeing this label knows "concentration edge possible" without knowing which tag — which is exactly the opaque-tag principle. Name is subject to change if something better surfaces during Stage 3 review.
4. **What about the `_THESIS_CATEGORY_MAP` in `polymarket_api.py`?** Its output feeds `market_metadata.category` (currently 2-value). Stage 1 adds tags alongside; Stage 5 can deprecate `_THESIS_CATEGORY_MAP` since its job is now done by `market_tags`. Tracked as part of Stage 5 cleanup.
5. **Existing `market_metadata.category` consumers outside the wallet-analysis path.** grep before Stage 5. Anything in `strategies/`, `routes/`, or `sim/` that reads `category` is out of this migration's scope but needs to be catalogued and redirected in a follow-up spec.

## 10. Non-goals (reiterate)

- Not fixing `reference_price.py`, `sim/market_type.py`, `sniper/settings/paper.py`, `news_intel.py`, `feeds/news.py`, `scripts/brain_health.py`, `sniper/db/db_utils.py:crypto_kw`, `sniper/intel/entity_clusters.py`, or any other keyword-list-based classifier outside the wallet-analysis path. The audit catalogued them; each gets its own spec when prioritized.
- Not changing the Polymarket API interactions beyond the single `_fetch_event_category_by_condition` extension.
- Not building a tag-hierarchy or taxonomy UI. Tags stay opaque slugs.

## 11. Bottom line

Five stages. Each is additive until Stage 4, which is the cutover. Stage 3 → Stage 4 requires a signed-off shadow diff artifact so the classification semantics change is conscious, not accidental.

After Stage 5, the wallet-analysis path has zero hardcoded tag/category names in logic. The opaque-tag principle from the v2 execution profile spec applies in practice, not just in policy. The other keyword-list offenders in the audit are separate projects.
