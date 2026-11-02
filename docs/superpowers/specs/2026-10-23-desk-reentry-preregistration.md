# Desk re-entry measurement: pre-registration (Gamma, 2026-10-23)

Frozen BEFORE any post-exit path is computed. Operator idea (via Alpha): after a desk exit keep watching the coin for a possible re-entry. This is MEASUREMENT ONLY: no build, no live change, no cohort change. Script: `scripts/research/desk_reentry_probe.py` (read-only). Builds on the Part 1 PASS-pool labels and the proven-virtual-quote pool price (`memecoin_desk_ps_tape`).

## Population and exits
- Tickets: the Part 1 label table (PASS-pool, UTC 2026-10-22 and 10-23). Entry = curve spot at PASS+8 s. Simulated exit = the first of stop (-18%), trail (12% off the post-entry peak, armed after the first tick above entry), TP (+30%), or TIMECAP at entry+300 s; rails 30/18/12 or the vet verdict's own. Exit price = the path price at that moment (spot, no impact or fees, same convention as Part 1).
- Post-exit window: 600 s after the exit. While the coin is on the curve the path is the curve tape; if it graduates inside the window the path continues on the canonical pool using only priced swaps (proven virtual quote), only for tickets on/after the PS capture start (06:57:02Z 10-23). A graduating coin before that, or a pool gap, is `censored` (counted, never filled with the curve spot). Tickets whose exit leaves less than 600 s of tape are `censored:tape_end`.
- Strata (reported separately, never pooled): exit type (stop | trail | TP | timecap), graduated-in-window vs not, day.

## Reads
- **R1 recovery:** for each stratum, share of exits whose price later reaches exit x (1+X) within 600 s for X = 10, 20, 30%, and the median time to first reach; also the share that reaches exit x 0.90 (keeps falling) first. Wilson 95% on shares, mint-cluster bootstrap on means.
- **R2 re-entry rule (frozen, one re-entry per coin, first signal only, within the 600 s window):**
  - R_struct (primary): signal at the first second t where the price is a NEW 60 s high (strictly above every price in the previous 60 s, window excluding the exit second) AND buy SOL volume in the last 10 s >= 2 x sell SOL volume in the last 10 s AND >= 3 distinct buyers in those 10 s AND the price is not below exit x 0.97 (no falling knife).
  - R_reclaim (secondary): signal at the first t after the price has traded below the exit price where it prints >= exit price x 1.02.
  - Re-entry fills at the first priced print at or after signal + 2 s; the same rails 30/18/12 apply from the re-entry; hold horizon 300 s or the end of the 600 s window; a re-entry that cannot complete its horizon inside the window is graded to the window end (flagged).
  - **Net of the second round trip:** reported gross and after subtracting 2.5 percentage points (two legs at ~1.25% fee, no impact; sensitivity 4.0).
- **R3 outputs:** per stratum: number of re-entry signals, mean/median re-entry return gross and net with 90% mint-cluster bootstrap, win rate, comparison with the same coins' FIRST-entry return and with a random re-entry control (same coins, signal replaced by a random second in the window, 200 draws, seed 23).
- **R4 the real desk closes:** the desk positions in the ledger (signal_source='desk'), descriptive only: realized pnl, exit reason, 600 s post-exit path from the tape (and pool for graduated), whether R_struct / R_reclaim would have fired. n is tiny; no inference.

## Decision rules (frozen)
1. A re-entry feature is worth a build proposal only if, in the stratum with >= 25 non-censored signals, the NET mean re-entry return has a 90% CI lower bound above 0 AND exceeds the random-signal control, and the same sign holds on both days.
2. Stop-out strata are judged separately from trail/TP strata. If only one stratum qualifies the proposal is limited to it.
3. Anything else: 'no measured edge at this n', with the numbers. Any build is the operator's and needs: the funded-leg 24 h re-entry cooldown and losing-coin block retuned for the desk, the universe/ticket cooldowns bypassed for the rule, and the prior trade on the JUDGE sheet (Beta's finding, new cohort).
4. Biases named: spot grading excludes impact and fees except the stated 2.5 pp; hindsight-free signal definition but the rails are the model-free defaults, not the desk's actual RISK behaviour; PASS-pool exits are simulated exits, not our fills; one day of pool tape.
