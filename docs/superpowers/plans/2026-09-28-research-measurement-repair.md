# Research measurement repair implementation plan

Goal: deploy reliable fresh measurements with three frozen, falsifiable questions.
Architecture: reuse the existing two collectors, exact quote scheduler and experiment-specific SQLite ledgers. Add one small read-only thesis evaluator; expose its contract/status in the current workspace.
Spec: docs/superpowers/specs/2026-09-28-research-measurement-repair.md

- [x] Test and repair HTTP no-route typing, exact request binding, and primary outcome separation in fresh_recordings/fresh_lane_report.
- [x] Test and record fixed latest-20 buyer breadth plus independent pre-event OHLC coverage. Move source-buy feature reads off the quote scheduling loop.
- [x] Test all three frozen comparisons, uncertainty from infrastructure failures, paired policy identity, sealed holdout, and no legacy result leakage. Keep price returns and available-positive-exit distinct.
- [x] Replace permanent UI repair placeholders with live thesis/status/comparator/rejection rule, publish only valid results.
- [x] Run focused regressions and independent review; merge and pin release; calibrate, atomically activate, verify organic complete rows and funded continuity; deploy web and check authenticated endpoint.

Review focus: unknown != zero; no-route errors still require in-window exact identity; features cannot arrive after entry; no single-thread blocking of quotes; process restarts must not mutate the experiment definition.

Verification before activation: 235 focused tests passed; new failure regressions were demonstrated red before their fixes. Independent review findings (feature failures consuming identity, buyer-span definition, legacy PID coupling, malformed payloads, source timestamp leakage, and daily-equivalence cancellation) were corrected and rechecked. Node syntax and diff whitespace checks passed. Schema reference check has no violations; 80 existing external/unknown-table warnings remain.

Live verification: fresh-20260928-v5 activated September 28 at 11:23 EDT. Research release 7ee43b0c; web release a6d04c59. Organic exact-quantity 30/60-second pairs complete in both collectors. Authenticated workspace endpoint and served renderer verified. Two subsequent focused regressions bring coverage to 237 distinct passing checks. See docs/audits/2026-09-28-research-repair-live.md.
