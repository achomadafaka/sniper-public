# Wallet Dossier Semantics v0

**Date:** 2026-05-25
**Status:** design only, awaiting review
**Author:** Claude (under operator + codex direction)
**Branch:** `feat/calibration-v4-position-reconciliation`

## 0. Why this exists

A multi-session debug of wallet reports (SwissMiss + 4 others) surfaced three
structural failures in the analyzer's report-time interpretation:

1. **Copy-target framing instead of strategy-intelligence framing** — reports
   reverted to "is this wallet copyable" language despite project doctrine
   (CLAUDE.md, AGENTS.md) being explicit: analyzer extracts reusable behavioral
   *theses*, names missing data, and refuses to issue copy advice.
2. **Accounting buckets promoted to strategy theses** — `YES / NO / OTHER`
   was used as if it were strategy grain. `OTHER` collapses unrelated
   strategy domains (MLB games, esports BO5, election brackets) into one
   bucket then reasons over it as one population.
3. **"Win rate" used ambiguously** — trade wins (price moved favorably
   pre-resolution), outcome wins (held to resolution and the event happened),
   and risk-management wins (cut a position that later resolved against the
   wallet) were collapsed into a single metric. Different skills, treated as
   one.

This spec defines a deterministic, offline, table-first **wallet dossier**
artifact that addresses all three failures. The dossier is the source of truth
that any human (or LLM, on explicit request) reads — without LLM
interpretation in the data pipeline.

## 1. Scope

### In scope (v0)
- Consume `WalletData` (fixture JSON) + `ProtocolReport` (analyzer output)
  for a single wallet.
- Produce a persisted SQLite database with the schema in §6.
- Produce a table-first markdown report with the structure in §10.
- Produce machine-readable JSON exports of every table for tests.
- Compute deterministic semantic decomposition (market_type,
  entry_price_bucket, etc.) from fields already in the fixture.
- Classify per-exit-episode counterfactuals from selected-token resolution
  where the fixture already contains it.
- Compute per-grain mechanism evidence with honesty gates.

### Out of scope (deferred)
- Live API fetches of any kind.
- Sibling-outcome price fetches for favorite/longshot ranking at entry.
  (Marked `data_unavailable` with reason.)
- `market_tags` ingestion for category decomposition. (Marked
  `data_unavailable` with reason; codex's `codex/market-tags-provisioning`
  is the unblocking work.)
- DB writes outside the dossier SQLite artifact.
- Mutation of `ProtocolReport.overall_verdict`, `gate_result`,
  `primary_edge_type`, `copyability_note`, `data_quality_flags`, or any
  existing surface.
- Production persistence (long-running SQLite ops, replication, retention).
- LLM-generated narrative inside the dossier or report.

### Hard rules
- The dossier **never** mutates the input `ProtocolReport`.
- The dossier **never** issues copy-trading advice or recommendations.
- The dossier **never** promotes a broad bucket (`OTHER`, `YES`, `NO`,
  `unknown`, `mixed`, `data_unavailable`) to a candidate thesis.
- The dossier **never** claims a category, favorite/longshot rank, or
  outcome_role inferred from a free-text market title via heuristic. If
  the structured source is missing, the field is `unknown` with explicit
  `data_unavailable_reason`.
- LLM summary is allowed **only** when the operator explicitly invokes
  `--llm-summary` and **only** after all tables exist. The summary cannot
  introduce claims absent from the tables.

## 2. Doctrine constraints (carried from CLAUDE.md + AGENTS.md)

- Strategy-intelligence framing, not copy-trading.
- Display-only contract: hard-walled against verdict/gate/edge mutations.
- Explicit coverage limits and missing-data identification.
- No live APIs unless explicitly scoped (this is offline).
- No raw SQL outside `db.py` (the dossier SQLite is a separate artifact,
  not the project DB; it goes through its own helper).

## 3. Inputs

The dossier consumes two inputs, both already produced by existing code:

1. **`WalletData`** — the per-wallet fetch fixture (e.g.
   `tests/calibration/fixtures-v4-staging/<wallet>_<date>.json`), loaded via
   `sniper.analysis.wallet_protocol.replay.load_fetch_snapshot`.
2. **`ProtocolReport`** — the analyzer JSON output from
   `scripts/run_wallet_protocol.py <wallet> --from-fixture <F> --json`.

Both are read-only. The dossier produces a new artifact set; it never writes
back into either.

## 4. Outputs

### Default mode (operator/inspection use)
- `dossiers/<wallet>_<date>.sqlite` — persisted SQLite with the full schema
  in §6. Queryable via `sqlite3 <file>`, any SQLite UI, or DuckDB.
- `dossiers/<wallet>_<date>.md` — table-first markdown report (§10).

### Test mode (deterministic regression)
- `--json-export <dir>` produces one JSON file per table, no SQLite. Tests
  load these and assert shape + key values. Same internal builder; only the
  sink differs.

### Filesystem layout (decided)
```
dossiers/                                      # gitignored — generated SQLite + MD
  v4/
    <wallet>_2026-05-25.sqlite
    <wallet>_2026-05-25.md
tests/calibration/dossier-fixtures/            # checked in — golden test data
  expected_<wallet>_<date>/
    positions.json
    position_exit_episodes.json
    market_semantics.json
    diagnostic_accounting.json
    ...
```

The `dossiers/` directory at repo root MUST be added to `.gitignore` before
implementation begins. Test goldens under `tests/calibration/dossier-fixtures/`
are checked in.

## 5. Entrypoint

```
python3 scripts/build_wallet_dossier.py <wallet> \
    --fixture <path/to/fetch_snapshot.json> \
    --protocol-report <path/to/protocol_report.json> \
    [--out-db dossiers/v4/<wallet>_<date>.sqlite] \
    [--out-md dossiers/v4/<wallet>_<date>.md] \
    [--json-export <dir>] \
    [--llm-summary]                        # explicit opt-in only
```

- `--llm-summary` is the ONLY path that invokes LLM interpretation. Off by
  default. When on, summary is appended to the markdown report under an
  explicit `## Operator-requested LLM Summary` heading and may not reference
  fields absent from the tables.

## 6. Schema

All tables live in the per-wallet SQLite. JSON export emits one file per
table with the same shape.

### 6.1 `positions`

One row per closed position from `WalletData.closed_positions`.

| Column | Type | Source | Notes |
|---|---|---|---|
| position_id | TEXT PK | hash(condition_id + asset_id) | stable across runs |
| wallet | TEXT | input | lowercased |
| condition_id | TEXT | fixture | lowercased |
| asset_id | TEXT | fixture | the selected outcome token |
| outcome_index | INTEGER | fixture if present, else NULL | from market metadata |
| outcome_label | TEXT | fixture `outcome` field | raw string; NOT used for classification |
| title | TEXT | fixture | raw market title; NOT parsed for category |
| entry_price | REAL | `avgPrice` from fixture | clamped [0,1] |
| size | REAL | derived | shares — v0 reads `totalBought` from `WalletData.closed_positions[i]`. Polymarket /closed-positions returns `size=0` for fully-exited positions and `totalBought` as the gross share count over the position's lifetime (verified empirically: `(curPrice - avgPrice) * totalBought ≈ realizedPnl`). NULL when `totalBought` is missing or nonnumeric. |
| cost_basis_usdc | REAL | derived | `avgPrice * totalBought` (price-per-share × gross shares). NULL if `avgPrice` is missing even when `size` is populated. ADR-004 applies to OPEN positions; for CLOSED positions `avgPrice * totalBought` is the gross cost basis. |
| realized_pnl | REAL | fixture | from /closed-positions |
| unrealized_pnl | REAL | fixture | open portion if any |
| entry_ts | INTEGER | earliest BUY in activity_rows for this position | unix epoch |
| exit_ts | INTEGER | last SELL or redemption | unix epoch; NULL if held & unresolved |
| exit_mode | TEXT | derived | `held` / `pre_resolution` / `unresolved` / `unknown` |
| n_exit_episodes | INTEGER | derived | count from `position_exit_episodes` |
| dominant_counterfactual_class | TEXT | rollup of episodes | see §6.3; **derived only**; cannot be used as truth without inspecting episodes |
| market_type | TEXT | derived | see §7.1 |
| entry_price_bucket | TEXT | derived | see §7.2 |
| outcome_role | TEXT | derived | `binary_yes` / `binary_no` / `multi_outcome_selected` / `unknown` per §7.3 |
| outcome_role_rank | INTEGER | NULL in v0 | favorite/longshot rank requires sibling prices |
| outcome_role_rank_source | TEXT | `data_unavailable` in v0 | reason: sibling prices not fetched |
| position_pattern | TEXT | from `position_building_surface.positions[i].accumulation_pattern` | `single_fill` / `gradual_accumulation` / etc. The producer field is `accumulation_pattern` (see `position_building.py:600`). |
| selected_token_resolution | TEXT | see §7.5 | `won` / `lost` / `still_open` / `unknown` |
| resolution_source | TEXT | see §7.5 | `closed_position_curPrice` / `redemption_event` / `market_outcome_table` / `unresolved` / `unknown` |
| resolution_confidence | TEXT | see §7.5 | `high` / `medium` / `low` |
| data_quality_flags | TEXT (JSON array) | per-position caveats | from position_building, exit_attribution |

### 6.2 `position_exit_episodes`

**Episode-level truth.** One row per discrete exit event for a position
(profitable partial sell, loss partial sell, held-to-resolution, etc.).
The position-level `dominant_counterfactual_class` in `positions` is a
rollup of these.

| Column | Type | Notes |
|---|---|---|
| episode_id | TEXT PK | hash(position_id + episode_index) |
| position_id | TEXT FK | → positions |
| episode_index | INTEGER | sequence within position, 0-based |
| episode_type | TEXT | `partial_sell` / `full_sell` / `held_to_resolution` / `redemption` |
| timestamp | INTEGER | unix epoch of the episode |
| size_exited | REAL | shares exited in this episode |
| price_exit | REAL | sell price or 0/1 for resolution; clamped [0,1] |
| price_entry_for_episode | REAL | volume-weighted entry for the shares in this episode |
| episode_pnl | REAL | (price_exit - price_entry_for_episode) * size_exited |
| trade_result | TEXT | `win` (pnl > 0) / `loss` (pnl < 0) / `flat` |
| selected_token_resolution_at_episode_close | TEXT | what the eventual resolution was (or `still_open`) |
| counterfactual_class | TEXT | one of 10 classes — see §7.6 |
| counterfactual_source | TEXT | `episode_is_resolution` / `eventual_resolution_observed` / `eventual_resolution_unknown` |
| counterfactual_confidence | TEXT | `high` / `medium` / `low` |

### 6.3 The 10-class result taxonomy

Stored in `position_exit_episodes.counterfactual_class`. Position-level
rollup picks the *dominant* class by size (or marks `mixed_episodes` if no
class > 50%).

| Class | Meaning |
|---|---|
| `held_won` | Episode is a resolution and the selected token won |
| `held_lost` | Episode is a resolution and the selected token lost |
| `profitable_exit_later_won` | Pre-resolution profitable exit; selected token later resolved in wallet's favor → wallet exited early, may have left money on the table |
| `profitable_exit_later_lost` | Pre-resolution profitable exit; selected token later resolved against wallet → pure trade/repricing skill, NOT prediction |
| `loss_exit_later_won` | Pre-resolution loss exit; selected token later resolved in wallet's favor → bad exit, sold a winner at a loss |
| `loss_exit_later_lost` | Pre-resolution loss exit; selected token later resolved against wallet → good risk cut, avoided full loss |
| `market_still_open` | Episode is pre-resolution exit and market hasn't resolved yet |
| `unresolved` | Episode is pre-resolution exit; resolution data was sought but unavailable |
| `insufficient_data` | Episode shape couldn't be classified (e.g. missing price/timestamp) |
| `unknown` | Default; counterfactual_source explains why |

### 6.4 `market_semantics`

One row per (position) with structured market/outcome attributes. Joinable
to `positions` 1:1 but kept separate so the structured grain is explicit.

**Note:** `accounting_bucket` (YES/NO/OTHER) is **NOT** in this table — it
lives in §6.4b `diagnostic_accounting` to physically separate accounting
diagnostics from strategy semantics. This prevents the bucket from leaking
into grain joins.

| Column | Type | Notes |
|---|---|---|
| position_id | TEXT PK FK |  |
| market_type | TEXT | `binary` / `multi_outcome` / `unknown` |
| market_type_source | TEXT | `outcome_token_count` / `unknown` |
| n_outcomes_in_market | INTEGER | when derivable |
| category | TEXT | always `"data_unavailable"` (literal string) in v0; never NULL |
| category_source | TEXT | `market_tags_provisioned` / `data_unavailable` |
| category_unavailable_reason | TEXT | e.g. `market_tags_table_empty`, `tag_not_found_in_leafs` |
| event_family | TEXT | always `"data_unavailable"` in v0 |
| event_family_source | TEXT | same dependency chain |
| outcome_role | TEXT | `binary_yes` / `binary_no` / `multi_outcome_selected` / `unknown` |
| favorite_status | TEXT | `unknown` in v0 (sibling prices not fetched) |
| favorite_status_source | TEXT | `sibling_token_prices_at_entry` / `data_unavailable` |
| entry_price_bucket | TEXT | see §7.2 |

### 6.4b `diagnostic_accounting`

**Separate table** so YES/NO/OTHER cannot leak into strategy grain joins.
DIAGNOSTIC ONLY. Per §9 hard rule, no row from this table participates in
`mechanism_evidence` grain keys.

| Column | Type | Notes |
|---|---|---|
| position_id | TEXT PK FK | → positions |
| accounting_bucket | TEXT | `YES` / `NO` / `OTHER` — derived from `outcome_label` string match |
| outcome_label_raw | TEXT | the unparsed string used to classify |
| classifier_version | TEXT | bump if the YES/NO matcher changes |

Tests (§11.7) assert `diagnostic_accounting` is never joined into any
`mechanism_evidence` row.

### 6.5 `result_semantics`

One row per (wallet, grain) aggregating `position_exit_episodes` over a
specific grain. The grain is the cartesian product of structured columns;
see §7.7.

| Column | Type | Notes |
|---|---|---|
| grain_id | TEXT PK | stable hash of grain key |
| grain_key | TEXT (JSON) | the structured grain (see §7.7) |
| n_positions | INTEGER |  |
| n_episodes | INTEGER |  |
| episodes_by_class | TEXT (JSON dict) | per-class counts (10 classes) |
| outcome_win_rate_held | REAL | held_won / (held_won + held_lost), NULL if denom < threshold |
| trade_win_rate_preres | REAL | (profitable_exit_*) / (profitable_exit_* + loss_exit_*) |
| sold_winners_early_share | REAL | profitable_exit_later_won / profitable_exit_total |
| sold_winner_at_loss_share | REAL | loss_exit_later_won / loss_exit_total |
| loss_cut_correctness_rate | REAL | loss_exit_later_lost / loss_exit_total |
| pnl_total | REAL |  |
| pnl_concentration_top5_share | REAL | top 5 positions in grain |
| total_size_usdc | REAL |  |
| attribution_coverage | REAL | (held + preres_classified) / n_episodes |
| grain_quality | TEXT | `interpretable` / `interpretable_evidence_only` / `diagnostic_only` / `not_interpretable_at_this_grain` / `underpowered` — see §8 |
| grain_quality_reasons | TEXT (JSON array) | which gates failed if any |

### 6.6 `mechanism_evidence`

One row per (grain, mechanism). Each grain can have evidence for multiple
mechanisms. No wallet-level mechanism is produced (codex constraint).

| Column | Type | Notes |
|---|---|---|
| grain_id | TEXT FK |  |
| mechanism | TEXT | `prediction_driven` / `trading_driven` / `risk_management_driven` / `market_selection_driven` / `mixed` / `underpowered` |
| evidence_strength | TEXT | `metric_supported` / `consistent` / `inconclusive` / `refuted` / `n/a` |
| supporting_metrics | TEXT (JSON) | the named metrics from result_semantics that support this |
| sample_size | INTEGER |  |
| caveats | TEXT (JSON array) |  |
| missing_data | TEXT (JSON array) |  |
| candidate_thesis_evidence | TEXT | the candidate thesis sentence IF promotion gates pass; otherwise NULL with reason in `not_promoted_reason` |
| not_promoted_reason | TEXT | which gate(s) blocked promotion |
| derived_from_fixture_fetched_at | INTEGER | mirrors `WalletData.fetched_at` — deterministic from input, NOT wall-clock |

Note the rename per codex: NOT `thesis_candidates`. Field is
`candidate_thesis_evidence` and stays NULL unless §8 promotion gates pass.

### 6.7 `hypothesis_results`

Mirror of `ProtocolReport.held_analyst_branch.hypotheses` +
`flipper_branch.hypotheses`, flattened to one row per hypothesis. No
mutation — direct copy.

| Column | Type |
|---|---|
| branch | TEXT (`held_analyst` / `flipper`) |
| name | TEXT (H1..H6, FH1..FH6) |
| verdict | TEXT |
| n | INTEGER |
| p_value | REAL |
| effect_size | REAL |
| note | TEXT |

### 6.8 `surfaces`

One row per existing `ProtocolReport` surface. Direct mirror — no
interpretation, no mutation.

| Column | Type |
|---|---|
| surface_name | TEXT PK |
| data_state | TEXT |
| key_metrics | TEXT (JSON) |
| caveats | TEXT (JSON array) |

### 6.9 `crosstabs`

Pre-computed aggregates over grain combinations. Computed by SQL view
materialization, not Python loops. Each crosstab row carries a grain_key.

Standard crosstabs (v0):
- market_type × entry_price_bucket × exit_mode → (n, pnl, outcome_win_rate_held, trade_win_rate_preres)
- market_type × accounting_bucket → (n, pnl) — DIAGNOSTIC ONLY
- entry_price_bucket × counterfactual_class → (n, pnl)
- position_pattern × exit_mode → (n, pnl)

### 6.10 `data_lineage` (required per codex)

One row per semantic claim made anywhere in the dossier.

| Column | Type | Notes |
|---|---|---|
| claim_id | TEXT PK |  |
| table | TEXT | which dossier table |
| column | TEXT | which column |
| row_key | TEXT | which row(s) this lineage covers |
| source | TEXT | `fixture_field` / `derived` / `protocol_report_surface` / `data_unavailable` |
| source_detail | TEXT | exact field path, e.g. `WalletData.closed_positions[i].avgPrice` |
| confidence | TEXT | `high` / `medium` / `low` |
| unavailable_reason | TEXT | when source=data_unavailable |
| derivation_rule | TEXT | which rule from §7 |

Every non-null cell in every table must trace to at least one
`data_lineage` row (or be a key/index field). Tests enforce this.

### 6.11 `data_gaps`

One row per known data dependency the v0 dossier could not satisfy.

| Column | Type | Example |
|---|---|---|
| gap_name | TEXT | `market_tags_not_provisioned` |
| affected_columns | TEXT (JSON array) | `["market_semantics.category", "market_semantics.event_family"]` |
| affected_grain_count | INTEGER | how many grains lose interpretability |
| unblocking_spec | TEXT | path to spec that would close it |
| severity | TEXT | `high` / `medium` / `low` |

## 7. Derivation rules

Every rule is a pure function from inputs (`WalletData` + `ProtocolReport`)
to outputs. No external calls. Every output cell produces a `data_lineage`
row.

### 7.1 `market_type`

```
n_outcomes = count of distinct asset_ids per condition_id observed in:
  WalletData.closed_positions ∪ WalletData.activity_rows

market_type =
  "binary"       if n_outcomes == 2
  "multi_outcome" if n_outcomes > 2
  "unknown"      if n_outcomes < 2 OR data missing
```

Caveat: if the wallet only ever traded one outcome of a multi-outcome
market, we may undercount. Mitigation: cross-reference with sibling token
references in activity_rows. If still ambiguous, mark `unknown` with
`market_type_source = "outcome_token_count_ambiguous"`.

### 7.2 `entry_price_bucket`

```
entry_price_bucket(p):
  "p_lt_05"      if p < 0.05
  "p_05_25"      if 0.05 <= p < 0.25
  "p_25_50"      if 0.25 <= p < 0.50
  "p_50_75"      if 0.50 <= p < 0.75
  "p_gte_75"     if p >= 0.75
```

Bins are stable across reports for cross-wallet comparison.

### 7.3 `outcome_role` (v0 — limited)

```
outcome_role =
  "binary_yes"  if market_type == "binary" AND outcome_label.lower() in {"yes", "true"}
  "binary_no"   if market_type == "binary" AND outcome_label.lower() in {"no", "false"}
  "multi_outcome_selected" if market_type == "multi_outcome"
  "unknown"     otherwise
```

**Hard rule:** `outcome_role` does NOT include favorite/longshot
distinction in v0. Favorite status is a separate column
(`favorite_status`) which is always `unknown` in v0 with reason
`sibling_token_prices_at_entry_not_fetched`.

### 7.4 `category` (v0 — always unavailable)

> **Superseded by v1**: This §7.4 rule ("always unavailable") is replaced in
> `docs/superpowers/specs/2026-05-26-wallet-dossier-market-semantics-v1.md`
> which implements nav-category resolution via `leafs_by_cid + nav_lookup`.
> The v0 rule below is preserved for historical reference only.

```
category = "data_unavailable"   # literal string, never NULL
category_source = "data_unavailable"
category_unavailable_reason =
  "market_tags_table_empty"  if WalletData.leafs_by_cid == {} or None
  "tag_not_found_in_leafs"   else per-condition
```

Use the literal string `"data_unavailable"` rather than SQL NULL so gate
enforcement (§8) and grain-key matching are testable with simple equality
checks — `category != "data_unavailable"` is unambiguous; `category IS NOT
NULL` would force two-path logic everywhere.

When `market_tags` is provisioned (codex/market-tags-provisioning), this
rule is replaced with the tag-lookup version that returns actual category
strings.

### 7.5 `selected_token_resolution`

This is the load-bearing classifier. Resolution must be **selected-token
based** (per codex), never `condition_id` alone.

```
For each position (condition_id, asset_id):

1. Check WalletData.redemptions for any row with
   {condition: condition_id, AND token matching asset_id via indexSet}.
   If found:
     selected_token_resolution =
       "won" if redemption.payout > 0
       "lost" if redemption.payout == 0
     resolution_source = "redemption_event"
     resolution_confidence = "high"
     RETURN

2. [FEATURE FLAG: `RESOLVE_BY_CURPRICE_ENABLED` — DEFAULT **OFF** IN V0]
   Check WalletData.closed_positions[i].curPrice. Only runs if flag is ON.
   Flag may not be enabled until §11.5 empirical verification passes on all
   10 v4 calibration wallets. Until then, this rule is a no-op.
   If enabled AND position is fully closed AND curPrice clearly indicates
   resolution (within delta of 0 or 1):
     selected_token_resolution = "won" if curPrice > (1 - threshold), else "lost"
     resolution_source = "closed_position_curPrice"
     resolution_confidence = "medium"
     RETURN

3. Sibling-redemption inference (CONSERVATIVE).
   Check if any closed_positions row references the same condition_id with
   a DIFFERENT asset_id AND that sibling appears in WalletData.redemptions
   with payout > 0.
   This rule fires ONLY when ALL of the following hold:
     (a) we have evidence the market is single-winner / exhaustive — i.e.
         the number of distinct asset_ids observed across closed/open
         positions for this condition_id equals the number of outcomes the
         market should have (verified by counting outcome tokens in
         activity_rows for this condition_id);
     (b) the sibling's redemption payout > 0 (sibling won);
     (c) we hold the OTHER side (asset_id differs from the winning sibling);
     (d) no redemption record exists for our own asset_id.
   If all four hold:
     selected_token_resolution = "lost"  (sibling won, ours lost)
     resolution_source = "sibling_redemption_inferred_exhaustive"
     resolution_confidence = "medium"
     RETURN
   If (a) fails — market may have multiple winners, non-exhaustive
   outcomes, or split payouts — the inference is unsafe. Do NOT fire. Fall
   through to rule 4 with `selected_token_resolution = "unknown"`.

4. Otherwise:
     selected_token_resolution =
       "still_open"  if any open_positions row for this condition_id
       "unknown"     else
     resolution_source = "unresolved" or "unknown"
     resolution_confidence = "low"
```

**Feature flag default: OFF.** Rule 2 (`curPrice`-based resolution) is
disabled by default. v0 ships with rules 1, 3, 4 only. Rule 2 may be
enabled only after §11.5 empirically verifies on all 10 v4 calibration
wallets that `curPrice` reflects actual resolution and not most-recent
mark. **More `unknown` is preferable to fake certainty.**

**Sibling-inference conservatism (codex constraint).** Rule 3 fires only
when the market is provably single-winner / exhaustive. Otherwise the
inference is unsafe (could be multi-winner markets, partial-payout
schemes, or markets with rebated/split resolutions) and we mark
`unknown` rather than guess.

### 7.6 `counterfactual_class` (per episode)

```
For each position_exit_episode:

if episode_type in {"held_to_resolution", "redemption"}:
    counterfactual_class =
      "held_won"  if selected_token_resolution == "won"
      "held_lost" if selected_token_resolution == "lost"
      "unresolved" if "still_open"
      "unknown"   otherwise

elif episode_type in {"partial_sell", "full_sell"} (pre-resolution exit):
    if trade_result == "win":
        counterfactual_class =
          "profitable_exit_later_won"   if selected_token_resolution_at_episode_close == "won"
          "profitable_exit_later_lost"  if "lost"
          "market_still_open"           if "still_open"
          "unresolved"                  if "unknown" with source = "unresolved"
          "unknown"                     otherwise
    elif trade_result == "loss":
        counterfactual_class =
          "loss_exit_later_won"   if won
          "loss_exit_later_lost"  if lost
          "market_still_open"     if still_open
          "unresolved"            if unknown w/ unresolved source
          "unknown"               otherwise
    else:  # flat
        counterfactual_class = "insufficient_data"

else:
    counterfactual_class = "insufficient_data"
```

### 7.7 Grain key

The structured grain key for `result_semantics` and `mechanism_evidence`:

```
grain_key = {
  category_source: str,        # "market_tags_provisioned" or "data_unavailable"
  category: str,               # always a literal string — "data_unavailable"
                               # in v0, real category post-tag-provisioning;
                               # never NULL
  market_type: str,            # binary / multi_outcome / unknown
  outcome_role: str,           # binary_yes / binary_no / multi_outcome_selected / unknown
  entry_price_bucket: str,
  exit_mode: str,              # held / pre_resolution / unresolved
  position_pattern: str,       # gradual_accumulation / single_fill / etc.
}
```

All seven dimensions are mandatory in the key.

**Broad-bucket leakage rule (refined per codex).** A grain dimension is
"leaky" when its value is one of the broad-bucket sentinels: `unknown`,
`mixed`, `OTHER`, or `data_unavailable`.

Distinguish two cases:

- **Category-only leakage** (`category == "data_unavailable"` AND all
  six other dimensions hold non-leaky values): the grain is
  `interpretable_evidence_only` — eligible for `evidence_strength`
  computation but never for `candidate_thesis_evidence` promotion.
- **Any other leakage** (any of: `market_type ∈ {unknown}`,
  `outcome_role ∈ {unknown}`, `exit_mode ∈ {unresolved, unknown}`,
  `entry_price_bucket missing`, `position_pattern ∈ {unknown, mixed}`,
  `category == "data_unavailable"` combined with any other leaky
  dimension): the grain is `not_interpretable_at_this_grain` — no
  evidence, no promotion.

This is the codex split: evidence eligibility (gates 1, 2, 4, 5, 6, 7)
is decoupled from promotion eligibility (gate 3 — real category)
ONLY when category is the sole missing piece.

### 7.8 Mechanism evidence rules (per grain)

Computed for grains with `grain_quality ∈ { "interpretable",
"interpretable_evidence_only" }`. Skipped for grains with
`grain_quality ∈ { "diagnostic_only", "not_interpretable_at_this_grain",
"underpowered" }`.

```
prediction_driven metric_supported IF:
  - n_episodes >= 30
  - exit_mode == "held"
  - outcome_win_rate_held > 0.55
  - pnl_total > 0
  - grain_quality in ("interpretable", "interpretable_evidence_only")

trading_driven metric_supported IF:
  - n_episodes >= 30
  - exit_mode == "pre_resolution"
  - trade_win_rate_preres > 0.55
  - profitable_exit_later_lost share > 0.30
    (i.e. the wallet wins trades that the event later loses — proves
     trading independent of prediction)
  - grain_quality in ("interpretable", "interpretable_evidence_only")

risk_management_driven metric_supported IF:
  - n_episodes >= 30
  - exit_mode == "pre_resolution"
  - loss_cut_correctness_rate > 0.55
  - sold_winner_at_loss_share < 0.30
  - grain_quality in ("interpretable", "interpretable_evidence_only")

market_selection_driven:
  - DEFERRED in v0; requires comparable-unselected-markets baseline (not in fixture)
  - always n/a with reason "selection_baseline_not_computable_offline"

mixed IF:
  - 2+ mechanisms each get evidence in same grain
  - report explicitly says "mixed; do not promote"

underpowered IF:
  - n_episodes < 30 OR coverage < 0.7
```

## 8. Honesty gates

Seven gates. A grain receives `grain_quality = "interpretable"` only if
ALL seven pass. The category gate (3) has special handling per the
codex evidence-vs-promotion split (§8.1, §8.3).

1. **Sample size**: `n_episodes >= 30` for that grain.
2. **Attribution coverage**: `attribution_coverage >= 0.7`.
3. **Category homogeneity**: all positions in the grain share the same
   `category` AND category is a real string (not `"data_unavailable"`).
   In v0, this gate is always failed because no position has a real
   category. The failure mode is special — see §8.1.
4. **Market type homogeneity**: all positions in the grain share the same
   `market_type`.
5. **Outcome role homogeneity**: all positions share the same
   `outcome_role`.
6. **PnL concentration sanity**: `pnl_concentration_top5_share <= 0.7`. If
   top 5 positions drive >70% of the grain's PnL, the grain is too
   concentrated to support a population-level claim.
7. **No broad-bucket leakage on non-category dimensions**: no dimension
   in the grain key OTHER than `category` may be `unknown`, `mixed`,
   `OTHER`, or `data_unavailable`. Category alone failing gate 3 does
   NOT trigger gate 7 failure.

Gate-failure-to-quality mapping is in §8.2.

### 8.1 v0 category gap — DECIDED: Option B with evidence/promotion split

**Decision (codex-approved, refined):** Option B applies via a clean
split between `evidence_strength` and `candidate_thesis_evidence`.

When `category == "data_unavailable"` AND all other gates (1, 2, 4, 5,
6, 7) pass:
- `grain_quality = "interpretable_evidence_only"`.
- `evidence_strength` may reach `"metric_supported"` on any mechanism if metric
  thresholds in §7.8 are met.
- `candidate_thesis_evidence` MUST be NULL.
- `not_promoted_reason` MUST equal `"category_unavailable"`.
- Reports refer to the finding as `metric_supported_but_not_promotable` to make
  the gating posture explicit.

When `category == "data_unavailable"` AND any other gate fails:
- `grain_quality` follows the worst other gate's mapping in §8.2.
- No evidence, no promotion.

When `market_tags` is provisioned and category becomes a real string:
- Gate 3 evaluates normally (homogeneity check on the real category).
- If gate 3 passes (along with all others), `grain_quality =
  "interpretable"` and `candidate_thesis_evidence` may be set.

### 8.2 Gate failure → grain_quality mapping

The mapping evaluates in order. The first gate that fails determines
`grain_quality`. Gate 3 (category) has special handling: if ONLY gate 3
fails (i.e. gates 1, 2, 4, 5, 6, 7 all pass and the only problem is
`category == "data_unavailable"`), the grain becomes
`interpretable_evidence_only` instead of failing outright.

| Failed gate(s) | grain_quality | Evidence allowed? | Promotion allowed? |
|---|---|---|---|
| Gate 1 (sample size) | `underpowered` | no | no |
| Gate 2 (attribution coverage) | `underpowered` | no | no |
| Gates 4, 5, or 7 (homogeneity / broad-bucket on non-category) | `not_interpretable_at_this_grain` | no | no |
| Gate 6 (PnL concentration) | `diagnostic_only` | no | no |
| **Only gate 3** (category is `data_unavailable`; all other gates pass) | **`interpretable_evidence_only`** | **yes** | **no** |
| All 7 gates pass | `interpretable` | yes | yes |
| Gate 3 fails AND any of gates 1, 2, 4, 5, 6, 7 also fails | follows the worst other gate's mapping | no | no |

### 8.3 Hard promotion rule (codex split: evidence vs promotion)

`candidate_thesis_evidence` is non-null ONLY when ALL of:
- `grain_quality == "interpretable"` (all 7 gates passed), AND
- at least one mechanism in `mechanism_evidence` has
  `evidence_strength == "metric_supported"`, AND
- `not_promoted_reason IS NULL`.

`evidence_strength` may reach `"metric_supported"` when:
- `grain_quality ∈ { "interpretable", "interpretable_evidence_only" }`,
  AND
- the mechanism-specific metric thresholds in §7.8 are met.

**Behavior when `category == "data_unavailable"` (v0):**
- `grain_quality = "interpretable_evidence_only"` (assuming gates 1, 2,
  4, 5, 6, 7 all pass).
- `evidence_strength` may be `"metric_supported"`.
- `candidate_thesis_evidence = NULL` (always).
- `not_promoted_reason = "category_unavailable"` (always).

**Behavior when category is real AND all gates pass:**
- `grain_quality = "interpretable"`.
- If a mechanism reaches `evidence_strength = "metric_supported"`,
  `candidate_thesis_evidence` may be non-null and `not_promoted_reason
  IS NULL`.

**Behavior when any other gate fails:**
- No evidence. No promotion. `grain_quality` per §8.2 mapping.

This separation is the codex split: evidence eligibility (gates 1, 2, 4,
5, 6, 7) is decoupled from promotion eligibility (gate 3) — but ONLY
when category is the sole leaky dimension. Any other broad-bucket
leakage still hard-fails both evidence and promotion.

## 9. Hard rules (codified)

1. Broad buckets (`YES`, `NO`, `OTHER`, `unknown`, `mixed`) MAY appear in
   the dossier only as diagnostic or non-interpretable values. Category
   value `"data_unavailable"` is the sole exception: when category is the
   only missing dimension, the grain may be `interpretable_evidence_only`
   but may never produce `candidate_thesis_evidence`.
2. Title strings (`positions.title`) are surfaced for human reading. They
   are NEVER parsed for category, domain, or favorite/longshot inference.
3. Resolution uses `(condition_id, asset_id, outcome_index)` as the
   selected-token key. `condition_id` alone is INSUFFICIENT for
   multi-outcome markets.
4. Episode-level truth is first-class. Position-level
   `dominant_counterfactual_class` is a rollup with an explicit
   `n_exit_episodes > 1` flag.
5. No wallet-level mechanism. Mechanism is per-grain only.
6. Every semantic claim has a `data_lineage` row. Tests enforce.

## 10. Markdown report structure

The markdown report is generated from the SQLite by queries. No
free-text claims. Section structure:

```
# Wallet Dossier — <wallet> (<date>)

## A. Top-level (read-only mirror)
[Table: ProtocolReport.{overall_verdict, gate_result, primary_edge_type,
 copyability_note, archetype_layer1, trading_mode, flip_rate,
 net_pnl, n_closed_positions}]

`copyability_note` is included only because it already exists on
`ProtocolReport`. The dossier treats it as a read-only legacy mirror
field: it is not interpreted, not acted on, and never used to generate new
copyability language.

## B. Hypothesis tally (read-only mirror of held + flipper branches)
[Table: name | verdict | n | p_value | effect_size]

## C. Surfaces (read-only mirror)
[Table: surface_name | data_state | key_metrics]

## D. Diagnostic accounting (YES/NO/OTHER)
EXPLICITLY MARKED "diagnostic only — not a strategy thesis source"
[Table: accounting_bucket | n_positions | n_episodes | pnl | top_5_markets_by_pnl
 (raw titles only)]

## E. Market semantics (per position)
[Table: market_type counts/PnL]
[Table: entry_price_bucket counts/PnL]
[Table: outcome_role counts/PnL]
[Table: data_gaps — explicit list of what's NULL and why]

## F. Result semantics (the 10-class taxonomy)
[Table: counterfactual_class | n_episodes | pnl | share_of_episodes]
[Table: named win-rate metrics with their denominators]
  - outcome_win_rate_held
  - trade_win_rate_preres
  - sold_winners_early_share
  - sold_winner_at_loss_share
  - loss_cut_correctness_rate

## G. Mechanism evidence by grain (interpretable grains only)
[Table per interpretable grain:
  grain_key | n | mechanism | evidence_strength | supporting_metrics |
  candidate_thesis_evidence (or not_promoted_reason)]

For `grain_quality = "interpretable_evidence_only"`, report phrasing is
`metric_supported_but_not_promotable`. The math pattern exists, but the
category gap prevents strategy-thesis promotion.

## H. Diagnostic-only grains
[Same shape as G but marked diagnostic_only; gate_failures column shows why]

## I. Data lineage summary
[Table: column_source_distribution — % of cells from fixture vs derived vs data_unavailable]

## J. Data gaps
[Table: gap_name | affected_columns | severity | unblocking_spec]

## K. Operator-requested LLM Summary  [PRESENT ONLY IF --llm-summary]

**Predefined template only. No free-form interpretation.** The LLM fills
fixed slots from table queries — it does not write a narrative. Template:

```text
## K. Operator-requested LLM Summary

This summary is filled from queries against sections A–J. It introduces
no claims absent from those sections.

[1] Top-level verdict from §A.
    Verdict: {overall_verdict from A.surfaces}
    Edge type: {primary_edge_type from A}
    Net PnL: {net_pnl from A}

[2] Hypotheses supported from §B.
    Held: {list of held hypotheses with verdict='supported' from B}
    Flipper: {list of flipper hypotheses with verdict='supported' from B}

[3] Counterfactual class distribution from §F.
    Held won: {n}, Held lost: {n}
    Profitable exit later won: {n}, Profitable exit later lost: {n}
    Loss exit later won: {n}, Loss exit later lost: {n}
    Market still open / unresolved / unknown: {n}

[4] Metric-supported mechanism evidence by grain from §G.
    For each row in G:
      Grain: {grain_key fields}
      Mechanism: {mechanism} ({evidence_strength})
      Promotable: {YES / NO} ({not_promoted_reason if NO})

[5] Data gaps from §J.
    {list of gap_name + severity}

[6] What this report DOES NOT say.
    - It does not recommend copying this wallet.
    - It does not score a thesis as ready for paper/live trading.
    - It does not infer category from market titles.
    - It does not aggregate broad buckets (YES/NO/OTHER) into theses.
```

The LLM is invoked exactly once per dossier with the section queries as
input, and emits ONLY the template above with slots filled. No
free-form additions.
```

## 11. Tests

### 11.1 Counterfactual classification correctness
- Synthetic fixture with one position per class (10 cases).
- Assert classifier returns each class correctly.
- Edge cases: partial sell + held residual; multiple loss exits then a profitable exit; price exactly at 0.5.

### 11.2 Partial-exit handling
- Synthetic position with: BUY 100 @ 0.40, SELL 40 @ 0.55 (profit), SELL 30 @ 0.30 (loss), HOLD 30 → resolution YES @ 1.00.
- Assert: 3 exit_episodes, one each of profitable_exit_*, loss_exit_*, held_won.
- Assert: dominant_counterfactual_class on positions row is `mixed_episodes` (no single class > 50%).

### 11.3 SwissMiss OTHER regression
- Load real SwissMiss v4 fixture + ProtocolReport.
- Assert: no grain in mechanism_evidence with non-null candidate_thesis_evidence
  has `accounting_bucket == OTHER` as a grain key dimension.
- Assert: any grain that aggregates 591 multi-outcome positions across baseball/
  esports/college football with category as the only missing dimension is
  marked `interpretable_evidence_only`, has `candidate_thesis_evidence = NULL`,
  and carries `not_promoted_reason = "category_unavailable"`.
- Assert: "multi-outcome edge" cannot be promoted from a
  category-`data_unavailable` grain.

### 11.4 Honesty gate enforcement
- Generate grain with n=10. Assert grain_quality=`underpowered`, no promotion.
- Generate grain with top-1 position carrying 80% of PnL. Assert
  grain_quality=`diagnostic_only`, no promotion.

### 11.5 Selected-token resolution (empirical verification)
- For each of the 10 v4 calibration wallets:
  - Compute `selected_token_resolution` for every position.
  - For positions in `WalletData.redemptions`, verify resolution_source =
    `redemption_event` and result matches redemption.payout.
  - For positions NOT in redemptions but with `closed_positions[i].curPrice`
    near 0 or 1, manually verify (operator step) that curPrice indicates
    resolution and not stale mark. **Until verified, rule §7.5 #2 must be
    disabled in code with a feature flag.**
  - Assert no position has resolution_source = `closed_position_curPrice`
    while the feature flag is off.

### 11.6 Data lineage completeness
- For every non-key, non-null cell in every table, assert at least one
  data_lineage row exists for that (table, column, row_key).
- Assert no `data_lineage.source == "data_unavailable"` is missing an
  `unavailable_reason`.

### 11.7 Hard-rule enforcement (codex-approved blocklist)
- Assert: no row in `mechanism_evidence` has `candidate_thesis_evidence`
  non-null while any grain_key dimension equals `OTHER`, `unknown`,
  `mixed`, or `data_unavailable`.
- Assert: no surface or column inside the dossier mutates a value from
  the input ProtocolReport (by hash check on the input).
- Assert: no row in `mechanism_evidence` joins to `diagnostic_accounting`
  (i.e. the accounting bucket physically cannot appear in a grain key).
- **Vocabulary blocklist (case-insensitive substring match) across the
  generated markdown report AND any TEXT cell in `mechanism_evidence`,
  `candidate_thesis_evidence`, or `result_semantics`:**
    - `copy target`
    - `copy trade` / `copy-trade` / `copytrade`
    - `mirror this wallet`
    - `follow signal`
    - `entry trigger`
    - `expected pnl` (this exact phrase — discussing realized/historical
      PnL is fine)
    - `recommended size` / `recommended position size`
  Test asserts the dossier produces zero matches for these substrings
  outside of the docstring/comment context inside the source code itself.
- Words OUTSIDE the blocklist (intentionally allowed because they're
  technical-not-advisory): `copyability_note`, `copy_latency_surface`,
  `trade_win`, `trade_result`, `copier_ew_roi`, `mirror image` (math),
  etc. The blocklist is phrase-specific, not word-specific.

## 12. Data dependencies

| Dependency | Status | Where it unblocks |
|---|---|---|
| `WalletData.redemptions/splits/merges` | ✅ available in v4 fixtures | §7.5, §7.6 |
| `closed_positions[i].curPrice` semantic verification | ⚠️ requires empirical test (§11.5) | §7.5 rule 2 |
| `WalletData.leafs_by_cid` populated | ❌ `market_tags` table is empty locally | §6.4 category, §8 gate 3 |
| Sibling outcome prices at entry timestamp | ❌ not fetched in v0 | favorite/longshot ranking |
| Comparable-unselected-market baseline | ❌ not computable from single-wallet fixture | `market_selection_driven` mechanism |

## 13. Not in v0

- Selection-skill testing (requires comparable-market universe).
- Favorite/longshot rank at entry (requires sibling-price fetches).
- Category decomposition (requires `market_tags` provisioning).
- Cross-wallet comparison aggregates (per-wallet dossier only).
- LLM auto-summary (only `--llm-summary` explicit opt-in).
- Mutation of any existing analyzer surface or verdict.

## 14. Acceptance criteria

1. `python3 scripts/build_wallet_dossier.py <wallet> --fixture <F> --protocol-report <R> --out-db <D> --out-md <M>` produces **byte-identical SQLite content** (compared via dumped row hashes per table) and **byte-identical markdown** when run twice with the same inputs. All "timestamp" columns in derived tables source from `WalletData.fetched_at` (or are explicitly stable hashes), never `time.time()` or wall-clock. SQLite file binary may differ in metadata (mtime, etc.), so the determinism check compares table contents, not raw bytes of the .sqlite file.
2. The dossier SQLite has all tables in §6 populated (or empty with reason).
3. Every row in `position_exit_episodes` has a `counterfactual_class` value from §6.3's 10 classes.
4. No grain in `mechanism_evidence` has non-null `candidate_thesis_evidence` while violating any §9 hard rule.
5. All tests in §11 pass.
6. The markdown report contains no narrative claim absent from a table.
7. Running with `--llm-summary` appends a clearly-delineated LLM section that **fills the predefined template in §10.K** — no free-form additions, only slot-filling from table queries.
8. Re-issuing wallets 1–5 from v4 fixtures produces dossiers that respect the SwissMiss OTHER regression test (§11.3).
9. **`diagnostic_accounting` is physically separated from `market_semantics` and never appears as a column in any `mechanism_evidence` grain_key** (codex constraint).
10. **§7.5 rule 2 (`curPrice` resolution) is OFF by default** in v0; the dossier produces NO rows with `resolution_source == "closed_position_curPrice"` until the feature flag is explicitly enabled after §11.5 verification passes.
11. **No generated markdown table uses a generic `win_rate` column name.** Win-rate-like fields must name the denominator/meaning in the field name, e.g. `outcome_win_rate_held` or `trade_win_rate_preres`.

## 15. Decisions log (all open questions resolved)

| # | Question | Decision | Source |
|---|---|---|---|
| 1 | Category-gate policy (Option A strict vs B pragmatic) | **Option B**: evidence may be `metric_supported`, `candidate_thesis_evidence` stays NULL with `not_promoted_reason = category_unavailable` | codex 2026-05-25 |
| 2 | `accounting_bucket` placement | **Separate `diagnostic_accounting` table** (§6.4b); never inside `market_semantics` | codex 2026-05-25 |
| 3 | `curPrice` resolution feature flag | **OFF by default**; rule 2 disabled until §11.5 verifies on all 10 v4 wallets | codex 2026-05-25 |
| 4 | `data_lineage` granularity | **Per (table, column, row_key)** — verbose is fine in v0; rollups for display only | codex 2026-05-25 |
| 5 | LLM summary scope | **Predefined template only** (§10.K); slot-fill from table queries; no free-form | codex 2026-05-25 |
| 6 | Vocabulary blocklist | **Ship**; phrases listed in §11.7 (not generic word matches) | codex 2026-05-25 |
| 7 | Persistence location | **`dossiers/v4/` at repo root, gitignored**; test goldens under `tests/calibration/dossier-fixtures/` | codex 2026-05-25 |

## 15a. v0 expectation

v0 is not expected to produce many promotable theses. Category is
`data_unavailable`, favorite/longshot status is unavailable, and market
selection testing is out of scope. v0 is expected to prevent bad theses
and produce queryable mechanism evidence.

## 15b. Spec contradictions fixed before implementation

| # | Issue | Fix |
|---|---|---|
| A | §8.3 said `not_promoted_reason IS NULL` required for promotion AND allowed Option B with `category_unavailable` reason | Rewrote §8.3 simple rule: `category != "data_unavailable"` is now a hard requirement for promotion; evidence vs promoted thesis cleanly separated |
| B | Acceptance #1 said "byte-identical outputs" but `derived_at` used current epoch | Renamed to `derived_from_fixture_fetched_at`; deterministic from input. Acceptance #1 specifies table-content comparison, not raw .sqlite bytes |
| C | §7.4 said `category = NULL`; §7.7/§8 referenced `data_unavailable` as a grain value | Standardized on literal string `"data_unavailable"` (never NULL); makes gate enforcement testable with simple equality |
| D | §7.5 sibling-redemption inference was too aggressive | Tightened to fire only when the market is provably single-winner / exhaustive (four conditions); otherwise `unknown` |
| E | §7.7 typed `category` as `str \| None` while elsewhere `data_unavailable` was a literal string | Changed type annotation to `str` always (`# always a literal string` in the code comment) |
| F | §7.7 said any `data_unavailable` grain dimension is auto-`diagnostic_only` while §8.1 allowed metric support when category is unavailable | Split evidence eligibility from promotion eligibility. Category-only `data_unavailable` → `grain_quality = "interpretable_evidence_only"` (evidence yes, promotion no). Any non-category broad-bucket leakage still hard-fails to `not_interpretable_at_this_grain` (codex 2026-05-25 second review). |
| G | §8 gate 7 ("no broad-bucket leakage") originally lumped `category == data_unavailable` with other broad buckets | Narrowed gate 7 to "no broad-bucket leakage **on non-category dimensions**"; category leakage is the special case handled by gate 3 + §8.1 + §8.3 |

## 16. Implementation sequencing (post-spec-approval)

1. Implement §6 schema + write empty tables.
2. Implement §7.1–§7.4 (deterministic, no resolution).
3. **Micro-v0 checkpoint:** build SQLite + deterministic base tables only:
   `positions`, `market_semantics`, `diagnostic_accounting`,
   `data_lineage`, and `data_gaps`. Run on one synthetic fixture and one
   real fixture. No `mechanism_evidence` yet.
4. Implement §7.5 (selected-token resolution with feature flag for rule 2).
5. Run §11.5 empirical verification on v4 calibration wallets; decide rule 2.
6. Implement §7.6 (counterfactual classification).
7. Implement §7.7–§7.8 (grain key + mechanism evidence).
8. Implement §8 honesty gates.
9. Implement §9 hard-rule enforcement.
10. Implement §10 markdown report generator.
11. Tests §11.1–§11.7.
12. Re-issue wallets 1–5 from v4 fixtures via the new dossier.
13. Compare to existing narrative reports — document where the dossier
    produces more conservative / more specific claims.

No implementation before this spec is reviewed and approved.
