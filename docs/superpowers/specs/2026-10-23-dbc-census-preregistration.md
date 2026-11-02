# DBC shadow census: pre-registration (frozen 2026-10-23, Gamma)

Status: FROZEN before the first census byte exists. Edits after the first run starts are amendments, listed at the bottom, never rewrites.
Tape: `sniper/research/dbc_census_tape.py`; worker: `scripts/memecoin_dbc_census_worker.py` (Lambda, SHA b0e5e4ef8). Measurement only. No funded authority, no strategy claim, no copy advice.

## Question
How many Meteora DBC swaps occur per config and quote class (WSOL / other / unresolved), so a DBC lane has a measured denominator?

## Reads (the only ones permitted)
1. Per 15-min and 60-min bucket x config x quote_class: swaps, buys, sells, failed, err_unknown, distinct pools, distinct mints, notional_quote (successful + unknown-err, quote units), dropped, partial.
2. Share of delivered txs by outcome: decoded EvtSwap swap, failed tx, EvtSwap2-only skip, decode skip, no-event tx (from `stats` / `skips` / `tx_minutes`).
3. Resolution rate: fraction of swaps with mint non-NULL and non-'' (resolved), '' (unreadable), NULL (unresolved), by hour.
4. Coverage verdict from the worker (3 random 5-min windows, +/-5% vs getSignaturesForAddress).

## Validity gates (each read is labelled by its gate; a failed gate means the read is "lower bound / partial", never dropped from the report)
- G1 Coverage: verdict `pass`. `partial` => all counts are lower bounds and the report says so in its first line.
- G2 Continuity: any `stream_gap:*` skip or `drops.n>0` in a bucket => that bucket is a lower bound. Hours after `stop_reason != window_end` are NULL.
- G3 Resolution: report SOL-vs-other shares ONLY over resolved swaps; if unresolved+'' > 20% of swaps in the run, the SOL/other split is labelled "unresolved-heavy" and no share is quoted as a headline.
- G4 Swap2 share: if `skip:evtswap2_only` > 5% of delivered non-failed txs, the census is "EvtSwap-only" and is NOT called a swap census; the report states the decoded fraction.
- G5 Sample: one 6 h window, one time-of-day slice. No "typical day", no per-day rate extrapolation; hourly series shown raw.

## Denominator definition
A swap = one EvtSwap self-CPI inner instruction in a delivered tx (ordinal per tx), including failed-tx rows reported separately (failed rows have no readable event: whether a failed tx was a swap is unknown, so they are NOT added to swap counts; they are reported as `failed_tx`). Rows are the host's view (arrival_ts), not the market's.

## Not asked / forbidden conclusions
No PnL, edge, fill quality, price impact, exit quote, or "DBC is worth a lane" verdict from this tape. The only decision it feeds: whether a DBC lane has enough volume per config to be worth building the next measurement step.

## Pre-start conditions (Gamma, before Lambda starts)
- C1: decoder checked on >= 5 real DBC txs (getTransaction, research key, <=10 calls): decoded amount_in/amount_out match the tx's token balance deltas. Synthetic-byte tests alone are not enough.
- C2: same 5-10 txs report whether swap2-only txs exist and the share of EvtSwap vs EvtSwap2.
- C3: run niced as in the worker docstring; operator yes already given via Alpha.

## First-10-minute abort/sanity (read by Gamma, read-only)
Abort and report if: tx_minutes shows 0 txs for 3 consecutive minutes; or process_errors > 1% of tx; or the tape grows > 2 GB/h; or the worker's CPU is above 50% of one core for 5 min.

## Amendments
A1 (2026-10-23, before first run): C1/C2 sample done by Lambda on 5 real swap txs (research key, 19 calls). Decode correct on all 5 (out and direction exact). amount_in equals the vault delta in 4/5; in 1/5 it is NET of the input-side trading+protocol fee (event fees 159,040 + amount_in 3,816,960 = vault delta 3,976,000; a third account +24,000 not in the event). Consequence: `notional_quote` is "event amount_in basis", can understate gross by the input-side fee (~4% on that row), and is a RANKING quantity only, never quoted as volume. C2: EvtSwap and EvtSwap2 both present in 6/6 swap txs, swap2-only 0/6 (n small; gate G4 still decides at run time). GO given on that basis.
A2 (2026-10-23 09:40Z, run in progress): at minute 8-9 the census tx rate fell ~100x (800-1300/min to 8-18/min). One RPC cross-check (Lambda, 1 call) matched the tape exactly on 4 low-volume minutes (18/8/18/3), so the dip is real. That check does not verify burst-rate delivery. If all final coverage windows hold <100 tx, G1 reads "pass (low-flow windows only)" and burst-rate counts stay labelled lower-bound-unverified.
A3 (2026-10-23 09:45Z): burst delivery verified. Windows 09:26-09:31Z tape 4455 vs rpc 4453 (-0.04%), 09:31-09:36Z tape 1132 vs 1130 (-0.18%), both complete (paged to window start), failed txs counted both sides. A2's "burst unverified" caveat is withdrawn for this run's first 10 min; later bursts are still covered only by the end-of-run random windows.
A4 (2026-10-23 09:42Z, operator order: desk latency first): the shared research key (pacer GAP_S 1.1 s) was slowing the desk hard filter (emission->PASS p50 1.42 s before 09:25:30Z, 3.07 s after, n=13, Lambda). RUN SPLIT: run 20261023T092528Z-1c00d9 (with resolver) was stopped 09:41:54Z, stop_reason 'operator_desk_latency', 985.6 s, 8956 swap rows, its end-of-run coverage did NOT run; burst coverage A3 (windows 09:26-09:36Z) belongs to it and stands. Run 20261023T094228Z-241df8 (09:42:28Z, 21600 s, --no-resolve --no-coverage, zero research-key calls) is a SEPARATE run; the 34 s between them is a gap. Rules: (1) the two runs are never summed into one 6 h window; run 1 is a 16-min run. (2) Run 2 has mint/quote_mint/decimals NULL until a post-run resolve, so quote_class is NULL for all of run 2 until then: G3 applies, no SOL/other share is quoted from it before the resolve. The resolve is throttled and happens after the run (Alpha clears the key use). (3) Run 2's G1 needs the coverage check later; until it runs, run 2 counts are "coverage-unverified" (A3's check does not transfer to it). (4) pool-count and swaps-per-config read from run 2 need the post-run resolve of config too: config is in the event (EvtSwap), so config reads are available now; quote_mint/mint are not.
