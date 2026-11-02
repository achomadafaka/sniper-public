# Desk v28 policy and compatibility glue implementation plan

> **For agentic workers:** Use the assigned isolated worktree and explicit file ownership. Run TDD for code changes; preserve other workers' edits. Each task carries its own focused checks and independent review.

**Goal:** expose frozen partial-flow/chart evidence to the paper JUDGE under a new hash-bound v28 cohort, without downgrading PS safety or changing funded behavior.

**Architecture:** Delta owns versioned facts, structure and loss classification. Codex owns the playbook, policy/provisioning and version/binding glue. The existing recorder, paper kernel and RISK tape retain their strict PS path for both supported facts versions. No new provider, scheduler or execution engine.

**Tech stack:** existing Python/SQLite/Codex subscription transport and pinned runtime scripts.

**Spec:** `docs/audits/2026-10-27-three-feed-desk-inputs-v28-preregistration.md` at executed freeze `45895eb569bece68c527cb6438944f5530b1013e`, containing original `6a97dfe59` and Amendments1–3. Gamma approved citing the latest executed commit in policy; the launch record also lists all four SHAs.

## Global constraints and edge cases

- Paper only. Model remains GPT-6 Luna/low/codex_cli/subscription_flat; preserve25USD/5positions/40SOL/40calls-per-hour, existing12s deadline, rails, halt/kill and output-cap refusal. No funded, money, LI.FI, `.env` or database-backup action.
- Delta API: `facts_for(..., version='pumpswap-facts-v1')`; v28 explicitly requests `pumpswap-facts-v2`. V1 output and existing policy/CID bytes remain unchanged.
- V2 totals use all causal usable seen swaps in `[cut-60,cut)`, `lower_bound:true`. `covered_s` is the proven-complete suffix. Seen totals survive zero suffix; no seen swaps or unknown tape gives null. Do not extrapolate uncovered time or claim direction from missing sells.
- Loss classes are disjoint, `loss_classes_exclusive:true`: distinct(signature,cause), legacy gap once by gap row identity; never count an attested batch and its members twice. Missing is not zero.
- Chart remains24five-second eight-column bars plus aligned `chart_coverage`; observed partial bars remain visible and uncovered demand is never zero-filled. Price/breakout/rails uncertainty stays conservative.
- Unknown safety is never clean. Partial zero observed sells must not become proven False; positive observed sell proves True, complete zero may be False. Existing pre-dispatch safety/freshness refusals stay intact.
- No new synchronous provider calls, mutable process-global configuration or locks on funded filling. New digest computation is local to the paper JUDGE.
- Startup/dispatch prompt mismatch, unsupported facts versions, malformed hashes, missing data and model/context spoofing fail closed before a model/admission claim where applicable. Earlier interrupted calls are not restamped/rejudged.
- New cohort registration must be idempotent in both existing stores; preview writes nothing. The specific v27 `--confirm` exception does not authorize a new v28 registration script. Prepare concrete reviewed command before requesting its operator execution/exception.
- Cutover quiesces/drains the old primary; preserves positions, clocks, cursors and cooldowns. No mixed primary cohorts. Keep rollback code and original records. The separate Universe diagnostic release is not semantic authority for v28.

## Review focus

Zero suffix with observed trades; missing/partial zero sells; v1/v2 cross-version input or position stamps; non-six-decimal exact quantities; tampered prompt/model metadata and recovered older calls. Each is covered by the task that owns its boundary.

## Task1 — playbook (Codex root)

Files: new `docs/superpowers/specs/2026-10-27-desk-judge-playbook-v28.md`.

- [ ] Start from v27; change only version, input/coverage interpretation and partial-chart wording. Preserve seven-key JSON and all other rules/bounds/context.
- [ ] State exact Amendment3 interpretation, disjoint receipt counts, missing-vs-zero and no extrapolation/unknown direction. Partial evidence alone never forces SKIP; null tape/flow remains blind.
- [ ] Document SHA meaning: exact UTF-8 system text below the existing `\n---\n` delimiter, matching actual `system_sha256`.
- [ ] Freeze the final playbook before provisioning preview; independent review compares old and new semantics.

## Task2 — policy, JUDGE and recording glue (assigned worker)

Owned source: `scripts/memecoin_desk_provision_cohort.py`, `sniper/services/memecoin_desk_contract.py`, `sniper/services/memecoin_desk.py`, `sniper/services/memecoin_desk_judge.py`, `scripts/memecoin_desk_judge_worker.py`; new focused v28 tests. Do not edit Delta's files or the paper/RISK worker's files.

Interfaces: extend `frozen_policy()` with optional `ps_inputs_version` and `judge_playbook_sha256`; default behavior stays byte-identical. Existing `--ps-inputs --playbook <v28>` selects facts-v2 from the explicit prompt version and hashes that actual system prompt; no extra CLI grammar. Canonical policy stores `judge_playbook_sha256`; prompt id remains the existing cohort-wrapper prompt version. `validate_ps_inputs(policy,prompt_version)` accepts only the strict v27/v1 and v28/v2 pairings, with v28 digest required.

- [ ] RED: old v27 policy/CID unchanged; new preview facts-v2+digest+executed freeze yields new identity with no writes; crossed versions and malformed/missing hashes refuse.
- [ ] GREEN: implement minimal builder/validator/version routing; use existing canonical ledger/cohort digest as call `policy_hash`, not the inner-policy hash. Verify its helper/encoding against current code.
- [ ] RED: wrong loaded system prompt refuses before claim/model; spoofed trusted prompt/policy/release metadata cannot survive either new save branch; prior-call recovery stays unchanged.
- [ ] GREEN: local prompt digest enforcement and trusted new v28 metadata; pass frozen facts version to Delta API. Preserve cap/capacity/refusal behavior and existing safety gates.
- [ ] RED/GREEN: partial zero sells→None, positive observed sells→True, complete zero→False; actual serialized sheet contains v2flow/counters/chart_coverage, including zero-suffix seen evidence, and its compressed bytes/hash match.
- [ ] Run the existing affected contract/provisioner/JUDGE/recording tests unchanged plus new tests with `-n 2`; report exact RED/GREEN and reviewed diff. No model/provider/runtime/production DB action.

## Task3 — paper and RISK compatibility (separate assigned worker; Beta review)

Owned source: `sniper/services/memecoin_desk_paper.py`, `sniper/services/memecoin_desk_risk_tape.py`; new compatibility tests only. Alpha/Beta temporarily handed these seams to Codex for this release.

- [ ] RED: v28 keeps the strict PS entry/capture/mark/exit/RISK quantity path and stamps the actual frozen facts ID; non-six-decimal quantities remain exact. Crossed v1/v2 identities refuse with a typed reason, no legacy fallback.
- [ ] GREEN: change version/binding detection only. A missing version on an integrity-mode PS row keeps conservative v1 handling; do not change unrelated legacy venue policy. Preserve fill/mark/cut/PROVEN_ZERO/unknown-keeps-prior behavior and Delta's prior paper fixes.
- [ ] Run existing paper/RISK integrity tests unchanged plus new tests with `-n 2`. Beta reviews exact source SHA; no economics/safety changes.

## Task4 — integration and operator gate (Codex root)

- [ ] Combine reviewed Codex pieces, Delta versioned facts and the then-current main/diagnostic release, preserving Gamma amendments and source ancestry; verify all ownership seams and final prompt digest.
- [ ] Run covering/treewalk gates under the shared full-suite lock where broad, always `-n 2`; obtain independent exact combined interaction review, including real serialized partial facts→new policy→paper/RISK path.
- [ ] Stage exact immutable release with no backup and no funded activation; retain-lock it. Produce pinned read-only provisioning preview and verify new canonical identity/policy in neither store before registration.
- [ ] Prepare the exact new registration script. Operator executes or explicitly grants the required exception for that reviewed script; do not generalize the v27 exception.
- [ ] After confirmed registration, independently compare both stores, drain old primary work, cut over all necessary paper roles with explicit Desk1+executionpaper pairing, and verify zero new funded intents/funded continuity.
- [ ] Send Gamma the first actual v28 call's stored release/prompt/policy identities and verified sheet hash. Record partial flow/chart as shown, not profitability or complete-flow proof. Keep natural lifecycle proof separate.

## Review correction — paper RISK row isolation

Beta's exact `e47ea59d7` review passed with one P2: an unsupported stored cohort version raised inside `risk_positions`, aborting the entire paper RISK pass. Successor `0c831e5db5a4bff5f9cf5708171ad52596b85250` catches only that typed per-row error and retains the position with an unknown version marker, which the existing tape reader blinds before any proof read. Other positions remain visible; entry still refuses the unsupported version. SQL failures and unrelated malformed JSON remain pass-level errors. No economic, quantity, quote or funded path changes. Mixed-cohort regressions failed first in both supported versions, then 53 focused tests passed; independent source follow-up passed. Final combined validation and activation remain separate gates.
