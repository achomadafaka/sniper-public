# Office live coin view: design note

Operator order 2026-10-30, via Alpha: seats and the operator look at a coin live as it changes, not one sheet at one time. Beta owns the host; Epsilon owns the UI. Not built. Numbers below are one-off measurements on the live host at Nice 19, today.

## The limit stated first

A model can't stream. A seat decides in turns. So "live" means: the host keeps the series current and cheap; the seat reads it whenever it is awake; and the host wakes it when a condition it set is met. Decision latency is detection + wake + the model's turn. Last hour, 68 wakes: duration p50 16 s, p90 92 s. So expect tens of seconds from event to order at best, against a move that can take seconds. That is the honest ceiling; it isn't fixed by this design. A rendered chart doesn't change it.

## Host: `coin_view(mint, window_s)`

One read returns a bounded series plus the sheet fields, each field with its age and proof status:

- Candles (10 s, 1 m) and flow per bucket: buys, sells, buy and sell SOL, from the PumpSwap tape, by the existing `swaps_ps_pool_received` index. Reuse `memecoin_candles.py`; price basis named; every window carries its tape loss count and reads `incomplete` when losses apply.
- Marks with binding kind, proven, carried, estimated, null: same estimate path as exit marks, labelled.
- Holders: top accounts and owners from the chain tool, cached 60 s, on its pace; age shown; absent for coins nobody watches.
- Sheet fields: the latest receipt, with its stamp and expiry.

No store at first: compute on read. A read costs 0.24 s for the busiest pool (11,421 rows in 15 minutes) and 1 ms for the median pool (6 rows). Add a cache only if measured load needs it. So no new disk beside the runner and no retention rule, apart from the read hashes Gamma requires, stored like results reads, deduped, on the same retention horizon.

Output cap 16 KB per read. Closed results and open PnL are never in it.

## `watch(mint, conditions)` and `unwatch(mint)`

A fixed grammar: field, operator, threshold, over view fields such as price change, buys, sells, liquidity, holder jump. No code or free text. Limits per seat's epoch are not caps on use: a watch expires at a stated time, default 15 minutes, so forgotten watches stop costing.

A thread on one host evaluates watches every 5 s over all watched coins under a per-pass CPU budget; a skipped evaluation is counted, never silent. A hit mails the seat from sender `watch`, and the existing mail path wakes it. Detection latency is about 5 s plus commit lag, currently p50 about 10 s, up to 30 s (hour read due 19:15Z). The evaluator shares the tape, not the chain tool, unless a condition names holders.

## Decision flow

Judge reads a sheet, sets a watch, and enters later if conditions hold, within the same sheet expiry, stamp plus 120 s, and the same hard filter, as Gamma states. So patience is bounded at 120 s by the sheet; beyond that the Judge rereads a fresh sheet, which already works. `paper_order` is unchanged.

Rendered chart image: skip for now. Vision tokens per image on every read, plus render CPU, for data the series already has; and seats read numbers well. The operator gets a real chart from the same series. Revisit if seats show they miss patterns.

## Operator view (Epsilon)

The Office reads the same `coin_view` through an authenticated route and polls it every few seconds for coins any seat watches. Same envelope, so the operator sees what the seat sees. Reuses the existing chart code; Epsilon's note covers rendering.

## Cost

- Quota: a read inside a wake adds up to 16 KB of tokens, about 5,000. Each watch hit is a wake: recent medians 3,000 counted tokens and 0.95M cache read; rotation bounds the session. Cost per watched coin is its hit rate times that, so conditions decide it; a coin that fires every minute costs what a seat used to cost at its worst. Report wakes per watched coin. The account is at 7d 66 percent, guard at 80. Unmeasured until live.
- Host: the evaluator runs in Head's host under Nice 19, IOSchedulingClass idle: at 5 s per pass, about 0.24 s per busy coin, so ten busy coins would be about half of one core at worst, and the budget skips beyond that. Measure before more.
- Chain credits: holders only, cached, on the shared pace.

## Tape dependency

Everything inherits the tape: commit lag; multi-hop swaps not decoded, so on routed coins flow undercounts and marks rely on estimates. Multi-hop decode is therefore the view's largest quality fix. Every derived number shows loss count and age, so a thin view reads thin.

## Gamma's requirements carried

Order labels gain `live_view_asof`, `live_view_reads_n`; wakes record reason and watch id; view hashes stored before return; event-to-order latency reported for watch against ticket. A 120 s extension or a live re-filter is a definition change needing Alpha's reference; not in this design. A watch on a non-feed coin is information only.

## Order of work

1. `coin_view` read tool and the operator route. 2. `watch` evaluator. 3. Labels. Each logged, reviewed by Zeta for walls, Gamma for labels.

## Not handled

True real-time entries; other chains; coins without tape; a chart image.
