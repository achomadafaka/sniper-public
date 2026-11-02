# BSC mixed V3 → V2 hop as a funded venue (design)

Status: DRAFT for Alpha's GO. Nothing here is built yet. Author: Beta, 2026-09-25.

## 2026-10-03 implementation amendment

The draft below is historical: the funded mixed rung now exists and is enabled by the operator.
This amendment changes its cost measurement after AIKOL attempt `84d50cdd` and supersedes entry
steps 2–3 and 6 where they disagree with current code.

- Forward quotes retain a 4% configurable bridge-transfer allowance and one final route-wide
  slippage floor. The second router leg spends its whole intermediate balance (`amountIn=0`),
  verified on chain on 2026-09-25; it does not leave deliberately unspent bridge dust.
- The already-required whole-balance user-owned exit simulation provides actual quote-token
  proceeds, including reverse transfer taxes. Prefix a simulation-only base-inventory sweep in
  that same call; it never enters signed entry or held-position exit calldata. Require canonical
  `[empty,uint256,uint256]` output. Old unisolated, zero, unavailable or malformed outputs refuse.
  Do not substitute two
  reverse quotes with additional assumed tax deductions. No extra RPC or retry is introduced.
- The exit proof uses the exact forward V3 pools in reverse order. Compare at most two proven
  bridge candidates by actual atomic sell-back. Divide by the original atomic quote-token input;
  preserve the existing round-trip budget, signed floors, approval and accounting gates.
- The canonical WBNB/USDT bridge default uses verified fee 100 (0.01%) pool
  `0x172fcd41e0913e95784454622d1c3724f546f849`, replacing fee 10000 inside the existing quote batch.
  Coin-side tiers and batch size remain unchanged. Explicit fee overrides win; custom connectors
  retain their prior fee default. This does not add automatic connector-fee failover.

Evidence, regression checks, current-price probes and historical-state limits:
`docs/audits/2026-10-03-bsc-aikol-roundtrip.md`. Neither simulated current prices nor green tests
prove the old AIKOL trade would have passed or future source-block inclusion.

## Why

### 2026-10-10 exit valuation amendment

The executable exit preview values the whole bag using the existing
`MEMECOIN_EVM_EXIT_VALUE_TAX_PCT` allowance, then requotes the selected downstream V3 path at
that intermediate amount. The 15% maximum-tax cut belongs to the signed floors, not the preview
valuation. Retain the selected V2 valuation metadata and exact V3 path; expected output and its
evidence may change, but calldata, sell quantity, both floors and route selection remain intact.
Invalid identity or unavailable valuation retains the conservative cut. This adds one V2 and one
selected-path V3 read only to successful mixed exit previews, with no entry/send work. Setting the
existing value allowance at/above the maximum-tax cut restores the old valuation without reads.

Evidence: `docs/audits/2026-10-10-bsc-valuation-repair.md`. Actual future tax, pool movement and
spot-versus-whole-bag price impact remain separate; focused tests do not prove historical TP
timing or live quote latency.

Operator, 2026-09-25: "we should've filled". Funded BSC signal 螃蟹 `0x7bc023e0…7777` (DipWheeler,
$2.6K buy, ref `b849bb8a`) aborted with `route_over_budget`. The ladder priced every route it knows
and every one was a dust pool:

| rung | result |
|---|---|
| flap_curve | no hit (graduated) |
| v3 | round trip −86.3% |
| v2 | round trip −99.7% |
| kyber | round trip −41.9% |
| v4, v4_multihop | unavailable on 56 |
| mixed_v3_v2_probe | `would_quote` (measurement only, never selectable) |

The coin's real market is one PancakeSwap **V2** pair, 螃蟹/**oANTHROPIC**, $50.6K liquidity.
oANTHROPIC's only market is one PancakeSwap **V3** pool against USDT, $87.3K. GoPlus: buy tax 0,
sell tax 0.99%, not a honeypot. So the −41.9% is route cost through dust, not tax; the real path
USDT → V3 → oANTHROPIC → V2 → 螃蟹 crosses two healthy pools.

This is the class already measured on 2026-09-22 (brain
`finding/bsc-no-route-is-a-base-token-bridge-gap-not-v4-2026-09-22`): 8 of 13 chain-56 `no_route`
signals were V2 pairs in a non-connector base token, and a PCS V3 → V2 mixed hop fixes 3 of 13
within 10%. The atomic executor for exactly this route exists (`ff3f68fd`,
`sniper/execution/evm_mixed_hop_router.py`) and is imported by nothing, so the existing flag
`MEMECOIN_EVM_MIXED_HOP_EXECUTION_ENABLED` does nothing today.

## Scope

Chain 56 only (`MEMECOIN_EVM_MIXED_HOP_PROBE_CHAINS`, default "56"), behind the existing
`MEMECOIN_EVM_MIXED_HOP_EXECUTION_ENABLED` (default 0). Entry and exit. No new chain, no new
router, no new signing surface: the SmartRouter was verified on chain on 2026-09-22 and the V3 leg
reuses the calldata the standalone V3 path already funds.

Out of scope: a two-transaction exit (see "If the route disappears"), V4 → V2 mixes (fixed 1 of
13), and any other base-token bridge.

## Entry: a real ladder rung `mixed_v3_v2`

Runs inside the existing measured buy ladder, in parallel with the other rungs, and competes on
the same measured round trip. It does not jump the queue: the best in-budget round trip wins, as
for every rung since 2026-09-22.

1. **Base tokens.** `pair_quote_tokens(chain, coin)` (already cached 10 min). Drop connector bases;
   the single-venue rungs already cover them.
2. **Legs.** `dex_v3_quote(quote → base)` picks the V3 fee tier and yields `bridged`;
   `dex_v2_exit_quote(base → coin, tax_buffer_bps=0)` yields `coin_out`. Best `coin_out` across
   bases wins.
3. **Round trip.** Reverse on the same two pools at the WHOLE `coin_out`: V2 `coin → base`, then
   V3 `base → quote`. `round_trip_pct = back / amount_in − 1`, judged by
   `round_trip_within_budget` exactly like the other rungs (same marginal-retry class).
4. **Exit proof before any entry.** `plan_mixed_entry` refuses to return a plan unless
   `simulate_exit` succeeds at the whole would-be balance through the SmartRouter. The simulation
   needs the coin balance and allowance funded by `eth_call` state override; the rung passes the
   chain's token-safety `_fund_overrides` (today a closure inside `build_evm_token_safety`, so it
   is exposed as a callable, not re-implemented). A transport failure gives no plan: "we could not
   ask" is not "we may sell".
5. **Quote object** in the shape the executor already consumes: `buyAmount`, `sellAmount`,
   `transaction {to: SmartRouter, data: multicall, value: 0}`,
   `issues.allowance.spender = SmartRouter`, and
   `route {kind: "mixed_v3_v2", base_token, v3_fee, bridged_min, round_trip_pct}`. The route is
   persisted as today in `memecoin_positions.evm_route_json`, which is how the exit knows.
6. **Min-outs.** `bridged_min` and `coin_min` from each leg's quote minus the chain's slippage.
   Leg 2 spends `bridged_min`, not the router balance (the router's "whole balance" sentinel is
   unverified; see the executor docstring). Excess leg-1 output stays in the router as dust,
   bounded by the slippage band. Removing it needs that sentinel verified by simulation first.

## The new approval spender

`0x13f4EA83D0bd40E75C8222255bc855a974568Dd4` (PCS SmartRouter on 56), a constant in
`SMART_ROUTERS`. It was verified on chain 2026-09-22: its code, `deployer()` and `factory()`
match our funded PCS V3 router, and `WETH9()` matches V2 Router02's `WETH()`.

- The entry approves the quote token to it for the **exact** entry amount; the exit approves the
  coin to it for the **exact** sell amount. Never unlimited.
- Both go through the existing `_approve`, so they inherit its pipelining and fail-closed rules: a
  pipelined approval proceeds only on a positive acceptance.
- The rung can only ever name this constant; no provider response supplies the spender.

## Token safety

The chain's standard sell probe is single-venue, so it would call every mixed-route coin
unsellable. That is the same gap, one step earlier. For a coin whose chosen route is
`mixed_v3_v2`, the rung's whole-balance exit simulation IS the sellability evidence, recorded
under its own reason. The honeypot and tax checks that do not depend on the route stay as they are.

TO VERIFY DURING BUILD: whether chain 56 runs the sell probe before or after the quote on the
funded path, and whether it can take the proof from the chosen route. If it cannot, this spec
comes back for a decision before any code.

## Exit ladder

- A position whose `evm_route_json.kind` is `mixed_v3_v2` gets the mixed exit rung
  (`build_exit_calldata`, coin → base on V2, then base → quote on V3, one transaction), and that
  rung is SELECTED whenever it answers.
- Without that preference the priority ladder would take the first answer, which on this coin is
  the dust V3 pool. The existing dust check only rescues that when it looks implausible.
- Exits are never refused on cost, as today.

## If the mixed route disappears after we hold

The bridge pool or the coin's V2 pair drains:

1. The mixed exit rung returns None. The ladder falls through to the remaining rungs, and the
   existing sell dust check (bounded wait, largest output) picks the best of what answers.
   Kyber and Relay may still route it.
2. If nothing routes, the existing unsellable path applies unchanged: the exact-quote miss counter,
   then the stuck-bag and unsellable pages. There is no silent hold.
3. NOT in v1: a two-transaction exit (sell coin → base on V2, then sell the base token
   separately). It is recoverable, but it leaves a base-token bag the book does not track. It
   needs the other-holdings sweep to own that bag first, and it gets its own GO.

## Rule 5

1. **Money path:** yes, entry, exit and approvals. This spec needs its own GO.
2. **Fill speed:** the rung runs in parallel inside the existing ladder, and the ladder's
   grace/deadline rules are unchanged. Its own cost: two leg quotes, two reverse quotes and one
   `eth_call` simulation. That will be measured with a p50/p90 on 56 in shadow before arming (see
   Rollout); no estimate is claimed here.
3. **Concurrency:** no module state beyond the cached pair-token discovery that already exists.
   The approval nonce pipeline is the existing one.
4. **Absent vs zero:** no base token, no V3 bridge, no V2 leg, zero `coin_out`, or no override
   builder: each is a named miss, never a zero-cost route. `round_trip_unmeasured` refuses, as for
   every rung.
5. **Dependencies:**
   - A quote transport failure makes the rung blind (retryable, `provider_*`).
   - A simulation transport failure gives `exit_sim_unknown`, which refuses.
   - A reverted simulation gives `exit_sim_reverted:<reason>`, which refuses.
   - The SmartRouter is immutable, but its address is re-checked (`eth_getCode` length) once per
     process before first use.
6. **Units:** BSC stables are 18 decimals (brain finding 09-06). The quote-token decimals come
   from the chain config, never a constant. `v3_fee` is in hundredths of a basis point, as in
   `evm_dex_quote`.
7. **Sample:** the addressable set is non-connector-base V2 coins on 56, which was 8 of 13
   no-route signals in 8.8 days. Expect a few fills a week, not a volume change.
8. **Rollback:** flag off. A position already opened on the mixed route keeps its exit rung,
   because the exit reads `evm_route_json` and not the flag, so turning entry off never strands a
   held bag.

## Rollout

1. Build with the flag off. The rung runs in SHADOW on 56 (quote, round trip and exit simulation
   recorded on the rung ledger, never selectable) until n ≥ 10 signals, measuring its latency and
   the quoted round trip against the budget.
2. Arm on 56 only with Alpha's GO. At the first 5 mixed fills, read the realised round trip
   (entry fill vs exact exit quote) against the quoted one, and exit success, before calling it
   working.

## Tests (each fails on HEAD)

- The rung selects the mixed route for a 螃蟹-shaped fixture: dust v3/v2 and a healthy
  V3-bridge + V2-pair route.
- No plan without a successful whole-balance exit simulation, and no plan on a simulation
  transport failure.
- The spender is always the SmartRouter constant.
- The exit for a `mixed_v3_v2` position selects the mixed rung over a dust v3 answer. When the
  mixed rung returns None, the exit falls through to the dust check.
- The flag off means the rung is never configured, and an existing mixed position still exits
  through the mixed rung.
