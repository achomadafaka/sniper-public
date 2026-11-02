# Memecoin shared fast-submission backend (2026-07-25)

> **SUPERSEDED (paid-mode provider selection) by
> [`docs/superpowers/specs/2026-07-25-memecoin-free-direct-submission-design.md`](2026-07-25-memecoin-free-direct-submission-design.md).**
> The shared same-bytes transport, durable identity binding, aggregate precedence, threading, and
> audit contract described below are still current. The paid provider-selection sections — Helius
> Sender, Jito, and the EVM multi-endpoint `fanout` mode (below: "Provider semantics", and the
> `solana_fast_submit.py` Sender/Jito providers) — are **retired**. The production default is now
> `auto_free`: custody RPC + chain-owner official public endpoints + a local keyless Solana TPU
> sidecar, with no tip/subscription/key. `pooled_rpc` (one warm custody RPC) and `rpc` (legacy seam)
> are the only rollback modes; `sender` / `jito` / `fast_fanout` / EVM `fanout` fail startup.

Status: **transport implemented and current; paid provider-selection retired (see supersession note)**.
This document describes one chain-aware submission backend shared by every funded send: Solana entries
/ full exits / partial exits, and EVM entries / approvals / pre-transactions / full exits / partial
exits.

### 2026-10-10 amendment: dashboard USDC withdrawals

Dashboard Solana USDC withdrawals now use the funded context's shared same-byte sender, rather
than a separate public-RPC send. The withdrawal intent retains its existing signature/expiry
write-ahead fence; it does not use the entry-reservation binding. Submission audit ownership is
`action=withdraw`, `owner_kind=withdraw_intent`. Audit work is queued on the existing background
writer; provider threads still receive no database handle.

An immutable optional `SubmissionRequest.preflight_commitment` sets each RPC leg's preflight
commitment. Withdrawals use `confirmed` to match their blockhash. Existing entry/exit defaults
are unchanged; this adds no entry-path I/O, lock or retry. The shared USDC builder adds a fixed
50,000 CU limit and 2,000,000 micro-lamports/CU price: at most 0.0001 SOL priority fee, plus base
fee and any recipient ATA rent. It refuses unknown recipient-account reads and invalid expiry
heights before signing. CLI withdrawals reuse this builder and match simulation/preflight to
the confirmed blockhash, while retaining the CLI's existing direct submission seam.

Accepted/ambiguous sends remain fenced until chain resolution. Positive pending evidence cannot
be overwritten by a second endpoint's null response. Expiry proof stays pinned to the custody
anchor; if that anchor is unavailable, recovery waits even when the funded pool can rotate reads
or sends. Failed historical requests are never automatically replayed. See
[`2026-10-10-usdc-withdrawals.md`](../../audits/2026-10-10-usdc-withdrawals.md) for incident evidence
and focused regression checks.

The V5 decision engine, entry contracts, exit rails, quote/safety/exact-balance/post-peak gates,
claims, `ExitIntent`, and reservation-recovery are unchanged. This project begins only after a
decision or entry approval has produced an exact executable action.

## Why

Landing speed is dominated by (a) new TCP/TLS per request, (b) a single RPC endpoint, and (c) the
Jupiter `/swap` path being unable to attach both a priority fee and a Jito/Sender tip. The backend
addresses all three without weakening any crash-safety invariant, and preserves latency telemetry so
real p50/p95 stays visible after activation.

## Architecture

The load-bearing insight: both chain executors already treat `send_fn(payload)` as **tri-state** —
a truthy identity = accepted (proceed to chain confirmation), `None` = ambiguous (leave in
submitted-unknown), `False` = explicit rejection (safe to release / re-sign) — and both persist the
signed identity **before** calling `send_fn`. The shared transport preserves that seam, and Solana
entry adds one small executor contract: a persistent adapter owns the stronger immutable
payload/identity/validity binding instead of the legacy signature-only write.

Modules (all new, focused; the large seam files were not bloated):

- `sniper/execution/fast_http.py` — one long-lived pooled `httpx.Client` behind the injected
  `(method,url,headers,body,timeout)->(status,json,date)` seam. Keep-alive, bounded pool, bounded
  timeouts, **no automatic POST retry**, redacted transport errors (never a URL/api-key), idempotent
  `close()`, and shutdown-safe connection warming (`start_warming`, daemon thread + stop event).
- `sniper/execution/submission_transport.py` — immutable `AttemptState` / `SubmissionAttempt` /
  `SubmissionRequest` / `SubmissionResult` types + a bounded same-bytes fanout coordinator.
- `sniper/execution/solana_fast_transaction.py` — Jupiter `/swap-instructions` → one local v0 tx
  carrying a bounded priority fee + tip; `build_solana_fast_tx_builder` binds the network seams.
- `sniper/execution/solana_fast_submit.py` — standard-RPC / Helius-Sender / Jito providers.
- `sniper/execution/evm_fast_submit.py` — one provider per RPC endpoint, same-raw-tx fanout.
- `sniper/execution/submission_wiring.py` — adapter mapping the transport onto the `send_fn` seam.
- `sniper/execution/submission_backends.py` — constructs + owns the lifecycle from config.
- `sniper/settings/memecoin_submission.py` — validated config (shared pooled RPC by default;
  explicit `rpc` is the legacy rollback).
- Persistence: additive `memecoin_pending_entries` binding columns + `memecoin_submission_attempts`
  audit table (`db_memecoin.py`).

### Aggregate precedence (money-safe)

```
any ACCEPTED          -> ACCEPTED
else any AMBIGUOUS    -> AMBIGUOUS
else                  -> EXPLICIT_REJECTED
```

Seam mapping (`aggregate_to_seam`): `ACCEPTED -> tx_identity` (truthy), `AMBIGUOUS -> None`,
`EXPLICIT_REJECTED -> False`. This reproduces each executor's existing behavior exactly.

## Same-signed-bytes invariant

- The action is signed **exactly once**. The transport and every provider broadcast
  `request.signed_payload` **verbatim** — they never sign, re-sign, or refresh a blockhash/nonce.
- Every provider receives byte-identical bytes (`SubmissionRequest` is immutable and shared).
- Fanout is **duplicate delivery of one transaction**, never competing transactions.
- A provider that returns an unexpected transaction identity is AMBIGUOUS, never accepted (enforced
  in the provider and again defensively in the coordinator).
- A same-bytes rebroadcast (recovery re-invoking the transport) is therefore bounded by the original
  blockhash / nonce validity, because nothing here produces new bytes.

## Provider semantics

**Solana standard RPC** — `sendTransaction`, `encoding=base64`; the default pooled custody-RPC path
keeps preflight and the existing `maxRetries=3` landing reliability. Optional same-bytes fanout uses
`skipPreflight=true`, `maxRetries=0` because provider diversity supplies delivery redundancy.

**Helius Sender** — `sendTransaction`, `skipPreflight=true`, `maxRetries=0`, optional `swqos_only`.
The signed tx **must already contain** the tip + priority fee; a tx missing either is refused
(EXPLICIT_REJECTED) and **never submitted**. An HTTP receipt is submission, not confirmation.

**Jito** — `/api/v1/transactions` `sendTransaction`, base64; receipt = submission only; explicit
JSON-RPC rejection is classified separately from transport ambiguity.

**EVM RPC endpoint** — `eth_sendRawTransaction`; a returned hash matching the local identity (or
`already known`/`already imported`) is accepted. Only transaction-intrinsic invalidity is explicit.
State-dependent errors (`nonce too low`, underpriced, base fee, insufficient funds) are ambiguous
because another provider may already have accepted the same bytes. Endpoint labels contain no URL.

## Failure matrix

| Situation | Aggregate | Seam value | Executor effect |
|---|---|---|---|
| ≥1 provider accepted (identity matches) | ACCEPTED | identity | proceed to chain confirmation |
| all providers explicitly rejected | EXPLICIT_REJECTED | `False` | release / re-sign (existing safe path) |
| any timeout / transport loss / 5xx after write | AMBIGUOUS | `None` | leave submitted-unknown |
| returned identity mismatch | AMBIGUOUS | `None` | leave submitted-unknown (never accepted) |
| slow provider past the fanout deadline | AMBIGUOUS (that provider) | — | early accept wins; no wait past deadline |
| durable binding CAS fails (Solana entry) | AMBIGUOUS | `None` | **zero providers contacted**; preserve prior owner |
| HTTP 200 receipt but tx never lands | (accepted then) confirm unknown | — | stays submitted-unknown (chain is truth) |

An HTTP 200 or a returned signature/hash is **never** confirmation. Only chain status + balance
deltas confirm execution (unchanged).

## Durable submission binding + audit

### 2026-10-11 amendment: asynchronous final provider observations

The caller-thread audit statements below are superseded by the existing asynchronous file-ledger
audit writer introduced on September 26. Identity binding stays synchronous before broadcast.
A provider still pending at the first accepted result has unknown latency; its eventual completion
appends a separate observation through that writer and never revises the initial aggregate or
reservation state. No provider worker receives the caller connection. Actual Nozomi HTTP start/end
use per-call perf-counter timestamps; historical provider-wrapper timings retain their older basis.
Queue-full/process-loss limits remain observational gaps, never fabricated provider results.


- Solana **entry** persists the signed payload + sha256 + blockhash + last-valid height + backend via
  `bind_entry_submission` — an immutable CAS on `status='reserved'` plus the signed identity/hash.
  Rebinding the same bytes is allowed; different bytes are refused. The wiring calls it **before**
  any provider; a failed CAS sends to zero providers and returns `None` because an older signed
  identity may already own the reservation.
- **Exits** keep `ExitIntent` (already persists the signed payload + tx identity + validity before
  broadcast) as their binding — not duplicated.
- `memecoin_submission_attempts` is an **append-only audit** (chain, action, owner_kind, owner_id,
  tx_identity, payload **sha256 only**, provider_id, state, latency, response/reason code, timestamp),
  written on the **caller thread** after the fanout returns. Audit rows are observability only;
  reservation/claim/intent state remains execution authority.

## Threading

Provider workers receive only the `SubmissionRequest` (no DB handle) and touch no SQLite; the caller
owns all persistence and writes the attempt audit on its own thread after the coordinator returns.
The coordinator uses one bounded, reused executor — no per-call pool, no unbounded future
accumulation. Analytics/telemetry failure never changes an execution outcome.

## Configuration

Solana: `MEMECOIN_SOLANA_SUBMIT_MODE` ∈ `{pooled_rpc, rpc, sender, jito, fast_fanout}`
(default `pooled_rpc`); `rpc` is the legacy rollback;
`MEMECOIN_SOLANA_SENDER_URL`, `MEMECOIN_SOLANA_JITO_URL`, `MEMECOIN_SOLANA_SENDER_SWQOS_ONLY`,
`MEMECOIN_SOLANA_FANOUT_INCLUDE_RPC`, `MEMECOIN_SOLANA_FAST_TIP_LAMPORTS` (+ `_CAP`),
`MEMECOIN_SOLANA_FAST_PRIORITY_FEE_MICROLAMPORTS` (+ `_CAP`), `MEMECOIN_SOLANA_TIP_ACCOUNT`,
`MEMECOIN_SOLANA_SUBMIT_DEADLINE_MS`, `MEMECOIN_SOLANA_SUBMIT_TIMEOUT_MS`,
`MEMECOIN_SOLANA_WARM_INTERVAL_S`.

EVM: `MEMECOIN_EVM_SUBMIT_MODE` ∈ `{pooled_rpc, rpc, fanout}` (default `pooled_rpc`);
`rpc` is the legacy rollback; `MEMECOIN_EVM_SUBMIT_DEADLINE_MS`,
`MEMECOIN_EVM_SUBMIT_TIMEOUT_MS`, `MEMECOIN_EVM_SUBMIT_RPC_URLS_<chain>` (comma-separated).

Startup validation (`validate_submission_config`, called from `build_all_contexts`) refuses a fast
mode with: a missing required endpoint; a non-positive tip / priority fee / cap; a requested value
over its cap; fewer than two distinct fanout providers; a malformed or duplicate EVM endpoint; a
non-positive or unbounded (> 60 s) deadline/timeout; or an invalid tip account. The pooled defaults
use existing custody RPC URLs and need no extra provider credentials. Status output is URL-free.
The pre-existing exit-tiering priority-fee knobs remain separate.

## Recovery behavior

The executor recovery contract is unchanged. A crash after a provider accepted (or timed out)
remains submitted-unknown: the executor
committed the signed identity before the network write, so a restart sees a signed reservation/claim
that recovery resolves against chain — never released, never reclaimed, never double-submitted.
Release/re-arm happens only when every provider explicitly rejected before acceptance, or chain
evidence proves expiry/failure. The payload is retained for exact forensic/recovery identity; the
current recovery pass checks chain state and does not automatically rebroadcast it.

## Explicitly NOT built (no false claims)

- **No direct-TPU backend** — this host has no Rust toolchain; nothing here fakes TPU or installs
  system tooling.
- **No pool-family-specific Solana AMM instruction builders** — fast Solana tx construction consumes
  Jupiter's instructions unchanged.
- No new strategy / V5 / sizing logic in the transport package.
- EVM V2/V3/V4 route construction and exact-balance behavior are unchanged; only the network
  submission transport is shared.

## Runtime effect

At the next funded-runner construction, absent an explicit mode override, entry/full-exit/partial
submission uses the shared pooled custody-RPC backend. `rpc` restores the legacy seam. Sender/Jito
and multi-RPC EVM fanout still require explicit validated configuration. The backends carry no
durable in-memory state; `atexit` closes them on normal exit and the OS reclaims sockets on hard kill.
This code change performs no deploy, restart, live send, or `.env` mutation.

### Note on test placement

Transport-backed crash safety is covered in `tests/test_memecoin_fast_submit_crash_safety.py` (a new
focused file) rather than by editing `tests/test_crash_windows.py`, to avoid perturbing the existing
crash harness; the same submitted-unknown / no-double-submit / safe-reset invariants are asserted on
the real executor paths with a transport injected.
