# Memecoin Pons V2 entry cost gate (chain 4663) — design

Date: 2026-09-05. Owner: Alpha (sniper-fc). Status: approved by the operator ("Go") 2026-09-05.
Builder: sniper-f0.

## Problem

The Pons V2 rung admits a funded entry with no measure of what the round trip costs and stores
no curve state on the route. The V4 multihop rung measures its round trip on every decision and
the caller refuses it against the take-profit budget; Pons has no such check. Funded Pons V2
history to 2026-09-05: 13 positions, -$77.5 net, 5 take-profits (+$212) and 4 stops (-$309).
SOLANO (52064) lost $120.6 in 59 seconds when its curve drained; nothing recorded the curve's
reserves, fee or tax at entry, so the stops cannot be explained from the ledger.

## Outcome

1. Every Pons V2 buy quote carries the curve state and the round-trip cost of the clip, computed
   from the same numbers the quote already reads (no extra RPC call).
2. The ladder refuses a Pons V2 entry whose measured round trip exceeds the take-profit budget,
   with the same helper and the same budget the multihop rung uses, and names the reason.

## Round trip, pure function of the curve state

Inputs already in `dex_pons_v2_quote`: `amount` (wei in), `quote_reserve`, `token_reserve`,
`fee_bps`, `tax_bps`, `sellable`, router fee `_GMGN_FEE_BPS`.

Buy (as today):
- `router_net = amount - amount * router_fee // BPS`
- `net = router_net - router_net * fee_bps // BPS - router_net * tax_bps // BPS`
- `tokens = net * token_reserve // (quote_reserve + net)`

Reserves after the buy: `q1 = quote_reserve + net`, `t1 = token_reserve - tokens`.

Sell of `tokens` straight back (mirror of today's sell branch, on the post-buy reserves):
- `gross = tokens * q1 // (t1 + tokens)`
- `curve_net = gross - gross * fee_bps // BPS - gross * tax_bps // BPS`
- `back = curve_net - curve_net * router_fee // BPS`

`round_trip_pct = 100.0 * (back / amount - 1.0)`. Negative for a loss. Integer arithmetic with the
same rounding as the quote.

## Route fields

The buy quote's `route` gains: `quote_reserve`, `token_reserve`, `sellable`, `graduated` (when the
state carries it), `clip_share_pct` = `100 * net / quote_reserve`, and `round_trip_pct`. The
executor already stores `quote["route"]` in `evm_route_json`, so no persistence change.

## Gate

In `evm_real_seams.venue_quote_with_v2_fallback`, the Pons rung wraps the quote exactly as the
multihop rung does: if the quote is not None and `round_trip_within_budget(route.round_trip_pct,
_entry_tp_pct(chain))` is False, the rung returns `(None, None, "round_trip_over_budget:<pct>")`.
Entry only. The exit rung is untouched.

## Not in scope

- Any new depth floor or clip cap. The measured cost decides, as for the multihop.
- Predicting a drained curve. SOLANO's curve quoted a stable exit for 59 seconds; this gate would
  not have refused it. It gives the ledger the numbers to test a depth or share hypothesis later.

## Testing

Unit (`tests/test_memecoin_pons_v2_quote.py`, extend):

1. Round trip for a fixed curve state equals the hand-computed value (golden numbers).
2. The buy quote's route carries reserves, sellable, clip share and round trip; the sell quote's
   route is unchanged.
3. A curve with fee + tax that makes the round trip worse than the budget is refused by the ladder
   with reason `round_trip_over_budget:<pct>`; a cheap curve passes.
4. The exit rung is never refused on cost.

Read-only canary (publicnode): compute the round trip for the 13 funded Pons positions' curves at
their stored `curve` address today and report the distribution; state plainly that today's
reserves are not entry-time reserves.

## Rollout

Ship inert-by-default is not needed: the gate uses the existing budget
(`MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_TP_FRAC`, currently 0.4 of the take-profit). Deploy with
the normal pinned deploy; report the first Pons refusal that names its cost and the first fill
whose route carries the curve state.
