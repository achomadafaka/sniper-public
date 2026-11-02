# Memecoin V6 Live-Paper Operator Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the isolated V6 live-paper book in the Real Positions tab and expose honest lifecycle reporting and operator censoring without changing funded behavior.

**Architecture:** Add separate read-only V6 API responses and a separate V6 Paper section; never merge mock rows into the funded positions contract. Reports reconstruct metrics from immutable V6 entries/fills/decisions and label quote-only outcomes honestly. No retired-engine comparator, compatibility audit, or replay surface is part of this plan.

**Tech Stack:** Flask routes, SQLite read-only queries, existing vanilla JavaScript UI, pytest, static source guards.

## Global Constraints

- Complete and review the V6 foundation and runtime plans first.
- Read `docs/superpowers/specs/2026-07-26-memecoin-v6-live-paper-engine-design.md` first.
- Work in an isolated worktree created with `superpowers:using-git-worktrees`.
- Use RED-first TDD and one logical commit per task.
- V6 rows remain separate from `/api/memecoin/real/positions`.
- V6 rows expose no funded buy, sell, mirror, override, reservation, or kill-switch controls.
- The only operator mutation is `censored_operator` in the V6 DB with a trimmed reason of at least eight characters.
- Censored positions have no synthetic exit fill and are excluded from engine performance.
- All fills are labeled `modeled_live_quote`, never actual/on-chain.
- No V5 comparison or replay result appears.
- Do not add V5 code, reports, audits, compatibility work, labels, or controls. V6 is the only candidate engine lineage.
- Do not activate the worker, modify production DBs, restart services, push, or deploy.

## File Map

- Create `sniper/services/memecoin_v6_paper_report.py` and its tests.
- Modify `sniper/routes/api_memecoin.py`; create separate V6 API tests.
- Modify `static/app.js` and, only if required, the existing Real Positions template.
- Run the full Knowledge Sync Gate before PR creation.

---

### Task 1: Build Honest V6 Lifecycle Reports

**Files:**
- Create: `sniper/services/memecoin_v6_paper_report.py`
- Test: `tests/test_memecoin_v6_paper_report.py`

**Interfaces:**
- Consumes: read-only V6 ledger rows and `position_timeline`.
- Produces: `list_positions`, `position_detail`, and `summary`.

- [ ] **Step 1: Write failing reconstruction tests**

Build open, closed, unfilled-action, and censored fixtures on both chains. Assert detail returns immutable entry evidence, current inventory, latest executable value/freshness, latest thought, and ordered observation/decision/fill timeline. Every fill has `fill_kind="modeled_live_quote"`; none has a transaction signature.

- [ ] **Step 2: Write failing metric-honesty tests**

Assert summary exposes opened/open/closed/censored/action-unfilled counts by chain; gross PnL; net PnL only when every required cost is known; winner rate; median/worst/worst-decile outcome; drawdown; winner retention; loss containment; time to first action; action count; inventory path; hold duration; quote/capture availability; data-insufficient count; top-winner-removed results; and reason-code outcomes.

Use this unknown-cost assertion:

```python
assert report["net_modeled_pnl_usd"] is None
assert report["net_unavailable_reason"] == "unknown_cost_components"
```

Censored rows must not enter PnL, winner, duration, concentration, or reason-performance denominators.

- [ ] **Step 3: Run RED**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_report.py -q
```

- [ ] **Step 4: Implement reconstructable calculations**

Derive gross PnL from modeled entry cost and modeled exit proceeds. Report route/impact cost and known fees separately. Never coalesce unknown fees to zero. Use closed non-censored lifecycles for outcome metrics; use open plus closed positions for capture/action availability.

- [ ] **Step 5: Run GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_report.py -q
git add sniper/services/memecoin_v6_paper_report.py tests/test_memecoin_v6_paper_report.py
git commit -m "feat: report v6 live-paper outcomes"
```

### Task 2: Add Separate V6 Paper API Endpoints

**Files:**
- Modify: `sniper/routes/api_memecoin.py`
- Test: `tests/test_memecoin_v6_paper_api.py`
- Verify unchanged: files returned by `rg -l '/api/memecoin/real/positions' tests`.

**Interfaces:**
- Consumes: `connect_existing`, report functions, and `censor_position` from the V6 ledger.
- Produces: `GET /api/memecoin/v6-paper/positions`, `GET /api/memecoin/v6-paper/positions/<position_id>`, `GET /api/memecoin/v6-paper/summary`, and `POST /api/memecoin/v6-paper/positions/<position_id>/censor`.

- [ ] **Step 1: Write failing route-contract tests**

Absent/empty V6 DB response:

```json
{
  "mode": "v6_paper",
  "funded": false,
  "fill_label": "modeled_live_quote",
  "positions": []
}
```

The funded route must still succeed. GET endpoints open the V6 DB read-only.

- [ ] **Step 2: Write censor tests**

Require existing memecoin operator auth, an open V6 position, and a trimmed reason of at least eight characters. Censor only the V6 row, write corrected timestamp/reason, create no fill, and return idempotently for the same reason. Closed positions return conflict.

- [ ] **Step 3: Run RED**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_api.py -q
```

- [ ] **Step 4: Implement separate routes**

Do not change the funded endpoint query or response shape. GET routes use `connect_existing()`. The censor route opens only the V6 DB writable through its persistence helper; never pass a production Flask DB connection.

- [ ] **Step 5: Add forbidden-key guards**

Reject these keys from every V6 JSON row:

```python
FORBIDDEN_KEYS = {
    "reservation_id", "tx_sig", "signed_payload", "private_key",
    "manual_sell_url", "mirror_url", "override_url", "kill_switch_url",
}
```

- [ ] **Step 6: Run GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_api.py $(rg -l '/api/memecoin/real/positions' tests | tr '\n' ' ') -q
git add sniper/routes/api_memecoin.py tests/test_memecoin_v6_paper_api.py
git commit -m "feat: expose separate v6 paper api"
```

### Task 3: Render V6 Paper in the Real Positions Tab

**Files:**
- Modify: `static/app.js`
- Modify: the template found by `rg -n 'Real Positions' templates static` only if JavaScript cannot create the container.
- Test: `tests/test_memecoin_v6_paper_ui.py`

**Interfaces:**
- Consumes: Task 2 endpoints.
- Produces: separate V6 Paper section, cards, timeline, and censor confirmation.

- [ ] **Step 1: Write failing UI contract tests**

Assert the tab fetches the funded and V6 endpoints separately, renders `V6 Paper` and `V6 PAPER — NO FUNDS`, and contains none of Buy, Sell, Mirror, Override, Kill switch, `realBuy`, `realSell`, or funded mutation URLs in V6 markup.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_ui.py -q
```

- [ ] **Step 3: Add separate empty/degraded states**

Render funded positions first, then V6. V6 fetch failure says `V6 paper unavailable` only in that section. Empty state says `Waiting for an accepted signal and modeled live fill.`

- [ ] **Step 4: Render lifecycle evidence**

Cards show chain, token, source, modeled entry, age, remaining quantity, executable value/quote freshness, modeled PnL, latest action/thought, and status. Timeline orders observation, decision, action attempt, and modeled fill by corrected UTC. Label entry/exit values `modeled live quote`.

- [ ] **Step 5: Add censor-only control**

Show `Censor paper position` only for open V6 rows. Require browser confirmation plus reason input. POST only to the V6 censor route and reload only the V6 section.

- [ ] **Step 6: Run GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_ui.py -q
git add static/app.js tests/test_memecoin_v6_paper_ui.py
git commit -m "feat: show v6 paper in real positions tab"
```

Include a modified template in the add command only if Step 3 required one.

### Task 4: Verify the Complete Change and Close Knowledge Sync

**Files:**
- Modify: `AGENTS.md`, `CLAUDE.md`, or the V6 design only if implementation changed a documented contract.
- Inspect: `.github/pull_request_template.md`
- Inspect: `docs/superpowers/specs/2026-05-15-branch-close-knowledge-sync-design.md`

**Interfaces:**
- Produces: review evidence and a PR-ready Knowledge Delta Report; no push or PR.

- [ ] **Step 1: Run every V6 focused test**

```bash
python3 -m pytest \
  tests/test_memecoin_v6_engine.py \
  tests/test_memecoin_v6_paper_settings.py \
  tests/test_memecoin_v6_paper_db.py \
  tests/test_memecoin_v6_paper_quotes.py \
  tests/test_memecoin_v6_paper_signals.py \
  tests/test_memecoin_v6_observations.py \
  tests/test_memecoin_v6_paper_runner.py \
  tests/test_memecoin_v6_paper_worker.py \
  tests/test_memecoin_v6_paper_report.py \
  tests/test_memecoin_v6_paper_api.py \
  tests/test_memecoin_v6_paper_ui.py \
  tests/test_memecoin_v6_action_replay.py -q
```

- [ ] **Step 2: Run funded-boundary regression tests**

```bash
rg -l 'MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS|open_real_positions|/api/memecoin/real/positions|memecoin_sol_tape_worker' tests
```

Run the returned non-stress files plus `tests/test_memecoin_runner.py` if it exists. Existing funded behavior and response contracts must remain unchanged.

- [ ] **Step 3: Run static/schema validation**

```bash
python3 scripts/validate_schema_refs.py
python3 scripts/validate_schema_refs.py --warn-unknown-tables
python3 -m compileall -q sniper scripts/memecoin_v6_paper_worker.py
git diff --check
rg -n 'TBD|TODO|implement later|V5.*compar|replay.*V6|actual_fill' \
  sniper/services/memecoin_v6* sniper/persistence/db_memecoin_v6_paper.py \
  scripts/memecoin_v6* static/app.js \
  docs/superpowers/specs/2026-07-26-memecoin-v6-live-paper-engine-design.md
```

Report existing unrelated schema warnings; do not attribute them to this branch. The text scan must find no new placeholder, V5-comparison, replay-goal, or actual-fill claim.

- [ ] **Step 4: Run fixture-only end-to-end smoke**

```bash
python3 scripts/memecoin_v6_paper_worker.py --smoke-no-rpc --once
```

Expected: one Solana and one 4663 fixture lifecycle reconstruct without network, signing, broadcast, or production writes. Do not enable live mode.

- [ ] **Step 5: Complete Knowledge Sync Gate**

Inspect the full branch diff and answer all 11 PR-template questions. Expected delta: implementation of the approved V6 live-paper architecture. Update design/doctrine only for material interface drift. Do not write Supabase memory without explicit operator approval.

- [ ] **Step 6: Request review**

Use `superpowers:requesting-code-review`. Report commit range, exact tests, fixture limits, unchanged funded runtime, and unrun live activation. Do not push or open a PR unless explicitly requested.

## Activation Gate Outside These Plans

After review and merge, live testing needs a separate operator-approved runbook covering merged SHA, V6 DB path/backup, source/tape read-only access, corrected clock, quote adapters, watchdog, UI auth, and rollback. It may enable only V6 paper. It cannot arm funded V6 or change `MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS`.
