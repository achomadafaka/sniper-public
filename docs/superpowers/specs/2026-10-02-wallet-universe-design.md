# Wallet Map: arming proof and a useful per-chain universe

Audit and proposed design, 2026-10-02. Read-only production inspection; no wallet authority, funds,
policy, subscriptions, service state, or production database changed. The accompanying JSON is a
snapshot, not a continuously updated universe. No new external API calls were made successfully.

## Verdict

Wallet Map's action uses the same per-chain authority writer as Wallets → Armed. Isolated Flask
checks proved a Solana address becomes effective/relevant on the Armed board, Robinhood's alias
stores `4663` without removing Solana, and disarming Solana preserves Robinhood. The existing
66 action/map/UI tests passed. This proves route-to-ledger-to-board behavior on fixtures, not an
organic funded fill or a live subscription acknowledgement. The live dashboard opened at its
login page; no live arm was performed to test it. Web pin `fc81186a5`, funded pin `ea863c695`.

The current funnel file contained every active arm at inspection. A separate isolated case proves
an omission: `funnel_live_overlay` visits only saved funnel/candidate rows. A newly added or armed
wallet absent from both stays invisible until regeneration. A paused wallet can also remain in
its configured `armed` stage: that stage is not effective authority. An overlay read failure serves
old authority with `live_error`, but `funnelStampText` does not display that flag. A deleted hourly
file prevents any live overlay at all.

## What is worth fixing first

1. Build live roster membership from the ledger, then attach optional saved measurements. Use
   configured chains for controls and the same effective-authority predicate as Armed for status.
   Missing/stale measurements remain unknown. An arm response should link to Wallets → Armed on
   that exact canonical chain. A failed/ambiguous POST must prompt a readback, never a blind arm
   retry. Preserve all other chains and excluded/paused semantics. Missing address proof must be
   visible before a money action, even when profile identity is known.
2. Separate **authority**, **proven address**, **active subscription**, **last observed buy**, and
   **paper outcome coverage**. Do not call any one of these “tracking.” Both current fast Solana
   and EVM watcher helpers filter to effectively armed owners. Paper-only roster membership does
   not prove the direct watcher sees that address. The Armed UI's “not on J7 ... cannot signal”
   tooltip is obsolete for wallets covered by direct on-chain lanes; replace it with actual source
   coverage. A proof helper is not a subscription heartbeat.
3. Remove cross-chain comparisons from the decision surface. `funnel_rows.agg` weights per-chain
   pooled returns by close count. This is neither chain-specific evidence nor the actual pooled
   total-book return when notionals differ. Keep per-chain `funded_clone`, gate-feasibility,
   censoring, funded outcomes, open inventory, and policy identity beside one another.
4. Remove the evidence bottleneck before expanding funding. Paper-only observation needs its own
   explicit read-only owner universe, separate from funded subscriptions/authority. Otherwise the
   workflow requires funding a candidate to obtain the evidence needed to decide whether to fund
   it. Keep current relay paper intake while adding direct observation; deduplicate exact origins.

## Current coverage and evidence

These are current ledger/helper results, not live subscriber counts. Some wallets may still signal
through the relay. They must not be labeled dead solely because a direct proof is missing.

| Chain | Effective armed wallets | Wallets with proven direct-watch addresses | Funded-clone wallet pairs with priced closes since Sept 1 |
|---|---:|---:|---:|
| Solana | 26 | 24 | 185 |
| Robinhood 4663 | 32 | 29 | 264 |
| BSC 56 | 24 | 21 | 65 |
| Base 8453 | 24 | 14 | 15 |
| Arc 5042 | 29 | 0 | 0 |

Solana missing proof: yayayayayey, Thokani. Robinhood: yayayayayey, Thokani, cap. BSC:
loganlim_x, figaro, Thokani. Base: Ew4rdo, starcatcher444, jpegkite, LevisNFT, fmpumpguy,
OmakaseOnly, songz, Thokani, notanicecat69, probablyinsider. Arc has no proven owners in the
EVM watcher helper; its actual supported signal/route path needs separate verification.
Checkout configuration enables Solana/Base/Robinhood/BSC/Arc, on-chain lane on, slow follow off,
and per-wallet copy-arm policy on. Checkout configuration is not proof the process loaded it.

Solana has measured graph edges: follows, same-slot co-buy, slot follower, shared funder, and
sells into another wallet's buys. Other chains have no stored relationship edges. At the later
inspection the herder materialization had 2,954/3,687 first buys read, 726 UUID-only and 5 left out.
The universe JSON captures graph edges and copy evidence on independent read handles; it is not
an atomic cross-database snapshot. Unidentified position owners are excluded, not named wallets.

Discovery examples: SQHK48QT was ahead on 15 coins across 7 past winning sources, median lead
22 s; 55msLm12 ahead on 9 coins across 2 sources, median lead 6 s. Both stored upstream verdicts
were `not_distinguished`. Their “won” counts describe the sampled source coins, not the candidates'
realized or executable copy PnL. Do not present either as ready to arm based on those numbers.

Paper survivor diagnostics with >=20 priced closes: Solana jpegkite +30.3%/42 priced, 23 unpriced;
chrollo0o0 +22.6%/26, 18 unpriced; starcatcher444 +14.6%/24, 1 unpriced. Robinhood chrollo0o0
+20.3%/29, 2 unpriced; ether_monk +16.6%/25, none unpriced; econoar +7.4%/25, none unpriced.
BSC has only cissy at that sample floor, -19.5%/20. Base has no wallet reaching that floor.
These are candidates for examination, not a funding ranking: survivor bias, open exposure,
shared coins, differing entry gates and entry-price provenance still need matching.

## Proposed decision view

Use one chain picker and a searchable wallet list. Default to the operator's selected chain,
never silently return to Solana after an action. Keep roster, candidates and measured neighbors
in one scoped universe, with their intake source and coverage. Do not infer independence from
missing links. Add profiles only when chain/address ownership is proved; keep aliases explicit.

The main view answers five specific questions, with an action or an evidence gap beside each:

| Question | Evidence | Decision it supports |
|---|---|---|
| Can we actually see this wallet? | Proven address, source, active subscription generation/ack, last buy and lag p50/p90 | Repair observation or collect paper before considering funding |
| Does our execution work on its coin setups? | Matched exact origins; executable entry/exit coverage, landed delay, copy drift, route capacity and fees | Separate good source timing from fills we can reproduce |
| Is it adding anything to our roster? | Distinct shared coins, ordered buys, k/n first-buy coverage, shared followers/funders | Avoid increasing exposure to the same trade cluster |
| Which policy fits its setups? | Identical forward origin set across frozen paper policies, censored/open outcomes and coin concentration | Choose a policy for further paper validation; show current funded rails separately |
| Why did we miss its signals? | Actual attempt/refusal, exact gate inputs/config, forward refusal-capture coverage | Measure one selection-gate change without exempting safety rules |

Click a wallet to show an ordered path: earlier observed wallet → coin/venue/source buy → this
wallet → later observed buyers → our own detection/decision/landing. Put source clock trust and
received-at time on the path. Same-slot order is transaction index, not invented millisecond
precision. Shared funder/co-buy are undirected associations; timing edges do not prove causation.
Each edge opens the supporting distinct coins and timestamps, with missing/truncated counts.
Display only the selected neighborhood, with a complete edge list available; do not restore the
retired all-wallet force simulation. A matrix of shared coins is the comparison view for choosing
between several candidates. No graph edges on BSC/Robinhood/Base/Arc means “not collected.”

The accompanying interactive evidence explorer already provides per-chain selection, 849 stored
identified wallet-chain nodes, actual measured Solana connections and coin examples, direct-proof
coverage, and priced/unpriced paper/funded closes. It is an audit snapshot outside the live app,
not a production implementation or a funded recommendation. It caps displayed coin examples at
six per edge; the JSON keeps the original stored evidence.

## Collect coin setups once, then compare wallet strategies

Reuse existing feed observations v2, address proofs, entry snapshots, immutable attempt traces,
classification/funded contracts, strict forward paper cohorts, refusal capture, and graph reads.
Do not create another execution engine or put enrichment on the funded callback.

The unit is (chain, wallet identity, exact source transaction/event, mint, source buy episode).
Persist raw event identity, exact atomic amounts as TEXT, source/detection/decision/landing clocks,
confidence and provenance. Retain duplicate/re-entry episodes as their own typed observations,
then declare the analysis denominator explicitly. An observation is recorded before admission;
one genuine source event is not multiplied by each paper arm or retry.

At that observation freeze: venue and route family (Pump curve, PumpSwap, EVM V2/V3/V4), native
versus cash quote asset, verified launch state/coin age, market-cap band and source, quote-side
depth/route capacity, source size versus depth, recent flow/turnover, prior distinct buyers and
leader/follower timing, creator/funder relationship when proved, and token controls when known.
Unknown stays unknown. Narrative/theme tags are asynchronous, versioned enrichment; they cannot
be reconstructed later and treated as known at entry. No DexScreener prices may enter fill evidence.

Then record both accepted and blocked outcomes with one frozen policy/config identity. Existing
`gated_paper` only approximates market-cap/source-buy feasibility using current config; it omits
volume/turnover and many executable gates. Relabel it **partial feasibility**, not “funded-admissible.”
Historical hypothetical gate changes stay diagnostic when the original inputs are missing.

Prioritize capture as a capped research queue:

1. Current armed wallets missing direct proof/subscription evidence: repair observability first.
2. Newly observed candidates with exact identity and recent chain-local buys: watch without funded
   authority; gather origin-matched paper with all tested policies.
3. Boundary refusals with adequate recorded inputs: reuse the refusal-capture worker and its
   provider yielding, paced reads and typed censoring. Missing route/quote is not a zero return.
4. Earlier buyers and connected candidates: collect their complete next-window buy population,
   not only the coins that later produced a winning herder. Keep creator/copy-bot hypotheses as
   visible screen reasons with supporting counts, not evidence-free vetoes.

Use isolated research RPC capacity and a finite read/provider budget. Activation must first prove
it does not consume funded slots, keys, quote capacity or entry-thread work. Report collection
p50/p90 off the hot path separately from organic funded fill p50/p90. No speed claim from fixtures.

## How to identify a wallet that fits

Do not invent a single weighted score. Use a shortlist with evidence states: unknown identity,
insufficient observation, enough observations for a paper comparison, forward paper fit, trial,
funded operator decision. Existing promote/trial rules remain authoritative until a separately
approved policy change. Show which source/coin setups each wallet actually trades and which
setups we can reproduce; separate watch-only from paused collection.

For each wallet × chain × setup compare policies on the SAME whole-origin cohort. Show priced,
unpriced, open and rejected episodes; total notional and net execution costs; distinct coins;
last-window return; concentration in its largest coin; leave-largest-coin-out result; detection and
landing latency; realistic route coverage. Preserve current strict-selector outputs separately
from the survivor “best arm.” Never borrow evidence from another chain.

Freeze candidate selection at a time cutoff. Evaluate on later observations with existing rails,
including every qualifying coin, and compare to the current roster on contemporaneous similar
setups. Cluster shared coin/launch episodes so ten wallets in the same coin do not look like ten
independent wins. Policy search is training; subsequent unchanged paper is validation. Require
adequate independent coins and stated uncertainty before using a result; no arbitrary new funding
threshold is introduced by this design.

For adjustments, show the current per-wallet chain exit override, selected strict paper arm,
coin-band fallback and frozen funded contract. A proposed coin-type filter or gate exemption must
show affected origins and unsupported inputs, then run a separate frozen paper comparison. Safety,
identity, route, reserve, signing and close-claim invariants remain non-exemptible. Do not let a
per-wallet discovery label mutate existing funded positions.

## Delivery order and acceptance checks

A. Repair live overlay membership/stale authority and add chain-specific Armed navigation. Regression
checks: stale/missing file; arm not present in saved rows; pause/exclude; alias and multi-chain arm;
error/timeout readback; map arm response and Armed row agree. No funded path code or calls added.

B. Add the per-chain decision view using offline materialization of existing facts. Check chain
isolation, unknowns, exact identity, stale/error labels, graph directions and provenance. Every
relationship and shortlist row must open supporting coins/events. A failed graph read must not
become “no relationships.” Large refreshes remain off the web hot path.

C. Add a separate paper observation intake after a resource-budget check. It must never call the
funded hook or grant authority. Validate watcher handoff, duplicate origins, restart cursors,
provider pressure, unsupported venues, quiet wallets, ban/rate-limit/timeout and partial decoding.
Start with one observable chain and prove coverage before adding another; no fabricated EVM
relationship map from router addresses.

D. Expose setup-conditioned strict policy comparisons and refusal outcomes, then propose one
measurable policy adjustment at a time. Policy changes, funded activation and service deployment
are separate operator decisions. No automatic arming or filter exemptions.

Rollback: A/B revert UI/read models without modifying authority; keep previous materialized format
readable and mark newer fields unavailable. C stop only research intake and retain its immutable
observations; funded observers remain intact. D revert only future policy selection, preserving
frozen contracts and exact existing close identities. Half-written materialization must publish
atomically, never as an empty successful universe.

## Scope limits

This design does not recover omitted historical transactions, prove strategy profitability from
past winners, grant new funded authority, verify current live subscription acknowledgements, infer
causal influence from timing, create new cash/native routes, or alter safety gates. No extra
synchronous call/lock/retry is allowed on the entry path. No latency delta was measured because
production entry code was unchanged. Whole-universe cross-chain pooling is deliberately absent.

Runnable existing checks:

```bash
python3 -m pytest tests/test_wallet_map_actions.py tests/test_memecoin_wallet_map.py tests/test_memecoin_wallet_map_ui.py -q
```

Current checks pass; they do not cover a live roster wallet absent from both saved lists. The
minimal isolated reproduction is: add a wallet to an in-memory ledger, force-arm one chain,
call `funnel_live_overlay` with empty funnel/candidate lists, then assert the wallet is present.
At design time that assertion failed. It belongs in the first repair's failing regression suite.

The runnable audit proof is `docs/audits/2026-10-02-check-wallet-map.py`. Run it from a Sniper root; its default checks pass. At design time adding `--require-live-overlay` failed with the reproduced missing-live-arm assertion. It uses only in-memory fixture authority.

## Delivery evidence, 2026-10-02

Implemented Map, isolated research CLI and setup evidence. Final focused suite: 389 passed; browser fixtures and the original live-overlay reproduction pass. Seven chain snapshots materialized from read-only ledgers to scratch; the Solana snapshot is 10,557,278 bytes. Producer and reader therefore share a 16 MiB cap and reject oversized snapshots before publication. Research remains default off; no separate provider capacity, watcher activation, policy promotion or new routing was established. Quote results preserve source/receipt clocks and censor source-expired entries and late horizon completions. Frozen proposal policies must belong to the selected setup/window and have one explicit identity per arm.
