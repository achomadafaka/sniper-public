# BSC 56 Relay pre-buy: implementation plan

> **For agentic workers:** native execution in this session (lambda), TDD per task, then Gamma reviews the whole branch. Steps use checkbox (`- [ ]`) syntax.

**Goal:** a $20 pre-buy on 56 when an armed user's verified Relay deposit names a 56 coin before the source's own fill lands. It reuses the 4663 pre-buy's rules, and executes through the BSC lane's executor.

**Architecture:**
- The 4663 feed process polls Relay, as today, and routes a 56 match to the BSC lane's socket.
- In the runner, the BSC lane's own `BscRelayPrebuyWorker`:
  - prices the coin's PancakeSwap V2 pairs in one batch on the lane's host;
  - picks the cheapest route;
  - runs `PrebuyBscLive`: the BSC lane's checks, the full funded hook and the lane's executor.
- Defer and re-judge the source fill on the BSC lane, the on-chain lane and J7.

**Tech stack:** Python 3.13, SQLite, pytest (`-n 4` max).

**Spec:** `docs/superpowers/specs/2026-10-14-bsc-relay-prebuy-design.md` (approved, inline allowed on 56).

**Branch:** `lambda/bsc-relay-prebuy`, stacked on `lambda/rh-relay-prebuy` d99e38846 (release 8). It is rebased onto main after release 8 merges.

## Global constraints
- The knob is `MEMECOIN_BSC_RELAY_PREBUY`, default `0`.
- The size and cap knobs are shared with 4663: `MEMECOIN_RH_RELAY_PREBUY_CLIP_USD`, `_MAX_PER_HOUR`, `_PILOT_FILLS`, `_MAX_REVERTS`, `_MAX_IMPACT_PCT`, `MEMECOIN_RH_RELAY_POLL_*`.
- Counters, streaks and records are per chain.
- No new submission path: the BSC lane's `execute_reserved`, lock, nonce floor, write-ahead and builder bundle.
- Nothing is added to the BSC listener's fill path beyond dict reads and writes.
- Absent or unknown means no buy, and is counted.

## Review focus (inputs no task's happy path exercises)
1. A lane host that answers the batch out of order or with a missing id, or does not batch at all: no buy. Task 3.
2. The source's own fill arriving while the pre-buy's inline check runs: the pre-buy yields before its hook. Task 4.
3. A coin with both a WBNB pair and a thinner USDT pair: the cheaper route wins. If both exceed 5% impact, no buy. Task 3.
4. A 56 deposit of a user armed on 4663 only, or the reverse: never routed to the wrong chain. Task 2.
5. 56's revert stop must not stop 4663's pre-buys, and the reverse. Task 4.

---

### Task 1: Knob, and the initiation proof on 56

**Files:**
- Modify: `sniper/settings/memecoin.py` (after the 4663 pre-buy knobs).
- Modify: `sniper/services/memecoin_source_initiation.py` (`_prebuy_refusal`, and the guarded set already covering `relay_prebuy` / `rhprebuy:`).
- Modify: `sniper/services/memecoin_rh_relay_prebuy.py` (`prebuy_initiation(found, chain="4663")`).
- Test: `tests/test_memecoin_bsc_relay_prebuy.py` (new).

**Interfaces:**
- Produces:
  - `cfg.MEMECOIN_BSC_RELAY_PREBUY` (bool);
  - `PREBUY_CHAIN_KNOBS = {"4663": "MEMECOIN_RH_RELAY_PREBUY", "56": "MEMECOIN_BSC_RELAY_PREBUY"}`, used by `_prebuy_refusal`;
  - `prebuy_initiation(found, chain)`: `network_id` and `request_out_chain` are the chain's.

- [ ] Tests first:
  - a 56 proof is accepted with the 56 knob on;
  - refused with it off;
  - refused under the 4663 knob alone;
  - a 4663 proof on 56 is refused, and the reverse;
  - the origin `rhprebuy:56:<order>:<mint>` is required;
  - every identity mismatch from the 4663 suite also refuses on 56.
- [ ] Generalize `_prebuy_refusal(ev, chain, proof, origin)`:
  - the chain must be in `PREBUY_CHAIN_KNOBS` with its knob on;
  - `origin == "rhprebuy:%s:%s:%s" % (chain, order, mint)`;
  - `proof["network_id"] == chain`;
  - `proof["request_out_chain"] == int(chain)`.
- [ ] Run the new file plus `tests/test_memecoin_rh_relay_prebuy.py`; commit.

### Task 2: Feed side: per-chain roster, match, and the 56 hand-off

**Files:**
- Modify: `sniper/services/memecoin_rh_relay_prebuy.py`: `request_match`, `RequestPoller`, `RelayPipeline`, `start_relay_poller`.
- Modify: `sniper/services/memecoin_rh_fast_decide.py`: `start_decide` passes the per-chain roster function.
- Modify: `sniper/services/memecoin_rh_feed_shadow.py`: `start_shadow` refreshes `{chain: evm_watched_owners(conn, chain)}` for the chains whose pre-buy or prewarm knob is on.
- Test: `tests/test_memecoin_bsc_relay_prebuy.py`.

**Interfaces:**
- `request_match(payload, deposit, *, watched)`: `watched` is `{chain_int: {address: (uid, handle)}}`.
  - The recipient must be in `watched[out_chain]`, with its uid among the deposit's uids.
  - An `out_chain` not in `watched` is `destination_not_enabled`. This replaces `destination_not_4663`; counters are updated to match.
- `RelayPipeline(..., watched_fn, bsc_address, bsc_send)`:
  - A 4663 match goes the existing way.
  - A 56 match calls `bsc_prebuy_payload(found, arrival_utc, sent_epoch)` and `bsc_send(bsc_address, payload)`. Its row has `prebuy_reason` `sent_56` or `send_failed`.
- `bsc_prebuy_payload(found, *, arrival_utc, sent_epoch)` returns:

  ```python
  {"kind": "relay_prebuy_v1", "chain": "56", "tx_hash": order, "seq": 0, "wallet", "mint", "handle",
   "source_user_id", "permit_nonce": "relay:" + order, "arrival_utc", "sent_epoch",
   "paid_usd": amount_atomic / 1e6, "source_initiation": prebuy_initiation(found, "56"),
   "prebuy": {order_id, deposit_sig, deposit_slot, request_id, request_status,
              deposit_seen_at, request_found_at, found_ms}}
  ```

  No route or price fields.

- [ ] Tests first:
  - a 56 request reaches the BSC sender as one datagram;
  - with the 56 knob off, nothing is sent and the request is not polled (not armed);
  - a 56 recipient that is only in the 4663 roster is refused, and the reverse (Review focus 4);
  - the 4663 suite still passes unchanged except the renamed reason.
- [ ] Implement; run both pre-buy files plus the decide and feed covering set; commit.

### Task 3: Runner pricing: one batch on the lane host, V2 route choice

**Files:**
- Create: `sniper/services/memecoin_bsc_relay_prebuy.py` (the runner side for 56; it imports the BSC lane).
- Test: `tests/test_memecoin_bsc_relay_prebuy.py`.

**Interfaces:**
- `bsc_spot_batch(host, mint, hop_quotes)` returns `(block, {pair: (reserve0, reserve1)})`, or `(None, reason)`.
  - It makes one `host.batch` call: `eth_blockNumber`, `getReserves` (selector `0x0902f1ac`) on `our_pair(mint, WBNB)`, and on `our_pair(mint, q)` and `our_pair(WBNB, q)` for each q.
  - It returns `(None, "batch_unsupported")` on None.
  - A raised error or malformed reply gives `(None, "spot_read_failed")`.
  - A pair whose `getReserves` returns `0x` (no pair) is absent.
- `bsc_prebuy_route(mint, reserves, *, clip_wei, max_impact_pct)` returns `(fields, None)` or `(None, reason)`.
  - Candidates: coin/WBNB direct, and coin/q through WBNB/q. A zero side means skip.
  - est_out comes from `v2_amount_out`, hop first. Spot is the coin leg's reserve ratio; on a hop it is the quote converted to WBNB at the fee-inclusive hop spot.
  - Impact = (paid ÷ est_out ÷ spot − 1) × 100. The lowest impact wins. No candidate gives `no_v2_route`; above the cap gives `impact_over_cap`.
  - `fields`: the BSC native payload's pool, quote, coin1, r_coin, r_quote, hop (`{pair, r_wbnb, r_quote, source: "lane_host", height}`), `tax_num=0`, `tax_den=1`, cap_num/cap_den (spot × 1.05, quote per coin atomic), src_in = clip in quote units, src_out = est_out, `spot_wbnb_num`/`_den`, `impact_pct`, `state_kind="prebuy_lane_host"`.
- `bnb_usd_now(now)` returns `(usd, observed)` from `memecoin_bsc_v2_live` `quote` within its TTL, or `(None, None)`.

- [ ] Tests first:
  - a WBNB pair alone gives cap = spot × 1.05, with `min_out_for(clip, payload)` ≥ the cap rule;
  - a USDT hop route is priced through the hop;
  - both routes present: the cheaper one wins (Review focus 3);
  - impact over 5% is no buy;
  - no pair gives `no_v2_route`;
  - a batch answer that is None, raises, is out of order, has a repeated id or a missing id: no buy (Review focus 1). `LaneHost.batch`'s own checks are reused.
- [ ] Implement; run; commit.

### Task 4: Runner lane and worker

**Files:**
- Modify: `sniper/services/memecoin_bsc_fast_lane.py`:
  - a `_lane_event(payload, market_cap)` seam replaces the inline `BscLaneEvent(...)` in `BscLive._handle`;
  - an `_initiation_refusal(payload)` seam replaces the inline `bsc_payload_initiation_refusal(payload)` call at the top of `_handle`;
  - the `prebuy_inflight_fn` check after it returns `relay_prebuy_in_flight`;
  - `build_live` keeps `live_kwargs`;
  - `BscDispatch` routes `kind == "relay_prebuy_v1"`, authenticated by the rh feed's detector pid (`memecoin_rh_fast_lane.detector_pid` on `memecoin_rh_feed_shadow.heartbeat`), and notes each source signal's `(uid, mint) → time`;
  - `build_bsc_lane` builds the worker when the knob is on and the mode is live; `start_bsc_lane` starts it and registers it to close.
- Modify: `sniper/services/memecoin_bsc_relay_prebuy.py`: `PrebuyBscEvent`, `PrebuyBscLive`, `BscRelayPrebuyWorker`, `build_bsc_prebuy_worker`.
- Modify: `sniper/services/memecoin_rh_relay_prebuy.py`: `ACTIVE` becomes `{chain: worker}`, and `note_source_fill(chain, …)`. `RelayPrebuyWorker` takes the chain, and `_seed_pilot` filters by it.

**Interfaces:**
- `PrebuyBscEvent(payload, *, market_cap)`:
  - `signal_source="relay_prebuy"`, `delivery_lane="bsc_relay_prebuy"`, `network_id="56"`, origin `rhprebuy:56:<order>:<mint>`;
  - `their_size_usd` = `paid_usd`; `their_price` = spot USD; chase `{"reason": "relay_prebuy_no_source_fill"}`.
- `PrebuyBscLive(BscLive)`:
  - `RELEASE_PREFIX="bsc_prebuy_"`, `ORIGIN_PREFIX="rhprebuy"`, `DELIVERY_LANE="bsc_relay_prebuy"`;
  - `_initiation_refusal` uses `source_initiation_refusal(ev, "56")`;
  - `_lane_event` builds a `PrebuyBscEvent`.
- `BscRelayPrebuyWorker(RelayPrebuyWorker)`, where `run_one` does, in order:
  1. the stop and caps (inherited);
  2. the collision check;
  3. note the coin as a candidate at priority -1;
  4. `bnb_usd_now`, `bsc_spot_batch` and `bsc_prebuy_route`;
  5. safety: cached gives `prebuy_cached`; cold runs the lane's own inline check, which gives `prebuy_inline`;
  6. the collision check again;
  7. `PrebuyBscLive.handle(merged payload)`.
  - The outcome registry and records are inherited, and the records go to the BSC lane DB.

- [ ] Tests first, on the `live56` harness (`tests/test_memecoin_bsc_fast_lane_live.py`):
  - a warm coin reaches the builder send with min_out at the cap, `prebuy_cached`;
  - a cold coin runs inline and reaches the send, `prebuy_inline`;
  - a cold coin whose inline check refuses means no pre-buy;
  - a source fill seen before the decision gives `source_fill_seen`;
  - a source fill seen during the inline check gives `source_fill_seen`, with no pending row (Review focus 2);
  - the knob off: no worker, and the datagram is dropped;
  - a datagram from the 56 watcher's pid, rather than the feed's, is dropped;
  - per-chain streak and caps (Review focus 5).
- [ ] Implement; run the BSC lane covering set; commit.

### Task 5: Defer and re-judge on 56

**Files:**
- Modify: `sniper/services/memecoin_onchain_lane.py`:
  - `relay_prebuy_in_flight(conn, mint, detected_at, *, network_id="4663", ...)`;
  - `_route` also checks it on 56, before the `bsc_native_pending` / `sent` hand-off block;
  - `_note_prebuy_source` covers 56.
- Modify: `sniper/services/memecoin_bsc_fast_lane.py`: `BscDispatch` calls `note_prebuy_source` after a judged signal.
- Test: `tests/test_memecoin_bsc_relay_prebuy.py`.

- [ ] Tests first, on the 56 harness:
  - a reverted pre-buy, then the source fill is copied by the BSC lane;
  - an aborted pre-buy in flight, then the source fill is copied by the on-chain lane;
  - a landed pre-buy, then the source fill is refused;
  - `submitted_unknown`, then refused;
  - J7 in the pre-sign window on 56 re-asks and copies;
  - the counters land in the BSC beat.
- [ ] Implement; run; commit.

### Task 6: Heartbeat, records, spec as built, Gamma

- [ ] Add `prebuy_counters` (JSON) to the BSC lane beat table and write the worker's `beat()` there.
- [ ] The `rh_relay_prebuys` rows go to the BSC lane DB, with the source-fill notes flushed at the beat.
- [ ] Run the covering set: importers of the BSC lane, the on-chain lane, the pre-buy modules, source initiation, the runner, the deposit proof and the feed. `-n 4`, with watchdog and deploy tests excluded.
- [ ] Update the spec as built and commit. Send the branch to Gamma with the measured inputs and the open LOWs.
