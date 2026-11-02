# In-band m5 turnover floor — per-band value study (pre-registration)

Status: PRE-REGISTERED. The first run is complete and the verdict is NOT ESTIMABLE.
Owner: Claude (Beta-side analysis). Requested by Alpha, 2026-09-09.
Decision this study serves: what value the in-band m5 turnover FLOOR takes, per chain and per
`coin_band`. The floor stays at its current value until this study says otherwise.

Read-only. This document proposes no code change, no setting change, and no deploy.

## 1. Why this exists

Alpha and Beta debated a rolling relative floor and rejected it. Both converged on a STATIC
per-band floor, on the shape already deployed for the CEILING
(`cfg.inband_max_turnover_m5_for(chain, coin_band)`). Alpha refused to set band numbers by eye,
because the nano ceiling change of 2026-09-08 rests on 31 of 33 trades inside a nine-day window.
This document names the cohort, the cuts, and the kill criterion, so the numbers come from data.

## 2. Two configuration facts found while scoping this study

Both are verified. Both change what the study can claim.

**2.1 The per-chain floor is deployed but not configured.**
`cfg.inband_min_turnover_m5_for_chain` reads `MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5_<chain>` first,
then the global `MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5`. No per-chain key exists in `.env`. Only the
global key is set. So all four chains use one number, and the docstring statement "4663 wants 0.02
and Solana wants 0.10" is not in force. The per-chain mechanism is live and empty.

**2.2 The floor that decides is not the floor that is recorded.**
`memecoin_entry_attempts.gate_inputs_json` records `ratio_floor` from
`MEMECOIN_REAL_MIN_TURNOVER_M5`. That constant is the OVER-band leg. `memecoin_real_runner.py:2219`
supplies the in-band leg from `inband_min_turnover_m5_for_chain` instead, and that value is never
persisted. `_volume_active` is true only above `MEMECOIN_REAL_STRICT_VOLUME_MIN_MCAP_USD`, so every
nano, micro and small coin takes the in-band leg. A reader of `ratio_floor` therefore gets a number
that did not decide the refusal. Any future study must take the floor from configuration, not from
the ledger.

## 3. Cohort

Closed funded positions that carry an entry-time turnover and a measurable outcome.

    memecoin_positions p JOIN memecoin_entry_attempts a ON a.attempt_id = p.attempt_id
    p.execution_mode = 'real' AND p.is_open = 0
    p.realized_pnl IS NOT NULL AND p.entry_notional > 0
    a.classification_json IS NOT NULL
    AND (measurable_outcome_sql('p'))

`measurable_outcome_sql` is mandatory. It carries the partial-leg censor of 2026-08-29. A cohort
without it republishes evidence the paper report already discarded.

Turnover is `facts.volume_m5 / facts.effective_mcap`, read from `classification_json`.

**Do not use `facts.volume_mcap`.** It equals neither `volume_m5 / effective_mcap` nor
`volume_h1 / effective_mcap` on any of 201 sampled rows. It is a different quantity.

Band is `classification_json.coin_band`, the same field the deployed ceiling uses.

Observed cohort at first run: 636 positions, 2026-08-23 to 2026-09-09, 17 distinct days.

## 4. Pre-registered cuts

Chain in (`solana`, `4663`). Chains `56` and `8453` are excluded: they contribute 1 and 0
positions.

Band from `coin_band`: `nano`, `micro`, `small`. `mid` and `out_of_scope` are excluded as
report-only; each contributes fewer than 12 positions in total.

Turnover buckets, fixed before any value is proposed:

    <0.02    0.02-0.10    0.10-0.50    >=0.50

The floor's decision region is the first two buckets only. The last two are the reference.

Primary measure: stake-weighted return, `sum(realized_pnl) / sum(entry_notional)`.

## 5. Kill criterion — the bar a band number must clear

A per-band floor value may be proposed only when ALL of these hold for that chain and band:

1. n >= 20 in the decision bucket the value would refuse.
2. The bucket's sign survives leave-one-day-out, at every day.
3. The bucket's sign survives removal of the two best days by PnL.
4. The bucket's sign survives the time half-split.
5. The bucket's sign survives removal of the single largest absolute PnL row.
6. The response is MONOTONIC across the buckets. A floor asserts that lower turnover is worse. A
    non-monotonic response is not a floor, whatever its p-value.

If any condition fails, the band keeps the chain-level number. If a chain fails, the chain keeps
the global number. No value is set by eye at any level.

## 6. First run — result

Verdict: **NOT ESTIMABLE per band.** No chain-and-band cell reaches the n >= 20 bar in the decision
region. The largest such cell is n = 12.

Decision-region cell counts, `<0.02` bucket:

    4663   micro 12    small  9    nano  5
    solana micro 12    nano   9    small 8

At the chain level, pooled across bands, the picture is:

    chain 4663    <0.02       n=30  ret -9.1%   LOO-worst -11.2%  drop2best -9.3%  halves -5.1/-12.9
    chain 4663    0.02-0.10   n=36  ret +8.9%   LOO-worst  +4.9%  drop2best +0.1%  halves +8.1/+9.7
    chain solana  <0.01       n=25  ret +7.0%   LOO-worst  +1.3%  drop2best -1.4%  halves -4.7/+12.3
    chain solana  0.01-0.10   n=73  ret -2.8%   LOO-worst  -4.7%  drop2best -5.6%  halves +2.8/-7.2
    chain solana  >=0.10      n=400 ret +2.6%   (reference)

Two consequences:

* The 4663 sub-0.02 result is the ONLY robust one. It is negative under every perturbation. A 4663
  floor at 0.02 is supported.
* The solana permissive rationale is NOT supported. `<0.01` is positive in aggregate but flips sign
  on the half-split and on removal of the two best days. It also fails condition 6: solana returns
  are positive below 0.01, negative from 0.01 to 0.10, and positive again above 0.10. A floor
  cannot express that shape.

## 6b. What each candidate cutoff actually decides

Cell size is not evidence about a cutoff. A cell of 225 positions where only 22 sit below the
candidate answers the candidate with n=22. So the census below counts what each cutoff MOVES.

`HELPS` means the refused set loses money under every condition in section 5: leave-one-day-out at
every day, drop-two-best-days, both time halves, and drop-largest-absolute-row. `HURTS` means it
makes money under all of them. `flips` means the sign is not stable.

    chain/band     cellN  medTO   c=0.02              c=0.04
    solana micro     225  0.498   n=12 +24$ HURTS     n=22  +26$ flips
    solana nano      170  0.444   n=9  +86$ HURTS     n=15 +114$ HURTS
    solana small      81  0.472   n=8  -47$ flips     n=11  -68$ flips
    4663   micro      80  0.170   n=12 -48$ flips     n=21 +105$ HURTS
    4663   small      25  0.055   n=9 -104$ HELPS     n=10  -91$ HELPS
    4663   nano       19  0.077   n=5 -101$ flips     n=6  -102$ HELPS

Three results:

1. No cell that clears n>=30 contains a defensible cutoff. The two cells that show a robust `HELPS`
   are 4663 small (n=25) and 4663 nano (n=19), which the n>=30 bar excludes. The bar and the signal
   point at disjoint cells.
2. The live 0.04 is the harmful direction where it decides most. It refuses 15 solana nano trades
   worth +$114 and 21 4663 micro trades worth +$105, and both survive every perturbation as `HURTS`.
   This is not sufficient evidence to remove the floor — n=15 and n=21 are thin, and agreement
   across perturbations is weak at that size — but it is the opposite of the direction a floor
   assumes.
3. A median turnover gap between chains does not justify a floor VALUE. A floor acts on the left
   tail, and two distributions with medians 3-6x apart can share a left tail. The gap predicts
   ENGAGEMENT, and that holds: at 0.04 the floor touches 10% of solana micro and 26% of 4663 micro.

## 7. The sample is frozen, not growing

The in-band floor became reachable on 2026-09-08. Before that it was unreachable, so the historical
sample contains sub-floor fills — 24% of fills as late as 2026-09-08. With the floor live, sub-floor
entries stop. The decision region therefore stops accruing outcomes.

This kills the plan to re-run the study when n reaches 20. The censored region can only be measured
by an arm that deliberately enters below the floor. V6 live-paper is the existing isolated mock
mechanism for that, and it is the only honest route to a per-band number. A shadow that records
refused candidates cannot help: with no fill there is no PnL.

## 8. Recommendation

1. Keep the current global in-band floor. Do not set per-band values.
2. Decide whether the per-chain keys should be set at all, given finding 2.1. The 4663 evidence
   supports 0.02. The solana evidence supports nothing.
3. Fix finding 2.2 so the in-band floor is persisted with the refusal. Until then no study can
   reconstruct which floor decided.
4. If a per-band number is genuinely wanted, fund a V6 sub-floor sampling arm and re-run section 5
   against it. Otherwise close this question.
