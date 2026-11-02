# Memecoin Radar and V6 Quant UI Separation Design

**Date:** 2026-08-06  
**Status:** Partially superseded
**Scope:** Operator UI and read-only projections only. No signal, quote, position, exit, or funded behavior changes.

The V6 separation remains active. The Radar entry-test presentation is superseded by
`2026-08-06-radar-valid-results-panel-design.md`.

## Problem

The current operator surface calls the active experiment `Radar v5 → V6` and renders it below the
funded Live book. That name collapses three different identities:

- Radar selection rule: `pump-confirmation-truth-v5`;
- realistic-entry bridge: `radar-paper-adapter-v1` with `route-floor-v1`;
- post-fill quant policy: `v6-position-engine-v2`.

Radar decides which launch qualifies. The adapter measures whether a fresh $25 entry quote is
available. V6 owns the paper position after a filled receipt and decides when to hold, reduce, or
exit. The adapter is plumbing, not an operator product or a new engine version.

The current card also leads with internal telemetry. An operator cannot quickly tell what is being
tested, whether any signal received a realistic paper entry, whether the quant engine has completed
anything, or whether there is evidence to consider funding.

## Decision

Render two separate current-state surfaces.

### Live workspace: V6 Quant Engine

Keep the current exact Radar-origin cohort in the Live workspace, but identify the product as the
post-entry engine:

> **V6 Quant Engine**  
> Paper test — no money  
> After a realistic paper buy, this engine decides when to hold, reduce, or exit.

The default view shows only:

1. experiment state in plain language;
2. open paper positions;
3. completed paper positions;
4. win rate among completed positions, separated by launch mode;
5. gross modeled result by launch mode, explicitly not net when network costs remain unavailable;
6. current position cards: coin, entry, executable return, current action, and plain reason.

Operational open/completed counts may span the current contract, but performance numbers must be
separate for `standard`, `cashback`, and `mayhem`; the UI never publishes a pooled result.

The cohort line reads `Input: Radar-qualified $25 paper buys`. It may expose exact version identity
only in a collapsed technical-details element. It must not use `Radar v5 → V6` as a product name.
It must not render Radar source health, adapter latency, request counts, legacy positions, or old
policy cohorts.

### Radar workspace: Realistic Entry Test

Add a compact current-state block near the top of Launch Opportunity Radar:

> **Realistic entry test**  
> When Radar finds a coin, we request a fresh $25 buy quote. A successful paper buy is handed to
> the V6 Quant Engine. No money is sent.

It shows only:

- `Signals tested` — immutable entry requests;
- `Paper buys` — filled receipts;
- `Could not buy` — terminal unfilled receipts;
- `Awaiting quote` — requests without a terminal receipt;
- a short state sentence such as `Waiting for the next qualifying signal`,
  `1 quote is pending`, or `3 of 8 signals received a realistic paper buy`.

Typed failures are translated for the default view:

| Stored reason | Operator label |
|---|---|
| `capacity_expired` | System was busy |
| `quote_unavailable` | Quote unavailable |
| `no_route` | No buy route |
| `malformed_quote` | Invalid quote |
| `stale_quote` | Quote expired |
| `identity_invalid` | Coin identity invalid |
| `consumer_unavailable` | Quant engine unavailable |

Unknown reasons render as `Other`, while exact codes remain available in the API. The Radar block
does not render V6 positions, exit decisions, engine PnL, old Radar outcomes, or internal version
names.

## Read Model and Data Flow

Radar already owns `op_paper_fill_requests` and `op_paper_fills`. Its read-only API projection gains
a compact `paper_entry_test` object derived from those immutable tables. The browser does not query
the V6 endpoint merely to explain Radar entry coverage.

```text
Radar database ── opportunity-radar API ── Realistic Entry Test in Radar

Radar filled receipt ── read-only cursor ── V6 paper database
                                           └─ V6 Quant Engine in Live
```

The existing V6 cockpit endpoint remains exact-cohort filtered. Its visible copy and metrics change
from adapter telemetry to quant-engine state. No query may pool Radar and Fomo sources or current
and old policy hashes.

## States and Errors

Radar entry state:

- `NOT RUNNING`: any required adapter/consumer enable flag is off;
- `STALE`: enabled, but the consumer heartbeat or Radar source is stale;
- `COLLECTING`: enabled and fresh, including the valid zero-request state.

V6 quant state:

- `NOT RUNNING`: worker disabled;
- `STALE`: worker heartbeat is missing or old;
- `WAITING`: healthy with no open or completed current-cohort position;
- `MANAGING`: at least one current-cohort position is open;
- `RESULTS`: at least one current-cohort position is complete and none is open.

An API failure leaves the last successful block visibly stale. It never substitutes legacy rows or
another source cohort.

## UI Placement and Lifecycle

- `#mc-real` owns the V6 Quant Engine block.
- the Radar section contains a fixed `#mc-radar-paper-entry-test` host.
- the V6 stream runs only while Live/Real is visible.
- the Opportunity Radar stream updates Radar health and `paper_entry_test` while Radar is visible.
- switching workspaces closes the hidden stream.

No new polling loop, endpoint, dependency, chart, or navigation level is added.

## Testing

Targeted tests must prove:

1. Live copy says `V6 Quant Engine` and never `Radar v5 → V6`;
2. Radar contains `Realistic entry test` and the five plain counters;
3. the Radar projection counts requests, fills, unfilled receipts, and pending requests correctly;
4. typed failure codes map to the required operator labels;
5. V6 positions remain exact-current-cohort only;
6. legacy/history copy does not return;
7. stream visibility remains scoped to its owning workspace;
8. a browser snapshot shows the two surfaces in their intended tabs at desktop and narrow width.

Only focused UI, API, report, schema-reference, and smoke checks run. No full test suite.

## Scope Guards

- No funded authority, signer, custody, reservation, kill-switch, runner, or policy mutation.
- No Radar rule, threshold, arm, outcome, quote, or retention change.
- No V6 decision, observation, exit, fill, or lifecycle change.
- No historical deletion. Old rows remain queryable through research surfaces but invisible here.
- No profitability or funding-readiness claim from an empty or thin cohort.
