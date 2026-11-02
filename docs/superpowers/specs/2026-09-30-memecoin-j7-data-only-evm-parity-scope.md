# J7 as data only on EVM: watcher parity scope (4663, 56)

Status: scope with measured numbers, spec only, no code. Operator order 2026-09-30 (via Alpha): J7
must never touch a funded decision. Solana is already `MEMECOIN_J7_SOLANA_MODE=shadow`. On EVM,
`MEMECOIN_J7_SHADOW_EVM_CHAINS` is unset, so J7 is live. The fallback spec 34964b734 stays parked.

Instrument: `scripts/research/j7_watcher_parity.py`, read-only. It was pre-registered as brain
episode 02c9d9c4 before any number was computed. Every match is on the exact transaction: J7
`fomo_event_id` `swap:<bot>:<tx>:<trader>:<mint>` against the detection signature
`<chain>:<tx>:<mint>`, or against `onchain_evm_sell_shadow.tx_hash` for sells. Window: 2026-09-13 to
2026-09-30 08:0xZ.

## Answer

- **Buys: close to parity for today's roster.** Every J7 buy by a wallet armed now came from an
  address the watcher subscribes now: 987 of 987 on 4663, 357 of 357 on 56 and 24 of 24 on 8453.
  The relayed-owner gap is gone with the 09-29 roster cut; sunnydog06 is disarmed. What remains is
  delivery. The misses cluster in incidents: none on 4663 since 2026-09-29 10:00Z (32 of 32) and
  none on 56 since 12:36Z (5 of 5). The forward sample is too small to certify.
- **Sells: not at parity, but no funded effect today.** The watcher decoded 26 of 58 J7 sells on
  4663 (45%) and 16 of 27 on 56 (59%) since its sell shadow started (09-29). Today alone: 5 of 17
  and 3 of 6. On EVM, a J7 sell reaches a funded position only through `real_mirror_on_sell`. That
  holds when the frozen contract's mirror is off (`contract_mirror_disabled`). Every funded EVM
  position opened since 09-15 is follow-30-50 with `mirror_on_sell=False`: 4663 v4 x100, 56 v2
  x39, 8453 v2 x2, 56 manual x2. `MEMECOIN_REAL_BAND_POLICY_BANDS` is empty. Size-aware selling is
  Solana-only (`MEMECOIN_REAL_MIRROR_SIZE_CHAINS=solana`). The gap becomes real the day an EVM
  policy mirrors.

## Buys

Covered = the wallet had a watcher detection of another transaction before the buy. That avoids
counting a buy's own detection as its coverage.

| chain | covered J7 buys, full window | exact-tx recall | misses | since the last miss |
|---|---:|---:|---|---|
| 4663 | 87 | 88.5% (77) | 10: 09-28 08:38 (3), 15:12 (3); 09-29 05:43-49 (3, the dead-subscription incident), 09:49 | 32 of 32 since 09-29 10:00Z |
| 56 | 42 | 42.9% (18) | 24: 21 on 09-28 13:30-16:57 (one dead-socket stretch), 23:23; 09-29 02:30, 12:35 | 5 of 5 since 09-29 12:36Z |
| 8453 | 4 | 100% | none | no buys |

Watcher detection minus J7 receipt, on hits (p10 / p50 / p90): 4663 -0.68 / -0.31 / -0.19 s,
56 -1.46 / -1.21 / -0.68 s, 8453 -0.52 / -0.43 / -0.40 s. When the watcher sees a buy, it is
earlier than J7, so shadowing adds no latency on a hit.

Today's roster (armed now, `evm_watched_owners`):
- 4663: 15 armed, 14 watched.
- 56: 18 armed, 16 watched.
- 8453: 1 armed, 1 watched.

The unwatched armed wallets (1 on 4663, 2 on 56) made no J7 buy in the window.

**Correction to the missed-signal map, section 4.** Its recall (4663 84.3%, 56 63.4%) matched a
detection of the same (wallet, mint) within 120 s. That scored a missed repeat buy as a hit, and it
counted a buy's own detection as coverage. The exact-tx numbers above supersede it.

## Sells

Every sell candidate the current watcher processes received has been decoded (4663: 2 of 2; 56:
1 of 1; heartbeat `sell_shadow`), so the losses happen before decoding. Either the Transfer-from log
never arrived, or it was prefiltered. Which one is not measured yet.

## What parity needs

1. **Strict data-only mode (build).** `_j7_evm_covered` shadows only covered wallets whose watcher
   is healthy; everything else stays J7-live. "J7 never touches funded decisions" means an
   uncovered wallet or an unhealthy watcher gives NO entry instead of a J7-live one. Consequence:
   EVM arming must require a watched address (today 3 armed wallets have none, with no buys).
2. **Delivery gate before switching a chain.** Exact-tx buy recall of at least 98% on at least 50
   covered J7 buys per chain, read daily with this script. Since the last miss: 4663 is at 32 of
   32, 56 at 5 of 5.
3. **Miss recovery stays J7-referenced.** The J7 cross-check forces a socket reconnect when J7
   reports a watched buy the socket missed, and backfill recovers it at +12-24 s (D2 read). That
   uses J7 as a liveness signal, not as an entry. **Operator call: is a J7-triggered reconnect
   allowed under "data only"?** Without it, a dead filter on a quiet chain is found only by the
   canary (reads due 2026-10-01).
4. **A sell guard (small build, before any EVM mirror).** Any EVM policy with `mirror_on_sell` or
   an EVM entry in `MEMECOIN_REAL_MIRROR_SIZE_CHAINS` needs watcher sell parity first, so a guard
   test should refuse that combination while EVM is data-only. The sell diagnosis comes before
   any such change: for each missed J7 sell, did the socket deliver a Transfer from the address in
   that tx (the cross-check's `delivered_tx` sees it), and was it prefiltered or `not_a_sale`?

## Historical stake (context, not a forecast)

Full window, all wallets armed at the time, most of them disarmed now. The signals the watcher
never saw or saw more than 10 s late carried:
- 4663: 2,586 attempts and 114 fills, realized -$213.51.
- 56: 1,219 attempts and 40 fills, realized +$136.88.
- 8453: 197 attempts and 1 fill, -$9.89.

## Amendment 2026-09-30: J7 buys with no transaction in their id are counted apart

Written before the next gate read (Alpha's go, 2026-09-30).

- The gate matches a J7 buy to a watcher detection on the exact transaction in the J7 id
  (`swap:<bot>:<tx>:<trader>:<mint>`). Some J7 buys arrive as `swap:<uuid>`, with no transaction
  and no trader address. They cannot be matched, and the gate dropped them without a count: they
  are in neither `n` nor `hits`.
- Since the gate start (2026-09-30T08:20:59Z, read at 15:55Z, all wallets): 6 of 45 J7 buys on
  4663, 7 of 20 on 56, 2 of 15 on 8453. The watcher's own J7 cross-check counts the same ids as
  `unparsable`.
- The gate output now carries, per chain and for the gate window, `no_tx_id` (all wallets) and
  `no_tx_id_armed` (wallets armed on that chain now).
- **The PASS rule is unchanged** (at least 50 covered buys, at least 98% detected within 60 s).
  **A PASS says nothing about `swap:<uuid>` buys.** Whether the watcher saw them is unknown, not
  yes. Matching them by wallet and coin inside a time window is the method that scored missed
  repeat buys as hits, so it is not used.
- Open: what these events are (which J7 path writes an id without a transaction), and whether a
  chain can be called data-only while a share of its J7 buys cannot be checked. That is a
  decision for Alpha and the operator once the shares are known over a full day.
