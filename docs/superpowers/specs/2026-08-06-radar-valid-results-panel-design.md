# Radar Valid Results Panel Design

**Date:** 2026-08-06  
**Status:** Approved  
**Scope:** The Radar browser surface and its source-state presentation.

## Goal

The Radar page shows one small results panel. The panel shows only valid forward paper results.

## Results Contract

The panel uses `insights.confirmation_microscope.forward_primary`.

It shows these fields:

- the valid result count;
- the average net return;
- the profit-first count;
- the loss-first count;
- the 60-second exit count.

The panel does not show invalid results, pending results, gates, blockers, controls, diagnostics, venues, or entry-test state.

If no valid result exists, the panel shows `No valid paper results yet`.

## Removed Surfaces

The Radar page removes these surfaces:

- `Realistic entry test`;
- the score chip row;
- `Full research detail`;
- all evidence, control, venue, and pattern panels.

The API can retain the source data. This change removes only the rejected browser presentation.

## Source State

One delayed venue must not label the full Radar as partial. The renderer already freezes rows from that affected venue.

The global verdict shows a data gap only when all market sources fail. A single source failure remains a per-source fact.

The API sets the global `stale` field only when all known capture sources fail. It keeps each source grade in `source_health`.

The default Radar page removes the source telemetry strip. This keeps source telemetry out of the primary result surface.

## Scope Guards

- Do not change a Radar rule, result, quote, fill, or outcome.
- Do not change V6 or funded behavior.
- Do not write a production database.
- Do not delete research data.
- Keep the current Radar graphic and launch lists.

## Verification

Focused tests must prove the exact results fields and the removed copy. A source-state test must prove one bad source cannot set the global partial verdict.
