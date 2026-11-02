# Memecoin direct DEX execution on chain 4663 — replace 0x for real swaps

**Date:** 2026-07-16 · **Status:** ready for implementation — REAL-MONEY PATH, all gates mandatory
**Depends on:** `2026-07-16-memecoin-gt-pricing-design.md` (pricing must land first; this spec is
execution only). Read `docs/superpowers/specs/2026-07-15-memecoin-entry-speed-coverage-design.md`
before touching any executor file — its identity/attribution/trace contracts bind this work.

## Why

The 0x API key is dead (403 "You cannot consume this service", plan cap, operator declines
upgrade — `finding/memecoin-0x-key-outage-2026-07-16`). 0x built every real EVM swap transaction
(`zerox_quote` → signed `.transaction`), so with the key dead:
- The 2 open real 4663 positions (~$80 notional) cannot be exited by the runner (mirror/TP/stop
  all quote through 0x). They are priced again once the GT spec lands, but not executable.
- No new real EVM entries can fill. Chain 4663 is armed and was the top-volume signal chain in the
  2026-07-16 corpus study — losing it real-side is a standing capability loss.

Aggregators (1inch/Kyber/OpenOcean/Odos) do not cover chain 4663 (too new). The only free,
vendor-proof execution path is talking to the chain's DEX directly.

## Phase A — venue identification (deliverable: a short findings doc, NO code on the money path)

Identify the DEX protocol and its contract addresses. Evidence sources, in order:

1. **Our own real fill transactions** (ground truth — these swaps worked):
   - `0x71cec534a7fac4dba696c236df19b83edb546c88800014ba6968d95a30b41ee9` (mirror_exit, Index)
   - `0x29bf365875307d43d991b1892c12356934468028f60c782eca6d11eba0a02418` (mirror_exit, ROBINHOOD)
   Fetch `eth_getTransactionByHash` + `eth_getTransactionReceipt` via the chain RPC
   (`sniper.settings.memecoin.cfg.evm_rpc_for_chain("4663")` → `rpc.mainnet.chain.robinhood.com`).
   **Use the `_rpc` helper in `sniper/execution/evm_real_seams.py`** — the RPC's WAF 403s the
   default Python UA, and `evm_wallet._http` requires a pre-encoded body (raw dict throws
   TypeError; both were hit live on 2026-07-16).
   From the receipt logs: swap event topic (Uniswap V2 `0xd78ad95f…`, V3 `0xc42079f9…`, or V4
   `Swap` on a singleton PoolManager), pool/singleton addresses, and the `to` target 0x used.
2. **GeckoTerminal pool metadata** — `relationships.dex` names the venue directly (fastest
   ground truth; the pool-id byte-length is a secondary heuristic). **Verified live 2026-07-16:
   chain 4663 is MIXED-venue.** `/networks/robinhood/dexes` lists Uniswap V2/V3/V4, Pancake V2/V3,
   and native launchpads (RobinSwap, Hoodit, Clanker, …). Our own fills already span two Uniswap
   versions, so this is not hypothetical:
   - Index → pool `robinhood_0x00dd2df2f17d431cf3a0938f06c9cf9abc5e9643b6cc466ca3f71f3af246edf3`
     (`Index / WETH 1%`), a **32-byte Uniswap-v4 PoolId** (`uniswap-v4-robinhood`) — NOT a
     v2/v3 pair address.
   - CASHCAT → pool `robinhood_0xa70fc67c9f69da90b63a0e4c05d229954574e313` (`CASHCAT / WETH 1%`),
     a **20-byte Uniswap-v3 pair** (`uniswap-v3-robinhood`).
   Both quote in chain WETH (`0x0Bd7…AD73`). Consequence: Phase C needs BOTH the v3-pair and the
   v4/UR branches, chosen **per pool** from the venue GT/quoter reports — never a single assumed
   version for the whole chain. Treat as corroboration, not ground truth: confirm against the
   decoded fill logs.
3. Chain explorer / official Robinhood chain docs for router/quoter/Permit2 deployments.

Phase A output (commit as `docs/research/2026-07-16-robinhood-4663-dex-venue.md`): the venue
protocol **and every version in play** (v3 and v4 are both confirmed present — enumerate, do not
assume one), the v3 router + v4 PoolManager/Universal-Router/quoter addresses, fee tiers observed,
WETH handling (chain WETH = `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73`, `CHAIN_QUOTE["4663"]`),
whether approvals go through Permit2 (v4 Universal Router does) or plain ERC-20 approve, how to map
a coin → its live pool/version (GT `relationships.dex` + pool id byte-length), and one worked
example decoded from our own fill **for each version we must support**. **STOP and surface if the
venue is not a Uniswap-family AMM** — a bonding-curve launchpad or custom singleton changes this
spec's shape and needs a new design. (Mixed Uniswap v3+v4 is in-family and does NOT stop the spec.)

## Phase B — executable quotes (read-only)

New module `sniper/execution/evm_dex_quote.py`:
- `dex_sell_quote(chain, mint, amount_atomic, *, http=None) -> {"buy_amount_atomic": int, "route": …} | None`
  and the buy-side equivalent — via `eth_call` to the venue quoter (v4 `Quoter`/`V4Quoter`
  `quoteExactInputSingle`, or v2 `getAmountsOut` if Phase A says v2). Free, no key, no rate cap
  beyond the RPC's.
- Fail-closed contract identical to `zerox_quote`: any error/no-route/zero-out → None.
- This replaces 0x's `buyAmount` role. GT (pricing spec) remains the marks source; the DEX quoter
  is the EXECUTION source — do not cross-wire them (marks want an index price; execution wants the
  exact venue's amountOut).

## Phase C — swap execution

Extend the existing executors rather than forking them: `sniper/execution/evm_swap_executor.py`
(entries) and `sniper/execution/evm_exit_executor.py` (exits) gain a venue-pluggable
quote+tx-builder seam: `venue="zerox"` (today's path, preserved verbatim behind its key check) and
`venue="direct"` (new). Selection: config-driven per chain —
`MEMECOIN_EVM_EXEC_VENUE` map, default `{"4663": "direct"}`, everything else `zerox`.

Direct-venue tx construction. Because 4663 is mixed (Phase A), `venue="direct"` is NOT a single
sub-path: resolve the coin's live pool + Uniswap version **per attempt** (from the Phase B quoter /
GT `dex`) and dispatch to the matching builder. A coin whose pool version cannot be resolved →
fail-closed (no trade), never a blind send against a guessed router.
- v2/v3-style (CASHCAT is v3): ERC-20 exact-amount `approve(router, sell_amount)` (reuse the
  existing exact-approve pattern in `evm_real_seams.py` ~line 254) → the router's exact-in swap
  (`swapExactTokensForTokens` v2 / `exactInputSingle` v3) with `amountOutMin` from the Phase B quote
  × the EXISTING slippage settings (no new slippage policy).
- v4/Universal Router (Index is v4): Permit2 approve flow + UR `execute` with an exact-in
  single-pool path; native-vs-WETH handling per Phase A findings.
- Reuse the signing/gas seams as-is, including the gasPrice buffer lesson (first 4663 fill,
  2026-07-13, deploy `3c7a6e5f`) and checksummed addresses (`to_checksum` choke point in
  `evm_real_seams.py` — non-checksummed router was a live footgun).
- **Entry-speed contract (binding):** stamp `attempt_id`/`origin_event_id` traces in
  `memecoin_entry_attempts` exactly like the 0x path; preserve `(network_id, mint)` identity and
  entry/exit SYMMETRY (the 08fc5965 lesson: identity resolved at entry must be resolvable at
  close); record `source_drift_pct`/`execution_slippage_pct` from the direct quote the same way.

## Gates — non-negotiable, in order

1. Full test suite green including new unit tests with injected RPC seams (quote parse, approve
   flow, amountOutMin math, venue selection, fail-closed on every error branch).
2. **Dry canary:** Phase B quote for a live liquid 4663 coin matches GT price within sanity bounds
   (log both; no tx).
3. **$5 canary round trip** on ONE liquid 4663 coin (buy → confirm → sell → confirm), manually
   operator-triggered via the existing `/real/buy` manual path, BEFORE any automated arming.
   Record both fills in the standard tables; verify PnL accounting and traces end-to-end.
4. Only then enable `direct` for automated 4663 exits (the 2 open positions), and only after that
   for entries. Deploy via `scripts/deploy_memecoin_runtime.sh <sha>` with standard gates (backup,
   zero pending reservations, reconciliation, positive terminal-exit config gate).
5. Kill switch, daily-loss breaker, and per-chain risk caps apply unchanged.

## Out of scope

Solana (Jupiter path untouched). Execution on 0x-covered chains (Base/BSC/Ethereum keep the zerox
venue; do NOT route them direct — DexScreener/0x semantics there are not the constraint). Pricing
(previous spec). Sizing/policy/arming changes. Removing the zerox code path.

## What could go wrong → mitigation

- **Wrong venue assumption** → Phase A is mandatory and STOPs on a non-Uniswap-family answer.
- **v4 hook pools with custom behavior** → quote through the official quoter only; refuse pools
  whose quote reverts; fail-closed = no trade, never a blind send.
- **Thin pools → brutal price impact** → `amountOutMin` from quoter × existing slippage cap;
  existing mcap-tiered impact caps unchanged.
- **Approval griefing / unlimited approvals** → exact-amount approvals only (existing pattern).
- **RPC WAF flakiness** → browser-UA `_rpc` helper + fail-closed; the watchdog's existing alerting
  covers a dead RPC.

## Verification

Per gate above; final acceptance = the 2 open real 4663 positions can be exited by the runner
through the direct venue (or have been exited during the canary), `memecoin_entry_attempts` shows
direct-venue attempts with complete stage timings, and a log-grep shows zero 0x calls on the 4663
money path.

<!-- schema:external=memecoin_entry_attempts -->
<!-- schema:external=memecoin_positions -->
<!-- schema:external=memecoin_fills -->
