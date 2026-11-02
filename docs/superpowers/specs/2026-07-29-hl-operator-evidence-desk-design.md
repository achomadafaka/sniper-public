# Hyperliquid Operator Evidence Desk Design

**Date:** 2026-07-29  
**Status:** approved for implementation by the user's explicit fix-and-deploy request  
**Scope:** Hyperliquid paper-research truth and no-cost evidence recovery only

## Problem

The current Edge Desk exposes implementation and archive detail before the decision. Five runtime cells, two evidence receipts, a four-part atlas, weekly diagnostics, and the retained copy ledger compete for attention. The correct conclusion—alt-short is paused—does not dominate the page.

Four evidence debts also lack one structured UI contract:

1. 38 provider symbols have no candle artifact because delisted-tail history is absent.
2. Historical source receive timestamps were never observed.
3. Legacy depth snapshots stop at 15 levels and are lower bounds.
4. The current standalone arm has zero forward closes and zero replicated regimes.

## Decision

Build a compact flight-recorder surface, not another analytics dashboard.

- The first line is the verdict: `ALT-SHORT PAUSED` until every existing promotion gate passes.
- A four-stop evidence runway shows history coverage, source timing, capacity scope, and forward proof.
- Current copy total-book truth and the next falsification step remain visible.
- Historical statistics, runtime diagnostics, concentration, and retained rows move under one collapsed `Audit detail` disclosure.
- No funded controls, strategy mutation, automatic promotion, or profitability language are added.

## Visual System

The subject is an operator evidence console. Its single job is to answer: “Can this lane be trusted yet?”

- **Void:** `#08111a`
- **Panel:** `#0e1b26`
- **Ink:** `#e7eef5`
- **Slate:** `#8ca0b3`
- **Caution:** `#f2b84b`
- **Capture:** `#5ec8e5`

Display text uses the existing compact sans stack with narrow fallbacks. Data uses the existing tabular monospace stack. No fonts, packages, images, gradients, or animation dependencies are added.

The signature element is the evidence runway: four connected stops whose state is `blocked`, `collecting`, or `complete`. Caution is spent only on the paused verdict and blocked stops.

```text
┌──────────────────────────────────────────────────────────────────────┐
│ ALT-SHORT PAUSED                                      PAPER ONLY     │
│ 0 forward closes. Historical tail is censored.                      │
│                                                                      │
│ History ─── Source timing ─── Capacity ─── Forward proof             │
│ 194/232     live capture       provider-book  0/200                  │
│ 38 censored 0 gaps             lower bound    0/2 regimes            │
│                                                                      │
│ COPY CONTROL                 │ WHAT CHANGES THE VERDICT               │
│ total book, open, realized   │ collect closes + two regimes          │
│                                                                      │
│ ▸ Audit detail (history, runtime, ledger, method)                    │
└──────────────────────────────────────────────────────────────────────┘
```

## Evidence-Debt Contract

`GET /api/hyperlive/edge-desk` advances to schema version 3 and adds `evidence_debt`:

```json
{
  "verdict": {"status": "paused", "reason": "partial_censored_and_zero_forward"},
  "history": {
    "status": "blocked",
    "provider_symbols": 232,
    "available_candles": 194,
    "missing_candles": 38,
    "free_archive": "unavailable"
  },
  "source_timing": {
    "status": "collecting",
    "historical_rows": 623,
    "event_time_reconstructed": 0,
    "receive_time_recoverable": 0,
    "forward_live_events": 6,
    "gap_count": 0
  },
  "capacity": {
    "status": "lower_bound",
    "legacy_depth_rows": 743,
    "prospective_capture_format": 2,
    "scope": "all_provider_returned_levels"
  },
  "forward": {
    "status": "collecting",
    "complete_net_closes": 0,
    "proof_target": 200,
    "represented_regimes": 0,
    "regime_target": 2,
    "recorded_hours": 2
  }
}
```

Missing or malformed sources return explicit `unknown` values. The server never converts unavailable evidence to zero.

## No-Cost Remediation

### Delisted tail

Hyperliquid's official archive is requester-pays and does not provide historical candles. It is not used. The sealed provider-exact cache remains censored. The UI exposes exact available/missing counts, and continuous prospective capture replaces the missing era over time. No cross-venue candle substitution is allowed.

### Historical source timing

A bounded backfill reads the public `userFillsByTime` endpoint, which exposes at most the latest 10,000 fills per wallet. It reconstructs only event execution time when one exact provider-symbol, direction, size, and causal-window match exists.

- Cold starts, ambiguous matches, unavailable API windows, and mismatched sizes remain censored.
- `source_received_ts_ms` remains `NULL`; receive and network latency are never reconstructed.
- Reconstructed rows use `source_latency_status='historical_event_reconstructed'`, so latency scorecards continue to exclude them.
- Dry-run is the default; `--apply` is required to mutate the observational ledger.

### Capacity

Legacy 15-level books remain labeled `legacy_client_truncated_15`. Current code stores every provider-returned exact level, up to the provider's current 20-level response. The copy heartbeat advertises capture format 2 and `provider_returned_snapshot`; the UI describes this as a lower bound, never an exact market capacity ceiling. Aggregated books are not substituted for executable exact depth.

### Forward proof

The frozen arm remains running and paper-only. Zero is displayed as zero. Recorded hours, complete-net closes, represented regimes, source gaps, and worker freshness prove whether collection is operating. Filters and milestones do not change to manufacture observations.

## Performance and Failure Behavior

- Edge Desk remains read-only and uses bounded aggregate SQL plus one sealed JSON read.
- The backfill uses the existing shared token bucket, paginates to the provider's 10,000-fill ceiling, and commits per wallet only in apply mode.
- No additional calls run in the 30-second copy loop.
- API schema mismatch fails visibly; the UI retains the last good payload only with a stale banner.
- The default DOM contains no legacy position rows. They render only when `Audit detail` is opened.

## Tests and Deployment

- API fixtures verify schema 3, exact debt counts, malformed seal behavior, and unavailable database behavior.
- Pure tests verify strict historical matching and all ambiguity refusals before the backfill implementation is written.
- UI contract tests verify the paused verdict, four runway stops, collapsed audit detail, and removal of “LIVE EDGE PROOF.”
- Focused Hyperliquid/API tests, schema validation, JavaScript syntax, authenticated HTTP checks, immutable runtime release checks, and live worker/service health are required before completion.

## Non-Goals

- Buying archive access.
- Synthesizing provider candles from another venue.
- Fabricating received timestamps or latency.
- Treating aggregated depth as exact fill depth.
- Relaxing the strategy, adding funded authority, or promoting alt-short.
