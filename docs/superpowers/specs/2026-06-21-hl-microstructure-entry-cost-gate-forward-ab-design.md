# HL Microstructure — Entry Cost Gate Forward A/B (design)

Status: design only. No live policy change. `fusion_radar` is untouched. This spec
pre-registers a shadow forward A/B before any code is written.

## Why

The evidence audit + MFE/MAE exit replay (commit `9b69ae1`) established, on 2008 closed
`fusion_radar` positions:

- Realized net **−9.5 bps**; median favorable excursion **+10.8 bps**; round-trip taker
  cost **~13 bps** (2×4.5 bps fee + spread).
- **0/18** taker exit-grid configs positive; frictionless upper bound only **+3.5 bps**.
- 0 source/horizon cells survive FDR screening (Student-t, entry-clustered).

Conclusion: exits are not the bottleneck — **median entry edge is below round-trip
cost**. The only remaining policy lever is *entry selectivity* (enter a smaller, higher-
edge subset), not exit tuning. This A/B tests whether such a subset exists and clears
cost, without touching the live lane.

## What stays fixed

- `fusion_radar` keeps running unchanged as the **control (A)**.
- All arms are paper-only, taker-cost-inclusive (existing `PaperEngine`/`_close`), public
  market data only. Maker economics remain a non-evidential sensitivity (see exit replay).
- Persistence schema is unchanged: arms are distinguished by `policy_id` only.
  `PaperEngine.open_position_for` already keys on `(policy_id, asset)`, so one engine
  instance manages all arms with no engine change.

## Arms (pre-registered, frozen at launch)

Run on identical snapshots, in parallel, head-to-head:

- **A — `fusion_radar`** (control): current rule (spread≤15, trade_count_10s≥3, ≥2 any
  source families, confidence≥0.35).
- **B1 — `fusion_gated_b1`** (regime selectivity only): isolates levers 2+3.
- **B2 — `fusion_gated_b2`** (regime + entry cost gate): B1 plus lever 1.

### Lever 2 — regime selectivity (B1, B2)

Enter only when ALL hold (vs A's looser gates):

- `spread_bps <= 8.0` (tight; A allows ≤15).
- `min(bid_depth_5, ask_depth_5) >= 10_000` USDC (deep; matches the audit `deep` bucket).
- `trade_count_10s >= 8` (active; A allows ≥3).

### Lever 3 — demote funding/premium (B1, B2)

The ≥2-source-family entry requirement counts only **fast families** {flow, book,
momentum, expert}. funding/premium may still confirm direction and add to confidence but
**cannot be one of the two required initiators**. Rationale: carry signals are slow and
mis-timed for a 5–30 s microstructure horizon; funding/premium 5s were among the worst
net cells in the audit.

### Lever 1 — entry cost gate (B2 only)

Enter only when `est_edge_bps >= round_trip_cost_bps + buffer_bps`, where:

- `round_trip_cost_bps = 2 * taker_fee_bps + spread_bps` evaluated at the snapshot
  (taker_fee_bps = 4.5 → 9 bps + live spread).
- `buffer_bps = 3.0` (frozen).
- `est_edge_bps` is a **pre-registered, frozen** estimator — NOT fit forward:
  `est_edge_bps = EDGE_K * (confidence - 0.35) * vol_scale`, with
  `EDGE_K = 35.0`, `vol_scale = clamp(realized_vol_60s / 0.004, 0.5, 2.0)`.
  Constants are anchored to the audit MFE distribution (median +10.8 bps at typical
  confidence/vol), then frozen. The estimator is itself a hypothesis: if it is
  uninformative, B2 will be negative and the estimator is refuted — an acceptable,
  falsifiable outcome.

## Eval protocol (pre-registered)

- Milestones: 200 / 500 / 1000 closed positions **per arm**.
- Primary metric: realized net bps per arm (existing taker fee + book-fill cost in
  `_close`), grouped by `policy_id`.
- **Pass bar** (an arm "passes"): `closed >= 200` AND mean net > 0 AND the arm's
  one-sided, entry-clustered Student-t test of `mean net > 0` is significant at α = 0.05
  after Holm correction across the candidate arms (B1, B2) AND no single asset > 40% of
  the arm's PnL AND net positive in ≥ 2 of 3 chronological thirds. (The test statistic is
  a clustered t-stat compared to its t critical value, not a fixed `z` threshold.)
- **Kill**: at the 500 milestone, if an arm's net CI upper bound < 0, stop logging it.
- Reuse the audit's honest inference (one-sided Student-t on entry-clustered effective
  samples) for the significance test; do not pool overlapping forward windows as IID.

## Launch discipline (mirrors btc5m forward v0)

- Freeze all thresholds/constants into a manifest; hash it; record git commit; only then
  start logging arms. No mid-run threshold edits — a change starts a new manifest.
- Operator-gated launch (detached runner or watchdog), paper-only.

## Implementation surface (for the build phase — not built here)

- New pure function `evaluate_fusion_gated_policy(snapshot, context, *, arm)` returning a
  `PolicyDecision` with `policy_id` `fusion_gated_b1` / `fusion_gated_b2`. Reuses
  `source_votes` but filters initiators to fast families and applies the gates above.
- Runner `_run_decisions` fusion path: additionally evaluate B1/B2 and feed each to the
  same `PaperEngine` (one instance; policy_id keys the position). `fusion_radar` decision
  path is byte-for-byte unchanged.
- A/B readout: extend the evidence audit (or a small compare script) to segment summary /
  source-forward / exit metrics by `policy_id`.

## Honesty / risks

- The cost gate's estimator is unproven; B2 may simply refuse most entries. **Zero
  surviving entries is a valid result** (confirms no tradeable subset at these gates).
- Selectivity shrinks sample size — milestones are per-arm so a thin arm just takes longer
  to reach a verdict; we do not lower the bar to compensate.
- Paper fills use the visible book; maker economics stay a labeled sensitivity, never
  evidence. A positive B-arm here is still a *paper* result requiring its own live gate.
