# Wallet Understanding Brain — Design

**Date:** 2026-04-22
**Version:** v1
**Status:** design draft after archetype-extractor iteration + SwissMiss review
**Primary objective:** understand how wallets behave, how they win, and what the brain should learn from them

---

## 1. Goal

Build a wallet-understanding system whose first responsibility is **faithful explanation**, not action.

The system should answer:

1. What did this wallet actually do?
2. What stable behavior pattern does that resemble?
3. What edge sources are plausible?
4. Where did the actual winnings come from?
5. What should the platform do with that knowledge?

This is deliberately different from "should we copy this wallet?".

`copyability` is a downstream implication of understanding, not the center of the design.

If a wallet is:
- highly profitable,
- mechanically unusual,
- not cleanly copyable,
- hard to label,
- or directly contrary to our prior assumptions,

it is still valuable because it teaches the brain what profitable prediction-market behavior can look like in the wild.

---

## 2. Why This Exists

The current system has already shown that assumptions fail in two directions:

1. We previously over-relied on score labels (`insider_score`, `speculator_score`) and discovered they can be directionally wrong or based on weak premises.
2. Even after moving toward archetype extraction, there is a risk of collapsing the system too early into action labels like `study_only` or `validated_copy`.

That second failure mode is subtler but just as dangerous.

If the system asks "what should I do with this wallet?" too early, it starts forcing shallow interpretations:
- it looks for reasons to exclude,
- it overfits current strategy needs,
- it mistakes uncertainty for uselessness,
- it forgets that wallets are training data for how the market works.

The brain should learn from:
- the good,
- the bad,
- the pretty,
- and the ugly.

That requires preserving structure:
- facts separate from patterns,
- patterns separate from hypotheses,
- hypotheses separate from outcomes,
- outcomes separate from actions.

---

## 3. Design Principle

**Each layer must answer a different class of question.**

Layers must not overlap, smuggle conclusions, or back-fill each other's uncertainties.

The design should be:
- **descriptive first** — say what happened before saying what it means
- **assumption-light** — derive from observable behavior wherever possible
- **uncertainty-preserving** — "unknown" is a valid result
- **useful without action** — a wallet can teach us something even if no strategy should copy it
- **brain-oriented** — the output should be reusable across wallets, not just written once and forgotten

If a layer is doing work that belongs to a downstream layer, the design has become lazy.

---

## 4. Non-goals

- Not forcing every wallet into a neat identity class.
- Not making "copy / no-copy" the core output.
- Not treating a profitable wallet as validated just because it wins.
- Not pretending observable behavior proves privileged information.
- Not using one merged text blob as the brain record.
- Not collapsing the system into one scalar "wallet quality" score.

---

## 5. The Stack

The stack should have five layers.

### 5.1 Layer 1 — Observations

**Question:** What literally happened?

This layer contains only measurable facts. No interpretation.

#### Inputs

- Raw activity rows
- Raw positions
- Raw profile metadata
- Raw leaderboard presence
- Local derived statistics with direct mechanical meaning

#### Allowed outputs

- counts
- timestamps
- distributions
- ratios
- concentrations
- per-market aggregates
- recurrence counts
- data quality flags

#### Example fields

- `buy_count`
- `sell_count`
- `redeem_count`
- `distinct_conditions`
- `distinct_titles`
- `paired_condition_ratio`
- `paired_value_share`
- `entry_price_bucket_distribution`
- `midpoint_buy_ratio`
- `price_repetition_ratio`
- `usdc_size_cv`
- `inter_trade_interval_cv`
- `time_of_day_histogram`
- `wallet_lifetime_days`
- `wallet_lifetime_source`
- `pagination_complete`
- `resolved_position_count`
- `pnl_by_category`

#### Why this layer exists

Because most downstream mistakes come from weak observation discipline.

If the system cannot say cleanly what was observed, every later explanation is contaminated.

This layer should be maximally boring and maximally trustworthy.

#### Expected outcome

A structured fact packet that another engineer can audit without debating interpretation.

If two people disagree about a wallet, they should still be able to agree on Layer 1.

#### Failure mode if missing

The system will smuggle stories in as facts:
- "this wallet is an insider"
- "this wallet is a thesis specialist"
- "this wallet is copyable"

Those are not observations.

---

### 5.2 Layer 2 — Behavior Pattern

**Question:** What recurring behavioral pattern does the observed activity resemble?

This layer compresses facts into stable shapes, but still does not claim why the wallet wins.

#### Inputs

- Layer 1 observations only

#### Allowed outputs

Behavior-pattern labels such as:
- `paired_outcome_arb`
- `two_sided_inventory`
- `accumulator`
- `bursty_discretionary`
- `template_exploiter`
- `slow_thesis_builder`
- `mixed_unresolved`

These are examples, not a locked taxonomy.

#### Why this layer exists

Because many mechanical patterns are terminal or explanatory on their own.

For example:
- if a wallet is systematically buying both sides, that matters regardless of intent
- if a wallet almost never sells and repeatedly scales in, that matters regardless of narrative

Pattern is a better second layer than identity because it stays closer to observable behavior.

#### Expected outcome

A small number of non-overlapping shape labels with rationale tied to Layer 1.

This layer should tell us what the wallet tends to do, not what kind of person or trader it "is".

#### Failure mode if missing

The system jumps straight from facts to theory:
- low paired ratio + bursty timing -> "insider"
- concentrated sports activity -> "sharp sports bettor"

That is too much semantic leap too early.

---

### 5.3 Layer 3 — Edge Hypotheses

**Question:** What edge sources are plausibly generating the wallet's performance?

This is the most important new layer and the least likely to be represented by a single label.

The system should allow multiple plausible edges at once.

#### Inputs

- Layer 1 observations
- Layer 2 behavior pattern

#### Allowed outputs

An edge vector, not a single archetype:

- `market_selection_edge`
- `entry_timing_edge`
- `sizing_edge`
- `exit_management_edge`
- `structural_exploitation_edge`
- `portfolio_construction_edge`
- `informational_edge_signature`

Each should be represented as:
- `low`
- `medium`
- `high`
- `unknown`

with explicit rationale.

#### Why this layer exists

Because wallets do not win for one reason.

A profitable wallet may be:
- average at market selection,
- excellent at sizing,
- weak at exits,
- and still highly profitable.

Forcing a single edge label destroys information.

This layer is where the system becomes useful as a learning machine for prediction-market behavior rather than a copy filter.

#### Expected outcome

A structured hypothesis profile explaining what the wallet may be good at, what it may be bad at, and what remains unproven.

The key word is **hypothesis**. This layer should not claim certainty where only plausibility exists.

#### Edge dimensions — reason and expected outcome

##### `market_selection_edge`

**Reason:** Some wallets do not trade better; they choose better places to trade.

Signals:
- recurring profit in specific categories or market families
- avoiding domains where returns are weak
- repeatedly showing up in mispriced event types

Expected outcome:
- tells the brain whether the wallet's advantage begins before the first trade
- helps cluster wallets that "find good ponds" even if their execution differs

##### `entry_timing_edge`

**Reason:** A wallet may be right mainly because it enters early enough.

Signals:
- favorable entry-price buckets
- large buys before subsequent price drift
- positioning before resolution paths become obvious
- buying into dips before recovery

Expected outcome:
- helps separate "knows what to buy" from "knows when to buy"
- is especially important for any later copy/replay simulation

##### `sizing_edge`

**Reason:** Many good traders are not mostly better selectors; they are better capital allocators.

Signals:
- larger size on better outcomes
- unequal sizing that maps to later realized gains
- readable scale-in or scale-up behavior

Expected outcome:
- teaches the brain what conviction sizing looks like
- differentiates systematic equal-size trading from human or hybrid judgment

##### `exit_management_edge`

**Reason:** Some steady winners make money by managing exits well, not entries.

Signals:
- meaningful use of sells vs redeems
- recovery from bad entries
- trimming behavior
- realized outcomes better than naive hold equivalents

Expected outcome:
- prevents over-crediting entry skill for wallets that actually win through exits
- identifies wallets whose edge may be operationally hard to reproduce

##### `structural_exploitation_edge`

**Reason:** Some wallets are not informationally superior; they are better at exploiting repeatable market structure.

Signals:
- recurring behavior in narrow templates
- repeated use of specific price zones
- negative-risk habits
- high regularity in trade placement
- consistent exploitation of one market family

Expected outcome:
- identifies reverse-engineering candidates
- helps the brain learn which market structures may support systematic edges

##### `portfolio_construction_edge`

**Reason:** A wallet can win because the book is coherent, hedged, or selectively concentrated, even when individual positions are not extraordinary.

Signals:
- thematic baskets
- concentration management
- correlated position building
- coexistence of high-probability carries and selective directional shots

Expected outcome:
- helps explain why some wallets have stable equity curves without obvious superstar entries
- teaches the brain how profitable portfolios are assembled, not just how bets are picked

##### `informational_edge_signature`

**Reason:** Some wallets may exhibit behavior consistent with unusually early or unusually informed entries, even when direct proof is impossible.

Signals:
- narrow burst activity
- early concentration into few markets
- rapid correctness
- pre-repricing participation

Expected outcome:
- conservative flag, not accusation
- teaches the brain when a wallet's behavior is difficult to explain by normal broad-market participation

#### Failure mode if missing

The system ends up knowing only labels, not edges.

That means it cannot learn what profitable trading looks like, only whether a wallet superficially resembles a category we already know.

---

### 5.4 Layer 4 — Outcome Attribution

**Question:** Where did the actual winnings come from?

This layer grounds the hypotheses in realized and unrealized results.

#### Inputs

- Layer 1 observations
- Layer 2 patterns
- Layer 3 edge hypotheses

#### Allowed outputs

- realized PnL by category
- realized PnL by subcategory
- realized PnL by entry-price bucket
- realized PnL by hold-vs-sell path
- concentration of gains
- broad vs episodic outcome structure
- fragility vs stability of returns

#### Why this layer exists

Because hypotheses are cheap and attractive stories are dangerous.

If we think a wallet wins because of timing, this layer should show whether realized gains actually came from trades with superior entry timing.

If we think a wallet is a thesis specialist, this layer should show whether profits were broad across that thesis area or concentrated in one lucky episode.

#### Expected outcome

A fact-backed answer to:
- what paid,
- what did not,
- and whether the edge is broad, narrow, stable, or episodic.

This is the layer that stops the brain from romanticizing wallets.

#### Failure mode if missing

The system will produce elegant but ungrounded theories:
- "sports specialist"
- "great selector"
- "high conviction"

without showing whether profit actually came from those things.

---

### 5.5 Layer 5 — Operational Implications

**Question:** What should the platform do with this wallet or what should the brain remember operationally?

This is the final layer because it is the least epistemically fundamental.

#### Inputs

- All previous layers

#### Allowed outputs

- `monitor`
- `study`
- `reverse_engineer_candidate`
- `validated_copy_candidate`
- `exclude_for_copy`
- `anti_pattern`

Multiple can coexist:
- `study` + `reverse_engineer_candidate`
- `monitor` + `exclude_for_copy`

#### Why this layer exists

Because the platform eventually needs operational consequences, but those consequences should be downstream of understanding.

This layer should never overwrite upstream uncertainty.

#### Expected outcome

A decision summary that is useful for humans and downstream agents, while remaining subordinate to the rest of the model.

#### Failure mode if missing

The system becomes academically descriptive and operationally inert.

#### Failure mode if moved earlier

The system becomes shallow, action-hungry, and overfit to current strategy needs.

---

## 6. Non-overlap Rules

These are mandatory.

### Rule 1

Layer 1 cannot contain labels like:
- `thesis_specialist`
- `copyable`
- `sharp`

### Rule 2

Layer 2 cannot claim edge:
- `good market selection`
- `informed`
- `copyable`

Pattern is not edge.

### Rule 3

Layer 3 cannot produce action decisions.

It can say:
- `high structural_exploitation_edge`

It cannot say:
- `reverse engineer this now`

### Rule 4

Layer 4 cannot invent causes.

It can say:
- "72% of realized PnL came from MMA"

It cannot say:
- "therefore the wallet is an MMA genius"

### Rule 5

Layer 5 cannot erase uncertainty.

If upstream says:
- `entry_timing_edge = unknown`

Layer 5 cannot silently produce:
- `validated_copy_candidate`

without carrying that uncertainty forward.

---

## 7. Uncertainty Rules

### 7.1 Unknown is valid

The system must allow:
- `mixed_unresolved`
- `edge unknown`
- `tenure incomplete`
- `outcome attribution insufficient`

### 7.2 Missing data is explicit

Every wallet record should carry missing-data flags such as:
- `pagination_complete = false`
- `profile_joined_ts missing`
- `insufficient resolved sample`
- `open-book only`

### 7.3 Hypothesis confidence is not action confidence

The system can have:
- `sizing_edge = medium`
- `entry_timing_edge = unknown`
- `operational implication = study`

That is not indecision. That is correct structure.

---

## 8. Brain Storage Model

The brain should store separate payloads, not one merged blob.

### Proposed structure

```json
{
  "wallet": "0x...",
  "as_of_ts": 1776820000,
  "observations": {...},
  "behavior_pattern": {...},
  "edge_hypotheses": {...},
  "outcome_attribution": {...},
  "operational_implications": {...},
  "data_quality": {...}
}
```

### Implementation contract for the current phase

The full five-layer schema is the target model, not the claim about what today's
extractor already does.

Near-term code should emit the full envelope so downstream systems can depend on
stable structure, but only some sections will be populated initially:

```json
{
  "wallet": "0x...",
  "as_of_ts": 1776820000,
  "observations": {...},
  "behavior_pattern": {...},
  "edge_hypotheses": {},
  "outcome_attribution": {},
  "operational_implications": {},
  "data_quality": {...}
}
```

Reason:
- keeps the storage contract stable while the brain grows in phases
- avoids hiding partial implementation behind an implied full theory stack
- lets current code become schema-correct without inventing premature edge models

Expected outcome:
- Phase 1/2 code can store high-quality observations and behavior patterns now
- Layer 3 onward can be added later without reshaping every caller
- absence of a populated section means "not implemented yet", not "implicitly none"

### Why this matters

Because later the brain should be able to ask:
- show me wallets with similar behavior patterns
- show me wallets with high sizing edge but low exit-management edge
- show me wallets whose gains are concentrated in one subcategory
- show me all unresolved but profitable wallets

That requires typed memory, not prose-only memory.

### Expected outcome

A reusable wallet-knowledge layer that supports:
- clustering,
- retrieval,
- research comparisons,
- future automated review agents,
- and eventual copy/reverse-engineering decisions.

---

## 9. Example: SwissMiss Under This Model

SwissMiss is a useful test case because it resists clean labeling.

### Observations

- old wallet by profile age (`~813` days)
- high activity
- very low sell rate
- low paired-condition ratio
- sports-heavy but not narrow
- very high sizing variance
- public activity history API-capped

### Behavior Pattern

- high-frequency accumulator

### Edge Hypotheses

Plausible:
- `market_selection_edge = medium`
- `sizing_edge = medium`
- `portfolio_construction_edge = medium`

Unclear:
- `entry_timing_edge = unknown`
- `exit_management_edge = low/unknown`

Weak:
- `structural_exploitation_edge = low`
- `informational_edge_signature = low`

### Outcome Attribution

- gains concentrated in a subset of sports and geopolitical positions
- not enough evidence yet for a clean thesis-specialist story
- not enough evidence for a structural-exploitation story either

### Operational Implication

- `study`
- `monitor`
- not yet `validated_copy_candidate`
- not `exclude_for_copy`

### Why this example matters

Because the system should be able to describe a profitable wallet honestly without forcing it into a neat class.

If the design cannot handle wallets like SwissMiss, it is not robust enough for real market behavior.

---

## 10. Relation to Current Code

Current extractor work should be treated as a strong Layer 1 foundation plus a
provisional Layer 2 bridge.

### Current code that fits Layer 1 well

- trade counts
- sell / redeem counts
- paired-condition ratios
- domain concentration
- size dispersion
- timing dispersion
- profile tenure
- resolved PnL distribution

### Current code that fits Layer 2 well

- mechanism labels like paired-outcome / inventory / accumulator
- conservative secondary pattern guesses that are still close to behavior

Important:
- today's second classifier is useful, but it is not yet the final Layer 2 taxonomy
- some of its labels still carry more interpretation than the design ultimately wants
- that means it should be stored as a provisional behavior-pattern bridge, not as proof of edge or identity

### What is still missing

Layer 3 and Layer 4 are not yet first-class structures.

Today we can observe and pattern-match.
We are not yet systematically modeling edge sources or attributing realized outcomes to them.

---

## 11. Expected Outcomes of the Design

If implemented correctly, this design should produce five concrete improvements.

### 11.1 Better research quality

The system will stop mistaking action labels for understanding.

### 11.2 Better brain reuse

The brain will accumulate reusable knowledge about what profitable prediction-market behavior looks like.

### 11.3 Better tolerance for weird winners

Unusual but profitable wallets will become research assets instead of classification failures.

### 11.4 Better operational judgment

Copy / study / reverse-engineer decisions will be grounded in a richer model of the wallet.

### 11.5 Better falsifiability

Because facts, patterns, hypotheses, outcomes, and implications are separated, errors can be found and corrected without rewriting the entire interpretation.

---

## 12. Implementation Order

This should be built in phases.

### Phase 1 — Normalize Layer 1

- finalize observation schema
- make data-quality flags first-class
- ensure every feature is explicitly observational

### Phase 2 — Clean Layer 2

- reduce overlap in pattern labels
- keep patterns close to mechanics
- remove identity-heavy labels where evidence is weak
- keep provisional secondary patterns explicitly marked as provisional until the taxonomy settles

### Phase 3 — Add Layer 3 edge-hypothesis model

- define edge dimensions
- compute heuristic evidence per edge
- store low / medium / high / unknown with rationale

### Phase 4 — Add Layer 4 outcome attribution

- realized PnL by category / subcategory / bucket / behavior path
- concentration and stability reporting

### Phase 5 — Rework Layer 5 operational implications

- make operational outputs downstream only
- prohibit them from overriding uncertainty

---

## 13. Open Questions

These are acceptable open questions, not design failures.

1. How many behavior-pattern labels are actually useful before the taxonomy becomes fragile?
2. Which edge dimensions are currently measurable versus only aspirational?
3. How should uncertainty be summarized in UI without flattening it?
4. When should the brain merge repeated wallet reviews versus keep them separate over time?
5. Which operational outputs deserve hard gates and which should remain advisory?

---

## 14. Bottom Line

The wallet-understanding brain should not be a dressed-up copy filter.

It should be a layered system that:
- observes behavior faithfully,
- compresses it into patterns,
- proposes plausible edges,
- checks those edges against realized outcomes,
- and only then decides what to do.

That is the only design that is:
- pragmatic,
- open-minded,
- brain-compatible,
- and robust to the fact that profitable prediction-market behavior is often stranger than our current taxonomy.
