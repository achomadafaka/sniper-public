# Desk Cost Pricing Implementation Plan

> **For agentic workers:** Use subagent-driven development for owned independent tasks and TDD. Root owns staging/commits; never stage a peer's WIP.

**Goal:** Automatically resolve evidenced desk expenses without slowing fills or fabricating zero.

**Architecture:** Bounded off-path reader/lookup/resolver; immutable evidence and refund-first ledger
transaction; pure receipt parsers; an explicit worker executable, never started by this build.

**Tech Stack:** Python standard library, existing SQLite helpers, pytest `-n 2`.

**Spec:** `docs/superpowers/specs/2026-10-21-desk-cost-pricing-design.md`.

## Global Constraints

No push/deploy/runtime/env/production writes. Preserve $25/5/$200/-$200 and all existing book caps.
No new I/O/lock/import/retry on funded fill path. Unknown remains NULL; kills never clear.

## Review Focus

1. Booking between lookup and write cannot double-charge pending + entry.
2. Refund plus gross cost must not create an intermediate irreversible trip.
3. Failed/retried signatures remain visible even when live pointers change.
4. Wrong identity, incomplete proof, stale rates and SDK estimates cannot become known cash.
5. One unavailable row cannot starve others; no network request holds a SQLite writer/read snapshot.

## 1. Persistence — desk_store worker

Files: `sniper/persistence/db_memecoin.py`, `tests/test_memecoin_desk_cost_store.py`.

Interfaces: `list_unpriced_desk_execution_costs(conn, *, after=None, limit=20)` and
`resolve_desk_execution_cost(conn, *, request, execution_usd, refund_usd=0, evidence, known_at, provenance, commit=True)`.

- [x] Write RED temporary-ledger/race tests for the five review classes and caller rollback.
- [x] Implement bounded whitelisted reads and immutable evidence/refund-first resolution.
- [x] Run focused `python3 -m pytest -n 2 tests/test_memecoin_desk_cost_store.py tests/test_memecoin_desk_cohort.py -q --tb=short`.

## 2. Pure Solana parser — codex_startup worker

Files: `sniper/services/memecoin_desk_costs_solana.py`, matching test file.

Interface: `price_solana_expense(request, receipt, *, owner, usd_quote, known_at)` returns
`execution_usd`, `refund_usd`, evidence or raises a typed ValueError.

- [x] RED native/USDC/exit/failed/WSOL/refund/identity/FX cases; prove missing differs from zero.
- [x] Implement strict exact-atom accounting with labeled historical USD conversion.
- [x] Run `python3 -m pytest -n 2 tests/test_memecoin_desk_costs_solana.py -q --tb=short`.

## 3. Model evidence — desk_contract worker; root ingress integration

Files: `sniper/services/memecoin_desk_model_costs.py`, matching tests; root only modifies
`sniper/services/memecoin_desk.py` and its pipeline regressions.

Interface: `normalize_model_cost_receipt(receipt, *, ticket_id, model, response_at, billing_basis='unknown')`.

- [x] RED: estimate never billed, explicit zero versus absent, wrong identity, bad usage/error.
- [x] Preserve usage/notional independently; actual billed proof resolves cash only once.
- [x] Operator delegated the subscription choice: explicit frozen flat basis gives $0 marginal cash;
  usage/notional stay separate, and missing proof remains unknown.
- [x] Integrate supplied receipt at existing result ingress without a model/provider call.

## 4. Worker/transport — coordinator

Files: `sniper/services/memecoin_desk_costs.py`, `scripts/memecoin_desk_cost_worker.py`, matching tests.

- [x] RED: historical candle selection, provider failures, bounded cursor, real-ledger integration,
  stale ownership, provider I/O outside transactions and CLI path validation.
- [x] Implement serial bounded batch with injected receipt/FX callbacks, separate read/write handles,
  response bounds/timeouts and typed deferrals. Loop is opt-in; no service installed or started.
- [x] Run targeted adapter + step-3/manual/recovery gates; review final diff and run schema checks.
  Final: 810 passed in 36.13s at -n 2; schema/connection/diff checks pass.
- [x] Commit logical changes locally: money `6cf6ef22c`, facade/worker `4ddbad6cf`.
  Exact final handoff goes to Beta/Gamma; Lambda has the receipt mapping and ownership boundaries.

## Delivery

- [x] Inspect Beta's exact money review and resolve any reproducible defect with RED/GREEN proof.
- [x] Gamma PASS received for `cc9f56d92`; further review findings remain in scope.
- [x] Fresh independent adapter review and audit-only knowledge delta; report in the step-3 review
  amendment. Both internal scoped reviews PASS; no full/live/deploy checks.

## Review corrections

Beta PASS on ff48dbb32 (no new defect); Gamma PASS on cc9f56d92. New adapter independent review
caught and fixed: failed-commit rollback after savepoint release, canonical funded float quantity
round-trip, receipt-present malformed scalar losing the incurred-call hold, and truncated HTTP
response escaping the worker. Each had actual RED/GREEN proof. Existing generic cost writer and
funded reservation/entry/exit code remain unchanged. The root worker does not call JUDGE.

Lambda owns step-4 awaiting-JUDGE helper/error-cause additions on his own branch. No Codex edit
in db_memecoin_desk.py or memecoin_desk_contract.py conflicts with that work. His future caller
will supply the structured final SDK receipt and freeze subscription billing basis explicitly.
