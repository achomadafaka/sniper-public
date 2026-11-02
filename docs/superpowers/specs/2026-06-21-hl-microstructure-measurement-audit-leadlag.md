# HL Microstructure Measurement Audit v1 + Leading-Venue Capture v0

Type: offline research study + perishable data capture. NOT a trading policy change.
Isolation: branch `research/hl-measurement-audit-leadlag` (worktree), read-only against the
live paper DB; capture writes a separate DB. The live A/B campaign is untouched.

## Framing

Diagnostics: frictionless ≈ +3.5 bps, maker ≈ +1.5 bps, taker ≈ −7 to −9 bps. The question
is not "is there signal" but:
1. Is native HL signal *larger than +3.5 bps* once we fix a measurement lens that likely
   understates it (mid labels + wall-clock + trade-imbalance instead of true OFI)? → Part A.
2. How much apparent edge is HL lagging a leading CEX, and is the lag tradable at our
   latency? → Parts B–C (capture is perishable, starts now; study runs later).

Cost-structure bounds:
- **Column A (+1.5 maker) is unprovable offline** — touch-fill upper bound before queue
  position (unknowable on public data) and flow toxicity (VPIN). Deferred, infra-gated. No
  result here may be reported as "we have +1.5 bps maker edge."
- **Column B (signal clearing 7–9 bps taker) is the only provable path.** HL-native,
  non-latency-race candidate = liquidation-cascade (Part E, queued).

No magnitude promises. Report measured bps with CIs; never project "could double the signal."

## Clock discipline (load-bearing — confirmed in data)

`hlm_raw_events.receive_ts_ms − exchange_ts_ms ≈ 7.006 h`. That is **local-clock skew, not
latency.** Rules:
- Never mix an exchange timestamp with a receive timestamp in one delta.
- Cross-venue *source* alignment → each venue's exchange clock (HL `time`, Binance `E`/`T`).
- *Receive-time* (latency) alignment → same-host receive clock only (skew cancels for
  HL↔Binance because both captured by this host).
- Tradability gate: edge stronger on source-time than receive-time ⇒ latency illusion.

## Marking convention (all of Part A)

- **Lead-structure** ("does X predict where price goes?") → label on forward **microprice**
  return/direction. Microprice = `(ask·bid_sz + bid·ask_sz)/(bid_sz+ask_sz)` (Stoikov).
- **Cost-clearing** ("does it beat the taker spread?") → mark hypothetical entry/exit at the
  **executable** price at the future ts (buy at best ask, sell at best bid) + modeled
  slippage + fee. **Never mark PnL at microprice or mid** (microprice is a transform of the
  predictor; marking PnL on it manufactures phantom edge). Microprice is a feature/lead
  target only, never a PnL mark.

## Do-not list

No live A/B rule change · no live orders/keys · no prod-DB writes · no live-weight backfill
from interim results (Part D) · no edge claim (produces a recommendation) · never treat a 1s
tick or a book event as independent.

## Part A — offline audit on existing HL data (run first)
- **A1 microprice labels** — forward mid vs forward microprice return/direction;
  `microprice_minus_mid_bps`, slope, cross-signal. Which families look stronger on microprice.
- **A2 event/volume clock** — horizons in next 5/10/25/50 trades, next N book updates, next
  X notional, vs wall-clock 1/5/10/30s. Sample on events (trade; bbo/l2 move), not 1s grid.
- **A3 true OFI** — Cont–Kukanov–Stoikov best-level OFI from consecutive bbo/l2Book best
  levels (snapshot deltas). Horse-race: trade flow_imbalance vs static book_imbalance vs OFI
  vs combos vs fusion_score, on both targets, under executable marking for cost-clearing.
- **Free lead-lag reads (no ingestion):** mark−oracle basis (stored; oracle is a CEX
  composite) as CEX-distance proxy → predicts HL forward executable return? · BTC→alt
  internal lead-lag on HL-only data (lag grid) · historical Binance source-time
  cross-correlation (existence-only, NOT tradability).

## Part B — leading-venue capture v0 (parallel, immediately; perishable)
Binance USD-M, symbols matching active HL assets (BTC/ETH/SOL + top HL by radar). Streams:
`@aggTrade`, `@bookTicker`, `@depth5@100ms`. Persist append-only raw: venue, symbol, stream,
source_event_ts (E), transaction_ts (T), local_receive_ts, best bid/ask + qty, price, qty,
aggressor, update ids, raw payload, capture_lag_ms. Separate DB; no strategy use.

## Part C — lead-lag study (after enough simultaneous data)
Targets: HL mid return · HL microprice return (lead only) · **HL taker-executable direction
after spread/fee (the one that matters)**. Predictors: Binance return shock / aggTrade
imbalance / book-OFI / microprice deviation; BTC lead for alts. Lag grid −2000..+5000ms on
**both** source and receive clocks. Negative controls required: wrong-asset, reversed-lag,
day-shuffled, randomized-event-times, future-leakage block, **source-vs-receive illusion**.

## Part D — do NOT close live-weight loop yet
Offline reliability only → freeze a candidate weighting (weights from data strictly prior to
each decision, expanding/rolling, never the pooled in-sample table) → activate only as a NEW
forward-paper cohort → never backfill old windows.

## Part E — liquidation-cascade (queued, after A–C)
distance-to-liq-cluster, directional liq pressure, OI expansion/contraction, mark−oracle
trigger, cascade continuation. Pass bar: realized move > spread + fee + slippage.

## Validation
Walk-forward / time-blocked only · purge/embargo on overlapping labels · net of fee + visible
slippage, executable marking · same-window paired A/B of methods · asset/regime segmentation ·
multiple-testing log, BH-FDR 0.1 · negative controls (incl. latency-illusion) · no live A/B
mutation.

## Required outputs
- **Measurement Audit v1**: sample sizes; reproduced frictionless/maker/taker; mid vs
  microprice; wall vs event vs volume clock; OFI vs trade-imbalance vs static book; families
  surviving spread+fee under executable marking; free lead-lag reads; assets/regimes carrying
  signal; fusion over/under-weighting; verdict (measurement- vs cost-caused); one recommendation.
- **Capture v0 status**: symbols/streams/event counts, receive-latency dist, gaps/reconnects,
  skew vs HL, first lead-lag diagnostic, sufficiency.

## Recommendation set (exactly one)
no change · new frozen cohort on OFI/microprice · new frozen cohort on Binance lead-lag ·
abandon taker, focus maker/queue (Column A, infra-gated) · run liquidation-cascade study.
