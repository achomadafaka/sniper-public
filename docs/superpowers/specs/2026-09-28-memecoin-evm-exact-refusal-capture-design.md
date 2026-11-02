# Exact-route EVM refusal capture

Status: rule-5 spec, no code. Order via Alpha (2026-09-28, after the 56 second cause): "At refusal,
quote the clip through our own V3/V4 quoter on a NON-custody read host, then quote the sell of those
tokens every ~25 s to 810 s, into the same refusal-captures store and shape the Solana exact captures
use. Off the fill path, bounded rate per host, research hosts only (4663 publicnode eth_call; 56
defibit; 8453 needs a sanctioned host: say which)." Author: Lambda. Extends
`docs/superpowers/specs/2026-08-26-memecoin-refused-mint-forward-capture-design.md`, whose §"Out of
scope for v1" lists EVM chains and whose 2026-09-16 correction says "EVM observations remain display
marks".

<!-- schema:external=memecoin_refusal_captures -->
<!-- schema:external=memecoin_evm_v4_pool_keys -->

## What exists today (measured 2026-09-28 22:06–22:15Z)

- **The EVM lane is display-only.** `memecoin_refusal_capture.display_observe` writes one DexScreener
  `priceUsd` per due capture, `quote_price_basis='display'`, no side, no clip. Chains
  `MEMECOIN_CAPTURE_EVM_NETWORKS` = 4663, 56, 5042 (8453 is not in it: its refusals get no capture at
  all), cadence 25 s, horizon 810 s, max 6 concurrent. Last 7 days: 4663 242 captures / 6,021 rows,
  56 95 / 2,223, 5042 13 / 268.
- **Refusal volume.** Capturable EVM refusals (families in `CAPTURED_FAMILIES`), 7 days to 22:06Z:
  4663 258 attempts over 109 mints, 56 100 over 37, 8453 46 over 17. Largest families:
  `source_buy_floor` and `mcap_over_ceiling` on every chain, then `chase_first_seen` (4663 16) and
  `m5_turnover_under_floor` (4663 14). About 37, 14 and 7 captures a day.
- **Where funded EVM fills actually route** (`quote_route_target`, filled attempts, 14 days): 4663
  n=114: V4 UniversalRouter 78, Relay 19, KyberSwap 9, Pons V2 router 4, other 4. 56 n=41:
  PancakeSwap V2 Router02 35, PancakeSwap V3 3, flap curve 2, mixed hop 1. 8453 n=2: V2 1, V3 1.
- **V4 PoolKeys are already stored.** `memecoin_evm_v4_pool_keys` (ledger, PoolManager-verified,
  indexed on each currency) holds a key for 106 of the 109 refused 4663 mints, and 105 of those 106
  have a pool whose other side is native ETH (median 3 pools per mint, max 25). 8453: 15 of 17. 56 has
  no V4 deployment. So the capture never needs `eth_getLogs`, which the research hosts refuse or cap.
- **The reaper is not scheduled.** `crontab -l` has no `memecoin_refusal_capture_loop.py --reap` line,
  and the store holds zero `complete`, `gapped` or `truncated_worker` stamps in 7 days on any chain
  (only `handed_off_to_position` and `not_captured_capacity`). Every consumer that filters
  `outcome='complete'` (the design's §6 use 1) reads nothing today, Solana included. Separate fix
  (a cron line is persistent configuration: operator or Alpha); named here because this capture is
  useless to those consumers until it lands.

### Live probe (22:13–22:15Z, one refused mint per chain, read-only)

The funded quoters were called with an injected `call_fn` bound to a research host, paced at 4 calls/s,
browser User-Agent: V4Quoter `quoteExactInputSingle` on every stored ETH-side key of the mint,
`dex_v3_quote` (configured deployments, fee tiers and connectors) and `dex_v2_exit_quote` (configured
Router02 and connectors), clip ~$20 of the chain quote token.

| chain | host | calls | status | p50 / p90 ms | best venue |
|---|---|---|---|---|---|
| 4663 | robinhood-rpc.publicnode.com | 30 (V4 5, V3 22, V2 3) | 30 x 200 | 36 / 44 | V4, hook pool, fee 0 |
| 4663 | robinhood.drpc.org | 30 | 30 x 200 | 168 / 200 | V4, same pool |
| 56 | bsc-dataseed2.defibit.io | 28 (V3 24, V2 4) | 28 x 200 | 123 / 135 | V2 |
| 56 | bsc-dataseed1.ninicoin.io | 28 | 28 x 200 | 125 / 136 | V2 |
| 8453 | base.drpc.org | 13 (V4 3, V3 8, V2 2) | 13 x 200 | 67 / 84 | V4 |

JSON-RPC error 3 ("execution reverted") on V3/V2 candidates is the quoter saying "no pool on this
tier or path", an answer, not a failure. The 4663 hook-pool buy output fell 12% between the two 4663
probes 65 s apart: the instrument sees the coin move. Neither publicnode tape logged a stall or reset in
the probe window (the same grep finds 182 and 96 historical `head stream stalled` lines, so it can
fire). Host screen (eth_chainId, eth_blockNumber, one eth_call, 1 s apart): base.llamarpc.com 525 on
all three; base.meowrpc.com and bsc.meowrpc.com do not resolve; 1rpc.io/base and blastapi answer at
130–370 ms.

## Hosts: say which

Every host below was checked against `.env` and the code for any other role.

| chain | Alpha named | other roles on that host | recommend |
|---|---|---|---|
| 4663 | robinhood-rpc.publicnode.com | 4663 volume tape WS (m5 gate input), 4663 watcher WS (detection), J7 post-trade receipt read (`MEMECOIN_ONCHAIN_EVM_TX_READ_URL_4663`; a 403/429 benches it 60 s and the chase reference falls back to avg_fill); publicnode throttles per IP across chains (finding 2026-09-22: bursts stalled the 4663 AND 8453 tapes at the same second) | robinhood.drpc.org (no role anywhere; slower, 168 ms). publicnode is acceptable only at the rate below |
| 56 | defibit | `bsc-dataseed1.defibit.io` is now `MEMECOIN_ONCHAIN_EVM_RPC_URL_56`, the 56 watcher's HTTP read host (tx and receipt decode for detection) | bsc-dataseed1.ninicoin.io (different operator, no role). If defibit is kept, use dataseed2, never dataseed1; whether defibit rate-limits its nodes per IP jointly is unknown |
| 8453 | none | mainnet.base.org and mainnet-preconf.base.org are custody and free-submission endpoints; the Alchemy host is the funded read pool; base-rpc.publicnode.com is the funded V4 key-discovery archive (`MEMECOIN_EVM_ARCHIVE_RPC_URLS_8453`), the 8453 tape and the watcher | **base.drpc.org** (no role anywhere; 67 ms) |

Recommendation: robinhood.drpc.org, bsc-dataseed1.ninicoin.io, base.drpc.org. None of them shares a
provider with a custody, submission, detection, gate-input or funded-discovery role, so a research
throttle there cannot reach money. Alpha's publicnode/defibit choice works under the rate bound
below; the table names what it shares.

## Design

A third capture lane, `evm_exact`, inside the existing capture worker
(`scripts/memecoin_refusal_capture_loop.py`), beside the Solana lane and the EVM display lane. Its
own module, `sniper/services/memecoin_evm_exact_capture.py`, imported only by the worker.

1. **Enrolment.** `poll_refusals` is unchanged; its network list gains
   `MEMECOIN_CAPTURE_EVM_EXACT_NETWORKS` (csv of canonical ids, default empty). A refusal on an exact
   chain starts an exact capture in addition to the display capture where the chain has one. Capture
   id `<network>:<mint>:<origin>#exact`, so the reaper, `observation_counts` and every reader that
   groups by `capture_id` keep the two series apart; `terminal_row` still reads the chain from the
   prefix. Own capacity `MEMECOIN_CAPTURE_EVM_EXACT_MAX_CONCURRENT` (default 6), counted apart from the
   display lane. Stale, size-unknown and capacity refusals use today's terminal outcomes. The
   `handed_off` check applies unchanged.
2. **Clip.** `quote_input_usd` for sized families (the reservation notional, as Solana), else
   `MEMECOIN_CAPTURE_CLIP_USD`. Converted to the chain quote token (`CHAIN_QUOTE`: WETH on 4663 and
   8453, WBNB on 56, 18 decimals) with `native_usd_price(chain)`, the funded sizing source (Binance,
   then FMP), cached 60 s per chain in the lane object: at most two reads a minute from this IP.
3. **Route scan (observation 0, the buy).** Candidates, each through the lane's `call_fn`:
   - V4: every stored key for the mint whose other currency is native ETH or WETH, at most 8, ordered
     by pool id; one `quoteExactInputSingle` each (the encoder from `evm_v4_quote`, no change there).
     Key read through the worker's existing read-only ledger handle.
   - V3: `dex_v3_quote(chain, quote_token, mint, amount, taker, call_fn=...)`: the funded deployments,
     tiers and connectors, and its own keep-the-largest-amountOut rule.
   - V2: `dex_v2_exit_quote(..., call_fn=..., tax_buffer_bps=0)`: the funded Router02 and connectors
     (the funded buy path also zeroes the input tax buffer).

   The largest amountOut wins; ties resolve V4, V3, V2. The winning route is FIXED for the series and
   written to `quote_route_target` (`v4:<pool_id>`, `v3:<protocol>:<tokens>:<fees>`,
   `v2:<router>:<path>`). A lane-owned route cache keyed `(chain, mint)` serves concurrent refusals of
   one coin: 60 s for a hit, 120 s for a miss, so a curve-only coin is rescanned at most seven times a
   window.
4. **Sells.** Every `MEMECOIN_CAPTURE_EVM_CADENCE_S` (25 s) to the horizon (810 s, 32 observations),
   one quote of the fixed bag on the fixed route, reversed: V4 the same key with `zeroForOne` flipped;
   V3 the reversed tokens and fees asked of each configured quoter of that protocol (4663 runs two
   uniswap_v3 factories, so up to two calls); V2 `getAmountsOut` on the reversed path. One call on the
   dominant venues.
5. **Row shape = the Solana exact shape.** Same columns, same meaning where the meaning can match:
   `quote_side` buy then sell; `quote_price` USD per token (buy: clip USD / tokens; sell: quote-token
   out x `native_usd` / tokens, funded's executable-exit convention); `token_qty_atomic` the fixed
   bag; `token_decimals` from one `decimals()` call per mint, cached; `quote_output_atomic` the
   quoter's raw amountOut; `quote_min_output_atomic` NULL (no executable floor is built);
   `quote_price_basis='exact_route'`; `provider` `evm_v4_quoter` / `evm_v3_quoter` / `evm_v2_router`;
   `endpoint_index` 0; `http_status`, `latency_ms` from the call; `cadence_s`, `horizon_s`,
   `expected_obs`, `age_s` from the refusal as today.
   **One difference, made explicit:** on these rows a sell's `quote_output_atomic` is quote-token atoms
   (WETH/WBNB, 18 decimals), not USDC atoms (6). One additive nullable column `quote_asset_usd REAL`
   carries the `native_usd` each row used; it is NULL on Solana rows, where the quote asset is USDC and
   the column does not apply. The store docstring states the unit per network.
6. **Transport.** One POST per call, no rotation, no retry, 3 s timeout, browser User-Agent (the
   publicnode 403 trap), to `MEMECOIN_CAPTURE_EVM_READ_URL_<chain>` only. Never `evm_rpc_for_chain`,
   never an RPC pool, never the funded `_make_call_fn` (it chooses from the custody pool).
7. **Rate and yield, per host.** A lane-owned token bucket per host: `MEMECOIN_CAPTURE_EVM_EXACT_MAX_RPS`
   (default 4, the probed pace), burst 8. A scan that runs out of tokens resumes on the next tick; an
   observation that finds no token is a gap, never a burst. HTTP 429, 403, 5xx, a transport error or
   timeout, or JSON-RPC -32005/-32003 hold the host `MEMECOIN_CAPTURE_EVM_EXACT_YIELD_S`
   (default 120 s) and write no row: the held observations are gaps, the reaper sees them. Error 3 is
   an answer. The heartbeat gains per-host counts: calls, ok, reverted, limited, held.
8. **Startup guard.** For each exact chain the read URL must be set, must answer `eth_chainId` with
   that chain's id, and its host must differ from
   every host in `MEMECOIN_EVM_RPC_URL_<c>`, `MEMECOIN_EVM_RPC_POOL_URLS_<c>`,
   `MEMECOIN_EVM_ARCHIVE_RPC_URLS_<c>`, `MEMECOIN_LIFI_PROOF_RPC_URL_<c>`,
   `MEMECOIN_TICKSTREAM_RPC_URL_<c>`, the free-endpoint registry for the chain and the pool that
   `evm_rpc_for_chain(c)` resolves. A violation refuses that chain loudly and keeps the rest of the
   worker running. Hosts that are shared but allowed (watcher HTTP/WS, J7 receipt read, tape WS) are
   named in the startup log line.

Not in this build: any gate, cap or clip change; retiring the display lane (Gamma's first-seen read
and the refusal-card fallback use it); Relay/Kyber/curve venues; the reaper cron line.

## Edge cases (CLAUDE.md rule 5)

1. **Money path: no direct path, one indirect channel.** The worker is its own process, reads the
   ledger read-only and writes only `data/memecoin_refusal_captures.db`. The indirect channel is a
   shared provider budget: on publicnode the capture shares an IP budget with the 4663 tape (m5 gate),
   the watcher sockets (detection) and the J7 receipt read (chase reference); on defibit dataseed1 with
   the 56 watcher. The recommended hosts remove that channel. Knob default off; Alpha activates.
2. **Speed on the fill path.** No code on the entry path. The funded process does not import the new
   module (a fresh-interpreter test pins it). Added fill-path latency: none, so there is no p50/p90 to
   measure. The capture's own calls measured p50/p90 36/44 ms (publicnode 4663), 168/200 (drpc 4663),
   123/135 (defibit 56), 125/136 (ninicoin 56), 67/84 (drpc 8453). Load: sustained at most
   6 captures x 1 call / 25 s = 0.24 calls/s per lane; a scan is at most 30 calls at 4/s; about 2,300
   calls a day on 4663, 840 on 56, 315 on 8453.
3. **Concurrency.** The worker loop is single-threaded. Route cache, buckets, yields and the price
   cache live on one lane object built in `main()`; no module global is added or mutated. No funded
   module imports `memecoin_refusal_capture` (`evm_swap_executor.py:220` names it in a comment only).
4. **Absent vs zero vs unknown.** Error 3 or amountOut 0 on every candidate = `no_route` (answered:
   no pool on our venues). A limit, transport error or timeout = `provider_error`, host held, no row.
   No stored V4 key and no V3/V2 pool = `no_route`, and the row cannot tell "curve coin" from "pool we
   never learned": named limit. `native_usd` unknown: the buy cannot be sized and waits for the next
   tick; a sell keeps its atoms with `quote_price` NULL and quality `price_unknown` (a new value in the
   quality vocabulary). Unknown decimals = `provider_error`.
5. **Dependencies.** Research host: limit, 403, timeout and a wrong answer. A wrong answer is guarded
   by the chain identity check at startup (eth_chainId must equal the chain) and by the quoter address
   being the funded one. Binance/FMP for `native_usd`: two reads a minute; a failure is item 4. Ledger
   read: the existing read-only handle; a failure is `no_route` for V4 only, logged with the mint.
6. **Units and identity.** Canonical chain ids. `quote_price` is USD per token on every exact row, so
   the Solana and EVM series share a price space; `quote_output_atomic` on sells is quote-token atoms on
   EVM and USDC atoms on Solana (item 5 of the design; the reader fixes below). V4 native ETH and WETH
   share 18 decimals and are both counted ETH-side. The fixed route is our own best single venue at
   refusal time: on 4663, 25% of funded fills went through Relay or KyberSwap, which can beat it, so a
   series is a lower bound on what an aggregator would have paid out. Fee-on-transfer tax is not in a
   quoter's output: named limit.
7. **The sample.** A row exists only when one of our gates refused a copied buy of an armed wallet:
   the numbers describe our gates on our roster, as intended. One coin refused for N wallets gives N
   captures; a reader clusters by `(network_id, mint)`. Censoring: capacity, stale, size-unknown,
   handed-off, held hosts, and no-route coins (Aerodrome-only on 8453, flap and launchpad curves).
   Independent quotes do not replay our own buy's price impact on the sells (the design's standing
   limit).
8. **Rollback.** Empty `MEMECOIN_CAPTURE_EVM_EXACT_NETWORKS`; the watchdog restarts the worker (or the
   next pinned deploy does). Rows already written stay, immutable, and are identified by the
   `#exact` capture id and the `evm_*` provider. Half-applied: a new worker with an old web release
   shows the exact series on EVM refusal cards (the card's existing `exact_route` branch, which is the
   intent); the new column is additive and every reader names its columns.

### Readers the new rows would break (fixed in the same build)

- `scripts/research/gate_instrument_audit.py:246-256` divides every sell `quote_output_atomic` by 1e6
  and reads all networks: EVM rows would read 10^12 too large. Add `network_id='solana'` (its output
  already says "solana").
- `scripts/research/first_seen_refusal_outcomes.py:70` (Gamma, f7aa269f) reads by origin and gate
  with no basis filter; an EVM origin would interleave display and exact rows. Pin
  `quote_price_basis='display'` so the committed read stays byte-identical.
- `scripts/research/turnover_band_floor_calibration.py:214` reads `exact_route` on every network and
  gains the EVM `m5_turnover_under_floor` captures (4663: 14 in 7 days). Units match (USD/token);
  the population change is declared, not fixed.
- `sniper/routes/api_memecoin.py:1245`: EVM refusal cards switch from display to route prices once an
  exact row exists. Intended.
- `scripts/research/impact_shadow_read.py:65` divides by 1e6 by origin; `memecoin_impact_shadow` holds
  Solana rows only (5), so it cannot reach an EVM origin today. Unchanged.

## Check left behind

`tests/test_memecoin_evm_exact_capture.py`, on real `eth_call` answers captured in the probe above
(V4Quoter on the 4663 hook pool, PancakeSwap V2 `getAmountsOut` on 56, V3 reverts), each test proven
red first:

- a buy then two sells produce rows with exactly the Solana row's keys plus `quote_asset_usd`, USD
  per token in `quote_price`, quote-token atoms in `quote_output_atomic`, the `#exact` capture id and
  an `evm_*` provider;
- every call goes to the configured research host and nowhere else (call recorder);
- a read URL whose host is any custody, pool, archive, submission or discovery host refuses the chain
  (proven by pointing 8453 at mainnet.base.org and 4663 at the Alchemy pool host);
- 429 and -32005 hold the host and write no row; error 3 is `no_route`;
- a 30-call scan never exceeds the bucket (at most 8 calls in any 1 s window);
- with the knob empty the worker starts no exact capture and the display and Solana rows are
  byte-identical to today;
- `gate_instrument_audit` and `first_seen_refusal_outcomes` ignore exact EVM rows;
- a fresh interpreter importing `evm_swap_executor` and `memecoin_real_runner` does not import the new
  module.

## Decisions for Alpha

1. Hosts: the recommended isolated set, or publicnode 4663 / defibit dataseed2 56, with base.drpc.org
   on 8453 either way.
2. `native_usd` from `native_usd_price` (funded's sizing source, two reads a minute), or an on-chain
   stable quote on the research host (three more code paths: ETH/USDG V4 on 4663, WBNB/USDT V2 on 56,
   WETH/USDC V3 on 8453). Recommend the first.
3. The reaper cron line, so `outcome` stamps exist for any capture.

## As built (2026-09-28, Alpha's GO on eeea7a0a1)

Alpha's decisions: hosts as recommended (robinhood.drpc.org, bsc-dataseed1.ninicoin.io,
base.drpc.org); the guard refuses any shared host; the reaper cron line goes to Alpha; the two
readers are fixed in this build; knob default empty, Alpha activates 4663, 56 and 8453 at merge.
Where the build departs from the design above, and why:

1. **The guard is stricter than specced.** It refuses a read host that appears in ANY other
   environment value, any resolved EVM read pool, or the free-submission registry, derived at
   startup rather than listed. There is no "shared but allowed" class. Verified on the production
   environment: the three recommended hosts pass their `eth_chainId` checks; publicnode 4663, defibit
   dataseed1 and publicnode 8453 are each refused without a call.
2. **The native USD rate is a plain Binance GET on the lane's own 60 s cache**, not
   `native_usd_price`. That function holds no module-global cooldown, breaker or counter, but when
   Binance fails it falls back to FMP on `FMP_API_KEY`, the funded sizing fallback's quota. A Binance
   failure holds the price read for the yield window; the buy then writes `price_unknown`.
3. **V4 pools follow the funded rule** (`evm_v4_quote.v4_quote_amount_out`): a native pool, a WETH
   token pool only where funded enables one, and never a pool failing `_fee_is_sane` (fee over 10%).
   Without that screen the largest 8453 buy in the first recording came from an 87% fee pool and its
   round trip read -98.7%.
4. **56 pair tokens come from the flap Portal** (`getTokenV6` word 9, one eth_call on the read host).
   34 of the 37 refused 56 mints in the week were flap-style coins paired with a non-connector token,
   and the funded pair lookup (`pair_quote_tokens`) reads DexScreener through the funded seam. The
   22:13Z probe above ran the funded V2/V3 functions with that lookup still enabled, so it made one
   DexScreener read per probe process through the funded seam from this host (two in total). The
   lane never does.
5. **The rate bound** is a token bucket holding one second of rate: at most 2 x 4 = 8 calls in any
   one-second window, idle or not.
6. **`first_seen_refusal_outcomes.py` excludes `#exact` capture ids** instead of pinning the display
   basis: byte-identical on every existing row, Solana included (its origin list is not EVM-only).
7. **The reaper line** was proven by hand on a backup copy of the store: 2,294 of 2,638 captures
   stamped (complete 1,898, truncated_worker 382, gapped 14), 0.25 s; a second run stamped 0.

8. **8453 should read blastapi, not drpc.** Live coverage run (the lane itself, the 20 most recent
   refused mints per chain, 22:36–22:40Z): 4663 13 of 20 priced (V4 11, V3 2), 56 18 of 20 (V2 17,
   V3 1), 8453 5 of 17 (V4 3, V3 1, V2 1). base.drpc.org answered HTTP 429 "You reached Public
   endpoint rate limit" (JSON-RPC code 15) after about 300 drpc calls in 75 s, most of them on
   robinhood.drpc.org: drpc limits its public endpoints per IP across chains, so 4663 and 8453 on
   drpc share one budget. The hold worked as built (the remaining 8453 captures were gaps, no calls).
   base-mainnet.public.blastapi.io then served 191 calls at 4/s with no limit and has no other role.
   Production load is far below either bound (about 0.02 calls/s averaged, scans of 15 to 30 calls),
   but separate providers remove the coupling. The read URL is configuration; no code depends on it.
   Round trips on priced coins (independent buy and sell quotes, $20): 4663 median about -6%, 56
   about -2%, with single outliers at -97% and -99% on pools that pass the fee screen but hold
   almost nothing. Unpriced coins: pools only in USDG or equity tokens (4663, multi-hop is out of
   scope), fee-trap-only coins, curve coins, and Aerodrome-only coins (8453).

9. **Review fixes (Beta's review of 735d8917, Alpha's order).**
   - C6: the lane runs on ITS OWN THREAD (`ExactWorker`): its own capture dict, read-only ledger
     handle and store handle; the loop only `submit`s refusals. A time budget per pump could not
     finish a 15 to 30 call scan. Measured 22:49–22:53Z with the real `cap.pump` over 6 Solana
     captures (fake Jupiter, 150 ms per quote) and the real worker scanning live mints on the three
     research hosts into the same store: pump wall p50/p90/max 902/902/902 ms with the lane off and
     902/902/902 ms with it on, 45 passes each; the worker wrote 36 exact rows (24 priced), 0 errors.
     The heartbeat carries the pump's wall (`pump_ms`) and the thread's own `last_pass`, `pass_ms`,
     `errors` and `alive`, so a dead thread is visible without the watchdog.
   - C1: the guard compares REGISTRABLE DOMAINS (last two labels; an IP is its own), because
     publicnode, Alchemy and drpc limit per IP or account across their chain hostnames.
   - C5: any HTTP 403/429 and any error text naming a limit (limit, too many, exceed, quota, rate)
     holds the host. A revert is still an answer when its reason says "limit" (a hook's max-wallet
     revert is the pool speaking), so the revert test runs first.
   - `impact_shadow_read.py` reads Solana rows only (sells and the buy clip).
   - A V4 amount at or above 2^128 is skipped, as funded does.

## Limits (stated, not fixed)

- **V4 multi-hop: built 2026-09-30, store only (see the section below).** Before that it was not
  scanned: 23 of 99 funded 4663 fills bound to `uniswap_v4_multihop` in the post-trade census, and
  a coin whose depth is quoted in USDG or an equity token wrote `no_route`.
- **Discovery is our stored keys only.** Funded discovers V4 pools through GeckoTerminal and
  DexScreener; the capture reads `memecoin_evm_v4_pool_keys` and the configured V3/V2 venues.
  Rows are biased toward coins whose keys the ledger already holds, and a busy ledger read
  (`_stored_pool_key` gives up after 250 ms) silently narrows the pool set for that scan.
- **Binance is shared.** The native USD read goes to api.binance.com, which the EVM watcher's price
  cache also reads. Two reads a minute at most, so "no budget shared with a money role" holds for
  every RPC host and not literally for this one price read.
- Relay, Kyber, flap and launchpad curves, Aerodrome and fee-on-transfer tax are out of scope, as
  above.

Check: `tests/test_memecoin_evm_exact_capture.py`, 26 tests on frames the lane itself recorded on
the three research hosts at 22:33Z. Twenty-two mutations of the build (guard by env, registry
and domain; fee screen; hold on status, code and words; revert precedence; bucket; price hold;
store column; lane capacity; pump dispatch; the loop starting or pumping exact captures itself;
the worker's hand-off and stop; all three readers; the funded import; the knob default) each fail
it.

## V4 multihop, store only (2026-09-30, Alpha's order)

**Why.** Gamma: since 09-29, 41 refusals on 4663 got an exact row and 15 are `no_route` (37%).
Her block-tag grader prices 51 of 77 with single-hop venues and 71 of 77 (92%) when the funded
multihop search runs on the key store alone, up to 3 legs, with the provider pool lists disabled.

**What.**
- When the single-hop scan (V4, V3, V2) prices nothing, `ExactLane._multihop` runs the funded
  search `evm_v4_multihop.multihop_quote(..., store_only=True, limits=MULTIHOP_LIMITS)`.
- `store_only=True` searches our PoolKey store alone and never asks a provider, whatever
  `MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST` says. Production keeps that flag off and the funded rung
  is unchanged: both new parameters default to the funded behaviour.
- Chains: `MEMECOIN_CAPTURE_EVM_MULTIHOP_CHAINS`, default `4663`. Empty turns it off.
- The winning path is FIXED like every other route: later observations quote it in reverse with
  one quoter call. Row: provider `evm_v4_multihop_quoter`, route `v4_multihop:<pool>><pool>`.
- A multihop search runs only after a single-hop miss, so a coin with a direct pool costs nothing
  more than before.

**No display feed, no funded host (operator order 86d053ce).**
- Every request of the search goes through `ExactLane._rpc_http`, a seam with the lane's own
  bucket, hold and counters. It refuses any URL but the lane's read host before a byte is sent, so
  a helper that falls back to an archive host, a pool member or a provider cannot reach it.
- If the read URL expands into an RPC pool, the search does not run on that chain (logged once).
- The store-only search makes no GeckoTerminal or DexScreener read (test on the recorded 4663
  fixture, with the funded flag on and off).
- Own database (`data/memecoin_refusal_captures.db`). The ledger is read read-only for keys.

**Added calls per captured row.**
- First observation (the buy), only after a single-hop miss: bounded by `MULTIHOP_LIMITS`, 40
  requests and 45 s. On the recorded 4663 fixture, cold, a store-only search made 12-24 path
  quotes, 3-5 batched liquidity reads and 1 block read: **16 to 30 requests** (7 coins).
- Every later observation: **1** quoter call, as for a single-hop route.
- These are replay counts. NOT measured on the live research host: two dry captures on
  2026-09-30 at ~13:30Z got HTTP 429 (code 15, "public endpoint rate limit") from
  robinhood.drpc.org, the first after 10 calls in 2.0 s, the second on its first call three
  minutes later. The running lane showed 0 calls on that host, so it was not the lane's load.

**Limits, stated.**
- A cold store search can pick a worse path than the provider search (the reason the funded flag
  is off: -49.3% against -2.6% on coin 0x8920d999 when the liquidity-read allowance ran out). A
  multihop row is what OUR store can route, not always what funded would have traded.
  - Measured by Gamma (2026-09-30) on 11 chase refusals whose funded winner was v4_multihop:
    the store-only search reproduced the funded quote within 2% on 8, and was 1.7%, 5.8% and
    7.0% worse on the other 3.
- The route label names one pool per leg, in path order (`v4_multihop:<pool>><pool>[><pool>]`),
  each id derived from that leg's own key.
- A search of 16-30 requests at 4 requests/s may itself trip the public endpoint's limit. Then
  the host is held for 120 s and the observation is a gap, never a `no_route` row.
- There is no launchpad-curve venue in this lane. A curve coin (0xd1ced051, 2026-09-30) stays
  `no_route`. Building one is a separate decision.
- Not proved by this build: that the lane prices a direct pool since its ~12:16Z relaunch. Its
  heartbeat shows 0 calls on the 4663 host since then.

### Amendment 2026-09-30: an identity read that did not answer is pending, not refused

The startup guard (item 8) refused a chain for the life of the process when its host did not
answer `eth_chainId`. base-mainnet.public.blastapi.io answered 429 to that read at two relaunches
(2026-09-29 17:57 and 2026-09-30 15:01:07Z), so 8453 had no exact lane until a hand relaunch.

- The right chain id admits the chain. ANOTHER chain id refuses it for good, at startup and at a
  retry. No chain id (limit, timeout, error reply, null result) is unknown: the chain is PENDING.
- The lane's own thread retries a pending host 30 s, 60 s and 120 s after each failure, then
  every 300 s. A pending chain makes no quote call and no price read; its captures are started
  and their observations are gaps, never a row on another chain and never `no_route`.
- The startup line names `pending=`; the heartbeat's `evm_exact.pending` and `.refused` name the
  chains; each failed retry logs a warning with the host and the status.
- `usable_read_urls` (the one-shot research scripts) keeps the strict answer: a proven host only.
- Not handled: a host that never answers is asked every 5 minutes for the life of the process,
  and a pending chain's captures hold slots of the shared exact budget.
