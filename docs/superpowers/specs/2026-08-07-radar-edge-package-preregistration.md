# Radar edge package — preregistration record (2026-08-07)

Operator approved all items on 2026-08-07. This document records WHAT was preregistered and WHY,
so no later session can retune any of it on outcomes and call the result forward evidence. The
evidence basis is the three-agent hunt of 2026-08-07 (memory:
`finding-radar-edge-hunt-2026-08-07`), run after the realistic-v1 slippage-squared retraction
(`finding-radar-slippage-floor-is-not-a-fill-2026-08-07`).

## 1. The v6 exit is LOCKED: TP = +1,200 bps inside a 60-second window

`paper_lifecycle.py` already carries `TARGET_BPS = 1_200`, `EXIT_AGE_MS = 60_000`,
`STOP_BPS = -800`, haircut = measured funded p90 (122 bps/leg). This record adds the decision:
**TP at or below 800 is REJECTED** — on the retro-v6 cohort (n=132 launches / 121 creators) each
step down from 1,200 costs ~230 bps/alert (1200: +723 [613, 834]; 800: +497; 400: +232). Do not
lower the target to raise the hit rate; the hit rate is not the objective.

Known display hazard, accepted: the −500 admission floor plus the squared haircut (−244 bps) puts
the MEDIAN admitted entry at ≈ −744 bps at the first exit quote, so ~half of admitted entries
open within reach of the −800 stop. `stop_first` therefore partly re-measures admission-time
drag. The valid forward number is arm-minus-control (identical lifecycle both sides), never the
raw stop rate.

## 2. target-ladder-v3: cells above the v2 corner

`TARGET_BPS` gains 2,000 / 3,000 / 5,000. v2's EV was monotone to its top cell in every cut and
PROFIT_FIRST winners' max favorable excursion ran a median +4,007 bps — the optimum sits above
what v2 could see. v3 cells NEVER pool with v2 cells (evaluator_version split).

## 3. The funding gate matches at the published scale=2 band

`GATE_MATCH_SCALE = 2` (decision-age ±20 s, progress ±2,000 bps). At scale=1 the decision-age
band was the binding matching constraint (~2–3 pairs/day; 25-pair verdict 8–13 days out).
scale=2 was ALREADY a published `band_sensitivity` cell before promotion — this is not a
post-hoc widening. Unchanged: the arm predicate, outcome scoring, comparability covariates,
±100 bps modeled-cost band, and the 1 h pair-time bound. scale=1 stays published so narrowing
remains visible. The funding bar itself is unchanged: positive realistic returns AND positive
matched-control advantage at 25 / 50 / 100 pairs.

## 4. pump-creator-prior-v1 RETIRED

Evidence basis was retracted for target leakage on ship day; the clean-universe rescan confirms
the null (t = 0.5, n = 196 covered). The arm consumed the control's slot on ~half of
control-eligible launches and structurally excluded positive-prior creators from the control
cohort. The slot returns to `route-control-v2`. History keeps its rows; the `creator_prior_*`
features keep accruing as decision-time facts. Recorded in the frozen-registry test.

## 5. Anti-blitz ships as a labelled SPLIT, not an arm

`slow_capital_split` in the microscope payload: truth and control cohorts split at a FROZEN
threshold, `eligible_capital_velocity_atomic_per_s ≤ 3.5e9` (the clean-universe median). The
clean-universe scan found fast capital the strongest decision-time discriminator (acceleration
t = −5.9, velocity t = −5.2, LOCO-stable), but it attenuates 2–4× in the second half and does
not clear the bar inside v5-standard (t = −1.77). A split costs no control slots and adds no
multiple-testing burden to the funding gate. If the split separates on the forward v6 cohort at
its own 25-pair clock, arming it becomes a new operator decision.

## 6. Alert-lane outage pages

**2026-08-11 amendment:** The prior alert-count proxy did not test the quote path.

`scripts/radar_alert_lane_watchdog.sh` now reads direct quote-path status. It pages on a current
failure or a stale request. It keeps one outage latch and clears the latch after recovery.

## Explicitly rejected (do not revisit without new evidence)

- Creator-prior as a gate or feature input anywhere (leakage-prone; null when causal).
- Measured pair quality as a *predictor* (it is cost passthrough; +14 bps excess). It remains
  the −500 solvency floor, nothing more.
- Cashback or mayhem admission (measured pair −456 / −1,816 vs standard −448; v5 cashback
  TP-1200 EV +208 vs standard +564).
- Widening the −800 stop or replacing floors with expected amounts to improve results.
- Admission thresholds −400 (control-side cliff starves matching) and −600 (admits ~nothing new).
