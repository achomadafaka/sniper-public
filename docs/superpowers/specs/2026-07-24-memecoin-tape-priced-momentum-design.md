# Tape-priced momentum_a on 4663 — design

**Date:** 2026-07-24 · **Status:** design, approved for planning · **Scope:** PAPER only, zero funded-behaviour change
**Companion:** `docs/superpowers/plans/2026-07-24-memecoin-quant-engine-brief.md` (P1/P4 context)

---

## 1. Why

**The edge is front-loaded and we hold far past it.** Feed decay (n≈12k marks, since 07-10): up-fraction
64% @60s → 59% @5m → 53% @15m → **46% @1h → 39% @6h → 31% @24h**, avg 24h return −9%, hit20@1h 12%.
Measured average real hold is **605 minutes**. We hold ~40× past the horizon where the signal has edge.

**`momentum_a` is the controlled proof of the opposite shape** — 15-min fast-ride, no win cap,
momentum-loss cut, never mirrors sells. Measured 2026-07-24 over closed paper positions:

| population | n | PnL | WR | avg/trade |
|---|---|---|---|---|
| all wallets | 1446 | +$3,387 | 31.3% | +$2.34 |
| armed only | 495 | +$1,891 | 30.5% | +$3.82 |
| **solana + armed** | **329** | **+$1,840** | **41.0%** | **+$5.59** |
| solana non-armed | 692 | +$1,381 | 41.2% | +$2.00 |

On the population we would actually trade (armed wallets), the average trade is **2.8× the non-armed
average while the win rate is identical** (41.0% vs 41.2%) — the entire difference is winner *size*,
not hit rate. That is the fat-right-tail shape the decay curve predicts.

**But momentum_a is dead on 4663 for a fixable reason.** Chain split of the same closed set:

| chain | n | PnL | WR |
|---|---|---|---|
| solana | 1021 | +$3,221.80 | 41.1% |
| evm-unknown | 247 | +$126.60 | 6.5% |
| **4663** | **138** | **$0.00** | **0.0%** |

Per `sniper/services/memecoin_copy_trader.py:363-366`, its 15-min hold expires before the first
DexScreener quote ever lands on 4663 → 100% `a_timecap_noquote` at $0, so the arm is now hard-blocked
off Solana (`skipped_slow_chain`, `:370`). This is a **price-latency** failure, not a strategy failure —
and we now hold a 2.8M-row on-chain tick tape for that exact chain, which is a far faster price source
than DexScreener.

## 2. What we are building

Give `momentum_a` a tape-derived USD price on 4663 and un-block it there, so the arm can run at all on
a chain where it has only ever failed for want of a quote.

**Explicitly NOT in scope:** any funded-behaviour change, any clip-size or exposure change (brief §6 —
live expectancy is negative), Solana (no tape exists; see §7), and any change to `momentum_decision`'s
logic. We change the *price fed in*, never the rules.

## 3. Decisions taken (and the ones rejected)

| Decision | Chosen | Rejected alternative |
|---|---|---|
| Arm shape | Un-block `momentum_a` on 4663 (one arm) | A separate tape-priced arm. Operator chose the merged form; §6 keeps the Solana control attributable instead. |
| Price bridge | **Full USD conversion** from the tape | Scale-invariant ratio (no decimals, no USD). Operator chose absolute prices for DexScreener comparability; §5 exists because this is the riskier bridge. |
| Chain scope | 4663 only | All EVM. The tape only covers 4663 (8453 is watched-pool-filtered); `evm-unknown`/56/1 have no tape and stay blocked. |

## 4. Components

### 4.1 Token decimals in `pool_meta` (new, fail-closed)
`pool_meta` (`sniper/analysis/tick_features.py:33-41`) gains `token0_decimals` / `token1_decimals`,
resolved via the EXISTING `erc20_decimals(token, rpc_url)` helper
(`sniper/execution/evm_wallet.py:130`, already fail-closes to `None`) inside the existing bounded
`resolve_pool_meta` pass (`sniper/analysis/tick_features.py:63`). No new RPC plumbing.
Immutable per token, cached forever.

**Fail-closed:** decimals unresolved ⇒ **no tape price is produced at all**. Never a guessed scale.

<!-- schema:external=pool_meta -->
```sql
ALTER TABLE pool_meta ADD COLUMN token0_decimals INTEGER;   -- NULL = unresolved -> no tape price
ALTER TABLE pool_meta ADD COLUMN token1_decimals INTEGER;
```

### 4.2 `tape_price_usd()` — one conversion, units in the name
Consumes the per-block size-weighted VWAP already built (`exec_price_path_per_block`,
`sniper/analysis/exit_features.py`), so it inherits the impact-free block price rather than a single
print:

```
human = (|quote_atomic| / 10**quote_decimals) / (|base_atomic| / 10**base_decimals)   # quote per base
usd   = human * quote_usd_price(chain)                                                # USD per base
```

`quote_usd_price` already exists (`sniper/execution/evm_wallet.py`, used at
`scripts/tick_feature_job.py:77`). The raw atomic ratio keeps its
`price_kind="block_vwap_exec_atomic"` provenance marker so it can never be mistaken downstream for the
USD number. Returns `None` on any missing input — decimals, quote USD, or an empty block path.

### 4.3 Chain-aware un-block
`skipped_slow_chain` (`memecoin_copy_trader.py:370`) becomes: Solana allowed (as today), **4663
allowed**, every other network still refused. The guard's reason string stays for the refused chains so
existing attribution is unchanged.

### 4.3b Position → pool resolution (CORRECTED 2026-07-24, after the first gate run)

**The original design was wrong.** It joined a position to its pool via
`memecoin_positions.quote_route_target`. That column is the **router/settler we route through, not the
AMM pool** — the live values are addresses like `0x0000000000001ff368…` (an 0x settler; `zerox` is one
of the mark sources). Pool events (`Swap`/`Sync`/`Mint`/`Burn`) are emitted by *pool* contracts, so all
3 distinct 4663 route targets have **zero ticks** and always will. The first cross-check run returned
`0/30 aligned pairs`, and every one of 99,567 marks was skipped. The unit tests could not catch this:
they seed `pool_ticks` and `pool_meta` under the same key the code reads, so the fixture agreed with
the bug.

**Corrected rule — a position's pool is resolved from its `mint`:**

```sql
SELECT pool, quote_side FROM pool_meta
 WHERE chain_id = '4663'
   AND quote_side IS NOT NULL
   AND token0_decimals IS NOT NULL AND token1_decimals IS NOT NULL
   AND ((quote_side = 0 AND LOWER(token1) = :mint)     -- mint must be the BASE token
     OR (quote_side = 1 AND LOWER(token0) = :mint))
```

The mint must match the **base** side. Matching `token0 OR token1` is also wrong: WETH (the 4663
quote asset, `0x0bd7d308f8…`) then matches **168** pools. The base-side predicate excludes that by
construction.

**Ambiguity ⇒ fail closed.** If the query returns more than one pool, no tape price is produced.
Measured 2026-07-24 over all 279 distinct 4663 position mints:

| usable pools for the mint | mints |
|---|---|
| 0 (not yet in `pool_meta`) | 243 |
| **exactly 1** | **35** |
| 2 | 1 |

**35 of 36 resolvable mints (97%) are unambiguous**, so failing closed costs ~3% of coverage. The one
ambiguous mint splits 65% / 35% by tick count — genuinely two markets, no dominant pool. Pricing a
position off the wrong market is severe and asymmetric against that 3%, and registering a dominance
threshold on n=1 would be fitting a rule to a single observation. Revisit only with evidence.

**Coverage is the real limit, not ambiguity.** 243 of 279 mints have no usable pool yet, because
`pool_meta` is populated from the 200 busiest pools per run and the job that fills it had **never
executed** (its cron was missing a `cd`; fixed 2026-07-24). With the cron working it resolves ~50
pools per 5-minute run. Coverage must be **re-measured** before any claim about how many positions the
tape can price.

### 4.4 Price selection in the exit sweep
On 4663, `momentum_a` resolves its `mid` in this order:
1. tape USD price (§4.2) — if fresh within a configured bound,
2. DexScreener quote (today's behaviour),
3. neither ⇒ today's `a_timecap_noquote` path, unchanged.

Every position records which source priced it via the existing `memecoin_positions.price_attribution`
column, so each close is attributable to its price source.

`momentum_decision(entry_price, peak_price, no_new_high_sweeps, price, held_s, rollover_sweeps,
trail_pct, stop_pct, max_hold_s)` is called **unchanged**. Only `price` differs.

### 4.5 New thresholds
All new knobs go in `sniper/settings/memecoin.py` (never service files): tape-price freshness bound,
and the §5 tolerance/minimum-n. Defaults chosen from mechanics, not fitted.

## 5. Validation gate — the tape price must be PROVEN before it drives an exit

The chosen USD bridge rebuilds the atomic→human→USD chain that already produced one P0 unit-mixing
defect in this project. It is therefore gated on measurement, not review.

A cross-check harness compares **tape-USD against DexScreener-USD** for 4663 coins where both exist at
approximately the same timestamp, and reports the distribution of relative error.

- **Pre-registered before running:** the tolerance band, the minimum n, and the timestamp-alignment
  window. Written down before the first comparison is computed.
- **Pass ⇒** the tape price may drive `momentum_a` on 4663.
- **Fail ⇒ the conversion is wrong and does not ship.** No live arm on a price we cannot reconcile.

The harness also records the **executable gap** — tape/DexScreener mid versus a whole-bag exit preview —
wherever such a preview exists, so the paper→real translation cost is *measured* rather than assumed
(see §6).

### 5.1 Pre-registration (written 2026-07-24, before any comparison was computed)
- **Alignment window:** a tape block and a DexScreener mark are comparable when their timestamps are
  within **120 s**.
- **Tolerance:** PASS requires **median relative error ≤ 5%** AND **p90 ≤ 15%**.
- **Minimum n:** **30** aligned pairs. Below that the harness reports progress only and gives no verdict.
- **Branch on PASS:** the tape price may drive `momentum_a` on 4663 (plan Task 5).
- **Branch on FAIL:** the conversion is wrong. Task 5 does **NOT** ship. Investigate decimals
  resolution and `quote_side` orientation first — those are the two ways this goes wrong.
- No re-slicing, no tolerance change after seeing the numbers.

## 6. Evidence hygiene

Paper results have already failed to translate to real once in this project
(`finding-memecoin-translation-gates-2026-07-16`: real lost on the *same coins* control won; selection
was fine at entered +$2,349 vs skipped −$918; the villains were the 3rd-buyer trigger, the TP+60 cap and
re-entries). Real is −$285 over 88 closes with a **best win ever of $40.13**, while paper `momentum_a`
has uncapped winners. That gap is the thing to distrust.

Therefore:
- **Every `momentum_a` read that informs funding is armed-only**, split by `network_id` **and**
  `price_attribution`. The Solana +$3,221 control never merges into one number with 4663 results.
- The known translation villains are stated in any read: paper opens at signal price (real pays chase +
  confirm latency); paper models slippage on `mid × qty` (real sells the whole bag into an AMM, worst
  precisely when a coin is dying).
- `executable_price` stays `None` (exit-brain §3.4). The genuine fix for the third villain is the
  fast-lane whole-bag route price, which is an **operator STOP condition** (paid provider) and is not
  proposed here.

## 7. Solana (deferred, and why)

`momentum_a`'s edge is Solana; the exit brain's signals are EVM-only. They do not intersect today:
`MEMECOIN_SOLANA_RPC_URL` is empty in both the repo `.env` and the pinned runtime, no arrival lane is
running, and `sol_tx` ticks = **0**. There is no Solana tape at all.

Unlocking it (slot→time backfill, `getTransaction` vault-delta amounts, then a new dated prereg +
`feature_version` bump per prereg §5) is a separate spec. This 4663 work de-risks it: the same
tape→price machinery gets proven on a chain we can already see, before the larger gated build.

## 8. Testing

- `tape_price_usd` against known decimals/quote-USD → known USD (both token orders, quote_side 0 and 1).
- Fail-closed: missing decimals, missing quote USD, empty block path ⇒ `None`, never a number.
- Decimals resolution is bounded, cached, and never re-fetches a resolved token.
- Chain gating: 4663 admitted; solana admitted; 8453/56/1/evm-unknown still `skipped_slow_chain`.
- Price-selection precedence, including the unchanged `a_timecap_noquote` fallback.
- `price_attribution` is recorded for every priced position.
- The §5 harness itself, on controlled inputs.
- No funded-path test changes; the exit brain stays propose-only.

## 9. Constraints inherited (must not be violated)

Monotonic risk-reduction only; no gate read below n≥30; funded changes ship via policy versions and the
deploy script, never hand-edits to `.runtime/memecoin/current`; no clip-size or exposure increase while
live expectancy is negative; new thresholds live in `sniper/settings/memecoin.py`.
