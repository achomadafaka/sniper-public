# Radar Truth, Churn, and Recovery Design

**Status:** Approved by the 2026-07-28 request to review and immediately repair clear Radar defects.

## Goal

Make the paper-only launch Radar recover its Bags/Solana source, stop repainting a half-megabyte
payload every second, and present one consistent retained-evidence answer above the visualization.

## Boundaries

- Preserve capture-only and paper-only authority. No funded imports, signing, submission, sizing,
  reservations, positions, or funded-runner changes.
- Do not change forward-arm thresholds or promote an arm to funded execution.
- Preserve immutable hot and insight ledgers. This pass changes projection and presentation, not
  historical receipts.
- Keep raw per-launch evidence available through the launch and evidence endpoints.

## Source recovery

Every Solana socket owner starts at most one recovery task for each source it owns after the first
successful subscription. Pump's program socket owns Pump recovery. A Bags shard owns Bags recovery
and passes the same bounded-recovery abandonment callback. Multiple pool subscriptions on one shard
must not start duplicate Bags recovery tasks.

After bounded recovery is exhausted, the existing discardable-gap guard remains authoritative:
decode gaps cannot be discarded; transport/enumeration gaps may quarantine the affected venue epoch
and rebase only onto a raw cursor already present in the ledger.

## Compact live projection

The primary snapshot and SSE stream contain only data rendered by the Board:

- active opportunities;
- summary and source health;
- compact retained insight aggregates;
- at most 16 recent alert rows without their 30-row target matrices.

Archived opportunity rows and raw forward-alert receipts stay available from explicit evidence or
launch-detail reads. The compact snapshot must remain below 150 KiB with a representative 50-alert
fixture.

SSE compares a SHA-256 digest instead of retaining the serialized payload as a so-called
fingerprint. Clock-only ages and heartbeat bookkeeping do not trigger a market repaint. Health-only
refreshes may be sent at a five-second cadence; market changes remain immediate.

## Retained evidence contract

`arm_comparison` becomes the source of truth for all headline counts. Each arm and horizon reports
hits, losses, timeouts, invalid samples, pending samples, scored samples, and hit rate. At 60 seconds
it also reports average final executable return when one exists. Candidate totals and the control
baseline are computed over the full retained ledger, not the newest 50 detail rows.

The newest detail rows remain a receipt list only. They cannot drive the headline verdict, target
ladder, or control denominator.

## Operator layout

The page's single job is to answer two questions before showing the map:

1. Is there a qualified paper entry now?
2. Which forward arm, if any, is separating from ordinary executable routes?

The top strip therefore shows the live decision on the left and the retained 60-second comparison on
the right. The radar plot remains the signature instrument below it. `CAPTURE PARTIAL` names the
affected venue so a Bags/Solana gap is not mistaken for a global stop.

## Validation

- Production-shaped asynchronous test: a Bags shard subscription starts one recovery task and
  receives the real abandonment callback.
- Insight test: retained arm counts include rows outside the recent-detail window and exclude invalid
  rows from scored rates.
- API test: compact payload drops history and per-alert target matrices, stays bounded, and its stream
  digest ignores heartbeat-only churn.
- UI contract and real-browser check: the top strip uses retained aggregates, panels stay open, and
  no full-root rerender occurs.
- Focused Radar pytest files, Python compile, JavaScript syntax, schema-reference validation, and
  `git diff --check`.
