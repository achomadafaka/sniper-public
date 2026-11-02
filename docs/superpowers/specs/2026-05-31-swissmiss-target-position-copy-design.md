# SwissMiss target-position copy — target anatomy + paper strategy design

**Status:** design only. No live API calls, DB writes, service changes, paper deployment, or real-money execution in this spec.
**Subject wallet:** `0xdbade4c82fb72780a0db9a38f821d8671aba9c95` (SwissMiss).
**Purpose:** use the analyzer on the exact target slice we may copy, then deploy a paper-only platform strategy that copies her source-position behavior, not isolated BUY rows.

## 1. Problem

The thesis extractor says SwissMiss's plausible edge is not "copy all SwissMiss." It is narrower:

- sports / public outcome markets;
- held-to-resolution behavior;
- entry prices away from near-certainty;
- excluding her geopolitics / Iran / Middle East loss center.

That still does not tell us which market to buy. Her selection is the edge. The only honest forward test is a filtered copy-paper strategy.

The current platform plugin, `shadow_copy_v1`, copies one qualifying BUY row. That is too shallow for SwissMiss. Her capital deployment may be the signal: probe, add, add again, then commit. A realistic paper copy should observe the source position and enter only after her net commitment crosses a pre-registered threshold.

## 2. Architecture

Build two linked pieces:

1. **Target-position anatomy report**
   - Historical, target-slice-only analyzer pass.
   - Answers how SwissMiss builds positions inside the slice we may copy.
   - Produces one recommended, pre-registered paper trigger plus descriptive alternatives.

2. **`source_position_copy_v1` paper strategy plugin**
   - Platform strategy plugin, paper-only.
   - Reads source-wallet activity observations.
   - Aggregates them into source positions by `(wallet, event_id, condition_id, asset_id/outcome)`.
   - Emits a paper entry only when a source-position threshold crosses.
   - Runs under `strategy_runner` like any other strategy.

Do not overload `shadow_copy_v1`. It is a trade-row copier. This needs a source-position state machine.

## 3. Target Universe

The target-anatomy run is not a full-wallet report. It only uses the candidate copy slice:

- wallet: SwissMiss;
- side: BUY activity only for entry analysis;
- include tags/categories: sports and sports subfamilies, with UFC / CFB / MLB reported separately;
- exclude tags/categories: geopolitics, Middle East, Iran, elections, macro, politics, and other info-sensitive tags;
- entry price floor: default `>= 0.30`;
- unit: condition/outcome asset, with event-level basket warnings where a market/event has multiple linked conditions;
- result: held-to-resolution and sold positions both included, with exit value decomposed.

Tagging must be platform-tag-driven. No hardcoded entity keyword buckets beyond a small parent-rollup list already accepted by the analyzer doctrine.

## 4. Target-Position Anatomy

For every source position in the target universe, compute:

- first BUY timestamp and price;
- source fill count;
- cumulative buy cost over time;
- cumulative sell proceeds over time;
- net source cost over time;
- net shares over time;
- source VWAP over time;
- final source buy cost;
- final net source exposure;
- final realized PnL;
- resolution result;
- sell-vs-hold value;
- whether she averaged up, averaged down, or single-shot entered;
- whether she crossed source-commitment thresholds.

Threshold grid is descriptive:

- source notional thresholds: `$5k`, `$10k`, `$25k`, `$50k`, `$100k`;
- time windows from first buy: `1h`, `6h`, `24h`;
- price drift at crossing vs source VWAP;
- crossing frequency, win rate, ROI, sell-vs-hold value, and median time-to-cross.

The grid is not a forward-selection engine. It describes her behavior and helps pick one simple trigger before paper starts.

## 5. Trigger Selection Rule

The paper trigger must be selected before any forward paper result is observed.

Selection principle:

- prefer behavioral fidelity and enough sample over historical PnL maximization;
- do not pick the threshold that maximizes backtest PnL;
- report alternatives, but deploy one trigger.

Default if the target-anatomy report does not clearly contradict it:

```json
{
  "source_commitment_threshold_usdc": 25000,
  "source_commitment_window_hours": 6,
  "source_commitment_mode": "net_buy_cost",
  "entry_price_min": 0.30,
  "max_threshold_age_minutes": 20,
  "max_price_drift_from_source_vwap_pct": 10
}
```

If `$25k / 6h` rarely triggers in the target slice, choose the lowest threshold with enough historical crossings to make a forward paper test feasible. That choice must be documented in the setup report before deployment.

## 6. Strategy Plugin

New plugin: `source_position_copy_v1`.

Display label for first deployment:

```text
SwissMiss Copy - Filtered
```

Strategy params:

```json
{
  "target_wallets": ["0xdbade4c82fb72780a0db9a38f821d8671aba9c95"],
  "accepted_tags": ["sports", "ufc", "cfb", "mlb"],
  "rejected_tags": ["geopolitics", "middle east", "iran", "elections", "macro", "politics"],
  "entry_price_min": 0.30,
  "source_commitment_threshold_usdc": 25000,
  "source_commitment_window_hours": 6,
  "source_commitment_mode": "net_buy_cost",
  "max_threshold_age_minutes": 20,
  "max_price_drift_from_source_vwap_pct": 10,
  "paper_size_usdc": 25,
  "max_concurrent": 15,
  "exit_policy": "hold_to_resolution",
  "execution_mode": "paper"
}
```

The plugin must reject any non-paper execution mode.

## 7. Source Observation Store

Do not use `shadow_copy_pending_signals` as the canonical store for this plugin. It was designed around individual pending trade rows and `processed/rejected` row states.

Add a source-position observation store with raw-ish activity rows:

- wallet;
- condition_id;
- asset_id;
- outcome;
- side;
- price;
- size;
- usdc_size;
- observed_ts;
- transaction hash or deterministic activity key;
- title;
- event_slug / event_id when available;
- platform tags;
- source endpoint;
- raw_json.

The poller writes observations idempotently. The plugin derives source-position state from observations each cycle and records its own per-strategy copy state.

## 8. Source-Position State Machine

Per strategy and source position:

- `tracking`: target slice passes, but source commitment threshold not crossed;
- `rejected`: target slice fails, stale crossing, conflicting side, basket ambiguity, or drift violation;
- `entered`: paper position emitted once;
- `source_reduced`: source materially sold after entry;
- `source_exited`: source net shares near zero;
- `resolved`: platform paper position closed by resolution.

Idempotency key:

```text
strategy_id | wallet | condition_id | asset_id | trigger_version
```

Never emit more than one paper entry for the same key.

## 9. Entry Logic

For each source position:

1. Apply target-slice filters.
2. Build cumulative source position from ordered observations.
3. Subtract sells from net exposure.
4. Detect first timestamp where `net_buy_cost >= threshold`.
5. Reject if threshold crossing happened outside the configured source-commitment window.
6. Reject if the observation is too stale for a fair paper fill.
7. Reject if current / observed crossing price drifted too far from source VWAP.
8. Enter one fixed-size paper position.

Paper entry price should be the copy-available price at or immediately after threshold crossing. If only source activity price is available, mark the paper fill as `source_price_proxy` in metadata.

## 10. Exit Logic

Run the first deployment as `hold_to_resolution`.

Also support a second strategy instance later:

```text
SwissMiss Copy - Filtered Source Exit
```

That instance follows material source exits:

- if source sells enough to reduce net shares by a configured share, close paper;
- still record hold-to-resolution counterfactual in reports.

Do not mix both policies in one strategy result. They answer different questions.

## 11. Edge Cases

The implementation must handle these explicitly:

- probe buys below threshold;
- multiple adds before threshold;
- partial sells before threshold;
- re-buys after threshold;
- averaging down vs averaging up;
- threshold crossing on a stale observation;
- price jumps after threshold crossing;
- source flips to the opposite side;
- same event with multiple condition IDs;
- negRisk / basket markets;
- duplicate poll rows;
- missing tags;
- missing asset ID;
- SELL-only observations;
- old observations outside max age;
- paper position already open;
- max concurrent reached;
- source exits after paper entry.

When in doubt, reject and log the reason. Do not guess.

## 12. Reports

Target-anatomy report must show:

- included/excluded target universe counts;
- threshold grid summary;
- threshold crossing frequency;
- outcome and ROI by threshold/time window;
- exit value in target slice;
- source sell behavior after threshold;
- recommended paper trigger;
- caveat that historical target anatomy is development data, not proof.

Paper strategy report must show:

- observations seen;
- source positions tracked;
- rejected count by reason;
- threshold crossings;
- paper entries;
- open/resolved positions;
- paper PnL / ROI;
- hold-to-resolution vs source-exit counterfactual when available;
- age of unresolved paper positions.

## 13. Validation Doctrine

The target-anatomy report can choose a trigger, but cannot validate it.

Validation happens only through forward paper:

- no filter changes after deployment without ending the current run;
- no adding missed trades after seeing outcomes;
- no judging before minimum resolved count;
- no deployment language beyond paper.

Minimum reporting thresholds:

- `<20` resolved paper positions: operational readout only;
- `20-79`: screening-grade;
- `>=80`: first meaningful forward read;
- `>=120`: confirmation-grade.

## 14. Testing

Required tests:

- source observations are idempotent;
- target filter accepts sports and rejects geopolitics;
- threshold does not fire on probe buy;
- threshold fires after cumulative adds;
- sells before threshold reduce net commitment;
- stale threshold crossing rejects;
- price drift rejects;
- already-entered state blocks duplicate entry;
- source flip to the opposite outcome rejects for v1;
- hold-to-resolution exit stays open until resolution;
- source-exit policy closes on material source reduction;
- plugin rejects non-paper execution mode;
- strategy registry exposes `source_position_copy_v1`.

## 15. Non-Goals

- No real-money execution.
- No live deployment in the implementation PR.
- No new generic analyzer detector.
- No full-wallet SwissMiss verdict mutation.
- No copying geopolitics / Iran / info-sensitive markets.
- No threshold optimization on forward results.

## 16. Relationship To Existing Work

This replaces the trade-row copy assumption in `2026-05-28-swissmiss-paper-copy-validation.md` with source-position copying. The older study remains useful for forward validation doctrine and kill criteria, but this spec owns the implementation design.

Existing pieces reused:

- strategy platform and `strategy_runner`;
- `shadow_copy_v1` concepts and tests as precedent;
- platform-tag enrichment from the winning-wallet thesis extractor;
- capital/risk and exit-value metrics from Phase 6;
- output-store pattern for durable reports.

Existing pieces not reused directly:

- `shadow_copy_pending_signals` as canonical state;
- `paper_card_enrollments`, because this is a concrete platform strategy, not an abstract thesis-card admission.
