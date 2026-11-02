# Engineering Followup Fixes Implementation Plan

> **For agentic workers:** Use the debugging and TDD skills. Independent file groups run in parallel; the parent reviews and verifies the combined branch.

**Goal:** Repair residual EVM02 and N01–N13 from the October 9 followup audit.

**Architecture:** Correct the existing producers and consumers. Keep typed unknowns, exact identities and frozen evidence; add no entry-path RPC, lock or retry. Leave production state and historical results untouched.

**Tech Stack:** Python, SQLite, pytest, existing fake-provider fixtures.

**Spec:** `docs/audits/2026-10-09-engineering-followup-review.md` and its four area reports, plus the current contracts linked there.

## Constraints and edge cases

- N01 reaches funded admission: correct distinct-transaction evidence without changing thresholds or introducing synchronous dependencies. Measure local p50/p90.
- No process-global mutation, production DB access, service restart, deployment or authority change.
- Missing evidence stays unknown; genuine zero and valid empty pages remain distinct.
- Timeout, rejection, 429/ban, malformed and partial responses must not become measured absence.
- Bind chain/pool/mint/transaction identity; distinguish slot/time, token atomic quantities and USD conversion.
- Report denominator/censor counts; unresolved signed sends cannot become confirmed losses.
- Rollback is branch reversal. This repair work does not change running pinned releases.
- Historical regrading, provider incidence and live latency are outside this repair.

## Tasks

Each task: add the actual-function counterexample to repository tests, confirm its expected failure, apply the smallest repair, run positive controls and adjacent tests. Audit probes supply exact failing fixtures; do not import the temporary snapshot into permanent tests.

- [x] N01 — `memecoin_real_runner.py`: count distinct same-pool transaction identities, not swap-log rows; include duplicate/missing identity controls and local timing.
- [x] N02 — `memecoin_copy_trader.py`: forward saved transaction-count inputs and frozen own evidence into paper reconstruction; preserve unknown legacy evidence.
- [x] N03 — `memecoin_onchain_volume.py`: censor momentum when relevant explicit tape gaps intersect its required window; retain unrelated-gap controls.
- [x] EVM02 — `memecoin_v4_poolkey_index.py`: stop wanted-key reads for every refusal, including an empty exception message.
- [x] N12 — `relay_deposit_coverage.py`: stop the run after provider refusal rather than advancing to another transaction.
- [x] N13 — same script: accept only a valid `requests` list; malformed/null shapes remain unavailable, valid `[]` remains absence.
- [x] N04 — `sniper/services/memecoin_sol_cohort.py`: honor the canonical AMM target during graduation instead of rediscovering the completed curve.
- [x] N05 — same script: count only connected websocket time, preserving valid time before a later disconnect/error.
- [x] N06 — same script: use renewed retention consistently for cost projections, including reconnect/repeated sightings.
- [x] N07 — `db_memecoin.py`: select newest acceptable positive own-source mark within the age bound; unsafe-only marks stay unknown.
- [x] N08 — `s_landing_census.py`: validate pages and source ordering; report unknown/censored coverage separately from measured zero.
- [x] N09 — `s_landing_replay.py`: join the exact source-and-mint signal, including missing/mismatched controls.
- [x] N10 — same script: censor unsupported/unpriced quote units before building USD arms.
- [x] N11 — same script: preserve unresolved send outcomes and exclude unknown baselines from paired comparisons; retain confirmed-failure fee control.

## Integration verification

- [x] Review every diff and all red/green evidence; rerun the original audit counterexamples against the repaired source.
- [x] Run combined focused tests and schema-reference validation; broaden only for uncovered risk/failures.
- [x] Save a closure report mapping all 14 findings to repairs and checks, with runtime/history limits.
- [x] Preserve unrelated state and previous audit artifacts. A separate workflow parked the tracked repairs in `55acd7e8d`; this repair work made no commit.

Completion: 926 combined offline tests passed; independent review clear. Tracked source repairs are parked in `55acd7e8d` by a separate workflow; four new regression files and closure documents remain uncommitted. No deployment or historical regrading by this repair work. See `docs/audits/2026-10-09-engineering-followup-fixes.md`.
