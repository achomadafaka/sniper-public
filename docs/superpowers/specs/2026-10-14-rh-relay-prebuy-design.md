# 4663 Relay prewarm (P2) and pre-buy (P3): design

Status: design approved by Alpha, 2026-10-14. Operator order for P3: "Live now, small size" (via Alpha).
Both knobs default off.

## 1. Why

Banksy / koy (alert 6d7d05e5, 2026-10-14 11:21Z):
- koy deposited $750 USDC to Relay on Solana. Our deposit socket saw the deposit at 11:21:15.579.
- Relay's request record appeared at 15.877.
- The 4663 fill reached our feed at 16.394.
- Five other buys landed in blocks 724–726 and moved the price about +34%. The slow path refused at 1.29× the post-trade price.

`GET /requests/{v2,v3}?orderId=<order id from the deposit>` names the destination mint
(`metadata.currencyOut`) and the recipient while the request is still unsettled. That gives a lead of about 260 ms over our feed.

- **P2 (prewarm):** use the lead to warm the 4663 lane for that coin. No decision changes.
- **P3 (pre-buy):** buy $20 of the coin before the source's own fill lands. Every funded gate applies, except the source post-trade reference, which does not exist yet.

## 2. Rules (Alpha's terms)

### 2.1 Trigger (P2 and P3)
1. A deposit proof from `DepositProofs.put`: a signed Solana deposit into Relay's depository, from a verified, conflict-free depositor of a user armed on 4663 (`deposit_from_frame`).
2. The poller runs on its own thread, `rh-relay-poller`, in the feed process. It is never the decide or detector thread.
   - At most 8 polls per deposit, 166 ms apart (6/s), on the poller's own burst rate.
   - It stops on the first 429 or 5xx and counts it. It never retries beyond those 8 polls.
3. A request is accepted only when all of these hold:
   - exactly one request;
   - `recipient` == that user's verified 4663 wallet (`memecoin_address_registry`, network 4663, verified, conflict-free);
   - `currencyOut.chainId` == 4663, and `currencyOut.address` names the mint;
   - `currencyIn` is Solana USDC, and its amount == the deposit's atomic amount;
   - the status is unsettled (v2: pending or depositing; v3: also waiting or submitted), with no `outTxs`.
4. A request at `success`, or one carrying `outTxs`, was indexed after the fill. Then there is no pre-buy: the normal path applies.
5. Anything missing, malformed or unknown is a reject, never a match.

### 2.2 P2: prewarm (`MEMECOIN_RH_RELAY_PREWARM`, default 0)
- Detector side: `learn_offer(mint)`, which loads the coin's pools into the decide's `TapeState`. P3 does this too.
- Runner side: one `candidates` datagram (the detector's existing kind, so the lane needs no new code) holding the coin at priority -1 with its route. The lane registers it (`cache.note_candidates`), and the existing worker warms safety and stats under today's TTL rules.
  - As built: the warm is sent only when the tape already has a route. A warm without a route could cache a failing safety verdict, because the safety quote needs the route. With no route it is counted `warm_no_route`.
- No reservation. No decision input.

### 2.3 P3: pre-buy (`MEMECOIN_RH_RELAY_PREBUY`, default 0)
0. **Warm only (Alpha, 2026-10-14, on Gamma's MEDIUM 1).**
   - A pre-buy runs only when the lane's safety cache already holds a fresh verdict for the coin.
   - An absent or stale verdict is `prebuy_skipped:cold_safety`. It is counted and recorded, and it is skipped before any hook or reservation.
   - The pre-buy path has no inline safety or facts read.
   - Gamma's measure: a cold send leaves at found+400 to 850 ms, against a source block at about found+450 to 600 ms. It loses the race, and after the source 28% of Relay-origin buys move the price more than 5%.
   - P2 still warms the coin for the normal copy.
1. **Route and state, on the poller thread:** `TapeState.route(mint)` picks a native, WETH or bridge V4 route. Tape state is the pool's latest row.
   - The tape must be current: lag ≤ `MEMECOIN_RH_FAST_TAPE_MAX_LAG_BLOCKS`.
   - No unresolved 4663 `tick_gaps` row may overlap (the pool's last row block, the tape head]. If one does, no pre-buy.
   - There is no pool-age bound (Alpha option 1). `state_age_blocks` and the route pool are recorded.
   - A hook not listed in `MEMECOIN_RH_FAST_HOOK_OUTPUT_TAKE_BPS` (an unknown take), or an unknown V4 key, means no buy:
     `prebuy_refused{unknown_hook}`. A known hook is allowed (Alpha 2026-10-14; the lane already buys hooked pools).
     A dynamic-fee pool has no deductible LP fee, so its impact estimate reads slightly optimistic. That optimism
     is bounded by min_out.
2. **Price:** spot is the pool's current price on the tape. The $20 clip is quoted through the decide's own swap math, giving `est_out`.
   - average price = clip_wei ÷ `est_out`; impact = (average price ÷ spot − 1) × 100. If impact > `MEMECOIN_RH_RELAY_PREBUY_MAX_IMPACT_PCT` (5), no buy.
   - cap = spot × 1.05; `min_out = min_out_at(clip_wei, cap)`.
   - This min_out is the protection: we never pay more than 1.05× the tape spot, or the swap reverts.
3. **Datagram `relay_prebuy_v1` to the runner lane**, authenticated by the detector pid exactly like the signal datagram. It carries:
   - the route fields `lane_buy_tx` needs, cap_num and cap_den;
   - `eth_usd` and the snapshot inputs;
   - the deposit fields (signature, slot, depositor, atomic amount, uid, arrival);
   - the request fields (id, recipient, `request_found_at`);
   - `state_age_blocks`.
4. **Runner:** `LaneDispatch` hands `relay_prebuy_v1` to `RelayPrebuyWorker` (its own thread, the `SharedRouteWorker` pattern).
   - It works on a `copy.copy(live)` with its own connection.
   - It runs today's funded hook in full with the lane's executor (`execute_reserved`: wallet exec lock, nonce floor, pipelined 4663, write-ahead identity before any network I/O). No new submission path.
   - It keeps its own pilot counters, seeded from the ledger by `_seed_pilot(signal_source="relay_prebuy", network_id="4663")`:
     - fills ≤ `MEMECOIN_RH_RELAY_PREBUY_PILOT_FILLS` = 20;
     - sends ≤ `MEMECOIN_RH_RELAY_PREBUY_MAX_PER_HOUR` = 4.
   - At 20 fills it stops itself and alerts.
   - After 3 consecutive reverts it also stops itself and alerts. That count is persisted in the lane DB, so a restart cannot reset it.
     - As built: the run is read from `rh_relay_prebuys` at start. A missing file means no history. A file that cannot be read is a stop.
     - To resume after a revert stop, the operator writes a row with reason `operator_reset`. A fill also ends the run.
   - The fast lane's stop file stops it too.
   - As built: the worker takes one pre-buy at a time. A second one that arrives while it is busy is refused `prebuy_busy`, never queued.
   - Release reasons are `rh_prebuy_*`. The submission backend name stays the lane's.
5. **Event (`PrebuyEvent`):**
   - `signal_source='relay_prebuy'`, `delivery_lane='rh_relay_prebuy'`, `fomo_event_id='rhprebuy:4663:<order_id>:<mint>'`.
   - `their_size_usd`: the deposit's signed USDC (atomic ÷ 10^6). Never the API's amountUsd.
   - `their_price`: spot in USD. `their_market_cap`: chain supply (lane facts) × spot × `eth_usd`.
   - `evm_chase_reference = EvmChaseReference('4663', post={'reason': 'relay_prebuy_no_source_fill'})`: the chase gates compare against spot. This is the only exemption.
   - `source_initiation`: proof version `relay-request-prebuy-v1`, holding:
     - the deposit (signature, slot, depositor, atomic amount, order id, uid);
     - the request (id, recipient, currencyOut mint and chain, currencyIn amount).
6. **Initiation guard:** `source_initiation_refusal` adds `relay_prebuy` / `rhprebuy:` to its guarded set. Today an unknown signal source returns None, which would let a pre-buy pass with no proof.
   - It accepts `relay-request-prebuy-v1` only for `rhprebuy:4663:` origins and only with the knob on.
   - It requires the same identity checks as the deposit proof: the depositor is the uid's verified Solana identity, recipient == the uid's verified 4663 wallet, the mint matches, and the amount matches.
   - Every other shape refuses.
7. **Every other gate unchanged:** safety, mcap, volume, source band, one position per coin, re-entry cooldown, exposure.
8. **The hook's native-lane cases (as built):** the hook special-cases `rh_fast_lane` in two places. Both now include `relay_prebuy`:
   - no early shared-route quote. Before the fix it started one for a pre-buy; a red test showed it.
   - fixed pilot sizing, with no native-price term.

### 2.4 Dedup: defer and re-judge (Alpha, 2026-10-14, on Gamma's MEDIUM 2)
The rule: no pre-buy outcome that acquired nothing may cost us the normal copy.

Gamma found the gap in the first build. A pre-buy in flight made the source fill's lane attempt refuse ("another entry attempt was in progress"). The on-chain lane then skipped the same buy as `duplicate_of_feed`. When the pre-buy reverted, nobody copied the buy.

- **While a pre-buy of the coin is in flight:**
  - The fast lane: in memory, from the offer to the end of the worker's job (`RelayPrebuyWorker.inflight`).
    - The source fill falls back as `relay_prebuy_in_flight` before the hook.
    - It writes no attempt row and releases its hold.
  - The on-chain lane: by row state, as in Beta's `15b613fae` `rh_lane_pending`.
    - A `relay_prebuy` attempt of that 4663 coin with no terminal defers the source's detection as `relay_prebuy_in_flight`.
    - The wait is bounded by the detection's own freshness (`MEMECOIN_ONCHAIN_LANE_MAX_AGE_S`). Past it, the detection expires as `relay_prebuy_wait_expired`.
    - The sweep is never blocked.
    - Knob on only.
- **When the pre-buy ends, the source fill is judged as today.** One position per coin decides:
  - It refuses only when the pre-buy holds the coin (`filled`) or may hold it (`submitted_unknown`, `receipt_unknown`, `booking_held`, a binding or identity conflict, or an error).
  - A canonical revert or an unsigned pre-send abort released its reservation and acquired nothing, so the copy goes through.
- The pre-buy's origin carries the order id, never the source tx, so the per-transaction duplicate lookup never sees it.
- **J7:** J7 carries most 4663 attempts (495 against 324 on-chain since 10-07).
  - Once the pre-buy has signed, J7's refusal of the same buy is `unresolved signed transaction on 4663`. That is a retry-lane transient: it is re-asked every `MEMECOIN_ENTRY_RETRY_UNRESOLVED_S` (1.0 s live) inside the 60 s window, and judged after the pre-buy resolves.
  - Inside the pre-sign window (reserved and unsigned, tens of ms with a warm cache), the refusal is "one position per coin: another entry attempt was in progress at decision time".
    - Alpha, 2026-10-14: behind a `relay_prebuy` reservation, and only there, that refusal is the same retry transient, at the same 1.0 s and 60 s (`_prebuy_reservation_miss`).
    - The reservation blocker names `signal_source` only for a pre-buy, so every other blocker and refusal is unchanged.
    - The re-ask meets the same gate. A pre-buy that filled ends it as "open funded position"; a released one copies.

  **Amendment 2026-10-14: temporary reservation collisions** (ba2545dbc).  The pre-buy-only transient above
  is now generalized
  across entry owners: an exact in-progress refusal with a known reservation ID joins the existing
  unresolved retry cadence and window. The worker waits locally for held coins, without provider
  work, consuming attempts or resetting the original receipt/window. Held rows are filtered before
  the eligible-row limit so they cannot starve other coins. Once available, the original event
  reruns every funded gate; an open position or its own terminal price/safety refusal ends the
  retry. Signed or ambiguous sends retain the existing fence. This expands forward retry
  eligibility; it does not infer a UUID-to-transaction match, borrow another event's price verdict,
  relax a source cap or replay historical orders. A new reservation racing the snapshot is caught
  by the full funded gate.
- **Counters:**
  - `source_fill_refused_by_prebuy:<outcome>` and `source_fill_rejudged_after_prebuy:<outcome>`.
  - Each counts once per pre-buy that reserved, when a lane first judges that user's buy of the coin within 300 s.
  - They appear in the lane heartbeat's `prebuy_counters` and in `source_fill_tx`, `_lane`, `_reason` and `_disposition` on the `rh_relay_prebuys` row.

### 2.5 Records and attribution
There is no ledger schema change. As built, two tables, joined on `order_id`, because each side lives in its own process.

**Our side: `rh_relay_prebuys`**, in the lane's SQLite (`data/memecoin_rh_fast_lane.db`), written by the runner's worker.

| Group | Fields |
|---|---|
| Identity | `order_id`, `uid`, `mint`, `deposit_sig`, `request_id` |
| Timing | `deposit_seen_at`, `request_found_at`, `decided_at` |
| Route | `decide_head_block`, `state_age_blocks`, `route_kind`, `route_pool`, `impact_pct` |
| Our side | `reason`, `pending_id`, `position_id`, `our_tx`, `our_landed_block`, `reverted`, `rec_json` |

- `decide_head_block` is the chain head the decision priced at. The send block is not read separately.

**The source side: `rh_relay_requests`**, in the feed's SQLite (`data/memecoin_rh_feed_shadow.db`). It has one row per polled deposit, with these fields:
- the deposit;
- the poll's outcome, `found_ms` and the request;
- `warmed`;
- `prebuy_reason`: `sent`, `send_failed` or the decision's refusal;
- the route;
- the source outcome.

- The source outcome is settled 30 s after the request was found, on the poller thread, and only while it is idle:
  - `landed`: the feed saw the source's Relay fill for that order id. Its tx and block are recorded.
  - Otherwise, ONE Relay call on the poller's rate: `refunded`, `failed`, `landed` (from `outTxs`) or `none_30s`.
  - An unanswered call is `unknown`, never `none_30s`.
- The source's post-trade price is a join to `rh_feed_signals` on `source_fill_tx`. It is not copied.
- No new exit rule. Normal rails manage the position; changes to exits go to paper first.

### 2.6 Heartbeat
As built, the feed writes `rh_fast_heartbeat.relay_counters` and the lane writes `rh_fast_lane_heartbeat.prebuy_counters`, each as JSON. The lane's is NULL when the worker is off (unknown, never zeros). Together they count:
- `deposits_seen`, `polls`, `http_429`, `http_5xx`;
- `mint_found_ms` (p50, p90), `not_indexed_after_8`, `rejected_mismatch`;
- `warmed`, `warmed_then_signal`;
- `prebuy_sent`, `prebuy_refused{reason}`, `prebuy_reverts`;
- `source_outcome{landed, refunded, none_30s}`.

## 3. Settings (`sniper/settings/memecoin.py`)

The operator-order comment ("Live now, small size", 2026-10-14) sits on the size and cap knobs.

| Knob | Default |
|---|---|
| `MEMECOIN_RH_RELAY_PREWARM` | 0 |
| `MEMECOIN_RH_RELAY_PREBUY` | 0 |
| `MEMECOIN_RH_RELAY_PREBUY_CLIP_USD` | 20 |
| `MEMECOIN_RH_RELAY_PREBUY_MAX_PER_HOUR` | 4 |
| `MEMECOIN_RH_RELAY_PREBUY_PILOT_FILLS` | 20 |
| `MEMECOIN_RH_RELAY_PREBUY_MAX_IMPACT_PCT` | 5 |
| `MEMECOIN_RH_RELAY_PREBUY_MAX_REVERTS` | 3 |
| `MEMECOIN_RH_RELAY_POLL_MAX`, `_POLL_PER_S` | 8, 6 |

## 4. Edge cases (CLAUDE.md §5)

1. **Money path:** yes, a new entry trigger. Every gate stays except the chase reference: spot with a 5% impact cap, enforced by min_out. $20 clip. 4 sends an hour, 20 fills, 3 reverts. Both knobs off by default.
2. **Speed:** nothing is added to the detector, decide, watcher or lane signal threads. Relay calls happen only on the poller thread; the pre-buy runs on its own worker.
3. **Concurrency:**
   - The worker uses a `copy.copy(live)` with its own connection.
   - It shares the file-based wallet exec lock and the nonce floor with the lane: the same serialization as `SharedRouteWorker`.
   - Pilot counters are its own. The poller's per-deposit queue is bounded, and only the poller thread touches its own state.
4. **Absent / zero / unknown:**
   - A missing or unknown Relay field is a reject.
   - A zero or unknown supply means no mcap, so refuse.
   - Unknown spot (an empty pool) means no buy.
   - A missing `eth_usd` means no buy.
5. **Dependencies:**
   - Relay outage, 429 or 5xx: counted, stop, normal path.
   - A request indexed after the fill: no pre-buy.
   - Two orders from one depositor: each is judged on its own; one-position caps us at one buy per coin.
   - A non-4663 destination: no warm, no buy.
   - The right uid's wallet on the wrong chain: no warm, no buy.
   - A hooked pool or unknown V4 key: no buy.
   - A tape gap on the pool: no buy.
   - A token tax: the swap math has no tax, so a taxed token fails the impact check or reverts on min_out (fail closed, gas only). The safety gate's tax simulation still applies.
   - The source fill lands first in the same block: we revert on min_out (gas only); it counts toward the revert stop.
   - If the watchers' Relay verify calls see a 429 inside a poller burst, the poller rate is lowered.
6. **Units:** USDC 6 decimals; `currencyOut` atomic 18; spot in wei per coin atomic; impact in percent.
7. **Sample:** these fills are tagged `relay_prebuy` and graded apart from copy fills; `source_outcome` is recorded on each.
8. **Rollback:** either knob off; the stop file; the self-stops at 20 fills or 3 reverts.
9. **Gamma's LOWs on cd0865d18:**
   - The request recipient now has its own test.
   - A request buying native, WETH or a stable stops at `quote_asset_out`, with no learn, warm or pre-buy.
   - The learn being asynchronous no longer matters under warm only, because the safety cache's candidates come from tape routes.
   - A revert reconciled after the lane's receipt window does not count toward the revert streak. It stays documented.
10. **Found while building:**
   - A deposit from a user armed only on another chain (Relay option A's roster) is skipped without a Relay call.
   - A source outcome call never delays a deposit: it runs only when the poller is idle.
   - Not handled: a second signed deposit on the same order id that arrives after the pre-buy decision does not cancel it. The source fill's proof refuses that order (`duplicate_order`), and one position per coin still holds.

## 5. Tests (TDD)

**Poller**
- Accepts the matching request.
- Recipient mismatch: warms nothing, buys nothing.
- The right uid's wallet on the wrong chain: nothing.
- A non-4663 `currencyOut`: nothing.
- An amount mismatch: nothing.
- Status `success`, or `outTxs` present: no pre-buy.
- Not indexed after 8 polls: counted.
- A 429 or 5xx: stops at once and is counted.
- Spacing ≥ 166 ms.

**Pre-buy decide**
- Impact ≤ 5% gives cap = spot × 1.05 and the decide's min_out.
- Impact > 5%: no buy.
- A tape gap over the pool: no buy.
- Tape behind: no buy.
- Hooked / unknown key: no buy.
- An old pool with a current tape is allowed, and its age is recorded.

**Stale state**
- A pool with a missed buy (real price above tape spot): the executor's min_out reverts, and it never pays more than 1.05× tape spot.

**Event and initiation**
- `relay-request-prebuy-v1` is accepted only with matching identities.
- An unknown signal source no longer passes with no proof.
- `their_size_usd` comes from the deposit.

**Dedup**
- After a pre-buy reserves, the lane path and the slow path both refuse the source fill.
- A pre-buy aborted before the send blocks neither.

**Worker**
- Caps: 4 an hour, 20 fills (self-stop and alert), 3 consecutive reverts (self-stop and alert, persisted across a restart).
- The stop file stops the pre-buy.
- Knobs off: nothing is sent or started.

**Records**
- The `rh_relay_prebuys` row has its fields.
- The observer marks landed, refunded, or none within 30 s.

## 6. Not in this change
- New exit rules.
- Chains other than 4663.
- Watcher or detector changes beyond the poller thread and its learn offer.

### 2026-10-14 amendment: shared request pacing and durable send cap

The per-poller six-per-second request-start budget is shared by every first poll, retry and delayed outcome poll on the worker; distinct deposits do not each receive a fresh burst allowance. The existing queue, per-deposit attempt bound, 429/5xx behavior and off-source ownership remain unchanged.

Hourly send state now seeds from the durable entry submission audit by network, signal source and signed transaction identity. Provider attempts and same-byte reoffers deduplicate at the earliest recorded attempt, while canonical reverts remain counted after mutable reservation bindings are cleared. Legacy binding timestamps are used only where the matching durable audit is absent; audited attempt states count conservatively. Malformed or timezone-less audit timestamps refuse startup instead of resetting the cap. Fill and consecutive-revert stops keep their existing behavior. Copied execution workers read the live owner's stop state immediately before the funded hook.
