# Radar Bags CPI and Result UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore honest Bags/Solana capture and make exact paper-entry outcomes immediately readable.

**Architecture:** Keep WebSocket notifications as the live clock, use the Bags feed as a forward-only pool allowlist, then read the already-notified transaction and extract Anchor event-CPI payloads into the existing versioned event decoder. Exact chain data remains event truth. Treat one launch/rule/clip as one immutable paper entry and ignore later quote completions for that identity. Render result rows as stable decision receipts with plain-English state, percentage return, and cost.

**Tech Stack:** Python 3.13, asyncio, SQLite, Solana JSON-RPC, vanilla JavaScript/CSS, pytest.

## Global Constraints

- Paper/capture-only; no signer, funded runner, submission, or production-position imports.
- Bags API scopes shared-DBC pool discovery and audits coverage; only exact chain data creates facts.
- Every code change follows RED then GREEN with focused tests only.
- Do not touch unrelated checkout WIP.

---

### Task 1: Decode current Bags Anchor CPI events

**Files:**
- Modify: `sniper/services/memecoin_opportunity_live.py`
- Modify: `scripts/memecoin_opportunity_worker.py`
- Test: `tests/test_memecoin_opportunity_live_sources.py`

**Interfaces:**
- Produces: `program_cpi_event_logs(transaction, program_id) -> tuple[tuple[int, str], ...]`
- Consumes: the existing `decode_bags_solana_event()` program-data contract.

- [ ] Write a failing fixture test proving a DBC inner instruction with the Anchor event-CPI prefix becomes one `Program data:` event with a unique packed instruction index.
- [ ] Run the focused test and confirm failure because the helper is absent.
- [ ] Implement base58 decoding and strict event-CPI extraction; ignore foreign or malformed instructions.
- [ ] Run the focused test and confirm pass.
- [ ] Write a failing worker/source contract test proving Bags notifications use CPI events while Pump remains on direct logs.
- [ ] Add the paced Bags-only transaction read in live and recovery paths.
- [ ] Run focused live-source, decoder, source, and worker contract tests.

### Task 2: Make exact paper entry creation idempotent

**Files:**
- Modify: `sniper/research/memecoin_opportunity/engine.py`
- Test: `tests/test_memecoin_opportunity_engine.py`

**Interfaces:**
- Consumes: `forward_alert_id(launch_id, rule_version, clip_usd_micros)`.
- Produces: one immutable entry per alert identity; later matching completions return `None`.

- [ ] Write a failing test that submits two valid quote completions for the same launch and asserts the second is ignored without extra entry/snapshot rows.
- [ ] Run the focused test and confirm the current alert/snapshot mismatch failure.
- [ ] Add the minimal early existing-alert guard.
- [ ] Run the focused engine tests and confirm pass.

### Task 3: Render decision-readable result receipts

**Files:**
- Modify: `static/app.js`
- Test: `tests/test_memecoin_opportunity_ui_contract.py`

**Interfaces:**
- Consumes: `insights.alert_evidence` fields already exposed by the read-only API.
- Produces: plain-English verdict, signed percentage, quote cost, and measuring state in stable rows.

- [ ] Write failing UI contract assertions for `Won before stop`, `Lost before target`, `No decisive move`, `Still measuring`, percentage return, and explicit entry cost.
- [ ] Run the focused contract test and confirm failure.
- [ ] Implement semantic row classes and compact two-column receipt copy without replacing the surrounding panel DOM.
- [ ] Run the UI contract test and inspect the real page with Playwright.

### Task 4: Live activation and verification

**Files:**
- Configuration only: `/home/bigpapi/Desktop/Sniper/.env` (secret remains outside Git).

**Interfaces:**
- Consumes: isolated Bags agent API key stored under `~/.config/bags/`.
- Produces: live worker health, Bags audit truth, and readable Radar UI.

- [ ] Run focused Radar tests plus `git diff --check` and the structural no-funded-import guard.
- [ ] Commit explicit changed paths and merge the branch into local `main`.
- [ ] Restart only `scripts/memecoin_opportunity_worker.py` through its watchdog.
- [ ] Verify process identity, database health, source frontiers, Bags captures/audit, RPC/storage metrics, and browser rendering.
