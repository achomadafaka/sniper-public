# J7 as data only on EVM: strict mode, forward gate, reconnect, sell guard

Status: build spec. Money path (EVM entry admission and the EVM mirror exit). Operator-gated,
default off. Operator orders 2026-09-30:
- J7 must never touch a funded decision.
- A J7-triggered watcher reconnect is allowed (brain fact 4c1b7846).

Scope and numbers: `docs/superpowers/specs/2026-09-30-memecoin-j7-data-only-evm-parity-scope.md`.
This supersedes the parked J7 fallback spec (34964b734): a fallback is J7 touching an entry.

## Change

1. **Knob `MEMECOIN_J7_DATA_ONLY_EVM_CHAINS`**, comma-separated chain ids or names. Default empty,
   which is today's behaviour. It is parsed with the existing `_evm_chain_knob`, so an unknown
   entry is warned once and ignored, and "solana" is ignored.
2. **Buys.** In `j7_shadow_event`, a J7 follow buy on a listed chain returns the shadow copy
   unconditionally. It skips `_j7_evm_covered` and the watcher-health read, so an uncovered wallet
   or an unhealthy watcher gives no entry instead of a J7-live one. The shadow decision is still
   recorded exactly as today. The on-chain watcher and slot lanes are untouched: they are the only
   entry sources on a listed chain.
3. **Sells (the guard).** `real_mirror_on_sell` returns `{"status": "j7_data_only"}` for a J7 sell
   on a listed chain, before any evidence read and before any exit. Today this changes nothing,
   because every funded EVM contract has `mirror_on_sell=False`. It makes "never touch" hold even
   if an EVM policy mirrors later. The watcher's own decoded sells are not wired to the mirror;
   that is out of scope until sell parity (see the scope).
4. **Reconnect.** `memecoin_onchain_evm_j7_xcheck_chains()` returns its own list plus every
   data-only chain. A data-only chain therefore always runs the D2 cross-check: a J7 buy by a
   watched address that the socket did not deliver within the grace forces one reconnect, and gap
   backfill recovers it. J7 acts as a liveness signal only.

## Forward gate (pre-registered, before a chain is added to the knob)

`scripts/research/j7_watcher_parity.py --gate` prints PASS or FAIL per chain:
- **Population (amendment f1888d26d, POST HOC):** this amendment was written AFTER the first live
  gate read (08:16Z, 4663 15.6% on n 276), which used "armed and watched NOW" over 7 days and
  counted buys from before the watcher subscribed those addresses. The population is now J7 follow
  buys whose J7 trader address was in the watcher's acked transfer set at the J7 receipt. The set
  comes from the watcher's own log (`acked transfer set (n): ...`, logged on every change since
  2026-09-29T21:06Z).
- **Restart windows count (Gamma MEDIUM 2):** a process start makes the set unknown until its
  first acked line, and those windows are where misses happen. A buy in one counts when its
  address is in the nearest known acked set before or after. It is a miss unless it is detected
  within 60 s, and it is tallied as `unknown_watch_state`.
- **Forward only (Gamma MEDIUM 1):** only J7 receipts after 2026-09-30T08:20:59Z (the amendment's
  build commit) count toward n, whatever `--since` says (`gate_start`). What was seen before is
  printed as `seen_before_gate_not_counted` and never enters the gate: 4663 17 of 17 (1 late),
  56 2 of 2, 8453 none.
- **Hit:** a watcher detection of the exact tx no later than 60 s after the J7 receipt (the follow
  age gate). A backfill recovery inside 60 s counts as a hit and is also counted separately as
  late (more than 10 s).
- **PASS:** n >= 50 AND hit rate >= 98%.
- **Also printed:** the armed wallets on the chain with no watched address. Under data-only they
  cannot trade there.

The read keeps running after a chain is switched, since J7 events are still recorded. A FAIL is
the operator's cue to remove the chain. Both chains are short of n = 50 today.

## Edge cases (CLAUDE.md rule 5)

1. **Money path.** Yes. Only J7-originated buys and J7-originated mirror sells on listed chains
   change, and both can only be removed, never added. Stops, TP, trail, timecap, catastrophe and
   manual exits are untouched, as is every Solana path.
2. **Speed on the fill path.** A listed chain skips the coverage and health reads it does today,
   so the change removes work. The added check is one frozenset membership after a knob parse
   (existing parser, measured p50 0.0003 ms, p90 0.0003 ms). No provider call, lock or retry.
3. **Concurrency.** The knob is read-only and parsed at call time. The xcheck union is read once
   per watcher process at start.
4. **Absent vs zero vs unknown.** An empty knob means today. An unknown chain is ignored with one
   warning. A listed chain with an unreadable watcher heartbeat still shadows: unknown health
   never makes J7 live.
5. **Dependency failures.**
   - Watcher down on a listed chain: no entries there until the watchdog restarts it. Gap
     backfill then recovers detections, and entries older than 60 s are refused as stale. This is
     the cost the operator accepted.
   - J7 down: no cross-check reference. The silent-socket detector still runs, and the canary
     reads are due 2026-10-01.
6. **Units and identity.** Chains go through `canonical_chain_id`, and the knob accepts names.
   Exact-tx identity in the gate is `<chain>:<tx lowercase>:` against the detection signature.
7. **Sample.** The gate counts J7 buys by armed, watched wallets. Our own behaviour does not put a
   row in it.
8. **Rollback.** Remove the chain from the knob and restart through the deploy script. Half-applied
   state (runner new, watcher old): the watcher's xcheck list lacks the union until its restart.
   The deploy restarts both. Today's `.env` already lists 4663, 56 and 8453 in
   `MEMECOIN_ONCHAIN_EVM_J7_XCHECK_CHAINS`.

## Checks the build leaves behind

- An empty knob gives the same `j7_shadow_event` results as today: guard tests on covered,
  uncovered and unhealthy.
- On a listed chain, an uncovered wallet's buy and a buy under an unhealthy watcher both come back
  as the shadow copy. The same buy on an unlisted chain stays live.
- A J7 sell on a listed chain with a mirroring contract returns `j7_data_only`, and `exit_fn` is
  never called. An unlisted chain keeps today's mirror.
- `memecoin_onchain_evm_j7_xcheck_chains()` includes every data-only chain.
- `--gate`: PASS and FAIL on fixtures at the n and rate boundaries. A recovery after 60 s is a
  miss.
- The runner-source guard tests stay in the covering set.
