# Hyperliquid Runtime Remediation Design

**Status:** approved for implementation by operator on 2026-07-28

## Problem

The real dry-run leg can remain alive while an SDK request blocks forever. A failed
reconciliation is treated as zero faults, so the same cycle can admit a new mirror.
Read-only web requests open writable SQLite connections and rerun migrations. The paper
runner rescans the same incomplete provider hour and rereads the full funding tape. Public
API calls do not reuse connections. Existing Hyperliquid logs are not rotated. The broad
follow runner burns hundreds of calls per cycle despite sustained negative evidence.

## Contract

- Give every Hyperliquid SDK info request a finite timeout.
- A reconciliation transport failure sets the sticky entry block and is reported separately
  from a clean zero-fault reconciliation.
- The real watchdog requires both a matching process and a fresh heartbeat.
- Every GET route opens `hl_intel.sqlite` read-only and never calls schema migration code.
  POST routes and writer startups remain schema owners.
- Excursion updates do no write when neither extreme changes.
- The paper scanner evaluates each completed provider hour once and imports only tape rows
  newer than its funding cursor.
- Public Hyperliquid HTTP calls reuse one process-local session.
- Edge Desk requests copy summary data without serializing the open copy book it does not
  render.
- Existing logrotate owns Hyperliquid logs.
- Stop and unschedule the disproven broad follow runner. Preserve its database evidence.

## Boundaries

No exchange submission is enabled. `HL_REAL_DRY_RUN=1` remains mandatory at cutover. No
paper evidence, tables, logs, or historical runner files are deleted. Squeeze retention,
universe-policy changes, strategy promotion, and scorecard semantics are not changed because
they require policy choices rather than bug fixes.

## Verification

Regression tests must prove timeout propagation, reconciliation fail-closed behavior,
heartbeat-aware watchdog restart, read-only GET connections, no-op write suppression,
hour/cursor scan gating, session reuse, and the compact copy response. Cutover verification
must check one process per active runner, fresh heartbeats, `dry_run=true`, API responses,
logrotate syntax, and SQLite integrity.
