# SRE-F11/F12 — Selection-rule learner + bot-vs-human verdict

**Parent:** `2026-05-30-selection-reconstruction-engine-design.md`
**Purpose:** (F11) learn the simplest predicate that separates her picks from the matched skip-set, *within strata*; (F12) synthesize the whole battery into a calibrated, explained verdict + a route. This is the engine that replaces the execution-only classifier.

## F11 — selection-rule learner
Input: `sre_decision_matrix` (picked/skipped, features, groups), `sre_detection_results`.
**Interpretable-first** (we want the rule, not a black box):
1. **per-group single-threshold rules** — for each separating feature (T1), the best threshold by F1-score on picked-vs-skipped, with precision + **coverage** + stability (T7).
2. **rule lists / shallow trees** — depth ≤ 3 over features; CORELS/greedy rule-list so the output reads as `IF price∈[lo,hi] AND drift_6h<−x AND ttr<48h THEN enter`.
3. **conviction model** — separately, predict her *size* (T8) — the size-driving feature is her real signal even when entry-coverage is broad.
Score every candidate on: **precision** (entries satisfy it), **coverage** (predicate-matches she entered), **determinism** = min(precision,coverage), **stability** (holds out-of-time), **replicability_class** (self-contained features only? or needs external/price-stale data).
Output `sre_selection_rule`: the predicate(s) + scores + the features used + replicability_class + "residual" (entries the rule misses → unexplained judgment).

## F12 — verdict synthesizer
Combine into one calibrated verdict. Inputs & how they vote:

| Evidence | → algorithmic selection | → human selection |
|---|---|---|
| coverage/exhaustiveness (T3) | high & stable | low/selective |
| threshold sharpness (T4/determinism) | sharp, precision&coverage→1 | fuzzy, low determinism |
| new-market schedule (T5/T5b) | 24/7, dead-hours≈0, sub-min creation latency | waking-hours, irregular, slow |
| within-group separability (T1/F11) | a stable predicate separates | none separates (residual≈all) |
| stationarity (T7) | rule stable across time | drifts / adapts |
| edge locus (T2/T2b) | within-member edge a rule captures | edge is class-only or absent |
| external alignment (T9) | — | (orthogonal: flags external-intel) |

**Verdict states** (calibrated confidence, never binary-certain):
- `algorithmic_selection / self_contained` → rule found, self-contained features → **rebuild_rule** (deploy the learned predicate as an independent paper strategy; its live picks-vs-hers = the replication proof).
- `algorithmic_selection / external_intel` → entries explained only with external/cross-venue/news alignment → **flag_human_ai** (can't replay).
- `human_selection` → selective, no separating predicate, waking-hours, drifting → **copy_mirror** (copy live; can't rebuild judgment).
- `mixed` (e.g. SwissMiss: human selection + bot execution) → route on the SELECTION verdict (`copy_mirror`), record execution-automation separately.
- `undetermined` → insufficient support / fidelity → **collect_more** (more data / forward observation); never force a route.

Output `sre_verdict`: state, confidence, the evidence fragments that drove it (explainable), replicability_class, recommended route, and the learned rule (if any). Hard-walled display/decision-support — never a live trigger.

## Honest design rules
- **Falsifiability:** always report the residual + the strongest counter-evidence; a confident verdict must survive the opposite test.
- **No certainty claim:** a bot mimicking human irregularity is unfalsifiable; cap confidence and say so.
- **Forward is the arbiter:** even `rebuild_rule` only earns trust when its live picks overlap hers and its paper edge clears the price-implied null.

## Acceptance
1. SwissMiss → `mixed` (human_selection + bot execution), route `copy_mirror`, with the schedule + selectivity + no-separating-predicate evidence cited.
2. A synthetic exhaustive-threshold bot fixture → `algorithmic_selection/self_contained` with the exact predicate recovered (precision&coverage ≈ 1).
3. A synthetic news-reactive fixture → `external_intel` via T9, route `flag_human_ai`.
4. Every verdict lists residual + counter-evidence; sub-support cases → `undetermined`, never forced.
