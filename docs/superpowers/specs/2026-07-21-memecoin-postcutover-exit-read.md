# Pre-registered read: post-cutover funded exit policy (SEALED 2026-07-21)

## Question

Under the funded-contract regime (per-band derisk/trail/stop policies, cutover
`MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT` = 2026-07-18T13:50:27Z), are the exit rails
net-positive on the coins the entry gates admit — and specifically, do stop-class exits
still dominate gross losses the way `b_stop` did in the pre-cutover book
(−$547 over 19 closes, avg ≈ −58%/hit, vs +$307 over 25 TP/trail winners in the 14d
window read on 2026-07-21)?

## Cohort

REAL closes with `funded_contract_json IS NOT NULL` (`execution_mode='real'`,
`is_open=0`). This is exactly the contract-governed cohort — manual pre-fix entries
(YAMINE) carry a backfilled contract and are included; pre-cutover closes are excluded
by construction.

## Read gate

**n ≥ 30 closes.** The reader script (`scripts/memecoin_postcutover_exit_read.py`)
refuses to print the decomposition before the gate and prints only progress
(`n/30`). No exit-knob changes on the funded runner before the gate fires, except
operator emergencies (catastrophe floor class).

## Sealed metrics (computed at the gate, in this order)

1. Total realized PnL; win rate; avg win; avg loss.
2. Per-exit-reason decomposition (n, total, avg) — stop-class = `b_stop`, `stop`,
   `catastrophe`; winner-class = `real_tp`, `real_trail`, `mcap_target`, `derisk`-
   completions; `mirror` and `manual` reported separately.
3. Sensitivity: drop-top-1 and drop-top-2 winners (does the verdict survive
   without the best fills?).
4. Stop-share: stop-class losses as a fraction of gross losses.

## Pre-registered actions

- **Stop-share > 55% of gross losses AND total PnL < 0** → draft a stop/derisk
  re-design proposal for the operator (tighter derisk-first recovery, or per-band
  stop displacement). Proposal only — no autonomous knob change.
- **Total PnL ≥ 0 with stop-share ≤ 55%** → rails hold; next lever is entry
  selection/latency, not exits. No change.
- **Anything ambiguous** (total ≈ 0, CI spans both) → keep accruing to n=50, re-read
  once. No peeking between 30 and 50.

## Disclosure (what was already seen when sealing)

- Cohort size n=28 and cohort total −$160.48 were seen on 2026-07-21 while sizing
  this read (2 closes short of the gate).
- The ALL-real 14d exit-reason decomposition (which overlaps but is not this cohort —
  it includes pre-cutover closes) was seen the same night and motivated the question.
- The cohort-specific decomposition, win rate, and sensitivity cuts have NOT been
  computed or seen.
