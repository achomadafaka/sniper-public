# Base 8453 fast lane, step 1: implementation plan

**Author:** Beta, 2026-10-14. **Spec:** `docs/superpowers/specs/2026-10-02-memecoin-base-8453-fast-lane-design.md`
(revision 3). **Order:** operator "yes to all" via Alpha (Base step-1 GO, D1 named, D3 "adjust for
whatever we have"). Alpha approved parts A-E as written below (2026-10-14).

## Ruling summary

- v1 route kinds are `v4_native` and `v4_weth` only. `v4_bridge` stays refused, so there is no hop-A
  poll. Step 0 recorded 23 would-send decisions (10-08..10-13), all native (19) or WETH (4).
- **D1** is `https://base.drpc.org` as `MEMECOIN_BASE_FAST_LANE_RPC_URL`. Alpha probed it: chain
  0x2105, p50 103 ms, eth_call OK, used by no role.
- **D3:** the float default is the $150-clip equivalent, capped by `balance_bound_float`. On
  2026-10-14 that is about 0.0309 ETH (~$77), so larger clips take the ordinary path.
- **Off by default.** The lane ships with `MEMECOIN_BASE_FAST_LANE` in `off|step0`, unchanged, until
  the eth_call template proof on D1 passes. Then Alpha sets `runner_shadow`. `live` needs the
  operator's existing yes and Gamma's read of the shadow.

## Design choice: the shared native EVM lane learns its chain

The 4663 `LiveLane` (memecoin_rh_fast_lane.py) is already the executor for the BSC lane
(`BscLive(LiveLane)`, CHAIN_ID 56). Base reuses it the same way: `BaseLive(LiveLane)`, CHAIN_ID 8453.

**Base events use signal_source `rh_fast_lane`, origin `rhfast:8453:<tx>:<mint>`, delivery_lane
`base_fast_lane`.** Reason: every funded integration already keys on signal_source `rh_fast_lane`
and the `rhfast:<chain>:` origin family:
- the early-quote skip and pilot sizing in `memecoin_real_runner`;
- receipt recovery in `evm_real_seams.recover_confirmed_entry_fn`;
- the ordinary lane's hold in `memecoin_onchain_lane`;
- the origin parsers in `fill_timing`, `volume_writer`, `db_memecoin` and `_SOURCE_ORIGIN`.

A new signal source would need all of them rewired; one missed site would be a silent money bug,
for example an unrecoverable reservation. Only the sites that hard-code chain 4663 change.
`network_id=8453` and `delivery_lane=base_fast_lane` keep Base rows separable for every reader.

## Part A: LiveLane chain parameterization. 4663 behaviour stays byte-identical.

| site | change |
|---|---|
| `_LaneChecks` | class attributes `CHAIN_ID = 4663`, `DELIVERY_LANE = "rh_fast_lane"`, `SAFETY_LABEL = "rh_fast_safety_cache"`. `_precheck`'s `entry_cap_usd` chain, `lane_buy_tx` chain, and `_lane_context`'s context key all come from `CHAIN_ID`. |
| `lane_buy_tx` | `chain="4663"` keyword, passed to `native_buy_tx`, `weth_buy_tx` and `native_multihop_buy_tx`. |
| `LaneEvent` | `chain="4663"` and `delivery_lane="rh_fast_lane"` keywords, used for `network_id`, the chase reference, `fomo_event_id` and `source_evidence`. |
| `native_source_price_matches` | `CHAIN_QUOTE[payload chain]`, defaulting to 4663. |
| `LiveLane.__init__` | `submission_transport` for chains in `{4663, 8453}`. `SequencerSender` stays 4663-only. |
| `LiveLane` | inline safety context key, `_handle_shared` context key, and `_receipt_amounts` pool manager (`V4[str(CHAIN_ID)]`) from `CHAIN_ID`. |
| `_seed_pilot` | `network_id` filter; 4663 passes "4663" (a Base fill never counts toward 4663's pilot). |
| `source_initiation_refusal` | the `rhfast:` origin and the receipt-backed deposit branch accept chain 8453 as well as 4663. Option C (receipt-less) stays 4663-only. |
| `evm_real_seams.recover_confirmed_entry_fn` | `("8453", "rh_fast_lane")` joins the lane recovery. |
| `memecoin_onchain_lane._route` | the `LANE_HOLDS` wait applies to network 8453 too. The holds are keyed by (tx, mint). |

Test: the 4663 lane suites pass unchanged, plus one test per changed site proving that 8453 reads
its own chain.

## Part B: `sniper/services/memecoin_base_fast_lane.py` (new)

- `BaseLive(LiveLane)`:
  - CHAIN_ID 8453; BACKEND `base_fast_lane`; RELEASE_PREFIX `base_fast_`;
    DELIVERY_LANE `base_fast_lane`; SAFETY_LABEL `base_fast_safety_cache`;
  - submission through `ctx["submission_transport"]` (auto_free). Build refuses without a
    transport, and `_post` raises.
- `BaseShadow(RunnerShadow)` with CHAIN_ID 8453.
- `check_sender`:
  - the 8453 watcher's pid, from its heartbeat;
  - required fields as 4663 native, plus `chain == "8453"`;
  - route kind in {v4_native, v4_weth}.
  `Seen` is keyed by (tx, mint).
- `Snapshot` (the BSC class, with Base WETH) on D1, polled every `SNAPSHOT_PERIOD_S` off the signal path.
- Safety:
  - `SafetyCache` + `routed_safety` with `route_quote` over `ctx["v4_direct_quote_fn"]`;
  - every read on D1 through `PacedHttp` and a lane-only http seam;
  - the registry {mint: kind, key, pool} comes from the datagrams;
  - inline on a cache miss, on D1 under a deadline.
- `lane_host_refusal` for D1: https, and a domain no other role or funded pool uses (the BSC rule).
- `build_base_lane` / `start_base_lane`, with the same thread set as BSC (listener, snapshot,
  safety, stop file).
- Settings:
  - `memecoin_base_fast_lane_mode()` accepts `runner_shadow|live` beside `step0`.
  - New knobs, BSC-shaped: `MEMECOIN_BASE_FAST_ROUTES` (default empty; anything outside
    {v4_native, v4_weth} refused at parse), `MEMECOIN_BASE_FAST_LANE_RPC_URL`, PILOT_FILLS,
    MAX_SENDS_PER_HOUR, LOCK_RECEIPT_CAP_S, SNAPSHOT_MAX_AGE_S, SNAPSHOT_PERIOD_S,
    ETH_USD_MAX_AGE_S, GENERATION_MARGIN_S.
  - The clip stays `MEMECOIN_BASE_FAST_LANE_CLIP_USD`, bounded by the 8453 per-entry ceiling.

## Part C: the 8453 watcher sends the datagram

In `runner_shadow|live`, the decide thread (`_decide_base_lane`, already off the socket thread)
turns a `would_send` record of an enabled kind into the 4663-shaped native datagram:
- the key, pool and post state from the step-0 record;
- `source_fill` via `source_fill_from_buy` with the receipt's `evm_post`;
- `src_in_wei` and the cap by the 4663 rule (`native_source_price_matches` must pass);
- the peeked ETH/USD and its stamp, `chain="8453"`, and `sent_epoch`.
It sends one datagram to the Base lane's socket. The step-0 record keeps being written, with
`lane_sent`. No network call is added.

## Part D: float and template proof

- `lane_float_bounds` for 8453: `MEMECOIN_BASE_FAST_LANE_FLOAT_NATIVE` (default 0.06 ETH, the $150
  clip at ~$2,500), applied by the 8453 gas pass only while the mode is `live`, under the existing
  `balance_bound_float`.
- `scripts/research/base_fast_template_proof.py`: one eth_call per kind on D1, using the lane's own
  builder at a real step-0 pool, from our address, value = a $1 clip. Pass means the call does not
  revert and returns the coin. Run once by Alpha before `runner_shadow`.

## Part E: runner wiring

- `scripts/memecoin_runner_loop.py` starts the Base lane next to the 4663 and BSC lanes.
- `memecoin_fill_timing._LANE_LIVE["8453"]`.

## Edge cases (CLAUDE.md 5)

1. **Money path: yes.**
   - Every funded gate runs through `make_real_hook`.
   - The executor is the 4663 lane's: lock without waiting, shared nonce floor, write-ahead
     identity, identical-bytes auto_free submission, booking from the receipt, unknown left as
     unknown.
   - Default off; shadow before live.
2. **Speed.**
   - Watcher: CPU only on the decide thread, measured.
   - Runner: all reads before the hook are in memory (snapshot and cache on D1, off path).
   - The inline check on a cache miss is bounded by its deadline.
   - p50/p90 go in each part's commit.
3. **Threads.**
   - The snapshot has one writer.
   - The cache uses its existing lock.
   - The lane thread is single.
   - No module state beyond the shared `LANE_HOLDS`.
4. **Absent, zero, unknown.**
   - No snapshot, a stale price, or an unknown generation each fall back by name.
   - A float of 0 is `float_short` in live and noted in the shadow.
   - Unknown proofs are refused, never defaulted.
5. **Dependencies.**
   - D1 down: snapshot and safety age out, and every signal falls back.
   - A D1 429 pauses 120 s.
   - auto_free ambiguity becomes `submitted_unknown`, never a release.
6. **Units.**
   - Wei and coin atomic, and the cap as an exact fraction.
   - ETH/USD comes from the watcher's peek.
   - The chain id is 8453 in the signature and identity checks.
7. **Sample.**
   - 6 wallets made the 23 step-0 would-send decisions; probablyinsider made 9.
   - The shadow read must be per wallet.
8. **Rollback.**
   - `MEMECOIN_BASE_FAST_LANE=off` stops the datagrams and the lane.
   - Half-applied (new watcher, old runner): the datagram goes nowhere (no socket bound).
   - Part A alone changes nothing on 4663.

Not in v1: `v4_bridge` (hop A), V3/Aerodrome/V2 sources, receipt-less deposit buys on 8453, and
Flashblocks.
