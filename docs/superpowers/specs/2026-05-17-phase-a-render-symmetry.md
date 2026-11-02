# Phase A Render Symmetry — G3 / G4 / G6 markdown rendering

**Date:** 2026-05-17
**Status:** spec → tests → implementation
**Origin:** Phase A render coverage gap caught while planning Phase B. Codex closed the G2 gap (commit `c4ec26d`) but the same gap is open for G3 (`triage_context`), G4 (`tag_lookup_state`), and G6 (`category_clusters`). All three are JSON-exposed but invisible in the rendered markdown report — operators reading cards can't see them.
**Scope:** Add three new markdown render sections/lines so the card prose surfaces every Phase A field. No analyzer logic change. No JSON shape change. No CLI change.

## 0. Doctrine

The audit's recommendations were "operator can see X" — meaning the rendered surface. Codex correctly recognized this for G2 ("as plainly as activity coverage" → rendered line). The same goal applies to G3/G4/G6. JSON-only exposure is incomplete; the markdown report is the canonical operator surface for card-based review (Phase B B5 depends on this).

## 1. What ships

### 1.1 G4 — tag_lookup_state rendering

`sniper/analysis/wallet_protocol/report.py:_data_completeness_block`:

- **Activity capture sub-section** gets a new line after the existing surface lines:
  ```text
  - tag_lookup_state: ok (5 conditions tagged)
  ```
  Format `tag_lookup_state: <state> (<detail>)` where:
  - `ok` → `(N conditions tagged)` (count derived from `leafs_by_cid`-driven `tag_coverage.with_leaf_tags` if available, else just `tag_lookup_state: ok`).
  - `empty` → `(no tag data found for any condition)`.
  - `unavailable` → `(lookup failed — see TAG_LOOKUP_UNAVAILABLE caveat)`.
  - `skipped` → `(--no-tags requested)`.

- **Caveats sub-section** gets a new caveat when `tag_lookup_state == "unavailable"`:
  ```text
  - TAG_LOOKUP_UNAVAILABLE: tag-grain lookups could not run (missing
    market_tags table or other DB error). Category/specialist
    extraction is degraded for this report.
  ```

### 1.2 G3 — triage_context block

New top-level section rendered ONLY when `report.triage_context is not None`. Inserted between the headline and `## Data completeness`:

```text
## Triage context

- source: polymarket_public_leaderboard
- period: MONTH
- fetched_at: 2026-05-13T14:30:00Z   (when "fetched_at" is an int unix epoch)
- rank: 3
- lb_pnl_at_capture: $1,760,000
- note: free-text from operator
```

Renderer iterates the dict's items in **insertion order** and renders `- {key}: {value}`. No interpretation, no schema enforcement (matches the field's verbatim-passthrough doctrine). `fetched_at` is rendered as ISO-8601 UTC if it parses as a unix epoch int; otherwise printed as-is.

If the dict is empty (`{}`), render `## Triage context` + `- (no entries)` so the field's presence is visible.

### 1.3 G6 — category_clusters summary

`sniper/analysis/wallet_protocol/report.py:_outcome_side_lines` (or a new helper). Rendered ONLY when `outcome_side_breakdown` has a populated `category_clusters` list:

```text
### Category clusters
- politics: n=6, pnl=$600, share=94% (primary)
- sports:   n=2, pnl=-$40, share=-6%
- geo:      n=3, pnl=$60,  share=9%
```

Top 5 by `n` (already sorted by producer). Each line shows category, count, pnl, and share-of-wallet-pnl. Primary cluster (first in sorted order) gets `(primary)` annotation.

`share` = `cluster.pnl / cluster.total_pnl` clamped for display: when `total_pnl == 0`, render `share=—`.

### 1.4 Non-changes

- `ProtocolReport` schema unchanged.
- `_compute_outcome_side_breakdown` unchanged.
- `data_completeness` dict shape unchanged.
- `triage_context` dict shape unchanged.
- Existing render sections (Activity capture, Attribution coverage, Caveats, Outcome-side breakdown YES/NO/OTHER, etc.) — content unchanged; only ADD new lines/sections.
- Codex's G2 render fix (`closed_positions_fetched` line + `CLOSED_POSITIONS_TRUNCATED` caveat) untouched.

## 2. Acceptance tests

All in `tests/test_wallet_protocol_completeness_surface.py` (Codex's pattern). 10 tests total.

### 2.1 G4 tag_lookup_state render

```text
test_render_tag_lookup_state_ok_line
  tag_lookup_state="ok", leafs_by_cid={"cid": ["tag"]} →
  md contains "tag_lookup_state: ok".

test_render_tag_lookup_state_unavailable_line_and_caveat
  tag_lookup_state="unavailable" →
  md contains both "tag_lookup_state: unavailable" AND
  "TAG_LOOKUP_UNAVAILABLE:" caveat.

test_render_tag_lookup_state_skipped_line_no_caveat
  tag_lookup_state="skipped" →
  md contains "tag_lookup_state: skipped" AND
  does NOT contain "TAG_LOOKUP_UNAVAILABLE:".

test_render_tag_lookup_state_empty_line_no_caveat
  tag_lookup_state="empty" →
  md contains "tag_lookup_state: empty" AND
  no TAG_LOOKUP_UNAVAILABLE caveat.
```

### 2.2 G3 triage_context render

```text
test_render_triage_context_block_when_present
  triage_context={"source": "lb", "period": "MONTH", "rank": 3}
  → md contains "## Triage context" and all three k:v lines.

test_render_triage_context_block_absent_when_none
  triage_context=None → "## Triage context" NOT in md.

test_render_triage_context_fetched_at_int_renders_as_iso8601
  triage_context={"fetched_at": 1747700000} →
  md contains an ISO-8601 timestamp, not the raw int.
```

### 2.3 G6 category_clusters render

```text
test_render_category_clusters_when_populated
  outcome_side_breakdown.category_clusters=[
    {"category":"politics","n":6,"pnl":600.0,"total_pnl":640.0},
    {"category":"sports","n":2,"pnl":-40.0,"total_pnl":640.0}
  ]
  → md contains "### Category clusters", a line for politics with
  share≈94%, and "(primary)" annotation on the first row.

test_render_category_clusters_absent_when_no_data
  outcome_side_breakdown without category_clusters key →
  "### Category clusters" NOT in md.

test_render_category_clusters_handles_zero_total_pnl
  All clusters with total_pnl=0 → "share=—" rendered, no division-by-zero.
```

## 3. Implementation surface

| File | Change |
|---|---|
| `sniper/analysis/wallet_protocol/report.py` | `_data_completeness_block`: pull `tag_lookup_state` from `dc`, append a line + caveat. `_outcome_side_lines` (or sibling helper): emit `### Category clusters` when populated. `render_report` (or helper): emit `## Triage context` block when `report.triage_context` is non-None. |
| `tests/test_wallet_protocol_completeness_surface.py` | 10 new tests per §2. |

No other file touched.

## 4. Risks

1. **Position of the new sections.** `## Triage context` placement (between headline and data completeness) is a layout choice — if operator prefers it after the headline summary or under "Data completeness", a follow-up moves it. Tests pin the SECTION HEADER + LINES, not the position relative to other sections.
2. **`category_clusters` won't appear under bounded HARNESS_ARGS.** Cards generated by `calibration_report_card.py` (which passes `--no-tags`) won't have `category_clusters` because `leafs_by_cid=None`. This is INTENTIONAL — bounded cards skip local-DB state. The render helper still works; it just renders nothing when the key is absent. Phase B closeout will surface this if operator wants `category_clusters` visible in cards (would require a tag-DB-aware capture path).
3. **`triage_context` is operator-supplied free text.** Renderer doesn't validate. Keys/values are printed as-is. Operator who passes `{"</script>":"injection"}` gets the literal string in markdown — acceptable since markdown is rendered by the operator's tooling, not auto-published.
4. **Render render-test coupling with Codex's tests.** Codex's existing render tests pin specific substrings. My new tests add new substrings; nothing removed. Codex tests stay green.

## 5. Out of scope

- ANALYST_FAST_MODE_OMITTED dedicated line (currently appears in `data_quality_flags` list — already visible).
- Restructuring the existing `## Data completeness` block layout.
- Adding `triage_context` validation or schema.
- Persisting `triage_context` in any DB column.

## 6. Done definition

- 10 new acceptance tests in `tests/test_wallet_protocol_completeness_surface.py` red, then green.
- Codex's 3 existing render tests stay green.
- `python3 -m pytest tests/test_wallet_protocol_completeness_surface.py tests/test_wallet_protocol_completeness_closed_positions.py tests/test_wallet_protocol_tag_lookup_state.py tests/test_wallet_protocol_triage_context.py tests/test_wallet_protocol_outcome_side_breakdown_category_clusters.py -q` passes.
- `python3 scripts/validate_schema_refs.py` passes.
- `git diff --stat` shows only `report.py` + 1 test file + spec.
