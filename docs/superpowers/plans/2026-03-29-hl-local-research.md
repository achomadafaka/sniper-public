# HL Local Research Implementation Plan

**Goal:** Make the audited Hyperliquid research workflow runnable locally against a
dedicated SQLite DB with explicit artifacts and OI-contract safety.

**Spec:** `docs/superpowers/specs/2026-03-29-hl-local-research-design.md`

## Phase 1: Local Isolation

- [x] Choose a dedicated local research DB path
  - Default: `data/hl_research.db`
- [x] Keep research outputs out of the main runtime DB
- [x] Define a local artifact directory
  - Default: `output/hl_research_local/`

## Phase 2: Script Interface Cleanup

- [x] Add `--db` support to the audited analysis scripts
  - `scripts/hl_research_v2.py`
  - `scripts/hl_regression_analysis.py`
- [x] Add a strict OI mode to the OI backfill path
  - `scripts/backfill_oi_history.py --require-coinalyze`

## Phase 3: Local Bootstrap Entrypoint

- [x] Add `scripts/run_hl_research_local.py`
- [x] Support the following flags:
  - `--db`
  - `--output-dir`
  - `--months`
  - `--assets`
  - `--steps`
  - `--reset-db`
  - `--dry-run`
  - `--require-coinalyze`
- [x] Run steps in a stable order:
  - funding
  - price
  - oi
  - dataset
  - v2
  - combo
  - regression
- [x] Write a manifest for local artifact tracking

## Phase 4: Validation

- [x] Add unit tests for the bootstrap planner
- [x] Keep OI contract tests green
- [ ] Run a full local bootstrap against `data/hl_research.db`
- [ ] Inspect artifact outputs and compare local findings vs the prior VPS audit

## Phase 5: Follow-On Work

- [ ] Add a one-command PM-native backtest bootstrap once the local HL DB path is stable
- [ ] Decide whether to add a repo-level task runner target (`make`, `just`, or shell alias)
- [ ] Optionally document a VPS-sync path for exact reproduction from a copied research DB

