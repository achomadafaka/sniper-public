# Pre-registration — does a genuine arming-bar pass predict a better funded outcome?

**Date sealed:** 2026-08-26 · **Author:** Gamma · **Review:** Alpha · **Status:** sealed before
further data accumulates. Read-only measurement. No sizing, gate, or roster change is authorised by
this document.

## Why this exists

`docs/audits/2026-08-26-memecoin-arming-bar-audit.md` found that the funded arming bar is bypassed
on 308 of 329 arms since 2026-08-01, and that 11 of 14 currently armed wallets fail it. The only
reading that argued *for* the bar was 2 arm episodes that passed it on its own terms, both positive,
against a −$5.78 median first-5 for the 15 that failed. **n=2 is an anecdote.** This document seals
the forward count before more data exists, because sealing afterwards is worthless.

## Hypothesis

H1: arm episodes admitted by a genuine gate pass (`forced=0`) deliver a better funded outcome over
their first closes than arm episodes admitted by operator override (`forced=1`).

H0: they do not differ.

## Population

Every arm episode on Solana whose opening event is written on or after **2026-08-27T00:00:00+00:00**
(the day after sealing), from `memecoin_wallet_approvals`:

- `action IN ('chain_arm','approve')`, `chain='solana' OR chain IS NULL`. Chains are never pooled.
- Arm episodes are rebuilt as intervals: an arm opens an interval, the next
  `chain_revoke`/`revoke` on the same scope closes it.
- **Treatment arm** = `forced=0`. **Control arm** = `forced=1` opened in the same window.
- One episode per (wallet, interval). A re-arm after a revoke is a new episode.

Base rate, for feasibility: genuine passes have run at **0.82/day** since 2026-08-01 and 1.24/day
since 08-15 (`memecoin_wallet_approvals`, same predicate). **20 unarmed, non-excluded wallets clear
the bar today**, so the treatment pool is not empty.

## Metric

Per episode, over its **first 5 funded closes** inside the interval
(`memecoin_positions`, `execution_mode='real'`, `network_id='solana'`, `is_open=0`, ordered by
`closed_at`):

- **Primary:** median of `realized_pnl / entry_notional` across those 5 closes — size-neutral, so a
  bankroll clip that drifts with equity cannot drive the result. This is the lesson of the Simpson
  reversal recorded in `finding/memecoin-arming-bar-metric-is-near-zero-2026-08-26`.
- **Secondary:** summed `realized_pnl` over the same 5 closes, reported with its median alongside so
  a single outlier cannot be read as an effect.
- Episodes that never reach 5 closes are **censored, not dropped**, and reported with their count
  and the reason the interval ended.

## Comparison and test

Treatment versus concurrent control, first-5 median return. **Day-clustered permutation** on the arm
date, 20,000 draws, two-sided. Arms cluster heavily on a few days — the audit's 17 episodes fell on 5
distinct days — so a plain label shuffle overstates significance and must not be the headline test.

Historical baseline, for context only and **not** pooled into the test: 2 forced=0 episodes
(+$22.64, +$8.22) against 15 forced=1 episodes at −$5.78 median first-5.

## Read date and stopping rule

- **Minimum:** 8 treatment episodes that reach 5 closes.
- **First read:** the earlier of 8 treatment episodes or **2026-09-17** (3 weeks).
- **No peeking.** If the count is short at the read date, report it as short and set one extension to
  2026-10-08. One extension only.
- **Stop early only for harm:** if 4 consecutive treatment episodes each close 5 trades at a negative
  median return, report immediately. This is a safety exit, not a result.

## What result justifies what action

| Outcome at the read | Action |
|---|---|
| Treatment better, day-clustered p < 0.05 | The bar earns enforcement. Propose reducing override frequency. Operator decides. |
| Treatment better, p ≥ 0.05, direction consistent | Not proven. Extend once, or accept as weak support. No config change. |
| No difference | The bar as configured does not select. Its enforcement cost is not justified; revisit the aggregate (below) before discarding the concept. |
| Treatment worse | The bar is actively harmful as configured. Escalate immediately. |

**No result of this test authorises a sizing change.** Sizing is the operator's call and is out of
scope here.

## A known confound, pre-registered rather than discovered later

The bar being tested contains a defect the audit already measured: its PnL term is a `SUM`, so it
ranks lottery tickets. Four of the six highest-scoring wallets have a **negative median paper close**.
`jack7offsuit` clears the bar today at 14 closes / +$6,988.55 on a 21% win rate and a −$47.69 median,
and it lost 78% of its funded notional when armed.

So a null or negative result is **ambiguous** between "a pass carries no signal" and "the SUM
aggregate carries no signal". To let that be separated without running a second experiment, record
at arm time, for every episode in both arms:

- `alpha_b_copy_stats` closes and SUM PnL (the gate's own snapshot, already persisted on the
  approvals row as `copy_closes` / `copy_pnl`);
- the **median** honest paper PnL per close, and the **median paper return**, computed over the same
  predicate at that instant.

At the read, the treatment arm can then be split by whether the wallet also had a positive paper
median. That split is **exploratory and labelled as such** — it is not a second primary test.

## Threats to validity, stated now

1. **Operator selection.** Which wallets get armed at all is a human choice; only the `forced` flag
   is quasi-random. This measures the pass conditional on the operator wanting the wallet.
2. **Small n, day-clustered.** Both handled by the permutation test and the honest read date.
3. **Config drift.** Any change to entry gates, exit rails, or sizing inside the window is recorded
   with its date, and the read reports whether the window crossed one. It does not silently pool.
4. **Survivorship in the control.** A `forced=1` arm on a wallet with prior funded history is
   outcome-conditioned; the audit found this. Report prior funded closes per episode so the
   comparison can be restricted to zero-prior-history episodes if the imbalance is large.

## Instruments

`memecoin_wallet_approvals` (arm trail, gate snapshot at decision time);
`memecoin_positions` (funded outcomes); `alpha_b_copy_stats` at
`sniper/persistence/db_memecoin.py:2280` with `MEMECOIN_REAL_PROMOTE_MIN_CLOSES` /
`MEMECOIN_REAL_PROMOTE_MIN_PNL_USD` from `sniper/settings/memecoin.py`. SQLite time filters use
Python-built ISO cutoffs, never `datetime('now')`.

<!-- schema:external=memecoin_wallet_approvals -->
