# Pre-registration: the turnover x source-size entry conjunction

Locked at: 2026-09-08T18:48:16Z. Author: Beta, at Alpha's request.

**Status: SUPERSEDED, same day.** The operator priced the cost of waiting against the cost of
being wrong and chose to switch the gate on directly, without a shadow period. See
`2026-09-08-turnover-source-conjunction-kill-criterion.md` for the replacement control: a
pre-committed kill criterion, checked forward, instead of a pre-committed go criterion checked
before activation. The shadow code this document was written to gate
(`turnover_source_conjunction_v1` in `sniper/services/memecoin_entry_shadow.py`) still runs and is
still harmless telemetry; it is simply no longer the thing that decides whether the gate goes live.

Status: pre-registration. This document sets the forward test before forward data exists. Do not
edit the decision rule after forward data starts to arrive. A later change is a new
pre-registration. Mark it as a new pre-registration, not an amendment.

This document changes no funded behavior. It activates no funded gate. It sets the rule that
decides whether the design in `2026-09-08-turnover-source-funded-setup-design.md` (superseded), or
any successor gate, may go live on real capital.

## 1. Why this document exists

`docs/audits/2026-09-08-entry-knob-winner-evidence.md` found a conjunction. Raw five-minute
turnover must sit in `[10%, 50%)`. The source buy must sit in `[$1,000, $10,000)`. In the studied
sample, matching trades returned +22.13% over 39 trades. Control trades returned near zero or
negative.

Alpha does not trust this number yet. The audit itself gives the reason: *"These intervals do not
correct the prior rule search... Neither period is an untouched holdout."* The rule search found
the rule inside the same data that scores it. A number computed that way cannot show that the rule
will work forward. It only describes what worked in the searched sample.

Only one test tells the difference. Score the rule on data the search never touched. Decide the
scoring rule before that data exists. This document is that scoring rule.

## 2. The rule under test

Identity: `turnover_source_conjunction_v1`. Implementation: `sniper/services/memecoin_entry_shadow.py`
(`_band_shadow`, `_conjunction_shadow`, `_research_candidates`). Tests:
`tests/test_memecoin_entry_shadow.py`.

A funded entry attempt matches the rule when all these conditions hold. The inputs are the same
inputs the funded runner already resolves for its own gates. The rule uses no second data path.

- `network_id` is `solana` or `4663`. `signal_source` is not `manual`.
- `0.10 <= raw_m5_volume_usd / effective_mcap_usd < 0.50`. The rule always uses raw volume, never
  organic volume. The floor and the ceiling share the same numerator. The live volume gate does
  not share a numerator between its floor and its ceiling; this rule is deliberately different.
- `1000 <= source_size_usd < 10000`.
- Both conditions must resolve to a known, finite value. A missing input produces `unknown`. The
  rule never guesses `would_allow` or `would_block` from a missing input.

This rule only observes. It runs inside the existing `funded_entry_shadow_v1` substage. That
substage records its result AFTER the real entry decision. It cannot change that decision
(`_record_entry_shadows_best_effort`, `sniper/services/memecoin_real_runner.py:6336`). Every
funded entry keeps its current admission rules. This rule filters, sizes, and blocks nothing.

## 3. What counts as forward data

The clock starts at the first deploy of this shadow extension to the pinned funded release. An
observation with a `completed_at` before that deploy timestamp is NOT forward data. This holds
even if a later reconstruction computes that observation retroactively. A retroactive observation
comes from the window the original search already used. It carries the same contamination.

**The deploying agent must record the deploy timestamp in section 8.** Record the release SHA and
the activation time. Do this before anyone reads a forward result in section 4. Before that record
exists, no one may read forward results. No one may add the record after already reading results.

## 4. The decision rule, fixed now

Compare two forward-only populations. Draw both populations from the same post-deploy window. This
way, neither population can hide a regime change or a bankroll change that the other population
saw.

- **Candidate**: closed, measurable, strict-copy funded positions on `solana` or `4663`. Each
  position's entry attempt must show `turnover_source_conjunction_v1.state == "would_allow"`.
- **Control**: closed, measurable, strict-copy funded positions on the same two chains, in the
  same window. Each position's entry attempt must show
  `turnover_source_conjunction_v1.state == "would_block"`. This is an explicit rejection by the
  rule. An `unknown` or `not_applicable` row belongs to neither population. The original audit
  excluded manual and off-chain signal sources the same way.

**Answer GO only when every one of these five rules holds:**

1. Candidate n is at least 30 closed positions. The original study's population held 39 trades.
   This forward sample must carry comparable weight. The forward sample carries the entire
   evidentiary burden that the historical sample cannot carry.
2. The candidate's stake-weighted return is positive. Stake-weighted return means total realized
   PnL divided by total entry notional.
3. Build a bootstrap 95% confidence interval on the gap between the candidate return and the
   control return. Resample by TRADER, not by trade. The original study's own bootstrap intervals
   used trader resampling. Per-trade resampling lets one prolific trader inflate the interval; that
   exact failure sank the source-size half of the original interaction. The interval must exclude
   zero, on the positive side.
4. Remove the single largest-PnL trader from the candidate population. Recompute rules 1 and 2.
   The point estimate must stay positive. The original audit already ran this same sensitivity
   check and reported it.
5. Split the observation window into two halves by time. Cross that split against the two chains.
   This gives four cells: {solana, first half}, {solana, second half}, {4663, first half},
   {4663, second half}. At least two of the four cells must hold 5 or more candidate trades each.
   A result concentrated in one chain or one half of the window does not count as a GO. The
   original audit flagged this same weakness in its own two-trade and four-trade cells.

**Answer NO-GO when any one of these four conditions holds, regardless of the other numbers:**

- 45 days pass after the section 8 deploy timestamp, and candidate n has not reached 30. The
  measured historical base rate supports this ceiling: 79 attempts matched both bands across 8
  days and 10,797 scored attempts (chains 4663 and solana, 2026-09-01 through 2026-09-08). That is
  roughly 10 raw matches per day. Historically, only a minority of raw matches close as measurable
  strict-copy positions within a short window. This document fixes the 45-day ceiling now, before
  any forward trend is visible.
- The rule 3 bootstrap interval includes zero.
- The candidate's stake-weighted return is negative.
- The rule 4 sensitivity check flips the candidate return negative.

**A result that is not clearly GO or NO-GO counts as NO-GO.** An example: candidate n reaches 30,
and the bootstrap interval excludes zero on one resample seed but includes zero on another seed.
That counts as NO-GO. The rule was already found by one search. The burden sits on the candidate to
clear the bar cleanly. The burden does not sit on the evaluator to find a reading that clears it.

## 5. What this harness cannot see

Alpha named this caveat as the one that applies hardest here, harder than it applied to the BSC
no-route classification earlier today. State it now, in advance.

- **This test measures selection. It does not measure capacity reallocation.** In observe mode,
  every entry keeps firing under the current rules. The shadow only labels which entries the rule
  would have kept. If an operator later switches the rule into a real filter, roughly 88% of
  currently admitted entries would stop firing, by Alpha's own estimate. That does not mean the
  remaining 12% receive bigger positions, better fills, or more attention. It means freed
  concurrency slots and freed reservation capacity go somewhere else. This harness holds no data
  about where they go. A closed candidate row shows one number: "this trade, sized and executed
  exactly as it happened, returned X." It shows nothing about what a differently sized, less
  concurrent runner would have returned with 88% of its slots empty. Section 4 answers one
  question: does this selection correlate with a better realized return, on the same per-trade
  capital basis the runner uses today. Section 4 does not answer a harder question: would this
  filter improve total book PnL. A GO verdict under section 4 is not, by itself, authorization to
  switch on a real admission filter.
- **Only closed rows count.** An open candidate position belongs to neither population until it
  closes. A rule that only ever produces open positions during the window would report n=0
  forever. That is a stalled measurement, not a clean null result. Check for this condition before
  a report concludes NO-GO from a low count.
- **`source_size_usd` and `raw_m5_volume_usd` carry `claimed_untrusted` provenance.** The original
  audit recorded this same limit. This measurement scores the rule using the same untrusted
  provider claims the funded runner already trades on. It does not score a verified-on-chain
  version of the rule. That match is correct, because it mirrors how the rule would actually run
  live. A GO verdict is a statement about "the rule as fed by the existing data path." A GO
  verdict is not a statement about "the rule as fed by ground truth."
- **The candidate population inherits whatever exit policy is live during the window.** The
  original audit studied one fixed cohort: a 25% take-profit, a 35% stop, and a four-hour hold.
  The live exit policy may not match that cohort during this forward window. The coin-attention
  demand-continuation direction is already in flight
  (`2026-09-08-coin-attention-demand-continuation-design.md`) and may change the live exit policy
  mid-window. If the live exit policy changes mid-window, record the policy digest on every
  position. Report results split by policy epoch. Do not pool results across policy epochs — a
  pooled report would silently blend two different experiments into one number.
- **This document pre-registers one test of one rule.** It blesses no other threshold, band, or
  variant. A near miss on these exact numbers is not evidence for a nearby threshold. Testing a
  nearby threshold after seeing this result repeats the same re-search this document exists to
  prevent.

## 6. Reporting requirement

The reader checks the GO/NO-GO conditions after they are met, or after the 45-day ceiling is hit.
The reader reports these items without edits that make the numbers look more favorable:

- Candidate n and control n, each broken out by chain and by half-window per rule 5.
- Candidate and control stake-weighted returns, with the trader-resampled bootstrap interval.
- The single-largest-trader-removed sensitivity check.
- Every rule in section 4, marked pass or fail on its own line. Do not report only the final
  verdict.
- Every position excluded from either population, with the exclusion reason: open position,
  missing PnL or notional, unknown conjunction state, wrong signal source, or wrong network.

A GO verdict is a recommendation to Alpha and the operator. It states that the evidence bar this
document set in advance has been cleared. A GO verdict does not itself activate a real admission
filter for any funded gate.

## 7. Reproduction

The conjunction shadow lives in `sniper/services/memecoin_entry_shadow.py`: `turnover_10_50_v1`,
`source_1k_10k_v1`, `turnover_source_conjunction_v1`. Its tests live in
`tests/test_memecoin_entry_shadow.py`
(`test_turnover_source_conjunction_allows_only_when_both_bands_allow` and its neighbors).

The section 4 base-rate measurement queried `memecoin_entry_attempts.trace_json`, the
`funded_entry_shadow_v1` substage, and its `gate_provenance_v1.inputs` field. The query covered
2026-09-01 through 2026-09-08, filtered to `network_id IN ('4663','solana')`. This was an ad hoc
query at pre-registration time, not a committed script. The forward read in section 6 should query
the same `trace_json` path directly, filtered by `completed_at >= <deploy timestamp>`.

## 8. Deploy record

Fill in this section when this shadow ships to the pinned funded release.

Release SHA:
Deploy or activation timestamp (UTC):
Deployed by:

Before this section holds a value, section 4's forward window has no start time. No one should
read a forward result before this section holds a value.
