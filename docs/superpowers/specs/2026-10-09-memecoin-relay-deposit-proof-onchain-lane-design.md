# Relay option A: prove on-chain-lane Relay buys from the wallet's own signed deposit

Status: Gamma PASS on design (1b22af3b9); version (b) chosen (Alpha 2026-10-09, operator may veto); building. Author: Beta, 2026-10-09. Order: Alpha (operator chose Relay
option A). Builds on option C (`memecoin_rh_deposit_proof.py`, da23b6ec2, live on the 4663 fast lane).
No code before Gamma; section 5 needs its own operator yes.

## 1. The problem, measured

An armed wallet's Relay buy reaches the on-chain watcher (`scripts/research/onchain_evm_wallet_watch.py`)
with `source_initiation.proof_version = relay-envelope-other-target-v1`. `_defer_relay_origin` holds the
row on the `OriginWorker` until `WatcherVerifier` gets a settled answer from Relay's GET `/requests/v3`
(poll every `MEMECOIN_RELAY_ORIGIN_POLL_S` 0.30 s up to `ORIGIN_VERIFY_BUDGET_S` 8 s). Only then is the
detection committed and the funded runner woken. The runner checks `source_initiation_refusal` first
(`memecoin_real_runner.py:11758`), so every unproved row refuses `wallet_initiation_unproved`, whatever
else is wrong with the buy.

`relay origin` lines in `logs/onchain_evm_armed{,.56,.8453}.log` since 2026-10-07 (`.1`/`.5042` had none):

| reason | 4663 | 56 | 8453 | all |
|---|---|---|---|---|
| verified | 48 | 32 | 10 | 90 |
| relay_request_not_indexed (gives up at ~8.2 s) | 21 | 12 | 2 | 35 |
| origin_wallet_unverified | 18 | 4 | 1 | 23 |
| origin_rate_limit | 3 | 2 | 2 | 7 |
| origin_transaction_not_indexed | 1 | 0 | 0 | 1 |

Gamma (via Alpha): 39 distinct Relay txs refused `wallet_initiation_unproved` (4663 28, 56 8, 8453 3).
Of the 31 on 4663/8453, only 4 pass every other gate (4663 change, figaro x2, 8453 ResellCalendar); 26
were refused anyway (mostly the source-buy floor, 9 the mcap ceiling, e.g. ABYSS at about $4.3M FDV, 1
chase), and 1 is unknown (Pons pool). Those 4 ran a median +88% at +5 min.

The 2 rows that "log verified but refused" are not a handoff bug in this path. Both are `rhfast:4663`
attempts of txs the watcher verified (0xc18757a5a4 at 00:39:06.401, 0x707ccd5111 at 18:19:33.470). The fast
lane decided on its own (00:39:06.557; 18:19:33.442, before the watcher), had no deposit proof, and
refused. The same buys' `swap:raybot` attempts refused on other gates (over ceiling; chase +32%), so no
`onchain:` attempt was written (duplicate of the feed). The fast lane never reads the watcher's Relay
verification; with this spec it reads the same deposit store (section 3.1).

## 2. Coverage, measured

**Origin chain.** All 125 Relay detections the watcher verified since 2026-10-06 have a SOLANA origin
(4663 65, 56 45, 8453 15; `onchain_wallet_detections.source_valuation_json`). The refused classes Gamma
recovered also have Solana depositors. EVM-origin deposits do not matter today, so A reads Solana
deposits only. `_verify_evm_origin` (depositor == beneficiary) stays on the Relay path for the rare one.

**Who signed.** Gamma recovered the Solana depositor for 102 of 153 Relay buys:
- 29 of 32 wallets use exactly one depositor on their verified buys, and it repeats (NotARandomUser 11,
  badabeepp 8, 0xdetweiler 6). That depositor is the wallet's `memecoin_address_registry` row
  (`network_id='solana'`, `status='verified'`, `j7_trader_receipt`). For example: ResellCalendar
  DxM1hfY8, Tekkerrss 7UBjMaYs, DopamineFeenFr1 FXr1GVAQ, degen_ak 815fk3tu.
- Gamma re-keyed the 29 refused attempts that have a recovered depositor by `source_user_id` and checked each
  against `memecoin_address_registry` (solana, verified, `conflict_at` NULL). By Relay reason:

  | class | n | Relay reason today | with A |
  |---|---|---|---|
  | the uid's OWN registry-verified depositor (`status_at` 2026-09-27T00:34, before every buy) | 10 | `relay_request_not_indexed` | provable at detection |
  | 3vxhT4NLc8ovyuewpwniGdJ6ZhjWeDveq1Db53DajBFg (7 distinct uids, no registry row for any) | 15 | 14 `origin_wallet_unverified`, 1 `origin_transaction_not_indexed` | refused, `relay_origin_shared_depositor` |
  | foreign depositor, no registry row (figaro Fdzxariy, frankdegods 8Ls1gXe4) | 2 | `origin_wallet_unverified` | refused, `relay_origin_foreign_depositor` |
  | already Relay-verified, refused by later runner gates (seralberttrades Cqu5uKfK, chrollo0o0 DeFdPbeB) | 2 | none (not initiation refusals) | unchanged |

  - The 10 are A's whole gain in the sample: 4663 7, 56 1, 8453 2.
    - randomuser123 (d47559fc) 2L17Sw85 x3;
    - DopamineFeenFr1 FXr1GVAQ x2;
    - bluntz_capital 4ZZW5ePC;
    - onmycheck Ay77dkJk;
    - Tekkerrss 7UBjMaYs (56);
    - degen_ak 815fk3tu and ResellCalendar DxM1hfY8 (8453).
  - Only 16 of the 29 are `origin_wallet_unverified`.
  - Corrected from ed. 1b22af3b9, which had "29 `origin_wallet_unverified`", "5 + 3" own-depositor buys and
    "6 singletons", taken from an observed-pair census rather than the registry.
- My own check (`scripts/research/relay_deposit_coverage.py`, Relay's public endpoint) classed 5 timeouts as
  `solana_verified`: Tekkerrss 2, randomuser123 2 (2L17Sw85) and ResellCalendar 1 (ref 5c680cc3, HIP). These
  are a subset of Gamma's 10. 33 of its 66 reads drew HTTP 429, so Gamma's re-keyed registry figures are the
  coverage.

So the provable class is the 10 timeouts from each uid's own registry identity, plus the verified buys that
today wait on Relay. 3vxhT4 and the foreign depositors stay refused (section 4).


**Amount.** On the 125 verified buys, the watcher's fill USD over the deposit USD was:

| chain | n | p10 | median | min |
|---|---|---|---|---|
| 4663 | 65 | 0.994 | 0.999 | 0.993 |
| 56 | 45 | 0.990 | 0.991 | 0.981 |
| 8453 | 15 | 0.994 | 0.998 | 0.994 |

Max was 0.999 on all three. Option C's band of 0.98..1.00 holds on every chain.

**Arrival order.** Option C's 22 confirmed matches (`data/memecoin_rh_feed_shadow.db` `rh_deposit_proofs`)
saw the deposit frame 0.68..2.19 s BEFORE the 4663 fill (mean 1.03 s). The watcher detects a fill only
after its EVM block, so the deposit is normally already in the store at detection.

## 3. Design

### 3.1 One deposit socket, one shared store

- **The socket** is option C's `_socket` and `deposit_from_frame`, unchanged in what they prove:
  - no error;
  - exactly one top-level depository `depositErc20` (amount, order id);
  - sender == depositor, and the depositor signed;
  - vault and USDC, with balance deltas equal to the amount;
  - the depositor is a verified, conflict-free Solana identity, verified before the frame arrived.
- **The roster** widens from users armed on 4663 to users armed on ANY funded EVM chain (4663, 8453, 56, 1,
  5042): `deposit_owners(conn, networks=...)`.
- **Filter.** `transactionSubscribe` uses `accountRequired=[depository]` with `accountInclude=roster`, so
  only roster deposits stream. Today it is `accountInclude` only: 597 frames, 7.9 MB at the last beat.
- **The key** is e1f95763, as option C (the paid research key; continuous use is the operator's exception).
  Its monthly headroom is still owed (task be32b78b).
- **The store** is `data/relay_deposit_proofs.db`, one row per order id: order_id PK, signature,
  depositor, uids, amount_atomic, slot, arrived, duplicate, used_fill_tx, used_at. Written by the socket
  thread with synchronous=NORMAL in WAL.
  - Two signed deposits on one order id mark it `duplicate`; neither binds a fill (option C's rule).
  - Rows past TTL 600 s are pruned.
- **The fast lane** keeps its in-memory map and also reads this store, so both lanes see one deposit
  stream and one single-use record.

The socket runs in ONE process, the rh feed process where option C already runs it. The per-chain
watchers only read the store.

### 3.2 The match, in the watcher (no Relay wait when it matches)

At `_defer_relay_origin`, before anything is queued:

1. `order_id = '0x' + transaction.input[-64:]`, the same hint `WatcherVerifier._verify` uses.
2. One store consume runs in a single statement, using `RETURNING` (UPDATE ... SET
   used_fill_tx=:tx ...). It succeeds only when all of these hold:
   - the row is not `duplicate`;
   - `used_fill_tx` is NULL or already this tx (the same fill re-read by another lane);
   - `0 <= detection_arrival - arrived <= 120 s`, the bound `_verify_relay` uses;
   - the row's `uids` contain the row's `source_user_id`;
   - the depositor is not shared (section 4);
   - the registry still holds the depositor verified and conflict-free for this uid, checked at consume
     time by one indexed read on `memecoin_address_registry` (PK source_user_id, network_id, address).
     A `conflict_at` set after the frame arrived therefore revokes a pending row (Gamma's gap; the `uids`
     captured at insert are not trusted alone).
3. The fill's USD (the watcher's `source_size_usd`) over the deposit USD is in 0.98..1.00.
4. On a match, `source_initiation` becomes `relay-deposit-processed-v1` with origin_network_id solana,
   origin_wallet, origin_tx, origin_slot, origin_amount_atomic, order_id, origin_identity_user_id and
   payer. The row commits at once and the runner is woken. Nothing is queued on the OriginWorker.
5. On no match, the row goes to the `OriginWorker` as today. Each poll first re-checks the store (the
   deposit can land after detection), so whichever proof lands first wins.

`source_initiation_refusal` accepts `relay-deposit-processed-v1` for `onchain:<chain>:` origins on every
funded EVM chain, behind `MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS` (default empty; today the rule is
`rhfast:4663` only). It keeps every identity check it applies to option C.

The BSC V2 watcher handoff changes only the event envelope to `bscfast:56:`. That envelope accepts
the same receipt-backed deposit proof when `56` is in `MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS`.
Receipt block hash, acquired amount and all wallet/transaction/mint/user/origin bindings remain
required; it does not inherit the receipt-free `rhfast:4663` exception. Positions 72574 and 72575
exposed the missing envelope on October 9: their deposit proof passed the ordinary watcher path
but was refused by the native BSC lane.

### 3.3 After the send

`DepositConfirmer` (option C) confirms every consumed deposit with getSignatureStatuses every 2 s until
`confirmed`. A failed deposit, one unseen at 90 s, or one the RPC never answered:
- pages ("on-chain-lane deposit-proved fill X: its Solana deposit Y was dropped");
- is recorded `initiation_unconfirmed:<why>`.
Nothing is sold automatically: the user signed the deposit, so authority holds.

## 4. Anti-spoof (unchanged authority, two new refusals)

The only authority is the wallet's verified Solana identity signing the deposit, bound by order id to
this fill and used once. Token delivery alone never counts.

New, from Gamma's data:
- **shared_depositor.** The key is `source_user_id`, never the handle: one user carries two names
  (d47559fc is NotARandomUser in the feed and randomuser123 in `memecoin_wallets`). The set comes from
  every settled Relay answer and every store row, kept in the store as (depositor, source_user_id) pairs.
  Measured: 3vxhT4 funds 7 distinct source users (figaro, badabeepp, CryoPalmar, ser_albert, chrollo0o0,
  Onchainmetrics, SerAvocado). 2L17Sw85 funds ONE (d47559fc). No depositor in the data funds exactly two.

  d47559fc's Relay buys since 2026-10-06 (`onchain_wallet_detections`, all 4663, every one funded by
  2L17Sw85, its `j7_trader_receipt` identity):
  - 12 verified (11 `relay-origin-bound-v1`, 1 `relay-origin-paid-v1`);
  - 11 refused `relay_request_not_indexed` (ABYSS among them).

  | rule | d47559fc today | d47559fc with A |
  |---|---|---|
  | either version below | 12 proved by Relay, 11 refused on the 8 s timeout | all 23 provable by the deposit before any wait (the depositor is its registry identity and it funds one source user, so it is not shared), then judged by every runner gate (ABYSS still refuses over the mcap ceiling); a projection, since the store does not exist yet |

  Two versions for the operator. They differ only for a depositor seen on exactly two source users,
  which no data has today:
  - **(a) Shared at 2.** Every user it funds is refused `relay_origin_shared_depositor`, including the user
    whose registry identity it is. That is stricter than today's Relay path, which accepts that user.
  - **(b) Shared at 3, page at 2 (Alpha's alternative).**
    - At 3 or more source users (3vxhT4 has 7), every user is refused.
    - At exactly 2, nobody is refused by this rule. The user whose verified registry identity it is keeps
      its proof. The other user's buys refuse `relay_origin_foreign_depositor`, as they do today.
    - The 2-user link pages a registry review.
    - Version (b) keeps today's authority for a verified identity until a third user appears.

  The shared rule has 0 hits on A's path in this data. 3vxhT4 has no registry row, so it never enters the
  roster (`accountInclude=roster`) or the store. It stays refused on the Relay path either way, and the rule
  only renames its refusal from foreign to shared. Its value is defence against a future registered
  identity that is also a service wallet.

  Residuals, accepted (Gamma):
  - A new shared depositor passes until co-funding is observed, but only if its registry row is wrong. An
    unregistered depositor never matches in A.
  - Shared pairs come only from Relay answers on fills we watch, so co-funding of a user we do not watch is
    invisible.
  - No hot-path heuristic (closed account, tx rate) is added unless a case appears.

  Chosen: (b) (Alpha 2026-10-09). The cost of (a) is a working armed wallet's whole Relay flow on the first 2-user
  sighting. The risk (b) leaves open is the window before a shared service wallet reaches its third user.
  In both versions, a shared depositor that is registry-verified is paged for registry review, and setting
  its `conflict_at` is a manual step.
- **foreign_depositor.** A settled Relay answer whose depositor is not a verified identity of this user.
  Today this is the early give-up's `origin_wallet_unverified`.

Refusal reasons name the class instead of `wallet_initiation_unproved`:
- `relay_origin_shared_depositor`
- `relay_origin_foreign_depositor`
- `relay_origin_not_indexed`
- `relay_origin_rate_limit`
- `relay_origin_unconfirmed`

The watcher carries the class in `valuation.relay_origin_error` and the runner maps it through
`source_initiation_refusal`. No reader matches the old string outside `memecoin_source_initiation.py` and
tests (grep 2026-10-09). Gamma's grader groups on `terminal_reason`.

## 5. Label-only pre-gate beside the Relay wait (Alpha option ii, 2026-10-09; the operator may veto)

Today 26 of 31 refused Relay buys were blocked by other gates anyway, yet they refused 8 s late as
`wallet_initiation_unproved`. ABYSS is the operator's example.

Built as `sniper/services/memecoin_relay_pregate.py` behind `MEMECOIN_RELAY_PREGATE` (off | shadow | on;
default off). There is no hook refactor: it calls the hook's own pure pieces, in the hook's order.
- **Ceiling:** `mcap_entry_decision` with `_wallet_mcap_ceiling`/`_wallet_mcap_floor`, then
  `_bigcap_lift` on an over-ceiling result.
- **Floor:** `source_buy_family_reason` with `_band_source_buy_ratio`, `_wallet_source_buy_ratio` and
  `min_source_buy_usd_for`.

**Inputs, local only (no RPC, no provider).**
- The mcap is the feed history (`resolve_mint_mcap` with the hook's arguments). Failing that, it is
  the volume observer's cached supply x the source price (`_cached_evm_source_mcap`).
- The source size is the watcher's own.
- The prior net is `source_prior_net_buy_usd`.
- Roster and exemptions are read from the ledger.

**The runner's config, not the watcher's (Gamma M2).** The runner judges on the overrides in
`memecoin_entry_knob_overrides` (applied by its own cycle only) and on the .env it last started with.
- `decision` takes every value it judges with in `knobs`. `for_row` passes
  `memecoin_entry_knobs.effective()` read-only, never `apply_overrides`, so nothing writes the settings
  module under the worker threads. The active override names ride on the row.
- Both directions of env drift are silent. Its per-wallet and lift settings, which come from env, may
  not be the runner's:
  - an env file newer than the watcher's own start gives `config_stale`;
  - an env file newer than the runner's start (Gamma L4: a watcher that restarted after an edit the
    runner has not taken) gives `config_stale_runner`, judged from
    `data/memecoin_runner.heartbeat` `started_at`;
  - an unreadable heartbeat or a missing `started_at` gives `runner_start_unknown`.
- The live case Gamma proved: with the .env floor at 200, the runner's override at 197 and the 2%
  tolerance, buys from $193.06 to $196 would have been labelled and lost.

**Silent unless certain:**
- the wallet is not armed on the chain, or the floor family is exempt for it;
- the mcap is unknown, or its basis is not one of history, evm_source or evm_source_cached;
- the ceiling result is not `over_ceiling` (under-floor or unresolvable is the hook's own call);
- auto-templates could admit the coin;
- the source size is unknown;
- any read fails.

**What a label does to the event.**
- The worker publishes the unproved row at once. Its valuation carries
  `relay_origin_error = pregate:<the hook's reason text>` and `relay_pregate` (mcap, basis, size, prior
  net).
- The runner refuses it as it would (it is unproved), with the terminal reason "<reason> [relay origin
  unproved]".
- **The proofs still record (Alpha condition 2).** The same worker then runs today's verification to its
  end: deposit re-check, Relay polls and the shared rule. Its outcome is written onto the already
  published row as `relay_origin_late` (error, proof_version, ms), with one UPDATE by the row's own
  writer and a `late_after_pregate` log line.
- The row is never published twice and the lanes are never signalled twice, so Gamma can grade every
  label against the proof that followed.
- A deposit that lands after a label is still claimed and recorded late; the buy stays refused.
- The pre-gate never runs on a row the deposit proved at detection: that row was never queued.

**Agreement check (Alpha condition 1).** `scripts/research/relay_pregate_agreement.py` replays every
recorded EVM attempt since 2026-10-01 that reached the mcap stage through `decision`.
- **Inputs:** each attempt's own mcap and basis, its `source_buy_floor` substage (size, prior net),
  its `bigcap_lift` verdict and `exemptions_applied`.
- **Rule:** wherever the pre-gate labels, the hook's terminal reason must have the same class, and an
  admitted attempt labelled is a failure. Exit 1 on any failure.
- **Run 2026-10-09:**
  - 1,065 replayed;
  - 286 over-ceiling labels, all agreeing;
  - 56 floor labels, 55 agreeing and 1 `config_drift` (ratio 0.002 then, 0.005 today, chain 1). Drift
    is resolved by the check with the hook's own functions, never read from the pre-gate;
  - 723 silent (614 mcap basis outside the three, 107 passes, 2 size unknown);
  - 0 failures.
- **It can fail:** a mutant that skips the band ratio fails 5 rows, 2 of them filled.
- **Not covered by the replay:**
  - Live mcap sourcing: the replay feeds the hook's recorded mcap. The live label's mcap and basis
    ride on the row for grading.
  - The lift's admit branch: no replayable attempt was lifted. It is unit-tested.

**Activation (Gamma M2).**
1. `shadow` first. The verdict rides on the row (`relay_pregate`, mode shadow) while the row waits as
   today, so the runner decides every graded row itself.
2. `scripts/research/relay_pregate_agreement.py --live` grades it against the runner's own terminal
   reason on the same origin, on proved rows only.
3. `on` only when `ready_for_on`: at least 30 graded labels and none that refuse what the runner
   admitted.

The live grade also covers what the replay cannot: live mcap sourcing, the lift's admit branch, and
any config difference between the two processes.

A quiet shadow is not an agreeing one (Alpha). Every operator `.env` edit silences the pre-gate
(`config_stale`) until the next watcher restart; a deploy relaunches the watchers. So `--live` reports
`rows`, `graded`, `config_stale` (all three stale reasons), `config_stale_share` and `silences` by reason
on their own lines, so coverage can be read as well as agreement. `ready_for_on` counts graded labels
only.

**Speed:** zero on proved buys. The pre-gate runs only on the Relay-wait worker thread after the
deposit check failed, and its reads are local SQLite. It never delays a Relay-verified publish: that
row publishes as before.

## 6. Latency (measured 2026-10-09, this host, NVMe)

- **Store consume**, as a one-statement UPDATE ... RETURNING on a 4,000-row WAL store beside a writer
  process inserting every 50 ms:
  - synchronous=NORMAL: hit p50 0.049 ms, p90 0.094 ms; miss p50 0.033 ms, p90 0.064 ms.
  - Default FULL: hit p50 6.5 ms, p90 6.8 ms.
  - The store uses NORMAL. A power loss can lose the last consume; the fill tx is still unique, and the
    attempt dedupes on it.
- **What it replaces** on the 5 proved timeouts: up to 8.2 s of Relay polling. On verified buys it
  replaces the Relay wait (p50 1.7 s, p90 7.8 s per Alpha's 152 checks) whenever the deposit is already
  in the store, which was every option C match (frame 0.68..2.19 s ahead).
- The fill path in the runner is unchanged: the same row reaches it earlier.

## 7. Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.** It opens a proof that admits on-chain-lane entries on five chains. Default off per
   chain (`MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS`). Every runner gate is kept.
2. **Fill path speed.** Added: one indexed SQLite statement (p90 0.094 ms) and one indexed registry read on the ledger (a PK lookup). Removed: the Relay wait on
   matched rows. No new network call on the decision path. Relay polling is unchanged for the rest; each
   poll adds one store read (~0.03 ms).
3. **Concurrency.**
   - Single use across processes is the one UPDATE ... WHERE used_fill_tx IS NULL OR = :tx.
   - The socket thread is the only writer of new rows. Roster refresh replaces a tuple under its lock
     (option C).
   - No module attribute or env var is mutated.
4. **Absent, zero, unknown.**
   - No row means `no_deposit`, which falls back to the Relay path, never a refusal by itself.
   - A duplicate order, a stale row (>120 s), a uid mismatch, an amount out of band or a shared depositor
     is no proof, and the Relay path still runs.
   - A zero amount is refused at the frame.
5. **Dependency failures.**
   - Socket down, 429 or closed: option C's 60 s backoff with no proofs, so today's Relay path runs.
   - Store locked: busy_timeout 50 ms, then no proof (Relay path).
   - Relay down: the deposit proof is unaffected.
   - Confirmer RPC errors: retried every tick, then `unchecked` at 90 s and paged.
6. **Units and identity.**
   - The order id is bytes32 hex, lowercase, compared with the fill calldata's last 32 bytes.
   - USDC has 6 decimals at the deposit; the fill is in USD from the watcher's own valuation.
   - Times are host time for both arrivals (same host), as option C.
   - Solana addresses are base58 and case-sensitive; EVM addresses are lowercase.
7. **Sample.** The coverage is our own armed wallets' Relay buys since 10-07 (n=156 checks, 39 refused
   txs). One shared depositor accounts for 15 of the refusals. A wallet's depositor history is short;
   the shared set grows with observation.
8. **Rollback.**
   - Empty `MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS` stops new deposit proofs; the Relay path is unchanged.
   - The store is additive.
   - Half-applied (socket roster widened, chains empty) proves nothing new.

**The check left behind:** a test where a registry-verified depositor of user A, also recorded funding
fills of users B and C, consumes nothing for A and refuses `relay_origin_shared_depositor`. It fails if
the shared-depositor rule is removed, keyed on the registry alone, or keyed on handles; under (b) a
second test proves A keeps its proof at two users and the page fires. One user under two handles (the
d47559fc fixture) is never shared. Also: a second fill tx on a consumed
order id is refused; a deposit arriving after detection is taken on the next poll.

## 7a. Residuals named in the build review (Gamma, b20b367d1)

- The socket subscribes with `maxSupportedTransactionVersion` 0. A v1 deposit is not delivered and
  falls back to the Relay path. Gamma read all 102 recovered Relay deposits: every one is v0.
- `MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS` validates against a constant list of funded EVM chains and
  refuses to load otherwise. A typo stops every importer, the funded runner included. This is
  intended (precedent: the Solana pool). Add Ink when it goes funded.
- Two lanes proving one fill: with 4663 listed and option C on, `rhfast:4663` and `onchain:4663` can
  both prove the same fill tx within ms. The guard is the partial UNIQUE index
  `idx_mc_pending_coin_uniq_v2` on (network, mint) WHERE status='reserved': the second reservation is
  None ("one position per coin: lost reservation race"). It is pinned by
  `test_two_lanes_proving_one_fill_reserve_the_coin_once`, which fails if the index widens.

## 8. Build order (one branch each, red tests first, Gamma)

1. Store, wider roster and required-account filter in the socket; the fast lane reads the store. Default
   off.
2. Watcher match plus the per-poll re-check; `relay-deposit-processed-v1` accepted per chain behind the
   knob.
3. shared/foreign classes and the named reasons (both paths).
4. The label-only pre-gate (section 5), default off; the operator may veto.
5. Alpha lists chains in `MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS`, starting with 4663 and 8453.
# Amendment 2026-10-10: preserve verified receipt initiation

The watcher fill builder accepts verified `relay-deposit-processed-v1` initiation alongside decoded Relay receipt economics, preserves it in the canonical fill proof and revalidates exact wallet/UID/mint/transaction/block/output/origin binding. RH receipt-backed handoff retains the configured on-chain deposit authority; missing receipt hash or acquired atoms cannot borrow this path. Receipt-less fast deposit proofs keep their separate option C gate. See `docs/audits/2026-10-10-evm-fill-incidents.md`.
