# Relay origin wait: retry schedule and pre-sign overlap (design, 2026-10-07)

Status: DESIGN ONLY. Operator approved designing it (via Alpha, 2026-10-07); no build until the
operator approves this spec. Author Beta. Data cut 2026-10-07 ~18:45Z.

## Problem

A Relay buy seen by our EVM watcher is committed only after the Relay origin verifier proves the
watched wallet initiated it (`sniper/services/memecoin_relay_initiation.py`, `WatcherVerifier`,
on the watcher's `OriginWorker`). Until then the runner does not see the detection. The verify
spends the watcher's 0.5-1.4 s lead over J7:

- 8453: 12 detections dropped `duplicate_of_feed` (J7 decided first).
- 56: HUSH (0x52ceb7d3, chrollo0o0, attempt 2c0ab7cf8714). The verify took 7,886 ms (3x
  `relay_request_not_indexed`, 3x `malformed_origin_evidence`, then ok). J7 filled first, the
  lane refused `snapshot_predates_execution`, and the J7 fill closed b_stop at -$61.51 (Gamma).

Verify time (watcher log lines `relay origin ... verify_ms=`; attempts notes since ~14:41Z on 10-07):

| chain | verifies/day | verify p50 / p90 (all lines) | recent with attempts: outcome |
|---|---|---|---|
| 4663 | ~30 | 0.86 / 7.85 s (n=58) | 7 verified, 7 `origin_wallet_unverified` (n=14); p50 7.7 s (Gamma) |
| 56 | 20-52 | 0.65 / 2.02 s (n=115) | 5 verified (n=5); p50 2.7 s (Gamma) |
| 8453 | ~10 | 1.01 / 2.46 s (n=19) | 3 verified (n=3); p50 1.6 s (Gamma) |

## Why the cap is 8.0 s

It is not a constant. `WatcherVerifier.__call__` makes up to 7 attempts. After a
`relay_request_not_indexed`, `relay_not_settled` or `malformed_origin_evidence` it sleeps
`0.1 * 2 ** attempt` for attempts 0..5, so the sleeps add up to 0.1+0.2+0.4+0.8+1.6+3.2 = 6.3 s.
Each attempt's own fetch is p50 0.22-0.27 s / p90 0.32-0.42 s, about 1.7 s over 7 attempts. The
sum, about 8 s, is the cap every chain shows.

## Relay's indexing lag (time from the first attempt to the answer that verifies)

From the attempts notes. Each note gives its own ms, and the sleeps are known. Readiness falls
between the start of the last failed attempt and the start of the ok attempt:

| chain | n (verified after a retry) | readiness lower bound p50 / p90 | upper bound p50 / p90 |
|---|---|---|---|
| 4663 | 7 | 2.26 / 3.79 s | 4.26 / 7.29 s |
| 56 | 3 | 3.97 s | 7.65 s |
| 8453 | 2 | 0.66 s | 1.17 s |

The samples are small. The late field is now known on 4663. Since the attempt detail deployed
(9ac89b348), all 11 `malformed_origin_evidence` attempts (to 19:14Z) are
`KeyError('block')@memecoin_relay_initiation.py:131`. That is the matched destination `outTxs[]`
entry's `block`, which Relay fills in after it reports `success`. No 56 or 8453 verify has run
since the deploy.

## Part 1: the retry schedule (the lever on 4663)

Exponential backoff overshoots readiness by half the last gap on average. A flat poll at interval
p overshoots by about (p + fetch)/2.

| chain | exponential last gap p50 | expected overshoot today | flat 250 ms poll | gain p50 |
|---|---|---|---|---|
| 4663 | 2.00 s | ~1.00 s | ~0.24 s | ~0.8 s |
| 56 | 3.55 s | ~1.78 s | ~0.26 s | ~1.5 s |
| 8453 | 0.51 s | ~0.26 s | ~0.25 s | ~0 |

Rate limit. Relay's keyed limit for `/requests` is 10 requests/s per key (docs.relay.link API
keys). Every `origin_rate_limit:relay_requests` refusal we logged was on `/requests/v2`, which
Relay throttles in stages ahead of its retirement (finding a404457d). There have been none since
the v3 switch. One verify at a flat 250 ms with ~250 ms fetches makes about 2 requests/s. Up to 4
watcher processes (4663, 56, 8453, 1) with 2 workers each share one key: 16/s at worst, which is
over the limit.

Proposal:
- A flat poll of `MEMECOIN_RELAY_ORIGIN_POLL_S` (default 0.30 s) for the same three retryable
  reasons, inside the same ~8 s wall budget.
- A per-process pacer for Relay `/requests` at `MEMECOIN_RELAY_REQUESTS_MAX_PER_S` (default 2/s).
  Four processes then stay at 8/s or less.
- A 429 keeps today's handling: `origin_rate_limit` retries twice at 0.1 s, then refuses.
- The non-retryable classes are unchanged (`origin_wallet_unverified`, mismatches).

Edge cases:
1. Money path: no (detection only).
2. The fill path: shorter wait, no new call type.
3. Concurrency: the pacer is per process with one lock; workers share it.
4. A wrong Relay answer is still refused by the same proof checks.
5. Rollback: set the poll knob to 0, which restores exponential.

## Part 2: the pre-sign overlap

Shape (Alpha): commit the detection with `origin=pending` and wake the lane. The lane runs its
pre-sign stages while the verifier works, and signs only after `origin=ok`. A refusal or a
timeout signs nothing and drops the pre-reads.

Today the hook refuses `source_initiation_unknown` right after chain resolution
(`memecoin_real_runner.py` ~11314, `source_initiation_refusal`). That check runs before market
data, the reservation, safety and the quote. The reservation (one position per coin, one DB
writer) comes before safety and the quote.

### Who holds the reservation while origin is pending (one signature, J7's duplicate)

- (A) The lane reserves while pending. J7's copy of the same buy is refused ("already reserved").
  If origin then fails, a valid J7 buy has been lost. Rejected.
- (B) The lane reserves, and J7 takes the reservation over for the same source tx. That needs a
  new handoff state and two writers racing to sign one coin, so a double-sign risk. Rejected.
- (C) No reservation while pending (RECOMMENDED). The lane runs only read stages (market data,
  token safety, the entry quote) in the existing early-quote envelope
  (`memecoin_evm_early_quote.EarlyEvmQuote`: used only for the clip the reservation approves, and
  only while fresh). On `origin=ok` it re-checks `already_attempted_from_another_lane`, then
  reserves and signs. If J7 reserved first, the lane's reservation is refused by the existing
  one-position-per-coin rule. One signature, with no new state.

### What it costs when origin later fails

Per refused detection: 1-2 provider reads for market data, the token-safety reads (code, the sell
ladder, one simulation), and one buy ladder (4663 multihop: 20-40 RPC calls on the funded read
pool). There is no reservation, signature or send. Volume: 4663 refuses about half (7/14 recent,
`origin_wallet_unverified`), so about 15 wasted ladders a day on 4663, against about 130 J7
attempts a day. 56 and 8453: 0 refusals in the recent sample.

### Sign-time gain (from existing traces; small samples)

Pre-sign stages (market_data + reservation + safety_checks + quote) on signed EVM attempts since
10-01: 4663 p50 1.5-2.2 s; 56 p50 1.1-1.3 s; 8453 p50 1.7-2.6 s. With the overlap, sign time is
about max(verify, pre-sign) instead of verify + pre-sign:

| chain | today ~ verify + pre-sign | overlap ~ max(...) | gain | J7 decide (lag + total) | verdict |
|---|---|---|---|---|---|
| 4663 | 7.7 + 1.5 = 9.2 s | 7.7 s | ~1.5 s | ~3.5-4 s | still loses; Part 1 is the lever |
| 56 | 2.7 + 1.2 = 3.9 s | 2.7 s | ~1.2 s | ~2.5-3.4 s | about even |
| 8453 | 1.6 + 2.0 = 3.6 s | 2.0 s | ~1.6 s | ~6 s (n=3) | wins |

### Reusing the pre-reads: what exists, and the deciding count

The early-quote envelope cannot carry a quote across the wait. `_start_early_evm_quote` runs on 56
and 8453 only. The quote is bound to one attempt's trace and is taken at most 3 s old
(`EarlyEvmQuote.take(max_age_s=3.0)`). The 4663 wait is 7.7 s at p50. That leaves (a) warm-only:
enqueue the coin's V4 PoolKey on `memecoin_evm_key_prewarm` at detection arrival, so the lane's
quote after `origin=ok` reads a stored key.

Deciding input (Alpha): how many Relay-origin entries took a live V4 resolve instead of a stored
key? Method: watcher `relay origin ... reason=verified` lines (4663 from 10-06 14:13Z, 56 from
10-04 22:37Z, 8453 from 10-06 17:00Z, to 10-07 ~19:15Z). Each is joined to its
`onchain_wallet_detections` row and to the same coin's `memecoin_entry_attempts`. A lane attempt
is counted from -1 s to verify + 5 s; a J7 attempt from -1 s to +3 s. A live resolve is an
`eth_getLogs` call on the `v4` or `v4_multihop` rung of an `evm.quote*` substage. Quoting runs
before token safety, so this rung is the first V4 discovery.

| chain | verified buys | buys with no lane attempt | lane quotes reaching V4 | lane live resolves | J7 quotes reaching V4 | J7 live resolves |
|---|---|---|---|---|---|---|
| 4663 | 35 | 31 | 1 | 0 | 8 | 3 |
| 56 | 44 | 17 | 0 | 0 | 0 | 0 |
| 8453 | 12 | 7 | 2 | 0 | 2 | 0 |

- **0 Relay-origin lane entries took a live resolve.** The 3 lane quotes that reached V4 read a
  stored key (0 `eth_getLogs`).
- The cold walks happen on J7's attempt for the same buy. J7 starts 0.1-0.5 s after watcher
  arrival, while the verifier is still running. All 3 were on 4663: the `v4` walk took 1.1-1.7 s
  (7 `eth_getLogs`), and `v4_multihop` discovery took 3.7-3.8 s (8-21 `eth_getLogs`). A key the
  walk finds goes into the in-process cache and `memecoin_evm_v4_pool_keys`, so a lane quote after
  `origin=ok` reads it warm.
- No V4 rung appears in any 56 trace in the window, so 56 has nothing to warm.
- On 4663 the lane attempted 4 of the 35 verified buys. 3 of those are `rh_fast_lane` refusals
  `wallet_initiation_unproved`, which start before or within ~30 ms of the origin commit
  (0x707ccd5111: lane 18:19:33.442Z, commit 18:19:33.469Z). Lane and verifier race. Unread so
  far.

Verdict: (a) has no measured target, so do not build it. A warm started at watcher arrival would
lead J7's cold walk by only 0.1-0.5 s, against a 1.1-3.8 s walk. Re-count after Part 1 ships, or
if the lane starts quoting Relay-origin buys before J7.

## Edge map: accept a hash-bound outTx match while Relay's `block` is absent (operator decision; no build)

Proposal. `_verify_relay` matches the destination `outTxs[]` entry on `txHash`, `chainId`,
`status == 'success'` and `block == proof.block_number`. Since the attempt detail deployed, all 15
4663 malformed attempts (4 verifies, 19:12-19:19Z) are `KeyError('block')` at that line: Relay
already lists our tx as a success, but has not written its block. The change would read a
missing `block` as "not labeled yet", not as malformed. A `block` that is present and differs
still refuses.

Fields that still bind, all unchanged:
- Our own node: `proof.tx_hash`, `block_hash` and `block_number` equal our RPC read of the
  destination tx (`destination` hash, blockHash, blockNumber).
- Relay's outTx: exactly one entry with our hash, our chain and `success`. Its `data.data` and
  `data.to` equal our tx's input and to. Settlement `fills` lists our tx.
- The order: `orderId` is 32-byte hex and equals the last 32 bytes of our destination calldata.
  The Solana deposit instruction, read from our own Solana RPC, commits the same `orderId` and
  amount (`signed_deposit_mismatch`).
- The origin wallet: `request.user == depositor`. The depositor is a verified Solana identity of
  the source, `status_at <= origin.blockTime`. It signed the origin tx, which succeeded, and the
  payer and vault balances moved by exactly the amount.
- Solver == our receipt's payer. `acquired >= minimum`. `0 <= destination_ts - origin.blockTime <= 120`.

What Relay's `block` adds beyond our receipt: no identity. The hash fixes the signed tx, and
calldata equality plus the on-chain `orderId` fix the order. It adds (a) agreement between
Relay's indexer and our node on the block, a node-divergence and reorg cross-check, and (b) a
readiness signal that Relay finished indexing the fill.

Reorg (4663 is a sequencer L2). If our tx is reorged and re-included under the same hash in
another block, today's check refuses only when Relay re-labels before our verify ends. Block-absent
acceptance keeps our pre-reorg block number. If the tx is dropped outright, both versions accept
on our receipt read. That exposure equals the direct-wallet-paid proof, which accepts our receipt
with no Relay label and no finality wait. The copied buy is still the wallet's own. The stale
block number reaches only `source_initiation_refusal`'s block equality (both sides from our read)
and timing.

Unknown before any build. Relaxing line 131 may only move the KeyError:
1. Is the outTx `timestamp` (`destination_ts`, read on the next line) absent with `block`? So
   too `data`, settlement `fills`, and the origin `inTxs[0].block` (line 178, also a Relay block
   label). Capture first: on a malformed attempt, log the key sets of the matched outTx and of
   inTxs[0] for about 20 verifies. That is a diagnostic only, no proof change.
2. If `timestamp` is also absent, `destination_ts` must come from our own node's block header
   (the watcher already holds the head's time). Never the arrival clock.

Other items on the rule-5 list:
- Money path: yes. The verifier decides whether a Relay buy reaches the funded entry.
- Fill-path speed: shorter, no new call, unless (2) needs a header read the watcher lacks.
- Concurrency: none; a pure function.
- Absent vs zero: absent means not labeled. A present block of 0, or one that differs, refuses.
- Units: Relay `block` and ours are both ints, already compared as ints.
- Dependency failure: a Relay record that is wrong but labeled success still has to pass calldata
  equality and the on-chain `orderId`. Two outTxs with our hash still refuse (exactly one).
- Rollback: a default-off knob restores the strict match.
- Check left behind: a present-but-unequal `block` refuses, and a missing `block` with
  mismatched `data` refuses.

Expected 4663 verify time. All 23 4663 lines with attempt notes since 14:41Z, cut at the first
malformed attempt. Assumes that attempt carried everything except `block`. The cause is proven
for 4 lines and assumed for the 11 earlier ones:

| outcome | n | today p50 / p90 | block-absent p50 / p90 |
|---|---|---|---|
| verified | 8 | 4.59 / 7.81 s | ~1.80 / ~2.27 s |
| origin_wallet_unverified | 14 | 7.53 / 8.03 s | ~2.03 / ~2.38 s |

The 4 detailed verifies all ended `origin_wallet_unverified`: 8.09 to 0.31 s, 4.52 to 0.36 s,
7.84 to 2.21 s, 7.55 to 2.24 s. Each refusal would free an origin worker about 5.5 s sooner. The
remaining ~2 s is `relay_request_not_indexed` (Relay does not yet know the request), which is
Part 1's flat poll.

## A separate finding: 4663 `origin_wallet_unverified`

Half the recent 4663 verifies end `origin_wallet_unverified` after the malformed retries, about
7.8 s each. The Relay deposit's origin wallet is not a registered identity of the source wallet.
Either the identity registry (Solana origin wallets per source) is incomplete, or these buys
really are not the wallet's own. Neither Part 1 nor Part 2 changes that verdict. Worth a
read-only check of the origin wallets against `memecoin_address_registry` before any build.

### Open item: give up early on a request Relay already answered for someone else (no build)

Checked 2026-10-07 (brain finding 9f6cb3f4). The 9 refusals since 00:00Z are correct given the
registry. 7 are one coin (0x9f3938b7, 16:26-16:45Z) from 4 tracked wallets. Under each wallet's
verified Solana identity, Relay v3 `user=` lists none of those fills, so the deposits came from
other Solana wallets. They cannot pass as registered.

Each such verify still holds one of the watcher's 2 origin workers for about 7.8 s. Most of that
is the malformed/not_indexed retries before the terminal identity check, so a burst delays every
other Relay verify queued behind it.

If provable, the faster poll should stop at the first indexed answer whose depositor (or Solana
origin signer) is not one of the source's registered identities, instead of retrying it. Open
question before any build: is the depositor field trustworthy before Relay reports `success`?
`malformed_origin_evidence` with status `success` and `d1` suggests the origin record is filled
in late.

### Open item: J7 and the lane read different market caps for the same coin (no build)

On 4663 coin 0x109b383a42, 10-06 23:53:32Z, J7's attempt refused `mcap $412,198 over ceiling
$250,000`. 0.07 s earlier the on-chain lane's attempt classified the same coin micro at
`effective_mcap` $242,892 and filled. That is about 1.7x apart. J7's number is its feed's
`source_market_cap`; the lane's is its own market read. Two other J7 over_ceiling refusals of
Relay-origin buys on this coin ($369k, $330k) had no lane attempt. Over_ceiling decides about 71%
of routed buys (Alpha), so which number is right decides admissions. Gamma to grade: for pairs of
same-coin J7 and lane attempts within a few seconds, compare both caps against the on-chain cap
(supply x pool price at the source block).

## Order

1. Deploy the attempt detail (9ac89b348); Gamma tallies the late field per chain.
2. Part 1: retry schedule and pacer (small, measurable, one branch).
3. The `origin_wallet_unverified` check (read-only).
4. Part 2: the overlap, on the operator's approval, after Part 1's numbers.

Each step leaves one runnable check behind: for Part 1, a test that the poll schedule
reaches a readiness at t within poll + fetch, and that the pacer never exceeds its rate; for
Part 2, a test that a J7 reservation made during the wait makes the lane's later reservation
refuse, with zero signs.
