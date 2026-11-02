# Memecoin EVM Real-Money Leg — Runner Wiring (multi-venue)

**Status:** design → implement (2026-07-09)
**Goal:** make the built-but-inert EVM execution stack actually trade by wiring it into the poll loop
as a **second venue alongside Solana**, and — critically — make the exit sweep, mirror-on-sell, and
reconcile **venue-aware** so a Solana position is never handed to the EVM executor and vice versa.

## Problem

The EVM stack (custody, risk, entry/exit executors, seams, token-safety, leg wrapper) is built and
unit-tested, but nothing calls it at runtime: `memecoin_real_runner.real_entry_hook` passes
`evm_entry_fn=None`, so the network router drops every `0x` buy as `evm_unarmed`. Worse, the three
Solana-only maintenance paths would MIShandle EVM rows if EVM positions ever existed:

- `real_exit_sweep` / `real_mirror_on_sell` call the **Solana** `exit_fn` on *every* `execution_mode='real'`
  position → an `0x` mint sent to the Jupiter/solders executor.
- `real_reconcile` reads *every* real position's balance via the **Solana** `token_fn` (Solana RPC) →
  EVM positions read as 0 → false drift alert.

The review named this exactly: "wiring it later without also touching real_reconcile / real_exit_sweep /
real_mirror_on_sell (all Solana-only today) produces false drift alerts and 0x mints handed to the
Solana executor."

## Design

### Venue map

A single dict `contexts: {network_id -> context}`. Key `"solana"` → the Solana context
(`build_real_context`); each allowlisted + RPC-configured EVM chain under its fomo chain id
(e.g. `"8453"`) → an EVM context (`build_real_evm_context`). Built once at runner start:

- disarmed / no creds for a venue → that key is absent → that venue is a no-op.
- Solana-only deploy → `{"solana": rc}` → byte-identical to today's behavior.
- empty dict → real leg fully off (paper unchanged).

Every real position already carries `network_id` (`'solana'` for Solana fills via `execute_entry`;
the chain id for EVM fills via `execute_entry_evm`). **Routing is by `pos["network_id"]`.**

`context_for(contexts, network_id) -> context | None` — `contexts.get(network_id or 'solana')`.
None ⇒ that venue isn't configured ⇒ the caller **skips + logs** (never hands the position to the
wrong leg). This is the load-bearing correctness guard.

### Entry (multi-venue)

`real_entry_hook(conn, ev, contexts, ...)` runs the staleness gate, then calls `real_entry` (the
network router) with:
- the Solana context's `execute_fn` / `token_safety_fn` / balances for the `solana` path (safe
  rejectors if no Solana context), and
- `evm_entry_fn = _make_evm_entry_fn(contexts)` for `0x` mints.

`_make_evm_entry_fn(contexts)` returns a closure matching `real_entry`'s call shape; it extracts the
chain from the route, looks up the EVM context, and calls `evm_real_entry` with **that chain's**
balances / token-safety / executor. No context for the chain → `evm_unarmed` drop (counted).

Entry alerts (`alert_entry`/`alert_failed_tx`) fire **once**, centralized in `real_entry_hook` on the
final result (works for both venues via `sig`/`tx_hash`). The per-leg self-alert in `evm_real_entry`
is REMOVED to avoid a double page.

### Exit sweep / mirror / reconcile (venue-aware)

- `real_exit_sweep(conn, contexts, ...)`: iterate all real alpha_b positions; for each, route by
  `network_id` to its context's `exit_fn`; skip + log if no context. Decision (`alpha_b_real_exit_reason`)
  and price feed (DexScreener, multi-chain) are venue-agnostic.
- `real_mirror_on_sell(conn, ev, contexts, ...)`: route the matched position to its venue's `exit_fn`.
- `real_reconcile(conn, contexts, ...)`: pass `reconcile_real_positions` a position-aware `balance_fn`
  that routes to the venue's `token_fn`; a missing context raises → reconcile treats as drift (fail-closed,
  surfaced) rather than a silent 0.

### Reservation recovery

`confirm_for(network_id, sig)` routes to `context_for(contexts, network_id)["confirm_fn"]`; no context
→ `"unknown"` (leave reserved, fail-safe). Requires `confirm_fn` exposed on **both** contexts (Solana
already; add to `build_real_evm_context`).

### Gas alert

`check_gas_alert` stays Solana-specific (SOL reserve formula). EVM per-chain native-gas paging is a
minor follow-up; EVM entries still fail-closed on the S1-EVM native reserve without a proactive page.
Documented, not silently dropped.

### Loop

`build_all_contexts()` replaces `build_real_context()`; every `rc` reference in the loop becomes
`contexts` (guard on truthiness of the dict). One coordinated restart still activates it.

## Edge cases

| Case | Behavior |
|---|---|
| Disarmed / no creds | `contexts` empty → real leg off, paper unchanged |
| Solana-only configured | `{"solana": rc}` → identical to today |
| EVM buy, chain not configured | `evm_unarmed` drop, counted (never sent to Solana executor) |
| Real position, network_id has no context | exit sweep / mirror skip + log; reconcile surfaces as drift |
| `0x` mint anywhere | can only ever reach an EVM context; a base58 mint only a Solana context |

## Test plan

1. Entry dispatch: an `0x` Base buy → the Base context's `evm_real_entry` → executor; a base58 buy →
   the Solana executor. Neither crosses.
2. Exit sweep: a Solana position → Solana `exit_fn` only; a Base position → Base `exit_fn` only —
   assert each context's exit_fn is called with only its own mint (the review's bug, guarded).
3. Mirror-on-sell: routes the held position to its venue's `exit_fn`.
4. Reconcile: routes the balance read by network; an EVM position drifts against its EVM balance, not
   the Solana one.
5. Recovery: `confirm_for` routes by network.
6. Back-compat: `{"solana": rc}` reproduces the existing single-venue tests.
7. `build_all_contexts` / `build_real_evm_contexts`: disarmed → empty; armed + Base creds → Base ctx.

## Out of scope (explicitly)

Per-EVM-chain gas paging; BSC (held out until per-chain USDC decimals + a BSC smoke); the arm itself
(operator + Base smoke gate). Solana remains the first-armed leg; EVM arms separately after its own
`memecoin_evm_smoke_test.py --confirm`.
