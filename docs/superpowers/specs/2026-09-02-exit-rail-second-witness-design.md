# Exit loss rails: require a route-quote witness — design

Date: 2026-09-02
Author: Gamma
Status: design. BLOCKED on an attribution field that does not exist yet. No code written.
Approved: operator, relayed via Alpha.

## Amendment, 2026-09-11

The chart-derived explanation below is unproven. The Relay adapter instead used the slippage floor as expected output.
The correction separates these amounts and confirms each EVM stop against its selected execution quote before funded writes.
This correction does not require the proposed chart-provenance predicate. The broader proposal below remains inactive.
See [the quote audit](../../audits/2026-09-11-stop-quotes-and-flame.md) for CHILLAH, SUIT, and the evidence limits.

## Why

Position 48515 (CHILLAH, 4663) was closed by `b_stop` 14.4 seconds after entry. The rail
confirmed against a preview of `$62.87` and an `executable_unit_pnl_pct` of `-37.29%`. The
sale executed at `$89.75`, a realized `-10.5%`. The fill beat the confirming quote by 42.7%.
A sell cannot execute 43% above the true mid, so the confirming number was wrong and the
position was healthy.

The entry was clean — 1.01x the chart mid. Nothing about that trade justified a stop.

## The predicate, and why this is blocked

The rail must hold when the confirmation contains NO ROUTE QUOTE — when the number that
authorised a sale was chart-derived rather than executable.

**That field does not exist, and I verified it cannot be proxied.**

1. `memecoin_exit_trigger_audit.detail.code` does not distinguish them. `threshold_confirmed`
   covers both quote-derived and chart-derived confirmations (176 b_stop, 284 real_tp over the
   sampled window).
2. `preview_usdc` presence does not distinguish them either. Over 14 days, loss-rail
   confirmations carrying a preview: **solana 163 of 163, 4663 18 of 18. Zero without.**
   CHILLAH itself carried one.

So the only way to know today is to reproduce the arithmetic by hand, which Alpha correctly
ruled out as a rail condition. Beta is making the nominal fallback attributable. This design
waits for that field and must not guess at it: a rail keyed on a predicate that cannot fire
is the inert-guard defect this codebase has hit repeatedly.

## The four conditions

### 1. Freeze, never fail open

Same shape as `state_uncovered_frozen` in the volume-death rail. The rail holds, records a
distinct reason, and self-arms the moment a route quote is available. A held position is the
intended outcome, not an error, and it must not read as one in the audit.

### 2. Loss rails only — `b_stop` covered, `real_tp` NOT

`b_stop` protects principal by selling into a low. A fake low makes it sell a healthy bag,
which is what happened to CHILLAH, and the loss is realized and permanent.

`real_tp` is the opposite risk. Freezing a take-profit means failing to sell into a fake
high. That costs opportunity, not principal, and the position remains ours to exit later.

The two are not symmetric and must not share a rule. Do NOT apply this to all six
`_EXECUTABLE_PRICE_REASONS`.

### 3. Catastrophe is EXEMPT, and this is a deliberate asymmetry

A catastrophe rail fires near -80%. Suppose the confirming quote is wrong by the worst
margin ever observed, the 43% of CHILLAH. A position reading -80% on a quote that generous
is still near -60% in truth — catastrophic either way. The rail's decision does not change.

But the failure directions do not match. Freezing a 35% stop on a bad quote risks holding a
position that should have cut, and it is recoverable. Freezing an 80% catastrophe risks
holding a bag to zero, and it is not.

So catastrophe fires on whatever confirmation it has. It is the rail of last resort and it
must never be gated on the availability of an instrument we know to be unreliable.

### 4. The cost, stated plainly

**Freezing `b_stop` on quote-less confirmations means some positions that genuinely should
cut will run further down.** That is the trade, and it is accepted knowingly.

Alpha estimated 58% of 4663 stops confirm against the chart. **I could not reproduce that
figure** — see the predicate section: every loss-rail confirmation in 14 days carries a
preview, so whatever produced 58% used a method the audit table does not expose. The true
exposure is therefore UNKNOWN until Beta's field lands, and the first thing to do when it
does is count it before arming anything.

## What to measure after it arms

- The frozen count, per chain, per day. If `b_stop` stops firing on 4663 almost entirely,
  the rail has become a de facto stop removal on that chain and the operator must be told.
- Realized PnL of positions that were frozen and later closed by another rail. That is the
  cost of the trade, measured rather than assumed.
- Whether the freeze rate falls as the v4 route work lands. It should, and that is the same
  forward prediction already recorded for entry premium and exit preview error.

## Not doing

No detection of my own. No arming until Beta's field exists and its exposure is counted.
