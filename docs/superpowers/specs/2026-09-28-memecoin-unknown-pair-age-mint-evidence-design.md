# Coin-age floor: decide an UNKNOWN pair age from the mint, off the fill path

Status: DRAFT for the operator's yes (Alpha's order 2026-09-28 ~18:12Z: "draft the rule-5 specs,
no code"). Author: Beta. Evidence: Gamma, `docs/audits/2026-09-28-fast-crash-stops.md` (pre-registered
f3d07c00, result 7d8fa6dd, `scripts/research/fast_crash_markers.py`). Money path, entry side.

## Problem

`MEMECOIN_REAL_MIN_COIN_AGE_S=120` has been live since 2026-09-15 17:27:53Z. `coin_age_refusal`
(`sniper/services/memecoin_risk.py:63`) reads `facts["pair_age_s"]` and ADMITS an unknown pair age by
design. The mint-evidence warm (`_warm_mint_age`, `memecoin_real_runner.py:2123`, 7d639e70) runs only
when a pair age EXISTS and is under the floor. DexScreener has not indexed a pair for the newest
coins, so an unknown pair age is disproportionately a brand-new coin. The operator's 120 s rule is
therefore not enforced on exactly the coins it targets.

Gamma, funded Solana closes since 09-14 (n=341), coin age from the mint's first transaction:
- 21 of the 22 fills on coins under 120 s came in with `pair_age_s` unknown: -$267.44, 95% CI
  [-881.65, +322.66], 10 wins, 3 of the 7 fast crashes (<= -50% within 120 s). Today's Clank 66478
  was 52 s old.
- Unknown-pair-age fills on coins >= 120 s: n=63, +$358.81. A blanket "refuse unknown age" would
  cost money, so it is NOT proposed.
- The money case is not proven (the CI spans 0). The case is rule consistency.

## Change

1. **Start the lookup without waiting.** In `_warm_mint_age`, when the chain's floor is > 0, the
   chain is Solana and `pair_age_s` is unknown, call `memecoin_coin_age.warm(network_id, mint,
   wait_s=0)`. It starts the existing lookup: Helius `getTransactionsForAddress` on the mint,
   ascending, first successful tx, on the WATCHER key `ONCHAIN_WATCH_HELIUS_RPC_URL`, in its own
   daemon thread. It returns at once. The known-pair-age branch keeps today's bounded wait.
2. **Decide just before signing.** A new pure function `coin_age_presign_refusal(network_id, mint,
   pair_age_s)` reads the cache only (`cached_age_s`). The outcomes:
   - pair age known: returns None; the reservation gate already decided;
   - cache empty (lookup not back, 429, error): returns None and marks
     `coin_age_presign resolved=false` (fail-open, as today);
   - mint age >= floor: returns None and marks `resolved=true`;
   - mint age < floor: returns `coin_too_young: mint 52s old (pair age unknown), floor 120s`.
3. **The executor calls it** through a new `execute_entry(..., presign_refusal_fn=None)` kwarg. The
   call sits after the chase and post-peak confirmations and before the swap build. On a refusal
   the executor runs `m.release_pending(conn, pending_id, "coin_age")` and returns `{"status":
   "rejected", "reason": "coin_too_young: mint Ns old (pair age unknown), floor Ms"}` with the quote
   fields. As built, this is `rejected` rather than a new `skipped_coin_age` status: every caller
   already treats `rejected` as a refusal, and the reason carries the reservation gate's
   `coin_too_young` family. A check that raises admits. `real_leg` passes it, and so do the
   funded seams, the oversize retry and `_requote_fresh`, the same path `chase_reference_note` takes
   (0d771690).
4. The refusal is terminal. `_is_transient_entry_miss` treats `rejected` as transient only for
   the unresolved-signed prefix, so the entry retry lane never re-queues it. A test holds this.
5. **Knob** `MEMECOIN_COIN_AGE_UNKNOWN_PAIR_CHECK`, default 0 (inert). 1 means on, on the operator's
   yes. `MEMECOIN_COIN_AGE_MINT_EVIDENCE=0` still disables every mint-evidence read.

### Where the lookup starts: measured, both options

| start point | lookups per active hour (Solana general path, since 09-21) | time to signing |
|---|---|---|
| **at classification (recommended)** | median 32, p90 63, max 96 (5,284 of 5,836 classified attempts had no pair age) | p10 164 ms, p50 511 ms, p90 1,017 ms (n=10 signed attempts with both marks, since 09-25) |
| after the gates, at the reservation | median 1, p90 3, max 12 (92 of 321 reservations) | n=5: 12 / 141 / 532 / 985 ms. The early-quote J7 path signs within ~12 ms of the reservation, so the lookup is rarely back |

Lookup latency: `getTransactionsForAddress`, measured on the RESEARCH key (same provider, different
key, flat book, 2026-09-28 18:15Z, n=20, new connection per call): p50 49 ms, p90 73 ms, max 77 ms.
From classification, the lookup is back before signing on at least the p10 path (164 ms). From the
reservation, it misses the early-quote path. Starting at classification costs about 32 extra watcher-key
calls an hour, and Helius's credit cost per call is unmeasured. Cache hits dedupe repeated attempts on
one mint.

## Edge cases (CLAUDE.md rule 5)

1. **Money path: YES, entry.** It can only REFUSE an entry that is admitted today, and only when
   the pair age is unknown and the mint is proven younger than the floor. It never admits anything
   new. It never touches exits, sizing or signing, beyond one early return before the swap build.
2. **Speed.** No new wait on the fill path. The lookup runs in its own thread from classification.
   Measured in the build (n=1000 each, with a 50 ms lookup kept off-thread): starting it,
   `warm(wait_s=0)`, costs p50 40.5 us, p90 54.7 us in the hook; the pre-sign check (a cache read
   under `memecoin_coin_age._LOCK`) costs p50 3.2 us, p90 3.3 us. A refusal after the quote spends one Jupiter quote on a
   coin we then refuse. `MEMECOIN_COIN_AGE_UNKNOWN_PAIR_CHECK=0` restores the fill path byte for
   byte.
3. **Concurrency.** `warm` is already thread-safe: `_LOCK` guards the cache, and `_IN_FLIGHT` holds
   one lookup per key, so two attempts on one mint make one call. The check only reads. No module
   attribute, env var, resolver or shared connection is written. Daemon lookup threads are bounded
   by `HTTP_TIMEOUT_S` (3 s).
4. **Absent vs zero vs unknown.**
   - Pair age unknown means the lookup runs.
   - Mint age unknown (lookup not back, 429, malformed answer, no Helius key) means ADMIT, as
     today, marked `resolved=false`. The forward read counts it.
   - A mint age of 0 s (first tx in this second) is a measured young coin: refuse.
   - A pair age of 0 is KNOWN, not unknown. It stays on the reservation gate's path.
5. **Dependency failures.** Timeout, 429, 5xx, a partial page or a malformed body leave the cache
   empty, so the entry is admitted as today. A WRONG answer, a first tx older than the true mint
   (a precomputed address funded earlier), makes the coin older, so it admits (fail-open). Failed
   first transactions are already skipped (`_helius_first_ts`).
6. **Units and identity.** `blockTime` is whole validator seconds and can sit 1-2 s off our clock
   (finding-solana-blocktime-is-not-a-latency-anchor-2026-09-28). The age is `time.time()` minus
   that. Against a 120 s floor, the error is at most about 2% of the floor; this change does not
   measure it. The key is `(network_id, mint)`, base58, Solana only.
7. **The sample.** Gamma's 341 closes are every funded Solana close since 09-14 (robot rows). The
   leak rows are a subset defined by the mint's age, which we did not choose. The forward read has
   to show how many unknown-age attempts resolve before signing, from the `coin_age_presign`
   substage: resolved and refused, resolved and admitted, unresolved.
8. **Rollback.** `MEMECOIN_COIN_AGE_UNKNOWN_PAIR_CHECK=0` plus a runner restart. Half-applied: a
   new runner with the knob at 0 is today's behaviour. A new executor with an old real_leg never
   receives the kwarg, and the entry is admitted.

**Review fix (Lambda F1, 2026-09-28).** `_helius_first_ts` sent `getTransactionsForAddress` with
no commitment, so the read defaulted to finalized. Measured live on the research key at 18:45Z
(pump.fun program, newest row, 6 samples each): the default ran median 9.9 s behind, finalized
10.4 s and confirmed 1.1 s. A mint younger than about 10 s returned no rows and was admitted, which
missed exactly the coins the floor targets. The lookup now reads at `confirmed`. That covers both
warm paths (known-young and unknown), because they share the function. A request-body test holds
it.

**Not handled, stated:**
- The slot lane. It classifies with a price only, so its pair age is always unknown. It is the
  S+1 speed path, it had 0 crashes in 24 closes, and 1 of its 23 fills was on a coin under 120 s
  (+$75.68, its best fill). It needs its own decision.
- EVM chains. Their mint evidence is our tape's lower bound, and there is no crash evidence here.
- The known-pair-age branch keeps its synchronous 0.4 s wait. Moving it to the same pre-sign
  check would take that wait off the refusal path. That is a separate follow-on.
- An early-quote attempt that signs before the lookup is back is admitted (fail-open).

## Check left behind

`tests/test_memecoin_coin_age_presign.py`:
- unknown pair age plus a mint age under the floor refuses before signing, and release_pending
  runs;
- a mint age >= floor, an empty cache and a known pair age each admit;
- the knob at 0 never calls the check;
- the kwarg reaches the executor through real_leg, the seams, the oversize retry and
  `_requote_fresh`;
- `skipped_coin_age` is never re-queued;
- the lookup is started without a wait (`warm(..., wait_s=0)`) only when the pair age is unknown.
