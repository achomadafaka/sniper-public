# Solana chase cap — pre-registration (FROZEN, NOT STARTED)

Status: **frozen pre-registration. No config change is authorized by this document.**
Live value: `MEMECOIN_REAL_CHASE_MAX_PCT=30`, `MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT=30`.

**Freeze identity.** A commit cannot name its own hash, so the authority is a content manifest,
not a commit id. An early draft of this line claimed `19393f37`, which predates the protocol.

The manifest covers three artifacts, because the protocol's meaning lives as much in the code as
in this prose — the stop count, the terminal backstop, the permutation unit and the hypothesis
family are all code, and hashing only the spec would let them change mid-run:

| artifact | scope |
|---|---|
| this document | the FROZEN section only |
| `scripts/research/chase_cap_prospective.py` | whole file |
| `tests/test_chase_cap_prospective_analyzer.py` | whole file |

    verify: python3 scripts/research/verify_chase_cap_freeze.py

Digests live in `docs/superpowers/specs/2026-08-19-solana-chase-cap-freeze.json`; the roll-up is
stamped as `FREEZE_DIGEST` below. Any edit to any of the three after the cohort opens breaks the
manifest and invalidates the run.

## Part 1 — Why the 10% cap was withdrawn (settled, not part of the registration)

The Solana exact-quote chase gate was dead until 2026-08-19: `token_balance_snapshot` reads the
wallet's own token accounts, so a first buy reported `decimals=None`, the quote priced to `None`,
and the gate skipped itself. `terminal_status='skipped_chase'` never appeared once. Fills landed
+76.5%, +519% and +2895% above source under a nominal 30% cap. Fixed at `4912664e`.

On that sample the cap was lowered 30 → 10 and reverted. Reproduce with
`python3 scripts/research/chase_cap_drift_bands.py`.

**Scope error (decisive).** The sample pooled 190 Solana positions with 58 chain-4663 positions.
This key feeds two gates: the Solana exact-quote check in `solana_swap_executor.py`, and the
shared display-price check at `memecoin_real_runner.py:1133`, which is all EVM gets.

**Full ordered Holm step-down over all 8 scanned caps, α=0.05, 20000 shuffles, seed 7:**

| sample | n / mints | best cap | p | first-rank bar | caps surviving |
|---|---|---|---|---|---|
| solana | 190 / 167 | 20 | 0.0075 | 0.00625 | **none** |
| 4663 | 58 / 56 | 10 | 0.0137 | 0.00625 | **none** |
| pooled | 248 / 223 | 20 | 0.0029 | 0.00625 | 20, 25, 10, 12, 30, 8 |

Two corrections to earlier claims in this repo:

- An earlier note said "nothing clears Holm, not even the pooled 0.0072". Wrong. That used
  Bonferroni on a single p-value, not Holm. Under the real step-down the pooled sample **does**
  survive at rank 3. The withdrawal rests on scope, not on multiplicity.
- On Solana alone the best cap is **20**, not the 10 the pooled table suggested. The pooled
  analysis pointed at the wrong value as well as the wrong chain.

Also corrected: `source_drift_pct` is quote-to-source (`entry_attribution.py:82`), not
fill-to-source; the cap table splits positions while the permutation splits mints (7 Solana mints
straddle 10%, 0 on 4663); and the median drift of what a 10% cap keeps is 1.52% pooled / 3.08%
Solana, not the 10.48% full-sample median once quoted.

## Part 2 — Production status of the fix

The fix is no longer test-only. Attempt `5c8affad41c848e38f55ae46f2e0635e`, Solana, mint
`7YQ2W4HZCDogDjqQVz6q64KzTvjukfZhDEeXhfc9pump`, handle `hihi33`, reached
`chase_quote_confirmation` at 2026-08-19T18:59:22.427754Z: source `1.118550341310824e-05`, quote
`1.5219009304280597e-05`, gap **+36.06%** against a 15% cap, `terminal_status='refused'` before
submission. First chase refusal in the funded history. It fired under the withdrawn 15%
fresh-follow value, ~52 s before the rollback restart; at the restored 30 it still refuses.

**Unmeasured:** the `unpriceable_quote` stand-down rate. Post-deploy Solana attempts have produced
1 `chase_quote_confirmation` and 0 `chase_quote_skipped` events. One confirmation is not a rate.

---

# FROZEN — everything below is fixed before any data is collected

## Hypothesis

A Solana exact-quote chase cap tighter than 30% raises median realized PnL per funded position.

## Scope

Solana only (`memecoin_positions.network_id = 'solana'`), against the exact-quote gate in
`sniper/execution/solana_swap_executor.py`. Chain 4663 and the shared display gate at
`memecoin_real_runner.py:1133` are **out of scope** and need their own registration.

## Cap list (exactly these, no others)

Arms `[10, 15, 20, 25]`, control **30**. The control is not a hypothesis against itself, so the
family is **four**, and the first-rank Holm bar is α/4 = 0.0125 at α=0.05. An earlier draft gave
the control a synthetic p=1.0 and ran a five-test family; that is conservative rather than
wrong-in-our-favour, but a wrong bar is still a wrong bar.

**These caps are not selection-free and this document does not claim they are.** All eight of
5, 8, 10, 12, 15, 20, 25, 30 were searched retrospectively, so dropping 5, 8 and 12 removes three
searched values and keeps five others — including 20, which was the retrospective Solana winner.
An earlier draft implied the trim purged prior selection. It does not. The protection here comes
from two other places: the run is on a **fresh sample** that no search has touched, and Holm runs
over every registered arm rather than the winner. Reading the retrospective ranking as a prior for
which arm will win is exactly the error to avoid.

No cap above the control is admissible. The cohort runs at 30, so a looser arm cannot be
reconstructed — the attempts it would have admitted were refused and never priced.

## Cohort

- **Start instant:** the first funded Solana entry attempt with `observed_at` strictly after the
  operator sets the experiment cap. Record that exact ISO instant in this file before the run.
- **Eligibility:** `execution_mode='real'`, `network_id='solana'`, `origin_event_id IS NOT NULL`
  (excludes operator-opened positions), and an entry attempt whose trace contains either
  `chase_quote_confirmation` or `chase_quote_skipped`. An attempt that never reached the gate is
  **not** eligible and is not counted toward the stop.
- **Decision cap:** every enrolled attempt must record `cap_pct` equal to the control, 30. The
  arm reconstruction assumes the live admission set was the control's; an attempt decided at any
  other cap has a different admission set and no arm can be rebuilt from it. The analyzer refuses
  to read a cohort containing one.
- **Cohort closure:** the 120th enrolling attempt fixes the cohort. Attempts after it are out of
  cohort and reported separately, never analysed. Without this the sample keeps growing while it
  is read.
- **One row per eligible attempt**, including refusals. A refused attempt enters the ledger with
  realized PnL **0.00** and outcome `blocked`. Recording only fills would score the cap on the
  trades it allowed and silently drop the ones it stopped, which is the selection this design
  exists to prevent.
- Attempts that reach the gate but die later for an unrelated reason (route failure, insufficient
  balance, kill switch) are recorded with outcome `aborted` and excluded from the primary metric,
  with their count reported.

## Stop rule — enrollment

**Exactly 120 eligible attempts enrolled.** Not a range. No interim analysis, no peeking, no early
stop. Enrollment counts attempts in state `blocked`, `closed` or `open`; `aborted` attempts are
reported but never counted toward the stop. If the cohort has not enrolled 120 by 2026-10-19 the
run is declared underpowered and reported as such; the cap stays at 30 and no conclusion is drawn.

## Stop rule — terminal outcomes

Enrollment closing is **not** the same as outcomes being known: the 120th attempt can enrol while
earlier accepted positions are still open, and an open position has no realized PnL.

The cohort is readable only when every one of the 120 enrolled attempts is terminal — that is,
each is `blocked` (0.00 by protocol) or `closed` (PnL known). While any enrolled position is open
the analyzer refuses to print arm results and exits 2.

**Terminal backstop:** 14 days after the 120th enrollment. Any position still open at that instant
is force-marked censored, excluded from the primary metric, and its count and its arm membership
are reported in the result. A censored fraction above 10% of enrolled attempts invalidates the
primary read; the run is reported as censored-inconclusive and the cap stays at 30.

`--allow-open` prints a censored interim view for operations. Its output is labelled NOT a
registered result and must never be cited as one.

## Primary metric

Median realized PnL per eligible attempt, blocked attempts entered at 0.00, mint-clustered.
One pre-declared comparison per arm against the 30% control.

The policy is applied **per attempt**, by that attempt's own quote drift — which is what the live
gate does. The permutation applies the identical per-attempt rule and randomises only the pairing
of whole mints' drift vectors to mints (within equal-attempt-count buckets), so clustering
survives without changing the policy unit. An earlier draft assigned whole mints to a band by
their median drift, which evaluated a policy the arm does not implement wherever a mint had
attempts on both sides of the cap.

## Statistics

- **The registered analyzer is `scripts/research/chase_cap_prospective.py`**, pinned by
  `tests/test_chase_cap_prospective_analyzer.py`. It reads entry ATTEMPTS, enters blocked
  attempts at 0.00 PnL, refuses to read while any enrolled position is open, and compares the five
  policy arms against the 30% control.
- `scripts/research/chase_cap_drift_bands.py` is the RETROSPECTIVE tool and is **not** the
  registered analysis. It reads closed positions only, so it never sees a refused attempt and
  cannot enter one at 0.00; it scans eight caps, not the frozen five; and it compares low-drift
  mints against high-drift mints, which is not a policy-arm comparison. An earlier draft of this
  document cited it as the implementation. That was false.
- Mint-clustered permutation, 20000 shuffles, seed 7, add-one corrected.
- All five p-values are computed and reported. The ordered Holm step-down runs over all five;
  reporting only the smallest is the error this registration was written to prevent.
- α = 0.05. A cap is adopted only if it rejects under the full step-down.

## Secondary, reported but never decisive

Kept sum PnL, win rate, refusal count, median drift of kept fills, ex-best-2/ex-worst-2 mints,
and the `chase_quote_skipped` reason histogram.

## Pre-declared outcome

If no cap rejects, `MEMECOIN_REAL_CHASE_MAX_PCT` stays at 30 and this document is closed with a
null result. That is the expected outcome given the retrospective Solana p of 0.0075 at the best
of eight caps.

FREEZE_DIGEST = 00b00d5714c422ffe42b8163724f8acb25cc95e1524a513d610ad4b8a73aa761
