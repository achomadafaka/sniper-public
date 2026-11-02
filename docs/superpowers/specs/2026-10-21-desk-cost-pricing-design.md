# Desk automatic cost pricing

Operator scope: automatically resolve incurred desk expenses and review defects. Local build only;
no push, deploy, restart, production writes or environment changes. Keep $25 clip, 5 positions,
$200 daily halt and -$200 cumulative cohort kill. The implementation extends step 3, not entry policy.

## Boundary and accounting

A separate bounded worker reads expense requests, collects evidence without a ledger transaction,
then prices one request in a short transaction. No new provider request, callback, lock, import or
retry enters the funded fill path. The worker never loads a private key or signs. It takes the
explicit ledger path and public custody address; observation uses its own read-only connection.

Existing `record_desk_cohort_cost` remains the once-only accounting primitive. Expenses are keyed
by `entry:<position>`, `partial:<position>:<signature>`, `exit:<position>:<signature>`, or
`pending:<reservation>:<signature>`. Successful entries own their fees; a failed pending receipt
must not be billed again when the same transaction books a position. Historical reset signatures
remain priceable after a later retry. Original incurred time is immutable; `known_at` is when all
receipt/rate evidence became available. Killed cohorts are still reconciled; a refund never clears
a persisted kill.

The resolver rechecks cohort, real desk ownership, chain, mint, exact signature and fill after the
lookup. It saves immutable evidence and gross/refund amounts in the same transaction. A rent refund
is recorded BEFORE its primary expense. Otherwise an intermediate gross charge can falsely latch
an irreversible kill although the complete transaction's net stays above -$200. A failure rolls
back evidence, refund, charge and any new trip together. Caller transactions require explicit
commit=False and a savepoint; no caller work is accidentally committed or discarded.

## Solana proof

Only confirmed, parsed receipts with the exact first signature and configured signer/fee payer
qualify. Missing meta, fee, token balances, block time, ambiguous ownership or malformed integers
remain unknown. Failed transactions contribute their actual network fee only; rolled-back tips,
transfers and account rent are not charged. A present valid zero fee is distinct from a missing fee.

For a successful trade, match the mint quantity and USDC cash movement to its exact persisted fill.
A native-funded entry also has persisted actual funding atoms. Non-trade native cost is:

`payer native decrease + owned WSOL token decrease - actual native trade principal`.

That amount already includes fees, paid tips and net rent. WSOL principal is not rent. The network
fee is a positive charge floor; excess returned rent is a separately recorded refund. Never add the
priority estimate to `meta.fee` or deduct the trade principal twice. Stored fill fee facts, where
present, must agree with the receipt rather than override it.

Native atoms are exact. USD is an explicitly labeled accounting reference valuation, not evidence
of an actual SOL/USD conversion. The default source is the last completed Coinbase SOL-USD
60-second candle at/before receipt block time; its close is at most 60 seconds old. Persist source,
rate, observation time, availability time, receipt hash and component atoms. Empty/incomplete,
stale, future or malformed rate results remain unknown. A proven zero cost needs no FX rate.

Solana is the current funded desk chain. EVM jobs remain explicitly unsupported/unknown in this
slice: a complete `memecoin_evm_tx_fees` row alone cannot prove approval/retry/tip completeness or
supply a contemporaneous USD rate. No EVM admission authority is added.

## Model proof

`total_cost_usd` from the Claude CLI/SDK is an API-equivalent estimate, not a billing statement.
Token counts and that estimate are preserved separately. They never become an exact cash charge.
A supplied attributable billing receipt can price the original stable JUDGE expense, including
failed, timeout or SKIP calls. Missing billing evidence remains NULL.

The operator confirmed subscription use and delegated the accounting choice. Covered subscription
calls count as $0 marginal cash; usage and API-equivalent estimates remain separate. This policy
must be explicitly frozen as `model_billing_basis=subscription_flat` at cohort provisioning and
match the caller-generated billing basis. Missing policy/proof remains unknown; setup-token
presence alone does not enable zero. A valid attributable actual bill overrides the flat basis.
Immutable vet rows
are not rewritten on a late valuation. Reporting must use the authoritative ledger expense when
available, while retaining the original vet audit.

## Worker and failure map

- Batches are serial, at most 100 requests, with a rotating `(cohort_id,event_id)` cursor. An
  unavailable oldest request cannot starve later requests. Every unresolved row stays visible.
- Public receipt/rate reads have bounded response bytes and timeouts, no inline retry or custody
  fallback. Timeout, transport, HTTP refusal/rate-limit and malformed response are distinct results.
  A provider rate limit stops the batch. No exception or empty payload is treated as zero cost.
- No process-wide settings, environment, connections, DNS or credentials are mutated. All I/O
  completes before the writer lock; a concurrent booking causes stale-request refusal.
- The denominator is every incurred desk expense, including failed transactions and model
  abstentions, independent of profit or current cohort kill state.
- No service is started or installed here. The worker's loop is available for a separately reviewed
  activation. Stopping it leaves unresolved expenses holding new admission, not silently priced.
- Out of scope: unseen/non-ledger signatures, missing receipt history, attribution of fixed monthly
  subscription overhead, EVM completeness, new admission filters, and live latency. Historical
  slot-lane fills that already credit a SOL surplus into trade notional remain unpriced: pricing
  that receipt's native credit again would double-refund it. The current desk uses the ordinary
  executor, which never applies that slot-specific surplus credit. Its strict USDC equality is
  retained; this build does not alter funded writers or invent missing surplus provenance.

## Implementation owners and proof

Codex persistence worker owns only `db_memecoin.py` plus cost-store regressions. Codex pure-pricing
worker owns the Solana parser plus its tests. Codex model worker owns the model normalizer/tests.
Coordinator owns orchestration, CLI, ingress integration and docs. Beta retains LP/exit code;
Lambda retains seat/provider/U-worker files. All work remains in the existing isolated worktree.

Regression gates: actual RED before implementation; real temporary ledgers; refund-order latch,
full rollback, pending-to-entry race, old retry, conflicting valuation race, cursor starvation,
zero/NULL, principal/rent separation, wrong identity, stale/future FX, estimate-not-bill, bad SDK
results, recovery and existing manual-intent contracts. All pytest uses `-n 2`; no full suite unless
justified and protected by the shared host lock.

Sources: [Solana getTransaction](https://solana.com/docs/rpc/http/gettransaction),
[Solana JSON structures](https://solana.com/docs/rpc/json-structures),
[Coinbase historical candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles).
