# Wallet Dossier — Market Semantics v1

**Date:** 2026-05-26
**Status:** design locked; ready for /ship
**Supersedes:** §7.4 of `2026-05-25-wallet-dossier-semantics-v0.md` (v0 "always unavailable" rule)
**Schema version:** bumps `DOSSIER_SCHEMA_VERSION` from `"v0_complete"` to `"v1_market_semantics"`

## 0. Why this exists

The v0_complete dossier hardcodes `category = "data_unavailable"` for every position via `derivers.derive_category`. Three empirically-probed wallet archetypes (NO-heavy losing wallet, OTHER-heavy profitable SwissMiss, extreme-OTHER $12.87M PnL wallet) all produce **0 / 234+ mechanism promotions** because §8 gate 3 (category homogeneity, real string required) can never pass. This blocks `candidate_thesis_evidence` system-wide.

The infrastructure to fix this is already in main as of `fd44a31` (2026-05-22 merge of `codex/market-tags-provisioning`): the `market_tags` and `tag_nav_category` SQLite tables exist; `tag_inference.is_skip_tag` exists; `data/tag_nav_category_seed.yaml` exists. But `derivers.derive_category` still ignores all of it — line `v0_does_not_consume_leafs` is literally hardcoded.

This spec is the consumer-side wiring that lets the dossier respect tags when they are present in the fixture, refuse to claim a category when they are absent, and preserve the determinism contract.

## 1. Scope

### In scope (v1)

- `derive_category` consumes `leafs_by_cid` and `nav_lookup` to resolve a nav category per position.
- `market_semantics` gains a `leaf_tags_json` column preserving raw post-skip leaves for v2 specialist-grain analysis.
- New `category_unavailable_reason` vocabulary distinguishes 6 honest-unavailable states.
- `_record_market_semantics_lineage` switches on actual row state instead of hardcoding `source="data_unavailable"`.
- `_build_data_gaps` renames the gap to `market_category_unavailable` and emits count-based affected_grain_count.
- New utility `scripts/refresh_fixture_tags.py` writes a tagged copy of an existing fixture (read-only DB query) so the three-step pipeline keeps WalletData → ProtocolReport → Dossier consistent.

### Out of scope (carry forward)

- Operator backfill of `market_tags` on the local `sniper_tracker.db`. Required before empirical proof; separate operator task.
- Leaf-tag specialist grain (the `leaf_tags_json` column is preserved for v2; not consumed as a grain key dimension in v1).
- Sibling-fill cross-reference / Gamma `/events` market-structure metadata for the `multi_outcome_undercounted_when_wallet_one_sided` blocker.
- `sibling_token_prices_at_entry` (favorite/longshot rank).
- Strategy Pattern Audit consuming dossiers.
- Simulator / thesis validation.
- Narrative wallet reports.

### Hard rules (carried from v0 + reaffirmed)

- `accounting_bucket` NEVER enters any grain key (§9 hard rule).
- Vocabulary blocklist (§9 + §11.7) clean in markdown AND DB TEXT cells.
- No live API calls from `derive_category` or `build_tables`.
- No DB fallback when `nav_lookup` is missing from a fixture — refuse with `category_unavailable_reason="nav_lookup_missing_from_fixture"`. Fixture input is the deterministic source of truth.

## 2. Schema changes

### 2.1 `DOSSIER_SCHEMA_VERSION`

```python
# sniper/analysis/wallet_dossier/schema.py:14
DOSSIER_SCHEMA_VERSION = "v1_market_semantics"  # was "v0_complete"
```

### 2.2 `market_semantics` — new column

| Column | Type | Source | Notes |
|---|---|---|---|
| `leaf_tags_json` | TEXT (JSON array) | fixture_field | post-skip, post-ambiguous-filter leaf tags, sorted alphabetically for byte-determinism. **NULL** when no usable leaf survives. |

Full DDL (unchanged columns omitted):

```sql
ALTER TABLE market_semantics ADD COLUMN leaf_tags_json TEXT;
```

(Live in the new DDL list in `SCHEMA_DDL`; existing CREATE TABLE statement updated.)

### 2.3 `data_gaps` — gap renamed + count-based emission

The v0 `market_tags_not_provisioned` gap was emitted unconditionally with `affected_grain_count = n_positions`. In v1:

- **Gap name:** `market_category_unavailable` (replaces `market_tags_not_provisioned`).
- **Emission rule:** count actual rows in `market_semantics` where `category == "data_unavailable"`. If count is 0, do NOT emit the gap row.
- **Severity:** `high` if affected ≥ 50% of positions; `medium` if 10–50%; `low` if 0 < affected < 10%.

## 3. Derivation rule §7.4 v1 — nav category resolution

```
derive_category(condition_id, leafs_by_cid, nav_lookup):
    """
    Returns (category, category_source, category_unavailable_reason, leaf_tags_sorted).

    Filter order:
      1. ambiguous filter — drop tags in AMBIGUOUS_TAGS
      2. skip-list filter — apply tag_inference.is_skip_tag
      3. nav mapping — leaf → nav via nav_lookup
      4. tie-break — alphabetical first on the resulting unique nav set
    """
    cid_lc = condition_id.lower()

    # --- presence check ---
    if not leafs_by_cid:
        return ("data_unavailable", "data_unavailable",
                "market_tags_table_empty", None)

    if cid_lc not in {k.lower() for k in leafs_by_cid}:
        return ("data_unavailable", "data_unavailable",
                "tag_not_found_in_leafs", None)

    raw_leafs = leafs_by_cid[cid_lc]

    # --- 1. ambiguous-first filter ---
    # AMBIGUOUS_TAGS is imported from tag_inference (frozenset; v0 contains
    # {"awards", "world", "tech"}). We do NOT call resolve_membership with
    # ambiguous_remap_to_other=True — dossier semantics refuse to claim
    # rather than route to "other".
    only_ambiguous = (
        len(raw_leafs) > 0
        and all(t in AMBIGUOUS_TAGS for t in raw_leafs)
    )
    if only_ambiguous:
        return ("data_unavailable", "data_unavailable",
                "ambiguous_tag_pending_operator_decision", None)

    non_ambiguous = [t for t in raw_leafs if t not in AMBIGUOUS_TAGS]

    # --- 2. skip-list filter ---
    surviving = [t for t in non_ambiguous if not is_skip_tag(t)]
    if not surviving:
        return ("data_unavailable", "data_unavailable",
                "all_leafs_skipped", None)

    leaf_tags_sorted = sorted(surviving)  # alphabetical, byte-stable

    # --- 3. nav lookup ---
    if not nav_lookup:
        # Fixture-deterministic refusal: no DB fallback.
        return ("data_unavailable", "data_unavailable",
                "nav_lookup_missing_from_fixture", leaf_tags_sorted)

    navs = {nav_lookup[t] for t in surviving if t in nav_lookup}
    if not navs:
        return ("data_unavailable", "data_unavailable",
                "no_nav_mapping_for_any_leaf", leaf_tags_sorted)

    # --- 4. tie-break (alphabetical; arbitrary-deterministic, NOT semantic priority) ---
    category = sorted(navs)[0]
    return (category, "tag_nav_category", None, leaf_tags_sorted)
```

### 3.1 `category_source` vocabulary (final)

| Value | When emitted |
|---|---|
| `tag_nav_category` | nav category resolved successfully |
| `data_unavailable` | any unavailable case (with `category_unavailable_reason` specifying which) |

`market_tags_leaf` is intentionally NOT in v1 vocab — leaf-grain promotion is v2.

### 3.2 `category_unavailable_reason` vocabulary (final, 6 values)

| Value | Trigger condition |
|---|---|
| `market_tags_table_empty` | `leafs_by_cid == {}` |
| `tag_not_found_in_leafs` | `leafs_by_cid` non-empty, this condition_id absent |
| `nav_lookup_missing_from_fixture` | leafs present, but `nav_lookup == {}` |
| `all_leafs_skipped` | every leaf hits `is_skip_tag` (after ambiguous filter) |
| `no_nav_mapping_for_any_leaf` | survivors exist but none map via `nav_lookup` |
| `ambiguous_tag_pending_operator_decision` | every leaf is in `AMBIGUOUS_TAGS` |

### 3.3 `leaf_tags_json` population rule

| Case | `leaf_tags_json` |
|---|---|
| `leafs_by_cid == {}` | NULL |
| condition_id absent from leafs_by_cid | NULL |
| all leaves skipped | NULL |
| all leaves ambiguous | NULL |
| ≥1 usable leaf (mapped to nav) | `JSON sorted(surviving)` |
| ≥1 usable leaf (no nav mapping) | `JSON sorted(surviving)` — preserved for v2 |

Sort order: standard Python `sorted()` on strings (lexicographic by Unicode code point). Deterministic.

### 3.4 Tie-break note

Alphabetical first on the resulting nav category set. This is **arbitrary-deterministic, not semantic priority**. The seed file (`data/tag_nav_category_seed.yaml`) is a YAML dict; insertion order is not a contract. Operator-visible: anyone reading the seed can predict the v1 outcome.

If `awards`/`world`/`tech` (ambiguous) are ever resolved by the operator (moved into `mappings`), the ambiguous filter no longer drops them — they flow into the nav-lookup step like any other tag.

## 4. Lineage rules

`_record_market_semantics_lineage` (currently in `builder.py:723-734`) switches on actual row state instead of hardcoding `source="data_unavailable"`:

```python
if ms_row["category"] != "data_unavailable":
    # Resolved nav category
    tracker.record(
        table="market_semantics", column="category", row_key=pid,
        source="derived", confidence="high",
        source_detail="WalletData.leafs_by_cid[<cid>] + WalletData.nav_lookup",
        derivation_rule="§7.4 v1 nav-category resolution",
    )
    tracker.record(
        table="market_semantics", column="category_source", row_key=pid,
        source="derived", confidence="high",
        source_detail="tag_nav_category resolution",
        derivation_rule="§7.4 v1",
    )
else:
    # Data unavailable — use the actual unavailable reason for BOTH columns
    # (existing v0 code hardcoded "market_tags_not_provisioned" for
    # category_source even when the real reason was different — fixed here)
    tracker.record(
        table="market_semantics", column="category", row_key=pid,
        source="data_unavailable", confidence="high",
        unavailable_reason=ms_row["category_unavailable_reason"],
        derivation_rule="§7.4 v1",
    )
    tracker.record(
        table="market_semantics", column="category_source", row_key=pid,
        source="data_unavailable", confidence="high",
        unavailable_reason=ms_row["category_unavailable_reason"],
        derivation_rule="§7.4 v1",
    )

if ms_row["leaf_tags_json"] is not None:
    tracker.record(
        table="market_semantics", column="leaf_tags_json", row_key=pid,
        source="fixture_field", confidence="high",
        source_detail="WalletData.leafs_by_cid[<cid>] (post-skip, post-ambiguous, alphabetical)",
        derivation_rule="§7.4 v1 leaf preservation",
    )
```

## 5. Builder integration

`build_tables` (currently at `builder.py:141`) reads `nav_lookup` alongside `leafs_by_cid`:

```python
# builder.py around line 165
leafs_by_cid = wallet_data.get("leafs_by_cid") or {}
nav_lookup = wallet_data.get("nav_lookup") or {}
```

`derive_category` call site (currently at `builder.py:265`) passes `nav_lookup` and unpacks the 4-tuple:

```python
category, category_source, category_unavailable_reason, leaf_tags_sorted = derive_category(
    condition_id=condition_id,
    leafs_by_cid=leafs_by_cid,
    nav_lookup=nav_lookup,
)
leaf_tags_json = json.dumps(leaf_tags_sorted) if leaf_tags_sorted else None
```

`market_semantics` row population includes the new column:

```python
ms_row = {
    ...,
    "category": category,
    "category_source": category_source,
    "category_unavailable_reason": category_unavailable_reason,
    "leaf_tags_json": leaf_tags_json,
    ...,
}
```

## 6. Fixture-refresh utility

New file: `scripts/refresh_fixture_tags.py`.

### CLI

```bash
python3 scripts/refresh_fixture_tags.py \
    --fixture <input.json> \
    --db <sniper_tracker.db> \
    --out <tagged.json> \
    [--apply-skip-list]    # default: ON; reuses tag_inference.is_skip_tag
```

### Behavior

1. Reads input fixture JSON; deep-copies.
2. Extracts all unique `condition_id` values from `closed_positions + open_positions + activity_rows + redemptions`.
3. Queries `market_tags` table for each `condition_id` (single bulk query via `db.fetch_market_tags_for_cids` when available; else SELECT IN-list).
4. Queries `tag_nav_category` for all distinct leaf tags returned.
5. Applies `tag_inference.is_skip_tag` filter to populate `leafs_by_cid` (post-skip).
6. Populates `nav_lookup` from `tag_nav_category` rows.
7. Writes JSON to `--out` with these two fields populated. Never mutates input file.
8. Logs stats: `n_conditions_total`, `n_conditions_with_leafs`, `n_conditions_unmapped`, `n_leafs_skipped`, `n_leafs_ambiguous`, `n_nav_mappings`.

### Constraints

- Read-only DB access (no UPDATE / INSERT).
- Does NOT call live APIs (Gamma, Polymarket).
- Reuses `tag_inference.is_skip_tag` — no second filter.
- Exit non-zero only on input read error, DB connection failure, or output write failure. Empty results (zero tags found) is exit 0 with a clear log line.

## 7. Three-step pipeline (operator-runnable)

```bash
# Step 1: refresh fixture with tags (DB read only, no live API)
python3 scripts/refresh_fixture_tags.py \
    --fixture tests/calibration/fixtures-v4-staging/<wallet>_<date>.json \
    --db <populated_sniper_tracker.db> \
    --out /tmp/<wallet>_<date>.tagged.json

# Step 2: regenerate ProtocolReport from the tagged fixture
python3 scripts/run_wallet_protocol.py <wallet> \
    --from-fixture /tmp/<wallet>_<date>.tagged.json \
    --json --no-peers --no-exec --no-history --no-persist \
    > /tmp/<wallet>_<date>.report.json

# Step 3: build dossier from MATCHING tagged fixture + report
python3 scripts/build_wallet_dossier.py <wallet> \
    --fixture /tmp/<wallet>_<date>.tagged.json \
    --protocol-report /tmp/<wallet>_<date>.report.json \
    --out-db /tmp/<wallet>_<date>.sqlite \
    --out-md /tmp/<wallet>_<date>.md
```

WalletData (tagged) → ProtocolReport (sees tagged input) → Dossier (consumes matching pair). Triangle internally consistent.

## 8. Tests

### T1 — tagged path resolves nav category

Synthetic fixture with one cid, `leafs_by_cid={"0xa…": ["microstrategy"]}`, `nav_lookup={"microstrategy": "crypto"}`. Assert:

- `market_semantics.category == "crypto"`
- `category_source == "tag_nav_category"`
- `category_unavailable_reason IS NULL`
- `leaf_tags_json == '["microstrategy"]'`

### T2a — `market_tags_table_empty` reason

`leafs_by_cid={}`. Assert:

- `category == "data_unavailable"`
- `category_unavailable_reason == "market_tags_table_empty"`
- `leaf_tags_json IS NULL`

### T2b — `tag_not_found_in_leafs` reason

`leafs_by_cid={"0xother…": ["sports"]}` (some other cid present, target cid absent). Assert:

- `category == "data_unavailable"`
- `category_unavailable_reason == "tag_not_found_in_leafs"`
- `leaf_tags_json IS NULL`

### T3 — multi-leaf alphabetical tie-break

Leafs `["microstrategy", "stocks", "soccer"]`, nav_lookup mapping them to `["economy", "economy", "sports"]`. Unique navs `{economy, sports}`. Assert:

- `category == "economy"` (alphabetical first)
- `leaf_tags_json == '["microstrategy","soccer","stocks"]'` (alphabetical sort of post-skip leaves)

### T4 — all skipped

Leafs `["hide-from-new", "recurring"]` (both in `_SKIP_LITERALS`). Assert:

- `category == "data_unavailable"`
- `category_unavailable_reason == "all_leafs_skipped"`
- `leaf_tags_json IS NULL`

### T5 — unmapped leaf preserved

Leaf `["unknown-new-tag"]` not in `nav_lookup`. Assert:

- `category == "data_unavailable"`
- `category_unavailable_reason == "no_nav_mapping_for_any_leaf"`
- `leaf_tags_json == '["unknown-new-tag"]'` (preserved for v2)

### T6 — ambiguous-only pending

Leafs `["awards"]` (in `AMBIGUOUS_TAGS`). Assert:

- `category == "data_unavailable"`
- `category_unavailable_reason == "ambiguous_tag_pending_operator_decision"`
- `leaf_tags_json IS NULL`
- The implementation must NOT route ambiguous tags to "other". This is asserted by checking the outcome (`category_source == "data_unavailable"` and reason vocab matches), not by mock-spying.

### T7 — `nav_lookup` missing on fixture (deterministic refusal)

Leafs `["microstrategy"]` present, but `nav_lookup={}`. Assert:

- `category == "data_unavailable"`
- `category_unavailable_reason == "nav_lookup_missing_from_fixture"`
- `leaf_tags_json == '["microstrategy"]'` (preserved)
- No DB query made during derivation (assert via passing `nav_lookup={}` explicitly and asserting outcome, not mock-spy).

### T8 — §9 hard-rule regression

After v1 category resolution lands, `accounting_bucket` STILL never appears as a grain key dimension in any `result_semantics` or `mechanism_evidence` row. Existing §9 hard-rule tests must continue to pass.

### T9 — lineage coverage including `leaf_tags_json`

For every row in `market_semantics` where `leaf_tags_json IS NOT NULL`, at least one `data_lineage` row exists with `table="market_semantics"`, `column="leaf_tags_json"`, matching `row_key`, and `source="fixture_field"`.

### T10 — data gap renamed + count-based

Synthetic build where 30% of positions resolve and 70% are `data_unavailable`. Assert:

- `data_gaps` contains a row with `gap_name == "market_category_unavailable"`.
- `affected_grain_count == count of market_semantics rows with category == "data_unavailable"`.
- Severity `high` (since 70% ≥ 50%).
- `gap_name == "market_tags_not_provisioned"` does NOT appear (renamed).

When build is 100% resolved: gap NOT emitted at all.

### T11 — schema version bumped

`DOSSIER_SCHEMA_VERSION == "v1_market_semantics"`. `dossier_meta` row with `key="schema_version"` carries the new value.

### T12 — update existing SwissMiss real-build smoke test (test_wallet_dossier_v0_micro.py:1067)

Drop the unconditional `assert r["category"] == "data_unavailable"`. Replace with: if the input fixture's `leafs_by_cid` is empty (current state of v4 staging fixtures), all rows MAY remain `data_unavailable`; otherwise the proportion of resolved rows must be > 0. Keep the test green regardless of whether the fixture carries tags.

### T13 — `refresh_fixture_tags.py` smoke

Build a tiny in-memory SQLite with `market_tags(condition_id, tag)` and `tag_nav_category(tag, nav_category)` rows. Run `scripts/refresh_fixture_tags.py` against a synthetic input fixture. Assert:

- Output fixture has `leafs_by_cid` populated for cids that had tag rows.
- Output `leafs_by_cid` does NOT contain skip-list tags (e.g. `hide-from-new`).
- Output `nav_lookup` is populated from `tag_nav_category` rows.
- Input fixture file is byte-identical before and after the run (no mutation).
- Re-running with the same inputs produces a byte-identical output (deterministic).

## 9. Acceptance criteria

1. All 13 new/updated tests pass.
2. All 153 existing v0_complete dossier tests still pass (regression-free).
3. `python3 scripts/validate_schema_refs.py` clean.
4. `git diff --check origin/main..HEAD` clean.
5. `DOSSIER_SCHEMA_VERSION` returns `"v1_market_semantics"` from `dossier_meta`.
6. All 6 `category_unavailable_reason` vocab values exercised by tests T2a, T2b, T4, T5, T6, T7.
7. The v0 spec (`2026-05-25-wallet-dossier-semantics-v0.md`) gets a §7.4 patch pointing at this v1 spec.
8. §9 hard-rule blocklist tests still pass on both markdown and DB TEXT cells.

## 10. Out of scope (explicit)

- Operator backfill of `market_tags`.
- Live API calls of any kind.
- Leaf-tag specialist grain.
- Multi_outcome undercount mitigation.
- Sibling token prices at entry.
- Strategy Pattern Audit.
- Specialist Consensus.
- Simulator.
- Narrative reports.
- LLM auto-summary (`--llm-summary` template unchanged).

## 11. Implementation sequencing

1. Bump `DOSSIER_SCHEMA_VERSION` + add `leaf_tags_json` column DDL.
2. Update `derive_category` signature + body (Section 3).
3. Update `build_tables` to read `nav_lookup` and pass it (Section 5).
4. Update `_record_market_semantics_lineage` (Section 4).
5. Update `_build_data_gaps` (Section 2.3).
6. Write `scripts/refresh_fixture_tags.py` (Section 6).
7. Write the 13 tests (Section 8); ensure all are RED first; then green via steps 1-6.
8. Update existing SwissMiss test at `tests/test_wallet_dossier_v0_micro.py:1067` (T12).
9. Patch the v0 spec §7.4 to reference v1.
10. Run full dossier suite + schema validator; confirm green.

## 12. Decisions log

| # | Question | Decision |
|---|---|---|
| 1 | Grain dimension | nav category (alphabetical tie-break). Leaf-grain deferred to v2. |
| 2 | Tie-break rule | alphabetical first on resulting unique nav set; arbitrary-deterministic, NOT semantic priority |
| 3 | Ambiguous tag handling | refuse with distinct reason `ambiguous_tag_pending_operator_decision`; do NOT route to "other" via `resolve_membership(ambiguous_remap_to_other=True)` |
| 4 | DB fallback when `nav_lookup` empty | refuse with `nav_lookup_missing_from_fixture`; deterministic fixture input only |
| 5 | `leaf_tags_json` population | NULL when no post-skip, non-ambiguous leaf survives; populated alphabetical list otherwise (even when nav unmapped, for v2) |
| 6 | Schema version | bump to `v1_market_semantics` |
| 7 | Existing data_gap rename | `market_tags_not_provisioned` → `market_category_unavailable`; count-based emission, severity from affected share |
| 8 | Fixture-refresh path | standalone `scripts/refresh_fixture_tags.py` produces a NEW tagged fixture file; the existing 3-step pipeline runs against the tagged fixture; dossier never mutates fixtures or calls live APIs |
