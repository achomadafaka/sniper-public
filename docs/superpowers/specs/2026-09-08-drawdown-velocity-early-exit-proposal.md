# Proposal: an early exit trigger keyed on rate of decline

Written: 2026-09-08, by Beta, at Alpha's request. This is a PROPOSAL, not an implementation. It
changes when real money exits a position. Per Alpha: not Beta's call and not Alpha's call to ship.
The operator decides.

## 0. A correction to my own earlier report, made before anything else

I told Alpha `drawdown_velocity` was "already computed in `memecoin_exit_market_state` but read by
nothing." That was wrong, and I found the error while writing this document, not before reporting
it — worth saying plainly rather than quietly folding the correction into the proposal below.

Checked directly against `data/memecoin_exit_state.db`: `drawdown_velocity` is **0 of 97,612 rows
populated. `drawdown` (the metric velocity would even be a rate of) is also 0 of 97,612.** Neither
value has ever been written. Grepping `sniper/services/memecoin_exit_market_state.py` for
`drawdown_velocity` finds exactly one line, and it sets the field to `None` as a fallback when
"no executable series" exists — there is no other line that computes a real value. This matches
Alpha's own correction from the same exchange: `controller_revision` is 0 on all 1042 funded rows,
funded contracts carry `engine_authoritative=False`, and the WO-5A reducer this table belongs to
only runs for an N=5 canary. The column exists in the schema. The signal does not exist in the
data. A populated schema is not an observation — I should have checked the values before citing the
field as available, and I'm flagging that I didn't, not just fixing the number.

**Consequence for this proposal: it cannot be built by "reading an existing signal."** It requires
computing a new one, from data the funded lane already touches on every sweep tick, not from the
currently-inert exit-state engine.

## 1. What this proposes

The b_stop decomposition found that 99.7% of the -$1,363.94 overshoot lands BEFORE the system's
own confirming check ever fires — median 5.1 points past the nominal stop at first detection, mean
8.6. The check itself is fast (sub-second for the dominant lane); the price is not continuous. On
a thin memecoin pool, a single trade can move price by double-digit percentage points, so the FIRST
observation below the stop line is often already deep past it. Waiting for the absolute threshold
to be crossed cannot fix this — by the time it is crossed, the damage may already be done.

The proposal: alongside the existing absolute-threshold check
(`alpha_b_real_exit_reason`, `sniper/services/memecoin_real_runner.py:518-593`), add a check on the
RATE of decline between two consecutive price observations for the same open position. A position
falling fast enough, even before it reaches the full stop distance, is evidence that a worse
outcome is likely coming — exit on the rate, not only on the level.

## 2. What "rate" would actually be computed from

Two consecutive observations are needed: the current price and one earlier reference. Two
candidate sources, neither yet verified as sufficient:

- **`memecoin_positions.last_quote_price` / `last_quote_ts`.** These already exist on every open
  position and are already read once in the runner (`memecoin_real_runner.py:588`, as a quote-age
  check, not a velocity input). Whether they are written frequently enough, and BEFORE being
  overwritten by the current tick's own quote, to serve as a genuine "N seconds ago" reference is
  NOT verified in this pass — the earlier finding that `last_quote_ts == closed_at` on 100% of
  b_stop closes shows they at minimum get overwritten to match the terminal state; whether an
  in-flight open position keeps a real trailing value between ticks needs to be checked by whoever
  implements this, before assuming the columns are usable as-is.
- **A new small per-position state cache**, written by whichever sweep lane owns the price read
  (`fastlane_poll` / `fastlane_wake`, per `memecoin_exit_trigger_audit.lane`), holding the last
  observed `(pnl_pct, observed_at)` pair. More certain to work; more code, and a new write path on
  the funded lane's hot path, which needs its own care about not slowing the sweep that already
  measures sub-second for the dominant lane.

This document does not pick between them. Whoever builds this should measure which is actually
available before choosing, the same discipline this whole investigation used everywhere else.

## 3. Pre-registration, so building it does not become another after-the-fact search

If this is built, decide these numbers BEFORE looking at how it performs, not after:

- **The rate threshold itself.** Proposed starting point, not yet measured: percentage points of
  decline per second, computed over the shortest interval the data source in §2 can reliably give.
  Before setting a number, pull the actual pnl_pct trajectory for a sample of the 221 b_stop
  overshoot positions (the underlying prices exist in `memecoin_exit_trigger_audit` rows already
  written by the routine sweep, even without a dedicated velocity field) and measure the REAL
  point-to-point rate in the seconds before the confirming check, for at least the 52-position
  "large gap" bucket that carries 46% of the dollar total. Set the threshold from that measured
  distribution, not a guessed round number.
- **Forward sample size before judging it**, mirroring the turnover-conjunction pre-registration's
  discipline: at least 20 positions where the new rate trigger fires DIFFERENTLY from the existing
  absolute-threshold trigger (i.e., it exits earlier than b_stop would have), so the comparison
  measures the trigger's own effect, not noise from positions where both would have fired at
  essentially the same moment.
- **The comparison that matters**: for those 20+ positions, realized PnL under the early rate exit
  versus the PnL the SAME position would have realized under the existing absolute stop (reconstruct
  from the same fill/price data this investigation already used). The early trigger is worth
  keeping only if it improves that comparison by a margin large enough to survive removing its
  single best-performing position — the same single-trade fragility check the turnover-conjunction
  kill criterion uses, for the same reason.

## 4. What would kill it, decided now

- **A false-positive rate that exits healthy positions early.** A fast SHORT-LIVED dip that
  recovers is not the same as a fast dip into a genuine collapse, and this proposal cannot
  distinguish them without a forward sample showing it can. If, across the pre-registered sample,
  a material share of early-triggered exits would have recovered to a better price than the actual
  early exit realized, that is not "the rule found a real edge" — it is a new source of overshoot
  on the other side of the trade, from exiting too early rather than too late.
- **No measurable improvement over the existing absolute stop** once the single-largest-trade
  removal check runs. A rate trigger that only helps because of one dramatic save is not a
  reliable mechanism.
- **A dependency on a data source that turns out to be unavailable or too stale** (§2's open
  question). If neither `last_quote_price`/`last_quote_ts` nor a new per-position cache can supply
  a genuinely recent second observation on the hot sweep path without adding meaningful latency to
  the EXISTING sub-second decision loop, this proposal is not implementable as designed and needs
  a different mechanism, not a weaker version of this one.

## 5. What this does not claim

- It does not claim the 9 catastrophic outliers (31% of the dollar total, at least 3 of which are
  independently confirmed bad-entry-route cases, not exit-timing cases) would be caught by this.
  A position that opened already deep underwater from a bad entry quote has no "healthy decline"
  to detect a rate on — the entry-side fix (pricing round-trip cost on every rung, not only
  `uniswap_v4_multihop`, per the separate finding on the 12 post-fix positions) is the lever for
  that population, not this one.
- It does not claim to eliminate gap risk, only to reduce its typical size by exiting somewhat
  earlier on the way down. The underlying cause — thin liquidity moving price in discrete jumps —
  is a market-structure fact this proposal cannot change.
- It is not a request for the operator's decision on the rate threshold, the data source, or the
  activation mode. It is a request for the operator's decision on whether this direction is worth
  building at all, given the pre-registration cost above, before more work goes into it.
