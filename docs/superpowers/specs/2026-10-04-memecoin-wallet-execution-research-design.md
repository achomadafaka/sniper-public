# Per-wallet memecoin execution research

Date: 2026-10-04. Protocol: `wallet-execution-research-v1`.

Status: operator approved the design and implementation plan in advance on 2026-10-04.
Protective implementation passed independent review and is live on pinned runner,
web and research release `24c360cc1b175ae3c73428b3146bbc1346d35b04`. No wallet
strategy is proven. Complete venue paths, statistical validation and authenticated
study-publication receipts remain unavailable. See the dated rollout audit.

## Purpose and scope

2026-10-06 amendment: the existing replay accepts quantity-specific `entry_quotes`
on the same ordered state/availability frame. The approved fixed-size Solana
diagnostic and its limits are in
[`2026-10-06-solana-sizing-shadow.md`](../../research/2026-10-06-solana-sizing-shadow.md).
Registered `outcome_tail_s` extends local archive windows and delays the fixed test
read until the last cohort entry's hold/settlement tail ends. Registration does not
supply the missing executable pricing, prior context or provenance evidence.

Answer, separately for each wallet and chain: can we enter close enough to its actual
buy, take an achievable part of the move, and exit efficiently? Compare immediate
entry with a causal dip entry, then compare TP, SL, trailing, source-mirror and timed
exits. Learn which coin conditions support each approach. A wallet may need different
policies on different chains and may have no supported policy yet.

The operator describes holds from three seconds to more than ten minutes. A price
one minute later, a horizon average, or a retrospective candle high cannot establish
the outcome of such a trade. Same-slot execution is a target where technically
possible, never an assumed property of a paper fill.

This applies to hypothetical **memecoin trade-performance conclusions**: wallet
rankings, entry/exit comparisons, filter recommendations and arming evidence. It
does not invalidate independently proven transaction failures, safety bugs, latency
measurements or non-performance wallet relationships. Polymarket and sports remain
separate. Radar remains archived.

## Edge cases and limits before any build

- Missing seconds, censored horizons, unavailable routes, unknown gas and source
  identity remain unknown. A successful empty response is not a measured zero.
- A one-second OHLC bar containing both TP and SL does not reveal their order. A
  bar's close is available only after the bar ends. Ambiguous outcomes have bounds,
  not a chosen profitable path. Ordering-dependent policies need ordered events.
- Source time, provider availability, our receipt time and chain inclusion are
  different clocks. Seconds, milliseconds, slots, blocks and transaction indices
  retain their units. Clock regressions and reorganizations invalidate affected
  evidence; sorting wall-clock timestamps does not repair it.
- Repeated buys, airdrops, transfers, identity changes, scale-ins, partial exits,
  failed buys, failed sells and unresolved transactions must have explicit states.
- Small samples, shared coins, herd relationships and the search over variants
  cannot create independent evidence or an out-of-sample winner.
- No new synchronous request, lock, retry, import or write on a funded entry or
  exit path. Research owns its process, connections, state and capacity. It must
  not borrow custody credentials or exhaust a shared IP/provider quota.
- Resume cursors, immutable manifests and idempotent output identities must survive
  a crash halfway through an episode or publication. A rollback stops the new
  worker and quarantines its outputs; it never re-enables legacy verdicts.

Not covered: reconstructing a path that was never recorded; proving sellability
from a chart; guarantees of same-slot inclusion; guaranteed profitability; or
automatic changes to funded sizing, filters, arming, exit rules or runtime.

## What exists and what is missing

Current source inspection and a bounded read-only sample are preserved in
`docs/audits/memecoin-wallet-research-20261004/snapshot.json`. The sample is local
ledger evidence, not a full runtime audit or a representative performance study.

| Existing component | Reuse | Limitation for this question |
|---|---|---|
| `sniper/services/memecoin_entry_capture.py` and its feature store | Entry features, typed coverage, provenance, decision-time windows | Later reconstruction is not proof a feature was available when we could trade |
| `sniper/services/memecoin_wallet_map.py` | Verified address relationships and wallet context | Funds, followers or network position do not establish a profitable exit policy |
| `sniper/services/memecoin_paper_report.py` | Frozen cohorts and exact-origin comparisons | Existing eligibility is not proof of a complete one-second execution path |
| `scripts/memecoin_promotion_lane_loop.py:exit_series` | Admission diagnostics | Uses a one-minute price ledger; cannot establish fast-trade outcomes |
| `sniper/services/memecoin_paper_path.py` | Historical descriptive paths | Stores chart mids, thins later observations; sampled gaps p50 30.00s, p90 65.42s |
| `sniper/services/memecoin_capture_v2.py` | Versioned observation identity and coverage handling | Default sweeps are 30s; endpoint marks do not reconstruct first-touch order |
| `sniper/research/memecoin_strategy/exit_search.py` | Pure model, immutable search receipts and chronological split structure | Holds are 3,600–86,400s; current data gate accepts minute paths; empty complete paths can produce a valid flat timecap result |
| Funded route series | Calibration against actual entry/exit execution | Conditional on trades we took; stops with our lifecycle; alternative sizes and later exits need new evidence |
| Refusal and dip capture stores | Typed route failures, exact quantities, distinct dip identity | Latest 300 refusal rows declared 25s cadence; dip default is 2s, so neither alone meets this protocol |
| EVM swap tape and Solana pool tape | Ordered pool activity where coverage is proven | Retention, pool coverage, source timing and complete pricing state must be audited per episode |

The roster snapshot has 45 armed wallets and 134 armed wallet-chain cells: Solana
25, BSC 26, Robinhood 29, Base 26 and Ink 28. There are 52 active watch wallets;
watch status alone is not an arming recommendation. Freeze exact identities and
chain authority in each study receipt. Add explicitly considered candidates with
a recorded nomination reason. New or changed roster members start a new manifest;
never silently change an existing study's denominator.

Sampled distinct funded exit-quote gaps, in insertion order, were p50/p90:
Solana 0.994/1.039s (six positions), BSC 1.143/1.268s (three), Robinhood
1.171/1.548s (three). These small conditional samples prove neither complete paths
nor all-chain coverage. No whole-cohort latency improvement is claimed here.

## Choice of approach

1. **Recommended: reuse the collectors, add a shared path-evidence contract and
   one eligibility gate.** Wallet studies share a coin's raw tape but retain
   separate signal, receipt, entry, size and policy identities. This avoids one
   provider request per wallet per second and makes output eligibility consistent.
2. Rewrite the entire paper engine. More isolation, but duplicates collectors,
   pricing and coverage rules; unnecessary before the shared contract is tested.
3. Patch each report separately. Faster initially, but leaves other publishers
   able to emit the same unsupported conclusions. Reject this as the final design.

## Evidence contract

Every episode has an immutable manifest and digested raw inputs. Identify chain,
token, pool/venue, verified source address, source transaction and buy episode;
keep the platform user ID and current display name separate from the address.
An alleged buy must show a paid swap and a token acquisition by the attributed
wallet. Transfers, mints and airdrops are not buys. Unproven identity cannot support
an address-specific trading conclusion.

The manifest records source ordering, chain time and its trust, source detection,
provider publication/availability, receipt, feature availability, decision,
quote request/response, simulated submission/inclusion, actual calibration fills,
policy version, source code revision and raw input digests. Use corrected UTC for
cross-process time and monotonic durations within a process. Preserve chain order
and ingestion sequence separately from wall-clock time.

### Path resolution and completeness

Use ordered trades and relevant pool-state changes, with one-second or finer
derived bars. Maximum bar width is one second. Bars are a display and summary
surface; retain the events needed to resolve within-bar order. A chart labeled
`1s` over sparse marks does not qualify.

Every second from the actionable signal through the candidate's entry wait, full
hold and exit settlement must be covered by a proven source frontier or recorded
failure. A quiet second may carry unchanged state only when gap-free collection
proves no relevant change; do not interpolate across missing observations or
assume no swaps means fees, transfer rules or pricing state stayed unchanged.

With sampled prices or quotes alone, an unobserved interval over one second makes
the affected episode ineligible for a precise fast-trade outcome. Even denser
samples cannot resolve an ordering-sensitive event that fell between them.
Bounds can be reported as such, without promoting a point estimate.

Capture at least 120s of pre-signal context where available. The initial proposed
post-signal window is at least 30 minutes and, for a registered longer hold,
`entry_wait + max_hold + measured exit-settlement margin`. The longer requirement
wins. Continue independently after our actual exit; otherwise later policies are
censored by our own behavior. Record retention and budget cuts explicitly.

Historical one-second tape may support a retrospective model, but does not prove
the live detector or provider delivered that information in time. Forward replay
must use what was actually available at each decision.

### Executable prices and quantities

For each modeled entry retain exact spend, input asset, decimals, acquired atomic
quantity, route, pinned state, quote age, expected output, minimum output and all
known fees. Store atomic amounts as decimal text. A USD conversion needs its own
timestamped evidence. FDV and circulating market cap are separate quantities.

Exits price the remaining acquired quantity on a validated route at the achievable
execution time. Include gas/priority costs, transfer taxes, price impact, slippage,
approval requirements and submission/landing delay. Unknown costs are not zero.
A quote floor is not a confirmed fill; a model result remains labeled modeled.

Prefer pure pricing against complete, validated historical pool state where that
state reproduces independent route quotes. V2 reserves alone do not prove taxes
or sell restrictions; V3 needs liquidity/ticks/fee state; V4 hooks and Solana curves
need their relevant state and fee behavior. Unsupported venues fail eligibility.
Block-end state cannot price an earlier transaction in that same block.

Record transport timeout, rate limit, ban/auth rejection, explicit route rejection,
partial data, wrong identity, malformed data, token unsellability and unknown
submission separately. A demonstrated unsellable token is an adverse outcome,
not a conveniently removed winner-selection row. A provider outage is unknown,
not proof of token unsellability. Preserve the funnel and conservative sensitivity.

## Entry policies

Compare a small frozen set on the same source episodes:

- Current policy as a calibration control, with the policy actually effective
  for that wallet-chain and date, plus an explicitly frozen forward baseline.
- Immediate entry: act at the earliest evidenced receipt/decision, using a
  chain/lane-specific execution delay calibrated on earlier real attempts.
- Dip entry: after the actionable signal, wait up to a registered deadline for
  the **first available executable** price satisfying a predeclared premium/dip
  condition relative to the source's verified average fill. Freeze the trigger,
  expiry, repeat-buy handling and maximum entries before outcomes arrive.

Never select the eventual trough, reuse a later source buy as an earlier signal,
or wait indefinitely until a successful entry appears. An unmet dip trigger is
`not_entered`, with time and opportunity cost retained in the denominator.

The model uses contemporaneously achievable timing. An optimized future lane can
be a separate feasibility scenario; its assumptions cannot masquerade as current
copy performance. Record and stress actual entry/exit delay distributions, queue
and provider pressure, failure rates and fill drift. Estimate them on training
data only, including failed attempts, by chain and compatible lane/venue.

Same-slot/block labels require source and our transaction order plus evidence the
signal was available before our modeled submission cutoff. A source observed only
after its block is sealed cannot justify a same-block fill. A one-second timestamp
cannot establish source-before-copy order. Unknown order stays unknown.

## Exit policies and trade state

Nominate from information available then; price the exit after the achievable
decision/submission/landing delay. A touched TP is not automatically a filled TP.
Within a candle, overlapping SL/TP or trail/partial events require ordered tape;
otherwise the policy outcome is bounded or censored.

Compare fixed TP/SL, trailing with an arm threshold, observed source-mirror exits,
time caps and one explicitly defined combined policy. Source-mirror reacts only
after the source sell is observable, with its own detection and execution delays;
never sells at the source's historical price. Define how partial source sells,
multiple buys and several source addresses interact before replay.

Proposed discovery hold durations: 3, 5, 10, 15, 30, 60, 120, 300, 600, 900 and
1,800 seconds. This is an inventory of hypotheses, not permission for an exhaustive
Cartesian search. Start with the current control and at most eight challenger
contracts per wallet-chain in a preregistered round. TP/SL/trail candidates are
selected from discovery data, then frozen before validation. All tried variants
and abandoned filters count in the trial ledger.

Use states for observed signal, waiting, not entered, entered, partial, exiting,
failed exit, unresolved, closed and censored. Preserve quantity conservation and
booked partial proceeds. A timeout without an executable exit is not a flat close.
Gas can be paid on a failed attempt; an unresolved bag remains capital tied up.

## Wallet-specific features and selection

Freeze features as available at the modeled entry decision. Evaluate wallet-chain
cells independently, using:

- Coin size: circulating market cap/FDV with their identities and coverage.
- Activity: short-window buy/sell counts and net flow, liquidity/route depth,
  concentration and measured source impact; do not infer organic activity from
  unavailable data or an arbitrary chart-volume value.
- Age: token creation, pool creation and migration age as separate fields.
- Venue, quote asset, source buy size, source repeat-buy/scale-in behavior,
  entry premium and detected signal freshness.
- Prior source holds and source sell visibility; verified companion/funding
  relationships and herding known at that time, with missing coverage explicit.

Start with one interpretable segmentation question at a time. Do not multiply
wallet × chain × age × size × activity × venue × TP × SL into a sparse winner
table. Pooling/shrinkage may aid discovery, but does not establish a wallet's
policy or transfer funded authority from another chain. Missing features may
block an experiment; they never silently choose a different cohort.

## Denominators, validation and outcome metrics

Register all detectable source buys for the frozen wallet-chain set before our
entry gates. Count signals, verified buys, eligible paths, would-pass admission,
refusals, unreadable admission, capacity skips, triggered entries, unfilled
attempts, priced exits and unresolved/censored outcomes separately.

Evaluate each complete policy on the same origins, including that policy's
refusals and `not_entered` episodes. Identical route entry fills may be shared
only by exit-only challengers with identical entry timing and size. Immediate
and dip entries have distinct fills. Whole-origin censoring preserves paired
comparisons but is not evidence that excluded origins performed like survivors.
Publish excluded shares and conservative bounds on the full registered cohort.

Wallet episode IDs are the unit of the funnel. Repeated buys are not independent
trials; uncertainty is clustered by chain/coin episode and day. For pooled
comparisons, wallets sharing the same coin cannot each create an independent n.
Define actual book/capital and concurrency constraints as part of each policy.
Equal-notional studies are explicit standardized comparisons, not forecasts of
our variable-size funded book.

Use chronological discovery, validation and an untouched future test. Purge
overlapping coin episodes around split boundaries by the longest registered
entry-wait/hold/settlement horizon; keep recurring coin clusters out of an
independent-coin holdout or explicitly measure repeat-coin generalization.
Fix the candidate set, stopping dates, sample/power target, multiplicity correction
and failure criteria before the test. Repeatedly checking a winner consumes the
holdout; revisions require a new future cohort.

There is no universal minimum-close shortcut. Before a preregistered round, use
training-only dispersion and coin/day dependence to justify a minimum detectable
net improvement and sufficient distinct-coin/day coverage. Until those numbers
are sealed, the round remains discovery-only. No result is eligible merely
because it exceeds an old 5-, 20- or 30-close floor.

Primary comparison: paired **net cash per registered source episode**, under
the same starting capital model. Report net return per executed trade, median,
win rate, profit factor when defined, drawdown/tail loss, turnover, hold time,
missed moves, locked capital and failure/censor rates alongside it. Distinguish
profitable trades from profitable policies. A high win rate or candle MFE alone
cannot establish a useful policy. Also stress p50/p90/p99 achievable delays,
adverse slippage/cost assumptions and exclusion of the best one/two coins.

## Automatic eligibility: the publication boundary

Implement one pure gate that every hypothetical trade-performance publisher must
call. Validate raw evidence, not a caller's unchecked `complete=true` flag.
Use an immutable manifest with protocol version, wallet/chain/origin identities,
path resolution/coverage, ordering proof, availability clocks, entry/exit evidence,
cost/quantity identities, admission coverage, policy/trial ledger, data split and
evidence digests. Mixed protocols and model versions cannot pool.

| Failure | Required rejection code |
|---|---|
| Missing manifest, old protocol or mixed versions | `protocol_missing_or_legacy` |
| Bar width over 1s, sparse prices, missing relevant seconds | `path_resolution_or_gap` |
| Unordered touches, unavailable feature/candle, future dip | `ordering_or_lookahead` |
| Unknown source transaction, wallet, chain, mint or venue | `identity_unproven` |
| Mark-only fills, wrong size/decimals, missing executable route | `execution_evidence_missing` |
| Unknown material costs or achievable delay | `cost_or_latency_unknown` |
| Truncated hold/exit, unresolved quantity or submission | `outcome_censored` |
| Admission-selected denominator or unmatched origins | `cohort_invalid` |
| In-sample winner, unsealed trial plan or insufficient holdout | `validation_missing` |

Separate data/model eligibility from statistical conclusion eligibility. A valid
modeled episode can support descriptive replay without proving a strategy.
Only a complete validated study can publish a supported performance conclusion.
Neither state grants funded authority. Store failed receipts and raw records;
automatically suppress rankings, recommended rails, arming claims and brain
performance findings from ineligible studies. Bounds and collection diagnostics
remain visible under those labels.

Inventory all existing output routes before implementation: promotion rollups,
paper reports and wallet arm cells, candidate exit-search receipts, adaptive/rail
counterfactual reports, refusal/dip studies, generated research documents and
brain performance-finding publication. The new gate must protect publication and
downstream policy consumption; changing only one display is insufficient.

Legacy results are automatically ineligible by missing protocol, with their
history retained. Do not rewrite old fills or delete valid operational findings.
The minute-promotion caveat in the 2026-09-23 UI design cannot authorize strategy
claims under this rule. Old counts/coverage gates and day-scale exit grids also
cannot satisfy it. Do not label the new automatic gate live until those consumers
are wired and fault injection proves they reject malformed input.

## Collection capacity and execution isolation

Share coin-level events across wallet studies; retain full source-specific episodes.
Archive relevant state before short tape retention deletes it. Validate complete
venue state offline and use separately budgeted independent quote probes for
calibration, rather than raising every existing worker to 1Hz.

A dedicated key alone does not isolate a provider's shared IP quota. The capacity
plan must include endpoints, per-IP/provider limits, queue delay, memory/disk,
retention and backpressure. Priority remains funded operations. Exhausted research
capacity records `budget_skipped` and prevents findings; it does not silently
downsample a study or reserve funded capacity. If a flat-book restriction is needed
to protect fills, report that conditional selection; do not generalize it to all
signals. Workers remain default off until their independent capacity is approved.

No funded latency claim follows from this document. Any later instrumentation
change on a funded path requires its own edge review and matched pre/post p50/p90
timing evidence. Research should read committed state with short read-only
transactions and write only its own store.

## Deliverables and implementation order

1. Enforce publication eligibility and label legacy performance results ineligible.
   Validate the contract with rejected counterexamples before collecting new data.
2. Produce coverage cards for every frozen armed wallet-chain cell and nominated
   candidate: proven address, signal count, path coverage, timing/route coverage,
   missing dependencies and why a verdict is blocked. No hidden empty wallets.
3. Close capture gaps on each supported chain/venue within independent capacity;
   do not claim all-chain readiness until every supported cell is evidenced.
4. Run sealed immediate/dip and exit-policy comparisons, using untouched future
   cohorts. Each wallet gets a supported policy or an explicit blocked/insufficient
   result. Several conditional policies require independent evidence per condition.
5. Show an inspectable wallet card with the tested rules, held-out result and
   interval, distinct coins/days, excluded cohort, delay/cost stress and individual
   entry/exit traces. A timeline must connect source buy → our information → our
   attainable entry → trigger → attainable exit, with a one-second zoom.

Funded promotion is a separate operator decision after evidence review. No automatic
arm/disarm, filter removal, contract mutation or deployment is part of this design.

## Acceptance checks

The future implementation must reject a 60s path despite high row counts; missing
prices must not create a flat timecap close. The runnable current-code probe is:

```bash
python3 docs/audits/memecoin-wallet-research-20261004/check_current_gate.py
```

It currently exits 1 for those two defects. `--expect-current-defects` exits 0 only
when both counterexamples reproduce. That probe alone is not full protocol proof.

Additional behavioral tests must cover: TP/SL order ambiguity; a 1s label over
sparse marks; a complete quiet second versus a lost collector frontier; source
observed after a sealed block; a dip picked using future lows; candle availability;
clock regression/reorganization; same address on different chains; airdrop versus
paid swap; wrong decimals/quote asset/quantity; partial and failed exits; unknown
costs; late mirror receipts; retention truncation; correlated repeated buys;
unmatched origins; unsealed holdout; exhausted research capacity; and an absent
protocol manifest through **each** publisher and downstream policy consumer.

Each rejected fixture must prove that the publisher produces no strategy verdict,
no funding instruction and no performance brain finding. Normal, fully evidenced
fixtures must remain readable as modeled research. Crash/resume and rollback tests
must preserve receipts and prevent duplicate simulated inventory or publication.


## Implementation boundary (2026-10-04)

The first release quarantines legacy hypothetical publishers and cached promotion/
profile consumers, supplies pure causal whole-bag replay, an immutable owned store,
sealed future rounds, full-census paired diagnostics, local tape/header archival and
wallet/chain coverage. `evaluate_round` deliberately emits no strategy winner: power,
coin/day uncertainty, cost/delay stress and independent review are required before
any supported conclusion. The local worker performs zero provider requests. It does
not invent missing V3/V4 tick/hook state or a one-second path from sparse quotes.
Partial source-mirror exits are explicitly unsupported and censor their model.

Research code deploys through `scripts/deploy_wallet_research.py` to a separate
content-addressed release and user service. A pinned service owns only research
state, restricts IP socket families, limits CPU/I/O priority and verifies its own
PID/release in coverage before deployment succeeds. Rollback restores its previous
release/service; it never restores legacy publication eligibility. No DB backup.

Actual funded records remain usable. New funded entries use the existing fallback
instead of unsupported paper-arm selection; existing frozen position contracts and
operator rail mappings retain their identity. No synchronous research read or new
provider request is introduced on entry.

## Local dossier and flow extension (2026-10-05)

The wallet/chain dossier reads 30 days from the owned research archive and retains
operator research notes. Its structural admission denominator requires an explicitly sealed
default baseline, transcripts of all frozen admission gates, positive decision-time market cap below
$1M, and a complete wallet/mint zero-prestate transcript. Admission and complete execution
outcomes have separate counts; missing paths remain censored. Legacy paper diagnostics
are collapsed beneath the universe. Historical records are retained.

`wallet-universe-flow-v7` separately measures the existing 14-day census of first
observed wallet/coin buys. Its stored anchors produce cumulative signed quote flow at
5/10/15/30/60 seconds, in each quote asset's decimal atomic units. Solana verified
buyer counts are lower bounds when payers remain unattributed; EVM owner counts stay
unknown. These are chain-timestamp windows, with integer-second clock resolution,
observed-venue scope and the existing source/own-transaction exclusions.

Solana queries wait until `t0 + 61` before capturing the inclusive `t0 + 60`
endpoint second. Old flow versions cannot supply current comparisons. Provider
pagination exhaustion is not independent proof of timely indexing. Transaction
balances can prove an add-on when positive prior holdings are visible; zero on
touched accounts cannot prove a new wallet-wide position. The UI preserves that
unknown. Flow at 120/300 seconds is explicitly not captured.

This extension adds no provider calls or funded-path work. Production collection
and deployment are separate from this local implementation. A producer of full
default-admission receipts, wallet-wide opening proofs and complete executable
paths is still required before clean paper outcomes or strategy support exist.

`wallet-fast-move-admission-v2` stores that transcript independently of an execution
episode in the existing private `we_raw` archive. Import with
`scripts/research/wallet_execution_worker.py --db <owned-store> --ingest-admission <receipt.json>`.
The packet binds its origin, source observation bytes, identity mapping, all-account
prestate, cap observation, frozen defaults, and per-gate input records through a
recomputed canonical digest. Unknown defaults, overrides, exemptions, add-ons,
late inputs, malformed units and identity mismatches fail. Legacy hash-only claims
do not enter the current denominator. Missing paths leave structurally valid
receipts pending; replay starts at the recorded decision plus calibrated entry delay.
Episode root identity, source units and source clocks must match the receipt.

These checks establish structural consistency only. They do not authenticate
the chain observation, enumerate holdings, or independently evaluate gate predicates.
The UI labels them **Structural receipts pass**, exposes decision/digest metadata,
and requires independent review of producer provenance and gate correctness.
The import command does not make the existing collector produce these receipts.
No historical row is upgraded to clean paper evidence, no strategy is supported,
and no forward worker is activated by this local change. Competing immutable
imports reject conflicting bytes; the store refuses foreign tables before DDL.

The observer computes compact horizon and source-position projections while its
selected decoder history is resident. Publication keeps current main's metadata-only
reads; it does not reload every transaction history to render the new flow windows.
Incomplete or older-version evidence cannot supply current horizon comparisons.

Runnable offline check: `python3 -m pytest tests/test_wallet_universe_flow.py tests/test_wallet_universe_navigation.py tests/test_wallet_dossier.py tests/test_wallet_dossier_admission.py -q`.

## Research table amendment (2026-10-05)

Wallet Map now presents per-chain research coverage separately from live roster
controls. The default funnel GET never reads `wallet_follow_funnel.json`; roster
identity and effective authority come from current ledger reads. Missing or broken
legacy caches cannot populate or disable live controls. Existing operator
confirmation and funded gates remain authoritative.

The coverage artifact adds distinct archived coins and observed UTC dates by
wallet/chain. These count collection since enrollment, not a frozen 30-day sample,
elapsed collection duration, independent episodes or validation. Naive, malformed
and missing timestamps cannot add UTC days. Older artifacts lacking those counts
display unknown. Structural admission counts require the dossier; raw receipt
jobs and archived buys are not structural passes or usable outcomes. Counts alone
never support a strategy.

The explicitly opened forensic archive uses `?diagnostic_only=1`, carries blocked
legacy eligibility and has no money controls or performance ranking. Returns are
priced-only; paper weights per-chain ratios by priced-close count, while the
partial-filter return pools priced notional. Selected-close counts include unpriced outcomes, whose
share is notional weighted. The cache does not record gated priced-close counts.
The count-weighted mixed-chain funded ratio is omitted rather than presented as
book ROI. Historical data is retained; the hourly legacy funnel publisher is
retired without enabling its diagnostic bypass. The wallet graph refresh remains.

Checks: `python3 -m pytest tests/test_wallet_map_research_boundary.py tests/test_wallet_map_actions.py tests/test_wallet_execution_worker.py -q`.
