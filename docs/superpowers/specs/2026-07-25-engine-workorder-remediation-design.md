# Engine work-order remediation design

**Date:** 2026-07-25
**Baseline:** `76478a60`
**Authority:** no flag activation, deployment, service restart, live RPC, or production-DB mutation

## Goal

Make the merged WO-1..12 work truthful and safe. The release must not permit a second full-exit
broadcast after any crash, the deployment smoke command must fail unless it inspects the pinned
release and every mandatory live prerequisite, and shadow/observability surfaces must either be
wired end-to-end or report that they are unavailable.

## 1. Durable full-close state machine

`is_open` remains the settled ledger fact; it is no longer used as a pre-broadcast claim by the real
executors. A real close uses position-owned fields:

- `close_claim_id`, `close_pending_at`: unsigned owner claim;
- `close_sig`, `close_submitted_at`, `close_exit_reason`: signed identity persisted before send;
- signed/submitted state is never released automatically, even after a restart;
- an explicitly rejected send or confirmed chain failure may return the same owner to pending during
  the same executor call; a crash leaves the durable submitted state untouched;
- confirmed fill insertion and position finalization commit atomically under the exact claim token and
  signature;
- legacy `is_open=0, closed_at=NULL` rows with no booked fill are quarantined, never reopened.

Normal confirmed exits still return `closed`; unknown confirmation returns `unknown`. A subsequent
executor sees the outstanding claim and returns `already_closing` without signing or broadcasting.

## 2. Deployment smoke gate

The deployment gate has two explicitly different modes:

- `--synthetic`: CI-only plumbing check, labelled `SYNTHETIC PASS`; it can never print deployment PASS.
- deployment mode: requires `--release-dir` and `--state-root`; imports and launches from the release,
  inspects the child process's actual DB descriptors, requires both chain tapes fresh, verifies the
  temp-copy snapshot/decision evidence chain through the release code, checks state-root flag reload,
  and treats every missing/skipped mandatory check as failure.

No check mutates the real state root. Child environment is supplied only to the child process; the
parent does not mutate global process environment.

## 3. Completion truth for WO-1/2/3/4/5/6/7/12

- WO-1: the supervisor must be callable from an actual loop, enforce fallback rather than expose a
  passive predicate, stamp notification time at receipt, consume prewarmed previews on CUT, and stay
  default-OFF.
- WO-2: full-exit counterfactuals read the continuing tape, not open-position snapshots that stop at
  close. The report remains proposal-only.
- WO-3/4/5/6: the worker writes one shadow decision per materialized position/pass. Context is computed
  causally as of the decision time. Solana regime reads its own DB. V5 produces a persisted comparison,
  and wallet activity remains `unavailable` until creation-history identity exists.
- WO-7: configured tiering is passed into the real executors only when its default-OFF flags are on;
  exit measurements use a dedicated exit-execution table, not entry latency.
- WO-12: the endpoint selects engine-envelope positions, reports exact-data unavailability honestly,
  and computes distance from the frozen catastrophe threshold. The existing authenticated dashboard
  renders the returned cockpit rows.

## 4. Tests and release gate

Every behavior is TDD. Required regression: crash after full-exit broadcast leaves `submitted_unknown`,
is never released or reclaimed, and a retry performs zero additional broadcasts on both Solana and
EVM. Focused funded-path, work-order, schema, and diff checks must pass. No full suite is required by
this remediation because the operator explicitly rejected it as wasteful.

### 2026-10-14 amendment: durable on-chain mirror deferral

An eligible on-chain source sale creates one durable exit intent per position and source transaction in the existing mirror audit. It pins the original position and sale quantity/proof; a later same-coin re-entry or modified source row cannot become that intent's owner/evidence. The sell sweep rehydrates pending intents independently of its cursor and the temporary in-flight-entry hold. An atomic audit transition owns execution. Only the existing wallet owner's typed proof that the executor body was never entered may return an executing intent to pending, such as an unsigned maintenance deferral. Ordinary deferred dictionaries, exceptions and unknown execution outcomes do not grant retry authority. A durable close/derisk claim or signature retains the existing recovery owner and cannot trigger a blind repeat broadcast. Confirmed/known-closed and policy-held decisions remain terminal; unknown position state remains pending and legacy audit rows are not replayed.
