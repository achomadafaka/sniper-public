# Wallet Intelligence Workflow — Design (v2)

**Date:** 2026-05-13
**Status:** design-only; no implementation, no schema changes, no live calls
**Scope:** what we do with wallet-analyzer insights once the analyzer is trusted
**Top-level layer name:** Wallet Intelligence Workflow (WIW)
**Author note:** strategy-intelligence framing per `CLAUDE.md` + `AGENTS.md` doctrine — wallet analyzer is NOT a copy bot
**v2 patch note:** adopted "Specialist Thesis Review" naming; introduced Wallet Intelligence Workflow as the orchestrating layer; added Simulation Backlog Generator to the consumer chain; reordered Independence Pruner before Specialist Thesis Review; tightened the Thesis Review Card example to render caveats above alignment summary and removed trade-guidance language from "Next research action."

### Durable architecture chain

```
wallet analyzer surfaces
  → Wallet Intelligence Workflow            (orchestrating evidence layer)
    → Specialist Consensus Surface          (primitive: who has category-local edge)
    → Specialist Thesis Review              (evidence review of one thesis)
    → Strategy Thesis Catalog
       / Strategy Pattern Audit expansion   (cross-wallet pattern → candidate thesis)
    → Simulation Backlog Generator          (emits sim specs only; not a simulator)
    → thesis-specific simulation            (downstream, not designed here)
    → paper / live validation               (downstream, not designed here, operator-gated)
```

WIW answers one question: **"What can we responsibly do with this evidence?"** It does not validate trades, generate trading signals, or recommend entries/exits/sizing.

---

## 0. Doctrinal framing (read first)

Wallet behavior is **evidence**, not a trigger. The analyzer's output is a set of conditional claims about *what a wallet has historically done well at, in which category, with what confidence*. None of that is a buy/sell instruction.

Specialist activity can **support or challenge** a thesis. It cannot validate one. Confirmation comes from a *separate* layer: simulation against captured fixtures plus paper validation with pre-registered kill criteria. This doc designs the surface that connects analyzer insights to that downstream layer; it does **not** design the simulator or the paper trader.

Forbidden framings (gate-equivalent to the calibration review rules):
- "X wallets agree therefore the trade is good" → no, that's vote-counting on non-independent samples
- "Copy this wallet's behavior" → no, copy is a deployment mode we do not endorse
- "Specialist consensus = recommendation" → no, consensus is conditional evidence to be weighted against independence and recency
- "Insight → strategy → deploy" → no, the path is insight → thesis → data → simulation → paper → operator-gated activation

---

## 1. Workflow map

```
                              ┌──────────────────────────┐
                              │  WALLET PROTOCOL OUTPUT  │
                              │   (ProtocolReport JSON)  │
                              └────────────┬─────────────┘
                                           │
                                           ▼
              ┌──────────────────────────────────────────────┐
              │ PER-WALLET CARD / CROSS-WALLET MINE /        │
              │ NOISE FILTER                                 │
              └──────────────────────┬───────────────────────┘
                                     │
                                     ▼
              ┌──────────────────────────────────────────────┐
              │ SPECIALIST CONSENSUS SURFACE                 │
              │ category-local edge, recency, completeness,  │
              │ independence, direct/adjacent/inactive state │
              └──────────────────────┬───────────────────────┘
                                     │
                                     ▼
              ┌──────────────────────────────────────────────┐
              │ SPECIALIST THESIS REVIEW / ANOMALY DETECTOR │
              │ / STRATEGY THESIS CATALOG                    │
              └──────────────────────┬───────────────────────┘
                                     │
                                     ▼
              ┌──────────────────────────────────────────────┐
              │ THESIS REVIEW CARD                           │
              │ evidence-graded; NOT a buy/sell signal       │
              └──────────────────────┬───────────────────────┘
                                     │
                                     ▼
              ┌──────────────────────────────────────────────┐
              │ STRATEGY EXTRACTION                          │
              └──────────────────────┬───────────────────────┘
                                     │
                                     ▼
              ┌──────────────────────────────────────────────┐
              │ SIMULATION BACKLOG                           │
              │ sim specs only; no simulator logic           │
              └──────────────────────┬───────────────────────┘
                                     │
                                     ▼
              [ thesis-specific simulation (out of scope here) ]
                                     │
                                     ▼
              [ paper / live validation (operator-gated, out of scope) ]
```

The **Wallet Intelligence Workflow** is the orchestrating layer. The **Specialist Consensus Surface** is the primitive every consumer reads from. Specialist Thesis Review, Anomaly Detector, and Strategy Thesis Catalog consume it rather than reimplementing category-local edge lookup, downgrade rules, and independence caveats.

---

## 2. Insight types the analyzer can produce

The analyzer ProtocolReport already names these. Each maps to a downstream use class.

| # | Insight type | Source field(s) | What it captures |
|---|---|---|---|
| 1 | **Category specialization** | `extractor_features_summary.domain_concentration`, `held_analyst_branch.tag_grains`, `flipper_branch.tag_grains`, `nav_lookup` | Where the wallet concentrates and where edge survives OOS |
| 2 | **Outcome-side bias** | `outcome_side_breakdown.yes / no / other` (WR, PnL, share) | Asymmetric edge on YES vs NO vs sports-line outcomes |
| 3 | **Holding-vs-flipping edge** | `trading_mode`, `flip_rate`, `held_analyst_branch.is_active`, `flipper_branch.is_active`, hypothesis verdicts | Whether the wallet earns by holding to resolution, by exit timing, or both |
| 4 | **Exit discipline** | `exit_attribution.by_attribution.pre_resolution_profitable_exit / loss_cut`, `timing_surface.classification` | How well the wallet captures gains and cuts losses pre-resolution |
| 5 | **Entry-timing / captured-travel** | hypothesis H4, `timing_surface`, `execution_quality_summary` | Whether entries land before favorable price movement |
| 6 | **Position-building / accumulation** | `position_building_surface.source_quality_distribution`, `identity_quality_distribution`, `row_quality_summary` | Whether the wallet builds positions cleanly vs fragmented/ambiguous |
| 7 | **Sizing / conviction discipline** | `extractor_features_summary.usdc_size_cv`, `top_condition_fill_share` | Variance of bet size, top-concentration ratio |
| 8 | **Sell-ladder / reaccumulation** | `capital_ledger.events`, `flipper_branch.tag_grains`, redemption ratio | Multi-leg exits vs single-shot exits |
| 9 | **Noise / underpowered** | `overall_verdict=no_edge_detected`, `applicable_limitations`, sample-size flags | The wallet does not carry usable signal — explicit reject |
| 10 | **Coverage caveats** | `data_quality_flags`, `data_completeness`, `subgraph_fallback_used`, `n_unmapped_fills` | What surfaces of THIS wallet are incomplete and must be downweighted |

Note: insight #10 is meta — every other insight is gated by it. A wallet with rich category specialization but poor completeness is downgraded across the board.

---

## 3. Possible uses for each insight type

| Insight type | Possible downstream uses |
|---|---|
| 1. Category specialization | (a) specialist registry entry per category; (b) thesis-review consultant for trades in that category; (c) data-collection target if the category lacks tag coverage |
| 2. Outcome-side bias | (a) thesis-review consultant for YES-leaning vs NO-leaning trades; (b) candidate thesis (NO-bias is structurally priced — does this wallet's edge survive?); (c) noise check (very high YES WR with small sample = priors not edge) |
| 3. Holding-vs-flipping edge | (a) workflow router — which side of the analyzer to weight when the wallet's observed behavior is consulted; (b) candidate strategy theses split by mode (hold-to-res vs short-window) |
| 4. Exit discipline | (a) post-trade review benchmark (how would a clean exit have compared); (b) candidate thesis about exit-rule design; (c) NOT a copy signal — discipline measured in their context, not ours |
| 5. Entry-timing / captured-travel | (a) candidate thesis if a class of wallets has consistent captured-travel in a category; (b) anomaly flag (a wallet that usually times well suddenly stops — surfaced by Anomaly Detector consumer, NOT a trade signal); (c) data target (need price history at higher fidelity to confirm) |
| 6. Position-building / accumulation | (a) accumulation thesis (gradual entry in thin markets); (b) honest-read filter — if Position Builder is fragmented/ambiguous, any timing/accumulation claim about the wallet is downgraded; (c) data target (need clean fill attribution) |
| 7. Sizing / conviction | (a) within-wallet conviction indicator — when this wallet bets at top-of-norm size in their specialist category, it's stronger evidence than dust; (b) thesis-review weight modifier |
| 8. Sell-ladder | (a) candidate thesis about multi-leg exit advantage in illiquid markets; (b) exit-rule design input |
| 9. Noise / underpowered | (a) reject from specialist registry; (b) remove from watchlist; (c) NOT used as evidence, period — but the wallet may still be useful as a *negative* example in cross-wallet mining |
| 10. Coverage caveats | (a) downweight on every other insight; (b) data-collection backlog input ("we systematically miss X for Y class of wallet"); (c) recapture trigger |

Concrete principle: a single wallet's insight rarely justifies any of the right-column actions on its own. The Strategy Pattern Audit layer aggregates across wallets to find patterns; per-wallet insights flow into that aggregator, they don't drive solo decisions.

---

## 4. Specialist-wallet workflow

### 4.1 What makes a wallet a "specialist" in category C

Hard gates (all must hold):

1. `overall_verdict in {edge_detected, suggestive}` and `gate_result == 'pass'`
2. `domain_concentration` exceeds a threshold (judgment call — start at 0.30, audit empirically)
3. The category C appears in `held_analyst_branch.tag_grains` OR `flipper_branch.tag_grains` with a supported verdict, AND the grain coverage in C clears a minimum sample size (e.g. ≥ 30 closed positions in C, audit empirically)
4. Within-grain PnL is positive (not just lifetime PnL — category-local)
5. `last_position_ts` is within recency window (e.g. ≤ 60 days, audit empirically — staleness gate)
6. Coverage clean: `walk_full_history=True`, `n_unmapped_fills` < some fraction of total (e.g. 15%), no critical `data_quality_flags`
7. `BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY` is **OK** for specialist registry purposes *as long as the consulted query is in the same tag grain*. The flag says "wallet-level OOS unvalidated" — perfectly fine when we only consult them on their grain.

Disqualifiers (any one rejects):

- `gate_result == 'fail'` (e.g. market-maker, dust wallet)
- `applicable_limitations` includes a category-incompatibility note
- Capital-ledger evidence of copy behavior (high overlap with another wallet, low independence — see §5)
- Stale fixture (older than recapture window) → must recapture before consulting
- Sample size in C below threshold

### 4.2 Specialist registry shape (logical, not implemented)

For each (wallet, category C) pair that passes the gate:

```
specialist_entry {
  wallet                    str
  category                  str               # nav category or leaf tag, normalized
  edge_evidence_strength    enum              # weak / moderate / strong
  effect_size_in_C          float             # from tag grain
  sample_size_in_C          int               # n closed positions in C
  ci_lower_in_C             float
  ci_upper_in_C             float
  in_sample_only            bool              # True if BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY for this branch
  recency_days              int               # days since last C trade
  completeness_score        enum              # green / yellow / red (from coverage caveats)
  side_bias_in_C            enum              # yes_leaning / no_leaning / agnostic / mixed
  mode_in_C                 enum              # held / flipped / mixed
  norm_position_size_in_C   float             # median USDC notional for this wallet in C
  fixture_path              str               # provenance — which v3 fixture
  fixture_captured_ts       int
  analyzer_commit           str
}
```

The registry is **read-only output** of analyzer runs. It is not curated by hand. If the analyzer updates, the registry rebuilds from scratch.

### 4.3 Using specialists to review a trade thesis

Trade thesis input:
- `condition_id`, `outcome_token_id` (YES or NO), `event_slug`, `nav_category`, `leaf_tags[]`
- Our side and rough conviction (high/medium/low — human input)
- Optional: our entry timing context (we already hold vs considering)

Lookup sequence:

1. **Direct evidence** — query specialist registry for wallets with category C == any of the trade's `nav_category` ∪ `leaf_tags`. For each, check `followed_wallet_activity` and `closed/open_positions` for THIS condition_id.
2. **Adjacent evidence** — same event-slug or same series (same nav category, related tags) but different condition_id. Useful when a market is too new to have specialist participation yet.
3. **Inactivity is data** — specialists exist for this category but none touched the market is itself *informational*, not a non-event. Could mean (a) they don't see edge here, (b) timing is wrong, (c) the market is below their threshold. Per §5.3, this is "absence of signal," NOT endorsement.
4. **Side comparison** — for each specialist who traded the market, capture: side, entry price, current size relative to their norm, holding mode (still open vs closed), and whether their behavior fits their known pattern (e.g. a holder taking a small flipping position is *atypical* — useful flag).
5. **Independence pruning** — see §5.

### 4.4 The output is evidence, not a recommendation

The output of this workflow is a **Thesis Review Card** (see §6). It is rendered prose with caveats, not a green/red light.

---

## 5. Multi-specialist evidence weighting

### 5.1 Independence is not assumed; it is tested

When N specialists are aligned on a thesis, we cannot treat them as N independent votes. The N → effective_N collapse is the whole game.

Independence checks (run on every pair of voting specialists):

| Check | Source | What it detects |
|---|---|---|
| Capital-ledger founding overlap | `capital_ledger.first_event_ts`, deposit flows | Wallets funded together — likely related |
| Timing correlation | activity-row timestamps, % of trades within Y minutes of each other | Copy or shared-signal reactors |
| Market-overlap ratio | shared condition_ids / union(condition_ids) | High overlap suggests one is downstream of the other |
| Archetype similarity | `archetype_layer1`, `archetype_layer2`, `extractor_features_summary` | Behavioral fingerprint similarity |
| On-chain proxy relations | shared `proxyWallet` for different EOAs, related EOAs from Goldsky | Identity overlap |

A pair flagged by ≥ 2 of the above is treated as **correlated**, and only ONE counts toward voting weight (the one with stronger category edge).

### 5.2 Per-specialist weight (design, not formula)

Each specialist's weight in a thesis review combines:

| Factor | Direction | Source |
|---|---|---|
| Category edge strength | + | effect size + CI tightness in grain C |
| Sample size in C | + | n closed positions in C |
| Recency | + (decay function) | days since last C trade |
| Completeness | + | coverage score (green / yellow / red) |
| Position-building cleanliness | + | position_building_surface |
| Position size for THIS trade relative to wallet norm in C | + | current size / norm size in C |
| Behavioral consistency | + | does this trade fit the wallet's known pattern? |
| Independence from other voters | + | correlation pruning result |
| In-sample-only edge | – | downweight `BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY` slightly |

A weighted vote summary is produced, **but never converted to a probability or confidence number**. The output is qualitative bands (strong support, mild support, mixed, mild opposition, strong opposition, no signal) with the underlying counts and caveats shown.

### 5.3 Hard rules against confirmation bias

- If three or more aligned voters are flagged as correlated, the review must say so explicitly and downgrade evidence quality.
- If "specialists are aligned" but their alignment is in a *different sub-grain* than the trade's market, the review must say "alignment exists in an adjacent category, not this one" rather than claiming direct support.
- If no specialists touched the market, the review says **"no specialist coverage — absence of signal, not endorsement."** It does not silently pass.

---

## 6. Specialist Thesis Review workflow

### 6.1 Input

```
trade_thesis {
  condition_id          str
  outcome_token_id      str        # YES or NO outcome token
  side                  enum       # yes / no
  event_slug            str
  nav_category          str
  leaf_tags             [str]
  entry_status          enum       # holding / considering
  operator_conviction   enum       # low / medium / high (human input)
  operator_thesis_note  str        # human-written rationale
}
```

### 6.2 Output

```
THESIS REVIEW CARD
==================
Thesis: <event_slug> — side=YES @ <condition_id_short>
Reviewed at: <utc_ts>; specialist registry as of <utc_ts>; analyzer commit <sha>

CATEGORY MATCH
  nav: <nav_category>     specialists registered: 7
  leaf tags: <list>        specialists registered: 3 (overlap with nav: 1)
  unique specialists considered: 9

DIRECT EVIDENCE (specialists who traded THIS condition_id)
  - <wallet_short>  side=YES  size=$<x> (1.4× their norm in this category)
                    entered <N days ago> at $<p>  status: open
                    fits pattern: yes (holder, geopolitics specialist)
                    completeness: green
                    independence: ok (no flagged correlations)
  - <wallet_short>  side=YES  size=$<y> (0.3× norm — atypically small)
                    closed at $<q> for $<pnl>  fits pattern: no (this wallet usually holds; closed early)
                    flag: behavioral mismatch
  - <wallet_short>  side=NO   size=$<z> (1.0× norm)
                    open, entered <M days ago>
                    completeness: yellow (n_unmapped_fills=18%)

ADJACENT EVIDENCE (specialists who traded same event-slug, different cond)
  - <wallet_short> traded resolved sibling market — YES win
  - <wallet_short> traded resolved sibling market — NO loss

INACTIVITY
  - 4 of 9 specialists with category match did NOT trade this market
    (specialists exist for this category but did not participate)

INDEPENDENCE PRUNING
  - 2 of 3 YES-aligned wallets flagged as correlated (archetype + timing)
    Effective independent supporters: 2 (not 3)

CAVEATS  (render before any alignment summary)
  - one supporting wallet has BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY in this grain
    → category-local evidence; wallet-level OOS not validated
  - one supporting wallet's position is atypically small for their norm in C
  - 18% unmapped fills on the opposing wallet's fixture — downweight applied
  - 4 specialists' inactivity is itself a soft caveat (absence of signal,
    NOT endorsement)

ALIGNMENT SUMMARY  (read only after caveats)
  Direct  : YES 2 (effective)  NO 1  inactive 4  pattern-mismatch 1
  Adjacent: 1 win 1 loss
  Overall : MILD SUPPORT (qualitative band; see caveats above)

NEXT RESEARCH ACTION (operator-facing, not auto-executed)
  Operator may review the thesis with these caveats. This card does not
  recommend entry, exit, or sizing. If the thesis remains interesting,
  collect more evidence or route it to the simulation/data-requirements
  backlog (see §10 layer 6 — Simulation Backlog Generator).

NOT A BUY OR SELL SIGNAL. NOT A COPY INSTRUCTION. NOT A SIZING RECOMMENDATION.
```

Every section above is mechanical — no narrative inference, no probability claims, no implied magnitude. The review tells the operator what was found and why; the operator decides.

---

## 7. Strategy extraction workflow

Insight → testable strategy thesis path (each step is operator-gated):

```
1. OBSERVATION (analyzer output)
   "N wallets in our calibration / curated set classified as category specialists
   in <category C> show a consistent pattern of gradual accumulation prior to
   favorable resolution drift, with effect size E and OOS support in K of N."

2. CANDIDATE THESIS
   Reframe the observation as a *testable rule*, not a behavior:
   "In category C, when ≥ M independent specialists have accumulated ≥ X% of
   their category-norm size in a market in the last D days, the market may be
   worth monitoring."

   NOT: "Copy them." NOT: "Buy when this fires."

3. DATA INVENTORY (what we'd need to test)
   - per-market accumulation timeline (we have this from activity-subgraph)
   - per-specialist category-norm baseline (computable from analyzer output)
   - resolution outcomes (we have this from market_outcomes)
   - price history at fidelity F (have this from CLOB; verify gaps)
   - liquidity / slippage estimates (need separate collection — flagged)

4. SIMULATION CANDIDATE
   Identify which downstream tools could test the thesis:
   - replay against captured fixtures (no live calls)
   - retrospective analyzer with the candidate rule as an entry filter
   - sensitivity sweep on parameters (M, X, D)
   Document what is and is not simulatable with existing fixtures.

5. KILL CRITERIA (pre-registered, NOT post-hoc)
   - effect size < threshold T after slippage assumption S → kill
   - profit factor < 1.05 over OOS sample → kill
   - alpha not surviving block bootstrap → kill
   - sample size too small for stat power → kill
   These are written down before the simulation runs.

6. PAPER VALIDATION
   Only if simulation passes the kill criteria, AND only if the operator
   approves moving from sim to paper.

7. LIVE
   Only operator-gated, after paper validation under pre-registered criteria,
   with bankroll and exposure limits documented.
```

The analyzer is the **observation-layer**. Steps 2-7 are downstream layers we may build later. This doc does NOT design steps 4-7; it only names them so the workflow is honest about what's missing.

---

## 8. Honesty / anti-overclaiming rules

The Thesis Review Card and Specialist Registry both run a **downgrade-and-reject** filter. Any of these triggers a downgrade band; multiple triggers can outright reject:

| Trigger | Effect on evidence |
|---|---|
| Completeness < threshold (red coverage score) | reject from registry |
| Attribution coverage < 95% in category C | reject the wallet's vote in C |
| Sample size < N closed positions in C | downgrade by one band |
| PnL concentrated > 60% in 1-2 markets in C | downgrade by one band; surface as caveat |
| Wallets not independent (correlation-pruned) | merge into one vote |
| Category labels weak (high `without_leaf_tags` share) | flag review as data-thin |
| Timing surface underpowered (low confidence) | suppress any timing-based claim |
| Position-building fragmented/ambiguous | suppress any accumulation/timing claim |
| Stale fixture (> recapture window) | reject until recaptured |
| Edge claim out-of-category | reject; only consult specialists in matching grain |
| `BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY` AND consulted grain ≠ supported grain | reject for this trade |
| Capital ledger pUSD/USDC.e mixed and not yet token-true | flag as data-thin; do not assume 1:1 silently |
| ACCOUNTING_TIE_UNSUPPORTED with delta > 100% | flag; do not block (this is a lifecycle-vs-portfolio gap, not a wallet-quality signal) |
| DW-EW divergence > 3× | downgrade by one band (concentration risk) |
| Inactivity of all specialists in C | report as "absence of signal," do not call it "no concern" |

Hard rule: every Thesis Review Card must include the **caveats section above the alignment summary** — caveats come before the verdict, not after.

---

## 9. Output taxonomy

Every insight (per-wallet) and every thesis review (per-trade) gets a label from this fixed set. Each label has explicit evidence requirements.

| Label | Evidence required |
|---|---|
| `ignore` | wallet failed `gate_result`, or insight has < N sample, or completeness red |
| `monitor` | `overall_verdict ∈ {edge_detected, suggestive}`, but limitations dominate or category labels are weak — keep in registry but do not consult yet |
| `research_only` | repeatable pattern at wallet level, but no testable rule yet — feeds into strategy extraction layer |
| `supports_existing_thesis` | ≥ 2 independent specialists with matching grain edge are aligned with our side, no rejecting flags |
| `contradicts_existing_thesis` | ≥ 1 independent specialist with matching grain edge opposed, with conviction-relative-to-norm size |
| `specialist_evidence` | wallet qualifies as specialist in a category, registry entry created. NOT a trading signal; does not imply entry / exit / sizing |
| `candidate_strategy_thesis` | cross-wallet pattern observed in Strategy Pattern Audit with reproducible feature definition |
| `simulation_candidate` | `candidate_strategy_thesis` + data inventory complete + kill criteria pre-registered |
| `paper_test_candidate` | `simulation_candidate` survived sim with effect above threshold after slippage assumptions |

Label transitions are explicit and one-way (except via re-analysis): a thesis cannot jump from `research_only` to `paper_test_candidate` without passing through `simulation_candidate`.

---

## 10. Recommended implementation order

Each layer is operator-gated; do not chain implementations without per-layer review.

1. **Wallet Intelligence Workflow contract** — design/spec layer first. Defines routing across consumers, evidence labels (§9 taxonomy), downgrade rules (§8), output contracts (Thesis Review Card shape, registry shape, sim-spec shape). No code yet; this is the contract every consumer ships against.

2. **Specialist Consensus Surface (concrete primitive)** — read-only output of analyzer runs, derived deterministically from `tag_grains` + `data_quality_flags` + `extractor_features_summary`. Provides category-local specialist lookup: "who has edge in category C, how strong, how recent, how complete, how independent." No new data collection, no new schema beyond an internal index. Hardens gate criteria from §4.1 and exposes the registry shape from §4.2.

3. **Independence Pruner** — implements §5.1 pair-wise checks on the registry and emits correlated-cluster annotations. **Must exist before Specialist Thesis Review is used seriously**; otherwise the review will overcount correlated wallets as independent voters.

4. **Specialist Thesis Review** — consumes Specialist Consensus Surface + Independence Pruner. Given a `trade_thesis` input, produces the Thesis Review Card per §6.2. Evidence only — no validation language, no entry/exit/sizing recommendation. Reads existing `followed_wallet_activity` / `closed_positions` / `open_positions` surfaces; no new data collection beyond category-label completeness work.

5. **Strategy Thesis Catalog / Strategy Pattern Audit cross-wallet expansion** — the existing per-wallet Strategy Pattern Audit (already in repo per `AGENTS.md`) consumes the registry to find cross-wallet patterns. Designs `research_only` and `candidate_strategy_thesis` outputs and persists them to a catalog the next layer can pull from.

6. **Simulation Backlog Generator** — emits simulation specs only. No simulator implementation here. For each `candidate_strategy_thesis` in the catalog, builds a spec containing: testable rule, data inventory (what surfaces are needed), pre-registered kill criteria, simulation scope (historical fixtures vs forward replay), and operator-gate marker. The downstream simulator (when designed separately) consumes these specs.

7. **Anomaly Detector** — specialist drift / abrupt-flip / coverage-staleness detection. Lower priority than the rest because it depends on `last_position_ts` being kept current via recurring recaptures.

8. **Operator-facing Insight Workflow UI** — the surface that combines all of the above for routine review. Last. Do not start until layers 1–7 each have a passing audit.

### Order rationale

- (1) is the contract; without it every consumer reimplements routing/labeling/downgrade.
- (2) is the primitive every consumer needs.
- (3) prevents (4) from overcounting; ship before (4) ships to honest use.
- (4) is the killer workflow but is honest only when (3) is in place.
- (5) and (7) are independent consumers of (2) that can build in parallel after (3).
- (6) builds on (5); cannot start before the catalog has entries.
- (8) is UX; everything below it must be honest first.

### Anti-recommendations (explicit non-starters)

- Do NOT start a "Specialist Copy Pipeline" as part of this workflow. Copy is a deployment mode; WIW is an evidence layer.
- Do NOT build a "specialist confidence number" or scalar score for thesis reviews. The output is qualitative bands and counts, never a probability or confidence value.
- Do NOT consume the registry from a live trading service. The registry is research-tier; any live use requires a separate operator-gated path.
- Do NOT name any consumer "Validator." Specialist evidence reviews a thesis; it does not validate it.
- Do NOT auto-promote outputs from `research_only` → `paper_test_candidate`. Each transition is operator-gated.

---

## 11. Acceptance check vs the task brief

| Acceptance criterion | Section satisfying it |
|---|---|
| "Clearly explains what we do after the analyzer works" | §1 workflow map; §2-§9 fill in each step |
| "Reframes wallet insights around strategy intelligence, not copying" | §0 doctrinal framing; §10 anti-recommendations |
| "Includes specialist review as evidence not proof" | §4-§6, especially §5.3 hard rules and §6.2 caveats-above-summary card |
| "Identifies multiple practical workflows, not just thesis review" | §1 map names six consumers (Specialist Thesis Review, Anomaly Detector, Strategy Pattern Audit cross-wallet, Strategy Thesis Catalog, Simulation Backlog Generator, Insight Workflow UI); §3 enumerates per-insight uses; §10 implementation order covers all |
| "Explains how wallet-specific insights become testable theses" | §7 extraction workflow |
| "Defines what must be simulated later and what should not be simulated yet" | §7 step 4 names sim path; §10 layer 6 (Simulation Backlog Generator) emits sim specs only; §0 + §12 explicitly exclude simulator design from this doc |

---

## 12. Non-goals (do not interpret this design as)

- A spec for the simulator
- A spec for paper trader integration with the registry
- A scoring formula for specialists
- An auto-trader, even gated
- An endorsement of "follow these N wallets and copy their trades"
- A claim that the analyzer is already validated — calibration is in progress

This doc designs **the layer that connects validated analyzer output to honest research workflows**. It deliberately stops at "operator-gated next steps" for every consumer. The simulator, paper trader, and any live activation paths are out of scope and must be designed separately.
