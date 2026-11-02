# Wallet Ledger Slice 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add resumable capture-run and capture-shard persistence, then use it as the base for activity and position ingest.

**Architecture:** Keep capture state deterministic and local-first. This slice builds the run/shard API before any live fetch wiring, then adds observed fact ingestion in small follow-up commits.

**Tech Stack:** Python, DuckDB, pytest.

---

## Task 1: Capture Run And Shard Persistence

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/runs.py`
- Modify: `sniper/analysis/wallet_protocol/ledger/schema.py`
- Test: `tests/test_wallet_ledger_runs.py`

- [ ] Write failing tests for creating capture runs, marking surfaces complete, inserting/updating shards, and selecting the next pending shard without returning completed shards.
- [ ] Run `python3 -m pytest tests/test_wallet_ledger_runs.py -q`; expected failure: module missing.
- [ ] Implement the run/shard API with canonical JSON serialization and wallet normalization.
- [ ] Add cheap CHECK constraints for run/shard statuses.
- [ ] Run `python3 -m pytest tests/test_wallet_ledger_runs.py tests/test_wallet_ledger_schema.py -q`.
- [ ] Commit locally.

## Task 2: Activity Ledger Ingest

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/events.py`
- Test: `tests/test_wallet_ledger_events.py`

- [ ] Write failing tests for deterministic `event_uid`, multi-token row ordinal preservation, raw payload split-out, idempotent insert, and ambiguous dedup retention.
- [ ] Implement offline normalization from Data API activity rows into `wallet_events` and `wallet_event_raw_payloads`.
- [ ] Do not call live APIs in this task.
- [ ] Run focused tests and commit locally.

## Task 3: Position Snapshot Ingest

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/positions.py`
- Test: `tests/test_wallet_ledger_positions.py`

- [ ] Write failing tests proving repeated runs can store the same `position_uid` under different `capture_run_id` values.
- [ ] Implement offline position snapshot normalization and `is_latest` update behavior.
- [ ] Do not call live APIs in this task.
- [ ] Run focused tests and commit locally.
