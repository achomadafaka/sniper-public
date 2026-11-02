# Memecoin Opportunity Radar UI Plan

**Goal:** Replace the wallet-centric primary Coin Radar view with the approved circular, event-pushed opportunity surface while preserving the old universe/search as secondary research.

**Architecture:** The Radar loads one snapshot, subscribes to SSE deltas, reconciles periodically, and animates locally. SVG renders venue sectors, five distance zones, sweep arm, trails, and accessible dots. A fact drawer renders exact state and rule explanations. Disconnect freezes motion and marks the surface stale.

**Constraints:** Always show `EXPLORATORY — EDGE NOT YET ESTABLISHED`; no expected-PnL headline; no fake best coin; honest dry-market state; text/icons accompany color; no wallet consensus as primary distance/fire input.

## Tasks

1. Add pure JS projection helpers for zone radius, venue angle, risk color/icon, stale state, and deterministic dot layout.
2. Replace the primary Radar cards with an SVG ship-radar view, source-health strip, closest/inward/new/risk lists, and dry-market copy.
3. Add initial snapshot + `EventSource` delta handling, local trails/animation, periodic snapshot reconciliation, visibility pause/resume, and disconnect freeze.
4. Add a launch drawer for landmark, kinetics, effective buyers, creator behavior, `$25` entry/exit, route, completeness, latency, and exact rule version.
5. Move the old wallet/episode universe into a separate secondary section without changing its read-only API.
6. Verify DOM contract, stale behavior, truth labels, no wallet-primary copy, and manual rendering at desktop/mobile widths.

## Verification

```bash
python3 -m pytest tests/test_memecoin_opportunity_ui_contract.py -q
node --check static/app.js
```
