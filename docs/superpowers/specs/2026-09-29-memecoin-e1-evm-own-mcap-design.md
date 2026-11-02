# E1 on 56 and 8453: our own band mcap, no DexScreener (rule-5 spec)

Date: 2026-09-29. Author: Beta. Order: operator item 2 via Alpha ("no dex anywhere"), the EVM half
of inventory row E1 (the Solana half is 2026-09-28-memecoin-e1-solana-own-mcap-design.md). No code
before Alpha's go.

## What decides a 56/8453 band today (origin/main a7510b597, memecoin_real_runner.py ~:9720-9905)

1. `event`: J7's own `their_market_cap`.
2. `history`: same-mint feed history, priced at this entry's price.
3. DexScreener `market_cap` (`stats_fn`, the GET that also feeds the other market-data gates).
   `dex_mcap_allowed(chain)` is True on 56/8453.
4. `evm_source`: the LAST resort. Source fill price x on-chain totalSupply, through
   `build_evm_mcap_for`. On a cache miss that is two SYNCHRONOUS eth_calls (decimals, totalSupply)
   on the chain's custody RPC.
5. Unresolved: EVM refuses `mcap_unresolved`. There is no assume path on EVM: `assumed_nano` applies
   only to Solana and the DexScreener-blind chains.

Ledger, 181 classifications on 56/8453 since 2026-09-27 09:00Z:
- event and history decide most bands;
- DexScreener decided 13, including 6 of the 10 fills in that window;
- `evm_source` decided 0, because DexScreener always answered first.

With DexScreener removed, those 13 would reach `evm_source` today, with its synchronous reads on the
fill path, or refuse.

Alpha's two suggested inputs do not exist before the band on EVM:
- EVM token safety reads no supply (bytecode, sell route, sell simulation), and it runs in the leg,
  after the band.
- There is no EVM early quote, and the executor's quote is in the leg, after the band.

Using either would move the band decision behind the quote. That reorders the entry and is out of
scope here.

## Change

1. **Knob.** `MEMECOIN_EVM_OWN_MCAP_CHAINS`, comma-separated chain ids, default empty (today).
   - On a listed chain, `dex_mcap_allowed(chain)` is False, so DexScreener never sets the band.
     The GET itself still runs, because volume, pair selection, liquidity and names still read it.
   - The own stage below replaces stage 3.
2. **The supply read rides the GET.**
   - When a listed chain's event reaches stage 3 (no event mcap, no history), the EVM prereads
     start a future that reads decimals and totalSupply through the SAME cached path
     `build_evm_mcap_for` uses. That is the process-wide `_supply_cache`, which the marks also fill.
   - It starts at the same instant as the GET.
   - A cached coin costs nothing: any coin we held or banded before.
3. **The own stage replaces stage 3 on a listed chain: `evm_source`, moved up.** It takes the
   future's supply and multiplies it by the source fill price: `their_price` for J7 events, and the
   detection's `source_price` for on-chain lane events. That is the basis Solana's E1 uses
   (`onchain_source_fill`).
   - The basis stays labelled `evm_source`.
   - If the future has not finished when the GET returns, the stage waits for it up to
     `MEMECOIN_EVM_OWN_MCAP_WAIT_MS` (default 150). Past that the coin is unresolved.
4. **Unresolved refuses, as it does today on EVM.** There is no assume branch to change. A coin no
   own stage prices is refused `mcap_unresolved`, never banded `assumed_nano`.

Activation (Alpha): `MEMECOIN_EVM_OWN_MCAP_CHAINS=56` first. 8453 waits for a latency sample (edge
case 2). Then restart the runner.

## Agreement, measured read-only (2026-09-29 04:1xZ)

- **Supply.** BSC totalSupply was read through the research RPC `bsc-dataseed1.defibit.io`. 8453
  supply came from our own marks (mcap/mid, where the marks' mcap is price x totalSupply). No Base
  research RPC is approved.
- **J7 event mcap / (their_price x totalSupply)**, J7 buys since 2026-09-22:
  - 56: n=338, p10 0.951, p50 0.989, p90 1.016; same band 333 of 338.
  - 8453: n=57, p10 0.980, p50 0.995, p90 1.008; same band 55 of 57.

  J7's mcap is total-supply based.
- **DexScreener supply (fdv/price) / totalSupply:**
  - 56: n=101, p10 0.989, p50 1.000, p90 1.000.
  - 8453: n=11, p10 0.911, p50 1.000.

  DexScreener `marketCap / fdv` = 1.000 at p10-p90 on both chains. So the supply basis agrees, and
  any difference is price.
- **The 13 DexScreener-banded decisions since 09-27 09:00Z:** our mcap (the source price at the
  decision x supply) is in the same band on 12 of 13. DS/ours at the median is 0.88, range
  0.58-1.13: ours is HIGHER.
  - The source fill carries the source's own impact, and DexScreener lags by about 25 s plus a
    30 s CDN.
  - The one band change is 0x3074bb17 on 56: DS $240,110 micro, ours $286,803 small, over the
    $250k ceiling. Ours would REFUSE it: narrower, never wider.
  - 8453's supply on these rows is DexScreener's fdv/price (proxy), for lack of a Base research
    endpoint.

## Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.** The band decides admission (ceiling, floor, reservable bands) and the exit
   policy.
   - Ours reads about 12% above DexScreener's at the median, so a boundary coin can move up a band:
     micro becomes small (a different exit policy), or it crosses the ceiling and is refused.
     Admission only narrows.
   - Exits, sizing and signing are untouched.
2. **Speed.**
   - Measured from traces since 2026-09-27: the market-data stage when the GET runs has p90 208 ms on
     56 (n=116) and 202 ms on 8453 (n=27). The p50 is 1 ms, because most events carry an mcap and
     never GET.
   - A funded EVM read (`evm.balance_pre`) on 56: p50 28 ms, p90 65 ms (n=10). The supply pair runs
     inside the GET, so 56 adds nothing measurable.
   - On 8453 the same read measured p50 269 ms, p90 509 ms, on n=2 only. Too few to promise
     anything, so 8453 activates only after a sample: the stage records the read's own ms as a
     trace substage, and 20 organic 8453 reads are needed.
   - Today's `evm_source` makes the same reads SYNCHRONOUSLY after the GET. This change can only
     shorten a path that already exists.
3. **Concurrency.**
   - `_supply_cache` is a module dict written with whole-value assignments. The same key always gets
     the same value, from the prereads thread and the marks thread.
   - The future belongs to the attempt's own prereads object.
   - No global is captured and restored.
4. **Absent vs zero vs unknown.** Every unknown is `None`, which is unresolved and refused:
   - decimals unreadable, or outside 0-36;
   - totalSupply `0x`, 0 or unreadable;
   - a price of 0 or None;
   - the read not done within the wait.

   A cached supply is never re-read. A token that mints after we cache it would read low. The
   existing `build_evm_mcap_for` makes the same assumption and leans on token safety. EVM safety does
   NOT check mint authority, so this case is NOT handled; it is named here.
5. **Dependencies.**
   - A custody-RPC 429 or timeout on the supply read means unresolved, which refuses: narrower.
   - It ADDS one decimals + totalSupply pair per NEW coin reaching stage 3, per process, on the
     chain's custody pool (Gamma, correcting this spec: `evm_source` decided 0 bands, so it spent
     about 0 reads). That is at most about 116 pairs on 56 since 09-27. It is pooled through
     `evm_wallet._rpc`, so a 429 there penalizes a custody-pool member that funded reads also use,
     as the old last resort would have.
   - A future that outlives its wait holds a worker until its read ends (10 s per call). A burst
     of slow reads queues, and later coins read `wait_expired`, which refuses: narrower. At a
     graceful stop, the interpreter joins in-flight reads. That is harmless (idempotent reads), so
     no shutdown hook was added.
   - No third party: DexScreener is out of the band.
6. **Units and identity.**
   - Decimals and totalSupply come from the same token address on the same chain.
   - `their_price` and the detection's `source_price` are USD per whole token. The J7 agreement of
     0.989 confirms the unit.
   - `(chain, mint)` keys the cache, with the mint as given, which is how the marks key it too.
7. **The sample.**
   - The agreement rows are J7 buys of the wallets armed that week plus our own decisions.
   - The 13-row band comparison is small.
   - The forward read: per day on each listed chain, the count of `evm_source` and `mcap_unresolved`
     outcomes, beside the band DexScreener would have given (the GET still runs, so its
     `market_cap` stays in `market_stats`).
8. **Rollback.**
   - Empty the knob and restart.
   - Half-applied (knob set, code older): the knob is unread and the behaviour is today's.

## Check left behind (red first)

`tests/test_memecoin_e1_evm_own_mcap.py`:
- knob empty: 56 bands from DexScreener exactly as today;
- knob 56: a coin with a DexScreener mcap and no event/history bands `evm_source` at
  their_price x supply, never at the DexScreener number;
- the supply future is started with the GET: a slow GET plus a fast read adds no wait, and a read
  slower than the wait refuses `mcap_unresolved`;
- supply 0/None, decimals None, or price 0 refuses. It never assumes on EVM;
- a cached supply makes no read;
- 8453 unlisted while 56 is listed: 8453 unchanged.

## Build notes (Alpha's go, 2026-09-29)

- The supply read runs on its own 4-worker pool (`_start_evm_supply_read`), not inside the EVM
  prereads object. The prereads exist only with `MEMECOIN_ENTRY_EARLY_GATE_READS_EVM` on and the
  balance seams present, and this read must not depend on either. It starts right before the
  prereads and the GET.
- `dex_mcap_allowed()` is unchanged. The own chain is excluded at the stage-3 line, because
  `dex_mcap_allowed` also routes MANUAL entries. Residual, as on Solana: a manual 56 entry still
  bands from DexScreener.
- The old synchronous last resort is skipped on a listed chain: the own stage already tried, with a
  bounded wait.
- (a) STOP line: `scripts/research/e1_evm_own_mcap_read.py --chain 56 --since <activation>`. More
  than 5% of the first 50 band decisions refused `mcap_unresolved` (3 or more, so it can fire
  before the 50th) means unset the chain and restart. The same read prints the 8453 activation
  gate: 20 cache-miss reads with p90 under the GET's p90.
- (b) Trace substage `evm_own_supply`: `outcome` (ok, wait_expired, read_failed, no_supply,
  no_price, not_started), `read_ms` (the read's own wall, 0 on a hit), `cache_hit`, `waited_ms`
  (how long the stage blocked after the GET), and `mcap`.
- (c) Tests: a 2 s read against the 150 ms knob ends the decision within the knob, not the read.
  Every failed read is unresolved and caches nothing, so no stale or borrowed supply is used.
- Baseline before activation (56, since 09-28): 94 band decisions (history 41, event 48,
  DexScreener 5); the GET, when it ran, was p50 216 ms, p90 283 ms (n=19).
- A J7 shadow decision on a listed chain also makes the supply read. It is a cached read, and the
  lane's decision on the same coin (first 49 of 50 times) warms or hits the same cache.
