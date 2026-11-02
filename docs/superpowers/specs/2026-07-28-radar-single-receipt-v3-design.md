# Radar Single-Receipt Confirmation v3 Design

**Status:** Approved on 2026-07-28. Implementation identity is
`pump-confirmation-truth-v3` with `pump-confirmation-delayed-v2` and
`confirmation-features-v2`.

**Supersedes:** The shadow-arm materialization, same-launch shadow comparison, and launch-mode
gain-share gate in `2026-07-28-radar-confirmation-microscope-design.md`. The economic-truth,
retention, evaluator, and capture contracts in that design remain authoritative.

## Goal

Measure whether executable Pump confirmation has forward paper value without manufacturing duplicate
entries from one receipt. Preserve every point-in-time condition as immutable evidence, compare each
condition with the launches it excludes, and require both Pump launch modes to replicate before the
system calls the direction promising.

This remains capture and paper research. It does not authorize funded entry, sizing, signing,
broadcast, reservation, or position changes.

## Evidence basis

The 2026-07-28 read-only retained snapshot contained 39 valid `pump-confirmation-truth-v2` outcomes:
24 reached +12% first, none reached -8% first, 15 exited at the time horizon, mean return was +17.59%,
and median return was +13.22%. Both cashback and standard-mode cohorts were positive. The independent
condition passed on 33 launches; its excluded complement contained only six.

The direction is encouraging, not established. The sample covers one UTC day, invalid or censored
receipts can hide tail loss, there are no real fills, and the exit policy has not been validated with
funded execution. The independent, clean, and low-friction v2 arms reused the same quote and entry
timestamp as truth-v2, so their paired return delta was structurally zero. Their overlap was a data
model property, not corroboration.

## Forward-only identity

The new primary rule is `pump-confirmation-truth-v3`. It uses the existing preregistered truth-v2
eligibility and the corrected `opportunity-path-v3` executable quote. It produces at most one primary
confirmation alert, paper entry, and outcome per launch.

The deployment instant is the forward boundary. Existing v1/v2 alerts and outcomes remain immutable
and visible as historical pilot evidence. They are never relabeled, rewritten, or pooled into the v3
validation headline.

`pump-confirmation-delayed-v2` remains a separate receipt because it uses a later chain event, a later
state fingerprint, and a later executable quote. It is paired to the v3 primary receipt and remains
measurement-only.

## One receipt, immutable condition labels

The v3 feature snapshot stores the existing point-in-time values needed to derive these labels:

- `independent_pass`: at least two new independent buyers in the current interval;
- `clean_pass`: `independent_pass`, sell pressure at or below 25%, non-worsening buyer HHI, and no
  creator sale;
- `low_friction_pass`: immediate round-trip quote drag no worse than eight times the effective
  one-way fee basis points;
- `launch_mode`: `cashback`, `mayhem`, or `standard` from the immutable launch facts.

The first three labels do not create alerts, entries, outcomes, remote quote requests, or due-horizon
computations. They are dimensions of the one v3 receipt. The label calculation is shared by capture
and projection so the UI never reinterprets a historical snapshot using mutable state.

The engine does not create `route-control-v1` for a launch that creates a v3 confirmation receipt.
The same-event route control has the same executable receipt and cannot measure action value. Route
control remains available for other registered candidate rules where the decision event differs.

## Evaluation

### Primary policy result

For v3, report valid, pending, invalid, and total receipts beside every headline. Hide a horizon rate
until that horizon has healthy coverage. Report outcome counts, cumulative return, mean, median,
range, and creator concentration. Never count censored, missing-coverage, or exit-failed rows as
scored.

### Condition separation

For each condition, split the v3 receipts into pass and excluded cohorts. Report the same metrics for
both plus:

- pass count and excluded count;
- difference in mean return;
- difference in median return;
- difference in +12%-first rate;
- overlap by definition: zero, because each receipt belongs to exactly one side.

This is descriptive cohort separation, not a randomized or causal estimate. Do not label the
condition useful until both sides have at least 25 valid outcomes. Do not add post-hoc age, progress,
or quote-drag thresholds from the v2 pilot to the v3 rule.

### Delayed comparison

Pair v3 primary and delayed-v2 only when both outcomes are valid. Report pair count, immediate wins,
delayed wins, ties, mean and median immediate-minus-delayed return, and coverage. Keep the verdict
`COLLECTING` before 25 valid pairs.

### Launch-mode replication

Cashback, mayhem, and standard are environments, not competing arms. A mode with no observations is
reported as absent. Every observed mode must independently reach 25 valid outcomes and positive
cumulative return before the overall direction can pass mode replication.

This replaces the 50% positive-gain share gate. Gain share punishes a genuinely stronger mode even
when the other mode is positive; replication asks the relevant question without dropping, quota
sampling, or slowing capture.

### Verdict contract

The v3 microscope uses these ordered statuses:

1. `COLLECTING`: fewer than 25 valid primary outcomes.
2. `EVIDENCE_BLOCKED`: cumulative primary return is non-positive, a fully sampled condition does not
   improve its excluded complement, an observed fully sampled mode is non-positive, required coverage
   is missing, or one creator contributes more than 50% of positive gain.
3. `SEPARATION_COLLECTING`: the primary checkpoint passed, but a required condition pass or excluded
   cohort has fewer than 25 valid outcomes.
4. `MODE_REPLICATION_COLLECTING`: an observed launch mode has fewer than 25 valid outcomes.
5. `PROMISING_DIRECTION`: a 25/50/100 primary checkpoint is reached; primary cumulative return is
   positive; every registered condition has at least 25 valid pass and excluded outcomes and improves
   its excluded complement in cumulative mean return; every observed mode has at least 25 valid
   outcomes and positive cumulative return; creator concentration is at most 50%; and required
   outcome coverage is complete.

`PROMISING_DIRECTION` means continue paper validation. It is not `ESTABLISHED_EDGE` and never grants
funded authority.

## UI contract

The current panel becomes two clearly separated sections:

- **Forward validation — v3:** one primary receipt, condition pass-versus-excluded tables, launch-mode
  replication, delayed comparison, coverage, and explicit blockers.
- **Historical pilot — v2:** immutable v2 truth and shadow-arm results labeled as overlapping pilot
  evidence. It cannot drive the v3 verdict.

Use `PAPER QUALIFIED`, not `FIRE NOW`. Place a small atomic live region on the text verdict only; the
animated plot is not live-announced. Bound recent receipts and DOM nodes under the existing limits.

## Performance and storage

- No new network call, RPC request, quote request, or event decode is added.
- A qualifying launch writes one primary receipt instead of truth, route-control, and up to three
  same-event shadow receipts.
- One delayed receipt is optional and bounded per primary receipt.
- Existing compact snapshots and evaluator retention remain authoritative.
- Insight aggregation is one bounded pass over retained evidence with no per-alert database lookup.
- The API keeps its 150 KiB representative-fixture ceiling and historical evidence cache.

The expected hot-path effect is fewer writes and less due-horizon work. Performance must be measured
before deployment; no claim is made from code shape alone.

## V6 and funded boundary

Radar must not import V6 services or funded execution modules. The current V6 admission contract
starts from an immutable realistic paper fill with approved source-ledger provenance; a Radar launch
receipt does not satisfy that contract. Creating a synthetic wallet, bypassing admission, or writing
the V6 ledger directly is prohibited.

If v3 later passes paper and human promotion gates, an adapter outside
`sniper.research.memecoin_opportunity` may translate an approved upstream paper fill through the
existing V6 admission interface. That adapter requires a separate design and forward validation.

## Failure handling

- Unsupported quote asset, incomplete block-scoped state, stale quote, missing 60/300-second
  coverage, persistent exit failure, and data gaps remain typed fail-closed outcomes.
- A failed label derivation makes that label unavailable; it does not create a false pass.
- Projection is additive and versioned. Old immutable rows are not repaired in place.
- If v3 projection fails, capture continues and the receipt remains retryable from durable evidence.

## Verification

Implementation is TDD-first and must prove:

- one qualifying event creates exactly one v3 primary alert and no same-event route control;
- condition labels match the immutable feature snapshot and create no extra quote work;
- delayed-v2 is the only paired later-entry arm;
- invalid/censored outcomes never enter scored aggregates;
- pass-versus-excluded, mode replication, creator concentration, and ordered statuses are exact at
  24/25/50/100 boundaries;
- v2 remains visible but cannot change the v3 verdict;
- the UI uses bounded rendering, correct language, and a verdict-only live region;
- focused tests, the full Radar/retention suite, schema validation, JavaScript syntax, whitespace,
  payload size, read latency, pinned runtime identity, process count, health, and live API all pass.

No Playwright, external API call, production database write, funded-path change, or strategy
promotion is required by this design.

## Knowledge sync

Knowledge delta: new paper-evidence architecture. Same-event confirmation subsets are immutable
labels, not corroborating entries; mode replication replaces gain-share concentration. Funded and V6
doctrine is unchanged. `AGENTS.md` routes future Radar work to this spec.
