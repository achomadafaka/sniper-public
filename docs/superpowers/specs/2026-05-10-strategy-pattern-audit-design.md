# Strategy Pattern Audit Design

**Date:** 2026-05-10
**Status:** design draft; implementation not started
**Surface:** `strategy_pattern_audit_v1`
**Core rule:** extract reusable behavioral theses, reject weak/noisy patterns, name data gaps

---

## 0. Framing

The wallet analyzer is a **strategy-intelligence system**, not a copy bot.
Its job is to:

1. Extract reusable behavioral theses from wallets,
2. Reject weak/noisy patterns,
3. Identify what data would be needed to test promising ideas.

The Strategy Pattern Audit is the surface that turns a curated population of
per-wallet `ProtocolReport`s into a **thesis catalog**. Each catalog entry is
an explicit, falsifiable behavioral pattern with explicit population support,
explicit anti-pattern criteria, and explicit data gaps. It does **not**
recommend trades. It does **not** simulate copy strategies. It does **not**
emit triggers, signals, or sizing.

The audit's role in the lineage:

```text
W1 (Edge Surface)            ─┐
Exit Attribution              │
Completeness Surface          ├──> Strategy Pattern Audit ──> Thesis Catalog
Timing Surface (W3)           │       (this spec)              (display + curation)
Position Building (W2)        │
Other curated dossiers       ─┘
```

Each wallet's surfaces describe one wallet. The audit cross-cuts wallets to
find recurring shape across the population.

---

## 1. Core Boundary

Use the same discipline pattern as W1/W2/W3/Exit Attribution/Completeness:

1. Pure compute over already-computed surfaces.
2. No I/O, no live API, no price-history calls, no analyzer batches.
3. No DB writes from inside the core.
4. No verdict / gate / `primary_edge_type` / `copyability_note` mutation.
5. No simulator. No trigger logic. No copy fields. No sizing recommendations.

Conceptual API:

```python
def compute_strategy_pattern_audit(
    reports: Sequence[ProtocolReport],
    *,
    rules: Sequence[ThesisRule] | None = None,
    audit_run_id: str | None = None,
    operator_priors: Mapping[str, Any] | None = None,
) -> dict | None:
    ...
```

Core auditor must not:

- import DB or ledger modules from the core,
- query ledger / Supabase / SQLite tables from the core,
- call live APIs,
- call price-history functions or endpoints,
- read or write files,
- mutate persistence,
- run analyzer batches,
- restart services,
- touch `overall_verdict`, `gate_result`, `primary_edge_type`, or
  `copyability_note` on the input reports,
- emit simulator, trigger, copy, or sizing recommendations,
- claim that a pattern "predicts" forward outcomes (the audit is descriptive;
  predictive claims require a separate validation surface with its own data).

Return `None` only when there are zero usable input reports. If reports exist
but none qualify under any rule, return a catalog with an empty `theses` list
and explicit `population_quality` caveats — do not let "no thesis" hide
"input population was too thin to evaluate."

### 1.1 Input contract

Each input is a `ProtocolReport` (per `sniper.analysis.wallet_protocol.types`).
The audit reads only the fields it declares. It does not depend on:

- `overall_verdict`,
- `gate_result`,
- `primary_edge_type`,
- `copyability_note`.

These fields are operator-curation outputs and must not feed pattern detection
(otherwise the audit would be circularly validating the existing curation).

The audit MAY read:

- per-surface descriptive fields (`outcome_side_breakdown`,
  `exit_attribution`, `data_completeness`, `timing_surface`,
  `position_building_surface`, `extractor_features_summary`,
  `execution_quality_summary`),
- archetype labels (`archetype_layer1`, `archetype_layer2`),
- coverage / sample-size fields (`n_closed_positions`, `n_open_positions`,
  `activity_cap_hit`, `subgraph_fallback_used`, `walk_full_history`,
  `n_unmapped_fills`),
- data-quality flags (`data_quality_flags`, `applicable_limitations`,
  `open_questions`),
- branch reports (`held_analyst_branch`, `flipper_branch`) for descriptive
  per-hypothesis details (NOT verdicts).

If a rule needs a field not on `ProtocolReport`, the rule must be marked as
`needs_data` (see §3.5) rather than silently degraded.

### 1.2 Operator priors

`operator_priors` is an optional dict letting the operator parameterize:

- minimum population for a thesis to survive (default: 10 wallets),
- minimum observation-quality share (default: 0.50 of supporting wallets must
  have at least `partial` quality on the surfaces the rule reads),
- forbidden archetype combinations (e.g., a thesis that relies on directional
  market selection must not fire on `arb_fund` archetype),
- inclusion / exclusion of known-bot wallets,
- coverage caveats that disqualify a wallet from being a supporter.

Defaults are conservative. Anything not specified in `operator_priors` falls
back to the defaults documented in §6.

Operator relaxations have hard lower bounds. The auditor must clamp or reject
priors below:

- `min_population_evaluated >= 5`,
- `min_supporting >= 2`,
- `min_observation_quality_share >= 0.20`,
- `min_archetype_diversity >= 1`,
- `min_n_closed_positions_per_wallet >= 1`.

These floors prevent an operator override from converting one anecdotal wallet
into a promoted thesis. Any clamped prior must be echoed in
`generated_from.operator_priors_applied` with a `clamped_from` value.

---

## 2. Pattern Discovery Method

### 2.1 v1: hand-coded `ThesisRule` set

In v1 the discovery method is **hand-coded rules** seeded by operator priors.
Each `ThesisRule` is a deterministic function from a `ProtocolReport` to one
of three states:

```text
matches    — wallet exhibits the pattern AND surface fidelity is sufficient
inconclusive — wallet might exhibit the pattern but coverage is too thin to tell
rejected   — wallet does not exhibit the pattern (or anti-pattern fires)
```

The auditor evaluates every rule against every input report, then aggregates
per-rule supporters / inconclusives / rejects.

v1 deliberately does not auto-discover patterns. Auto-discovery is deferred to
a later phase (§5.5) — premature clustering risks pattern-matching noise as
signal.

### 2.2 Rule shape

```python
@dataclass
class ThesisRule:
    rule_id: str                            # stable id, e.g., "front_loaded_held_winner_v1"
    title: str                              # human-readable thesis title
    description: str                        # plain-English thesis statement
    pattern_predicate: Callable[..., bool]  # report → True if shape matches
    anti_pattern_predicate: Callable[..., bool]  # report → True if shape disqualifies
    coverage_predicate: Callable[..., bool] # report → True if surfaces are usable
    surfaces_consumed: list[str]            # which ProtocolReport fields it reads
    operator_priors_used: list[str]         # which prior keys it pulls
    forbidden_archetypes: list[str]         # archetypes the rule must not fire on
    minimum_n_per_wallet: int               # min positions for the pattern claim
    needs_data: list[str]                   # data the rule needs but lacks
```

`needs_data` is a non-empty list when the rule cannot be fully evaluated with
the surfaces currently on `ProtocolReport`. v1 may include rules that are
marked `needs_data=["future_repricing_signal"]` — these rules do NOT
contribute matches but are catalogued so the audit surfaces the data gap (per
the framing principle).

### 2.3 Evaluation order per wallet per rule

```text
1. coverage_predicate(report)          → if False, mark inconclusive (coverage)
2. anti_pattern_predicate(report)      → if True, mark rejected (anti-pattern)
3. forbidden_archetype check           → if archetype is forbidden, mark rejected
4. minimum_n_per_wallet check          → if below floor, mark inconclusive (n)
5. needs_data check                    → if non-empty, mark inconclusive (data)
6. pattern_predicate(report)           → if True, mark match; else rejected
```

Rejected ≠ disqualified-from-other-rules. A wallet may match rule A and be
rejected by rule B; that's normal.

Aggregation counters use this denominator contract:

- `n_supporting`: rule result `matches`.
- `n_inconclusive`: rule result `inconclusive`, including `coverage`, `n`, and
  `data` causes.
- `n_rejected`: rule result `rejected`, including anti-pattern and forbidden-
  archetype causes.
- `n_evaluable`: `n_supporting + n_rejected`.
- `support_share`: `n_supporting / n_evaluable`; `None` when `n_evaluable == 0`.

Inconclusive wallets do NOT enter `n_evaluable`, because they could not be
honestly classified as supporting or rejecting the rule. They still appear in
the output with `inconclusive_reason` so coverage/data gaps remain visible.

### 2.4 Evaluation determinism

- Rules must be deterministic. Same input report → same output, every run.
- Rules must not mutate the input report.
- Rules must not depend on global state outside `operator_priors`.
- Rules must not call into other rules (no cascades inside a single audit
  pass).

---

## 3. Output: Thesis Catalog Shape

### 3.1 Top-level

```python
strategy_pattern_audit = {
    "audit_run_id": str,                         # stable id for traceability
    "surface_version": "strategy_pattern_audit_v1",
    "generated_from": {
        "n_reports": int,
        "n_reports_excluded_by_priors": int,
        "operator_priors_applied": dict,
        "report_protocol_versions": {str: int},  # version string → report count
    },
    "population_quality": {
        "share_with_partial_or_better": float | None,
        "share_with_activity_cap_hit": float | None,
        "share_with_subgraph_fallback_used": float | None,
        "median_n_closed_positions": int | None,
        "surface_quality_distribution": {...},
    },
    "theses": [...],                             # surviving theses
    "rejected_theses": [...],                    # rules that failed honesty bar
    "data_gaps": [...],                          # consolidated needs_data list
    "top_level_caveats": [...],
}
```

### 3.2 Per-thesis entry

```python
{
    "rule_id": str,
    "title": str,
    "description": str,                          # plain-English thesis statement
    "n_supporting": int,                         # wallets that match
    "n_inconclusive": int,                       # wallets that cannot be evaluated
    "n_rejected": int,                           # wallets that explicitly do not match
    "support_share": float,                      # n_supporting / n_evaluable
    "supporting_wallets": [
        {
            "wallet": str,
            "evidence_refs": [...],
            "coverage_caveats": [...],
        },
        ...
    ],
    "anti_pattern_examples": [
        {
            "wallet": str,
            "reason": str,
            "evidence_refs": [...],
        },
        ...
    ],
    "minimum_n_per_wallet": int,                 # the floor used
    "operator_priors_applied": dict,
    "honesty_bar": {                             # see §4
        "n_min_supporting_required": int,
        "min_observation_quality_share_required": float,
        "min_population_evaluated": int,
        "passed": bool,
        "fail_reasons": [...],                   # populated only if passed=False
    },
    "data_needs": [
        {
            "field_name": str,                   # e.g., "future_repricing_signal"
            "blocking": bool,                    # if True, thesis cannot be tested at all
            "rationale": str,                    # why we need it
            "candidate_sources": [...],          # external systems that could provide it
        },
        ...
    ],
    "fields_provenance": {
        "supporting_wallets": "factual" | "partial",
        "anti_pattern_examples": "factual" | "partial",
        "support_share": "factual" | "inferred",
    },
    "caveats": [...],
}
```

`evidence_refs` is a list of typed references. Rules may include only fields
they consumed.

```python
{
    "surface": str,        # e.g. "timing_surface"
    "path": str,           # dotted path, e.g. "timing_surface.classification"
    "value": Any,          # JSON-serializable observed value or compact summary
    "predicate": str,      # stable predicate id, e.g. "eq", "gte", "share_gte"
    "threshold": Any | None,
    "provenance": "factual" | "inferred" | "unavailable",
}
```

Example:

```python
{
    "surface": "position_building_surface",
    "path": "wallet_summary.share_small_buy_accumulation",
    "value": 0.42,
    "predicate": "gte",
    "threshold": 0.30,
    "provenance": "factual",
}
```

### 3.3 Per-rejected-thesis entry

A rule is "rejected" when it fails the honesty bar (§4) — distinct from a
wallet being rejected by the rule.

```python
{
    "rule_id": str,
    "title": str,
    "fail_reasons": [...],                       # which honesty-bar floors failed
    "n_supporting": int,
    "n_evaluable": int,
    "honesty_bar": {...},
    "data_needs": [...],
    "would_have_been_promoted_at": {
        "n_supporting": int,                     # supporters needed
        "data_fields": [...],                    # data fields needed
    },
    "caveats": [...],
}
```

Rejected theses stay in the output (with full rationale) so the operator can
see what almost qualified — important for curation, and for surfacing data
gaps.

### 3.4 Population-quality block

```python
{
    "share_with_partial_or_better": float | None,    # over all input reports
    "share_with_activity_cap_hit": float | None,
    "share_with_subgraph_fallback_used": float | None,
    "share_with_walk_full_history": float | None,
    "median_n_closed_positions": int | None,
    "median_n_open_positions": int | None,
    "archetype_layer1_distribution": {...},
    "data_quality_flag_distribution": {...},
    "n_excluded_by_forbidden_archetypes": int,
    "n_excluded_by_zero_n_closed_positions": int,
    "surface_quality_distribution": {
        "data_completeness": {...},
        "timing_surface": {...},
        "position_building_surface": {...},
        "exit_attribution": {...},
        "edge_surface": {...},
    },
}
```

This block answers: "is the input population strong enough to draw any
conclusions?" If `share_with_partial_or_better < 0.50`, every thesis carries
a population-quality caveat.

### 3.5 `data_gaps`

Top-level consolidation of all `data_needs` entries across theses.
Sorted by:

1. number of theses blocked by the gap,
2. whether the gap blocks any thesis fully (vs partially),
3. lexicographic on `field_name`.

This is the primary surface for "what would we need to build / acquire to
test more ideas" — directly serving the framing principle.

```python
{
    "field_name": str,
    "blocking_n_theses": int,
    "partial_n_theses": int,
    "blocked_thesis_ids": [...],
    "partial_thesis_ids": [...],
    "candidate_sources": [...],
    "operator_priority_hint": str | None,
}
```

---

## 4. Honesty Bar — Rejection Criteria

A thesis is **promoted** to the catalog only when ALL of these hold:

1. **Population floor:** `n_evaluable >= operator_priors["min_population_evaluated"]` (default 30).
   "Evaluable" follows §2.3: `n_supporting + n_rejected`. Inconclusive
   wallets from coverage, sample-size, or data gaps are excluded.
2. **Support floor:** `n_supporting >= operator_priors["min_supporting"]` (default 10).
3. **Quality floor:** of the supporting wallets, at least
   `operator_priors["min_observation_quality_share"]` (default 0.50) must have
   `partial` or better observation quality on every surface the rule reads.
4. **Anti-pattern symmetry:** `n_anti_pattern_examples >= 1` for any thesis
   with `n_supporting >= 5`. If we can't find a single anti-pattern wallet,
   the rule is suspect (it's likely tautologically defined).
5. **No fully-blocking data gap:** if any `data_need.blocking == True`, the
   thesis is downgraded to `rejected_theses` with reason
   `"blocking_data_gap"` — even if support exists. The thesis cannot be
   tested without that data; promoting it would imply false confidence.
6. **Archetype independence:** the supporting set must include at least
   `operator_priors["min_archetype_diversity"]` distinct `archetype_layer1`
   labels (default 2). If all supporters are one archetype, the thesis is
   describing an archetype, not a strategy pattern — flag and downgrade.
7. **Population coverage caveats:** if `share_with_partial_or_better < 0.30`
   for the wallets the rule could read, the thesis is downgraded with
   reason `"population_quality_too_low"` regardless of support count.

A thesis that fails ANY floor goes to `rejected_theses`. Rejected theses
include `would_have_been_promoted_at` so the operator can see the gap.

The honesty bar is intentionally aggressive. Better to reject 80% of rules
and promote 5 with high confidence than to promote 30 weak theses that
pollute curation.

Surface-quality sources are fixed:

- `data_completeness`: use `ProtocolReport.data_completeness`, especially
  coverage ratios and caveats.
- `timing_surface`: use `timing_surface.confidence_band` and
  `timing_surface.coverage_caveats`.
- `position_building_surface`: use `wallet_summary`,
  `source_quality_distribution`, `identity_quality_distribution`, and
  `observation_window_quality_distribution`.
- `exit_attribution`: use `exit_attribution_summary.coverage`, including
  `n_with_resolved_attribution` when present.
- `edge_surface`: use per-grain sample counts and evidence counts from
  `outcome_side_breakdown` / edge summary fields consumed by the rule.

If a rule reads a surface without a corresponding quality source, the wallet's
result for that rule is `inconclusive (coverage)`, not support.

### 4.1 What promotion does NOT mean

- It does NOT mean the thesis predicts forward outcomes.
- It does NOT mean copy-trading the supporting wallets is profitable.
- It does NOT mean the pattern is causal.
- It does NOT mean acting on the pattern produces P&L.

Promotion means: this pattern is **observable** in the population at a
non-trivial rate, the **anti-pattern is visible**, and the data needed to
test it forward is **either present or named explicitly**.

---

## 5. Scope and Phasing

### 5.1 Design phase (this PR)

Deliverable:

```text
docs/superpowers/specs/2026-05-10-strategy-pattern-audit-design.md
```

Branch: `strategy/pattern-audit-design`

Commit message:

```text
spec(strategy): design strategy pattern audit surface
```

Design-only verification:

```bash
git diff --check
git status --short
```

Design-only constraints:

- do not create any module or file under `sniper/analysis/strategy/` or
  `sniper/strategy/`,
- do not create any test file,
- do not add any field to `ProtocolReport`,
- do not write seed `ThesisRule` definitions in code,
- do not run analyzer batches,
- do not mutate DB,
- do not restart services,
- do not push to `main`.

### 5.2 Future Implementation Phase 1: Core only

Files (proposed):

```text
sniper/analysis/strategy_pattern_audit/__init__.py
sniper/analysis/strategy_pattern_audit/core.py
sniper/analysis/strategy_pattern_audit/rules.py        # ThesisRule dataclass + helpers
sniper/analysis/strategy_pattern_audit/seed_rules.py   # initial hand-coded rules
tests/test_strategy_pattern_audit_core.py
tests/test_strategy_pattern_audit_seed_rules.py
```

Scope:

- pure core (`compute_strategy_pattern_audit(...)`),
- `ThesisRule` shape + helpers,
- 5–10 seed rules from operator priors (e.g., front-loaded held winner,
  small-buy gradual accumulator with negative held travel,
  high-flip held losers, etc.) — each rule must include a fully-defined
  `needs_data` list,
- output-shape lock tests,
- honesty-bar tests,
- forbidden-output-key tests that recursively walk every key and string value
  in the returned audit dict and fail if any forbidden key or forbidden phrase
  from §7 appears outside §7's own test fixtures,
- no-HTTP regression test,
- fixture-replay smoke over a small curated population.

Initial seed-rule examples must be tied to current `ProtocolReport` fields:

1. `category_specialist_v1`: consumes `outcome_side_breakdown`,
   `extractor_features_summary`, `data_completeness`; tests whether realized
   edge concentrates in a tag/category cluster with acceptable coverage.
2. `held_prediction_edge_v1`: consumes `exit_attribution`,
   `held_analyst_branch`, `timing_surface`, `data_completeness`; tests whether
   profit is mostly held-to-resolution with non-underpowered timing evidence.
3. `pre_resolution_exit_discipline_v1`: consumes `exit_attribution`,
   `execution_quality_summary`, `timing_surface`; tests whether profitable
   pre-resolution exits repeat without relying on final-resolution wins.
4. `gradual_accumulation_pattern_v1`: consumes
   `position_building_surface`, `data_completeness`; tests whether positions
   repeatedly reach peak size through gradual multi-buy accumulation.
5. `front_loaded_conviction_v1`: consumes `position_building_surface`,
   `outcome_side_breakdown`; tests whether early size concentration is common
   among profitable positions.
6. `liquidity_opportunist_needs_data_v1`: consumes available edge/position
   surfaces but sets `needs_data=["order_book_depth", "slippage_model"]`
   because current reports cannot validate fillability.
7. `timing_capture_edge_v1`: consumes `timing_surface`,
   `execution_quality_summary`, `data_completeness`; tests whether captured
   travel is repeatably trading-driven or mixed with sufficient evidence.
8. `noise_or_luck_rejection_v1`: consumes all available summary counts and
   quality caveats; rejects wallets whose PnL is concentrated in too few
   markets or whose evidence is underpowered.

Out of scope for Phase 1:

- ProtocolReport wiring (Phase 2),
- analyzer integration (Phase 3),
- catalog rendering (Phase 4),
- auto-discovery / clustering (Phase 5+).

### 5.3 Future Implementation Phase 2: ProtocolReport wiring (optional)

The audit is a population-level surface; it does NOT belong on a single
`ProtocolReport`. Phase 2's wiring is OPTIONAL and only adds:

```python
# A new top-level analysis output type, NOT a per-wallet field:
@dataclass
class StrategyPatternAuditReport:
    audit_run_id: str
    generated_at: int
    surface_version: str
    generated_from: dict
    population_quality: dict
    theses: list[dict]
    rejected_theses: list[dict]
    data_gaps: list[dict]
    top_level_caveats: list[str]
```

`ProtocolReport` remains untouched.

### 5.4 Future Implementation Phase 3: Catalog curation tools

Tools for the operator to:

- mark theses as `accepted` / `parked` / `rejected`,
- attach operator notes,
- track which theses are awaiting data acquisition,
- diff catalog state across audit runs.

These are operator UI / CLI tools. They do NOT change the core
auditor.

### 5.5 Future Implementation Phase 4+: Auto-discovery (deferred)

Clustering, association mining, sequence pattern detection. Deferred until
v1 hand-coded catalog is mature and operator-validated. Premature auto-
discovery on a thin population reproduces noise as signal.

---

## 6. Default Operator Priors

```python
{
    "min_population_evaluated": 30,
    "min_supporting": 10,
    "min_observation_quality_share": 0.50,
    "min_archetype_diversity": 2,
    "forbidden_archetypes_for_directional_theses": ["arb_fund", "market_maker"],
    "exclude_wallets": [],                   # bot allowlist hook
    "exclude_known_phantom_wallets": True,
    "min_n_closed_positions_per_wallet": 5,
    "max_share_with_activity_cap_hit": 0.40, # population-quality gate
    "min_share_with_partial_or_better": 0.30,
}
```

These are conservative defaults. The operator may relax via
`operator_priors` argument; relaxations are echoed in the audit's
`generated_from.operator_priors_applied` block so reviewers see what gates
were loosened.

`exclude_known_phantom_wallets` means: exclude wallets from the audit input
when an upstream curation layer marks them as synthetic, phantom, duplicated,
or otherwise not a real independent wallet. If no such upstream flag exists,
the setting has no effect and must be reported in
`generated_from.operator_priors_applied` as `not_applicable`.

---

## 7. Non-Goals

The Strategy Pattern Audit must NOT emit or implement:

```text
recommended_trade
recommended_position_size
copy_signal
copy_trigger
expected_pnl_for_thesis
simulated_thesis_result
forward_prediction
buy_signal
sell_signal
```

The Strategy Pattern Audit must NOT perform:

```text
price-history calls
live API calls
analyzer batches
DB writes from the core
service restarts
verdict / gate / primary_edge_type / copyability_note mutation
predictive claims about forward outcomes
"if you copy these wallets you'll make X%" framing
auto-discovery in v1 (Phase 5+ only)
```

Allowed wording in catalog descriptions:

```text
"Pattern observed in N wallets across M archetypes"
"Anti-pattern observed in K wallets"
"Cannot be evaluated without <field_name> on ProtocolReport"
"Population coverage caveat: <reason>"
"Thesis would promote at n_supporting=12 (currently 7)"
```

Forbidden wording:

```text
"Copy these wallets when …"
"Expected return X%"
"Trigger on first BUY"
"Buy when pattern matches"
"This pattern predicts forward returns"
"Past performance suggests …" (without naming the inferential gap)
"If you held these positions you would have made …" (without naming
the simulator-class assumptions)
```

---

## 8. Lineage Discipline

This surface is the next-tier consumer of W1/W2/W3/Exit Attribution/
Completeness. It MUST follow the lineage's discipline pattern:

- Pure compute, no I/O, no simulator.
- Per-output-field provenance (factual / inferred / unavailable).
- Explicit caveat list at every level (per-thesis, per-supporting-wallet,
  population-level).
- "Flag rather than guess" when a rule cannot be fully evaluated.
- Forbidden output keys and forbidden phrasing enforced by tests.
- No imports from the ledger module inside the core (the ledger is an
  optional adapter for future data needs, not a runtime dependency).
- No imports from `analyze_wallet`, `report`, or any verdict / gate
  module — input is a sequence of `ProtocolReport`s only.
- No mutation of input reports.

---

## 9. Handoff Checklist

- §1 boundary accepted: pure compute, no I/O, no simulator, no verdict
  mutation.
- §2 rule shape and evaluation order accepted.
- §3 output shape accepted.
- §4 honesty bar accepted: 7 floors enforced, rejected theses retained
  with rationale.
- §5 phasing accepted: spec-only Phase 0; core-only Phase 1; population
  surface (not per-wallet).
- §6 operator priors accepted as conservative defaults.
- §7 non-goals accepted: no triggers, no copy fields, no forward
  predictions, no auto-discovery in v1.
- §8 lineage discipline accepted: matches W1/W2/W3 pattern.
- The audit's role is to extract reusable theses, reject weak/noisy
  patterns, and name data gaps — explicitly, with population support and
  population-level honesty bar.
- The audit does NOT recommend trades, simulate copy strategies, or
  emit forward-prediction claims.
