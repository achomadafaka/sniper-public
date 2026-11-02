# E14: funded momentum on our own data (DexScreener out of the funded classifier)

Status: spec for Gamma. Author: Lambda, 2026-10-08. Order: Alpha, relaying Gamma's finding on spec
4f8b3f32c ("DexScreener is still in the FUNDED entry path through capture_v2"). This change gets its own
id in the entry-gates spec (`2026-10-08-memecoin-evm-entry-gates-onchain-design.md`). It follows the same
pattern as E11: shadow first, then a switch per chain.

## 1. What reads DexScreener today

`classify_funded_entry` (`memecoin_market_state.py:506`) calls `_momentum` (`:345`). `_momentum` reads
`memecoin_quote_observations_v2` (quality `ok`/`probe_ok`) over `MEMECOIN_FUNDED_TAPE_WINDOW_S` = 900 s
before the decision. It then appends `stats["price"]`, which is the market-data GET's price, as the current
point.

- `capture_v2_sweep` (`memecoin_capture_v2.py:299`) writes those rows from the DexScreener batch, every
  30 s per cohort coin.
- 4663 writes `probe_ok` rows through the GeckoTerminal blind pass.

The phase uses three thresholds: at least 3 observations, a span of at least 30 s, and the
`POST_PEAK_RUNUP_PCT`=20 and `GIVEBACK_PCT`=30 percentages.

The momentum facts reach money in three places:

| Reader | Effect | Measured 2026-10-07..08 (2,473 classified attempts) |
|---|---|---|
| `confirm_post_peak` (`:206`), Solana `solana_swap_executor.py:1440`, EVM `evm_swap_executor.py:990/1576` | `post_peak` NOMINATES. A fresh exact quote at least 30% under the momentum `peak_price` REFUSES (`post_peak_confirmed`) | 40 nominations (Solana 39, 4663 1); 1 refusal (Solana) |
| `memecoin_rh_fast_lane.py:1857` | a nominated 4663 attempt leaves the fast lane for the normal path | at most 1 (the one 4663 nomination) |
| `_regime` (`:407`): `runup_pct >= 20` makes `hot_narrow` | SIZING. `hot_narrow` scales the clip by `MEMECOIN_FUNDED_REGIME_RISK_SCALE`=0.75 | 193 attempts were `hot_narrow` by runup alone (consensus < 2): Solana 176, 56 11, 4663 3, 8453 3. Without the runup, 187 of them read `normal` (clip 1.0) and 6 read `thin` (0.75). Of the 187, 7 filled (Solana 6, 56 1) |

The rest is descriptive and gates nothing: `lifecycle:revival` (3 attempts) and `data_quality`.

Phase by chain over the same 2,473 attempts. "Unknown" means fewer than 3 observations or a span under 30 s;
most of these have no capture row before the signal.

| chain | flat | climbing | extended | post_peak | unknown |
|---|---|---|---|---|---|
| solana | 514 | 141 | 33 | 39 | 1,281 |
| 4663 | 47 | 4 | 0 | 1 | 134 |
| 56 | 29 | 8 | 3 | 0 | 104 |
| 8453 | 9 | 1 | 0 | 0 | 15 |
| 1 | 3 | 0 | 0 | 0 | 105 |
| 5042 | 0 | 0 | 0 | 0 | 2 |

## 2. Our own pre-decision price sources, per chain (checked 2026-10-08)

Momentum needs a price path for the 900 s BEFORE the decision, on coins we do not hold.

| chain | own source | pre-decision? |
|---|---|---|
| 4663, 8453 | `data/memecoin_onchain_volume.db` `pool_ticks`. It is chain-wide over the covered protocols: V3 and V4 swaps carry `sqrt_price_x96` (V4 as kind `v3_swap` with 66-char pool ids), while Pons V2 and 8453 V2 rows carry amounts only. Retention is about 1 h (oldest row at 22:30Z when read at 23:35Z) | yes, for V3/V4 pools |
| 56 | `data/memecoin_bsc_v2_live.db` `swaps`: amounts only. `syncs` keeps only the latest reserves per pool, and V3 `sqrtPrice` is kept only for HELD pools (`v3_marks`). Retention is 900 blocks, which measured 0.45 s/block = about 405 s | no: no post-swap spot history, and the window is shorter than 900 s |
| 1, 5042 | `BscVolumePoller` (`CHAIN1`, `ARC`), which reads held positions' pools only | no |
| solana | `memecoin_solana_swap_marks` binds OPEN positions only (`bind_open_positions`). `data/memecoin_sol_tape.db` and `data/memecoin_ticks.db` hold position pools only | no |

So Alpha's "Solana swap marks/stream" does not exist before the decision. Solana momentum on our own data
needs a new stream (decision S below).

## 3. Design

### 3.1 The own series (EVM, tape chains)

`own_momentum(conn, chain, mint, read_at)` lives in `memecoin_onchain_volume.py`. It reuses that module's
pool lookup, protocol-lag and silence checks, feed age and hash verification (`_pool_protocols`,
`covered_protocols`, `_worst_protocol_lag`, `_silent_protocols`, `_newest_block_ts`, the verified join
used by `_swaps`).

- **Pool.** Use the coin's covered SPOT pool (V3/V4, `sqrt_price_x96` present) with the most verified swaps
  in the window. If none has a swap in the window, use the spot pool with the latest swap still in the tape.
  The series uses one pool and never mixes pools.
- **Grid.** Read the pool's spot at `read_at - 30k` for k = 0..30, the same cadence as capture_v2. The spot
  at a grid point is the post-state `sqrtPriceX96` of the last verified swap at or before it, carried
  forward. Only swaps move a V3/V4 spot, so a quiet covered pool is a measured constant (flat), not unknown.
  Grid points before the pool's first known swap are skipped.
  - Sampling the grid rather than every swap is deliberate. Per-swap spot includes intra-block spikes
    (a sandwich, or a buy undone in the next log), so its max would inflate `peak_price`. A higher peak
    makes `exact_giveback` larger, and that means false `post_peak_confirmed` refusals.
- **Units.**
  - Coin price in quote units = `(sqrt/2^96)^2 x 10^(dec0-dec1)`, oriented by `pool_meta.quote_side`.
  - USD = quote units x the tape's own quote price (`_default_price`) at `read_at`, applied to every point.
  - Ratios (runup, giveback) are therefore pure coin-versus-quote moves. `peak_price` is in USD per token,
    the unit `confirm_post_peak` compares with the exact quote's `quote_price`.
- **Phase.** Same thresholds and same code path as `_momentum`: at least 3 points, a span of at least 30 s,
  then post_peak / extended / climbing / flat. The provider's `stats["price"]` is never appended.
- **Unknown** (reason recorded):
  - `no_known_pool`, `no_spot_pool` (only V2/Pons pools), `protocol_lagging` / `protocol_silent`,
    `feed_stale`, `hash_unverified`, `quote_not_priceable`, `too_few_points`, `read_failed`,
    `read_not_ready` (see 3.2);
  - `tape_gap`: an open `tick_gaps` row overlapping the window. One indexed read on
    `idx_tick_gaps_open`. A dropped span can hide the peak, so we never call it flat.

### 3.2 Where it runs (no added fill latency)

The read starts on the EVM decision path at the same point as `_start_evm_supply_read`
(`memecoin_real_runner.py:11825`), BESIDE the market-data GET. It runs on its own pool of 2 workers with
its own `get_readonly_connection` per read. The worker stamps `ready_at` when the read finishes.

- **Shadow (E14 off): zero-wait peek** (Gamma, required).
  - Classification takes the result only if it is already done; otherwise it records `read_not_ready`
    and never waits.
  - It records `wait_would_ms = max(0, ready_at - classify_at)`. When the read finishes after
    classification, the worker attaches its result and `wait_would_ms` to the attempt's trace
    (`momentum_own_late` substage). Nothing changes on the frozen classification.
  - Some lanes classify without the GET, such as the 4663 RH fast lane (nomination exit at
    `memecoin_rh_fast_lane.py:1857`). On those lanes the shadow costs nothing.
- **Switch (E14 on):** classification joins with the cap `MEMECOIN_FUNDED_MOMENTUM_OWN_WAIT_MS`
  (default 50). An unfinished read is `read_not_ready` (unknown).

Measured on the live tape, 2026-10-08 ~23:40Z, with a fresh read-only connection per call:
- 73 mints attempted on 4663/8453 since 10-07, x3 (n=219): p50 3.78 ms, p90 4.74 ms, max 42.5 ms. The max
  is a coin with 6,941 known pools.
- 300 active 4663 mints: p50 3.99 ms, p90 4.86 ms. The tail is hub tokens with 35k to 150k pools; we never
  buy those.

The GET runs p90 246-276 ms (entry-gates spec section 2), so the read normally finishes inside it. A chain
switches only when the shadow's p90 `wait_would_ms` is at most 1 ms on EVERY lane that classifies there;
lanes that skip the GET are the ones to watch. After E13 drops the GET on a chain, the read is inline:
p50 3.8 ms, p90 4.7 ms. That latency gets named again at that step.

### 3.3 Shadow (switch off: today's behaviour)

`classify_funded_entry` takes the joined result and records it in `facts["momentum_own"]`:

- phase, points, span_s, pool, protocol, quote_token, quote_usd;
- own_first_price, own_peak_price, own_display_price (USD), own_runup_pct, own_giveback_pct;
- reason, read_at, read_ms, wait_would_ms (or `read_not_ready`).

It also records `facts["momentum_basis"] = "provider_mid"`. `momentum_phase` and every provider fact stay
today's.

The shadow lives on the attempt row (`classification_json`), so it joins directly to outcomes. A Solana
row carries no `momentum_own` (no source). Old rows have no `momentum_basis`, which means provider.

### 3.4 Switch `E14:<chain>` (in `MEMECOIN_DEXSCREENER_OFF`)

Add `E14` to `DEXSCREENER_CONSUMER_IDS`. With `E14:<chain>` on:

- `momentum_phase` comes from the own series. `memecoin_quote_observations_v2` and `stats["price"]` are not
  read for momentum.
- The provider-named facts (`first_price`, `peak_price`, `display_price`, `runup_pct`, `giveback_pct`,
  `momentum_observations`, `momentum_span_s`) are ABSENT. The own values stay in `momentum_own`, and
  `momentum_basis` is `"own_tape_spot"`. A reader that does not know the basis sees unknown, never another
  basis under the old name (Gamma's rule on spec 4f8b3f32c).
- `confirm_post_peak`, `_regime` (the runup of `hot_narrow`) and the `revival` lifecycle read the runup,
  giveback and peak through the basis. A row without a basis reads today's keys.
- Unknown keeps today's meaning: no nomination, no runup (so no `hot_narrow` by runup), and `data_quality`
  `partial` (or `critical_unknown` with an unknown regime).

Offline readers that pool by name must split on `momentum_basis`:
- `scripts/research/funded_winners_losers.py:41-45`;
- `scripts/research/never_up_pre_entry_dataset.py:65-70`.

capture_v2 is NOT touched by E14. It keeps writing for paper and research until inventory P3. This is also
why the paper-path spec 4f8b3f32c names capture_v2 out of its scope.

## 4. Rule 5

1. **Money path: yes.** Post-peak refusal (via `peak_price`), 4663 fast-lane eligibility, and clip SIZE
   (`hot_narrow` 0.75). Exits are untouched. Everything is behind `E14:<chain>`, and the default is today's
   behaviour.
2. **Speed.** The read runs beside the GET: measured p50 3.8 ms, p90 4.7 ms, max 42.5 ms (section 3.2).
   - The shadow never waits: a zero-wait peek, so 0 ms on the fill path.
   - Only the switched path joins, capped at 50 ms, and it is gated on the shadow's p90
     `wait_would_ms` <= 1 ms per lane. There is no new RPC, lock or retry. The pool has 2 workers; a queued read past the wait is
   `read_not_ready` (unknown).
3. **Concurrency.**
   - Each read opens its own read-only connection inside the worker.
   - The pool is created once. Create it under a lock, not with the lazy `if None` race that
     `_EVM_SUPPLY_POOL` has.
   - No module attribute, env var or shared connection is mutated. The switch is read at call time
     (`dexscreener_off`).
4. **Absent vs zero vs unknown.**
   - A covered spot pool with no swap in the window, behind a healthy tape, is a measured constant (flat).
   - No pool, no spot pool, a stale, lagging or silent feed, a gap, an unverified hash or an unpriced quote
     are unknown, never flat.
   - A provider-named key is absent on own rows, never 0.
5. **Dependency failures.**
   - Tape store missing or locked: `read_failed`, then unknown. The read uses `busy_timeout` 500, like
     `feed_status`, and stays bounded by the wait.
   - A swallowed exception must not read as flat. Every path returns a reason, and the phase is computed
     only on `reason == ok`.
   - A quote token price missing: `quote_not_priceable`.
6. **Units and identity.**
   - sqrtPrice to price uses decimals from `pool_meta`. The test pins a real stored 4663 V4 pool and a
     real 8453 V3 pool against an exact quote on record.
   - USD per token is the unit of `confirm_post_peak`.
   - Chain ids are network ids. Mint addresses are lowercase.
   - The tape's `block_ts` is TEXT. Compare with `unixepoch()`, as `_swaps` does. A bare float comparison
     against TEXT is always true.
   - The source swap may be missing at read time (4663 head lag p50 0.8 s, p90 2.0 s), so the current
     point can lag the source's own buy. The provider lags more.
7. **Sample.** Shadow rows are our own attempts. They show which coins we attempted, not the market.
   Compare phase agreement, nominations and runup >= 20 flips on the same attempts, both bases side by
   side. Do not compare outcome rates of rows that one basis admitted and the other refused.
8. **Rollback.**
   - Remove `E14:<chain>`; the next classification reads the provider again.
   - Shadow facts are additive keys.
   - Half-applied, each chain is independent. A reservation keeps the classification frozen in its
     contract, so a switch mid-flight changes only new attempts.

Not handled: Solana (decision S); 56 until step 2; V2/Pons-only coins on 4663/8453 (unknown); circulating
supply; anything in capture_v2.

## 5. Build order (each one branch, red tests first, Gamma)

1. **Own series + shadow on 4663 and 8453.**
   - `own_momentum` with its unknown reasons, the grid, and a carry-forward test.
   - The shadow's zero-wait test: with the shadow on and the read blocked, classification returns in
     under 1 ms and records `read_not_ready`. The late result lands on the trace with `wait_would_ms`.
   - The beside-GET start, `facts["momentum_own"]` and `momentum_basis`.
   - Unit test against a real stored pool.
   - A source-guard test that the provider's `stats["price"]` is never appended to the own series.
2. **DEFERRED (Alpha, 2026-10-09): 56 own spot history.**
   - Why deferred:
     - The BSC store has no chain-wide V2 Sync history. records_sync keeps held and hop pools only,
       and swaps keep amounts only.
     - It has no local coin decimals for coins we have not traded: pools.mint_decimals exists only for
       resolved held/source pools, and E1's decimals read is RPC beside the GET. Ratios (phase, runup)
       cancel decimals, but the USD peak that post_peak confirmation compares needs them.
     - The value is small: 11 runup hot_narrow and 0 post_peak nominations on 56 over 10-07..08
       (144 attempts).
   - 56 keeps the provider momentum, like Solana. Alpha puts the remaining DexScreener money-path
     uses (momentum on Solana and 56, E7 on 56) in front of the operator as one list.
   - The plan below is kept for when it is picked up. The BSC store keeps a per-swap post-state price, then 56 joins the shadow.
   - V2: the reserves of the Sync emitted with the Swap.
   - V3 / PancakeSwap V3: the swap log's `sqrtPriceX96`, which `decode_evm_log` already reads.
   - Retention must cover at least 2,400 blocks (about 1,080 s at 0.45 s).
   - Observer-only store change. Measure store size before and after (27 MB today at 900 blocks).
3. **Switch `E14:<chain>`.**
   - Facts move under the basis, and `confirm_post_peak`, `_regime` and the lifecycle read by basis.
   - The two research scripts split on `momentum_basis`.
   - Switch per chain after at least 1 day of shadow. Gamma reads:
     - a phase agreement table;
     - nominations under both bases;
     - runup >= 20 flips (the sizing effect);
     - p90 `wait_would_ms` per lane;
     - giveback-at-nomination under BOTH bases against the SAME exact quote, i.e. the exact giveback
       `1 - exact/peak` with each basis's peak (Gamma's note).
   - Why the last item: a post-swap spot runs above trade prices on buy-heavy coins (Gamma's 10-01..08
     read: post-trade at display x1.25 at the median). So the own peak is systematically higher and the
     exact giveback larger, which means more `post_peak_confirmed` for the same coin. The 30%
     `GIVEBACK_PCT` may need a refit on the spot basis before the switch, as E11 went from 1.5 to 1.2.
     The refit is an operator decision.
   - That read needs no executor change. The attempt row's `quote_price` is the exact quote that
     `confirm_post_peak` is handed (`quote_fields(quote)["quote_price"]`), so both givebacks are computed
     offline from `quote_price`, the provider `peak_price` and `momentum_own.own_peak_price`.
     - Caveat: a requote after the confirmation, such as a later rung, can change `quote_price`. Rows whose
       trace shows a requote after `post_peak_confirmation` are read separately.
4. **Chains 1 and 5042.** No pre-decision source, so with `E14` on the phase is always unknown. Measured
   effect over 10-07..08:
   - chain 1: 3 `flat` + 1 unknown-with-points become unknown; 0 nominations and 0 runup `hot_narrow`, so
     only `data_quality` changes;
   - 5042: 2 attempts, already unknown.
   These can switch with step 3 without a shadow, since the shadow would record only `no_known_pool`.

## 6. Operator decision S: Solana (the largest consumer)

Solana has no own pre-decision price path. The options:

- **(a) Keep the provider momentum on Solana** until a source exists. Today's behaviour. Recommended now.
- **(b) Build a Solana cohort stream.** From a coin's first feed observation, follow its pool for the cohort
  life (15 min) with swap and curve marks. The funded swap-mark cache binds only open positions.
  - The same corroboration rule applies: STATE marks on CLMM/DLMM are not spot.
  - It needs a WebSocket key and budget. The paid research Helius key is the candidate (operator answer
    2026-10-01).
  - It would get its own spec. Coverage would be the same subset the provider covers: coins seen at least
    30 s before the decision (727 of 2,008 attempts phased today).
- **(c) Switch Solana to unknown.** Measured effect over 10-07..08 (n=2,008):
  - 727 phased attempts become unknown;
  - 172 runup `hot_narrow` attempts become `normal` (clip 1.0 instead of 0.75), 6 of which filled;
  - 39 nominations are lost, one of which refused.
  Not recommended without evidence on whether runup scaling helps.
