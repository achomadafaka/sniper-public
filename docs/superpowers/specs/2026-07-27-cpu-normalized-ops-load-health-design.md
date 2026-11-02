# CPU-Normalized Ops Load Health Design

**Date:** 2026-07-27
**Status:** Approved
**Scope:** Web operational-health reporting only

## Problem

`GET /api/ops/health` marks `load_1min > 3.0` as `load_high` on every host. The live host exposes 32 usable CPUs, so ordinary concurrent research work around load 5 incorrectly turns the global UI health chip to `WARN`. The live WAL is healthy at 64 MB with no stalled checkpoints; this warning is unrelated to WAL state.

## Design

- Determine usable CPU capacity from `os.sched_getaffinity(0)` when available, falling back to `os.cpu_count()`.
- Warn when one-minute load exceeds 90% of usable CPU capacity. If capacity cannot be measured, preserve the existing `3.0` fallback.
- Return `cpu_count` and `load_warn_threshold` in the operational-health payload so the decision is inspectable.
- Keep WAL size, checkpoint, RAM, swap, disk, and every other health rule unchanged.
- Keep the UI contract unchanged: it consumes the API's explicit issue list and clears `WARN` on its next poll when no issue remains.

## Verification

- Regression: load `5.0` on 32 usable CPUs is healthy.
- Regression: load above the calculated 32-CPU threshold emits `load_high`.
- Regression: unavailable CPU capacity uses the `3.0` fallback.
- Run the focused web health tests, `python3 scripts/wal_health.py`, `node --check static/app.js`, schema validation, and `git diff --check`.
- After deployment, verify `/api/ops/health` reports no issues and the rendered shell chip reads `OK`.

## Rollout and Safety

Restart only the web service after focused verification. Confirm its exact service owner, PID, port, authenticated health payload, and rendered UI after restart. Do not restart or mutate funded, V6 paper, Radar, tape, or database workers. No database writes or funded behavior change.
