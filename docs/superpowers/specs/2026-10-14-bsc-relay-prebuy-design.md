# BSC 56 Relay pre-buy: design

Status: approved by Alpha, 2026-10-14 ("go"; inline allowed on 56, §4). Operator decision, same as 4663: "live now, small size". Knob `MEMECOIN_BSC_RELAY_PREBUY`, default off.

It extends the 4663 pre-buy (`docs/superpowers/specs/2026-10-14-rh-relay-prebuy-design.md`, branch `lambda/rh-relay-prebuy`, Gamma PASS at d99e38846). Every rule there holds unless this document says otherwise.

## 1. Why

Operator alert ref 3cbba5a1, 小王子 `0x71b58f07…7777`:
- bigwarzeth deposited $4,000 to Relay; our deposit socket saw it at 13:06:27.947Z (order `0x02effa2435…`).
- The 56 source fill `0x62a34dd6…` (block 127599765) reached our watcher at 13:06:30.422Z. That is a 2.55 s lead.
- Our BSC lane sent 184 ms after deciding and landed 4 blocks behind. Its pool was the coin/WBNB PancakeSwap V2 pair `0xe5818c71…`, its safety path was inline, and it reverted on min_out.
- J7 then refused at +35% above the source. The coin ran 35% in about 2 s.

On 56 the Relay fill takes longer than on 4663: two shared-store claims measured 1.24 s and 2.55 s from deposit to watcher (n=2), against a p50 of 0.92 s on 4663. A pre-buy has a real lead on 56.

## 2. What is reused unchanged

- **Deposit socket and Relay poller:** in the 4663 feed process.
  - The socket already watches 56-armed users: `MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS=4663,8453,56`.
  - Poller limits: 8 polls at 6/s, stop on a 429 or 5xx, the not-indexed and indexed-after-fill rules, and the registry re-check (p50 1.94 ms).
- **Request match:**
  - The recipient must be the uid's verified wallet on the destination chain, with the uid among the deposit's verified users.
  - `currencyIn` is the deposit's exact Solana USDC; the status is unsettled with no `outTxs`; a quote-asset `currencyOut` is refused.
- **Pre-buy rules:**
  - $20 clip, cap = spot × 1.05, refused above 5% impact.
  - 4 sends an hour, a 20-fill pilot, a stop after 3 consecutive reverts that survives a restart, and the fast lane's stop file.
  - The full funded hook with every gate. Chase is the only exemption: it compares against our spot.
- **Initiation proof** `relay-request-prebuy-v1`, now accepted on 56 too:
  - It binds `network_id` 56 and `request_out_chain` 56.
  - The origin is `rhprebuy:56:<order>:<mint>`.
  - It needs `MEMECOIN_BSC_RELAY_PREBUY` on.
- **Defer and re-judge:**
  - While a pre-buy of the coin is in flight, the source fill is not decided on the BSC lane (`relay_prebuy_in_flight`, no attempt row) or on the on-chain lane (it waits on the attempt row).
  - The J7 one-position refusal behind a `relay_prebuy` reservation re-asks every 1.0 s for 60 s. That rule is already chain-agnostic.
  - A pre-buy that acquired nothing hands the source fill back; one that filled, or is ambiguous, keeps refusing through one position per coin.
- **Records and counters:** `rh_relay_prebuys` and its source-fill disposition, per chain:
  - 56's live in the BSC lane's own SQLite;
  - the source outcome is settled by one Relay call at 30 s (the 4663 feed sees no 56 fills).

## 3. What is new on 56

### 3.1 Feed side: route the match to the BSC lane
- The pipeline's roster is per chain: `{chain: evm_watched_owners(conn, chain)}`, refreshed every 10 s as 4663's is.
- The not-armed check counts only users armed on a chain whose pre-buy knob is on.
- A matched request bound for 56 does not decide in the feed, because the feed holds no 56 state. It sends one `relay_prebuy_v1` datagram to the BSC lane's socket (`memecoin_bsc_fast_lane.default_address()`) carrying:
  - `chain` 56, `tx_hash` = order id, wallet, uid, handle, mint, `paid_usd` (the deposit's USDC), `arrival_utc`;
  - the deposit and request fields, and the proof.
- There are no route or price fields. The runner prices it.
- No warm datagram: the runner's worker notes the coin in the BSC safety cache at priority -1 when the pre-buy arrives (P2 for 56, about 2 s ahead of the source).

### 3.2 Runner side: `BscDispatch` → `BscRelayPrebuyWorker`
- **Authentication:** the datagram is accepted only from the 4663 feed detector's pid, read from its heartbeat (the 4663 lane's rule), not the 56 watcher's.
- **Worker:** its own thread and connection, one pre-buy at a time (busy means refused).
- **The pre-buy lane:** `PrebuyBscLive(BscLive)`, built from the live BSC lane's own arguments, with the pre-buy's clip and caps and pilot counters seeded by `signal_source='relay_prebuy'` and `network_id='56'`.
  - Execution is `BscLive.execute_reserved` as today: the wallet exec lock, the nonce floor, write-ahead identity and the builder bundle submission. No new submission path.

### 3.3 Price state on 56: the lane's own host, at one block
- The BSC tape (`memecoin_bsc_v2_live`) keeps Syncs only for held pools and hop pairs, so it holds no current reserves for an arbitrary coin pair.
- The pre-buy reads them itself, on the lane's own host (`LaneHost`), in ONE batch:
  - `eth_blockNumber`;
  - `getReserves` on the coin's PancakeSwap V2 pairs: coin/WBNB, and coin/q for each `MEMECOIN_BSC_FAST_LANE_HOP_QUOTES` q (USDT, USD1, …);
  - `getReserves` on each WBNB/q hop pair.
- All of these are read at `latest`, the same instant to within one batch.
- A host that does not batch, any error, or a malformed answer means no buy.
- BNB/USD comes from the tape's `quote` row within its 60 s TTL; otherwise no buy.
- Facts (decimals, supply) and market stats come from the BSC safety cache.

### 3.4 Route: V2 only, the cheapest of the coin's pairs
- **Candidates:** coin/WBNB direct, and coin/q through WBNB/q. A pair with a zero side is skipped.
- For each candidate, the $20 clip is priced through the router's own path, using `v2_amount_out` (0.25% fee) on the hop and then on the coin leg.
- Impact = (paid ÷ est_out ÷ spot − 1) × 100.
  - Spot is the coin leg's reserve ratio.
  - On a hop, spot is converted to WBNB at the hop's fee-inclusive spot, as `min_out_for` does.
- The lowest-impact candidate is used. Above 5%, no buy.
- **No V2 pair → `prebuy_refused:no_v2_route`.** That covers a flap curve before graduation, V3-only coins and anything else: no unknown route kinds.
- **The payload:** the BSC lane's native shape: pool, quote, coin1, r_coin, r_quote, hop, cap.
  - `state_kind` is `prebuy_lane_host` and `seq` is the block read.
  - `src_in`/`src_out` are set to the clip and its est_out. The pre-buy event overrides the chase reference, so these never price the hook.
- **Tax:** unknown before the source buys, so it is taken as 0.
  - A taxed coin then delivers less than min_out and reverts (gas only, and it counts toward the 3-revert stop).
  - The safety check's sell simulation still applies.

### 3.5 Event (`PrebuyBscEvent`)
- `signal_source='relay_prebuy'`, `delivery_lane='bsc_relay_prebuy'`, `fomo_event_id='rhprebuy:56:<order>:<mint>'`.
- `their_size_usd` = the deposit's USDC.
- `their_price` = spot USD per whole coin; `their_market_cap` = chain supply × spot.
- Chase reference `{'reason': 'relay_prebuy_no_source_fill'}`.
- `BscLive._handle` builds `BscLaneEvent` inline today. A one-line `_lane_event` seam lets the pre-buy lane override it.

### 3.6 The collision guard (new; Alpha's late-index edge case)
- The BSC lane notes, in memory, each source signal it receives as (uid, mint) → time.
- If the source's own fill for this user and coin has already reached the BSC lane, the pre-buy is refused `prebuy_refused:source_fill_seen`. The normal copy owns it.
- Relay's late indexing is caught earlier as `indexed_after_fill` (success or `outTxs`). This guard covers a fill the watcher saw before Relay reported it.

### 3.7 Defer and re-judge on 56
- **BSC lane:** `BscLive._handle` returns `relay_prebuy_in_flight` before the hook while the worker holds the coin. It writes no attempt row.
  - The watcher's `bsc_fast_lane_sent` hand-off still makes the on-chain lane wait for the lane's row, for up to 2 s. Absent that row, the pre-buy's wait below takes over.
- **On-chain lane:** `_route` on 56 checks `relay_prebuy_in_flight(conn, '56', mint, …)`, the 4663 query with the chain as a parameter, before 56's `bsc_native_pending` checks. It waits by row state within the detection's freshness, then judges as today.
- **J7:** already covered by the chain-agnostic transient.
- **Counters:** `source_fill_refused_by_prebuy:<outcome>` and `source_fill_rejudged_after_prebuy:<outcome>` in the BSC lane beat, plus the `rh_relay_prebuys` row in the BSC lane DB.

## 4. Safety on 56: inline allowed (Alpha, 2026-10-14)


On 56 the pre-buy may run the BSC lane's own inline check for a cold coin. It is bounded by `MEMECOIN_BSC_FAST_INLINE_SAFETY_DEADLINE_S` on the dedicated inline host, as the lane's own cold path is.
- A cached verdict counts as `prebuy_cached`; an inline one as `prebuy_inline`.
- If the source's own fill reaches the BSC lane while the check runs, the pre-buy yields (`source_fill_seen`, §3.6). It checks once more just before its hook.

Why, measured:
- The incident coin was cold: the lane's safety path for its source fill was `inline`.
- The BSC lane's live rows since 10-07 split 104 cached against 106 inline.
- The 56 lead is 1.24 to 2.55 s (n=2). The 56 inline path costs inputs p50 207 / p90 265 ms and safety p50 358 / p90 631 ms (n=4).

4663 stays warm only: its lead is shorter than an inline check.

## 5. Edge cases (CLAUDE.md §5)

1. **Money path:** yes, a new 56 entry trigger. Every gate, the $20 clip, the caps and the stop apply. Knob off by default.
2. **Speed on the fill path:**
   - The BSC lane's listener gains one dict lookup per signal: the in-flight check and the collision note.
   - The pre-buy reads its prices on its own worker thread, on the lane host's pooled client, one batch per deposit.
   - The live lane's own reads are untouched. Batch latency gets a p50/p90 at canary.
3. **Concurrency:**
   - The worker has its own lane instance with its own thread pools and read locks.
   - It shares the file-based wallet exec lock and the nonce floor with the BSC lane, as `SharedRouteWorker` does.
   - The (uid, mint) note is a bounded dict written by the listener and read by the worker.
4. **Absent / zero / unknown:**
   - A pair with no answer is unknown, so no buy; a zero reserve side means that pair is skipped.
   - No BNB/USD, no facts or no stats means no buy.
   - Tax is unknown and taken as 0 (fail closed through min_out).
5. **Dependencies:**
   - A lane host that is down, rate-limits or answers malformed: no buy, counted.
   - The rh feed down: no 56 pre-buys, today's paths unchanged.
   - The BSC lane socket not bound: the datagram send returns False and is counted on the feed.
6. **Units:**
   - Reserves are in token atomic; the cap is quote atomic per coin atomic, as the BSC lane's.
   - A hop is converted at its fee-inclusive spot.
   - USDC has 6 decimals; BNB/USD is a float from the tape.
7. **Sample:** these fills are tagged `relay_prebuy` with network 56 and graded apart from 4663's and from copy fills.
8. **Rollback:** `MEMECOIN_BSC_RELAY_PREBUY=0`, the stop file, or the self-stops.

## 6. Tests (TDD)

**Feed**
- A 56 request reaches the BSC lane as one datagram the lane accepts.
- A 56 request with the knob off sends nothing.
- A 56 wallet in the 4663 roster only is refused.
- The not-armed check is per chain.

**Runner**
- Spot read: one batch; a non-batching host, an error, or a malformed answer means no buy.
- Route: the lowest-impact V2 pair; a hop through USDT; no V2 pair is `no_v2_route`; impact over 5% is no buy.
- The payload passes the BSC lane's native build: min_out at the cap, and the hop rule.
- Collision guard: a source fill seen first means no pre-buy.
- Warm only, or inline per Alpha's decision.

**Dedup on 56**
- Reverted, then the source fill is copied by the BSC lane.
- Aborted in flight, then copied by the on-chain lane.
- Landed, then refused.
- `submitted_unknown`, then refused.
- J7 in the pre-sign window re-asks and copies.

**Initiation**
- The 56 proof is accepted only with the 56 knob and matching identities.
- A 4663 proof on 56 is refused, and the reverse.

**Caps:** per-chain counters; 56's streak does not stop 4663's.

## 7. Not in this change
- The flap curve and V3 routes.
- Chains other than 4663 and 56.
- New exit rules.

## 8. As built (2026-10-14, branch `lambda/bsc-relay-prebuy`)
- **Price read (§3.3):**
  - One `LaneHost.batch` through the lane's inline seam, inside the 56 inline deadline.
  - It reads `eth_blockNumber` and the coin's `decimals()` (a cold coin has no cached facts), plus `getReserves` on each candidate pair, all at `latest`.
  - A coin without readable decimals is `no_decimals`.
- **Collision guard (§3.6):**
  - A source signal for (uid, mint) within the last 60 s (`SOURCE_SEEN_LOOKBACK_S`) means the pre-buy yields.
  - It checks before any read, again after pricing, and again right after the inline safety check inside the hook. That last check returns `source_fill_seen`, which releases the unsigned reservation.
  - **Not handled:** a source fill that reaches the lane in the few ms between that last check and the send.
  - **Who copies after a yield (Gamma LOW 3):** "the normal copy owns it" holds only for a source fill that reached the lane BEFORE the pre-buy was offered.
    - A source fill arriving during the flight is stepped aside by the BSC lane (`relay_prebuy_in_flight`).
    - When the pre-buy then yields, reverts or aborts, the copy is the on-chain lane's (it waits on the row) or J7's re-ask, the slower paths, not the fast lane's.
- **Shared inline seam (Gamma LOW 4):** the pre-buy's batch and inline check run through the live lane's inline seam: one `PacedHttp`, its 429 pause and its 3-in-a-row dead marking. So pre-buy failures can pause the live lane's inline path. The load is small; it is noted, with its own pacer offered as a follow-up.
- **Background preparation (Gamma LOW 5):** the pre-buy lane's `entry_active` is its own, so the live lane's background preparation is not paused during a pre-buy's inline check.
- **Counters:** `prebuy_cached` / `prebuy_inline` (by the lane's safety path), `prebuy_refused:<reason>`, `prebuy_sent`, and the source-fill dispositions. They go in `bsc_lane_beat.lane_json["prebuy"]`.
- **Records:** `rh_relay_prebuys` in the BSC lane DB, written from the priced payload: `route_kind` is `v2_native`, plus the block, the route pool and the impact.
- **Registry:** `memecoin_rh_relay_prebuy.ACTIVE` is per chain (`{"4663": …, "56": …}`), and `note_source_fill` takes the chain.
- **Not changed:** pre-approval. 56 is not in `MEMECOIN_EVM_PREAPPROVE_CHAINS`.

### 2026-10-14 amendment: enforced stop and inherited budgets

BSC startup applies data/memecoin_rh_fast_lane.stop before accepting work and monitors it off the signal path at one-second cadence. The prebuy worker refreshes the live stop state before dispatch, and copied/private execution workers check their live owner's stop state before the funded hook. This repairs the promised fast-lane control; the global kill switch remains independent. The shared Relay request-start pacing and durable restart send-count contract are the 2026-10-14 RH Relay prebuy amendment. Default enablement, hourly send cap, pilot fill cap and consecutive-revert stop are unchanged.
