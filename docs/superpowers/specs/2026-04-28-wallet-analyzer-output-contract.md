# Wallet-Analyzer Output Contract (Phase 2)

> **Status:** spec — locks the contract. Implementation lives in Phase 3.
> **Predecessor:** docs/superpowers/plans/2026-04-28-wallet-analyzer-migration.md
> **Related specs:** docs/superpowers/specs/wallet-analysis-protocol-v2.md,
> docs/superpowers/specs/2026-04-22-wallet-understanding-brain-design.md
> **Authoritative shape source:** sniper/analysis/wallet_protocol/types.py

**Goal.** Define the *complete, frozen* output surface the new analyzer
publishes to live runtime: tables, enums, reason codes, deterministic
mapping from analytical verdict to operational decision, HTTP + MCP
boundaries, and versioning rules. After this spec is signed off, no
runtime consumer reads a field that isn't defined here.

**Non-goals.**
- How `run_wallet_protocol.py` is scheduled (Phase 3 plan).
- How alerter / paper trader gates are rewritten (Phase 5 plan).
- UI badge design (Phase 4 plan).
- Anomaly plugin (Phase 7 plan).

---

## 1. Hard rules carried forward from the migration plan

These constrain every decision in this spec.

1. **No synthetic composite scores anywhere as authority.** A synthetic
   composite is any value built by combining metrics with intuition-chosen
   weights and compared to an intuition-chosen threshold to gate a
   decision. Banned in: this contract, every consumer, every API
   response, every UI render, every agent prompt.
2. **Direct measurements with units are allowed.** `realized_pnl_usdc`,
   `win_rate` (fraction), `sample_size`, `p_value`, CI bounds, etc.
3. **Non-authority fields — context/display only.** This category covers
   `wallet_tag_fit.fit_score` (REAL 0..1), `wallet_tag_fit.fit_rank`
   (INTEGER ordinal), `wallet_layer5_decision.confidence_band` (enum),
   and any other ordered/numeric output documented as such in this
   contract. These fields:
   - may be displayed to humans;
   - may order candidates *within an already-authorized set*
     (authorization comes from `wallet_layer5_decision.action_decision`);
   - may inform sizing inside an authorized set;
   - **must never** authorize, veto, promote, demote, gate, or globally
     rank a wallet on their own. No live consumer compares them to a
     threshold to make a binary go/no-go decision.

   The same rule binds them all: `confidence_band` differs from
   `tag_fit_score` only in shape (enum vs float), not in standing.
4. **Internal arithmetic that resolves to an enum at the boundary is
   allowed.** A function may compute numerically internally if the value
   it returns and other code stores or compares is an enum / a direct
   measurement.
5. **`ProtocolReport.overall_verdict` is the analytical conclusion;
   `wallet_layer5_decision.action_decision` is the operational
   instruction.** They are distinct fields in distinct domains. The
   mapping between them (§ 4) is explicit, deterministic, and unit-
   tested. Live runtime never reads `overall_verdict` for an automated
   decision.
6. **No `wallet_layer5_decision_history` or score-history-equivalent
   table** is created in Phase 2/3. History is deferred until decisions
   are real and a concrete consumer demands it.

---

## 2. Source data — what already exists

This contract sits on top of code that is already built. Anything below
this line either exists or is consumed; nothing is invented.

### 2.1 ProtocolReport (the input to this contract)

`sniper/analysis/wallet_protocol/types.py:208-262` defines
`ProtocolReport`. Key fields (canonical names; do not rename):

| Field | Type | Source | Notes |
|---|---|---|---|
| `wallet` | str | input | normalized lowercase |
| `protocol_version` | str | const `"v2"` | |
| `analyzed_at` | int | unix s | run wallclock |
| `gate_result` | `GateResult` | Stage 1 | `pass` / `reject_hedge_archetype` / `reject_insufficient_sample` / `reject_dormant` |
| `gate_reason` | str \| None | Stage 1 | human-readable cause |
| `data_quality_flags` | list[str] | Stage 1 | F1-F4 caution flags + tag-mapping ambiguity |
| `dw_roi_point`, `dw_roi_ci_lower`, `dw_roi_ci_upper`, `ew_roi_point` | float \| None | I1 | dollar-weighted vs equal-weighted ROI |
| `flip_rate` | float \| None | mode router | |
| `trading_mode` | `TradingMode` | mode router | `held_analyst_only` / `flipper_only` / `mixed_mode` / `n/a_routing` |
| `held_analyst_branch` | `BranchReport` \| None | held analyst | H1-H7 + tag grains |
| `flipper_branch` | `BranchReport` \| None | flipper | FH1-FH6 + tag grains |
| `overall_verdict` | `OverallVerdict` | §11 mixed-mode matrix | `edge_detected` / `no_edge` / `underpowered` / `data_insufficient` / `operator_review` |
| `primary_edge_type` | str \| None | §11 | `entry_timing` / `size_conviction` / `tag_specialist` / `flipper_skill` / None |
| `copyability_note` | str \| None | §11 | one-sentence operator note |
| `applicable_limitations` | list[str] | §11 | caveats |
| `capital_ledger` | `CapitalLedger` \| None | C1 | onchain in/out flow |
| `n_closed_positions`, `n_held_to_resolution`, `n_open_positions`, `n_open_dead`, `net_pnl`, `activity_rows_fetched`, `activity_cap_hit`, `archetype_layer1` | numerics + str | data summary | |

This spec does not change `ProtocolReport`. The mapping in § 4 reads
these fields verbatim.

### 2.2 Existing tables this contract joins to

- `wallet_registry` (`wallet`, `display_label`, `status`, `gate0_status`,
  `copy_status`, `analysis_status`, `classification`, `notes`,
  `first_seen_ts`, `last_seen_ts`, `created_at`, `updated_at`).
  Wallet-ledger; identity + lifecycle. Not modified by this spec.
- `wallet_memberships` (`wallet`, `scope`, `status`, `label`, `reason`,
  `source`, `metadata_json`, `created_at`, `updated_at`). Followed /
  watchlist relationship. Not modified.
- `wallet_tag_fit` (`wallet`, `tag`, `fit_score` REAL 0..1,
  `fit_rank` INTEGER, `peer_count`, `confidence_flag`, `computed_at`,
  `inputs_json`). Cohort rank — context only.
- `wallet_tag_pnl` (`wallet`, `tag`, `n_positions`, `realized_pnl`,
  `win_rate`, `conviction_concentration`, `computed_at`,
  `win_rate_held_only`, `n_held_to_resolution`, `open_unrealized_pnl`,
  `n_open_positions`, `n_open_dead`). Direct measurements.
- `tag_nav_category` (`tag`, `nav_category`, ...) and `market_tags`
  (`condition_id`, `tag`). Tag taxonomy. Read-only here.

---

## 3. New tables — schemas locked

Three new tables. All written atomically per analysis run.

### 3.1 `wallet_layer5_decision`

Per-wallet operational verdict + cached analytical context. One live row
per `(wallet, analysis_version)`.

```sql
CREATE TABLE IF NOT EXISTS wallet_layer5_decision (
    wallet                          TEXT    NOT NULL,
    analysis_version                TEXT    NOT NULL,           -- e.g. "wpv2.0"
    analyzer_run_id                 TEXT    NOT NULL,           -- references analyzer_runs.run_id (see "FK enforcement" below)

    as_of_ts                        INTEGER NOT NULL,           -- unix s, when verdict derived
    data_window_start_ts            INTEGER,                    -- earliest input row ts
    data_window_end_ts              INTEGER,                    -- latest input row ts

    -- Direct copies of ProtocolReport headline fields (no derivation).
    gate_result                     TEXT    NOT NULL,           -- enum GateResult
    trading_mode                    TEXT    NOT NULL,           -- enum TradingMode (pass-through, not switched on)
    overall_verdict                 TEXT    NOT NULL,           -- enum OverallVerdict
    primary_edge_type               TEXT,                       -- nullable enum
    flip_rate                       REAL,
    n_closed_positions              INTEGER NOT NULL DEFAULT 0,
    n_held_to_resolution            INTEGER NOT NULL DEFAULT 0,
    n_open_positions                INTEGER NOT NULL DEFAULT 0,
    n_open_dead                     INTEGER NOT NULL DEFAULT 0,
    net_pnl_usdc                    REAL    NOT NULL DEFAULT 0, -- maps ProtocolReport.net_pnl (renamed to carry units)
    dw_roi_point                    REAL,                       -- maps ProtocolReport.dw_roi_point
    dw_roi_ci_lower                 REAL,                       -- maps ProtocolReport.dw_roi_ci_lower
    dw_roi_ci_upper                 REAL,                       -- maps ProtocolReport.dw_roi_ci_upper
    ew_roi_point                    REAL,                       -- maps ProtocolReport.ew_roi_point
    activity_cap_hit                INTEGER NOT NULL DEFAULT 0, -- 0/1
    archetype_layer1                TEXT,

    -- Operational decision (this spec's primary output).
    action_decision                 TEXT    NOT NULL,           -- enum ActionDecision (§ 4.1)
    action_reason_codes_json        TEXT    NOT NULL DEFAULT '[]',
    confidence_band                 TEXT    NOT NULL,           -- enum ConfidenceBand (§ 4.3) — display only
    is_current                      INTEGER NOT NULL DEFAULT 1, -- 0/1; persistence flips older versions to 0 when a new version row lands for the same wallet

    -- Cached analytical context as JSON (not authority surfaces).
    held_branch_summary_json        TEXT    NOT NULL DEFAULT '{}',
    flipper_branch_summary_json     TEXT    NOT NULL DEFAULT '{}',
    tag_grain_summary_json          TEXT    NOT NULL DEFAULT '[]',
    capital_ledger_summary_json     TEXT    NOT NULL DEFAULT '{}',
    data_quality_flags_json         TEXT    NOT NULL DEFAULT '[]',
    applicable_limitations_json     TEXT    NOT NULL DEFAULT '[]',
    open_questions_json             TEXT    NOT NULL DEFAULT '[]', -- maps ProtocolReport.open_questions
    copyability_note                TEXT,

    -- Rendered report (already produced by report.py; typical 2-20 KB,
    -- stored inline; SQLite spills oversize values to overflow pages
    -- automatically — the column has no special handling required).
    report_markdown                 TEXT    NOT NULL DEFAULT '',

    created_at                      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (wallet, analysis_version)
);

CREATE INDEX IF NOT EXISTS idx_wld_action_current
    ON wallet_layer5_decision(action_decision, is_current, as_of_ts DESC);
CREATE INDEX IF NOT EXISTS idx_wld_wallet_current
    ON wallet_layer5_decision(wallet, is_current, as_of_ts DESC);
CREATE INDEX IF NOT EXISTS idx_wld_run
    ON wallet_layer5_decision(analyzer_run_id);
```

**Notes.**
- **PK + history.** PK is `(wallet, analysis_version)` not `(wallet)` so
  an analyzer upgrade (`wpv2.0` → `wpv2.1`) does not destroy the prior
  row. There is no separate history table; we keep one row per (wallet,
  version). Within a single version, rerunning overwrites in place via
  the write-time behavior below.
- **Canonical "latest" query.** Consumers MUST filter on
  `is_current = 1` when fetching the live decision for a wallet. The
  query `SELECT * FROM wallet_layer5_decision WHERE wallet = ? AND
  is_current = 1` returns at most one row. The persistence layer is
  responsible for setting `is_current = 0` on all rows for a wallet
  when a new analysis_version row lands (single transaction). Sorting
  by `as_of_ts DESC` alone is **not** correct after a version bump
  because a debug rerun of an old version would surface as "latest" by
  timestamp.
- **`is_current` is mandatory infrastructure, not optional display
  context.** It is bound by § 1 #3 in the sense that no consumer
  *authorizes* on its value alone, but every consumer MUST filter on
  it in the canonical query above. Treating it as an optional field
  is a bug; reading a row with `is_current = 0` and acting on it as
  the live decision is a correctness failure, not a stylistic one.
- **Write-time behavior.** Atomic per-wallet sequence inside one DB
  transaction:
  1. Insert `analyzer_runs` row (commit-of-record for the run, see § 3.3).
  2. `UPDATE wallet_layer5_decision SET is_current = 0 WHERE wallet = ?
     AND analysis_version != ?` (clear stale-version current flags).
  3. `INSERT INTO wallet_layer5_decision … ON CONFLICT(wallet,
     analysis_version) DO UPDATE SET … is_current = 1, updated_at =
     CURRENT_TIMESTAMP` (in-place rerun within the same version).
  Phase 3 implements this in a single helper to make the sequence
  atomic.
- **FK enforcement.** SQLite leaves FK enforcement off by default. The
  persistence layer obtains its connection with `PRAGMA foreign_keys =
  ON` set, AND the write-time sequence above guarantees the `analyzer_
  runs` row exists before the decision row references its `run_id`. The
  PRAGMA enforces the invariant at runtime; the write order makes
  enforcement always pass.
- **Index choices.** `idx_wld_action_current` covers "list me all
  current `validated_copy_candidate` wallets" used by the new listing
  endpoint (§ 6.1) and Phase 5 alerter cutover. `idx_wld_wallet_current`
  covers per-wallet "give me the live decision" reads used by every
  other consumer. `idx_wld_run` covers debugging — "show me everything
  one run produced" — and is small (one row per (wallet, run)).
- **Enums are TEXT, not INTEGER.** CHECK constraints are optional; the
  persistence layer is the enforcement point.
- **JSON columns are blobs.** Held / flipper branch summaries,
  tag-grain summaries, capital ledger, open questions: cached context
  for explanation, never interpreted by gates. Their internal JSON
  shape is intentionally unstructured at the SQL level — Phase 3 plan
  may pin shape in a Python dataclass for serialization, but the SQL
  contract is just "valid JSON, read via run_id if you need raw fields"
  (§ 6.3 routes operators to `report_markdown` instead).
- **`confidence_band` and `is_current`** are constrained by § 1 #3
  (non-authority) and locked in § 4.3 / above respectively. Neither
  may gate behavior.

### 3.2 `position_signal_decision`

Per (wallet, condition_id, outcome) cache of the wallet decision the
analyzer was operating on at the time the signal was observed. Written
atomically with `position_market_state` (§ 5).

```sql
CREATE TABLE IF NOT EXISTS position_signal_decision (
    wallet                          TEXT    NOT NULL,
    condition_id                    TEXT    NOT NULL,
    outcome                         TEXT    NOT NULL,
    analysis_version                TEXT    NOT NULL,

    -- Cached enum from wallet_layer5_decision.action_decision at write time.
    wallet_action_decision          TEXT    NOT NULL,           -- enum ActionDecision

    -- Cached cohort rank for the market's primary tag, context only.
    primary_tag                     TEXT,
    tag_fit_score                   REAL,                       -- 0..1 cohort rank, NOT a gate
    tag_fit_rank                    INTEGER,                    -- ordinal in tag, NOT a gate
    tag_fit_peer_count              INTEGER,
    tag_fit_confidence_flag         TEXT,                       -- 'ok' | 'low_population'

    -- Reason codes that influenced caching at the wallet level.
    signal_reason_codes_json        TEXT    NOT NULL DEFAULT '[]',
    analyzed_ts                     INTEGER NOT NULL,
    created_at                      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (wallet, condition_id, outcome, analysis_version)
);

CREATE INDEX IF NOT EXISTS idx_psd_action
    ON position_signal_decision(wallet_action_decision, analyzed_ts DESC);
CREATE INDEX IF NOT EXISTS idx_psd_wallet
    ON position_signal_decision(wallet, analyzed_ts DESC);
```

**Notes.**
- Column names `tag_fit_score` and `tag_fit_rank` mirror the source
  table (`wallet_tag_fit.fit_score`, `wallet_tag_fit.fit_rank`). They
  are not synthetic composites — `fit_score` is a four-component
  cohort-normalized rank in [0,1], `fit_rank` is an integer ordinal.
  Both are direct cohort measurements. Per § 1 #3 they are context-only
  in every consumer.
- The intent is that consumers read `wallet_action_decision` to
  authorize and `tag_fit_*` to order/size. **No consumer may compare
  `tag_fit_score` to a threshold to authorize** (e.g. `if tag_fit_score
  >= 0.7` is banned in alerter/paper_trader/strategies).
- `position_market_state` (the market-data half of the split, see § 5)
  carries the live market columns. The two are joined as needed.

### 3.3 `analyzer_runs`

Audit table for every protocol invocation. One row per
`run_wallet_protocol` call.

```sql
CREATE TABLE IF NOT EXISTS analyzer_runs (
    run_id                          TEXT    PRIMARY KEY,        -- UUID4
    analysis_version                TEXT    NOT NULL,           -- e.g. "wpv2.0"
    wallet                          TEXT    NOT NULL,
    started_ts                      INTEGER NOT NULL,
    finished_ts                     INTEGER,
    duration_ms                     INTEGER,
    success                         INTEGER NOT NULL,           -- 0/1
    error_class                     TEXT,                       -- exception class name on failure
    error_message                   TEXT,                       -- truncated message
    invocation_source               TEXT NOT NULL DEFAULT '',   -- 'cli'|'http'|'mcp'|'scheduler'|'test'
    args_json                       TEXT NOT NULL DEFAULT '{}', -- e.g. {"with_capital_ledger": true}
    fetch_quality_flags_json        TEXT NOT NULL DEFAULT '[]',
    activity_rows_fetched           INTEGER,
    activity_cap_hit                INTEGER,
    subgraph_fallback_used          INTEGER NOT NULL DEFAULT 0,
    created_at                      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ar_wallet_started
    ON analyzer_runs(wallet, started_ts DESC);
CREATE INDEX IF NOT EXISTS idx_ar_success_started
    ON analyzer_runs(success, started_ts DESC);
```

**Notes.**
- This table is append-only. No update.
- Replaces nothing; the existing `analysis_runs` table semantics are
  retained for the legacy analyzer until Phase 6.

---

## 4. Mapping — `ProtocolReport` → `action_decision`

The mapping is deterministic, total, and unit-tested. No tunable
threshold escapes this section.

### 4.1 `ActionDecision` enum

Six values, taken verbatim from the brain-design Layer 5 spec
(`docs/superpowers/specs/2026-04-22-wallet-understanding-brain-design.md`):

| Value | Operational meaning |
|---|---|
| `validated_copy_candidate` | Edge supported on at least one branch, survives time-split, no blocking caveat. Eligible for paper-copy and (after § 0) for human-approved real-copy. |
| `reverse_engineer_candidate` | Edge supported but mode is mixed or operator-review territory; or held-supported with concentrated-cluster caveat. Worth deeper study; **not** an automated copy authorization. |
| `study` | Edge underpowered or recent regime change; the wallet looks promising but the data is not yet ready. Re-run on schedule. |
| `monitor` | No edge detected, but no anti-pattern either. Keep on the radar; do not act. |
| `exclude_for_copy` | Edge refuted, OR Stage 1 reject_dormant / reject_insufficient_sample. Do not paper-copy or alert. |
| `anti_pattern` | Refuted **and** carries hard caution flags (heavy CTF + dollar-weighted negative; sloppy operator + concentration; hedge-archetype). Effectively blocked. |

`exclude_for_copy` and `anti_pattern` differ in severity for display and
in whether the wallet may still appear in research views.

### 4.2 Mapping function (deterministic)

**Precedence.** `gate_result` dominates `overall_verdict`. Stage 1
rejects bypass every downstream check, even though `ProtocolReport`
defaults `overall_verdict` to `"data_insufficient"` for rejected
wallets. The order of if-statements below is the precedence order.

**Switch dimension.** The mapping is total over
`OverallVerdict × GateResult` (5 × 4 = 20 cells). `TradingMode` is read
inside the `edge_detected` arm only, where `mode_mixed` is one of the
downshift conditions; in every other arm `TradingMode` is a pass-
through column that the persistence layer copies into the row but the
mapping does not switch on. See § 8 #2 for the corresponding
acceptance criterion.

```text
INPUTS   ProtocolReport pr  (canonical fields from § 2.1)
OUTPUT   (action_decision, reason_codes[], confidence_band)

# ---------- 1. Stage 1 hard rejects ----------
if pr.gate_result == "reject_hedge_archetype":
    return ("anti_pattern", ["reject_hedge_archetype"], "n/a")
if pr.gate_result == "reject_dormant":
    return ("exclude_for_copy", ["reject_dormant"], "n/a")
if pr.gate_result == "reject_insufficient_sample":
    return ("exclude_for_copy", ["reject_insufficient_sample"], "n/a")

# At this point gate_result == "pass".
flags = set(pr.data_quality_flags or [])

# ---------- 2. Compute caveat reason codes (display-only) ----------
# These codes ALWAYS get emitted when their condition holds. They never
# by themselves authorize, downshift, or upgrade. Some of them are
# additionally consulted as downshift conditions in the edge_detected
# arm (step 5).

caveat_codes = []

if _dw_negative(pr):
    caveat_codes.append("dw_negative")

if _dw_vs_ew_diverges(pr):
    # DEMOTED: see helper docstring. The 2x cutoff has no statistical
    # derivation; this code is for operator/agent display only and does
    # NOT gate authorization. Re-promotion to a downshift condition
    # requires a permutation study and a minor-version bump.
    caveat_codes.append("dw_vs_ew_divergent")

if _h7_signaled(pr):
    caveat_codes.append("H7_regime_change_signaled")

if "F3_recent_regime" in flags:
    caveat_codes.append("F3_recent_regime")

if _independence_concentrated(pr):
    caveat_codes.append("independence_concentrated")
    caveat_codes.append("domain_specialization_caveat")  # dual-interpretation hint

# ---------- 3. Hard anti-pattern (cross-verdict) ----------
# Caution-flag combinations that signal structural anti-pattern. When
# overall_verdict is anything except edge_detected, these route to
# anti_pattern even if the verdict alone would have routed elsewhere
# (study / monitor / exclude_for_copy / reverse_engineer_candidate).
# When edge_detected is present, the same flags downgrade to
# reverse_engineer_candidate (step 5) but never anti_pattern, because
# Stage 2 has validated an edge.
#
# Note: _independence_concentrated is intentionally NOT in the
# hard_anti_pattern set. Concentration without an edge signal is
# informationally inert — a wallet with no_edge + concentration routes
# to exclude_for_copy via step 4 (the concentration is irrelevant if
# the wallet has no edge to exploit). Concentration only matters when
# combined with edge_detected, which step 5 handles via downshift.

hard_anti_pattern = []
if "F1_sloppy_operator" in flags and "F2_concentration_top3" in flags:
    hard_anti_pattern.append("F1_F2_combined")
if "F4_heavy_ctf" in flags and _dw_negative(pr):
    hard_anti_pattern.append("F4_with_dw_negative")

if hard_anti_pattern and pr.overall_verdict != "edge_detected":
    return ("anti_pattern",
            [pr.overall_verdict] + caveat_codes + sorted(hard_anti_pattern),
            "low")

# ---------- 4. Verdict-specific routing (no edge_detected) ----------
if pr.overall_verdict == "no_edge":
    return ("exclude_for_copy",
            ["no_edge"] + caveat_codes,
            "low")

if pr.overall_verdict == "underpowered":
    return ("study",
            ["underpowered"] + _powered_reason_codes(pr) + caveat_codes,
            "underpowered")

if pr.overall_verdict == "data_insufficient":
    return ("monitor",
            ["data_insufficient"] + caveat_codes,
            "underpowered")

if pr.overall_verdict == "operator_review":
    return ("reverse_engineer_candidate",
            ["operator_review"] + _branch_conflict_codes(pr) + caveat_codes,
            "medium")

# ---------- 5. edge_detected ----------
# The only path that can mint validated_copy_candidate.
if pr.overall_verdict == "edge_detected":
    edge_codes = [
        "edge_detected",
        f"primary_edge_type:{pr.primary_edge_type or 'unspecified'}",
    ]

    # Downshift conditions: structural caveats that don't cross the
    # anti-pattern bar but block "validated" status until human review.
    # _dw_vs_ew_diverges is intentionally NOT in this list (display-only
    # via caveat_codes); see helper docstring.
    downshift_reasons = []
    if "F2_concentration_top3" in flags:
        downshift_reasons.append("F2_concentration_top3")
    if "F3_recent_regime" in flags:
        downshift_reasons.append("F3_recent_regime")
    if pr.trading_mode == "mixed_mode":
        downshift_reasons.append("mode_mixed")
    if _independence_concentrated(pr):
        downshift_reasons.append("independence_concentrated")

    if hard_anti_pattern:
        # Edge detected but with hard caution-flag combos: route to
        # human review, never validated.
        return ("reverse_engineer_candidate",
                edge_codes + caveat_codes + sorted(hard_anti_pattern),
                "medium")

    if downshift_reasons:
        return ("reverse_engineer_candidate",
                edge_codes + caveat_codes + sorted(set(downshift_reasons)),
                "medium")

    # Clean edge_detected: validated.
    band = "high" if _stationary_and_robust(pr) else "medium"
    return ("validated_copy_candidate", edge_codes + caveat_codes, band)

# Unreachable — exhaustive on OverallVerdict literal.
raise NotImplementedError(pr.overall_verdict)
```

**Helpers** (defined in `wallet_layer5_mapping.py`, Phase 3; every
helper is a pure function of `pr` — no DB / env / clock reads, no
module-level mutable state):

- `_dw_negative(pr) -> bool` — `pr.dw_roi_point is not None and
  pr.dw_roi_point < 0`.

- `_dw_vs_ew_diverges(pr) -> bool` — DISPLAY-ONLY descriptor; never
  authorizes or downshifts. Returns `True` when both ROIs are present
  AND either:
  1. `pr.dw_roi_point * pr.ew_roi_point < 0` (sign flip, e.g.
     ew=+0.04 / dw=−0.04 — the textbook concentration-driven loss
     case), OR
  2. `abs(pr.dw_roi_point - pr.ew_roi_point) > 2 * max(abs(pr.ew_roi_point), 0.05)`.

  The 2× cutoff and 0.05 floor are unvalidated heuristics; the helper
  is intentionally non-authoritative because the constants have no
  statistical derivation in the project's data. Promotion to a
  downshift condition requires a permutation study and a minor-version
  bump (§ 9 #6).

- `_h7_signaled(pr) -> bool` — `True` iff any active branch's
  `stationarity is not None and stationarity.signaled == True`.

- `_independence_concentrated(pr) -> bool` — `True` iff any active
  branch's `independence_check is not None and independence_check.flag
  == "concentrated"`. Concentration may reflect legitimate domain
  specialization rather than statistical noise; the mapping emits both
  `independence_concentrated` and `domain_specialization_caveat` so
  operators see the dual interpretation.

- `_stationary_and_robust(pr) -> bool` — Returns `False` if NO branch
  is active (`held_analyst_branch is None and flipper_branch is None`)
  — the universal-quantifier-over-empty-set otherwise vacuously
  produces `True` and would auto-band a no-branch verdict to `high`,
  which is wrong. Otherwise returns `True` iff for **every** active
  branch ALL of:
  (a) `stationarity.signaled == False`,
  (b) `stationarity.pre_regime_n >= MIN_CUSUM_N` (so (a) is
      informative — CUSUM with too few points is uninformatively
      negative),
  (c) the branch's primary hypothesis (H1 for held, FH1 for flipper)
      is **not** flagged with `underpowered_reason == "n_below_threshold"`.
  `MIN_CUSUM_N` is fixed at `30` in this analysis_version, named
  `Final[int]` in `wallet_layer5_mapping.py`, and is the only constant
  the mapping reads from outside `pr`. A change to `MIN_CUSUM_N` is a
  minor-version bump.

  **Note on `MIN_CUSUM_N=30` vs. `TIME_SPLIT_MIN_N=60`** (in
  `sniper/analysis/wallet_protocol/statistics.py`). The CUSUM floor is
  intentionally lower than the time-split floor because: (i) CUSUM at
  `h=25, k=0.5σ` (calibrated against 26,359 positions) needs roughly
  half the sample of a powered OOS split to detect a regime change;
  (ii) `confidence_band` is display-only (§ 1 #3) so a "high" band on
  a 30-CUSUM/60-OOS wallet cannot mis-authorize. If a future Phase
  decides "high" should require both floors aligned, that is a minor-
  version bump. Documented here because the 30/60 gap is non-obvious
  to a future reader.

- `_powered_reason_codes(pr) -> list[str]` — one code per underpowered
  hypothesis in the format `"H<n>_underpowered:<UnderpoweredReason>"`
  (colon separator, lowercase reason). Examples:
  `"H1_underpowered:n_below_threshold"`,
  `"FH3_underpowered:zero_variance"`. Returns `[]` when no branch is
  active or no hypothesis is underpowered.

- `_branch_conflict_codes(pr) -> list[str]` — emits one code per active
  branch describing its primary-hypothesis verdict. Locked vocabulary:
  `held_branch_supported`, `held_branch_refuted`,
  `held_branch_inconclusive`, `held_branch_underpowered`,
  `flipper_branch_supported`, `flipper_branch_refuted`,
  `flipper_branch_inconclusive`, `flipper_branch_underpowered`.
  Inactive branches contribute no code. No other strings may be
  emitted.

- `_which_caveat` — REMOVED. Its prior responsibility (naming the
  reason codes that explain a downshift) is now handled directly in
  step 5: `downshift_reasons` is the literal output, drawn from the
  locked vocabulary `F2_concentration_top3`, `F3_recent_regime`,
  `mode_mixed`, `independence_concentrated`. No helper indirection.

**Null-branch safety.** Helpers that reference
`pr.held_analyst_branch` or `pr.flipper_branch` MUST handle `None`
explicitly. Implementations return the safe default (`False` for
booleans, `[]` for list emitters). `try / except AttributeError`
patterns are forbidden — they hide bugs and produce non-deterministic
behavior.

**Determinism.** The mapping is a *pure function* of `pr`. The Phase 3
plan adds a unit test that asserts the same `ProtocolReport` produces
the same `(action_decision, sorted(reason_codes), confidence_band)`
tuple on every invocation, across processes, and that no helper opens
a connection / reads `os.environ` / calls `time.time()`.

### 4.3 `ConfidenceBand` enum

Bands of decision confidence. Display order and audit only. Bound by the
same non-authority rule as `tag_fit_score` / `tag_fit_rank` — see § 1 #3.

| Band | Meaning | Set by |
|---|---|---|
| `high` | edge_detected, no caveats, stationary, n robust | clean validated_copy_candidate |
| `medium` | edge_detected with one caveat, OR operator_review | reverse_engineer_candidate, downshifted validated |
| `low` | no_edge, exclude_for_copy, anti_pattern (with reasons) | refuted-side outcomes |
| `underpowered` | underpowered or data_insufficient | study, monitor |
| `n/a` | Stage 1 rejects | hard-rejected wallets |

Band values are deterministic from the mapping function in § 4.2.

**Consumer guardrail.** Any consumer may sort or filter
`wallet_layer5_decision` rows by `confidence_band` *within a single
`action_decision` bucket* (e.g. order `validated_copy_candidate` rows
high → medium for display priority). No consumer may use the band to
escalate or downgrade authorization across `action_decision` buckets,
to gate copy/alert behavior, or to compute a score from. Phase 8 test
`test_no_synthetic_scores_program_wide.py` enforces this with a static
grep for `confidence_band ==` and `confidence_band IN` in non-display
files.

### 4.4 Reason-code taxonomy

Reason codes are short snake_case strings, stored in JSON arrays, used
for human display and for tests to assert mapping correctness. Every
code emitted by the mapping in § 4.2 is in the locked list below; no
mapping path may emit a code not listed here.

**Stable codes** (this spec locks them; renames require a new
`analysis_version`):

**Stage 1 gates** (emitted by step 1 of § 4.2):
- `reject_hedge_archetype`
- `reject_insufficient_sample`
- `reject_dormant`

**Verdicts** (emitted by steps 3–5 as the leading code):
- `edge_detected`
- `no_edge`
- `underpowered`
- `data_insufficient`
- `operator_review`

**Edge type** (emitted by step 5 with colon delimiter, mirroring the
`OverallVerdict.primary_edge_type` Literal):
- `primary_edge_type:entry_timing`
- `primary_edge_type:size_conviction`
- `primary_edge_type:tag_specialist`
- `primary_edge_type:flipper_skill`
- `primary_edge_type:unspecified` — emitted only when `pr.primary_edge_type
  is None` *and* `pr.overall_verdict == "edge_detected"`. This is the
  legitimate "edge detected, type not classified by the router" case;
  consumers may surface it as an open question, but it is not a bug.

**Caution flags** (emitted from `data_quality_flags`):
- `F1_sloppy_operator`
- `F2_concentration_top3`
- `F3_recent_regime`
- `F4_heavy_ctf`

**Hard-flag combinations** (emitted by step 3 / step 5 when caution
flags co-occur):
- `F1_F2_combined`
- `F4_with_dw_negative`

**Stationarity / regime change**:
- `H7_regime_change_signaled` — emitted whenever `_h7_signaled(pr)` is
  true, regardless of authorization outcome.

**Independence / concentration**:
- `independence_concentrated` — emitted when `_independence_concentrated(pr)`
  is true.
- `domain_specialization_caveat` — co-emitted alongside
  `independence_concentrated` to remind operators that concentration
  may reflect legitimate specialization, not noise. Always paired.

**Capital-flow descriptors** (display-only, never gates):
- `dw_negative` — `pr.dw_roi_point < 0`.
- `dw_vs_ew_divergent` — `_dw_vs_ew_diverges(pr)` true.

**Mode-router hint** (emitted by step 5 only):
- `mode_mixed` — `pr.trading_mode == "mixed_mode"`.

**Branch-conflict codes** (emitted by `_branch_conflict_codes` in
step 4 `operator_review` arm; locked vocabulary, no other strings
permitted):
- `held_branch_supported`
- `held_branch_refuted`
- `held_branch_inconclusive`
- `held_branch_underpowered`
- `flipper_branch_supported`
- `flipper_branch_refuted`
- `flipper_branch_inconclusive`
- `flipper_branch_underpowered`

**Underpowered-hypothesis codes** (emitted by `_powered_reason_codes`;
parametric on `UnderpoweredReason` from `types.py`; **colon delimiter**):
- `H1_underpowered:n_below_threshold`
- `H1_underpowered:heavy_tail_inconclusive`
- `H1_underpowered:regime_change_too_recent`
- `H1_underpowered:no_execution_profile`
- `H1_underpowered:no_price_history_fn`
- `H1_underpowered:peer_count_below_floor`
- `H1_underpowered:zero_variance`
- (same suffix set for `H2`, `H3`, `H4`, `H5`, `H6`, `H7`, `FH1`–`FH6`)
- `H2_sub080_only_supported` — emitted when full H2 refuted but the
  sub-0.80 entry-price slice was supported.

**Hypothesis diagnostics** (display-only, surfaced when relevant):
- (none locked beyond the underpowered set above; future minor
  versions may add).

**Data quality**:
- `activity_cap_hit_subgraph_used`
- `payback_capitalization_warning`
- `tag_grain_disagreement_leaf_vs_nav`
- `tag_market_stats_unavailable`

**Extension policy.** New codes may be added in non-breaking minor
versions (no rename of existing codes). Each addition requires:
1. A one-line entry under § 4.4 Stable codes in this spec.
2. Documenting which mapping arm emits it.
3. A unit-test fixture vector exercising the emission path.

A code rename or removal forces an `analysis_version` minor bump.

---

## 5. `position_signals` table — disposition

The migration plan § 4.6 left this open ("verify or replace"). Phase 2
spec locks the verdict:

**Verdict: split, do not preserve `position_signals` as-is.**

Phase 3 implementation:

1. Create `position_market_state` (NEW) with the validated market-data
   columns from `position_signals`. The validation procedure: for each
   currently-used column in `position_signals`, trace the writer in
   `db_signals.py` and confirm it is a raw market measurement (CLOB
   trade, Polymarket API field, derived only from those). Columns whose
   writer derives a value from any legacy score are not migrated.
2. Create `position_signal_decision` (NEW) per § 3.2.
3. Atomically dual-write both tables from the analyzer when a wallet's
   open positions are processed.
4. Provide a temporary view `position_signals` = LEFT JOIN of the two
   tables, for the duration of Phase 4–5 only, scoped to the
   already-validated subset of columns. Drop the view in Phase 8.
5. Score-bearing columns of the original `position_signals` are not
   recreated.

The `position_market_state` schema is owned by the Phase 3 plan, not
this spec, because the column-by-column validation must complete first.

**View column-list freeze.** Before any Phase 4 consumer is written
against the temporary `position_signals` view, the Phase 3 plan must
publish the final list of columns the view exposes. Once published,
the column list is frozen for the duration of Phases 4–5. Adding a
column to the view after a consumer ships is permitted; removing or
renaming one is not. Phase 8 drops the view; consumers migrate to
direct reads of `position_market_state` and `position_signal_decision`
before Phase 8 lands.

---

## 6. API surface

### 6.1 HTTP — new blueprint `routes/api_wallets.py`

| Method | Path | Returns | Notes |
|---|---|---|---|
| `GET` | `/api/wallet-decision/<wallet>` | 200 `WalletDecisionResponse` | reads the `is_current = 1` row from `wallet_layer5_decision`; see "Status semantics" below for the empty / stale-version cases |
| `GET` | `/api/wallet-decisions?action_decision=<enum>&analysis_version=<v>&limit=<n>` | 200 `WalletDecisionListResponse` | bulk listing keyed off `idx_wld_action_current`; for alerter / paper-trader cohort queries |
| `POST` | `/api/analyze-wallet` body `{wallet, with_capital_ledger?}` (header `Idempotency-Key` required) | 202 `{run_id, status: "queued" \| "deduplicated"}` | enqueues a `run_wallet_protocol` invocation; idempotency contract below |
| `GET` | `/api/analyzer-run/<run_id>` | 200 `AnalyzerRunStatus` | polls `analyzer_runs` |
| `DELETE` | `/api/wallet-decision/<wallet>?analysis_version=<v>` | 204 | operator override; marks the row `is_current = 0` and emits an audit memory; does not hard-delete |

**`WalletDecisionResponse` JSON** (canonical field names — every field
is either an enum, a direct measurement, a non-authority cohort rank,
or a JSON blob; no synthetic composite scores):

```json
{
  "status": "ok",
  "wallet": "0x…",
  "analysis_version": "wpv2.0",
  "is_current": true,
  "as_of_ts": 1782500000,

  "action_decision": "validated_copy_candidate",
  "action_reason_codes": ["edge_detected", "primary_edge_type:entry_timing"],
  "confidence_band": "high",

  "overall_verdict": "edge_detected",
  "primary_edge_type": "entry_timing",
  "trading_mode": "held_analyst_only",
  "gate_result": "pass",

  "n_closed_positions": 87,
  "n_held_to_resolution": 64,
  "n_open_positions": 12,
  "n_open_dead": 3,
  "net_pnl_usdc": 12345.0,
  "dw_roi_point": 0.34,
  "dw_roi_ci_lower": 0.18,
  "dw_roi_ci_upper": 0.51,
  "ew_roi_point": 0.41,
  "activity_cap_hit": false,
  "archetype_layer1": "directional_discretionary",

  "data_quality_flags": [],
  "applicable_limitations": [],
  "open_questions": [],
  "copyability_note": "Held-mode; entry-timing edge on geo tag.",

  "report_markdown_url": "/api/wallet-decision/0x…/report.md"
}
```

The `report_markdown_url` is a sub-route returning `report_markdown`
as `text/markdown`.

**Status semantics for `GET /api/wallet-decision/<wallet>`.** The
endpoint always returns 200 with a `status` field; consumers must
read the field, not rely on HTTP status:

| `status` | Meaning | Other fields |
|---|---|---|
| `ok` | Live decision row exists at the current analysis_version | full body as above |
| `stale_version` | A row exists for the wallet but only at an older analysis_version | body populated from the older row; `is_current` is `false`; consumer should treat result as advisory |
| `never_analyzed` | No row in `wallet_layer5_decision` for this wallet | only `wallet` and `status` present; `action_decision` absent |

**Idempotency contract for `POST /api/analyze-wallet`.** The
`Idempotency-Key` request header is required (HTTP 400 if absent).
The server stores `(idempotency_key, wallet, args_hash) → run_id`
for 24 hours.

- **Same `Idempotency-Key`, same `(wallet, args_hash)`** within 24 h:
  return `{run_id: <existing>, status: "deduplicated"}` (HTTP 202).
  Does not enqueue a second analyzer run.
- **Same `Idempotency-Key`, *different* `(wallet, args_hash)`** within
  24 h: return HTTP 422 Unprocessable Entity with body
  `{error: "idempotency_key_collision", existing_target: {wallet, args_hash}}`.
  This prevents one client's retry-key from silently colliding with
  another client's distinct request, per RFC draft-ietf-httpapi-
  idempotency-key-header.
- **New `Idempotency-Key`**: enqueue and return
  `{run_id: <new>, status: "queued"}` (HTTP 202).

This closes the § 9 #5 race deterministically.

**`AnalyzerRunStatus` JSON shape:**

```json
{
  "run_id": "f1a2…",
  "wallet": "0x…",
  "analysis_version": "wpv2.0",
  "started_ts": 1782500000,
  "finished_ts": 1782500087,
  "duration_ms": 87234,
  "status": "succeeded",
  "error_class": null,
  "error_message": null,
  "decision_written": true,
  "subgraph_fallback_used": false,
  "activity_cap_hit": false
}
```

`status ∈ {"queued", "running", "succeeded", "failed", "cancelled"}`.

**`WalletDecisionListResponse` JSON shape:**

```json
{
  "filter": {
    "action_decision": "validated_copy_candidate",
    "analysis_version": "wpv2.0",
    "limit": 50
  },
  "rows": [ { … abbreviated WalletDecisionResponse … } ],
  "next_cursor": null
}
```

Each row in `rows` is a `WalletDecisionResponse` minus
`report_markdown_url` and `open_questions` (for response-size
control). Cursor pagination by `as_of_ts DESC`.

**Sync vs async on `POST /api/analyze-wallet`.** Always async (202).
Justification: a single run can hit the activity cap and trigger
subgraph fallback, pushing duration into multi-minute territory.
Synchronous responses with a server-side timeout would silently fail
on the slow-path wallets that most need analysis. Consumers poll
`/api/analyzer-run/<run_id>`; the UI displays a progress indicator.

### 6.2 MCP — new module `mcp_tools/analyze_tools.py`

| Tool | Inputs | Output | Behavior |
|---|---|---|---|
| `analyze_wallet(wallet, with_capital_ledger=False, idempotency_key)` | wallet str, opt bool, str | `{run_id, status: "queued" \| "deduplicated"}` | async kickoff; idempotency_key required, mirrors HTTP contract |
| `get_wallet_decision(wallet)` | wallet str | `WalletDecisionResponse` (always with `status` field; never bare `null`) | sync read; same status enum as HTTP |
| `list_wallet_decisions(action_decision, analysis_version=None, limit=50)` | enum str, opt str, opt int | `WalletDecisionListResponse` | bulk listing |
| `get_analyzer_run(run_id)` | run_id str | `AnalyzerRunStatus` | poll status |
| `invalidate_wallet_decision(wallet, analysis_version, reason)` | str, str, str | `{ok: bool}` | operator override; mirrors `DELETE` endpoint; writes a brain memory tagged `wallet-decision-override` |

`mcp_tools/read_tools.py:get_wallet_profile` is **extended**: when a
row exists in `wallet_layer5_decision`, the response includes
`"wallet_decision": <WalletDecisionResponse>`. Legacy score fields
are still returned during Phases 4–7 but flagged `_deprecated_*`.

### 6.3 What is *not* exposed

These fields exist in the DB / `ProtocolReport` and are deliberately
**not** in any HTTP/MCP authority surface:

- `held_branch_summary_json`, `flipper_branch_summary_json`,
  `tag_grain_summary_json`, `capital_ledger_summary_json` — exposed
  only via the markdown report or the dossier knowledge file. Agents
  that need raw hypothesis numbers read those; the JSON columns are
  intentionally unstructured at the SQL level (§ 3.1) so consumers
  cannot mistake them for stable contracts.
- `wallet_tag_fit.fit_score` / `fit_rank` are not in
  `WalletDecisionResponse` because fit is per-tag, not per-wallet.
  They surface in `position_signal_decision.tag_fit_score` /
  `tag_fit_rank` for a specific market, bound by § 1 #3.

### 6.4 Numeric-field consumer guardrail

`net_pnl_usdc`, `dw_roi_point`, `dw_roi_ci_lower`, `dw_roi_ci_upper`,
`ew_roi_point`, and `n_*` counts are direct measurements (allowed by
§ 1 #2). They appear in `WalletDecisionResponse` for display, audit,
and report rendering. They MUST NOT be compared to thresholds in any
production-decision path. The pattern `if net_pnl_usdc >= X and
dw_roi_point >= Y: authorize(...)` is the school-project pattern with
one less weight; banned.

Phase 8 test `test_no_synthetic_scores_program_wide.py` enforces this
with static checks in **two layers**:

1. **Python files** (non-display, i.e. outside `static/`,
   `templates/`, `routes/api_*` response builders, and
   `sniper/web/serializers.py`): grep for `net_pnl_usdc`,
   `dw_roi_point`, `ew_roi_point`, `confidence_band`, `tag_fit_score`,
   `tag_fit_rank` followed by comparison operators (`==`, `>=`, `<=`,
   `>`, `<`, `IN`, `!=`).
2. **SQL files and string literals** (`.sql` files plus Python `str`
   constants containing `WHERE`, `HAVING`, `CASE WHEN`): grep for the
   same field names followed by SQL comparison operators. This closes
   the bypass where a route builds a SQL string at runtime that gates
   on `confidence_band = 'high'`.

Whitelist: `routes/api_wallets.py` listing endpoint may filter on
`action_decision` (an enum, not a measurement) and `is_current` (a
mandatory infrastructure field). No other field is exempt.

---

## 7. Versioning

- `analysis_version` is a string of the form `wp` + protocol-major +
  `.` + protocol-minor (e.g. `wpv2.0`, `wpv2.1`). Major bumps when the
  Stage 2 hypothesis set changes. Minor bumps when the mapping in § 4
  or reason-code taxonomy in § 4.4 changes.
- A consumer that reads `wallet_layer5_decision` filters by
  `analysis_version >= '<min_version>'` if it requires a particular
  mapping shape.
- Old rows are retained until Phase 8. No automatic upgrade in place.
- The mapping function (§ 4.2) is unit-tested per `analysis_version`.
  When the mapping changes, the new test set lives next to the old; old
  is preserved until rows of that version are gone.

---

## 8. Acceptance criteria

This spec is "locked" when the following hold. Each line is a binary
check; signoff requires every line green.

1. The schemas in § 3.1, § 3.2, § 3.3 pass `schema-architect` review.
2. The mapping in § 4.2 is total over `OverallVerdict × GateResult`
   (5 × 4 = 20 cells). `TradingMode` is a pass-through column at the
   row level and is read only inside the `edge_detected` arm where
   `mode_mixed` is one of four downshift conditions; it is not a
   switch dimension elsewhere. A reviewer can hand-trace any
   `(overall_verdict, gate_result)` pair to a single output without
   ambiguity, and the `edge_detected` arm separately produces the
   same `(action_decision, confidence_band)` regardless of
   `TradingMode` when no downshift condition fires.
3. The reason-code taxonomy in § 4.4 covers every code emitted by
   the mapping in § 4.2 (mechanical grep check); no orphan codes in
   either direction.
4. No field added in this spec — table column, JSON key, or API
   response key — authorizes by itself. `confidence_band`,
   `tag_fit_score`, `tag_fit_rank`, `is_current` are all reviewed
   against migration-plan § 0 #4 and are bound by § 1 #3 of this
   spec (display order / context only).
5. The HTTP and MCP surfaces in § 6 are signed off by the next
   `platform-reviewer` invocation, including: idempotency contract
   on `POST /api/analyze-wallet`, `status` enum on
   `GET /api/wallet-decision/<wallet>`, listing endpoint paired with
   `idx_wld_action_current`, operator-override `DELETE` route.
6. Sample mapping vectors are written down in
   `tests/fixtures/wallet_layer5_mapping_vectors.json` (Phase 3
   implements; this spec names the file and the minimum coverage):
   - one vector per `ActionDecision` value (six total)
   - one vector per Stage 1 reject (three)
   - one vector for `dw_vs_ew_divergent` emission **without**
     downshift (proves it stays display-only)
   - one vector for the `dw_vs_ew_divergent` sign-flip case
     (`ew=+0.04`, `dw=−0.04`) confirming the code is emitted
   - one vector for `F1+F2` anti-pattern path with
     `overall_verdict == "no_edge"` (routes to `anti_pattern`)
   - one vector for `F1+F2` anti-pattern path with
     `overall_verdict == "underpowered"` (also routes to
     `anti_pattern`, proves the cross-verdict fix)
   - one vector for `F1+F2` anti-pattern path with
     `overall_verdict == "edge_detected"` (routes to
     `reverse_engineer_candidate`, NOT `anti_pattern` — edge takes
     precedence)
   - one vector for `flipper_only` mode + `edge_detected` (clean
     validated, proves TradingMode pass-through)
   - one vector for `mixed_mode` + `edge_detected` (downshift to
     `reverse_engineer_candidate` with `mode_mixed` reason code)
   - one vector for `_stationary_and_robust` band downgrade due to
     `pre_regime_n < MIN_CUSUM_N` (proves the laundering fix)
7. The plan section "Critical retirement blockers" (migration plan §
   5.2) row "`wallet_layer5_decision` populated for all currently-
   followed + watchlisted wallets" can be operationalized: every
   currently-followed wallet has a `WalletDecisionResponse` round-
   trip that produces JSON without nulls in required fields and
   satisfies the `status: "ok"` precondition.

---

## 9. Open issues to resolve before Phase 3 plan

These are unresolved questions this spec deliberately defers but flags
so the Phase 3 plan author has a closed list:

1. **Where does `tag_market_stats` write?** `compute_tag_market_stats`
   exists with tests but no persistence. Phase 3 plan must pick: new
   table `tag_market_stats`, persisted JSON in `wallet_layer5_decision.
   tag_grain_summary_json`, or compute-on-read. This spec does not
   prescribe; the choice does not affect the contract above.
2. **`tag_nav_category` backfill cadence.** Currently ~50/600. Phase 3
   plan owns the backfill script (`scripts/sync_polymarket_tags.py`).
3. **`wallet_archetype_extractor` Layers 3–4.** Partial today.
   Tag-grain disagreement reason codes assume Layer 3 / 4 are
   populated; if not, the analyzer emits
   `tag_market_stats_unavailable` and the mapping continues without
   them.
4. **Subgraph fallback verbosity.** When `activity_cap_hit_subgraph_
   used` fires, should the report markdown include subgraph counts? UX
   decision, defer to Phase 4.
5. ~~**Concurrent runs of the same wallet.**~~ **CLOSED** by § 6.1
   `Idempotency-Key` contract on `POST /api/analyze-wallet` plus the
   write-time sequence locked in § 3.1 notes (`UPDATE … is_current=0`
   then `INSERT … ON CONFLICT(wallet, analysis_version) DO UPDATE`).
   Two POSTs with the same `Idempotency-Key` collapse to one run; two
   POSTs with different keys race in `analyzer_runs` but converge on
   the same final `wallet_layer5_decision` row via the ON CONFLICT
   path. The Phase 3 plan owns the connection-level lock choice
   (single-writer queue vs. SQLite BEGIN IMMEDIATE) but the
   correctness contract is locked here.
6. **Mapping correction loop.** If the mapping in § 4.2 is later
   judged wrong (e.g. someone wants to re-promote `_dw_vs_ew_diverges`
   to a downshift condition after a permutation study), the path is a
   minor-version bump (`wpv2.0` → `wpv2.1`), recompute every row, set
   the new rows' `is_current = 1` and the old rows' `is_current = 0`
   in a single transaction. The contract does not allow silently
   changing the mapping under the same version.
7. **`analyzer_runs` retention policy.** Append-only, no TTL today.
   Growth at one row per (wallet × scheduled run) per day for ~50
   followed wallets ≈ 18k rows/year — fine. Phase 3 plan may add a
   `scripts/db_prune.py` rule that drops `analyzer_runs` rows older
   than 12 months whose `success = 1` and that have no referencing
   `wallet_layer5_decision.analyzer_run_id` (i.e., the run was
   superseded by a later run for the same (wallet, version)).
   Failures are kept indefinitely as audit trail.
8. **Operator override audit.** `DELETE /api/wallet-decision/<wallet>`
   and `invalidate_wallet_decision` MCP tool both write a Supabase
   brain memory tagged `wallet-decision-override/<wallet>` with the
   reason field, so future operators see why a row was invalidated.
   Phase 3 plan implements the memory writer.

---

## 10. Self-review checks against migration plan

(For the reviewer to confirm before sign-off.)

- [ ] § 0 rule #4 — synthetic composites: spec defines no such field
  as authority. `confidence_band`, `tag_fit_score`, `tag_fit_rank`,
  `is_current`: all bound by § 1 #3 (display / context only). ✓
- [ ] § 0 rule #5 — verdict-vs-decision separation: § 4 distinguishes
  them, mapping is pure-function, helpers are unit-testable. ✓
- [ ] § 0 rule #6 — feature tables don't authorize: `wallet_tag_fit`
  is read only into `position_signal_decision` as cached context;
  consumer guardrail at § 6.4 plus Phase 8 grep test. ✓
- [ ] No `wallet_layer5_decision_history` introduced. ✓
- [ ] `tag_market_stats` claim is honest: function exists with tests,
  persistence path unresolved (§ 9 #1). ✓
- [ ] Existing tables `wallet_registry`, `wallet_memberships`,
  `wallet_tag_fit`, `wallet_tag_pnl` are not modified by this spec —
  only joined to. ✓
- [ ] `_dw_vs_ew_diverges` is demoted to display-only with sign-flip
  clause; promotion requires permutation study + minor-version bump
  (§ 4.2 helper docstring + § 9 #6). ✓
- [ ] `_stationary_and_robust` includes `pre_regime_n >= MIN_CUSUM_N`
  power check (§ 4.2 helper). ✓
- [ ] Hard caution flags (`F1_F2_combined`, `F4_with_dw_negative`)
  evaluated for all `pass`-gate verdicts, not only `no_edge`
  (§ 4.2 step 3). ✓
- [ ] All reason codes emitted by § 4.2 listed in § 4.4; format
  `H<n>_underpowered:<reason>` locked with colon delimiter. ✓
- [ ] API surface: `Idempotency-Key` required on POST,
  `WalletDecisionResponse.status` enum differentiates
  `ok`/`stale_version`/`never_analyzed`, `AnalyzerRunStatus` shape
  defined, `DELETE` operator endpoint and listing endpoint paired
  with `idx_wld_action_current`. ✓

---

## Appendix A — agents to run before Phase 3

In strict order before Phase 3 plan is written:

1. `schema-architect` — review § 3 schemas and § 5 split decision.
2. `platform-reviewer` — adversarial pass on § 4 mapping + § 6 API
   surface; look for fields a future reader could mis-treat as
   authority.
3. `research-methodologist` — sanity check that the mapping in § 4.2
   does not encode a hidden composite score (especially the
   `_dw_vs_ew_diverges` and `_stationary_and_robust` helpers).

A blocker from any of the three rolls back to spec revision before
Phase 3 starts.
