# Fresh research recordings implementation

Approved: operator approved `output/research/lanes-audit-20260928/fresh-recording-contract.md` on 2026-09-28. Execute in this isolated checkout. Radar/V6 excluded. No funded writer changes.

- [x] Add failing behavioral tests for exact quote identity/completion deadlines, fresh provenance, sample/restart isolation, tape coverage, and sealed reporting.
- [x] Add a research-only manifest/store and deadline scheduler. One exact observation per quantity/horizon, decimal-text atomic amounts, short process-owned SQLite transactions, bounded quote workers. Freeze all settings and label files. Preserve skipped evaluations and typed unknowns.
- [x] Reuse scanner feature evaluation/watch discovery with explicit unmatched/source-failure states. Frozen pre-entry features plus separately timestamped later features; Golden/Conviction are unvalidated labels. Replace duplicated paper polling in fresh mode.
- [x] Route impulse through the same quote recorder; require observed pre-window coverage for quiet labels. Keep its universe separate. Fresh cursors, no historical recovery.
- [x] Add active-epoch readers, coverage-first comparison, discovery/holdout read locks and freeze artifact. Old roster/scalp consumers must refuse mixing old results.
- [x] Prepare clone/refusal reporting boundaries using current immutable provenance; no producer or funded policy mutation. Unknown provenance stays excluded.
- [x] Verify focused tests, schema refs, and bounded calibration with p50/p90 RTT and deadline/budget accounting. Failed feasibility leaves prepared state.
- [x] Commit the isolated release, switch only research watchdogs, collect producer acknowledgements, activate atomically, inspect fresh rows and funded continuity. Preserve historical DBs and rollback pointers.

Edge cases: late completions, partial payloads, wrong mint/amount, zero vs missing, provider timeout/auth/rate limit, source staleness, clock jumps, restart and duplicate identities, same mint across lanes/chains, old closes after activation, configuration mismatch, partial activation and sealed holdout leakage. No inference of actual fills, between-sample stops/peaks, reliable single-coin prediction, or funded promotion.

Verification: `python3 -m pytest tests/test_fresh_research_recordings.py tests/test_runner_scanner_*.py -q`; report CLI against temporary fixture DBs and prepared live manifest; raw request/completion timestamps checked after activation. A fixture is not live feasibility proof.

Completed: fresh-20260928-v4 activated 2026-09-28T12:04:00.277554Z. 147 focused tests passed. Calibration and organic primary timing/quantity/provenance checks passed; early holdout and historical consumers refused reads. Runtime receipts and remaining evidence limitations are in `output/research/lanes-audit-20260928/activation.md`.
