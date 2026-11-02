# Solana Volume Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Enforce a 2% m5 organic volume floor only from fresh complete background snapshots.

**Architecture:** Versioned verified swap receipts in the existing pull-tape DB feed an immutable in-process cache. Native and ordinary entries consume it before reservation without I/O. Default OFF.

**Tech Stack:** Python standard library, SQLite, pytest.

**Spec:** docs/superpowers/specs/2026-10-19-solana-volume-cache-design.md

## Global Constraints

Unknown passes. No extra RPC. No entry DB read, lock or wait. Full 300s clean coverage and 5s freshness. Explicit source and own-trade exclusion. No live changes. Tests use -n 2; no full suite without shared host lock.

## Review Focus

Legacy cursor coverage is not decode proof; multi-swap transactions must sum gross events; stale worker publication must expire; migration/pool mismatch stays unknown; fallback cannot bypass the policy refusal.

### Task 1: Producer proof

Files: db_sol_volume.py, memecoin_solana_volume_evidence.py, memecoin_solana_tape.py, tests/test_memecoin_solana_volume_evidence.py.

- [x] Write tests for exact event volume and source keys, clean-window warmup, invalidation on failed/partial/ambiguous decode, legacy compatibility and unsupported venues. Run RED.
- [x] Add receipt persistence and guarded hooks around existing tape passes. Keep existing tick rows unchanged. Run GREEN and existing tape/amount tests.

### Task 2: Background cache

Files: memecoin_solana_volume_cache.py, tests/test_memecoin_solana_volume_cache.py.

- [x] Write tests for known zero/low/high, exact source exclusion, own exclusion, wrong identity, malformed/expired/incomplete coverage and immutable concurrent publication. Run RED.
- [x] Implement bounded read-only refresh and lock-free decisions. Run GREEN.

### Task 3: Wiring and latency

Files: memecoin_slot_lane.py, memecoin_real_runner.py, settings/memecoin.py, runner startup, focused integration tests, scripts/benchmark_solana_volume_gate.py.

- [x] Write tests that real lane refusal happens before reserve/build, unknown passes, ordinary fallback also enforces and disabled means unchanged. Forbid entry SQL/RPC. Run RED.
- [x] Wire shared cache, default-off config and heartbeat. Run GREEN.
- [x] Benchmark hit/miss/expired/refuse/allow and concurrent publication p50/p90 with enabled check p90 <20us. Keep activation OFF.
- [x] Run focused adjacent regressions and fresh independent review. Record runnable commands and results.
