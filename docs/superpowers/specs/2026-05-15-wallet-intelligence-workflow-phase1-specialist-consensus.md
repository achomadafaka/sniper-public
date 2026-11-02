# Wallet Intelligence Workflow Phase 1 - Specialist Consensus

**Date:** 2026-05-15
**Status:** design-only; no implementation, no schema changes, no live calls
**Scope:** Phase 1 contract for using promising wallet analyzer results through a Specialist Consensus Surface and manual thesis-card workflow

## 0. Doctrine

The wallet analyzer is not a copy bot. It is a strategy-intelligence system.

Durable workflow:

```text
wallet intelligence
-> strategy extraction
-> thesis formation
-> thesis-specific dataset
-> thesis-specific simulation
-> paper/live validation
```

Specialist wallet behavior is evidence, not proof. Specialist activity may support, challenge, or fail to inform a thesis. It cannot validate a thesis. Confirmation only comes later from thesis-specific simulation and paper/live validation with pre-registered kill criteria.

This spec defines Phase 1 only: manual thesis cards plus the Specialist Consensus Surface contract. It does not design the simulator, live feeds, alerts, paper/live execution, sizing, or any trade recommendation surface.

## 1. Three Layers

Layer 1 - manual work now:

- Pick promising wallets from current analyzer results.
- Turn each into a thesis card.
- Classify category, mechanism, evidence strength, caveats, potential lesson, missing data, and simulation-worthiness.
- Use the cards to build a draft specialist registry.

Layer 2 - workflow/spec to build next:

- Define the Wallet Intelligence Workflow contract.
- Define the Specialist Consensus Surface primitive.
- Define specialist inclusion gates and independence-pruning requirements.
- Define Specialist Thesis Review as a consumer, not the primitive.
- Define output taxonomy and downgrade rules.

Layer 3 - simulations later:

- Only simulate after an observation becomes a candidate thesis with a data inventory and pre-registered kill criteria.
- Simulations are thesis-specific, not generic wallet copying.
- Paper/live validation stays downstream and operator-gated.

## 2. Manual Workflow Now

For each promising wallet, create a manual thesis card:

```text
Wallet:
Source report / fixture / analyzer commit:
Category:
Observed behavior:
Mechanism:
Evidence strength:
Caveats:
Potential lesson:
Missing data:
Simulation-worthy: yes / no / maybe
Why:
Next research action:
```

Manual card rules:

- Start with caveats before conclusions.
- Describe the wallet's historical behavior, not what to trade.
- Treat `edge_detected` as a prompt for research, not a recommendation.
- Separate category-local evidence from wallet-wide results.
- Separate prediction skill, exit discipline, timing, accumulation, sizing, and coverage caveats.
- Mark stale, underpowered, incomplete, or concentrated results as downgrade triggers.

The immediate output is a small, auditable set of thesis cards. The cards are allowed to say "this might teach us a mechanism worth testing." They are not allowed to say "follow this wallet."

## 3. Phase 1 Workflow

```text
Wallet Protocol Output
-> Per-Wallet Card / Cross-Wallet Mine / Noise Filter
-> Specialist Consensus Surface
-> Specialist Thesis Review / Anomaly Detector / Strategy Thesis Catalog
-> Thesis Review Card
-> Strategy Extraction
-> Simulation Backlog
-> thesis-specific simulation
-> paper/live validation
```

Phase 1 builds only the first shared primitive and manual review shape:

- Specialist Consensus Surface: category-local specialist lookup and evidence state.
- Manual Thesis Review Card: operator-authored thesis review using specialist evidence.
- Draft Specialist Registry: provenance-aware list of candidate specialists.

## 4. Specialist Consensus Surface

Primitive:

Given category `C`, return which wallets have category-local edge, how strong, how recent, how complete, how independent, and whether their evidence is direct, adjacent, inactive, or not informative for the thesis under review.

Inputs:

- `ProtocolReport` fields for verdict, gate, branches, tag grains, outcome-side behavior, timing, exit attribution, position building, data completeness, and limitations.
- Fixture/report provenance: source report path, fixture path, analyzer commit, capture timestamp.
- Category mapping from nav category and leaf tags.
- Independence evidence once available from the Independence Pruner.

Evidence states:

| State | Meaning | Allowed use | Forbidden interpretation |
|---|---|---|---|
| `direct` | Specialist traded the exact market/condition or thesis object | Evidence in a thesis review card | Proof the thesis is right |
| `adjacent` | Specialist traded same event, series, category, or related tag | Research context and weak support/challenge | Direct market evidence |
| `inactive` | Qualified specialists exist but did not trade the market | Absence of specialist coverage | Endorsement or rejection |
| `not_informative` | Category match, recency, completeness, independence, or behavior is too weak | Downgrade or reject | Neutral-positive evidence |

Surface output must include:

- category-local specialist list,
- evidence state per wallet,
- direct/adjacent/inactive counts,
- independence-cluster counts, not raw wallet vote counts,
- caveats before alignment summary,
- provenance for every included report.

## 5. specialist_evidence Taxonomy

Use this fixed taxonomy for analyzer insights and thesis-review outputs.

| Label | Required evidence | Allowed use | Forbidden interpretation | Next action |
|---|---|---|---|---|
| `ignore` | Gate fail, red completeness, stale report, or unusable sample | Exclude from workflow | Hidden weak support | None until recaptured or fixed |
| `monitor` | Some useful behavior, but limitations dominate | Keep on watchlist | Specialist inclusion | Revisit after more data |
| `research_only` | Repeatable observation with no testable thesis yet | Manual research | Simulation-ready | Write or reject thesis card |
| `supports_existing_thesis` | Direct or adjacent specialist evidence aligns after caveats | Thesis review input | Trade approval | Record caveats and missing data |
| `contradicts_existing_thesis` | Specialist evidence opposes or behavior undermines thesis | Thesis review input | Automatic rejection | Review thesis assumptions |
| `specialist_evidence` | Wallet passes category-local specialist gate | Registry entry and evidence card | Trade signal | Add to consensus surface |
| `candidate_strategy_thesis` | Observation reframed as a testable rule with data needs | Strategy Thesis Catalog | Expected profitability | Build data inventory |
| `simulation_candidate` | Candidate thesis plus complete data inventory and kill criteria | Simulation Backlog | Deployment instruction | Design thesis-specific simulation |
| `paper_test_candidate` | Simulation passes pre-registered criteria after costs/slippage assumptions | Paper-test planning | Live permission | Operator-gated paper plan |

Do not use any specialist label that implies a trade trigger.

## 6. Specialist Registry Shape

Draft registry entry:

```text
specialist_registry_entry {
  wallet                         str
  category                       str
  edge_evidence_strength         enum   # weak / moderate / strong
  effect_size_in_category        number
  sample_size_in_category        int
  confidence_interval            text | null
  uncertainty                    text | null
  recency_days                   int
  completeness_score             enum   # green / yellow / red
  side_bias_in_category          enum   # yes_leaning / no_leaning / mixed / agnostic
  mode_in_category               enum   # held / flipped / mixed
  norm_position_size_in_category number | null
  source_report                  str
  source_fixture                 str
  analyzer_commit                str
  caveats                        [str]
  status                         enum   # active / stale
}
```

Registry constraints:

- Registry entries are evidence records, not watch instructions.
- Provenance is required. A specialist without report/fixture/commit provenance is not eligible.
- Category-local evidence must be stored separately from wallet-wide performance.
- `active` means recent enough for review; it does not mean actionable.
- `stale` entries remain useful for historical research but cannot support current thesis review without downgrade.

## 7. Specialist Gate

Hard inclusion gates:

| Gate | Requirement | Failure result |
|---|---|---|
| Category-local evidence | Edge must exist in category `C`, not only wallet-wide | Reject for category `C` |
| Enough sample | Closed-position count in `C` meets the configured floor | Reject or downgrade to `research_only` |
| Category-local positive result | Result in `C` is positive after known caveats | Reject |
| Recent enough | Last relevant activity within recency window | Mark `stale` or reject from current review |
| Completeness acceptable | Coverage is not red; attribution is adequate | Reject or downgrade |
| No disqualifying limitations | Limitations do not invalidate the claim | Reject |
| No stale fixture | Report predates no semantic fixes required by the current workflow | Recapture before use |
| No out-of-category promotion | Specialist in one category cannot be promoted to another | Reject for mismatched category |

Gate outputs:

- `include_as_specialist_evidence`,
- `monitor_only`,
- `research_only`,
- `reject`.

## 8. Independence Pruner Requirements

Specialist Thesis Review must not treat wallets as independent by default. Correlated wallets collapse into one evidence cluster.

Correlation inputs to define before implementation:

- funding overlap,
- timing correlation,
- market overlap,
- archetype/fingerprint similarity,
- proxy/on-chain relation,
- one wallet following another.

Required output:

```text
independence_cluster {
  cluster_id:
  wallets:
  representative_wallet:
  correlation_reasons:
  effective_vote_count:
  caveats:
}
```

Requirements:

- If wallets share funding or proxy identity, collapse unless evidence proves independence.
- If wallets trade the same markets within a narrow timing window repeatedly, collapse or downgrade.
- If wallets have high market overlap and the same archetype, collapse or downgrade.
- If a wallet appears to follow another wallet, the follower cannot add independent support.
- The strongest category-local wallet in a cluster represents the cluster; the rest remain provenance/caveat context.

Design test cases:

- Two wallets funded from the same source and trading the same conditions collapse to one cluster.
- Three wallets with aligned side but synchronized timing count as one effective cluster.
- Two specialists in the same category but with different markets, funding, and timing remain independent.
- A stale specialist and an active specialist do not become one current support cluster.
- A wallet with out-of-category edge cannot strengthen a category cluster.

No implementation in Phase 1. These are requirements for the next engineering slice.

## 9. Specialist Thesis Review

Specialist Thesis Review is a consumer of Specialist Consensus Surface plus Independence Pruner. It is not the first primitive.

Input:

```text
thesis_review_request {
  thesis:
  market_or_subject:
  category:
  tags:
  operator_rationale:
  source_time:
}
```

Output card order:

```text
Category match:
Direct evidence:
Adjacent evidence:
Inactivity:
Independence pruning:
Caveats:
Alignment summary:
Next research action:
NOT A BUY OR SELL SIGNAL:
```

Allowed conclusions:

- evidence supports,
- evidence challenges,
- evidence is mixed,
- evidence does not inform.

Forbidden conclusions:

- recommendation to enter, exit, or size,
- expected PnL,
- deployment instruction,
- validation claim,
- scalar probability or confidence score.

## 10. Strategy Extraction Path

Path:

```text
observation
-> candidate thesis
-> data inventory
-> simulation candidate
-> paper-test candidate
```

Example:

Observation:

```text
A category specialist gradually accumulates in thin markets before favorable movement.
```

Candidate thesis:

```text
Gradual accumulation by category specialists in category C may identify markets worth monitoring.
```

Data inventory:

- wallet fills,
- market tags,
- price history,
- liquidity and depth,
- resolution outcomes,
- independence checks,
- fixture/report provenance,
- delay and slippage assumptions for later simulation.

Simulation candidate:

- rule is thesis-specific,
- input data exists or missing fields are explicit,
- outcome definition is fixed before simulation,
- kill criteria are pre-registered.

Paper-test candidate:

- simulation survives kill criteria,
- paper plan has separate operator approval,
- live validation remains out of scope.

## 11. Simulation-Candidate Criteria

An insight is strong enough to enter the Simulation Backlog only when:

- it is a candidate strategy thesis, not a loose observation,
- the mechanism is named,
- category-local evidence survives specialist gates,
- correlated wallets are collapsed into clusters,
- data inventory is complete or gaps are explicit and bounded,
- the simulation can be run with realistic delay and slippage assumptions,
- pre-registered kill criteria exist before the run.

Do not simulate when:

- sample size is too small,
- completeness or attribution is low,
- PnL is concentrated in one or two markets,
- behavior is ambiguous,
- the effect depends on copying a named wallet,
- the category match is weak,
- the report is stale or predates semantic fixes,
- the thesis cannot be stated as a rule independent of the wallet identity.

Pre-registered kill criteria should include:

- minimum sample size,
- minimum post-cost effect threshold,
- maximum concentration allowed,
- maximum degradation under delay/slippage,
- out-of-sample or time-split requirement,
- failure rule for independence collapse,
- failure rule for incomplete data.

## 12. Manual First Five Actions

1. Pick the top 3 promising wallets from current analyzer results.
2. Create manual thesis cards for those wallets.
3. Build a draft specialist registry from those cards.
4. Identify one or two candidate category-level patterns.
5. For each pattern, decide whether it is `research_only`, `candidate_strategy_thesis`, or `simulation_candidate`.

Separate work streams:

- Manual research workflow: thesis cards and caveat review.
- Design/spec work: registry, consensus surface, gate, independence-pruner contract.
- Engineering work: only after Phase 1 spec approval.
- Simulation work: only after a thesis is formed and data inventory is complete.

## 13. Anti-Overclaiming Rules

Downgrade or reject when:

- completeness is low,
- attribution coverage is low,
- timing is underpowered,
- position-building is ambiguous,
- PnL is concentrated in one or two markets,
- specialists are correlated,
- category match is weak,
- edge is stale,
- report predates semantic fixes,
- wallet behavior does not match its claimed specialty,
- direct evidence is absent and only adjacent evidence exists,
- inactivity is being read as endorsement,
- a scalar confidence/probability number would hide caveats.

Hard rule: no surface alone creates a trade recommendation.

## 14. Non-Goals

Do not design or build:

- copy pipeline,
- live specialist feed,
- auto alerts,
- confidence score,
- generic wallet simulator,
- trade recommender,
- sizing model,
- paper/live integration,
- UI before the evidence model exists,
- legacy low-buy or insider-score workflow.

Phase 1 ends at the evidence model and manual thesis-card workflow.

## 15. Recommended Names

Spec/module family:

- `Wallet Intelligence Workflow`
- `Specialist Consensus Surface`
- `Specialist Thesis Review`
- `Thesis Evidence Review`
- `Strategy Thesis Catalog`
- `Simulation Backlog`

Avoid names containing `validator`, `copy`, `signal`, `trade recommender`, or `live feed`.

## 16. Acceptance Criteria

- The workflow starts with wallet intelligence and ends before simulator implementation.
- The first primitive is Specialist Consensus Surface.
- Specialist Thesis Review is a consumer.
- Taxonomy uses `specialist_evidence`.
- Caveats render before alignment summary.
- Independence pruning is required before serious thesis review.
- Correlated wallets collapse into evidence clusters.
- Inactivity means absence of specialist coverage, not endorsement.
- No expected PnL, entry, exit, sizing, live signal, or copy instruction appears.
- Manual first actions are clear enough to execute without code.
