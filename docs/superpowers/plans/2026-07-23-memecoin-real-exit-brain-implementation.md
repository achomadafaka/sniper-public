# Memecoin Real Exit Brain — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the offline → materialization → decision → shadow → replay stack for a causal, explainable memecoin exit engine, log-only, with funded behaviour untouched until an operator canary (Checkpoint A).

**Architecture:** Pure feature library computes a versioned `MarketState` from the recorded swap tape on **block-time** windows; a **standalone-process** materializer (separate entrypoint + watchdog, default OFF — isolated failure domain, never a runner thread) writes compact snapshots; a pure state-machine decision engine proposes actions that map onto existing exit primitives and defers to legacy on any missing-required/stale/untrusted state; a zero-authority shadow logs candidate-vs-legacy divergence; replay reuses the same code. See `docs/superpowers/specs/2026-07-23-memecoin-real-exit-brain-design.md`.

**Tech Stack:** Python 3, SQLite (WAL) — the separate ticks DB `data/memecoin_ticks.db`, `sniper.persistence.db_ticks`, `sniper.analysis.tick_features` patterns, pytest, `scripts/validate_schema_refs.py`.

## Global Constraints

- **Log-only.** No funded rail/env/production-DB/runner behaviour change ships from this plan. `MEMECOIN_EXIT_STATE_ENABLED` and every new worker/flag default **OFF** in code; conftest pins them off.
- **No executable proxy.** `executable_price` is `None` until the fast lane runs under an approved provider. Never fabricate it from mid+haircut. `nominal_price` (mid or §3.3 exec-price) is never labelled executable.
- **Monotonic risk-reduction only.** The candidate may tighten/de-risk/exit; never widen a stop, raise TP, add size, reset peak, or re-enter.
- **One code path.** `compute_state` + `propose` are used identically by materializer, replay, and (later) live. No second backtest implementation.
- **Chain-specific.** Solana = observation-only, forced legacy. 4663 V3 depth = unavailable. Never pool chains into one verdict.
- **Fail-closed.** Missing/stale/ambiguous/untrusted ⇒ non-actionable state ⇒ `DEFER_TO_LEGACY`. RPC error ⇒ skip, never invent.
- **Block-time, not ingest-time.** Time features window on `block_times.block_ts`; missing header ⇒ `DATA_INSUFFICIENT`, never an assumed constant.
- **Zero shadow authority.** No write to funded tables/reservations/rails/overrides/`memecoin_positions` money fields; no nominate/size/sign/approve/broadcast.
- **Namespaces:** module `sniper/services/memecoin_exit_market_state.py` reserved for state assembly; do NOT reuse `memecoin_market_state.py` (entry classification). Tables `memecoin_exit_market_state`, `memecoin_exit_candidate_decisions`, `block_times` (all in the ticks DB).
- **STOP conditions** (ask first): live RPC/API; paid provider; enabling the fast lane/materializer live; production DB writes; `.env` edits; kill switch; funded rails/runner; funded v3; service restart; deploy; canary activation; push/merge/PR.

## File Structure

- `sniper/analysis/exit_state_types.py` — `MarketState` + sub-dataclasses + state enums (pure).
- `sniper/analysis/exit_features.py` — pure feature functions + `compute_state()`. No DB/network.
- `sniper/persistence/db_ticks.py` (modify) — `block_times` table + accessors + `backfill_block_times`.
- `sniper/persistence/db_exit_state.py` — `memecoin_exit_market_state` + `memecoin_exit_candidate_decisions` DDL + writers/readers (ticks DB).
- `sniper/services/memecoin_exit_market_state.py` — MarketState assembly from the tape (glue over `exit_features`).
- `sniper/services/memecoin_exit_brain.py` — `propose()` state machine (pure).
- `sniper/services/memecoin_exit_materializer.py` — per-open-position materialize pass + shadow record.
- `scripts/memecoin_exit_state_worker.py` (new) + `scripts/memecoin_exit_state_watchdog.sh` (new) — standalone, env-gated materializer process (separate failure domain; `memecoin_runner_loop.py` untouched).
- `scripts/memecoin_exit_brain_replay.py` — deterministic replay + pre-registration/sealed-read tooling.
- `sniper/routes/api_*.py` (modify, Phase 6) — read-only observability endpoint.
- `docs/superpowers/plans/2026-07-23-memecoin-real-exit-brain-prereg.md` — pre-registration (Phase 3).
- Tests under `tests/test_exit_*`.

Each phase = one logical commit (or a small commit per task). Run focused suites after each; full non-integration suite before "local complete".

---

## Phase 1 — pure feature library + block-time source

### Task 1: block_times table + fail-closed accessors

**Files:**
- Modify: `sniper/persistence/db_ticks.py`
- Test: `tests/test_exit_block_times.py`

**Interfaces:**
- Produces: `ensure_block_times(c)`; `get_block_ts(c, chain_id, block_no) -> str|None`; `block_ts_map(c, chain_id, blocks) -> dict[int,str]`; `backfill_block_times(c, chain_id, blocks, get_block_fn, now_iso) -> int` where `get_block_fn(block_no) -> ts_int|None`.

- [ ] **Step 1: failing test** — map returns only known blocks; unknown → absent (fail-closed).
```python
def test_block_ts_map_is_fail_closed(tmp_path):
    import sqlite3
    from sniper.persistence import db_ticks as t
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; t.ensure_block_times(c)
    t.backfill_block_times(c, "4663", [100, 101], lambda b: {100: 1_700_000_000, 101: 1_700_000_003}.get(b), "2026-07-23T00:00:00+00:00")
    m = t.block_ts_map(c, "4663", [100, 101, 102])
    assert set(m) == {100, 101}          # 102 unknown -> absent, never guessed
```
- [ ] **Step 2:** `pytest tests/test_exit_block_times.py -v` → FAIL (no `ensure_block_times`).
- [ ] **Step 3:** implement in `db_ticks.py`:
```python
def ensure_block_times(c):
    c.execute("""CREATE TABLE IF NOT EXISTS block_times (
        chain_id TEXT NOT NULL, block_no INTEGER NOT NULL, block_ts TEXT NOT NULL,
        fetched_at TEXT NOT NULL, PRIMARY KEY (chain_id, block_no))"""); c.commit()

def backfill_block_times(c, chain_id, blocks, get_block_fn, now_iso):
    from datetime import datetime, timezone
    have = {r[0] for r in c.execute("SELECT block_no FROM block_times WHERE chain_id=?", (str(chain_id),))}
    rows = []
    for b in sorted({int(x) for x in blocks}) :
        if b in have: continue
        ts = get_block_fn(b)
        if ts is None: continue                          # fail-closed: no header -> no row
        rows.append((str(chain_id), b, datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(), now_iso))
    if rows:
        c.executemany("INSERT OR IGNORE INTO block_times VALUES (?,?,?,?)", rows); c.commit()
    return len(rows)

def block_ts_map(c, chain_id, blocks):
    bl = sorted({int(x) for x in blocks})
    if not bl: return {}
    q = ",".join("?" for _ in bl)
    return {r[0]: r[1] for r in c.execute(
        f"SELECT block_no, block_ts FROM block_times WHERE chain_id=? AND block_no IN ({q})", (str(chain_id), *bl))}

def get_block_ts(c, chain_id, block_no):
    r = c.execute("SELECT block_ts FROM block_times WHERE chain_id=? AND block_no=?", (str(chain_id), int(block_no))).fetchone()
    return r[0] if r else None
```
- [ ] **Step 4:** tests pass.
- [ ] **Step 5 (moved to Task 9):** the `block_times` production writer lives in the **standalone exit-state worker**,
  NOT `_tickstream_evm_worker` — the enrichment shares the materializer's isolated failure domain and only fetches the
  headers the open-position windows actually need. Per pass: collect the unique required block numbers, backfill them
  bounded+batched (capped by `MEMECOIN_EXIT_BLOCKTIME_MAX_PER_TICK`, default 64) and cached-forever, then fail closed on
  any missing header. The header fetch is a **live RPC call → STOP condition**: wired behind an injected `get_block_fn`,
  default OFF, never called in tests or by default. See Task 9.

### Task 2: MarketState types + state enums

**Files:** Create `sniper/analysis/exit_state_types.py`; Test `tests/test_exit_state_types.py`.
**Interfaces:** Produces frozen dataclasses `Provenance`, `MarketState` (fields per spec §4), enum-like `StateLabel` = {`WARMUP`,`TRENDING`,`WEAKENING`,`BREAK_NOMINATED`,`BREAK_CONFIRMED`,`EXIT_PROPOSED`,`DATA_INSUFFICIENT`,`DATA_STALE`,`SOURCE_UNTRUSTED`}. `MarketState.candidate_capable() -> bool`.

- [ ] **Step 1: failing test**
```python
def test_marketstate_none_is_not_zero_and_untrusted_not_capable():
    from sniper.analysis.exit_state_types import MarketState, StateLabel
    s = MarketState(chain_id="solana", mint="X", pool=None, position_id=1, engine_version="v1",
                    feature_version="v1", state=StateLabel.SOURCE_UNTRUSTED, nominal_price=None)
    assert s.nominal_price is None and s.candidate_capable() is False
```
- [ ] **Step 2–4:** implement the dataclasses (`frozen=True`, `None`-defaulted optionals; `candidate_capable()` returns False for `SOURCE_UNTRUSTED`/`DATA_INSUFFICIENT`/`DATA_STALE`/`WARMUP`). Verify pass.
- [ ] **Step 5:** commit `feat(memecoin): MarketState value type + state labels`.

### Task 3: causal pivots + no-lookahead (structure)

**Files:** Create `sniper/analysis/exit_features.py`; Test `tests/test_exit_features_structure.py`.
**Interfaces:** Produces `confirmed_pivots(series, *, confirm) -> list[Pivot]` where `series=[(block_ts, price)]` sorted; a pivot is confirmed only after `confirm` later points — **never uses points after `now`**.

- [ ] **Step 1: failing test** — a swing low is not reported until `confirm` later bars exist; truncating the series to before confirmation hides it (no lookahead).
```python
def test_pivot_requires_confirmation_and_has_no_lookahead():
    from sniper.analysis.exit_features import confirmed_pivots
    s = [(0,1.0),(1,0.8),(2,0.9),(3,1.0)]     # low at idx1
    assert confirmed_pivots(s[:3], confirm=2) == []          # idx1 not yet confirmed at t=2
    piv = confirmed_pivots(s, confirm=2)
    assert any(p.kind=="low" and p.price==0.8 for p in piv)
```
- [ ] **Step 2–4:** implement `confirmed_pivots` scanning only closed points; a low at i confirmed when `confirm` subsequent points are all ≥ it (mirror for highs). Verify.
- [ ] **Step 5:** commit `feat(memecoin): causal confirmed-pivot detector (no lookahead)`.

### Task 4: realized vol + drawdown-velocity (irregular Δt)

**Files:** Modify `exit_features.py`; Test `tests/test_exit_features_vol.py`.
**Interfaces:** Produces `realized_vol(series, *, window_s) -> float|None` (log-return stddev over the block_ts window; `None` if <2 points); `drawdown_velocity(series, exec_peak) -> float|None` (drawdown per second using real Δt, `None` on zero/negative Δt).

- [ ] **Step 1: failing test** — vol clamps to `None` on a single point; drawdown-velocity uses seconds not sample count.
```python
def test_vol_none_on_single_point_and_dd_velocity_uses_time():
    from sniper.analysis.exit_features import realized_vol, drawdown_velocity
    assert realized_vol([(0,1.0)], window_s=300) is None
    v = drawdown_velocity([(0,2.0),(10,1.0)], exec_peak=2.0)   # -50% over 10s
    assert v is not None and v < 0
```
- [ ] **Step 2–4:** implement; guard `Δt<=0`, `price<=0`; clamp non-finite. Verify.
- [ ] **Step 5:** commit `feat(memecoin): realized-vol + time-based drawdown velocity`.

### Task 5: flow (signed net, dust-only wash) + V3 exec-price + capability gating

**Files:** Modify `exit_features.py`; Test `tests/test_exit_features_flow.py`.
**Interfaces:** Produces `pool_flow(swaps, *, quote_side, dust_atomic) -> Flow` (buy_n/sell_n/net_flow_quote/arrival_rate/arrival_accel; **no maker/both-sides exclusion**; `wash_filter="disabled_router_ambiguity"`); `v3_exec_price_path(swaps, *, quote_side) -> [(block_ts, price)]` from `|amount_quote/amount_base|`; `profile_capability(chain_id, pool_kind) -> Capability` (Solana→observation-only; 4663 V3→depth unavailable).

- [ ] **Step 1: failing test** — a single router on both sides is NOT filtered (valid flow retained); dust still filtered; V3 exec price from amounts.
```python
def test_flow_keeps_router_both_sides_and_v3_price_from_amounts():
    from sniper.analysis.exit_features import pool_flow, v3_exec_price_path
    sw = [{"maker":"0xROUTER","amount0":"100","amount1":"-50","block_ts":1},   # buy quote-side0
          {"maker":"0xROUTER","amount0":"-100","amount1":"60","block_ts":2}]    # sell
    f = pool_flow(sw, quote_side=0, dust_atomic=0)
    assert f.buy_n==1 and f.sell_n==1 and f.wash_filter=="disabled_router_ambiguity"
    p = v3_exec_price_path(sw, quote_side=0)
    assert len(p)==2 and all(px>0 for _,px in p)
```
- [ ] **Step 2–4:** implement; keep only dust exclusion. Verify.
- [ ] **Step 5:** commit `feat(memecoin): pool-level signed flow (dust-only) + V3 exec-price path`.

### Task 6: `compute_state` assembly + WARMUP/STALE/UNTRUSTED

**Files:** Modify `exit_features.py`; Create `sniper/services/memecoin_exit_market_state.py`; Test `tests/test_exit_compute_state.py`.
**Interfaces:** Produces `compute_state(swaps, block_times, contract, cfg, *, now_ts) -> MarketState`. `contract` = frozen position dict (chain_id, pool, pool_kind, entry_price, executable_peak, position_id, entry_contract_version). Sets `SOURCE_UNTRUSTED` for Solana / 4663-V3-depth-required; `WARMUP` when <min closed windows; `DATA_STALE` when freshness > bound; `DATA_INSUFFICIENT` when block_ts missing.

- [ ] **Step 1: failing test** — Solana → `SOURCE_UNTRUSTED`, not candidate-capable; missing block_ts → `DATA_INSUFFICIENT`.
```python
def test_solana_untrusted_and_missing_blocktime_insufficient():
    from sniper.services.memecoin_exit_market_state import compute_state
    sol = compute_state([], {}, {"chain_id":"solana","position_id":1}, cfg=None, now_ts=100)
    assert sol.state.name=="SOURCE_UNTRUSTED" and not sol.candidate_capable()
    ev = compute_state([{"block_no":5,"amount0":"1","amount1":"-1","block_ts":None}], {},
                       {"chain_id":"4663","pool_kind":"v3","position_id":2}, cfg=None, now_ts=100)
    assert ev.state.name in ("DATA_INSUFFICIENT","WARMUP")
```
- [ ] **Step 2–4:** implement the assembly, attaching `Provenance` per family. Verify parity determinism (same inputs → identical MarketState).
- [ ] **Step 5:** commit `feat(memecoin): compute_state assembly with explicit non-actionable states`. Run `pytest tests/test_exit_*.py -q`.

---

## Phase 2 — persistence + materializer (1A, 2A)

### Task 7: snapshot + decision tables (ticks DB)

**Files:** Create `sniper/persistence/db_exit_state.py`; Test `tests/test_exit_state_persistence.py`.
**Interfaces:** Produces `ensure_exit_state_tables(c)`; `write_market_state(c, snap) -> int`; `write_candidate_decision(c, rec) -> int`; `latest_state(c, position_id) -> dict|None`. DDL exactly as spec §11 (this connection = ticks DB).

- [ ] **Step 1: failing test** — snapshot upsert is idempotent on `(position_id, computed_at)`; decision row records legacy_action + censored.
- [ ] **Step 2–4:** implement DDL + writers; `INSERT OR IGNORE`, atomic single-commit per row. Verify.
- [ ] **Step 5:** `python3 scripts/validate_schema_refs.py`; commit `feat(memecoin): exit-state + candidate-decision persistence (ticks DB)`.

### Task 8: materialize pass (direct-from-ticks, bounded, read-only)

**Files:** Create `sniper/services/memecoin_exit_materializer.py`; Test `tests/test_exit_materializer.py`.
**Interfaces:** Consumes `compute_state`, `db_exit_state`, `db_ticks`. Produces `materialize_open_positions(mconn, tc, contexts, now_ts, *, limit=None) -> int` — for each **open real** position (bounded by `MEMECOIN_MAX_CONCURRENT`), read its pool ticks in the block-time window from `tc`, `compute_state`, write snapshot to `tc`. Never raises; one bad position never denies the others.

- [ ] **Step 1: failing test** — drives the **production** `db_ticks`/`compute_state` (not a stub): given seeded ticks + block_times + one open 4663 V2 position, exactly one snapshot is written and it is candidate-capable; a Solana open writes a `SOURCE_UNTRUSTED` snapshot.
- [ ] **Step 2–4:** implement; explicit `in_scope`/profile gating mirrored from `memecoin_fastlane.py`. Verify.
- [ ] **Step 5:** commit `feat(memecoin): open-position materialize pass (direct-from-ticks, bounded)`.

### Task 9: standalone materializer process (default OFF)

**Settled 2026-07-23:** the materializer is a **separate process**, NOT a runner thread — feature/RPC/SQLite faults
must not share the funded runner's failure domain (an out-of-process worker cannot stall/OOM the money path).

**Files:** New `scripts/memecoin_exit_state_worker.py` (own entrypoint, `while True` loop) + its watchdog
`scripts/memecoin_exit_state_watchdog.sh`; do **not** modify `scripts/memecoin_runner_loop.py`.
Test `tests/test_exit_materializer_wiring.py`.
**Interfaces:** The process is gated by `MEMECOIN_EXIT_STATE_ENABLED` (default OFF) with its own ticks+ledger
connections; it never imports funded-rail code. A dedicated cron/watchdog supervises it, independent of the runner.

- [ ] **Step 1: failing test** — with the flag off, the worker's `main()` exits immediately without opening
      connections or materializing (assert the gate); with it on, one loop pass calls `materialize_open_positions`
      (patched). conftest pins the flag off.
- [ ] **Step 2–4:** implement the standalone entrypoint (own conns, try/except log-only per pass) + watchdog. Verify.
- [ ] **Step 5:** commit `feat(memecoin): standalone exit-state materializer process (default OFF)`. **CHECKPOINT note:** enabling `MEMECOIN_EXIT_STATE_ENABLED` is a STOP condition (needs block-time RPC budget) — do not flip.

**Activation blocker — CLEARED 2026-07-23 (Task 10).** `realized_vol` was discontinuous (one return used `abs(ret)`, two identical returns gave ~0 vol). It is now the frozen zero-mean RMS estimator `sqrt(Σrᵢ²/n)` — continuous in `n`, reducing exactly to `|r|` at n=1 — frozen as feature `v1` in the pre-registration file and covered by `tests/test_exit_features_vol.py::test_vol_is_continuous_in_n_and_reduces_to_abs_return_at_n1`. Remaining activation gates are the operator STOP conditions only.

---

## Phase 3 — candidate decision engine + pre-registration

### Task 10: pre-registration document (freeze before any outcome read)

**Files:** Create `docs/superpowers/plans/2026-07-23-memecoin-real-exit-brain-prereg.md`.
- [x] Write the frozen structure/flow/vol formulas, window lengths, hard bounds, minimum samples, chain differences, and the Gate-1/2/4 read definitions + decision branches. No outcome has been read. Commit `docs(memecoin): exit-brain pre-registration (frozen)`. **DONE 2026-07-23** — every constant tagged `[inherited]` / `[mechanical]` / `[bound]`; the vol estimator was corrected to continuous RMS before freezing.

### Task 11: `propose()` state machine (pure, defers to legacy)

**Files:** Create `sniper/services/memecoin_exit_brain.py`; Test `tests/test_exit_brain_engine.py`.
**Interfaces:** Produces `propose(contract, state, cfg) -> CandidateDecision(action, size, reason_codes, no_action_reason)` where `action ∈ {"DEFER_TO_LEGACY","TIGHTEN","DERISK","EXIT"}` mapping to existing `_EXECUTABLE_PRICE_REASONS` on EXIT. Non-actionable/untrusted/stale states ⇒ `DEFER_TO_LEGACY`.

- [x] **Step 1: failing tests** — (a) `SOURCE_UNTRUSTED` → `DEFER_TO_LEGACY`; (b) a confirmed structure break with confirming negative net-flow → `EXIT` with reason mapping to `real_trail`/`b_stop`; (c) monotonicity: engine never returns an action that widens a stop or raises TP (property test over random states).
- [x] **Step 2–4:** implement the WARMUP→…→EXIT_PROPOSED machine; vol scales tolerances within hard bounds; catastrophe guard absolute. Verify.
- [x] **Step 5:** commit `feat(memecoin): candidate exit decision state machine (pure, legacy-fallback)`. **DONE 2026-07-23** — 8 tests incl. a 500-case monotonicity property test; module imports only `dataclasses` (provably no DB/network/rail). `DERISK` is registered vocabulary with **no frozen trigger**, so it is never emitted; adding one needs a prereg amendment.

---

## Phase 4 — shadow adapter (zero authority)

### Task 12: shadow record + legacy-action capture

**Files:** Modify `memecoin_exit_materializer.py` (add `record_shadow_decision`); Test `tests/test_exit_shadow.py`.
**Interfaces:** After each snapshot, compute the **legacy action** via the production `alpha_b_real_exit_reason(pos, mid, now_iso, override=..., current_mcap=...)`, call `propose`, and write a `memecoin_exit_candidate_decisions` row (candidate vs legacy, divergent, executable_confirmed=0, censored=0). Read-only.

- [x] **Step 1: failing tests (production seams, not stubs):** (a) ~~spy-connection proxy~~ → **replaced with a STRUCTURAL assertion**: the shadow takes no ledger connection at all, so a spy would have passed vacuously; the signature check is the stronger property; (b) legacy_action equals `alpha_b_real_exit_reason(...)` on the same inputs; (c) divergence flagged when candidate≠legacy. **7 tests, verified red before green.**
- [x] **Step 2–4:** implement. Went further than "ledger connection is read-only": `record_shadow_decision(tc, pos, state, …)` holds **no** ledger handle, so it cannot write a money-path table even by mistake. Verified.
- [x] **Step 5:** commit `feat(memecoin): zero-authority shadow decision logger`. **DONE 2026-07-23.**

- [ ] **OPEN — wiring decision (operator):** `record_shadow_decision` is a tested seam but is deliberately **NOT** called from `materialize_open_positions` yet. The interface says "after each snapshot", but the legacy comparison needs a **`mid`**, and the only value the materializer holds is `nominal_price` — an **atomic quote/base ratio**, not a human-unit mid. Feeding it to `alpha_b_real_exit_reason` would repeat exactly the unit-mixing defect the 07-23 review raised as P0. Options: (a) pass `pos["last_quote_price"]` and record `last_quote_ts` so stale-mid rows can be excluded at read time; (b) wire the shadow only where a fresh mark is already in hand (the runner's sweep). Decide before Gate 2 — a stale or wrong `mid` silently corrupts every `legacy_action` in the read.

---

## Phase 5 — deterministic replay + sealed-read tooling

### Task 13: replay command (same code path; honest limitation labels)

**Files:** Create `scripts/memecoin_exit_brain_replay.py`; Test `tests/test_exit_replay.py`.
**Interfaces:** `replay_position(position_id) -> ReplayResult` reusing `compute_state`+`propose`; labels each result `price_series ∈ {mid_only, executable, missing}`; for the 29 historical positions returns `mid_only` (no tick tape) and **never** synthesizes an executable series.

- [ ] **Step 1: failing tests** — (a) replay/live parity: `compute_state` on the same seeded inputs equals the materializer's snapshot; (b) a historical position with no tick tape yields `price_series="mid_only"` and no executable number; (c) sealed-read accounting excludes censored/ambiguous.
- [ ] **Step 2–4:** implement; the sealed-read function is gated behind an explicit `--sealed` flag and a sentinel, matching `memecoin_postcutover_exit_read.py` discipline. Verify.
- [ ] **Step 5:** commit `feat(memecoin): deterministic exit-brain replay + sealed-read tooling`.

---

## Phase 6 — read-only observability

### Task 14: current-state endpoint

**Files:** Modify a `sniper/routes/api_*.py`; Test `tests/test_exit_state_api.py`.
**Interfaces:** `GET /api/memecoin/exit-state/<position_id>` → latest MarketState + freshness/quality + structure levels + vol + flow (unsupported fields explicitly `unavailable`) + candidate action/reasons + legacy action + divergence + materializer/shadow health. Read-only; no mutation.

- [ ] **Step 1–4:** failing test (200 with the payload; 404 unknown position); implement reading `db_exit_state.latest_state`; verify. **Web restart** needed only if run locally to serve the route (not a funded action).
- [ ] **Step 5:** commit `feat(memecoin): read-only exit-state observability endpoint`.

---

## Phase 7 — funded-canary proposal package (DRAFT ONLY — Checkpoint A)

### Task 15: canary proposal + cutover/rollback runbook

**Files:** Create `docs/superpowers/specs/2026-07-23-memecoin-exit-brain-funded-canary-proposal.md`; `docs/superpowers/plans/2026-07-23-memecoin-exit-brain-cutover-runbook.md`.
- [ ] Draft the versioned funded-canary policy (new policy version + cohort cutover) preserving every invariant in spec §8 Gate 3; specify exact diff, cohort contract, risk cap, rollback trigger, and the `deploy_memecoin_runtime.sh` commands. **Do NOT activate.** Commit `docs(memecoin): funded-canary proposal + cutover/rollback runbook (draft, unactivated)`.
- [ ] **STOP — Checkpoint A:** present the package; funded activation requires explicit operator approval.

---

## Verification & handoff

- After each phase: `pytest tests/test_exit_*.py -q` + `python3 scripts/validate_schema_refs.py`.
- Before "local implementation complete": full non-integration suite `python3 -m pytest tests/ -q` (report exact counts + the known pre-existing `test_ui_algo_tabs` red), `git diff --check`.
- Then `superpowers:requesting-code-review`, `superpowers:verification-before-completion`, and the Knowledge Sync Gate — before any PR (which itself needs approval).

## Self-review (against the spec)
- Spec §3 envelope → Tasks 5,6 (capability gating, disabled features). §3.5 block-time → Task 1. §4 MarketState → Tasks 2,6. §5 architecture → Tasks 7,8,9. §6 engine → Tasks 10,11. §8 Gate 0 tests → every task; Gate 1 → Task 13; Gate 2 → Tasks 9,12 (+ live, gated); Gate 3/4 → Tasks 13,15. §9 observability → Task 14. §10 detector → intentionally NOT planned (future spec). §11 schema → Tasks 1,7. §12 safety → Tasks 9,12. Coverage complete; the only spec section without a build task is §10 by design.
