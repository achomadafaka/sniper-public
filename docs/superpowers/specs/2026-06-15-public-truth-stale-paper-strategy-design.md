# Public-Truth Stale Paper Strategy v0

**Date:** 2026-06-15
**Status:** design-only
**Scope:** paper-only research strategy. No live orders, no production DB writes, no source scans, no migrations.
**Budget model:** each paper strategy gets a virtual $400 bankroll; this strategy uses fixed $20 paper entries.

## 1. Purpose and Non-Goals

Purpose: test whether public truth can beat Polymarket repricing. A signal exists when a market outcome appears determined by public evidence, while Polymarket still offers the winning outcome below payout after executable depth and modeled costs.

This is not prediction. It is not wallet copying. It is not ambiguity trading. It is paper-only research into latency between public truth, market repricing, and oracle/final resolution.

Non-goals:

- no live orders or live-order logic;
- no production DB mutation;
- no DB migrations in this spec phase;
- no LLM-decided finality;
- no source-lane mixing in performance claims;
- no rule-lawyer/dispute strategy in v0 scoring;
- no claim that a strategy works without naming the source lane.

## 2. Thesis

Markets resolve according to their rule text, resolution source, end date, and edge cases. Polymarket's resolution docs also describe proposal, challenge, dispute, and final redemption flow. That creates possible latency windows:

```text
public truth appears -> Polymarket reprices -> UMA/proposal/final resolution -> payout
```

The strategy papers entries during the first gap. It tests whether the gap is large and frequent enough to overcome spread, depth, slippage, source errors, and false positives.

Core hard rule:

```text
X can detect.
X does not settle unless market rules explicitly make that X source, post, or account the resolution source.
```

Sources:

- Polymarket resolution: https://docs.polymarket.com/concepts/resolution
- Polymarket orderbook/depth: https://docs.polymarket.com/trading/orderbook
- X API rate limits: https://docs.x.com/x-api/fundamentals/rate-limits

## 3. Source-Tier Lane Model

Results are always segmented by lane.

### Lane A: `resolution_authoritative`

Cleanest dataset. Source directly matches the market's stated resolution rules.

Entry requires:

```text
source_tier = resolution_authoritative
rule_match_status = exact_match
finality_status = official_final
net_edge >= 0.04
```

Claims must read:

```text
Lane A resolution-authoritative worked / failed.
```

Never combine Lane A PnL with Lane B/C PnL.

### Lane B: `official_social_source`

Official entity/team/exchange/agency/event social account, including X.

Use for fast detection and latency measurement. It may trigger a paper entry when market rules are compatible and the market is stale. It must later be checked against a rule-compatible source or final market resolution.

Claims must read:

```text
Lane B official-social leads worked / failed.
```

### Lane C: `trusted_news_source` / `specialist_account`

Curated journalists, specialist analysts, trackers, or domain feeds. These produce paper candidates with caveats and account-level false-positive tracking.

Claims must read:

```text
Lane C trusted-news/specialist leads worked / failed.
```

### Lane D: `ambiguity_dispute`

Experimental only. Logged, if at all, as research. Excluded from v0 pass/fail and requires separate approval before paper scoring.

## 4. Eligible Market Universe

Eligible:

- active Polymarket markets with known `condition_id`, token/asset id, outcome labels, and rule text;
- markets whose rule text can be classified as compatible with the source observation;
- objective outcome markets where a final state can be checked later;
- binary and multi-outcome markets, including negative-risk markets, if the token-level payout can be modeled.

Start with:

1. sports official finals plus official social for the same events;
2. official exchange listing/delisting announcements;
3. government/economic official releases;
4. weather official observations;
5. awards/TV official winner pages;
6. curated official social accounts;
7. curated specialist/news accounts.

Excluded from v0 scoring:

- subjective "credible reporting" markets unless the source exactly matches the rules;
- markets requiring interpretation of intent, tone, or "substantial" threshold;
- ambiguous/disputed markets;
- markets where the resolution source cannot be recovered and hashed;
- markets already resolved or not accepting orders at observation time.

## 5. Source-Account Registry

`source_accounts` is mandatory before Lane B/C automation. It is a proposed design entity, not an existing schema table.

Fields:

```text
account_id
handle
display_name
source_tier
account_class
official_url_crosslink
approved_market_categories
reliability_notes
active
created_at
updated_at
```

Allowed `account_class` values:

```text
official_entity
official_event
official_team
official_exchange
government_agency
journalist
specialist_analyst
aggregator
bot_tracker
unknown
```

Rules:

- do not trust blue checks or paid verification alone;
- require manual curation and category approval;
- require an official crosslink for official accounts where possible;
- Lane B/C entries from unregistered accounts are rejected or marked experimental;
- account performance is evaluated independently.

## 6. Adapter Contract

Every source adapter emits a normalized observation. Adapters do not decide finality beyond their declared evidence fields.

Required adapter output:

```text
adapter_name
adapter_version
source_family
source_tier
source_account_id
source_url
observed_source_value
source_timestamp
fetch_timestamp
raw_text_hash
linked_url
linked_url_hash
media_hash
evidence_hash
rate_limited
missed_due_to_rate_limit
source_fetch_error
source_fetch_latency_ms
```

For X/news observations, additionally:

```text
post_id
author_id
handle
post_url
post_timestamp
deleted_or_unavailable_later
corroborating_source_count
```

Rate-limit handling:

- log 429/rate-limit state from endpoint headers where available;
- distinguish `missed_due_to_rate_limit` from true no-signal;
- do not backfill missed signals into the paper book as if they were seen live;
- source outage periods are reported separately.

## 7. Rule-Match and Finality Gates

Rule matching is separate from source detection.

Fields:

```text
rule_text_hash
resolution_source_url
rule_match_status
finality_status
confirmation_status
requires_official_confirmation
```

Allowed `rule_match_status`:

```text
exact_match
compatible
weak_match
contradicted
unknown
```

Allowed `finality_status`:

```text
official_final
official_preliminary
official_social_claim
trusted_lead_claim
rumor
unknown
```

Lane A requires `exact_match` and `official_final`.

Lane B/C may enter paper on `compatible` plus an allowed source tier, but must be tracked as requiring later confirmation.

No LLM-decided finality. Parsers can classify leads; rule-compatible confirmation decides.

## 8. Paper Execution Model

Virtual bankroll:

```text
strategy_bankroll = 400
paper_stake = 20
max_open_exposure = 400
max_same_event_exposure = 40
```

Entry gate:

```text
market active
rule text compatible
source tier allowed
source observation says outcome appears determined
market has not repriced
net_edge = expected_payout - depth_weighted_entry_price - modeled_costs >= 0.04
```

`expected_payout`:

- winning outcome: `1.00`;
- confirmed 50/50/push-style outcome: `0.50`;
- unknown or disputed: no v0 entry.

Exit:

- hold to official final resolution;
- no early exit in v0 scoring;
- mark but do not exit at 5m, 15m, 60m, UMA proposal, and final resolution.

## 9. Price, Depth, and Slippage Model

Paper entry uses executable depth, not best ask alone.

For a $20 paper stake:

1. fetch token orderbook;
2. walk ask levels until $20 notional is filled or book depth is exhausted;
3. compute `depth_weighted_entry_price`;
4. add modeled costs;
5. reject if depth is insufficient or `net_edge < 0.04`.

Required price fields:

```text
best_bid
best_ask
spread
depth_weighted_entry_price
depth_available_usdc
modeled_costs
net_edge
orderbook_hash
price_fetch_timestamp
```

Marks:

```text
mark_5m
mark_15m
mark_60m
mark_uma_proposal
mark_final_resolution
```

CLV is measured from entry to each mark. Final PnL is measured from paper shares and final payout.

## 10. Persistence and Table Design

All entities here are proposed design entities. No migration is created in this phase.

Proposed entities:

```text
source_accounts
public_truth_observations
public_truth_paper_candidates
public_truth_paper_entries
public_truth_price_marks
public_truth_confirmations
public_truth_false_positive_reviews
```

### `public_truth_observations`

Fields:

```text
observation_id
source_tier
source_family
source_account_id
source_url
post_id
post_url
author_id
handle
observed_source_value
source_timestamp
fetch_timestamp
raw_text_hash
linked_url
linked_url_hash
media_hash
evidence_hash
adapter_name
adapter_version
rate_limited
missed_due_to_rate_limit
source_fetch_error
source_fetch_latency_ms
```

### `public_truth_paper_candidates`

Fields:

```text
candidate_id
observation_id
market_id
condition_id
token_id
asset_id
outcome
rule_text_hash
resolution_source_url
rule_match_status
finality_status
expected_payout
best_bid
best_ask
spread
depth_weighted_entry_price
modeled_costs
net_edge
reject_reason
created_at
```

### `public_truth_paper_entries`

Fields:

```text
entry_id
candidate_id
paper_lane
strategy_bankroll
paper_stake
shares
entry_price
entry_timestamp
expected_payout
max_same_event_exposure
status
final_payout
pnl_usdc
roi_pct
```

### `public_truth_confirmations`

Fields:

```text
confirmation_id
entry_id
confirmation_status
confirmation_source_url
confirmation_timestamp
market_resolution_status
market_resolution_timestamp
lead_to_confirmation_latency_s
lead_to_market_reprice_latency_s
notes
```

Allowed `confirmation_status`:

```text
confirmed_by_resolution_authoritative
confirmed_by_market_resolution
contradicted_by_resolution_authoritative
still_unconfirmed
ambiguous
```

The report distinguishes:

```text
trade result
source confirmation result
final market resolution result
```

These are not the same.

## 11. Metrics

All metrics are reported by lane, source family, account, and market category where sample size allows.

Required metrics:

```text
n_signals
n_resolved
false_positive_rate
confirmation_rate
lead_to_confirmation_latency
lead_to_market_reprice_latency
median_entry_edge
ROI after spread/slippage
account_level_pnl_concentration
source_family_pnl_concentration
deleted_or_unavailable_post_rate
rate_limit_misses
fetch_failures
source_classification_errors
adapter_errors
```

Forbidden report claim:

```text
strategy works
```

Required claim shape:

```text
Lane A resolution-authoritative worked / failed.
Lane B official-social leads worked / failed.
Lane C trusted-news/specialist leads worked / failed.
```

## 12. Pass and Kill Bars

### Lane A pass bar

```text
30 resolved paper signals
false positives < 5%
positive ROI after modeled costs
median net edge >= 3c
no single event > 25% of PnL
```

### Lane B/C pass bar

```text
50 resolved paper signals
false positives < 8-10%
confirmation rate >= 70%
positive ROI after modeled costs
no single account > 40% PnL unless explicitly labeled account-specific
no source-family claim unless the source family has enough resolved samples
```

### Kill bars

```text
3 rule-interpretation failures in 20 signals
any account or adapter has 2 false positives in first 10 signals
ROI negative after minimum resolved sample
most signals appear only after PM already repriced
ambiguity/dispute dominates sample
source fetch/rate-limit failures make missed-signal accounting unreliable
```

Lane D is excluded from v0 pass/fail.

## 13. False-Positive Review Process

Every false-positive candidate gets a review row.

Review categories:

```text
source_wrong
source_early
source_deleted_or_unavailable
parser_wrong
rule_match_wrong
finality_wrong
market_rule_ambiguous
market_resolution_surprised_source
adapter_outage
rate_limit_miss
unknown
```

Review requirements:

- preserve source evidence hashes;
- record whether the source was later deleted/unavailable;
- identify whether the failure was account-specific, adapter-specific, or rule-class-specific;
- suspend an account/adapter when it breaches kill bars.

## 14. Rate-Limit and Outage Handling

X and other sources must be collected with explicit rate-limit accounting. X docs publish per-endpoint windows and 429 behavior; the collector must treat rate-limit windows as observability gaps.

Required outage fields:

```text
rate_limited
missed_due_to_rate_limit
source_fetch_error
source_fetch_latency_ms
endpoint_name
rate_limit_reset_ts
```

Rules:

- missed signals due to rate limits are not counted as no-signal intervals;
- candidate absence during outage is not evidence against the strategy;
- paper entries are only created from observations seen at or before entry time;
- no retroactive paper entries from backfills.

## 15. Source-Family Rollout Order

Rollout starts where false positives are easiest to audit.

1. Sports official finals plus official social for the same events.
2. Official exchange listing/delisting announcements.
3. Government/economic official releases.
4. Weather official observations.
5. Awards/TV official winner pages.
6. Curated official social accounts beyond the event universe.
7. Curated specialist/news accounts.

Elections, courts, awards, and politics are deferred until the adapter framework and false-positive review flow are proven.

## 16. Implementation Phasing

Phase 0: design spec only.

Phase 1: manual paper log template for 5-10 signals.

Phase 2: `source_account` registry and observation schema.

Phase 3: one sports official-final adapter.

Phase 4: price/depth paper scorer.

Phase 5: X official-social lead adapter.

Phase 6: trusted-source lane.

Phase 7: evaluation report.

No phase may place live orders. Any transition beyond paper requires separate approval and a new spec.

## 17. Test Plan

Spec-only checks:

- schema-reference validation does not confuse proposed entities with current tables;
- no unresolved placeholders;
- no wording that mixes lanes in performance claims;
- no live-order language.

Phase 1 manual-paper tests:

- manually record 5-10 signals;
- verify every signal has a rule text hash and source evidence hash;
- verify Lane B/C entries later receive confirmation status.

Phase 2 schema tests:

- proposed entities validate required fields;
- account registry rejects unapproved account/category pairs;
- Lane B/C unregistered accounts are rejected or experimental.

Phase 3 adapter tests:

- sports final adapter emits normalized observations;
- rate-limit/outage states are logged;
- parser does not emit `official_final` unless source is rule-compatible.

Phase 4 price tests:

- depth walk computes executable entry for $20;
- insufficient depth rejects the candidate;
- net edge uses depth-weighted entry plus modeled costs.

Phase 5 X adapter tests:

- post evidence is hashed and preserved;
- deleted/unavailable status can be updated later;
- account-level false-positive gates work;
- rate-limit misses are counted separately from no-signal.

Evaluation tests:

- Lane A/B/C performance reports are separated;
- Lane D is excluded from pass/fail;
- report claim text includes lane names;
- false-positive review categories are exhaustive for observed failures.

## Open Questions

1. Which sports source comes first: ATP/WTA, NBA, MLB, UFC, or soccer?
2. What modeled cost should v0 use before live instrumentation exists?
3. What minimum book depth should reject a $20 paper entry?
4. Should Phase 1 manual logs live in a CSV, SQLite sidecar, or existing `strategy_research` notes?
5. Which X API access tier is available locally, and what endpoint limits apply to the selected account/source set?

## References

- Polymarket resolution docs: https://docs.polymarket.com/concepts/resolution
- Polymarket orderbook docs: https://docs.polymarket.com/trading/orderbook
- X API rate limits: https://docs.x.com/x-api/fundamentals/rate-limits

