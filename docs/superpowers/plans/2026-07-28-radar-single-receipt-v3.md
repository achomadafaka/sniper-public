# Radar Single-Receipt Confirmation v3 Implementation Plan

> Execute inline in `/home/bigpapi/Desktop/Sniper/.worktrees/radar-main` on
> `feat/radar-single-receipt-v3`. Follow TDD: add one failing behavior test, confirm the expected
> failure, implement the smallest change, rerun the focused test, then continue.

**Goal:** Replace duplicate same-event confirmation shadow receipts with one forward-only v3 receipt,
evaluate immutable condition labels against their excluded cohorts, render honest coverage and
replication state, and deploy the exact tested release without changing funded execution.

**Architecture:** `directional.py` owns the v3 predicate and condition-label derivation. `engine.py`
persists one primary receipt plus one optional later-event delayed receipt. `insights.py` calculates
v3-only validation while retaining v2 as a separate historical pilot. `static/app.js` renders the new
shape with bounded keyed updates. Existing append-only tables and retention paths are reused.

**Stack:** Python 3.11, SQLite, pytest, plain JavaScript, systemd pinned-release deployment.

---

## Task 1: Define v3 rule and immutable labels

**Files:**

- Modify: `tests/test_memecoin_opportunity_directional.py`
- Modify: `sniper/research/memecoin_opportunity/directional.py`

1. Add tests proving one eligible state returns only `pump-confirmation-truth-v3` and exact
   `independent_pass`, `clean_pass`, and `low_friction_pass` labels.
2. Add fail-closed boundary cases for missing HHI/sell pressure and prove creator sale cannot produce a
   clean pass.
3. Run the new tests and confirm they fail because the v3 functions do not exist.
4. Implement `evaluate_confirmation_v3()` and `confirmation_v3_labels()` by reusing the existing
   truth-v2 predicate values without changing thresholds.
5. Retain the v2 function/constants for immutable historical interpretation; add v3 rule constants.
6. Run `python3 -m pytest tests/test_memecoin_opportunity_directional.py -q`.

## Task 2: Persist one primary receipt and one genuine delayed receipt

**Files:**

- Modify: `tests/test_memecoin_opportunity_engine.py`
- Modify: `sniper/research/memecoin_opportunity/engine.py`
- Modify: `sniper/research/memecoin_opportunity/projection.py`
- Modify: `sniper/research/memecoin_opportunity/outcome_tracker.py`
- Modify: `sniper/research/memecoin_opportunity/retention.py`

1. Replace the paired-shadow engine test with assertions that a qualifying quote creates exactly one
   primary alert/entry/features row, uses v3 identities, writes no same-event route control, and stores
   all condition labels and launch mode.
2. Update delayed tests to require `pump-confirmation-delayed-v2` paired to the v3 alert.
3. Add a regression proving a replay cannot create a duplicate v3 entry or extra quote-derived work.
4. Run the focused engine tests and confirm failure on old v2/multi-arm behavior.
5. Wire v3 evaluation into `observe_remote_completion()`. Use the existing remote result once, select
   one v3 arm, and append one receipt. Preserve route controls for non-confirmation candidate rules.
6. Store v3 feature snapshots as `confirmation-features-v2`; keep legacy rows on v1.
7. Add v3 primary/delayed rules to extended outcome and retention protection without removing v1/v2.
8. Add v3 projection labels and rerun focused engine, projection, outcome, and retention tests.

## Task 3: Build honest forward validation and keep v2 separate

**Files:**

- Modify: `tests/test_memecoin_opportunity_insights.py`
- Modify: `sniper/research/memecoin_opportunity/insights.py`

1. Add fixture builders for v3 scored, pending, invalid, condition-pass/excluded, mode, creator, and
   delayed rows.
2. Add RED tests for exact 24/25 boundaries, invalid exclusion, condition separation, mode replication,
   creator concentration, negative evidence, delayed median, and immutable v2 pilot isolation.
3. Implement one-pass v3 aggregation helpers. Read explicit stored labels; do not derive them from
   mutable state or query per alert.
4. Return primary coverage, condition cohorts/deltas, mode rows, creator concentration, delayed pairs,
   blockers, checkpoint, and the ordered verdict status.
5. Preserve v2 arm results under `historical_pilot`; prove changing v2 results cannot change v3 status.
6. Add v3 rules to retained target comparison and rerun `tests/test_memecoin_opportunity_insights.py`.

## Task 4: Render v3 evidence without duplicate-arm claims

**Files:**

- Modify: `tests/test_memecoin_opportunity_ui_contract.py`
- Modify: `tests/test_memecoin_opportunity_projection.py`
- Modify: `static/app.js`

1. Add RED contract assertions for “Forward validation — v3”, pass-versus-excluded coverage, mode
   replication, historical v2 pilot labeling, no arm-overlap claim, and v3 receipt labels.
2. Add v3 projection arm fields and candidate evidence routing.
3. Replace the microscope rows with primary, conditions, modes, and delayed sections. Show
   `valid / total / invalid` beside the headline and explicit blockers.
4. Keep v2 under historical pilot details. Keep recent receipts at eight and existing keyed DOM patching.
5. Retain the verdict-only atomic live region and ensure `FIRE NOW` remains absent.
6. Run UI/projection tests and `node --check static/app.js`.

## Task 5: Documentation and branch-close knowledge sync

**Files:**

- Modify: `docs/superpowers/specs/2026-07-28-radar-single-receipt-v3-design.md`
- Modify: `AGENTS.md`

1. Mark the reviewed design approved and record the exact implemented versions.
2. Add the new authoritative spec to the AGENTS Radar documentation list.
3. Record Knowledge delta: new paper-evidence architecture; no funded doctrine change.
4. Run `python3 scripts/validate_schema_refs.py --warn-unknown-tables` and `git diff --check`.

## Task 6: Broad verification and performance proof

1. Run the full focused Radar/retention test selection discovered by
   `rg --files tests | rg 'memecoin_opportunity'`, excluding stress tests.
2. Run schema validation, JavaScript syntax, import-boundary tests, and whitespace checks.
3. Build a representative API fixture and verify payload remains below 150 KiB.
4. Measure repeated `read_live_insights()` calls against a read-only copy/fixture and compare with the
   current baseline. Reject any material regression.
5. Inspect the diff for funded imports, execution writes, threshold drift, hidden duplicate receipts,
   and unrelated workspace changes.
6. Commit logical implementation changes with explicit paths only.

## Task 7: Integrate, deploy, and verify live

1. Fetch `origin/main`, inspect divergence, and merge the tested branch without rewriting history.
   Do not open a PR. Do not force-push.
2. Run the final verification commands on the exact integration SHA.
3. Use the repository pinned-runtime deployment scripts and required backups. Do not activate or alter
   funded policy, V6, reservations, or rollover settings.
4. Deploy the opportunity worker and web release from the exact integration SHA; atomically switch
   manifests/symlinks and restart only those services.
5. Verify pinned SHA, runner commands, one process per service, authenticated health/API response,
   source health, storage/RPC state, logs, v3 response shape, payload size, and stable heartbeat.
6. Confirm historical v2 remains visible, v3 begins forward-only, and no funded service changed.

## Required completion evidence

- Focused and broad pytest totals with fresh output.
- Schema, JavaScript, boundary, and whitespace checks.
- Exact branch/integration/release SHAs.
- Runtime symlinks/manifests, process counts, health, API contract, logs, and heartbeat.
- Files inspected/changed, scope guards, risks, unresolved questions, and follow-ups.
