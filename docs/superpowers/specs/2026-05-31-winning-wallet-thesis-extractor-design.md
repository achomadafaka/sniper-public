# Spec - Winning-wallet thesis extractor

**Date:** 2026-05-31
**Status:** design, rev 2
**Supersedes direction of:** broad Becker-first wallet mining as the primary discovery loop. Becker remains a replay/null/calibration harness, not the source of truth for finding theses.
**Parent doctrine:** `AGENTS.md` analyzer pipeline: wallet intelligence -> strategy extraction -> thesis formation -> thesis-specific dataset -> thesis-specific simulation -> paper/live validation.

## 1. Doctrine

The analyzer's primary input is an active, persistently winning wallet. Intake does not re-fight whether the wallet was lucky. The job is to decompose *how* the wallet made money, locate the edge groups, and name the method classes that plausibly explain each group.

**V1 intake:** operator-provided / curated active winners. A live winner-feed is a later intake module. The V1 analyzer must work on one supplied wallet without claiming that the intake source itself is unbiased.

A wallet may contain multiple strategies. A single wallet-level verdict is the wrong unit. The output is a thesis profile containing multiple candidate theses:

- edge group: the subset of positions carrying the edge;
- method match: what market failure or trader behavior explains it;
- evidence: PnL, `(r - p)`, CLV/proxy drift, timing, sizing, exit, inventory, or event-basket evidence;
- blockers: missing feed, private info, speed, external venue data, or capital constraint;
- runnability: runnable, data-blocked, speed-required, capital-required, or non-replicable;
- forward test: what would prove or kill the thesis.

Do not emit copy-trading advice, live triggers, expected PnL, or real sizing.

## 2. Key correction

The previous conveyor was too broad and too null-first. It asked a random-ish population whether any single pre-wired card survived. That is useful for calibration, but it is not the main product.

The main product is:

```
active winning wallet
  -> canonical event ledger
  -> semantic/event enrichment
  -> edge-position slicing
  -> edge-cluster formation
  -> method detectors per cluster
  -> multi-thesis wallet profile
  -> thesis-specific forward/paper validation
```

This is not "find one edge per wallet." It is "find all edge-bearing groups in the wallet, then classify the method behind each."

## 3. Inputs and data foundation

### 3.1 Canonical event ledger

Build one ledger per wallet with normalized event rows:

| Row kind | Required fields | Purpose |
|---|---|---|
| buy | wallet, condition_id, token/outcome_index, price, size, usdc, ts, tx | entry price `p` |
| sell | wallet, condition_id, token/outcome_index, price, size, usdc, ts, tx | exit behavior |
| redeem | wallet, condition_id, payout/outcome, ts, tx | resolution truth `r` |
| split_merge | wallet, condition_id, size, side, ts, tx | negRisk / inventory basket accounting |
| reward_rebate | wallet, condition_id/event_id, amount, ts, tx | liquidity/reward method accounting |
| open_position | wallet, condition_id, outcome_index, avg_price, size, current_price, ts | unresolved inventory |

Redemptions are mandatory. A held-to-resolution specialist can have no sell rows; without redemptions, the analyzer misses the exact book where the edge often lives.

### 3.2 Market semantics enrichment

Enrich every condition with:

- Gamma title/question, event slug/id, market slug, category, tags, series if present;
- outcome labels, outcome indexes, token ids;
- end_date, created_at, closed/resolved flags, resolution source/status if present;
- `negativeRisk` / event grouping;
- active/liquidity metadata where available.

Platform tags are the first semantic layer. They are not enough. Specialty must also use event family and title/template clustering, because "Politics" is too coarse to distinguish "US Senate special elections" from "Iran regime survival."

### 3.3 Event-level grouping

Group by event, not only by condition. Negative-risk baskets, multi-leg hedges, related markets, and split/merge inventory are event-level theses. Treating each condition as independent inflates sample size and can hide the true strategy.

## 4. Core architecture

### 4.1 `wallet_event_ledger`

Owns ingestion and normalization. It must not decide whether an edge exists.

Output: `WalletEventLedger`.

### 4.2 `market_semantics_enricher`

Attaches tags, event families, title templates, negRisk/event relationships, and time fields. Cache fetches, but failed fetches remain unknown; never cache transient failures as "untagged" or "unresolved."

Output: `EnrichedLedger`.

### 4.3 `event_level_position_builder`

Builds:

- directional episodes: buy/sell/redeem lifecycle per condition/outcome/episode;
- event basket inputs: raw event-level legs and inventory flows;
- inventory/reward book: split/merge/rebate/reward flows.

Flat-then-rebuy activity becomes a new episode. Split/merge remains visible even when excluded from directional `(r - p)`. Full net exposure / max exposure basket accounting is Push 2, because SPLIT mints both outcomes and requires a dedicated negRisk fixture.

Output: `PositionBook`.

### 4.4 `edge_position_slicer`

Computes edge surfaces over the `PositionBook`.

Required slice axes:

- parent category;
- platform tag;
- specific tag;
- event family;
- title/template cluster derived from platform structure: event `series` when present, otherwise event/market slug family. Do not start with title-regex matching.
- event slug;
- price regime;
- time-to-resolution at entry;
- market age at entry;
- held vs sold vs open;
- entry hour/day/dead-hour;
- size bucket;
- negRisk/event basket;
- repeat-template occurrence index;
- liquidity/spread tier when available.

Core metrics per slice:

- n positions and n events;
- realized PnL and turnover;
- win rate;
- mean entry price;
- calibration edge `(realized win-rate - mean entry price)`;
- ROI per dollar;
- Wilson/confidence intervals for descriptive readout;
- concentration: top positions/events share of gross winning PnL;
- coverage/fidelity flags.

Output: `EdgeSliceTable`.

The slicer also computes a **within-wallet outcome-shuffle baseline**. For each wallet run, shuffle win/loss outcomes across that wallet's resolved positions within coarse price bands and rerun the same slice family. Report:

- expected green slices under the shuffle;
- observed green slices;
- observed/expected ratio;
- top-slice rank under the null.

This is descriptive calibration, not a kill gate. Its purpose is to stop the top green slice from being mistaken for a real thesis when the slice family itself would produce many green cells by chance.

### 4.5 `edge_cluster_builder`

Turns slices into candidate edge groups. This is the missing center.

Rules:

- A wallet may emit many clusters.
- Clusters can overlap, but overlap must be reported.
- Prefer clusters that are specific enough to explain behavior and broad enough to have real support.
- Do not sum multi-membership tags as a partition.
- Parent category rollups are partitions; platform tags are multi-membership lenses.

Cluster candidates come from:

- high positive PnL slices;
- high positive calibration slices;
- high positive event-level basket PnL;
- high negative slices, because loss centers define where the strategy does *not* work;
- conjunctive cells: tag x price, tag x timing, event-family x mechanism, template x price, category x dead-hour.

Hard cluster guards:

- minimum `n_events >= 5` and `n_positions >= 15` for any edge cluster shown above "descriptive";
- conjunction depth capped at 2 in V1;
- no cluster may be promoted from a single event, no matter how many conditions/legs it contains;
- every cluster carries its slice-layer outcome-shuffle baseline fields.

Each cluster emits:

```json
{
  "cluster_id": "stable deterministic id",
  "label": "UFC held lean-favorites",
  "scope": {"tags": ["UFC"], "price_regime": "lean_fav", "mechanism": "held"},
  "members": {"position_ids": [], "event_ids": []},
  "metrics": {"n_positions": 0, "n_events": 0, "pnl": 0, "edge_pp": 0},
  "negative_control": "nearest losing sibling if available",
  "overlap": [{"cluster_id": "...", "share": 0.0}]
}
```

### 4.6 `method_detector_runner`

Runs method detectors against each cluster, not just against the wallet. This is load-bearing: the whole wallet may look mixed while one cluster has stale-liquidity edge and another has specialist edge.

Output: `MethodFinding[]` per cluster.

### 4.7 `thesis_profile_assembler`

Ranks clusters and method findings into a wallet profile.

Output shape:

```json
{
  "wallet": "0x...",
  "run_id": "...",
  "edge_clusters": [],
  "theses": [],
  "loss_centers": [],
  "blocked_methods": [],
  "data_gaps": [],
  "runnability_summary": {}
}
```

### 4.8 `thesis_validation_spine`

Turns ranked theses into validation jobs. This module is required; otherwise the extractor is only a story generator.

It reuses the existing thesis-card conveyor where possible:

- paper enrollment contract: `docs/superpowers/specs/2026-05-30-forward-paper-enrollment-contract.md`;
- durable storage: `paper_card_enrollments`;
- forward tracking: `docs/superpowers/specs/2026-05-30-forward-tracking-job-d4.md`;
- null calibration gate and replication helper: `sniper.analysis.thesis_cards.batch_runner`.

Validation decision order:

1. **Forward-power gate.** Before enrollment, estimate whether the thesis can produce enough future resolved events inside a fixed horizon. If not, do not pretend single-wallet forward can prove it.
2. **Single-wallet paper.** If powered, enroll a thesis-specific card with its frozen `forward_test_design`.
3. **Cross-wallet replication fallback.** If single-wallet forward is thin, run the same cluster definition across comparable active winners and require replication across wallets before calling it more than a wallet-specific hypothesis.
4. **Null Calibration Gate.** For any board-level claim, compare the real promotion/replication rate against the within-wallet outcome-shuffle Monte Carlo baseline.

Validation output:

```json
{
  "thesis_id": "...",
  "validation_state": "paper_queued | active | promoted | killed | inconclusive | replication_required",
  "forward_power": {"expected_events": 0, "horizon_days": 0, "powered": false},
  "paper_enrollment_id": null,
  "replication": {"state": "not_run", "wallets_tested": 0, "replicated_wallets": 0},
  "null_calibration": {"real_rate": null, "null_rate": null}
}
```

## 5. Method detector catalog

Each detector returns: `method`, `state`, `score`, `effect`, `n`, `evidence`, `controls`, `blockers`, `runnability`, `forward_test_design`.

States:

- `supported_proxy`: current data supports a proxy for the method.
- `supported_direct`: current data directly supports the method.
- `descriptive`: useful description, not enough to score.
- `blocked_missing_feed`: detector cannot run honestly yet.
- `contradicted`: evidence argues against this method for the cluster.

### 5.1 Directly catchable now or with current repo-adjacent data

| Method | What it detects | Signal | Guard |
|---|---|---|---|
| specialist domain edge | edge lives in a narrow semantic niche | positive `(r - p)` by tag/event-family/template | FDR or rank-only within declared axis; min n events |
| recurring template edge | repeated market template where wallet wins | edge by platform series / slug-family template | avoid title-regex brittleness; avoid one event with many conditions |
| favorite/longshot calibration | mispriced price band | market-level win-rate vs entry price by band | market-level weighting, not position/volume weighting; filter near-certainty tautologies |
| settlement-rule edge | wins in rule-complex markets | repeat wins in disputed/ambiguous resolution families | descriptive until resolution metadata is strong |
| reward attribution | separates prediction edge from rewards/rebates | REWARD/YIELD/MAKER_REBATE share of total economics | reward-heavy clusters are not selection theses |
| sizing skill | bets bigger on better expected value | dollar-weighted edge > equal-weighted edge | rare/weak empirically; require positive base edge first and size permutation |
| exit skill | selling/holding adds value | realized exit vs hold-to-resolution counterfactual | descriptive by default until clean exit-time null exists |
| capital velocity | amplifies a real edge by turnover | ROI per dollar x turnover/recycling | meta-method only; velocity without per-bet edge is churn |
| inventory/hedge management | event basket is smart while legs look flat | event-level net PnL and exposure path | event-level unit, not condition-level n |
| negative-risk basket arb | exploits negRisk basket mechanics | split/merge + event basket payoff > cost | include all legs and inventory flows |
| intra-market sum arb | YES/NO or sibling sum inconsistency | simultaneous or near-simultaneous basket cost < payout | event-level proof |
| relationship pricing | related markets mispriced together | buys cheap side of explicit sibling/conditional relation | restrict to explicit/event-linked relations first |
| stale-liquidity sniping | buys stale quotes before reprice | favorable post-entry drift/CLV | defer direct V1 detector until reliable price path/feed exists |
| dead-hour timing | trades when books stale | edge/drift by hour/liquidity dead zone | fixed bucket definitions |
| crowd fade | fades recent burst/overreaction | enters against recent move, then mean reverts | separate from generic contrarian losing |
| momentum/attention arrival | enters with early move before follow-through | same-direction drift and later independent volume | exclude self-volume |
| first-attention discovery | enters before market attention arrives | first-N trader, later independent followers/volume | needs wallet identity for followers |
| market-making spread capture | earns spread/rebates not outcomes | maker fills, low adverse selection, rebates | requires maker/taker or strong proxy |
| adverse-selection-aware making | passive fills that do not get run over | post-fill drift not against maker | compare to maker baseline |
| queue/microstructure edge | gets filled at favorable queue position | maker/taker, fill timing, spread capture | needs fill/order detail |
| operational coverage | wins by monitoring more markets/time zones | fresh-market/dead-hour coverage + broad small edges | report as meta-method |

### 5.2 Catchable as anomaly, not provable as source

| Method | What is catchable | What is not provable yet |
|---|---|---|
| insider / privileged information | pre-resolution entries, pre-news timing anomalies, impossible speed clusters | actual information source |
| public-news latency | entry before/after known public events, if feed exists | without news feed, source attribution |
| external model edge | repeated wins in modelable domains, e.g. sports/specialist templates | the model itself |

### 5.3 External-feed proof blocked

| Method | Blocker |
|---|---|
| cross-venue arbitrage | needs synchronized external venue prices, e.g. Kalshi/sportsbook |
| public-news proof | needs event-timestamped news/odds feed |

Even blocked methods should emit a `GAP` finding when the wallet behavior strongly points there. The analyzer should say "looks like news latency, proof needs feed X," not drop the thesis.

## 6. Cluster-first detector flow

For every wallet:

1. Build position book.
2. Compute all edge slices.
3. Build clusters from positive edge, positive PnL, event-basket, and loss-center slices.
4. For each cluster, run all applicable detectors.
5. Merge detector findings into theses.
6. Rank theses by:
   - economic contribution;
   - statistical support;
   - event count;
   - method specificity;
   - runnability;
   - forward-test readiness.
7. Send forward-testable theses to `thesis_validation_spine`; send thin theses to replication-required or descriptive states.

Do not reject a cluster only because one detector fails. A sports cluster can be specialist + sizing + held-to-resolution. An Iran-regime cluster can be a loss center and still teach where the wallet's thesis breaks.

## 7. Controls and honesty guards

- **No global null gate at intake.** Winning/persistently winning is the premise.
- **No single-wallet copy claim.** A thesis may be real but not replicable by us.
- **No two-leg fake n.** Event baskets are one unit for basket/arb claims.
- **No hardcoded entity taxonomy.** Use platform tags, event families, title templates, and stable parent rollups. Entity-specific terms can appear in outputs because they came from platform metadata, not because the code knows "Iran" or "UFC."
- **No transient failure as truth.** Fetch failures remain unknown.
- **No p-hacking by unlimited subgroup search.** The cluster builder may generate many leads, but output is ranked hypotheses. Forward validation decides truth.
- **Slice false-discovery baseline.** Every run reports the within-wallet outcome-shuffle baseline so the number of green cells is calibrated against what the slicer would find by chance.
- **Forward-power gate.** A thesis that cannot produce enough future events in horizon is not "failed" or "proved"; it is `replication_required` or `inconclusive`.
- **Cross-wallet replication fallback.** Thin single-wallet clusters must replicate across comparable winners before becoming more than a wallet-specific story.
- **Report loss centers.** The wrong slice is part of the thesis profile.
- **Separate selection from execution.** `(r - p)` answers selection/calibration. CLV/spread/rebate/exit/velocity answer other profit sources.
- **Data tier is mandatory.** Every method result says whether it is direct, proxy, descriptive, or blocked.

## 8. Data objects

### 8.1 `PositionEpisode`

Fields:

- `position_id`
- `wallet`
- `condition_id`
- `event_id` / `event_slug`
- `outcome_index`
- `outcome_label`
- `episode_index`
- `entry_ts`, `exit_ts`, `redeem_ts`
- `buy_shares`, `buy_cost`, `entry_vwap`
- `sell_shares`, `sell_proceeds`, `exit_vwap`
- `redeem_payout`
- `resolved`, `won`, `push`
- `realized_pnl`
- semantic fields: tags, category, event_family, title_template

### 8.2 `EventBasket`

Fields:

- `event_id` / `event_slug`
- all condition/outcome legs;
- net cost, net proceeds, redemption payout, rewards/rebates;
- split/merge flow;
- max exposure;
- final PnL;
- event-level win/loss/push where meaningful.

### 8.3 `EdgeSlice`

Fields:

- `slice_key`
- `axis`
- `scope`
- `n_positions`
- `n_events`
- `pnl`
- `turnover`
- `win_rate`
- `mean_entry_price`
- `edge_pp`
- `roi_pct`
- `ci_pp`
- `concentration`
- `data_fidelity`

### 8.4 `EdgeCluster`

Fields listed in §4.5.

### 8.5 `MethodFinding`

Fields listed in §5.

### 8.6 `Thesis`

Fields:

- `thesis_id`
- `cluster_id`
- `headline`
- `edge_group`
- `primary_method`
- `secondary_methods`
- `evidence`
- `runnability`
- `blocked_data`
- `forward_test_design`
- `confidence`

### 8.7 `ValidationPlan`

Fields:

- `thesis_id`
- `validation_metric`
- `forward_power`
- `single_wallet_horizon`
- `replication_cohort_definition`
- `null_calibration_design`
- `paper_enrollment_payload`
- `success_criteria`
- `kill_criteria`

## 9. V1 build scope

V1 should not try to build every detector. It should build the foundation and enough detectors to prove the architecture.

### Build now

1. Promote `scripts/wallet_anatomy.py` prototype logic into tested modules.
2. Build `wallet_event_ledger`.
3. Build `market_semantics_enricher`.
4. Build `event_level_position_builder`.
5. Build `edge_position_slicer`.
6. Build `edge_cluster_builder`.
7. Build first detector pack:
   - specialist domain;
   - recurring template from platform series / slug family;
   - favorite/longshot calibration with market-level weighting;
   - reward attribution;
   - sizing skill as lower-priority / rare-signal detector;
   - exit/hold mechanism as descriptive unless a clean null exists;
   - capital velocity as an amplifier/meta-method.
8. Build `thesis_profile_assembler`.
9. Build `thesis_validation_spine` adapters to existing paper/forward/replication components.
10. Add CLI: `scripts/run_winning_wallet_thesis.py <wallet> --cached/--refresh --json`.

### Push 2 / deferred from V1

- event-basket / negRisk inventory accounting with full split/merge exposure tracking and a dedicated fixture;
- stale-liquidity / CLV detector requiring reliable per-fill price path;
- direct market-making, queue, and adverse-selection detectors requiring maker/taker or orderbook data.

### Leave as explicit gap or proxy

- news/info source proof;
- cross-venue arb proof;
- true order-book spread capture where historic book is missing;
- true first-attention follower flow until identity-bearing fill feed exists.

## 10. Testing strategy

Unit tests:

- failed fetches remain unknown;
- flat-then-rebuy splits into episodes;
- redemptions mark held episodes;
- split/merge flows remain visible as event-basket inputs;
- platform tags drive categories without entity hardcoding;
- title templates normalize recurring markets;
- edge slices compute `(r - p)` and PnL correctly;
- favorite/longshot calibration is market-level weighted;
- within-wallet outcome-shuffle baseline reports expected vs observed green slices;
- cluster builder emits multiple overlapping clusters and reports overlap;
- cluster builder enforces `n_events`, `n_positions`, and conjunction-depth guards;
- loss centers are emitted;
- method detectors return direct/proxy/blocked states honestly.
- validation spine marks underpowered single-wallet theses `replication_required`, not promoted.

Fixture tests:

- SwissMiss cached activity produces multiple clusters: sports positives, geopolitical loss center, held mechanism, and platform-tag coverage.
- A synthetic re-entry wallet proves episode accounting.
- A synthetic many-slice wallet proves the outcome-shuffle baseline prevents over-reading one green cell.

Push 2 fixture tests:

- A synthetic negRisk basket proves event-level accounting.
- A synthetic stale-liquidity path proves CLV proxy detector.

No live API tests in CI. Live runs remain operator-triggered and cached.

## 11. Output example

```json
{
  "wallet": "0x...",
  "headline": "multiple edge groups; sports held-to-resolution positive, Iran-regime loss center",
  "theses": [
    {
      "headline": "UFC held-to-resolution specialist cluster",
      "edge_group": {"tag": "UFC", "mechanism": "held"},
      "primary_method": "specialist_domain_edge",
      "secondary_methods": ["favorite_longshot_calibration", "capital_velocity"],
      "runnability": "forward_testable",
      "blocked_data": []
    },
    {
      "headline": "Middle East / Iran-regime loss center",
      "edge_group": {"tags": ["Middle East", "Iran", "Khamenei"]},
      "primary_method": "loss_center",
      "runnability": "avoid_or_counter-thesis_only",
      "blocked_data": []
    }
  ]
}
```

## 12. Implementation order

1. Land the current anatomy bugfixes and tests, or carry them into this branch as the foundation.
2. Extract data objects into `sniper/analysis/winning_wallet_thesis/types.py`.
3. Extract ledger/enrichment/position-builder modules.
4. Port anatomy report calculations into `edge_position_slicer`.
5. Build `edge_cluster_builder`.
6. Build first detector pack.
7. Build thesis profile assembler.
8. Wire validation spine to existing enrollment / forward tracking / replication components.
9. Add CLI.
10. Run cached SwissMiss as the first worked example.
11. Only after this works, run other active winning wallets.

## 13. Non-goals

- No live trade execution.
- No copy bot framing.
- No deployable trade triggers.
- No expected PnL promises.
- No broad random-wallet mining as the primary path.
- No Becker-only conclusion about active winners.
- No implementation on dirty `main`; implementation starts on a feature branch/worktree after this spec is approved.

## 14. Approaches considered

- **A. Keep expanding `scripts/wallet_anatomy.py`.** Fastest short-term path, rejected as the primary architecture because the script would mix ingestion, accounting, slicing, clustering, detectors, and reporting.
- **B. Promote anatomy into modules and build cluster-first thesis extraction.** Chosen. It preserves the useful tag-driven prototype while creating testable boundaries.
- **C. Wait for perfect feeds before building.** Rejected. Most methods are catchable now as direct signals or honest proxies. Only news/source proof and cross-venue proof are feed-blocked.

## 15. Revision history

- **rev 2 (2026-05-31):** applied review fixes: added `thesis_validation_spine`, forward-power gate, cross-wallet replication fallback, Null Calibration Gate reuse, slice-layer outcome-shuffle baseline, cluster min-events/min-positions/conjunction-depth guards, V1 curated intake, market-level favorite/longshot weighting, reward attribution, exit/sizing empirical caveats, capital velocity as meta-method, template clustering from series/slug structure, and deferred stale-liquidity/event-basket heavy work out of the first push.
- **rev 1 (2026-05-31):** initial design after doctrine correction: active winning wallet first, multi-edge clusters, method detectors per cluster, Becker demoted to validation/null harness.
