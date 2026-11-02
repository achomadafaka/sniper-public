# Runner-Profile Own-Signal Preregistration

**Date:** 2026-08-21. **Status:** SEALED before any forward data.
**Origin:** `finding-memecoin-runner-coin-criteria-2026-08-21` (in-sample, n=55, 12 runners).

## Question

Our first coin-state signal, independent of wallet buys: when a sub-250k Solana coin enters
the frozen runner profile, does a $100 quote-to-quote round trip at +30 seconds return a
positive median after route cost?

This study opens no position, signs nothing, grants no funded authority, and creates no
paper strategy arm. It measures executable quotes only.

## Frozen trigger

A candidate mint fires when, from pump.fun candles (pre-trigger data only, derived mcap =
price x 1e9 below $250k), either profile holds:

- **A — young igniter:** tape age <= 14 min AND volume in the last 60s >= $3,000. The
  tape must reach the coin's first trade (an untruncated candle tail); a truncated tail
  makes the age a lower bound and an old hyper-active coin would false-fire as young.
  (Pre-data amendment, 2026-08-21, zero forward rows collected at the time.)
- **B — re-ignition:** prior life run >= +380% from first candle AND volume last 60s >=
  $3,000 AND momentum last 300s >= +34%.

One measurement per mint per 600 s. At most 4 in-flight measurements; a capacity skip is a
censor with reason `capacity_skip`. The fired profile (A/B/both) is recorded.

## Discovery universe

Mints observed in `memecoin_events` (solana, last 60 min) plus creates from
`ps_tokens` (age <= 20 min). At most 60 candidates per cycle; overflow is logged.
Coverage is therefore bounded and NOT the full venue — the report states this.

## Measurement

1. At trigger: one Jupiter ExactIn quote, USDC -> mint, $100 (100,000,000 atomic). The
   quote must complete within 10 s of the trigger read, else censor `entry_quote_late`.
2. Sell quotes of the exact acquired quantity at +15 s (descriptive), **+30 s (primary)**,
   and +60 s (descriptive). Each leg has a +/-5 s window; a late primary censors
   (`exit_quote_late`), a late descriptive leg is marked absent.
3. Net return = primary out / 100,000,000 - 1. No mid prices, no imputation.
4. Transient retry: one retry after 200 ms for transport/timeout only.

## Profile C addendum — second act (2026-08-21, additive arm)

Day-one evidence: every scored signal fired on a coin younger than 3 minutes (n=76: 8%
run-and-hold, 37% rug at 5 minutes). Profile C targets the untested later window and is a
NEW arm with its own rows and its own read; profiles A and B are unchanged. C fires when
the candle tail reaches the coin's first trade AND tape age >= 900 s AND the life high is
>= 120 s old AND prior run >= +170% AND vol60 >= $3,000 AND +35% <= mom300 < +300%.
C's read uses the same gates and metrics as the primary read, computed separately.

## Recorded features (not gates)

At trigger the collector records, without gating on them: the fired profile, mcap, tape
age, vol60/vol300, mom60/mom300, range300, off-high, prior run, and holder concentration
from one `getTokenLargestAccounts` call (top-1 and top-5 share of supply, count of
non-zero accounts in the top 20). These exist so the read can test holder-based
refinements on forward data without a new collection round.

## Buyer-feature addendum (2026-08-21, post-review)

Buyer-address features (distinct buyers, top-1/top-3 volume share, same-second bundle,
crew co-occurrence) recorded per signal from the venue's latest-100 trade tail. Review
findings fixed before the boundary: the tail is not a guaranteed 120 s window
(`buyer_window_s` + `buyer_tail_truncated` record true coverage), raw buy rows are
retained in `runner_signal_buyers` for audit, the registry upsert refreshes `seen_at`,
and the feature fetches run AFTER the sealed entry quote with exit-leg servicing
interleaved. `crew_score` is a co-occurrence proxy, not entity resolution and not a
manipulation verdict. **Activation boundary: buyer rows recorded before the fixed build's
restart are discovery data only and enter no read.** The boundary signal id is the first
row after the restart logged in HANDOFF 2026-08-21.

## Read gates

Counts and censor reasons only until: 50 non-censored primary measurements, 25 distinct
mints, 5 distinct UTC dates. Then: median net return at +30 s with a mint-clustered
bootstrap (10,000 samples, seed 20260821), hit rates >= +10% and >= +25%, per-profile split.
`ESTABLISHED` = bootstrap lower 95% bound above zero. `REFUTED` = upper bound at or below
zero. Anything else = `WEAK`, one extension to 100/40/10, then final. A censor rate above
40% is a reportable defect. If floors are unmet in 21 days: `NOT ESTIMABLE`, stop.

An `ESTABLISHED` result is measurement evidence only; any strategy arm or funded use needs
its own spec and approval.

## Defect note 2026-08-22 — profile C never fired (age_known)

`age_known` was computed as "the 1 s tail has fewer than 1,000 candles". An active coin
older than ~17 minutes always has a full 1 s tail, so every coin profile C targets (age
>= 900 s) read as age-unknown and C fired zero times in 3,342 signals; profile A also lost
its oldest band. The sealed rule is "the tail reaches the coin's first trade". The fix
establishes the first trade from the 15 s tail (untruncated to ~4 h) when the 1 s tail is
full, one extra call for active coins only. C's read starts at the first row after this
note's commit; A's and B's rules and reads are unchanged (the fix only turns "unknown"
into a true age; it never makes an old coin look young).

## Discovery amendment 2026-08-22 — durable watchlist (coverage, not rule)

The bounded universe above sampled the newest 30 creates per cycle, which covered ~59 s
of launches at 448 creates per 20 min, and the scanner never evaluated a coin past ~5 min.
Discovery now enters EVERY ps_tokens create inside 30 min and every feed mint with a buy in
the last hour into `runner_watch` once, and re-evaluates each on a cadence (45 s under
5 min of age, 120 s to 30 min) under a per-cycle time budget; overflow stays due and
never-evaluated mints go first. The trigger rules, dedupe (600 s), quote measurement, and
read gates are unchanged. Rows before this commit sampled a narrower universe; the read
reports the universe per period.

## Profile D addendum — any-age active (2026-08-22, additive arm, operator order)

Operator read: age does not push performance. Tape: the oldest observed band (90-180 s)
reached $40k at 20% vs 10% under 90 s, and nothing past ~5 min was ever evaluated. D fires
when vol60 >= $3,000 AND no age-bound profile (A, B, C) admits the coin — i.e. the coin is
older than 14 min with a known age, or its age is unknown. D never duplicates A/B/C; a
coin is D only when it would otherwise have produced no signal. D is a NEW arm with its own
rows and its own read at the same gates; A, B, and C rules and reads are unchanged. The
600 s per-mint dedupe, quote legs, and capacity censor apply to D exactly as to A.
