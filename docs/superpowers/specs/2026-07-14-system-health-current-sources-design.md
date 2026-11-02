# Current System Health Sources — Design

Status: APPROVED 2026-07-14.

## Problem

The global header reports `stream: FAIL` because `_populate_health()` treats the retired `low_buy_trades` pipeline as a required live stream. The repository contract freezes that pipeline, so an empty table is expected and cannot prove an outage.

The simultaneous `wal: WARN` is valid. `sniper_tracker.db-wal` measured 319 MB against the existing 200 MB UI threshold.

## Decision

- Remove the retired `stream` component from `health_details`. Do not label a stopped legacy pipeline healthy, and do not let it fail current system health.
- Keep browser WebSocket connectivity in `static/app.js` as the UI's live-update signal. It already uses the actual client connection state.
- Keep WAL measurement and its 200 MB warning unchanged.
- Clear the current WAL with the existing `scripts/wal_checkpoint.py` maintenance path. Do not restart services.
- Do not substitute the memecoin runner heartbeat for the retired Polymarket stream. Memecoin already has `/api/memecoin/health`; coupling it into the global proof-bar contract would make unrelated surfaces fail together.

## Data Flow

`/api/system-stats` calls `_populate_health()` and receives only current proof/advisory components. `static/app.js` combines that server health with its own WebSocket connection state. `/api/ops/health` continues to report WAL bloat independently.

## Failure Semantics

- Current proof-required component failure: global health may fail.
- WAL above 200 MB: advisory warning remains visible.
- Retired `low_buy_trades` empty: no health component and no warning.
- Checkpoint remains above its threshold or is blocked: maintenance exits nonzero and the warning stays visible.

## Verification

- A regression test supplies `last_trade_ts=None` and asserts `_populate_health()` does not emit `stream` or fail the proof criterion.
- Existing MCP/dashboard health tests remain green.
- Run the live checkpoint, then verify the WAL file is below 200 MB while the runner log continues advancing.

## Scope

No execution logic, strategy policy, funded-runner configuration, database schema, or service lifecycle changes.
