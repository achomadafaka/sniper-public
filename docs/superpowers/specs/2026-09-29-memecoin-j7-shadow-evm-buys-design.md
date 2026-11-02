# J7 shadow on EVM buys, per chain, starting with 4663 (rule-5 spec)

Date: 2026-09-29. Author: Beta. Order: operator item 3 via Alpha ("retire J7 where our lanes see
everything"). No code before Alpha's go.

## Why

`MEMECOIN_J7_SOLANA_MODE=shadow` (2a8072f7) lets a J7 Solana buy by a covered wallet run every
entry gate and stop before the leg. The fast lanes own that buy. On 4663, Alpha's census counts
50 of 53 J7 buys that the on-chain EVM lane saw, outside the 15:06Z provider outage, and our lane
was first on 49 of those 50. The same switch can therefore retire J7 on 4663 buys, one chain at a
time.

## What exists today (origin/main 69ea191c1)

- `j7_shadow_event(conn, ev)` (memecoin_real_runner.py). It returns a copy tagged
  `delivery_lane='j7_shadow'` only when all of these hold:
  - the mode is `shadow`;
  - side is `buy` and the source is `follow_activity`;
  - the normalized chain is `solana`;
  - the wallet is covered (`armed_verified_owners`);
  - `solana_watcher_healthy()` passes.
- Everything downstream of the tag is chain-agnostic:
  - the stop in `real_entry_hook` (`status: shadow`, before sizing, quote, reservation and signing);
  - alert suppression, and no retry enqueue;
  - `trace.j7_shadow`;
  - the lane dedupe, which excludes `delivery_lane='j7_shadow'` for EVM `chain:tx:mint` keys too
    (memecoin_onchain_lane.py `already_attempted_from_another_lane`).
- `armed_verified_owners` is Solana-only: it returns 0 owners for 4663.
- The EVM watcher builds its own list:
  - `_verified_addresses(conn)` = `watcher_address_owners(conn)` rows on THIS chain, whose owner
    is in `m.real_approved_user_ids(conn, network_id=chain)`;
  - it re-reads that list after each delivered log and reconnects on a change (memecoin_onchain_push
    `run_push_stream`). 4663 Initialize logs arrive about 6 a minute; on 56 only the sparse
    transfer filter drives it.
- J7 EVM buy events carry their chain at the hook. Since 09-26: 4663 550, 1 349, 56 277, 8453 61,
  5042 16, 143 7 (memecoin_events, follow_activity, 0x mints).

## Change

1. **Knob.** `MEMECOIN_J7_SHADOW_EVM_CHAINS`, comma-separated chain ids, default empty (today).
   - Canonicalized with `canonical_chain_id`. An unknown entry is warned once and ignored.
   - `solana` is ignored here; it keeps its own `MEMECOIN_J7_SOLANA_MODE`.
   - The raw value is read at import, like the Solana mode. The runner parses it at call time
     (`_j7_shadow_evm_chains`), and an empty value returns before any read.
   - There is no EVM "uncovered" option: EVM is covered-only.
2. **One definition of "watched" per EVM chain.**
   - New `evm_watched_owners(conn, chain) -> {address: (source_user_id, handle)}` in
     `sniper/services/axiom_address_verify.py`, beside `watcher_address_owners`. Its body is the
     current `_verified_addresses`.
   - The watcher's `_verified_addresses` calls it, so the lane and the gate cannot drift.
3. **The watcher publishes what it has ACKED.**
   - `PushState.transfer_addresses` is set when the `transfers` subscribe reply arrives. It holds
     the tuple the filter was built from.
   - It is cleared at the start of every connection attempt.
   - The push beat writes it as `transfer_addresses` (sorted, lowercased). At 7 addresses that is
     about 330 bytes on a beat that already carries about 620.
   - Poll-mode beats do not carry it.
4. **Coverage in the tagger, EVM branch.** A J7 EVM buy is shadowed only when every one of these
   holds, checked in this order:
   - its canonical `ev.network_id` is in the knob;
   - the owner has at least one address in `evm_watched_owners(conn, chain)`;
   - EVERY one of the owner's addresses there is in the beat's `transfer_addresses`;
   - the chain passes `evm_watcher_healthy(chain)`.

   Anything else returns None, and the buy stays live, as today.
5. **Per-chain health gate `evm_watcher_healthy(chain)`.**
   - It reads `data/onchain_evm_watch.heartbeat` (4663) or `data/onchain_evm_watch.<chain>.heartbeat`.
   - It is cached 1 s per chain, and a state change logs once per chain:
     `j7 shadow: evm watcher <chain> <healthy|UNHEALTHY> (<why>); J7 <chain> buys <shadow|LIVE>`.
   - Healthy iff ALL of:
     - the beat is readable;
     - `mode == "push"`;
     - age is within `MEMECOIN_J7_SHADOW_EVM_MAX_AGE_S` (default 15, equal to
       `MEMECOIN_EVM_WS_STALE_S`, where the watcher itself reconnects);
     - `coverage` is not None and `coverage_low_since` is None;
     - `log_silence != "suspect"` and no subscription verdict is `suspect`;
     - `transfer_addresses` is present.
   - A dark watcher writes no push beat, so it ages out, and J7 goes back to live.
6. **Sells stay live.** The tagger is already buy-only, and there is no EVM sell path besides J7.
7. **A shadow EVM decision reads nothing it will not use.**
   - The EVM prereads (the native balance on the funded chain RPC) are consumed only inside
     `_make_evm_entry_fn`, which is after the stop, so a shadow decision skips
     `_start_entry_prereads` on EVM.
   - It also skips the PoolKey prewarm enqueue: the lane decides first 49 of 50 times and prewarms
     for itself.
   - It keeps the market-data GET, which feeds classification and mcap gates exactly as the Solana
     shadow does.
   - It keeps the post-trade receipt read (its own host and pool), which the chase gate reads.
   - The Solana shadow is unchanged.
8. **The read.** `j7_shadow_read.py --chain <id>` runs the same buckets and verdicts per chain:
   filled, shared, MISS, and STOP(1)/(2) on that chain's fills. The hourly read covers each chain in
   the knob.

Activation (Alpha): deploy (the watcher restarts on the release and starts writing
`transfer_addresses`), then `MEMECOIN_J7_SHADOW_EVM_CHAINS=4663`, then a runner restart.

## Edge cases (CLAUDE.md rule 5)

1. **Money path: yes, entry routing only.**
   - A shadowed J7 buy spends nothing, and the lane owns the coin.
   - The failure mode is a MISSED entry: shadowed, but the lane never decided. It is never a
     wrong spend.
   - Exits, sizing and signing are untouched.
2. **Speed.**
   - With the knob empty, nothing is added: a set-membership test returns first.
   - On a knob chain, every J7 EVM buy pays the definition read. Measured on the live ledger,
     read-only, n=40 each:
     - 4663: p50 0.77 ms, p90 0.78 ms (7 owners, 7 addresses);
     - 56: p50 0.77 ms, p90 0.78 ms;
     - 8453: p50 0.79 ms, p90 0.80 ms;
     - for comparison, the Solana `armed_verified_owners` read is p50 0.48 ms.
   - The beat read is cached 1 s per chain.
   - A shadowed decision skips the whole leg: approval, quote, reservation, signing.
   - The lane's fill path does not change.
3. **Concurrency and global state.**
   - The per-chain health cache is a dict of whole-entry replacements. The worst race is a
     duplicate state-change log line, as with Solana's `_WATCHER_HEALTH`.
   - No global is captured and restored.
   - Watcher side: the reply handler and `_beat` both run on the push thread.
4. **Absent vs zero vs unknown.** Every unknown means J7 LIVE:
   - an unreadable, absent or poll-mode beat;
   - `coverage` None (unknown only just after a start);
   - `transfer_addresses` absent (an older watcher, and the half-applied state);
   - a definition read that raises.

   An empty `transfer_addresses` means nobody is covered. An owner with no verified address on the
   chain is uncovered. An owner partly acked (one of two addresses) is uncovered.
   `log_silence == "no_reference"` (56 has no Initialize reference) passes: on 56 a silent socket is
   invisible to this gate. That is why 4663 goes first.
5. **Dependencies: what this gate cannot see (NOT handled).**
   - A dead transfer filter behind a live socket. The transfers subscription is `no_reference` by
     design (sparse), so the 4663 12:10-12:24Z shape (logs silent while heads arrive) is caught
     only once ALL subscriptions are silent for `MEMECOIN_EVM_WS_LOG_SILENCE_S` (300 s).
   - A failing tx-read host. A delivered log whose tx read fails writes no detection, and the beat
     does not count failures.
   - Both show up after the fact as MISS in the per-chain read (item 8).
   - A stall shorter than 15 s shows as healthy. The watcher's gap backfill (the chain's Alchemy
     pool member; 4663 has one, `robinhood-mainnet.g.alchemy.com`) recovers the missed transfers
     after the reconnect, bounded by the lane's max age.
6. **Units and identity.**
   - Chain ids are canonicalized on both sides.
   - Addresses are lowercased on both sides (the watcher already lowercases).
   - Coverage is by owner, and it requires ALL the owner's addresses, because the J7 event names
     the wallet, not the address it bought from.
   - Beat `ts` is ISO with an offset, compared with UTC now; a beat more than 5 s in the future is
     unhealthy, as on Solana.
   - The shadow tag keys on the event's chain. A J7 chain label that disagrees with the coin
     resolves the same way live would: identity mismatch refuses both.
7. **The sample.**
   - The 49/50 census describes one period's armed 4663 wallets with that period's watcher health.
   - The forward denominator is shadowed J7 buys by covered, healthy-chain wallets. The number to
     watch is MISS per chain per hour.
   - `shared` (the lane refused on the same gate) is our own behaviour, not coverage.
8. **Rollback.**
   - Empty the knob and restart the runner.
   - Half-applied (knob set before the watcher release): `transfer_addresses` is absent, so every
     wallet is uncovered and J7 stays live. That is safe.

## Checks left behind (red first)

`tests/test_memecoin_j7_shadow_evm.py`:
- knob empty: an EVM J7 buy is never tagged (byte-identical);
- 4663 in the knob, a covered and healthy owner: tagged. An EVM sell: never tagged;
- uncovered: owner not armed on the chain, no address, one of two addresses unacked,
  `transfer_addresses` absent;
- each unhealthy beat: stale, `mode` absent or `poll`, `coverage` None, `coverage_low_since` set,
  `log_silence` suspect, a subscription suspect, unreadable;
- a shadow EVM decision:
  - returns `status: shadow`;
  - never calls the EVM entry fn, the chain `native_fn` prereads, or the key prewarm enqueue;
  - is excluded by the lane's dedupe for its `chain:tx:mint` key;
- the watcher: `transfer_addresses` appears only after the `transfers` reply and is cleared on
  reconnect;
- `evm_watched_owners` equals the watcher's list on a fixture ledger (one definition).

## Build additions (Alpha's go, 2026-09-29)

- (a) STOP(3) in `j7_shadow_read.py`, EVM chains only: any MISS in the 90-minute window. An EVM buy
  is shadowed only while that chain's watcher is healthy, so a MISS there is a real miss with the
  watcher up. The action is to revert the chain to live. STOP(1) and (2) apply per chain as on
  Solana.
- (b) The push beat carries `delivered_since_connect` (heads and each subscription, reset on every
  connect) and `connected_at`. A dead transfer filter behind a live socket is then visible after the
  fact: heads climb while `transfers` stays at 0. The gate does not use it yet.
- (c) `test_the_half_applied_state_keeps_j7_live`: the knob is set while the beat has no
  `transfer_addresses` (the previous watcher), and the buy stays live.
