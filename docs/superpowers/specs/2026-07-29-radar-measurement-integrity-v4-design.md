# Radar Measurement Integrity v4 Design

**Status:** Approved on 2026-07-29. The user authorized implementation, testing, merge, push,
deployment, and live verification without intermediate approval pauses.

## Goal

Make Radar outcomes valid for forward research without adding network load or touching funded
execution. The primary result is net return per eligible alert, including time exits. Hit rate is
diagnostic only.

## Verified defects

- A new alert has no entry-time executable observation. Its first later chain event can decide the
  outcome, while quiet launches first appear at the horizon. This makes observation timing depend on
  the path being measured.
- Resolved-only hit rates discard time exits. The current Confirmation arm has more time exits than
  its route control, so resolved-only comparison overstates separation.
- Jupiter entry and exit calls are paced. The remote round trip therefore combines modeled route
  cost with price movement during the request. It is not a pure friction measurement.
- `independent_buyer_count` is an alias for the filtered eligible-buyer set, not an entity-clustered
  independence measure.
- Historical Confirmation v3 mostly lacks a same-launch control. Manufacturing a same-event copy is
  not a valid control and would duplicate the same receipt.
- Equal elapsed times are ordered by a content hash, which is deterministic but not temporal.

## Forward-only identities

- rule: `pump-confirmation-truth-v4`
- control: `route-control-v2`
- feature snapshot: `confirmation-features-v3`
- entry quote: `local_curve_math_at_inclusion-v2`
- path: `opportunity-path-v4`
- outcome evaluator: `opportunity-outcome-v3`
- policy result evaluator: `alert-outcome-v4`
- Jupiter receipt: `jupiter_quote_v3`

Existing rows remain immutable and retain their original versions. V1-V3 statistics are historical
measurement pilots and cannot be pooled with v4.

**Amended 2026-07-30 after live verification.** The rule version alone cannot separate the regimes:
`pump-ignition-v1`, `pump-clean-start-v1`, and `pump-confirmation-v1` kept their names across the
cutover and were being reported pooled (ignition +1515 bps pre-v4 against +69 bps post-v4 on the
same rule key). `evaluator_version` is the discriminator — `alert-outcome-v4` is the forward regime.
Every arm-level statistic is split on it; the pre-v4 side is published under an explicit
`Withdrawn pre-v4 measurement pilots` label and never in the forward result.

`pump-curve-break-v1` is **retired**, not ported. Its band required
`immediate_round_trip_return_bps` in [-3500, -1200); under v4 that field is the modeled round-trip
cost, which for the $25 primary clip is ~[-300, -190]. The band became unreachable at the cutover
and the arm has emitted nothing since. Its original band was a two-second price move, not a cost,
and it never had supporting evidence, so it is removed rather than restated in move units.

The modeled-cost bound the remaining arms carry (`-1200 <= cost <= 0`, and `>= -(8 x fee)` for the
low-friction label) is **non-binding at the $25 clip** and is retained only as a fail-closed guard.
It must not be described as a cost filter. Making cost discriminative requires the capacity ladder
($5/$10/$50 entries), which is out of scope here.

## Honest sampling

Do not synthesize a one-second grid. Pump curve state changes on trades; repeated carry-forward rows
would add no information and could hide a source gap.

The elapsed-zero baseline is keyed on the decision snapshot id. A synthetic identity produced a
second elapsed-zero row when the same snapshot later reached `record_launch_outcomes`, and because
both rows share `observed_at` the ordering key fell through to content-hash order — the exact defect
this design set out to remove. Every v4 entry carried the duplicate; some pairs disagreed (-250 vs
-254).

At the local decision time when the completed route receipt is consumed, materialize the paper entry
from that exact captured finalized state. The remote pair latency is already part of the decision delay;
do not add it a second time. In the same transaction, append an executable baseline exit observation at
elapsed zero using the exact local acquired quantity and curve state. Append later observations only
when captured state changes, plus the existing exact horizon computation. A source gap censors the path.

Outcome ordering is `(elapsed_ms, observed_at, canonical_state_id)`. `observed_at` resolves same-
millisecond ordering; the canonical ID is only the final stable tiebreaker.

## Quote semantics

The remote Jupiter pair is route-coverage and timing evidence. It does not define the v4 paper entry.
For each completion, compute from the latest local state:

- `modeled_round_trip_cost_bps`: exact current-state local entry plus immediate exit;
- `observed_pair_return_bps`: Jupiter expected-output round trip;
- `observed_pair_move_bps`: observed pair return minus modeled round-trip cost;
- `decision_age_ms`: local decision time minus launch time;
- `chain_state_age_ms`: local decision time minus the captured state time.

The v4 low-friction gate uses modeled cost, never the paced remote pair return. The persisted alert
receipt must carry the same value: `persist_state_evaluation` previously created the receipt for any
firing verdict from the raw probe, so the arm's stored `quote_drag_bps` was the paced pair while its
control's was the modeled cost — the same launch and snapshot reporting -4625 and -250. The drag the
rule decided on is now passed explicitly from both call sites. A route receipt whose
state fingerprint has advanced remains bounded route-coverage evidence, but never supplies the paper
quantity or price; the entry is recomputed from the exact current local state and the mismatch is
persisted. Unsupported quote assets fail closed.

Jupiter v3 receipts retain both `outAmount` (expected output after fees, before slippage) and
`otherAmountThreshold` (minimum output after slippage). Conservative route-floor fields use the latter.
Malformed responses and ordinary provider errors become typed failures; process-control exceptions are
not swallowed.

## Controls and reporting

`route-control-v2` is disjoint from truth v4 and reuses existing eligible completions; it creates no
extra RPC work. Historical same-event copied controls remain excluded.

Match truth to controls one-to-one without replacement within preregistered hard bands: same launch
mode and quote mint, decision time within one hour, decision age within ten seconds, curve progress
within 1,000 bps, and modeled cost within 100 bps. Report unmatched counts and overlap explicitly.

The matched advantage is published against band width, never as a single number.
`matched_control.band_sensitivity` reports pairs and mean advantage at the preregistered band and at
2x/4x the age and progress tolerances. Only the preregistered cell (`scale: 1`) may gate
`PROMISING_DIRECTION`; the widened cells exist so a point estimate that moves when the band moves is
read as selection noise rather than as a result.

> **AMENDED 2026-08-07 (operator approved):** the gate cell is now `scale: 2` (decision age ±20 s,
> curve progress ±2,000 bps). At `scale: 1` the decision-age band was the binding matching
> constraint (~2–3 pairs/day, a 25-pair verdict 8–13 days out); `scale: 2` was a published
> `band_sensitivity` cell from the day this spec shipped, so the promotion is not a post-hoc
> widening. All other bands, the covariates, and the outcome scoring are unchanged, and the
> narrower cell stays published. See
> `2026-08-07-radar-edge-package-preregistration.md` §3 — that record governs on this point.

The authoritative statistic is mean terminal net return over every scored alert. Time exits remain in
the denominator. Report median, win/loss/time-exit counts, invalid/censored counts, and target-hit rate
as supporting diagnostics. Creator-cluster bootstrap confidence intervals preserve within-creator
dependence. `PROMISING_DIRECTION` requires at least 25 matched pairs, positive cumulative return,
positive mean paired advantage with a lower 95% bound above zero, and no fragile creator or mode
dependency. Raw gain share is diagnostic, not an automatic veto: creator concentration is accepted only
when leave-one-creator-out advantage stays positive; mode concentration becomes
`MODE_SPECIFIC_DIRECTION` only when that mode has at least 25 matched pairs and its creator-clustered
lower bound is positive. It cannot become a global direction until other modes replicate. Otherwise
report `LEARNING`, `INSUFFICIENT_MATCHED_CONTROL`, or `CONCENTRATION_BLOCKED`; never hardcode a best
arm.

## Naming and coverage

Forward snapshots use `eligible_buyer_count`, `eligible_buyer_hhi_bps`, and
`new_eligible_buyers`. Historical alias fields remain readable but are not described as independent
confirmation.

**Amended 2026-07-30.** Renaming only the persisted snapshot fields left the decision path reading
`independent_buyer_count` / `independent_buyer_hhi_bps` — literal duplicates of the `effective_*`
values — so the rules still asserted a property no code computes (`op_entity_edges` is empty). The
aliases are deleted at the source: `state_derived_features` emits `effective_*` once,
`point_in_time_features` emits `new_eligible_buyers` and `eligible_capital_velocity_atomic_per_s`,
and the rules read those. Values are unchanged, so this is a rename, not a regime change. Rule
version strings (`pump-confirmation-independent-v1`) and the persisted label keys
(`confirmation_independent_pass`) keep their names because they are immutable identities; the
operator-facing copy now reads "New eligible buyers rising".

`LaunchState.independent_capital_in_atomic` and its siblings keep their names: they are persisted
dataclass fields inside `state_json` and renaming them would break restore of historical snapshots.

Bags coverage says `forward_sample_verified` only when all launches first seen during the current
audit window were checked. The UI shows `audited / forward-seen`; it does not imply total venue
coverage. Event age is telemetry. Silence alone does not mark a quiet venue unhealthy; explicit gaps do.

## Arm freeze and the open levers (added 2026-07-30)

Nine arms have now been measured against their control and none separated. The registry
is frozen at four live firing arms (`pump-clean-start-v1`, `pump-ignition-v1`,
`pump-confirmation-v1`, `pump-confirmation-truth-v4`) plus `route-control-v2`, pinned by
a test. Adding an arm is permitted but must be a recorded decision: each one costs
multiple-testing burden and restarts its own matched-control clock, which fills at a few
pairs per hour.

Three measurements now published on every read, because a number nobody derives is a
number nobody reads:

- `exit_realism` — a -800 bps stop realises a median **-2,436**. A bonding curve
  reprices only on a trade, so an exit lands at the next trade, not at the barrier.
  There is no stop, only a label. Losses, not take-profit levels, are the exit problem.
- `decision_latency` — 9,852 ms median decision age: 2,588 ms (26.3%) plumbing, all of
  it the remote quote round trip; 7,264 ms the arm waiting for its own bands. Neither is
  free to cut — the quote rejects most candidates, and the bands are strategy.
- `creator_prior` — **RETRACTED 2026-07-30 (target leakage).** The original +1,453 bps
  ordered priors by alert time only, so a prior alert whose outcome resolved *after* the
  current decision still counted. Over a third of pairs were built on results that had
  not happened yet. Requiring the prior outcome to have resolved first leaves
  **+392 bps at t=+0.70 over 193 pairs — no effect.** The replication on the pre-v4
  cohort carried the same leak and is not independent evidence. Now computed causally.

The full target x horizon exit ladder is persisted again. It had been narrowed at the v3
cutover to the two headline cells — (1200 bps, 60s) and (1200 bps, 300s) — not one cell as
first written, which still leaves no take-profit question answerable. Of the pre-v4
alerts, 2,743 carry all 30 cells and 412 carry 20; "3,155 with the full grid" was wrong.
Storage-only; the board payload drops `targets` in compaction.

## Creator-prior arm (added 2026-07-30, operator-approved)

`pump-creator-prior-v1` was added on evidence that has since been **retracted for target
leakage** (see above). Its forward store is causal by construction — `op_creator_outcomes`
is credited when an outcome finalizes — so the arm itself does not leak. But it is an
**untested hypothesis with a zero clock, not an evidence-backed arm**, and must not be
described as one. Whether to keep or retire it is an operator decision. Registry currently
five live firing arms.

**Predicate, preregistered exactly as measured:** the permissive route base (eligible,
local exit, verified remote entry and sell route) plus at least one scored prior outcome
for the launch's creator and a positive mean. The split was discovered on the sign of the
mean and is forward-tested on the sign of the mean — there is no threshold to tune, which
is the point.

**Disjoint from `route-control-v2`.** It takes the control's slot, never a slot beside it:
emitting both on one launch would produce two receipts from one decision with identical
outcomes, which is not a control. It is paired against control rows from other launches by
the existing matched-control machinery. Confirmation keeps precedence so its accruing
evidence is untouched, and candidate arms are unaffected.

**Causality is structural, not enforced by a filter.** `op_creator_outcomes` is credited
inside `append_competing_outcome`, so a creator's record only advances when an outcome
finalizes — which is strictly after the decision that created it. A launch can never be in
its own prior. Censored and reorg outcomes are excluded: they describe our capture, not the
creator. The table is chain-scoped and deliberately outside the retention delete set —
launches and outcomes prune within minutes, and a prior built from them would prune with
them.

**Expected coverage:** ~40% of alerts had a prior record in the study, so the arm fires on
a minority of the permissive population and the control keeps the rest.

Its matched-control clock starts from zero. Nothing about this arm makes the Radar
actionable: it is `research_only`, and the global status stays exploratory until it clears
the same matched-control gate as everything else.

## Confirmation v5 (added 2026-07-30, operator-approved forward experiment)

`pump-confirmation-truth-v5` **replaces** v4 as the firing confirmation arm, raising the breadth
gate from 5 to 10 eligible buyers. v4 is not mutated: it keeps its history and stops firing, because
changing a live rule in place silently redefines what its accrued evidence meant.

Why breadth and not size. In the permissive control population, buyers >=10 ran +660 against -259
(t=+3.68) while curve progress >=50% alone did not separate (t=+1.50). Few-buyer launches were far
more concentrated (median HHI 5,061 bps against 1,132), and the worst cell in the whole cohort was
large-but-narrow — progress >=50% with fewer than 10 buyers, at -916.

**It did not clear the gate retrospectively.** Restricting v4 to buyers >=10 gave 26 matched pairs
at +206 bps with a creator interval of [-324, +807], decaying to +137 and +49 as the band widens.
So v5 is a preregistered forward experiment on a fresh clock, not a validated improvement, and its
matched-control count starts at zero.

The reporter now judges v5 as the forward primary and leaves v4 readable as history. Note the
matcher names its forward arm explicitly (`_confirmation_microscope_v4`); any future version needs
that wiring changed deliberately rather than inherited.

One correction from the same review: the velocity-over-size result first reported as "-789 bps,
47% loss" was computed over 261 of 467 rows, because the feature key was renamed mid-cohort
(`independent_capital_velocity_atomic_per_s` -> `eligible_capital_velocity_atomic_per_s`).
Coalescing both immutable names covers all 467 and gives -687 bps at a 40.2% loss rate. The
direction survives; the headline did not.

## Runtime and scope

- No additional RPC calls, quote concurrency, deep queue, or polling cadence.
- No entity clustering, new arms, capacity ladder, new venue, funded import, signing, reservation,
  position, or strategy-promotion change.
- All new work is bounded by existing launch retention and compact insight projection.
- Radar remains diagnostic research until v4 reaches the matched-control evidence gate.

## Verification

- TDD regressions for baseline materialization, event/horizon ordering, time-exit inclusion, matched
  controls, creator-block intervals, quote decomposition, Jupiter floors, typed failures, aliases, and
  Bags wording.
- Focused Radar tests, full opportunity/Radar selection, schema validation, Python compilation,
  JavaScript syntax, whitespace checks, and production-shaped temporary-DB recovery checks.
- Deploy only from merged main using the opportunity and web pinned-runtime scripts. Verify manifests,
  process ownership/count, heartbeats, health, source gaps, storage, quote queue, and logs.
