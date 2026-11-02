# Base 8453: detect armed sources' buys from Flashblocks, live

Status: BUILT on lambda/base-flashblocks (Alpha GO with D1(b), 2026-10-09); at Gamma. §5 records what the build settled. Author: Lambda, 2026-10-09. Order, from the operator via
Alpha: "everything turned on live, not shadow or off. Base flashblocks: design it LIVE, not a shadow. A
flashblock-detected buy by an armed 8453 source feeds the funded on-chain lane like today's watcher
detection (same gates, same dedupe by origin/tx, first sighting wins). Rule-5 map first: duplicate
detection vs the block watcher, preconf reorg (a flashblock tx that never makes the final block = a
phantom source buy; what proves it), the socket's failure modes, and the added latency (none on the fill
path)." Builds on `docs/audits/2026-10-09-memecoin-evm-fast-inclusion-options.md`, where this is Base option 2.

## 0. Answer first

The 8453 watcher process opens a SECOND socket to Base's preconfirmation endpoint and subscribes `pendingLogs`
with its own filter: a Transfer to an armed 8453 wallet. A transfer seen there goes through the SAME
`on_transfer` path as a sealed one. Its transaction and receipt are read from the preconf endpoint, with the same
decoders and gates. It writes the same `onchain_wallet_detections` row (signature `8453:<tx>:<mint>`) and wakes the runner as today.

The new row carries `source_observation_state='preconfirmed'`. When the sealed log of the same transaction arrives
on today's socket, it confirms the row (`canonical`) instead of detecting it again. If the transaction never seals,
the row is marked `retracted` (a phantom). The on-chain lane already skips `retracted` rows.

Expected gain, from MEASURED parts:
- today we see a Base buy 0.17 s after its block's timestamp;
- Flashblocks show a block's transactions up to ~1.9 s before that, with the lead depending on the transaction's
  200 ms Flashblock (probe, §1);
- our decide is 3.06 s p50, so an S+2 landing becomes S+1 when the lead covers the leftover of the 2 s block.

The realised lead per buy is recorded on every row (`preconf_lead_s`) and graded.

## 1. Probes (MEASURED, 2026-10-09 ~14:47-15:05Z, public endpoints, under 30 calls in total)

- `wss://mainnet-preconf.base.org`:
  - `eth_chainId` returned `0x2105`.
  - `newHeads` (one per 2 s block, about 0.15 s after its timestamp) and `pendingLogs` were both accepted.
  - The first pending log of each of 4 blocks arrived 1.90-1.94 s before that block's header.
- A pending log carries the final `blockNumber`, `transactionIndex` and `logIndex`, and **`blockHash = 0x00…00`**.
- `https://mainnet-preconf.base.org` at first sight, 94-113 ms per read:
  - `eth_getTransactionReceipt` answered status `0x1` with all logs and the pending `blockNumber`. Its
    `blockHash` is `0x00…00` while the block is building and the real hash once sealed.
  - `eth_getTransactionByHash` answered with `blockHash = null` while pending.
- Python's `urllib` draws HTTP 403 from both Base hosts; the runner's httpx client with `evm_wallet._UA` does not.
  - `base-rpc.publicnode.com` answered a 3 s old receipt with "Archive requests require a personal token".
  - So the sealed checks below use `mainnet.base.org` or `mainnet-preconf.base.org` once sealed, never publicnode.

## 2. Design

1. **Knob.** `MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS`, a comma list, default empty. Malformed means empty. Only `8453` is
   accepted: the endpoint and the identity are pinned in code, and the block hash semantics are Base's.
2. **Socket.** In the 8453 watcher (`scripts/research/onchain_evm_wallet_watch.py`, `run_push`), one more WebSocket on
   its own thread:
   - `eth_chainId` must be `0x2105`;
   - subscriptions: `pendingLogs` `{"topics": [Transfer, null, [watched]]}` and `newHeads` as the beat;
   - same roster as the sealed socket (`addresses_fn`), resubscribed on a roster change;
   - an empty roster subscribes nothing (the wildcard rule).
   Logs go to `on_transfer(log, preconf=True)`.
3. **Reads.** `push_detection(..., preconf=True)`:
   - tx and receipt from `https://mainnet-preconf.base.org` (httpx, the runner's UA), the two in parallel as today;
   - the null-read retry runs to the same budget;
   - `_tx_unmined` is not applied: a null `blockHash` is the expected preconf shape. Instead the receipt must have
     status 1, a `blockNumber` equal to the log's, and a transaction index equal to the tx's.
4. **Decoders.** `direct_buy_from_receipt` is unchanged; it reads no block hash. `read_relayer_purchase` (shared) gets
   `preconfirmed=True`, honoured only on chain 8453:
   - its two hash equalities (tx vs receipt, log vs receipt) become equalities of `blockNumber` and
     `transactionIndex`, with every block hash null or zero;
   - every other check is unchanged.
   Every Base buy since 09-18 went through Relay (the Base lane spec counted 121 of 121), so without this the path detects nothing.
5. **Row.**
   - Same columns as today.
   - `source_observation_key = tx hash`, `source_observation_state = 'preconfirmed'`.
   - `block_time` is the pending block's timestamp.
   - The valuation JSON carries `preconf: {first_at, block_number, tx_index}`.
   - No `source_fill`: it binds a block hash, and its readers fall back exactly as for a row without one.
6. **Dedupe.** Both sockets share `on_transfer`: the `inflight` set, the `decided` window by tx hash, then
   `INSERT OR IGNORE` on the signature. The first sighting wins.
   - **A sealed log for a transaction already decided from preconf is a CONFIRMATION, never a second detection:**
     `UPDATE … SET source_observation_state='canonical'`, the sealed block hash and `preconf_lead_s`
     (sealed arrival − preconf arrival) in the valuation JSON.
   - A sealed log whose block number or log index differs from the preconf record: `canonical`, with
     `source_slot` updated and the move noted.
   - A transaction seen sealed first, with the preconf socket late or dark: today's path, `source_observation_state` NULL.
7. **Phantom proof (watcher thread, off any fill path).** A `preconfirmed` row whose block S has sealed (the beat's
   head ≥ S + 1) with no sealed log of it gets ONE receipt read on `mainnet.base.org`:
   - null: `retracted` (a phantom), page once;
   - in another block: `canonical` with `source_slot` moved;
   - a read error: retried at the next head, at most 5. Then `preconf_unresolved`, paged, and the lane treats it like
     `preconfirmed`.
8. **Lane and runner.**
   - The on-chain lane already skips `retracted` rows before routing.
   - Its held-row retraction re-check is limited to 57073 today; it becomes {57073, 8453}.
   - **Decision D1 for Alpha** (see §3, item 2): what a sign-time check does with a `preconfirmed` row.

## 3. Rule-5 map

1. **Money path: yes.** A preconf row feeds the funded on-chain lane, so it reaches entry. Every funded gate runs unchanged, on
   the same row shape. The only new thing a gate can see is an earlier `detected_at`, which is the point.
2. **Preconf reorg (the phantom).**
   - **What proves the source buy:**
     - at first sight, the sequencer's preconfirmed receipt (status 1, logs binding the wallet's delta, Relay proof);
     - **final:** the sealed log of the same transaction with a non-zero block hash (`canonical`);
     - **phantom:** block S sealed, no sealed log, and the canonical receipt null (`retracted`).
   - Base targets a Flashblock reorg rate under 0.1% (CLAIM). At ~4.5 armed Base buys a day that is about one phantom
     in 200+ days, if the claim holds. Every row records its state, and the grader counts them.
   - **D1, options:**
     - (a) **Trust the preconfirmation.** A phantom is caught only if it is retracted before the lane routes it.
     - (b) **(a), plus one local ledger read of the row's state just before signing:** refuse `retracted`; proceed on
       `preconfirmed` or `canonical`. Cost: one primary-key read on the funded ledger, p50/p90 measured in the build.
       Retraction lands about 2 s after the source's block (head S+1), and our sign is about 2-3 s after first sight, so
       (b) catches most phantoms.
     - (c) **Require `canonical` at signing.** No phantom can be bought. The gain is unchanged while decide (3 s) exceeds
       the lead (≤ 1.9 s), but a refusal at "not yet sealed" loses a buy whose sealed log is late.
     - **Recommend (b).**
   - **If a phantom is bought anyway:** a real position of ours on a real coin, at a price inside our chase cap. Its row
     says `retracted`, and the position's attribution carries it. Exits run as normal.
3. **Speed on the fill path.**
   - The runner adds nothing under (a). Under (b) it adds one local primary-key read before signing, measured.
   - Every new network read runs in the watcher process: two preconf reads per armed-wallet transfer at ~100 ms, in
     parallel, and one sealed read per unconfirmed row at its next head.
4. **Concurrency.**
   - The preconf socket's thread hands logs to the same `on_transfer`, whose lock guards only the claim (as for the gap
     backfill thread).
   - The confirm and retract `UPDATE`s run on the watcher's own connection, one short write each.
   - No env var, module attribute or shared connection is mutated.
5. **Absent, zero and unknown.**
   - A zero or null block hash is "not sealed yet". It is never written as a block hash, and never compared as one
     except on the Relay preconf branch, which compares block numbers instead.
   - `blockTimestamp` is the pending block's (seal) time, so `detect_latency_s` (detected_at − block_time) goes
     negative by up to 2 s. Fill timing already treats a negative detect as unknown. The lead is stored separately
     as `preconf_lead_s`.
   - A missing pending field (tx hash, block number) skips the log with a counted reason.
6. **Failure modes of the preconf socket and host.** The sealed socket is untouched, so coverage never falls below today.
   - **Refused subscription or wrong chain:** the preconf thread stops and logs it once.
   - **Error close or rate limit:** back off 60 s or more (rule a53667ee).
   - **Deaf but confirmed** (trap a66c5223):
     - beat: a `newHeads` frame at least every 10 s, else reconnect;
     - delivery: a sealed armed-wallet transfer that the preconf socket never delivered counts as `preconf_missed`.
       Three in a row force a reconnect, and the count is in the beat.
   - **Preconf host read errors:** today's `read_error` outcome. The transaction stays undecided, so its sealed log
     detects it normally.
   - **Partial answer** (receipt without logs, or block number ≠ log's): refused, then detected again on the sealed log.
   - **Wrong answer** (a preconf receipt that later differs from the sealed one): caught at confirmation (§2.6) and noted.
   - Base plans to retire Flashblocks in the Denim fork in favour of 200 ms canonical blocks (CLAIM). The sealed path
     then gives the same lead, and the knob is turned off.
7. **Units and identity.**
   - Chain 8453 is checked by `eth_chainId` on every connect.
   - Hashes and addresses are lowercased.
   - Blocks are ints, and Base blocks are 2.0 s.
   - Every time is host UTC: the watcher's `detected_at` is `datetime.now(timezone.utc)`.
   - Signature identity is unchanged (`8453:<tx>:<mint>`), so J7 and the runner's existing cross-lane rules see one buy.
8. **The sample.**
   - Graded per wallet and pooled, at 30 confirmed rows: `preconf_lead_s` p10/p50/p90, the landing gap (fill timing,
     S-offset) against the pre-change 6 fills (S+2 p50), `retracted` and `preconf_unresolved` counts, and `preconf_missed`.
   - Only armed 8453 sources, and our own wallet is not in the roster.
   - **Stop lines** (Gamma, stamped 2026-10-09T15:21:33Z after T8 = 15:20:06Z, with 0 8453 detection rows;
     agreed by Alpha). Each one means **recommend off**; the operator decides.
     1. **Phantom buys:** 2 or more funded positions opened on a row that is later `retracted`.
     2. **Deaf instrument:** `preconf_missed` forces 3 or more reconnects in one hour, in each of 3 consecutive
        hours. Page at the first such hour.
     3. **No lead:** at 30 confirmed rows, `preconf_lead_s` p50 is under 0.2 s.
     - Otherwise it stays on. The landing gap against the 6 pre-change fills is reported labelled before/after
       (no concurrent control, n=6 baseline).
9. **Rollback.**
   - Empty `MEMECOIN_ONCHAIN_EVM_PRECONF_CHAINS`: no preconf socket, today's behaviour.
   - Rows already written as `preconfirmed` stay valid inputs; the lane treats them as today's rows.
   - Half-applied (new watcher, old runner): (a)'s behaviour, with retraction honoured for unrouted rows only.

## 4. Build steps (TDD, after Alpha's D1)

1. The knob and the preconf thread, against a fake socket: subscription, beat, backoff, roster resubscribe.
2. `push_detection(preconf=True)` and the Relay decoder's preconf branch, on real preconf fixtures
   (receipt and tx captured at first sight, plus the sealed copies).
3. Confirm, move and retract in `on_transfer`, the head-driven phantom check, and the row state.
4. The lane's held-row retraction for 8453, then D1 in the runner.
5. Mutation proofs for each identity check and the dedupe, and the p50/p90 of the preconf path and the sign-time read.

Then Gamma, then Alpha deploys with the knob on.

## 5. As built (2026-10-09)

- **Publication scope.** A preconfirmed row is published only when its Relay buy is proved from the user's own
  stored deposit (Relay option A, `MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS` must list 8453).
  - Relay's API cannot have indexed an unsealed fill, and a direct buy's initiation proof binds a block hash.
  - Everything else (no buy yet, needs Relay, direct, missing block) records nothing and stays undecided, so its
    sealed log detects it as today.
- **Stored identity.**
  - The initiation proof of a preconfirmed row carries the receipt's `0x00..0` block hash. `confirm` rewrites it to
    the sealed hash, and the runner's existing format check accepts both.
  - No `source_fill` is written without a real block hash: without that guard a preconfirmed row got one bound
    to `0x00..0`.
  - **2026-10-10 correction:** a moved canonical source updates both `source_slot` and nested initiation
    block/hash atomically, preserving the original preconf block as provenance. Receipt resolution
    requires the requested transaction hash, valid block/hash, explicit status 0/1 and any supplied
    chain identity. Partial or wrong receipts remain unknown.
- **Phantom rule.**
  - A canonical receipt that reverted: `retracted` at once.
  - A null canonical receipt: `retracted` only on the second null, at a later head, so one lagging node cannot
    make a phantom.
  - 5 failed reads: `preconf_unresolved`.
  - Each is paged once (`onchain_preconf`).
  - **2026-10-10 correction:** only strictly later sealed heads advance null proof; preconf liveness
    and duplicate/out-of-order heads cannot count. Subscription replies are demultiplexed by ID.
- **D1(b) read**, measured on the real ledger, 400 primary-key reads:
  - per-thread cached read-only handle: p50 0.002 ms, p90 0.002 ms;
  - fresh handle per read: p50 1.9 ms, which is why the handle is kept.
  - The busy wait is bounded to 50 ms (the helper's default is 30 s).
  - An unreadable ledger proceeds and is logged; only `retracted` refuses (`source_retracted`).
  - An executor without the `presign_check_fn` seam refuses a Base on-chain entry (`preconf_presign_unavailable`).
- **Threads.** The preconf socket, two decode workers and one settle worker. Each worker thread has its own ledger
  handle, and the socket thread holds none: it reads the watcher's in-memory roster.
  - **2026-10-10 bound:** at most 128 admitted decodes and two-second queued age, with exact source-key
    dedupe and immutable arrival clocks. Canonical resolution coalesces to one newest pending sealed
    head on its separate worker. Full/expired preconf work retains the ordinary sealed fallback.
