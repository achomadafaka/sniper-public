# Crypto5m V3.3 — Flow-Scaled Participation (DRAFT, gated)

**Date:** 2026-07-06
**Status:** DRAFT — do NOT implement until both gates below pass.
**Parent spec:** `docs/superpowers/specs/2026-07-06-crypto5m-v33-post-gate-maker-design.md`.
This is a gated sub-lever, not the full V3.3 design.
**Motivating finding (HANDOFF 2026-07-06):** across the control's full history
(n=305 settled windows), fills-per-window vs PnL spearman = +0.30; high-flow
tercile (12+ fills) mean **+$10.5/window**, low/mid-flow **−$4/window**. Thin
flow means the only counterparties are informed (toxic fills); thick two-way
flow is where spread income lives. Vol does NOT explain PnL (spearman ≈ 0,
refuted 2026-07-06) — flow does.

## Idea

Scale maker participation with *observed, real-time, market-wide* taker flow:

```text
flow signal   = trailing print count + notional over the last N seconds
                (last_trade_price events from the capture tape, both tokens —
                 exogenous: ALL market prints, not just our fills)
flow_state    = thin | normal | thick   (bucketed)
action        = flow_state joins the moment context key, so the EXISTING
                arm learner learns participation per flow bucket
                (no hand-tuned "pause if thin" rule — doctrine: learner decides)
```

Engine already reads the tape per tick for print-verified fills
(`live_desk.tape_prints`), so the signal is ~free at runtime.

## Why not now

1. **Attribution:** v3.2r (markout reward) and v3.2f (honest vol floor) are
   mid-experiment. A third live lever before their n≥200 readouts pollutes the
   paired attribution that makes the campaign system meaningful.
2. **Endogeneity:** the motivating stat uses OUR fills as the flow proxy —
   fills are a component of PnL. The lever needs validation on the exogenous
   market-wide print tape, which (until today) was pruned at 2h.

## Gates (both required before implementation)

- **Gate 1 — v3.2 readout:** n ≥ 200 settled paired eval of v3.2r/v3.2f done;
  v3.3 builds on whichever configuration survives (expected: honest floor +
  markout reward combined, if both hold independently).
- **Gate 2 — offline signal validation:** with the 72h print retention
  (watchdog, 2026-07-06), accumulate ≥ 2 days of prints, then test on
  collected data only: does trailing market-wide print flow at time t predict
  the maker's realized PnL for fills after t (per window, per campaign)?
  Pre-register: positive monotone tercile relationship AND low-flow tercile
  materially negative (mean < −0.5c/share), on ≥ 400 window-campaign
  observations. If the
  exogenous signal does not reproduce the fills-based finding, drop the lever
  and record the null.

## Pre-registered eval (when implemented)

Single lever over the surviving baseline; separate campaign/DB; paired
same-window diff vs baseline; kill at CI-upper < 0, n ≥ 200 settled;
watch: fill count (should drop in thin windows), toxic-fill share via 10s
markout in thin vs thick buckets.

## Scope guards

- No changes to running v3.2 campaigns.
- No hand-tuned thresholds — flow buckets learned or percentile-derived from
  the validation data, then frozen.
- Paper only.
