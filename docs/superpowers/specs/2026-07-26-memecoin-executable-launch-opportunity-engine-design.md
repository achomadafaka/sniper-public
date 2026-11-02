# Memecoin Executable Launch Opportunity Engine — design

**Date:** 2026-07-26
**Status:** approved design
**Scope:** capture-first research for Pump.fun/Solana, Bags/Solana, and Bags/Robinhood 4663
**Primary clip:** $25; $5/$10/$50 are capacity diagnostics
**Authority:** default OFF, no funded authority, no automatic paper entries

## 1. Goal

Replace wallet-centric Coin Radar with an event-driven launch detector that estimates whether a
point-in-time $25 entry has enough executable upside and exit runway to deserve attention.

The analytical core is the **Executable Launch Opportunity Engine**. Radar is its visualization.

> Predict whether a point-in-time $25 paper entry can reach a net executable profit before a net
> executable loss, persistent exit failure, or lifecycle migration, with enough pessimistic time to
> enter and exit.

This is exploratory. It should point in a better direction before formal edge proof exists. Luck and
market regime remain material. Dry periods with no actionable launches are valid. Strictness applies
to truth, point-in-time reconstruction, executability, identity, and missing data—not to requiring
every week or OOS block to win.

## 2. Boundaries

The engine owns launch discovery, event decoding, venue state, landmarks, hypothetical $25 entry,
executable outcomes, and `ENTER_CANDIDATE`/`ABSTAIN` research output.

It does not own funded reservations/execution, position sizing, exits, V6 policy, X signals,
wallet-follow decisions, or LLM decisions. It has no funded credentials, custody interfaces, claims,
signing, or broadcast calls and does not read/write funded position tables.

V6 remains the only candidate exit-engine lineage. V0 opens no V6 position. After separate OOS review
and explicit approval, an immutable accepted signal plus realistic quote may become an upstream V6
live-paper fill; V6 then owns the mock position until close.

Wallet evidence is optional buyer-quality context and cannot independently set distance or fire. X is
absent from state, labels, models, and the decision path.

The system may paper-model existing momentum while exitability remains adequate. It must never create
deceptive activity, coordinate dumping, manipulate price, or describe those acts as features.

## 3. Architecture

```text
chain-native streams
  -> append-only raw event ledger
  -> venue-specific decoders
  -> canonical launch events
  -> venue-specific point-in-time state
  -> preregistered landmarks
  -> self-impact + executability evaluator
  -> opportunity model
  -> capture verdict / later approved paper signal
```

Isolated venue adapters/state machines:

```text
pumpfun_solana
bags_solana
bags_robinhood_4663
```

They share canonical schema, completeness semantics, timestamps, outcome codes, and evaluation code.
They do not share fitted probabilities, calibration, or venue constants. V0 ends at migration;
post-migration AMM behavior requires a separate model and execution contract.

Core interfaces:

```text
LaunchEventSource
LaunchDecoder
CurveStateReducer
EntityEvidenceProvider
OpportunityQuoteProvider
LandmarkEmitter
OpportunityEvaluator
```

`OpportunityQuoteProvider` performs local curve math and read-only RPC quote/simulation calls. It
never constructs a submission transaction, signs, or broadcasts.

## 4. Canonical event truth

Every event preserves network/venue/token/lifecycle, native chain ordering, transaction identity,
event index, chain timestamp, source receipt, decode and state timestamps, provider, confirmation,
raw-payload hash, and decoder version.

Fast observations may be tentative. Solana processed observations reconcile at confirmed/finalized.
Robinhood sequencer/provider observations reconcile against canonical RPC state. Reorgs and failed
confirmations append correction records; raw facts are never silently rewritten.

These states block prediction while remaining visible:

```text
EVENT_GAP
SOURCE_CONFLICT
UNKNOWN_PROGRAM_OR_CONTRACT_VERSION
DECODER_FAILURE
STALE_STATE
UNRESOLVED_LIFECYCLE
MISSING_TOKEN_OR_CREATOR_IDENTITY
UNSUPPORTED_CURVE_MATH
UNRECONCILED_REORG
```

A failure blocks only the affected venue/frontier.

## 5. Source lanes

### Solana

Use the Pump.fun program subscription directly. Meteora DBC is shared by unrelated launch
partners, and current Anchor events are emitted as event-CPI inner instructions rather than
`Program data:` log lines. For Bags, maintain feed-discovered pool subscriptions, fetch only the
notified transaction through the shared RPC governor, and decode exact DBC event-CPI payloads whose
pool and mint match the Bags receipt:

Pump uses a separate WebSocket connection. Bags pools are deterministically split across four
connections, keeping each well below provider subscription pressure while preserving stable pool
ownership as the 100-row feed rotates.

```text
processed log
-> persist envelope
-> fetch/decode transaction
-> tentative event/state
-> confirmed verification
-> finalized reconciliation
```

Ordering uses slot, transaction position when available, instruction index, and inner-instruction
index. Pump recovery paginates `getSignaturesForAddress` per program. Bags launch recovery uses the
feed's exact launch signature and accepts only a matching chain transaction; it is recorded as
recovery and cannot create low-latency actionable state. A page/budget bound creates `GAP`; the
cursor does not advance past incomplete history. Late recovery repairs research but never
fabricates a low-latency signal.

### Robinhood 4663

Benchmark provider WebSocket logs, public sequencer feed, and batched `eth_getLogs`. WebSocket is the
canonical-trigger candidate, sequencer is a low-latency candidate/wake lane, and `eth_getLogs` is
recovery/reconciliation. Ordering uses block number, transaction index, log index, and block hash.

Capture Bags creation, curve buys/sells, creator/fee-share changes, migration, and the V4 transition
identity needed to end pre-migration v0 honestly.

Before canonical state persistence, each 4663 event reads `BagsLens.getTokenState(token)` at the
event's exact block. That immutable receipt supplies curve identity, real/virtual reserves, price,
progress, migration, fee-share, and pool identity. A failed or conflicting historical read opens a
durable source gap; it never falls back to latest state or zero progress.

### Slow enrichment

Bags API, DexScreener, and GeckoTerminal are enrichment/recovery only. The Bags feed may discover
the exact pool and launch signature needed to scope the shared DBC program, but only chain
notifications and verified chain transactions create event facts. Recovery cannot retroactively
create an opportunity.

## 6. Dedicated storage and worker

Use isolated SQLite `data/memecoin_opportunity.db` with one WAL writer and these logical tables:

```text
op_raw_events
op_canonical_events
op_launches
op_source_frontiers
op_data_gaps
op_landmark_states
op_entity_edges
op_quote_observations
op_counterfactual_entries
op_competing_outcomes
op_predictions
op_model_registry
```

Raw events/corrections are append-only. Schema, decoders, curve math, features, labels, and models are
versioned. Corrected real UTC and native chain ordering are both stored. The DB has no funded
credentials, positions, reservations, claims, signatures, or payloads.

Dedicated worker/watchdog is default OFF:

```text
MEMECOIN_OPPORTUNITY_CAPTURE=0
```

When explicitly enabled it uses an atomic singleton lock, explicit DB path, startup validation,
corrected clock, health heartbeat, bounded queues, and durable frontier reconstruction. Falling behind
creates a gap and disables directional claims for that interval; events are never silently dropped.

## 7. Landmarks and eligibility

Capture every event. Permit decisions only at:

```text
time:     1s, 2s, 5s, 10s, 20s, 30s, 60s after launch
progress: 2%, 5%, 10%, 20%, 40%, 60%, 80%
```

V0 has no velocity-shock entry landmarks. Creator sales, route degradation, gaps, and migration are
immediate risk interrupts, never entry triggers. If one event crosses several landmarks, persist one
state row with multiple tags.

Observability order is chain event, source receipt, decode, state apply, then decision eligibility.
Hypothetical entry begins after eligibility and cannot use the pre-trigger price.

Eligibility requires a supported version, known token/creator identity, pre-migration lifecycle
resolved from the supported program/contract, complete observed frontier, valid curve state,
deterministic $25 self-impact math, fresh exit-route state, and no known reorg/gap. A processed or
sequencer observation may therefore be eligible while explicitly marked tentative. Final labels use
canonical state; an orphaned tentative landmark is censored as `CHAIN_REORG`. Ineligible landmarks
remain with reason codes.

There is no hard buyer-count or additional progress minimum. Early states may `ABSTAIN_UNCERTAIN`.

## 8. Point-in-time state

Each landmark freezes:

1. **Identity/lifecycle:** chain, launchpad, version, token, creator/fee-share parties, age, progress,
   migration distance, authority/extensions, route state.
2. **Curve kinetics:** real/virtual reserves, capital raised, time/trades to prior progress, conditioned
   capital/progress velocity and acceleration, capital per trade, buy/sell/net flow.
3. **Effective demand:** unique signers, effective independent buyers, new-buyer velocity, entropy,
   HHI, top-1/5/10 ownership, repeat/round-trip share, retention, bundle/funder evidence.
4. **Creator/insider:** linked allocation, buys/sells/transfers, fee-share participants, fresh-wallet
   transfers, and history known strictly before the landmark.
5. **Executability:** local $5/$10/$25/$50 self-impact, immediate reverse proceeds, costs, taxes, and
   remaining capacity. $25 is primary.
6. **Quality:** completeness, source health, confirmation, age, versions, clock domain, latency, and
   local-vs-remote quote disagreement.

Routers never count as buyers. Entity evidence is exact, strong, or weak. Weak timing/size patterns
remain features and never silently establish shared control. Missing values stay missing with reasons.

Remote exact quote/transaction simulation occurs only after loose inner-ring eligibility; latency and
local disagreement are recorded.

V0 persists interval duration, capital velocity/acceleration, buyer-breadth velocity/acceleration,
sell-pressure share, HHI change, and creator-sale change in the immutable state snapshot. Acceleration
uses only the current and prior completed landmark intervals. Missing denominators stay missing.

## 9. Counterfactual entry

At each eligible landmark, insert one hypothetical $25 buy after observability. Primary latency is the
venue p90 detection-to-entry-inclusion distribution; p50/p99 are sensitivity reads.

The entry pays impact, protocol/network/priority fees, and known taxes; changes reserves; and produces
an exact acquired quantity. Subsequent observed trades replay against adjusted state. The replay
assumes later traders still submit the same inputs. This non-interference limitation is explicit.

## 10. Executable outcomes

From modeled entry to 60 seconds later:

```text
PROFIT_FIRST
LOSS_FIRST
PERSISTENT_EXIT_FAILURE_FIRST
MIGRATION_FIRST
TIMEOUT
```

Primary net-executable barriers are +12% profit and -8% loss. Secondary diagnostics are +8%/+20%,
30s/120s, p50/p99 latency, and $5/$10/$50. They cannot replace the primary result after observation.

```text
R_net(t) = executable proceeds for acquired quantity
           - $25 entry spend
           - remaining entry/exit/network costs

return_pct(t) = R_net(t) / $25
```

No midpoint, market cap, nominal price, untouched-pool quote, or chart mark labels success.

Deterministic sell reverts, restrictions, removed reserves, sale disablement, or all routes being
deterministically invalid are economic exit failures. RPC timeout, rate limit, stale response, one
missing quote, decoder lag, or disconnect is `DATA_FAILURE`. Non-deterministic failures become
persistent only after fresh failures across two canonical states with healthy sources. A route below
-8% is `LOSS_FIRST`, not exit failure.

If migration wins, record `MIGRATION_FIRST`, force-value exit at the last executable pre-migration
state, and do not follow into the AMM. Migration may be profitable, flat, or losing.

`DATA_GAP`, `SOURCE_FAILURE`, `DECODER_FAILURE`, `UNSUPPORTED_VERSION`, and `NET_PNL_INCOMPLETE`
censor the landmark and remain in completeness denominators.

## 11. Utility and future fire gate

Models may emit competing probabilities, outcome-conditional net returns, and time distributions.

```text
U(s) = sum_k P(k | s) * E[net_return | k, s]
```

Costs are already net and are not subtracted twice. Capture-only v0 records `WOULD_FIRE`.

A later explicitly approved paper entry requires positive lower-bound utility, pessimistic safe-window
greater than measured joint p99 round-trip latency plus frozen buffer, bounded persistent-exit-failure
risk, fresh $25 entry quote, fresh acquired-quantity sell simulation, and a complete frontier. No
measured latency distribution means no paper firing.

## 12. Tiered evidence

### Level 0 — Observing

Immediate capture state, no directional claim.

### Level 1 — Directional Radar

No sample floor and no weighted score. Zones follow transparent rules:

```text
TRACKING:
  landmark is eligible

APPROACHING:
  current interval capital velocity > prior interval
  and effective-buyer arrival velocity > prior interval
  and a local $25 exit route exists

INNER:
  APPROACHING
  and effective-buyer HHI did not worsen
  and no creator-linked sale occurred since the prior landmark
  and immediate $25 round-trip return is above -8%

CENTER / WOULD_FIRE:
  INNER
  and a current-state remote $25 entry quote succeeds
  and the acquired quantity has a current-state sell simulation
```

Missing prior-interval state prevents `APPROACHING`; it is not synthesized. Creator selling, rising
concentration, round-trip/bundle dominance, route degradation, shrinking runway, or quality loss moves
a dot outward. Permanently label Level 1 `EXPLORATORY — EDGE NOT YET ESTABLISHED`. Later calibrated
models may replace these zone rules under a new version; they never rewrite the Level-1 history.

### Level 2 — Experimental model

About 100 complete launches, 20 `PROFIT_FIRST` examples, and multiple calendar periods are soft review
checkpoints, not blockers. Show utility, dominant risk, rank, comparable-state rate, sample size, and
`WATCH`/`APPROACHING`/`EXPERIMENTAL_CANDIDATE`/`ABSTAIN`. No edge claim or position.

### Level 3 — Forward-paper candidate

Practical evidence: positive chronological average executable value, inner dots beat outer dots,
results are not two lucky launches/one creator, $25 survives costs, and route/latency rails work. No
requirement that every week/block wins. Promotion requires explicit approval.

### Level 4 — Edge supported

Stronger confidence, cluster controls, larger samples, and forward paper are later funded-consideration
standards, not prerequisites for learning from Radar.

## 13. Models and evaluation

Separate per venue/lifecycle:

- M0: age, progress, migration distance.
- M1: M0 plus curve kinetics.
- M2: M1 plus entity-adjusted demand.
- M3: M2 plus creator/integrity, self-impact, route quality, exit risk, latency.

Candidate forms are regularized discrete-time competing-risk logistic regression, regularized GAM,
and small gradient boosting. Prefer the simplest model that wins chronologically; M3 is not automatic.

The independent unit is one launch. Resampling, intervals, attribution, and validation cluster by
launch. Lead with executable value per eligible launch/`WOULD_FIRE`, near-center vs outer monotonicity,
top-1/3/5 precision at capacity, false-entry rate, exit-failure rate, latency violations, sample size,
and opportunity rate. PR AUC, Brier, log loss, and calibration are diagnostics; ROC AUC is secondary.

Baselines are M0, raw momentum, raw volume, raw capital velocity, wallet-buy trigger, current Radar,
activity-matched placebo, and always-abstain. There is no minimum daily/weekly signal count and no
forced best coin. Passing $5 while failing $25 is not primary success. Seen holdout data is never tuned;
a retry requires a new version, new forward data, and a new sealed holdout.

## 14. Event-driven runtime and Radar

Decision flow is event receipt -> decode/state -> local curve evaluation -> optional inner-ring remote
quote. There is no 4/60/300-second decision poll. Record receipt/decode/state/local/quote/recovery
p50/p95/p99 and optimize only measured bottlenecks.

Radar center means a currently executable $25 opportunity with positive directional utility and enough
runway to exit.

```text
OUTER       newly detected
TRACKING    clean state, direction forming
APPROACHING kinetics/execution favorable
INNER       experimental candidate
CENTER      WOULD_FIRE capture counterfactual
```

V0 sends no order. `PAPER_FIRE` requires later approval. `FUNDED_FIRE` does not exist here.

Dot encoding:

```text
distance    exploratory/action-value rank
velocity    change in actionability
angle       chain x launchpad x lifecycle
size        executable capacity
color       dominant competing risk
halo        data quality/confidence
trail       recent movement
```

Green means profit, red loss/dump, purple exit failure, blue migration, gray incomplete/stale/conflict;
text/icons accompany color. Level 1 distance is transparent evidence; later distance is a monotonic
transform of utility. The mode is always visible.

UI uses initial snapshot -> WebSocket/SSE deltas -> local animation -> snapshot reconciliation. The
sweep arm is visual only. Disconnect freezes movement, fades dots gray, shows age, and marks `STALE`.

Show venue sectors, zones/trails, closest opportunities, inward movers, new launches, outward/risk
movers, source health, and honest `No current opportunities near the center` state. Dot drawer explains
landmark, kinetics, effective buyers, creator behavior, $25 self-impact, immediate round trip, route,
runway, risk, completeness, source health, and model/rule version with exact facts—not LLM prose.

The same surface shows launch-clustered insight receipts: captured/matured launches per venue,
competing outcomes, executable-return spread, and transparent pattern cohorts versus their own venue
baseline. One selected state per launch prevents transaction-rich launches from dominating. These are
early directional reads with visible sample counts, not promotion gates. If at least 20 executable
observations occupy a 10 bps-or-smaller range, the evaluator is labeled
`DEGENERATE — NO PRICE INFORMATION` and pattern ranks are suppressed.

Truth labels are `EXPLORATORY`, `EXPERIMENTAL`, `FORWARD-PAPER CANDIDATE`, `EDGE SUPPORTED`, `STALE`,
and `DATA INCOMPLETE`. Outcome panels show sample size, `WOULD_FIRE` count, executable hit rate, average
net outcome, recent/all history, and quote coverage. No expected-PnL headline lacks status/sample size.

Replace wallet consensus as primary Radar. Preserve useful universe/search separately; wallet ratings
become optional context.

## 15. Verification and delivery

Fixture tests cover venue decoding; ordering/dedupe; reorgs; pagination gaps; restart; point-in-time
landmarks; $25 self-impact; competing first-passage labels; data vs economic failure; migration exit;
source conflicts; default OFF; forbidden funded imports/writes; and UI stale/evidence behavior.

Evaluation tests enforce one observable state per landmark, launch-clustered resampling, chronological
splits, sealed-holdout immutability, and no future timestamps.

Delivery order:

1. Freeze schema, decoders, landmarks, point-in-time rules, quote math, and labels.
2. Build isolated store and canonical event primitives.
3. Build three venue adapters independently.
4. Build reducers, gaps/completeness, and entity evidence.
5. Build landmarks and local clip simulations.
6. Build counterfactual entries, outcomes, and exploratory Radar state.
7. Audit completeness and local/remote quote fidelity.
8. Add M0-M3 only when useful data exists.
9. Build event-pushed UI after state truth is trustworthy.
10. Consider forward paper only after explicit review.

V0 acceptance requires deterministic restart/frontiers, explicit blockers for unsupported venue data,
one point-in-time state per landmark, fixture-verified venue $25 math, an outcome/censor for every
eligible landmark, fact-explainable exploratory movement, immediate stale/gap blocking, structural
absence of funded authority, event-pushed UI, honest dry-market state, and no X/wallet-primary trigger.

## 16. References

- Robinhood endpoints: <https://docs.robinhood.com/chain/connecting/>
- Solana `logsSubscribe`: <https://solana.com/docs/rpc/websocket/logssubscribe>
- Bags launch feed: <https://docs.bags.fm/api-reference/get-token-launch-feed>
- Bags rate limits: <https://docs.bags.fm/principles/rate-limits>
- Bags Robinhood contracts/state: <https://docs.bags.fm/robinhood/contracts> and
  <https://docs.bags.fm/robinhood/read-state>
- Landmark competing risks: <https://pubmed.ncbi.nlm.nih.gov/23086627/>
- Pump.fun study: <https://arxiv.org/pdf/2602.14860>
- MemeTrans: <https://arxiv.org/abs/2602.13480>
- Coordinated cohort study: <https://arxiv.org/abs/2607.02795>
