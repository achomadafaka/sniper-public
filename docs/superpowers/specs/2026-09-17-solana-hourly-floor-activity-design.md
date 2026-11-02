# Solana hourly floor: feed buys and address activity

Status: approved by the operator on 2026-09-17 (data contract). The cutoff K stays unset until a measured value is approved separately.
Code baseline: `f4632693`. Scope: Solana entry admission only.

## Decision

Use two distinct forms of evidence when the hourly dollar-volume floor would refuse an entry:

1. Use eligible prior feed buys as a partial dollar-volume estimate.
2. If the hourly floor still fails, request one page of address signatures.
3. Permit an hourly-floor exception only when the qualifying count reaches the reviewed cutoff.
4. Apply every other entry gate and book limit.

The activity count never becomes dollar volume. It supports an explicit policy exception.
An error preserves the decision already computed from DexScreener and valid feed evidence.
A count below the cutoff also preserves that decision. It does not prove a thin market.

This proposal replaces the sampled transaction decoder in Gamma's `1c2de53d` proposal.
That proposal uses up to 21 calls and 800 ms. This proposal uses one RPC call and 600 ms.
It also narrows the scope to the hourly floors and uses feed buys only, as requested.

## Evidence and limits

The user supplied these results:

| Coin | DexScreener hourly USD volume | Successful mint-address transactions in the hour |
|---|---:|---:|
| SNORP | 2 | 1,922 |
| Purplechip | 7 | 1,411 |
| DYNABLOCKS | 16 | 1,376 |
| CUMINU | 364 | 1,586 |

Gamma's draft reports 16 hourly-floor refusals among 554 volume-gate attempts over seven days: 2.9%.
It reports that prior feed trades qualify SALAMI and CUMINU. These are reported results, not independently reproduced here.
The raw per-coin responses and exact source signatures remain required evidence before implementation review closes.

These counts establish address activity if the method, address, window, and error filter are correct.
They do not alone establish swaps, dollar turnover, a wrong pool, or an untracked venue.
To establish a wrong pool, retain the selected DexScreener pair and verified swaps in another pool during the same window.
A covered pair that the selector missed requires a different repair from a venue that DexScreener does not cover.
Do not change shared mark selection as part of this feature.

Solana's signature method returns transactions that reference the supplied address, newest first.
The response includes errors and nullable timestamps; it does not classify swaps.
A mint query is not guaranteed to cover every swap venue.
See [Solana's method contract](https://solana.com/docs/rpc/http/getsignaturesforaddress).

One page contains at most 1,000 signatures, including failed transactions.
It cannot return an exact count of 1,922 successful transactions.
Use a lower bound from the returned page; never multiply it into an hourly estimate.
See the [provider's page-limit documentation](https://docs.getblock.io/api-reference/solana-sol/getsignaturesforaddress-solana).

## S1: prior feed buys

Let `T` be the frozen, corrected entry-decision time. The hourly window is `[T - 3600 seconds, T)`.

Eligible rows must satisfy all these conditions:

- The chain is Solana and the mint matches exactly. Solana mint case is significant.
- The row describes a buy with a finite, positive, attributable USD amount.
- Its validated trade time is within the hourly window.
- The system received the row before `T`. Historical queries must apply this condition too.
- Its transaction identity is known and distinct from the source transaction.

Deduplicate the same transaction and mint across J7, Axiom, global, and on-chain delivery.
Count a multi-row transaction once. Exclude contradictory duplicate amounts instead of selecting the larger amount.
Exclude rows with unknown identity, unknown time, unknown USD basis, or an uncertain buy classification.
Exclude the source transaction across every delivery lane. Do not subtract it a second time.
Do not require that the wallet remains followed today when reconstructing prior decisions.

Use the greater of the eligible feed sum and the current provider organic-volume value.
Do not add the feed sum to the provider value: the same trades can appear in both.
Never use the feed sum for the upper-volume limit.
The feed sum remains partial evidence; it is not a complete market-volume measurement.
A missing result remains unknown. Do not claim zero added latency: this path requires a database read and trace work.

## S2: one-page address-activity exception

The queried address is the token mint, matching Gamma's proposed measurement contract.
Do not substitute a pool, vault, token account, or router without a separate method version and a new cutoff study.

Use one `getSignaturesForAddress` request with `limit=1000`, `commitment=confirmed`, and `before=source_signature`.
The source signature must come from a validated event format. Do not assume every origin string has the same colon-separated structure.
Without a source signature or an attributable mint, return `unknown`; make no RPC call.

Count each returned signature once only when:

- The signature and slot are valid.
- The `err` field exists and is null.
- The confirmation status is confirmed or finalized.
- The block timestamp is a valid integer within `[T - 3600 seconds, T)`.
- The signature differs from the source signature.

Missing timestamps add no count. Failed transactions add no count. No transaction decoder runs in the entry path.
The source cursor excludes later transactions, including activity between the signal and the decision.
That omission lowers the count; record the anchor and time gap.
Retain the raw page outside the funded ledger for audit when the existing research store supports it.

If the count reaches cutoff `K`, record `h1_floor_activity_exception` and waive only the hourly floor.
A partial page can prove `count >= K`. It cannot prove the complete hourly count.
A short page does not prove complete provider history.
Valid `K` must be a reviewed integer from 1 through 1,000. An unset cutoff disables admission use.
No numeric cutoff is selected by this proposal.

## Gate ordering

The current gate evaluates five-minute count, five-minute floor, and five-minute ceiling before the hourly floor.
Preserve that order in [memecoin_real_runner.py](/home/bigpapi/Desktop/Sniper/sniper/services/memecoin_real_runner.py:6213).

The new logic is eligible only for `turnover_under_floor` or `under_abs_floor` on Solana.
It does not run for an existing pass, missing provider data that already passes, manual policy exemptions, or another refusal family.

S1 can satisfy the hourly dollar floor. S2 can grant a named exception to that floor.
Neither source may waive an m5 floor, count requirement, ceiling, wallet authority, age, chase, route, safety, exposure, or loss limit.
The upper-volume check retains the original DexScreener value and effective threshold.
If that value describes the wrong pool, the ceiling can also miss excessive real activity. This proposal does not repair that limitation.
Run the existing fresh quote, round-trip cost, and premium checks after any exception.
An exception grants no reservation or position by itself.

## Deadline and provider isolation

| Control | Proposed value |
|---|---|
| RPC calls per entry attempt | At most one; existing retry attempts remain subject to the same rate budget |
| Page count | One; no pagination or transaction fetch |
| Total additional S2 deadline | 600 ms, measured with a monotonic clock |
| Concurrent requests | One |
| Rate limit | One request per second, burst one; no wait for a token |
| Rate-limit cooldown | 120 seconds |
| Endpoint | Dedicated research quota; no custody or public fallback |

The deadline includes admission to the worker, connection, response read, and parse.
A socket read timeout alone does not establish the total deadline.
A late response must not alter the funded decision. An in-flight timed-out request keeps its concurrency slot until it ends.
No automatic retry or endpoint rotation occurs after any RPC error.
A busy worker, exhausted rate budget, malformed response, or timeout returns `unknown` immediately or at the deadline.
A 429 starts the cooldown. A later distinct origin may use a healthy endpoint after the cooldown.

Reuse the existing endpoint isolation checks, but verify provider-account quotas as well as URL equality.
Two different URLs can still share one provider quota. The operator must attest that separation before activation.
Invalid configuration disables this optional check and reports the reason; it must not prevent the funded runner from starting.
Never log endpoint secrets. No new service, queue, decoder, or funded database table is required unless existing facilities prove insufficient.

## Cutoff study

Gamma's 20 winners and 20 losers are an exploratory, outcome-balanced sample.
They cannot estimate the live win rate or the frequency of false admissions.
Equal count distributions would mean no detected outcome separation; they would not prove zero predictive value.

The study must:

1. Freeze the 40-position roster, date range, outcome definitions, and source signatures before count analysis.
2. Separate manual closes, incomplete outcomes, and repeated positions in the same coin.
3. Reconstruct the pre-decision window with no post-entry transactions or later feed receipts.
4. Use exactly the proposed one-page mint query and count rules, including page saturation and missing timestamps.
5. Label independently verified thin and busy cases by actual swap evidence; profit labels do not define market activity.
6. Select the candidate cutoff on earlier data, then freeze it before testing later data.
   Keep repeated coins in one evaluation group; exclude overlap that would leak the same coin across the split.
7. Report venue, market-cap, wallet, missing-data, and saturated-page breakdowns.
8. Include hourly-floor refusals; existing funded trades already passed an earlier selection process.
9. Keep unsupported historical counts and unavailable outcomes unknown.

Do not select a threshold merely between the largest named thin count and the smallest named busy count.
The four examples can motivate a study but cannot validate its cutoff.
Historical RPC reconstruction may also expose confirmations that the provider could not serve within 600 ms at entry.
Report that availability gap. Measure actual request success and latency prospectively through existing traces before activation review.

Evaluate any rescued-refusal outcomes with intended size, chronological executable quotes, fees, and the actual funded exit policy.
Preserve the other gates and book limits. Missing paths remain unknown; a chart peak is not an executable fill.
This work adds no paper arm and submits no research-funded trade.

## Evidence record and implementation boundary

Record a versioned entry substage with these fields:

- Chain, mint, origin, attempt, decision time, selected provider pool, source signature, and method version.
- Raw provider h1/m5 values, organic h1 value, effective floor, ceiling, and original decision.
- Feed sum, eligible count, duplicate count, excluded-source count, and unknown-row count.
- Queried address type, anchor, valid signature count, failed count, missing-time count, page saturation, and oldest timestamp.
- Cutoff, result type, exception used, call count, elapsed time, budget state, and final gate decision.

Counts and dollars use different field names. A final failed route or safety check remains the final refusal reason.
Use existing attempt trace and frozen contract facilities; do not mutate old funded contracts.
Reuse the pure volume evaluator and add no RPC call inside it.
Place feed queries in the persistence layer and provider access in a separate read-only helper.
The existing pool-activity research script has different address, window, timeout, and error rules; it is not a drop-in implementation.

Acceptance tests must prove source exclusion, cross-feed deduplication, prior-only windows, one-call limits, and timeout fallback.
They must also prove that every non-hourly refusal survives a high activity count and that no late response changes a decision.
Calibration fixtures must use the same count function as the live helper.

## Review outcome

The simpler architecture is feasible as a capped address-activity exception, subject to the evidence and cutoff review above.
Raw counts do not prove dollar volume or remove the risk of synthetic activity.
The next approval must cover this data contract and the measured cutoff before code can change funded admission.

## Addendum 2026-09-17: trade identity for J7 rows without a signature

The operator ordered the build finished. Over two days, Solana J7 rows carried a random trade id
(`swap:<uuid>`, with a separate `tradeId`) 1,969 times and a transaction signature 642 times.
Excluding the id form made S1 admit 0 of 16 hourly refusals.

S1 therefore accepts the J7 event id `swap:<uuid>` as a trade identity, with these rules:

- One row counts once per identity. The source is excluded by the same identity.
- A cross-lane duplicate is one trade: same wallet, mint and side, USD within 1%, and trade time
  within 5 seconds. Contradictory amounts for that trade exclude both rows.
- Every other S1 condition in this document still applies.
- The evidence record names `identity_kind` (`signature` or `trade_id`) for each counted row.
