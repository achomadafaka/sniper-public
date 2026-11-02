# radar-buyer-breadth-v1 — preregistration record (2026-08-19)

Operator approved the label and this contract on 2026-08-19. The operator did NOT approve a
Radar restart, a funded bridge, or any change to funded authority.

This document records WHAT is frozen and WHY, so no later session can retune it on outcomes and
call the result forward evidence. It follows the pattern of
`docs/superpowers/specs/2026-08-07-radar-edge-package-preregistration.md`.

## 0. The status of the evidence that motivated this

`finding-radar-buyer-breadth-descriptive-2026-08-19` is **PROMISING_RETROSPECTIVE_DIRECTION**. It
is NOT a gate pass. Three facts set that status.

1. The buyer threshold came from terciles of the same frozen corpus that then scored it. The
   hypothesis was in-sample and the evaluation was in-sample.
2. The funding bar is a conjunction: "positive realistic returns AND positive matched-control
   advantage at 25 / 50 / 100 pairs". Only the matched half ran, and it ran retrospectively.
3. The existing V6 gate seals through `seal_gate_checkpoints`. That receipt does NOT implement the
   breadth gate. A batch run of the matcher over frozen rows is not a gate either.
   **CORRECTED 2026-08-20:** this line first said
   `op_insight_gate_checkpoints` holds 0 rows. It holds one checkpoint, sealed
   `2026-08-19T22:47:20Z` at scale 2 on 25 pairs, for `pump-confirmation-truth-v6`. That is a V6
   checkpoint. It is NOT a breadth checkpoint. No breadth checkpoint exists.

The retrospective numbers, for the record. The corpus held 4,624 route-control launches over
15.16 days. The test used one row per launch and held `rule_version` constant. At
`GATE_MATCH_SCALE=2`, buyers >=9 gave 293 pairs and 260 creators. The mean was +1,821 bps. The
creator-clustered interval was [1504, 2139]. The like-for-like hit advantage against
`radar_detection_evidence.py` is P(MFE >= 1,200) = **+0.246**. The first draft quoted
P(terminal > 0) = +0.276, which is a different statistic. Residual imbalance survives the
calipers. The arm side runs +957 bps higher in `progress_bps`. It also runs 2.53 s older.

## 1. The label is frozen

`sniper/research/memecoin_opportunity/breadth_label.py`:

- `RULE_VERSION = "radar-buyer-breadth-v1"`
- `PASS_MIN_BUYERS = 9`
- `CONTROL_MAX_BUYERS = 3`
- classes: `pass` (>=9), `middle` (4-8), `control` (<=3)
- `LABELLED_VENUE_ID = "pumpfun_solana"`
- an unknown `eligible_buyer_count` writes an explicit `censored` row with a reason

A change to any of these needs a NEW `rule_version` and its own clock. `tests/test_radar_breadth_label.py`
asserts the constants so a silent retune fails the suite.

## 2. The label is not an arm

`radar-buyer-breadth-v1` appends nothing to `arms` in `engine.py`. It takes no slot, so arm
selection is unchanged. The label covers every launch that reaches the decision point, including
the launches where no control arm is built.

**Why this is the load-bearing decision.** An arm on `buyer_count >= 9` would take the control's
slot. It would take that slot on the launches under test. The control cohort would then hold only
low-breadth launches. Slot allocation would manufacture the comparison. `pump-creator-prior-v1`
was retired for that failure, and `engine.py` records it. The historical +1,821 bps result used
`route-control-v2` rows on both sides.

**The label runs BEFORE arm selection.** The engine builds no control arm when a confirmation or
an adaptive arm fires. Those arms need a high buyer count: `directional.py` requires 10 or more
buyers. A label bound to the control receipt therefore dropped most `pass` rows. The engine writes
the label once per launch, ahead of arm selection.

**One venue per rule version.** `LABELLED_VENUE_ID = "pumpfun_solana"`.
`docs/superpowers/specs/2026-08-07-radar-venue-scoring-design.md` forbids a shared control across
venues. Bags and Pons need their own rule versions and their own clocks.

## 3. Storage

- `op_breadth_labels` in the hot store, schema 16, one row per `launch_id`, first write wins,
  immutable by trigger, and OUTSIDE the retention delete set.
- `op_insight_breadth_labels` in the insights store, schema 10, projected on maturity.
- A censored launch stores a row, never a gap. An absent row is ambiguous: it could mean an
  unknown count, an old schema, or a lost write. The gate must measure censor coverage, so the
  censor is a stored fact.
- The forward gate READS the stored class. It must never re-derive a class from `feature_json`,
  because a re-derived threshold is a threshold that can move.

## 4. Frozen gates and clocks

<!-- schema:external=op_insight_breadth_labels -->

1. Matched results at `GATE_MATCH_SCALE=2` only, at 25, 50, and 100 pairs.
2. `standard` and `cashback` reported SEPARATELY. **No pooled promotion result across launch
   modes.** The retrospective split was cashback +2,166 bps on 202 pairs against standard +1,089
   on 89. v6 is standard-only, so the cohort that would trade this shows about half the effect.
3. Positive matched advantage with the creator-clustered interval strictly above zero.
4. Standard-mode ABSOLUTE net return above the frozen round-trip cost floor. The matched number is
   a difference and cannot establish profitability on its own.
5. No velocity veto, no creator filter, no entity-count entry rule. None has separate forward
   proof, and the matched result was measured on buyer count alone.
6. Entity count stays a SHADOW measurement. `entity_buyer_count` stays NULL until point-in-time
   edges exist. A co-occurrence map built over a whole corpus looks ahead, which is the same class
   of leak that retracted the creator prior.
7. One hit definition, fixed before the clock starts: P(MFE >= 1,200 bps), matching
   `radar_detection_evidence.py:152`.
8. Pump.fun Solana only. A result from one venue cannot satisfy another venue gate.
9. Censor coverage reported with every result. A cohort whose censored share moves is not
   comparable across windows.

Gate 2 uses `radar-buyer-breadth-gate-v2`. Its clock starts at
`2026-08-21T00:00:00Z`. The source contract was frozen before that instant.

Gate 4 keeps the `2026-08-20T13:55:02.296629Z` realistic-lifecycle clock. The source correction
does not change that independent cohort.

**Measurement capability. Found 2026-08-20, after the original clock.**

**CORRECTED 2026-08-20.** The first version of this note said the cashback half of gate 2 is
UNAVAILABLE because the paper adapter fills no cashback request. That is wrong. It confused two
different evidence paths. The corrected statement follows.

**1. The RAW matched data for gate 2 does not depend on paper fills.** It comes from matched v4
alert outcomes, not realistic lifecycle fills. Cashback accrues it now. At `2026-08-20T14:45Z` the
post-clock cashback rows are 11 `pass` and 7 `control`.

**1b. The SEAL does depend on paper fills, and it is not a breadth seal.** `seal_gate_checkpoints`
calls `_realistic_gate_evidence`, which joins `op_paper_outcomes` to `op_paper_fill_requests` and
keeps only `FORWARD_CONFIRMATION_RULE` and `route-control-v2` rows. That path is V6-keyed and
standard-only. So "cashback gate 2 is available" is true of the raw data and FALSE of the receipt.
**The breadth gate now has an outcome source and an immutable receipt.**

- The source is `op_insight_alerts@all-rules@alert-outcome-v4@launch-union-v1`.
- The source groups rows by `launch_id`. It never filters on `rule_version`.
- `engine.py` emits at least one arm after each breadth label.
- All arms from one decision use the same economic receipt.
- Duplicate alerts must agree on their decision time, quote, entry, status, creator, features,
  and outcome.
- A missing alert, missing outcome, invalid JSON, or conflicting receipt is outcome-censored.
- The matcher uses `launch_id` as its identity. It uses the label time and label covariates.
- The receipt table is `op_insight_breadth_checkpoints`. Update and delete triggers make it
  immutable.

**`route-control-v2` stays DISQUALIFIED.** `engine.py` emits it only when no confirmation or
adaptive arm exists. Those arms require an effective buyer count of 10 or more. Breadth `pass`
starts at 9. The rule filter therefore removes rows based on the tested class.

The one-in-five rule runs after alert creation. It cannot restore a missing control alert. The
old standard split was 40.0% pass censor against 1.9% control censor.

The all-rule source was selected after the first Gate 2 window became visible. That window is
calibration only. It cannot satisfy a Gate 2 checkpoint. The new clock prevents that reuse.

The receipt records two separate censor rates with frozen denominators. It never combines them.
A combined rate can hide a change of cause.

      label_censor_rate(mode)         = censored labels / all labels
      outcome_censor_rate(mode,class) = missing valid outcomes / pass-or-control labels

A censored launch KEEPS a row. `breadth_label.py` assigns `breadth_class = "censored"` with a
`censor_reason`. The launch stays countable in the label denominator. An ABSENT row is never a
label censor. It is a lost launch and an error to find.

Report `outcome_censor_rate` separately for `pass` and `control`. EXCLUDE `middle`. It enters no
pair, so it cannot censor one. A scored row that stays unmatched is NOT censored. The matcher
rejected it on a caliper. Gate 1 governs that caliper.

Gate 9 requires a comparable censored share across windows. One pooled rate can stay flat while
the two causes move in opposite directions. Gate 9 would then miss the failure.

**2. The paper adapter constrains gate 4, not gate 2.** Gate 4 requires a standard-mode ABSOLUTE
return, and only standard requests fill. That limit is real, and it is confined to gate 4.

**3. The formal breadth evaluator is available for BOTH modes.** `breadth_scorer.py` reads the
stored labels and the all-rule v4 source. It reports standard and cashback separately. It can seal
the first receipt only after the new Gate 2 clock reaches 25 matched pairs.

**4. The cashback fill stop is diagnosed, and it is deliberate.** v6 requires
`launch_mode == "standard"` (`directional.py`, `CONFIRMATION_V6_LAUNCH_MODE`), and the matched
paper control rejects non-standard modes on pump.fun (`engine.py`). Commit `9295374c` added both
rules on 2026-08-07. The cashback fill requests belonged to v5. v6 and its control are
standard-only by design. The commit records the reason. The measured round trip refutes the
cheap-tier belief. The median is -456 bps for cashback against -448 bps for standard, over
5,026 alerts. The last cashback fill request is `2026-08-07T05:07:45.167375Z`, and `mayhem` never
filled at all.

**5. The gap probability is WITHDRAWN.** An earlier note priced a 78-minute cashback silence as
unlikely. Fill requests are not a homogeneous Poisson process, so that arithmetic does not apply.
The control admits one deterministic row in five. Post-clock evidence carried no v6 alert and only
two standard control candidates. Zero requests is not surprising from that denominator.

One consequence stands unchanged. Cashback carries the pooled retrospective headline of +1,821
bps. Cashback ran +2,166 bps on 202 pairs, against standard at +1,089 bps on 89. Gate 4 is standard-only,
so the mode that must clear the absolute-return bar is the weaker one.

Do NOT open the fill path to `cashback` to repair this. Cashback needs no repair for gate 2, and
opening it would change the forward cohort while the clock runs.


## 5. Sequencing — item 6 of the roadmap is BLOCKED

`radar-realistic-v3` cannot be activated today. `activate_realistic_v3` raises
`BaselineAgeNotPreregistered` while `MAX_BASELINE_AGE_MS is None`. The module also states a
second rule. Any sampler window recorded before the v3 collection change "measures the old load
and cannot set `MAX_BASELINE_AGE_MS`". The sealed v1 window is therefore disqualified.

**CORRECTED 2026-08-19.** Step 3 below originally read "Radar runs at least 24 hours so the
paper sampler v2 window closes under v3-shaped load". That step cannot happen. The
`paper-sampler-v2` window SEALED on 2026-08-12. `paper_sampler.py` states that a sealed
window never reopens. `persistence_due` returns False once `_sealed` is set, and `restore`
carries the flag back. **Waiting produces nothing.** A second measurement needs a new sampler
IDENTITY. That IDENTITY is a `detail_version` which does not match the stored one. It returns a
fresh sampler on a fresh 24-hour window.

The sealed v2 window already reports a verdict:

```
VERDICT: ACTIVATE
eligible: 38
coverage_bps: 9211
p90_ms: 14032
max_baseline_age_ms: 15000
```

So the operator has TWO valid choices, and this record does not pick one:

**A. Accept the sealed v2 window.** Preregister `MAX_BASELINE_AGE_MS = 15000` from the numbers
above and activate under separate authority. The measurement is real and already taken. Its
limit is that it was measured before the 2026-08-19 cutover, so it does not describe the
current release's load.

**B. Require post-cutover evidence.** A fresh window under the current release.

The order matters and an earlier draft of this record had it BACKWARDS. It said to measure the
window and then write the contract against it. That is not preregistration. The author would write the
selection rule with the distribution already visible. The number could then be chosen to suit
what was seen. The rule must be frozen while the answer is still unknown.

1. Freeze the new sampler IDENTITY and the SELECTION RULE. The rule names the percentile, the
   eligibility filter, and the rounding, and it is committed before any new data exists.
2. Deploy the new sampler identity.
3. Collect the 24-hour window. Do not look at the distribution to choose the rule.
4. Run the frozen rule over the window and derive the integer.
5. Preregister the derived integer.
6. Activate v3 under separate authority.

Slower by a day. The number then describes the system that will actually run, and it was not
selected after the fact.

The buyer-breadth threshold itself failed this same discipline. The value 9 came from terciles
of the corpus that then scored it. That is why the result is retrospective, and not a gate pass.

The order after that choice is unchanged:

1. This label change lands. (DONE 2026-08-19.)
2. Radar restarted and capture enabled. (DONE 2026-08-19.)
3. Choose A or B above and obtain a `MAX_BASELINE_AGE_MS` value.
4. Operator preregisters it, never from an outcome.
5. `activate_realistic_v3` opens the cohort: target +1,200 against the cash entry, stop -800
   against the on-time 5 s baseline, censor on a late or absent baseline.
6. The forward breadth clock starts HERE. Labels accruing before it are pre-clock.

## 6. Out of scope for this change

The funded queue, signer, reservations, funded database, and funded authority are untouched.
The label is paper research. It never signs, submits, reserves, or informs funded decisions.

At a future stage 4, a canary would need a Radar-specific authority flag and a position-specific
exit policy. It must NOT inherit authority from `MEMECOIN_REAL_AUTO_EXITS_ENABLED=1`. That stage
needs its own operator approval and is not authorized by this record.
