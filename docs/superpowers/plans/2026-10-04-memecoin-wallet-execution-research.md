# Wallet execution research implementation

> Execution: inline with `superpowers:executing-plans`. Operator approved the design and plan in advance on 2026-10-04; execute through rollout without another approval pause.

**Goal:** Reject unsupported hypothetical trade conclusions automatically, collect an admission-independent wallet/chain research census off the money path, and provide causal replay and inspectable coverage.

**Architecture:** Pure evidence validation and replay; immutable research-owned SQLite store; bounded read-only archival worker; shared publication embargo for legacy outputs and consumers; precomputed read-only Wallet Map coverage.

**Stack:** Python, SQLite, pytest, existing Flask and Wallet Map JavaScript, pinned releases.

**Spec:** `docs/superpowers/specs/2026-10-04-memecoin-wallet-execution-research-design.md`.

**Global constraints:** No new funded I/O, locks, retries, provider requests, roster changes, or mutations of frozen contracts. Atomic quantities are TEXT. Unknown is not zero. Actual funded history remains readable. Missing route state or historical seconds remains blocked. Research starts with existing committed local sources; no provider capture activation without independent capacity proof. No database backups during deployment.

**Review focus:** Look-ahead, false quiet coverage, cached-receipt bypasses, outcome-dependent denominators, funded selector latency and fallback, writes escaping the owned research store, immutability, and unsupported UI conclusions.

## Task 1: Pure evidence contract and causal replay

**Files:** Create `sniper/research/wallet_execution/{__init__,evidence,replay}.py`, `tests/test_wallet_execution_evidence.py`, `tests/test_wallet_execution_replay.py`.

**Interfaces:** `episode_gate(episode) -> Eligibility`; `study_gate(manifest, episodes) -> Eligibility`; canonical digest; `replay(episode, policy) -> modeled result`. A validated episode is not a validated strategy. A study receipt binds the frozen protocol, ordered raw episodes, wallet/chain, all-origin census, trial ledger and untouched future partition.

1. Write tests for legacy/missing protocol, empty path, seconds/gaps, unknown costs, wrong quantities/chain, publication after decision, clock regressions, ambiguous candle order, provider versus token failure, and causal dip/mirror delays.
2. Run `python3 -m pytest -o addopts='' tests/test_wallet_execution_evidence.py tests/test_wallet_execution_replay.py -q`.
   Expected: RED before implementation, including a positive exact-quantity fixture.
3. Implement pure validators and bounded immediate/dip plus TP/SL/trail/mirror/time-cap replay using full acquired-quantity quote evidence. No interpolation or later trough selection.
4. Run the same tests. Expected: GREEN. Commit `feat(research): validate causal one-second execution evidence`.

## Task 2: Independent census, immutable store and local worker

**Files:** Create `sniper/persistence/db_wallet_execution.py`, `sniper/services/wallet_execution_research.py`, `scripts/research/wallet_execution_worker.py`, `tests/test_wallet_execution_store.py`, `tests/test_wallet_execution_worker.py`; fix `scripts/research/sol_signal_window_tape.py` with focused regression.

**Interfaces:** Store frozen studies, immutable source episodes/raw evidence/results, append-only ingestion cursors; bounded local projections produce a JSON coverage artifact. Raw legacy rows enter the census with typed evidence gaps, never an eligible synthetic path. Writer owns only its database/artifact.

1. Write RED tests for immutability, exact atomic TEXT, repeated input deduplication, all-origin census including refused signals, per-wallet-chain isolation, missing-source handling, cursor progress, and malformed Solana RPC envelopes refusing quiet coverage.
2. Run focused new tests and signer-window tests. Expected: RED at the named new interfaces/false quiet bug.
3. Implement owned store, bounded incremental local worker and ingest/validate/replay CLI; preserve quiet-frontier limitations explicitly. Archive available ordered tape without inferring full pricing state. Expose typed coverage for every armed cell and known candidate cells.
4. Run focused suites. Expected: GREEN. Commit `feat(research): persist wallet-chain census and coverage off path`.

## Task 3: Close legacy publication and consumer bypasses

**Files:** Shared `sniper/research/wallet_execution/publication.py`; named publishers/consumers discovered in current code; focused tests plus `docs/audits/memecoin-wallet-research-20261004/check_current_gate.py`.

**Interfaces:** Legacy modeled data is diagnostic only. Cached receipts are rechecked. All standalone hypothetical report entry points require explicit diagnostic mode and cannot publish a strategy finding. Funded copy-arm selection returns the existing fallback before ledger projection; no research database read on entry.

1. Write RED tests proving sparse exit paths, empty timecaps, cached ready reads, unchecked fitted artifacts, unsupported paper rankings, and funded selection cannot bypass eligibility.
2. Run focused tests. Expected: RED with old count-only consumers.
3. Apply shared gate to conclusion builders and cached consumers; retain raw diagnostics, actual funded totals and source history. Disable automatic legacy paper-to-funded policy selection. Inventory and quarantine standalone legacy performance publishers behind explicit diagnostic-only execution.
4. Run focused tests and the committed runnable defect check; measure selector fallback p50/p90 against the old projection path. Expected: GREEN; both original defects rejected; no funded I/O. Commit `fix(research): reject unsupported performance claims and selectors`.

## Task 4: Wallet Map research coverage

**Files:** Existing `sniper/routes/api_memecoin.py`, `static/app.js`, related CSS; `tests/test_wallet_execution_endpoint.py` and browser verification.

**Interfaces:** Read-only precomputed coverage endpoint and per-wallet-chain inspector cards. Show sampled versus complete seconds, identity/execution gaps, census, study state and supported conclusions only when eligible. Keep navigation, pan and zoom working.

1. Write RED endpoint tests for absent/stale/malformed artifacts and wallet/chain isolation.
2. Implement bounded local artifact reads and clear Wallet Map research cards. No provider calls or full main-ledger scans during polling.
3. Run endpoint tests and browser interactions at desktop/mobile widths. Expected: GREEN; no overlap or fabricated winners. Commit `feat(ui): show wallet execution research coverage`.

## Task 5: Fresh review and durable evidence

**Files:** Spec status, audit README/evidence, deployment scripts/tests as needed; brain implementation record.

**Interfaces:** Fresh reviewer receives spec, plan, ledger and full diff. Research release is content-addressed, state root separate, read-only source handles, and rollout never needs funded restart solely for research.

1. Run focused integrated suites, schema-reference validation and compile checks. Expected: GREEN, with unrelated existing warnings identified.
2. Request a fresh whole-branch review using the most capable available reviewer. Fix important defects with observed RED then GREEN. Record minor rulings.
3. Complete knowledge-sync audit; record exact verified implementation evidence in the brain with honest authorship and no strategy-performance claim. Update normative rule state only to reflect gates actually implemented.
4. Commit final audit and rollout machinery. Expected: clean branch, concrete rollback and source/capacity limits documented.

## Task 6: Merge, deploy and prove served state

**Files:** Pinned web/research release metadata and operator audit.

**Interfaces:** Merge the reviewed branch without disturbing primary untracked work, push authorized commits, deploy pinned web and research worker; any necessary funded-selector cutover uses existing pinned-runtime reservation/reconciliation checks and no policy activation flags.

1. Verify active release/process/effective configuration and reservations before cutover. Expected: compatible clean merged SHA and safe runtime gates.
2. Deploy without backups; activate local-only archival research worker. Expected: research uses pinned code and separate state; no new provider requests; funded service continues under its documented policy.
3. Read back process identity, heartbeat, research artifact/census and authenticated served UI/API. Expected: exact reviewed SHA, truthful blocked coverage where data is absent, old hypothetical promotion bypass rejected, funded fills/exits intact.
4. Record verified rollout and residual data requirements. Finish with actual outcome and limitations; future evidence must accrue before any wallet strategy conclusion.

## Completion record

Tasks 1–6 protective implementation completed. Independent review defects were
reproduced and fixed; pinned required suite passed 9,715 tests, six skipped.
Runner/web/research deployed at 24c360cc1; actual runtime, ledger, coverage and
authenticated endpoints passed. Deployment and brain proof are in the dated audit.
This completes the protective research machinery, not the future empirical study.
Full executable venue collectors/prior calibration, coin-conditioned strategy
search, statistical uncertainty/stress and authenticated study publication remain
blocked. No wallet policy is supported, no money promotion is authorized, and no
organic fill-speed improvement is asserted.

## Task 7 addendum: preserve active ordered coin tape (2026-10-04)

Operator authorized closing the remaining collection gaps. This slice adds a
zero-provider, research-owned archive of the active `memecoin_onchain_volume.db`
producer. The old `memecoin_ticks.db` archive remains diagnostic; its Robinhood
cursor is stale and cannot stand in for the active sequencer stream.

Edge cases before editing: no money-path entry/exit/sizing/signing change or
added funded latency; owned connections and state only; missing/zero/unknown
frontiers stay distinct; timeout/lock/malformed/partial inputs become collection
gaps; chain/mint/atomic/order identities remain explicit; every committed buy
origin is enrolled irrespective of an admission attempt or fill; inode changes,
reorg/header/gap revisions and retention are retained; rollback stops only the
research worker and preserves receipts. This slice does not handle complete
V3 tick state, V4 hook/liquidity/fee changes, taxes, exact quote calibration,
prior delay/cost calibration or supported strategy publication. The episode
eligibility gate remains unchanged.

1. Write focused failing tests for active source selection, every-feed-origin
   enrollment, shared percoin capture, at least 120 seconds before receipt and
   1,800 seconds after it, bounded pagination/restart, ordered raw logs,
   immutable header/gap revisions and unsupported-state diagnostics.
2. Implement `sniper/research/wallet_execution/ordered_capture.py`; integrate it
   into the research service and worker. Archive only its owned store. Read the
   active producer through short read-only snapshots. Percoin pagination cycles
   through the retained window to preserve late revisions; a capture frontier
   is explicitly a producer observation, never proof of quiet executable state.
3. Run the focused suite, then the existing research worker/store/evidence/deploy
   checks. Measure a read-only local producer pilot writing only a temporary
   research store; record p50/p90 duration, rows/pages/bytes and resource limits.
4. Commit only the adapter, research service/worker, focused tests and this
   addendum after notifying the coordinating agent. The coordinating agent owns
   integrated review and rollout. Activate only the research pinned release;
   no funded restart and no provider activation. Verify prospective census,
   active source identity, changing raw archive and truthful blocked paths.

Capacity ruling: the existing isolated EVM quote lane has a four-calls-per-second
budget. An exact buy plus one acquired-quantity sell at each roughly 10 Hz
Robinhood block requires at least 20 calls per second per coin, before headers,
logs, currency/cost reads and alternative quantities. Complete validated state
is required to share pricing across wallets; sparse quote polling cannot replace
it. Solana's shared research pacer and flat-book restriction remain intact.

Task 7 implementation evidence: initial 13 negative checks failed before the
adapter existed; four additional queue/order/lifecycle regressions also failed
before correction. Focused wallet-execution suite: 96 passed. A read-only local
producer pilot enrolled all 200 origins in its bounded slice, shared 85 coin
requests, and preserved 752 ticks/664 header snapshots in a temporary store.
Twelve passes measured p50 4.60ms/p90 33.20ms, 2.47MB output and 26.3MiB max RSS.
No collector provider request or production write occurred. Budget/retention,
missing pool/header, revision completeness and unsupported pricing gaps remain
explicit. The pilot is not full-cohort or executable-path proof.

An independent unsigned diagnostic on the configured budget-disjoint 4663
capture endpoint used five requests at two-second spacing through the existing
HTTP seam. Chain identity, pinned header, deployed quoter bytecode and a recent
pool's exact buy/reverse-quantity quotes answered. Two earlier generic-transport
reads were rejected with HTTP 403. Current aggregate capture reverts cannot be
attributed to old dead coins without per-call mint/method evidence; a systemic
quoter/ABI failure was not reproduced. No net-fill, fee, floor or delay claim.

Rollout remains the coordinating agent's step: pin only the separate research
release, verify `source_state.ordered_capture` names the active producer and
preserves new origin/tick/header/frontier snapshots, then verify immutable raw
counts increase prospectively and executable episode counts remain honest.
No funded restart, provider activation or flat-book exception is required.
