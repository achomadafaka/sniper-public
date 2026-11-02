# Follow age from chain time (BSC first) — design

Status: **WITHDRAWN 2026-09-30.** Its premise was a window-matching artifact. Matched on the exact
transaction, J7 `created_at` is within 2.41 s of the chain trade on every chain (56 n 30, 4663 n 83,
8453 n 8, solana n 705; 09-13 to 09-29). Late J7 events are late deliveries, and the gate already
ages those correctly from `created_at`. See the corrected section 5 of
`docs/audits/2026-09-30-missed-signal-map.md` and Beta's brain finding 35766f77. Nothing below is
to be built.

(Original status: SPEC ONLY. Money path (entry admission), operator-gated, default off. No code in
this change.)
Owner: Lambda. Order: Alpha 2026-09-29 (overnight), from the missed-signal map
(`docs/audits/2026-09-30-missed-signal-map.md`, section 5).

## Problem

The follow age gate refuses a J7 follow buy older than `MEMECOIN_REAL_FOLLOW_MAX_AGE_S` (60 s):
`memecoin_real_runner.real_entry_hook` calls `memecoin_runner.is_stale_entry(ev.created_at,
now_iso, max_age_s)`. For a J7 event, `created_at` is J7's own timestamp, not the chain trade's.

The map measured `created_at` against the on-chain watcher's block time for the same buy (same
wallet, chain and mint, within 600 s; 09-13 to 09-29):

| chain | matched n | created_at minus chain p50 / p90 / p99 (s) |
|---|---:|---|
| 56 | 53 | 1.98 / 142.8 / 404.4 |
| 4663 | 99 | 0.83 / 1.25 / 248.2 |
| solana | 1,260 | 0.93 / 14.1 / 28.8 |
| 8453 | 13 | 0.53 / 0.56 / 18.0 |

On BSC, 1 J7 buy in 10 is already more than two minutes old on chain when J7 stamps it, and the gate
reads it as fresh. Today the cost is $0: all 10 BSC buys and 5 4663 buys that reached us more than
60 s after the chain were refused by the mcap gate. The exposure is latent. It becomes live the
first time one of them passes the other gates.

The on-chain lane is not affected. There, `ev.created_at` is the detection's arrival or its
backfill's block time (`memecoin_onchain_lane`, the `source_time(row)` stamp).

## Proposal

1. **Chain basis first.** When the on-chain watcher has the same buy, the age is `decision now`
   minus the detection's `block_time`. "The same buy" means a row in `onchain_wallet_detections`
   with the same `network_id`, `source_user_id` and `mint`, and `detected_at` within 120 s of the
   J7 receipt. If more than one row qualifies, the basis is ambiguous and we fall to step 2. No new
   RPC: the detection is already in the ledger, written by the watcher before or alongside the J7
   event (watcher detect p50 0.6-1.3 s against J7 receipt p50 1.2-2.2 s).
2. **Fallback basis.** With no detection, the age is `now - created_at + L(chain)`. `L(chain)` is the
   p50 of `created_at - block_time` over matched pairs on that chain for the trailing 7 days,
   floored at 0. It is refreshed daily by an offline job into a small state file that is replaced
   atomically. The p50 corrects the typical lag without refusing every J7 signal on a chain whose
   lag distribution is bimodal. On BSC a p90 bound (143 s) would refuse every unmatched J7 buy at a
   60 s cap. The p90 is recorded beside it for the read.
3. **Legacy basis.** With no detection and a missing or stale `L` (older than 48 h), the gate
   behaves exactly as today (`created_at`).
4. **Shadow first.**
   - Every J7 follow attempt records `age_basis` (`chain` | `j7_plus_lag` | `j7_legacy`),
     `chain_age_s` and `j7_age_s` in its trace.
   - The gate decision is unchanged until a chain is named in the knob
     `MEMECOIN_REAL_FOLLOW_AGE_CHAIN_BASIS_CHAINS` (default empty).
   - Activation is per chain, by the operator, after a pre-registered read.

## Rule 5

1. **Money path: YES.** It decides admission (a stale refusal). Default off. Shadow fields first;
   then per-chain activation by the operator, after a pre-registered read. The read counts:
   - the J7 attempts the chain basis would have refused and the legacy basis did not;
   - how many of those would otherwise have filled;
   - their diagnostic outcomes (`memecoin_missed_entry_grader.simulate_first_touch`, as in the
     map).
2. **Speed on the fill path.** No RPC and no lock. The lookup is one indexed read of a table of
   about 3.7k rows, but `onchain_wallet_detections` today has only `(network_id, id)` and
   `UNIQUE(signature)`. An index on `(network_id, source_user_id, mint)` is part of the build, and
   the build must measure the lookup's p50 and p90 inside the runner (target: p90 below 1 ms)
   before activation. The `L` file is read at runner start and on refresh, never per decision.
3. **Concurrency and global state.**
   - The lookup is a read on the caller's connection.
   - `L` lives in an immutable dict that is swapped whole on refresh: no module attribute is
     mutated in place, and there is no capture-and-restore.
   - The offline job writes a temp file and renames it.
4. **Absent versus zero versus unknown.**
   - No detection means the chain time is unknown, and the fallback applies. It is never an age
     of 0.
   - A NULL `block_time` counts as absent.
   - A missing or stale `L` means the legacy basis, flagged in the trace.
   - A negative age (clock skew) is clamped to 0 and flagged.
   - A detection with `detect_latency_s` above 60 s is still valid (chain time is chain time).
5. **Failure modes.**
   - Watcher down: no detections, so the fallback (the map measured watcher recall at 95% on
     Solana but 63% on BSC, where sunnydog06's relayed buys are never detected).
   - Lag job fails: stale `L`, so the legacy basis.
   - Wrong match (the same wallet buys the same mint twice within 120 s): ambiguous, so the
     fallback.
   - Ledger read error: the legacy basis, logged. The gate must not fail closed on an infra error
     it did not have before.
6. **Units and identity.**
   - `block_time` is epoch seconds on the chain clock (1 s resolution); the decision time is the
     corrected real-UTC clock. The pair mixes clocks by the J7 transit carried in the correction
     (about 0.05-0.1 s), which is negligible against 60 s.
   - Identity is `(network_id, source_user_id, mint)`. EVM mints are lowercase in both tables, and
     that must be checked in the build.
7. **Sample.** `L` comes from matched pairs, meaning buys by wallets the watcher covers. The
   fallback applies exactly where it is least representative: relayed-owner wallets and chains
   with low recall. The read must report the basis mix per chain.
8. **Rollback.** Empty the knob and the behaviour is today's. The trace fields stay (they are
   additive), and so does the index (harmless).

## Checks the build leaves behind

- A 56 J7 event whose `created_at` is 5 s old, with a detection whose `block_time` is 150 s old, is
  refused as stale when 56 is in the knob, and admitted, with `age_basis=chain` and
  `chain_age_s≈150`, when it is not.
- With no detection it keeps the fallback or legacy basis, never an age of 0.
- An ambiguous match (two detections in the window) takes the fallback.
- A stale `L` file gives the legacy basis.
- A guard test proves the lookup issues no network call. A fake RPC must stay unused.

## Not in scope

- EVM J7 events that carry a tx hash could be aged exactly through a receipt read. That is an RPC
  on the entry path, and Rule 5 item 2 refuses it.
- Retiring J7 on 56: watcher recall is 63%.
