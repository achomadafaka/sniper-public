# Polymarket crypto5m — Phase 1C: Runtime Data Campaign + Fee Truth + Market-Efficiency Gate

**Status:** spec (commit #1 of Phase 1C)
**Mode:** paper/shadow-only. No live orders, no private keys, no production DB, no services, no model
promotion, no UI. Read-only canonical/Binance/Chainlink probes are in scope.
**Direction:** per GPT Pro — do A (collect), B (fee truth), C (efficiency gate) in parallel; build C now
and run staged reports as data accrues; **no real fill**; no broad model discovery until the gate produces
evidence.

## Goal

Accumulate real BTC 5m windows; resolve fee truth from canonical read-only sources; and build a
deterministic Market-Efficiency Gate that measures (1) how good the market midpoint baseline already is,
checkpoint- and regime-specifically, and (2) the executable hurdle (fee/spread/slippage/latency) any model
must clear — so we get an evidence-based go/no-go before any probability-model discovery.

## Fee-truth finding (live probe, 2026-06-20 — CORRECTED)

GPT Pro expected `getClobMarketInfo(conditionID).fd` to return `{rate, exponent, taker_only}`. The CLOB `.fd`
is `None`, BUT a structured descriptor DOES exist — in **Gamma**. Verified live on every BTC 5m market:
- `gamma.feeSchedule = {"rate": 0.07, "exponent": 1, "takerOnly": true, "rebateRate": 0.2}` (feeType
  `crypto_fees_v2`). So the canonical rate is **0.07** (exponent 1 → matches `fee = rate·p·(1-p)`).
- CLOB `/markets/<cid>`: `fd = None`, flat `taker_base_fee=1000`. `GET /fee-rate?token_id` → `{"base_fee":
  1000}` (the un-decodable integer). `/fee`, `/fee-rate-bps` → 404.

So it is **NOT "missing"** (an earlier claim, corrected by review): the descriptor says **0.07**, but it
**conflicts with the capture-lane realized `fee_rate_bps = 0`** → `fee_status = conflict` → **entries stay
blocked** until a realized observation confirms the descriptor (a present-but-invalid realized value also
fails closed). Do NOT decode 1000; treat realized-0 as a conflict signal, not authoritative. Tool:
`scripts/probe_crypto5m_fees.py`; logic: `fee_model.synthesize_fee_status` (gate-compatible
{known,conflict,missing} + confidence). See [[finding-polymarket-crypto5m-fee-conflict]]. **The efficiency
gate runs regardless** — the executable-hurdle uses the fee-EXCLUSIVE ask walk and flags `fee_unknown`; the
forecast/calibration metrics don't need a fee at all.

## Scope guards (binding)

- No live orders, keys, production DB, services, UI. No **real fill** (GPT Pro: not in this phase).
- No broad feature search; no model promotion; no strategy/ROI claim. `LOCKED_BOUNDARY_RULE` stays `None`.
- Calibration alone is NOT a kill test — the gate is checkpoint/regime-specific and three-level.
- Unit of independence = the market WINDOW, never the 12 checkpoint rows. Use window-clustered uncertainty.
- Pre-register checkpoints + which baseline is primary BEFORE looking at the test sample. No checkpoint/
  baseline shopping. Negative controls must NOT show persistent edge.

## 1. Collection campaign (A — runtime, operator-run)

Bounded operator-run continuous collector into a dedicated DB (not the test DB):
```
python3 scripts/run_polymarket_crypto5m_capture.py --continuous \
    --db-path data/crypto5m_campaign.sqlite --max-windows 5000 \
    --discovery-interval-seconds 60 --max-concurrent-windows 2
```
Accumulates Chainlink + Binance obs, both token books, market metadata, fee-source payloads, settlements,
data-quality/gap records, feature snapshots (via offline replay). Reporting milestones (NOT auto-promotion):
200 (boundary cal/holdout), 1,000 (first efficiency report), 2,000 (temporal/regime stability), 5,000
(stronger model-discovery basis). BTC 5m ≈ 288 windows/day → ~3.5 / 6.9 / 17.4 days for 1k / 2k / 5k before
unusable/missing windows. Keep collecting past 200.

## 2. Boundary-rule lock (separate, two-stage — already built in `calibration.two_stage`)

cal = first 100 usable resolved windows; holdout = next 100. Candidate rules `last_at_or_before`,
`first_at_or_after`, `nearest`. Select rule+tolerance on cal only; freeze before opening holdout. Primary
lock: ≥99% of usable holdout windows within $0.01 of Gamma `priceToBeat`, no material systematic bias,
missing-boundary rate measured, every mismatch quarantined. Also report exact Decimal diff + within
$0.0001/$0.001/$0.01 + mean/max abs error. Do NOT pick tolerance after seeing holdout. On fail: keep
`LOCKED_BOUNDARY_RULE=None`, investigate authenticated Chainlink/RTDS delivery, do not loosen. Settlement
audit: when the terminal Chainlink value lies inside the reconstruction-error band around `priceToBeat` →
mark `threshold_resolution_ambiguous` and quarantine from validation.

## 3. Fee-truth probe (B — read-only canonical reconciliation)

A probe CLI samples ≥20 live + several resolved BTC 5m markets, persisting raw payloads. Sources:
- canonical: CLOB `/markets/<cid>` (`fd` if ever present) + `GET /fee-rate?token_id` (`base_fee`).
- secondary confirmation: market-channel WS `new_market.fee_schedule` (targeted capture).
- diagnostic only: Gamma `feesEnabled/feeType/takerBaseFee`, `last_trade_price.fee_rate_bps`.

Synthesis statuses (extends `fee_model`): `known_reconciled` (canonical + WS agree), `known_canonical_only`
(canonical valid, WS unavailable, caveat), `conflict` (canonical vs WS disagree), `missing` (feesEnabled but
no decodable canonical descriptor — the CURRENT live state). When a descriptor with a documented `rate`
appears, reconcile against the published curve `fee = shares · fee_rate · price · (1-price)` (5-dp USDC
rounding) at prices 0.10/0.25/0.50/0.75/0.90. Persist: canonical payload, decoded params, formula/table
reconciliation, explanation of the 1000-vs-0 fields, `fee_status` per market. Real fill only proposed as a
separate operator decision if canonical APIs stay self-contradictory — NOT in this phase.

## 4–8. Market-Efficiency Gate (C — the deliverable code)

New module `sniper/algos/polymarket_crypto5m/market_efficiency.py` (+ a report/CLI). Pure over the captured
DB. Three levels:

**Baselines (per checkpoint):** `up_midpoint`, `down_midpoint`, `normalized_up_midpoint = up/(up+down)`.
Pre-register **raw Up midpoint = primary**, normalized = diagnostic. Keep midpoint (displayed implied prob)
SEPARATE from executable entry (the ask).

**Forecast metrics (per checkpoint):** n windows, Brier, log loss, mean predicted Up, actual Up freq,
calibration intercept/slope, reliability bins, ECE. Evaluated separately by checkpoint / time period / hour
/ day-of-week / volatility regime / distance-to-PriceToBeat regime / spread regime / book-depth regime.
**Window-clustered uncertainty** (day/block bootstrap, temporal splits) — one window = one correlated path,
never 12 independent rows.

**Executable-hurdle report (per side/checkpoint/stake $5/$10/$20):** from the FOK engine — best ask,
depth-weighted executable price, fee-inclusive cost (null when fee unknown), spread, slippage, book age,
fillability; `breakeven_probability = total_cash_cost / acquired_shares`;
`hurdle = breakeven_probability − midpoint_probability`. Report median/p75/p90 hurdle, hurdle by checkpoint/
price-bucket/spread-depth regime, fillable share per stake. May show a forecast edge is economically unusable.

**Gate E1** marginal calibration (descriptive, not a stop). **Gate E2** incremental-information screen with a
tiny predeclared model ladder M0 (midpoint only) → M1 (+norm distance-to-PtB, seconds-remaining, chainlink
realized vol) → M2 (+short chainlink returns, basis) → M3 (+selected book features), temporal-holdout only,
compared on ΔBrier/Δlog-loss/calibration/temporal-stability via a predeclared walk-forward. **Gate E3**
executable economic screen: convert a model's prob to side-specific shadow decisions with actual fee-
inclusive FOK costs, fixed checkpoints/latency, predeclared min edge, paper result on untouched temporal
blocks only. Still diagnostic, not a strategy claim.

**Pre-registered primary checkpoints:** 60 / 180 / 240 / 270 s. Full 15–285 schedule = diagnostic plots
only. No pooled result unless predefined.

**Negative controls:** shuffled settlement labels within temporal blocks; a future-Chainlink feature
deliberately blocked by the leakage audit; random checkpoint policy; random Up/Down side; a lagged
irrelevant feature. The pipeline must not find persistent edge in any control.

## Go / no-go (after ≥1,000 usable windows; not a permanent verdict)

Proceed to full probability-model discovery only if ≥1 predeclared diagnostic model improves Brier or log
loss vs midpoint on untouched temporal data, with no severe calibration deterioration, surviving ≥2 temporal
blocks, with a plausible fee/spread/slippage/latency-adjusted path, not driven by one checkpoint/vol episode.
Pause the static-model path if no predeclared model improves OOS, or improvements vanish after time-blocking,
or the execution hurdle exceeds the forecast improvement, or positive paper vanishes after fee/slippage/
latency, or results concentrate in one regime → route to a separate reframe memo (latency/execution,
selection of abnormal windows, maker economics, cross-venue basis). Do NOT silently turn maker/latency
findings into taker-model success.

## Real `market_resolved` fixture

Keep capturing until a real BTC 5m `market_resolved` arrives; save raw + harden the parser. NOT a blocker
for starting collection or the efficiency code; a readiness item before settlement WS is called verified.

## Deliverables → commit sequence (to main, per operator workflow)

- [x] **#1 — this spec.**
- [x] **#2 — canonical fee probe + reconciliation** (`scripts/probe_crypto5m_fees.py` + `fee_model` synthesis statuses + persistence of raw payloads; live read-only run).
- [x] **#3 — market baseline metrics** (midpoints, Brier/log-loss/calibration/ECE per checkpoint).
- [x] **#4 — executable-hurdle report** (breakeven vs midpoint per side/checkpoint/stake).
- [x] **#5 — temporal/block uncertainty** (window-clustered bootstrap + temporal splits).
- [x] **#6 — diagnostic residual models M0–M3** (walk-forward, ΔBrier/Δlog-loss).
- [x] **#7 — negative controls.**
- [x] **#8 — milestone report/CLI** (E1/E2/E3 + go/no-go at 200/500/1k/2k/5k).
- [x] **#9 — tests + review fixes.**

## Verification

Focused: `python3 -m pytest tests/test_crypto5m_*.py -q`. Then `validate_schema_refs.py` +
`brain_index.py generate`. Bounded read-only live smoke for fee payloads only. No orders/keys/prod-DB/services.
