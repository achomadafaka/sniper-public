# Wallet-Analyzer Phase 3 — Persistence & Mapping (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended)
> or `superpowers:executing-plans` to execute Workstream A task-by-task. Steps use checkbox (`- [ ]`)
> syntax for tracking. Workstreams B–E are coordination scopes for Codex, not Claude TDD steps.

**Goal:** Make the new analyzer write `wallet_layer5_decision` and `analyzer_runs` rows for
every analysis. Land the deterministic mapping from `ProtocolReport` to `action_decision` per
Phase 2 § 4.2. Land the v3 amendments. Land the `position_signal_decision` and
`tag_market_stats` *schemas* (Task A1) plus the `tag_market_stats` writer/reader (Workstream D).
The `position_signal_decision` dual-write loop (paired with `position_market_state`) is
**conditional on Workstream B's column-trace report** (Task A7). If A7 doesn't land in Phase 3,
the schema is created idle and the dual-write moves to the Phase 4 plan; nothing else in Phase
3 depends on per-position-decision rows.

**Architecture:** New code lives in `sniper/analysis/wallet_protocol/` (`persistence.py`,
`wallet_layer5_mapping.py`). Persistence is invoked by `scripts/run_wallet_protocol.py` *after*
`analyze_wallet()` returns — `analyze_wallet()` itself stays a pure function so the mapping is
unit-testable. Schemas land in a new module `sniper/persistence/db_layer5.py` with `ensure_*`
helpers wired into `db.py:init_db()`. The work is split between Claude (analyzer core) and Codex
(research + scripts + tag-analytics). § 9 below.

**Tech stack:** SQLite WAL, `BEGIN IMMEDIATE` per write transaction, JSON1 functions where
helpful, pytest, gamma + Polymarket Data API for tag taxonomy backfill.

**Predecessors:**
- Phase 2 spec: `docs/superpowers/specs/2026-04-28-wallet-analyzer-output-contract.md`
- Migration plan: `docs/superpowers/plans/2026-04-28-wallet-analyzer-migration.md`
- Type source: `sniper/analysis/wallet_protocol/types.py`

---

## 0. Architecture decisions locked in this plan

These bind every workstream below. Changes require revising the plan.

### 0.1 Concurrency policy

**Decision:** Single-process write path with per-write `BEGIN IMMEDIATE` transaction. No dedicated
writer queue.

**Rationale:**
- Analyzer runs are invoked by `run_wallet_protocol.py` (CLI / scheduler) and by future MCP/HTTP
  endpoints (Phase 4). All of them route through the same Python entrypoint.
- SQLite WAL with `BEGIN IMMEDIATE` provides single-writer semantics out of the box. Two analyzer
  processes racing on the same wallet serialize at the SQL layer.
- The Phase 2 § 6.1 `Idempotency-Key` contract collapses duplicate-intent races at the API layer
  before they reach the DB.
- A dedicated queue would be premature — Phase 3 has no concurrent-throughput requirement that
  `BEGIN IMMEDIATE` cannot meet at our scale (~50 wallets × nightly rerun + on-demand).

**Consequence for the persistence layer:**
- The success path (insert `analyzer_runs` row, update `is_current=0` on stale-version rows,
  upsert decision) executes as a single `BEGIN IMMEDIATE … COMMIT` block per spec § 3.1. There
  is **no window** in which a `success=1` analyzer_runs row exists without a matching
  `wallet_layer5_decision` row.
- The failure path (insert `analyzer_runs` row with `success=0`, no decision) executes as its
  own standalone `BEGIN IMMEDIATE … COMMIT` block.
- The connection is opened with both `PRAGMA foreign_keys = ON` (so the FK from
  `wallet_layer5_decision.analyzer_run_id` is enforced) and `PRAGMA busy_timeout = 5000` (so a
  parallel reader's `SELECT` does not bounce a writer with `database is locked`). Both PRAGMAs
  are required by the persistence module; tests assert they are set.
- If the analyzer ever needs multi-process concurrency (it doesn't yet), revisit; that's a new ADR.

### 0.2 `compute_tag_market_stats` persistence

**Decision:** New table `tag_market_stats`, owned by Workstream D, schema landed by Workstream A.

**Rationale:**
- Persisting in `wallet_layer5_decision.tag_grain_summary_json` would couple per-tag stats to a
  per-wallet row → repeated denormalization, JSON parsing on every read.
- Compute-on-read forces every analyzer invocation to re-aggregate market metadata; wasteful when
  the tag distribution changes slowly.
- A dedicated table keeps the data queryable across wallets and lets the analyzer read it as a
  fast lookup.

**Schema** (locked in Workstream A step 5; Workstream D fills it):

```sql
CREATE TABLE IF NOT EXISTS tag_market_stats (
    tag                       TEXT    NOT NULL PRIMARY KEY,
    n_markets                 INTEGER NOT NULL DEFAULT 0,
    n_resolved                INTEGER NOT NULL DEFAULT 0,
    resolved_yes_count        INTEGER NOT NULL DEFAULT 0,
    yes_share                 REAL,
    avg_resolved_price_yes    REAL,
    median_market_volume_24h  REAL,
    top_co_tags_json          TEXT    NOT NULL DEFAULT '[]',
    computed_at               INTEGER NOT NULL,
    inputs_version            TEXT    NOT NULL DEFAULT 'v1'
);
CREATE INDEX IF NOT EXISTS idx_tms_computed_at ON tag_market_stats(computed_at DESC);
```

### 0.3 `position_market_state` column source-list

**Decision:** Source-list is produced by Workstream B (Codex). Workstream A consumes B's output
to define the table schema in step 7. Until B ships its report, A defers step 7.

**Rationale:** The Phase 2 spec § 5 said this explicitly. Splitting `position_signals` correctly
requires column-by-column tracing of the writer; we don't trust any column until it's verified
to be raw market data.

### 0.4 Pure mapping function

**Decision:** `wallet_layer5_mapping.py:map_protocol_report_to_decision(pr)` is a pure function
of `pr`. The persistence layer calls the mapping function and then writes the result. Persistence
and mapping are separate modules.

**Rationale:** Phase 2 § 4.2 requires this. Keeps the mapping unit-testable with fixture vectors.

### 0.5 `analyzer_runs` retention

**Decision:** Append-only in Phase 3. Pruning policy (per Phase 2 § 9 #7 — drop succeeded runs
> 12 months whose decision was superseded; keep failures indefinitely) is implemented in a
follow-up plan, not Phase 3. We don't have the year of data needed yet.

---

## 1. Workstream split

| Workstream | Owner | Scope summary |
|---|---|---|
| Pre-phase plan | Claude | This file. Lock 0.1–0.5. Get user sign-off. |
| **A. Analyzer core** | Claude | `persistence.py` (atomic per spec § 3.1), `wallet_layer5_mapping.py`, `db_layer5.py`, `init_db` wiring, fixture vectors, mapping unit tests (totality + emitted-codes-in-taxonomy + fail-on-call purity), persistence tests, v3 amendments A & B, `wallet_layer5_responder.py` round-trip, `tag_market_stats_reader.py` helpers (Phase-3 minimal — no analyzer behavior change), brain index regen, `run_wallet_protocol.py --persist` wiring. Tasks A1-A13. |
| B. `position_signals` column trace | Codex | Read-only research. Output: `docs/research/scopes/2026-04-29-position-signals-column-validation.md`. |
| C. `tag_nav_category` backfill | Codex | `scripts/sync_polymarket_tags.py` + tests. Idempotent backfill to ≥80% coverage of frequent tags. |
| D. `compute_tag_market_stats` persistence | Codex | Wires `sniper/analysis/tag_analytics.py:compute_tag_market_stats` to write `tag_market_stats` (schema landed by A). |
| E. `signal_history` parquet snapshot | Codex | `scripts/snapshot_signal_history.py` + tests + the snapshot file. |
| F. Backfill | Joint (Claude runs, Codex spot-checks) | After A + B + C, run `run_wallet_protocol.py --persist` over followed + watchlisted wallets (~50). |

**Conflict-prevention rules** (binding):

1. Each file in this repo has exactly one owner per workstream — see §3 file-allow lists.
2. `db.py` and `sniper/persistence/db_*.py` are Claude-only this phase. Codex never writes them.
   If Codex needs a new table, Claude lands the schema in Workstream A first.
3. `sniper/analysis/wallet_protocol/*` is Claude-only. Codex never writes there.
4. `sniper/analysis/tag_analytics.py` is Codex-only this phase. Claude never writes there.
5. `scripts/*` is Codex-only except for `scripts/run_wallet_protocol.py` (Claude — it's the
   analyzer entrypoint).
6. `tests/test_wallet_layer5_*.py`, `tests/fixtures/wallet_layer5_*` are Claude-only.
   `tests/test_sync_polymarket_*.py`, `tests/test_snapshot_signal_history.py`,
   extensions to `tests/test_tag_analytics.py` are Codex-only.
7. Brain index regeneration (`python3 scripts/brain_index.py generate`) is Claude's job at the
   end of Workstream A. Codex does not regen.
8. Foreign WIP rule from `CLAUDE.md`: if `git status` shows files outside the workstream's
   allowed list, park to `wip/foreign-…` immediately, never proceed.
9. One branch per workstream: `feat/phase3-analyzer-core`, `feat/phase3-tag-backfill`,
   `feat/phase3-tag-market-stats`, `feat/phase3-history-snapshot`. B writes a markdown report
   to a research-scope branch (`docs/phase3-position-signals-trace`).

---

## 2. Sequencing

```
[Pre-phase plan] (Claude, this doc)
        │
        ▼
[Codex B: position_signals trace] ──┐
                                    │
[Workstream A starts in parallel    ▼
 with B; consumes B at step 7]    [A continues]
        │                           │
        │                           │
[Workstream C parallel with A]      │
[Workstream E parallel with A]      │
                                    ▼
                                [A merges to main]
                                    │
                                    ▼
                                [Workstream D starts]
                                    │
                                    ▼
                                [D merges]
                                    │
                                    ▼
                                [Workstream F: backfill]
                                    │
                                    ▼
                                [Phase 3 done]
```

---

## 3. Workstream A — Analyzer core (Claude, TDD)

**Files allowed (write):**
- `sniper/analysis/wallet_protocol/persistence.py` (NEW)
- `sniper/analysis/wallet_protocol/wallet_layer5_mapping.py` (NEW)
- `sniper/analysis/wallet_protocol/held_analyst.py` (v3 amendment A)
- `sniper/analysis/wallet_protocol/stage1_gates.py` (v3 amendment B)
- `sniper/persistence/db_layer5.py` (NEW)
- `db.py` (single-line `init_db` additions)
- `scripts/run_wallet_protocol.py` (add `--persist` wiring)
- `tests/test_wallet_layer5_mapping.py` (NEW)
- `tests/test_wallet_layer5_persistence.py` (NEW)
- `tests/test_run_wallet_protocol_persist.py` (NEW, A6 integration smoke)
- `tests/test_wallet_layer5_responder.py` (NEW, A10 round-trip)
- `tests/test_tag_market_stats_reader.py` (NEW, A11 helpers)
- `tests/test_held_analyst_h2_sub080_time_split.py` (NEW, v3-A)
- `tests/test_stage1_gates_f2_promotion.py` (NEW, v3-B)
- `tests/fixtures/wallet_layer5_mapping_vectors.json` (NEW)
- `sniper/analysis/wallet_protocol/wallet_layer5_responder.py` (NEW, A10)
- `sniper/analysis/wallet_protocol/tag_market_stats_reader.py` (NEW, A11)
- `index/schema_registry.json`, `index/dependency_index.json` (regen at end)

**Files forbidden (must not write):**
- `sniper/analysis/tag_analytics.py`, `tag_wallet_pnl.py`, `wallet_tag_fit.py`, `wallet_behavior.py`
- Anything under `scripts/` except `run_wallet_protocol.py`
- `routes/api_*.py`, `mcp_tools/*.py`
- `archive/`, `static/`, `templates/`

### Task A1: scaffold `db_layer5.py` with the three tables

**Files:**
- Create: `sniper/persistence/db_layer5.py`
- Test: `tests/test_wallet_layer5_persistence.py`

- [ ] **Step 1: Write failing schema test**

```python
# tests/test_wallet_layer5_persistence.py
import sqlite3
import pytest
from sniper.persistence.db_layer5 import (
    ensure_wallet_layer5_decision,
    ensure_position_signal_decision,
    ensure_analyzer_runs,
    ensure_tag_market_stats,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_ensure_wallet_layer5_decision_creates_table(conn):
    ensure_wallet_layer5_decision(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(wallet_layer5_decision)")}
    expected = {
        "wallet", "analysis_version", "analyzer_run_id",
        "as_of_ts", "data_window_start_ts", "data_window_end_ts",
        "gate_result", "trading_mode", "overall_verdict",
        "primary_edge_type", "flip_rate",
        "n_closed_positions", "n_held_to_resolution",
        "n_open_positions", "n_open_dead",
        "net_pnl_usdc", "dw_roi_point", "dw_roi_ci_lower",
        "dw_roi_ci_upper", "ew_roi_point",
        "activity_cap_hit", "archetype_layer1",
        "action_decision", "action_reason_codes_json",
        "confidence_band", "is_current",
        "held_branch_summary_json", "flipper_branch_summary_json",
        "tag_grain_summary_json", "capital_ledger_summary_json",
        "data_quality_flags_json", "applicable_limitations_json",
        "open_questions_json", "copyability_note",
        "report_markdown", "created_at", "updated_at",
    }
    assert expected.issubset(cols), expected - cols


def test_ensure_position_signal_decision_creates_table(conn):
    ensure_position_signal_decision(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(position_signal_decision)")}
    assert {"wallet", "condition_id", "outcome", "analysis_version",
            "wallet_action_decision", "primary_tag",
            "tag_fit_score", "tag_fit_rank", "tag_fit_peer_count",
            "tag_fit_confidence_flag", "signal_reason_codes_json",
            "analyzed_ts", "created_at"}.issubset(cols)


def test_ensure_analyzer_runs_creates_table(conn):
    ensure_analyzer_runs(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(analyzer_runs)")}
    assert {"run_id", "analysis_version", "wallet",
            "started_ts", "finished_ts", "duration_ms",
            "success", "error_class", "error_message",
            "invocation_source", "args_json",
            "fetch_quality_flags_json", "activity_rows_fetched",
            "activity_cap_hit", "subgraph_fallback_used",
            "created_at"}.issubset(cols)


def test_ensure_tag_market_stats_creates_table(conn):
    ensure_tag_market_stats(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tag_market_stats)")}
    assert {"tag", "n_markets", "n_resolved", "resolved_yes_count",
            "yes_share", "avg_resolved_price_yes",
            "median_market_volume_24h", "top_co_tags_json",
            "computed_at", "inputs_version"}.issubset(cols)


def test_idempotent_ensure(conn):
    ensure_wallet_layer5_decision(conn)
    ensure_wallet_layer5_decision(conn)  # second call is a no-op


def test_indexes_created(conn):
    ensure_wallet_layer5_decision(conn)
    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='wallet_layer5_decision'")}
    assert "idx_wld_action_current" in idx
    assert "idx_wld_wallet_current" in idx
    assert "idx_wld_run" in idx
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_wallet_layer5_persistence.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sniper.persistence.db_layer5'`

- [ ] **Step 3: Write minimal `db_layer5.py`**

Implement `ensure_wallet_layer5_decision`, `ensure_position_signal_decision`,
`ensure_analyzer_runs`, `ensure_tag_market_stats`. Each function executes the exact
`CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` from Phase 2 § 3.1, § 3.2, § 3.3
and § 0.2 of this plan.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_wallet_layer5_persistence.py -v`
Expected: PASS (5/5).

- [ ] **Step 5: Commit**

```bash
git add sniper/persistence/db_layer5.py tests/test_wallet_layer5_persistence.py
git commit -m "feat(persistence): scaffold wallet_layer5_decision tables with ensure_* helpers"
```

### Task A2: wire `init_db` to call the new ensure_* helpers

**Files:**
- Modify: `db.py` (init_db function, single block addition)

- [ ] **Step 1: Locate `init_db` in `db.py`**

Run: `grep -n "def init_db" db.py`
Expected: one match.

- [ ] **Step 2: Add a failing integration test**

```python
# tests/test_wallet_layer5_persistence.py — append
def test_init_db_creates_layer5_tables(tmp_path):
    import sqlite3
    from db import init_db
    db_path = tmp_path / "t.db"
    init_db(str(db_path))
    conn = sqlite3.connect(str(db_path))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"wallet_layer5_decision", "position_signal_decision",
            "analyzer_runs", "tag_market_stats"}.issubset(tables)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_wallet_layer5_persistence.py::test_init_db_creates_layer5_tables -v`
Expected: FAIL — tables missing.

- [ ] **Step 4: Add the wiring in `db.py`**

In `init_db()`, immediately after the existing `ensure_*` calls in the same function, add:

```python
from sniper.persistence.db_layer5 import (
    ensure_wallet_layer5_decision,
    ensure_position_signal_decision,
    ensure_analyzer_runs,
    ensure_tag_market_stats,
)
ensure_wallet_layer5_decision(conn)
ensure_position_signal_decision(conn)
ensure_analyzer_runs(conn)
ensure_tag_market_stats(conn)
```

(Exact placement: after the last `ensure_*` call already present; before `conn.commit()` if one exists.)

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_wallet_layer5_persistence.py -v`
Expected: PASS (6/6).

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_wallet_layer5_persistence.py
git commit -m "feat(persistence): wire layer5 tables into init_db"
```

### Task A3: build the mapping fixture file

**Files:**
- Create: `tests/fixtures/wallet_layer5_mapping_vectors.json`

- [ ] **Step 1: Define the fixture schema (in-place, no test yet)**

Each vector is an object with:
- `name`: string identifier
- `protocol_report`: a partial `ProtocolReport` dict (only the fields the mapping reads)
- `expected_action_decision`: enum string
- `expected_reason_codes`: list of strings (sorted)
- `expected_confidence_band`: enum string
- `notes`: string explaining what this vector proves

- [ ] **Step 2: Author the fixtures from Phase 2 § 8 #6**

Each fixture vector locks one expected `(action_decision, reason_codes, confidence_band)`
tuple. No vector is allowed to express ambiguity ("high or medium") — every vector pins
stationarity, sample size, and flag inputs precisely so the expected band is unique.

| # | name | inputs (key fields) | expected action_decision | expected confidence_band | proves |
|---|---|---|---|---|---|
| 1 | `validated_copy_clean_held` | gate=pass, verdict=edge_detected, mode=held_analyst_only, primary_edge_type=entry_timing, no flags, H1 supported, stationarity.signaled=False, pre_regime_n=60, no concentration | validated_copy_candidate | high | clean held happy path |
| 2 | `validated_copy_clean_flipper` | gate=pass, verdict=edge_detected, mode=flipper_only, primary_edge_type=flipper_skill, FH1 supported, stationarity.signaled=False, pre_regime_n=60 | validated_copy_candidate | high | TradingMode pass-through (flipper) |
| 3 | `reverse_engineer_mixed_mode` | gate=pass, verdict=edge_detected, mode=mixed_mode, no other downshift | reverse_engineer_candidate | medium | mode_mixed downshift |
| 4 | `reverse_engineer_concentrated` | gate=pass, verdict=edge_detected, independence_check.flag=concentrated on held branch | reverse_engineer_candidate | medium | concentration → reverse_engineer + paired emission |
| 5 | `reverse_engineer_edge_with_anti_pattern` | gate=pass, verdict=edge_detected + F1_sloppy_operator + F2_concentration_top3 | reverse_engineer_candidate | medium | edge_detected overrides anti_pattern |
| 6 | `study_underpowered` | gate=pass, verdict=underpowered, H1 verdict=underpowered with reason=n_below_threshold, no anti-pattern flags | study | underpowered | underpowered routing |
| 7 | `monitor_data_insufficient` | gate=pass, verdict=data_insufficient | monitor | underpowered | data_insufficient routing |
| 8 | `exclude_for_copy_no_edge` | gate=pass, verdict=no_edge, no flags | exclude_for_copy | low | no_edge clean routing |
| 9 | `anti_pattern_no_edge_F1_F2` | gate=pass, verdict=no_edge + F1+F2 | anti_pattern | low | F1_F2_combined → anti_pattern |
| 10 | `anti_pattern_underpowered_F1_F2` | gate=pass, verdict=underpowered + F1+F2 | anti_pattern | low | cross-verdict anti_pattern (defect 4 fix) |
| 11a | `reject_dormant` | gate=reject_dormant | exclude_for_copy | n/a | Stage 1 reject |
| 11b | `reject_insufficient_sample` | gate=reject_insufficient_sample | exclude_for_copy | n/a | Stage 1 reject (third per § 8 #6) |
| 12 | `reject_hedge_archetype` | gate=reject_hedge_archetype | anti_pattern | n/a | Stage 1 reject (anti_pattern severity) |
| 13a | `dw_vs_ew_sign_flip_emit` | gate=pass, verdict=edge_detected, dw_roi_point=-0.04, ew_roi_point=+0.04, primary_edge=entry_timing, stationarity.signaled=False, pre_regime_n=60, no flags | validated_copy_candidate | high | sign-flip emits `dw_vs_ew_divergent` + `dw_negative` without downshift (display-only demotion) |
| 13b | `dw_vs_ew_diverges_no_downshift` | gate=pass, verdict=edge_detected, dw_roi_point=+0.50, ew_roi_point=+0.10, otherwise clean and robust | validated_copy_candidate | high | >2× divergence emits `dw_vs_ew_divergent` without downshift |
| 14 | `stationary_robust_band_drop_low_cusum_n` | gate=pass, verdict=edge_detected, stationarity.signaled=False, **pre_regime_n=10** (below MIN_CUSUM_N=30), H1 supported, no flags | validated_copy_candidate | medium | MIN_CUSUM_N power floor blocks band="high" (laundering fix) |
| 15 | `stationary_signaled_band_drop` | gate=pass, verdict=edge_detected, stationarity.signaled=True, H1 supported | validated_copy_candidate | medium | H7_regime_change_signaled emitted; band="medium" |

Each row in the JSON file populates the input fields literally — no placeholders, no
"depending on". Both `expected_reason_codes` arrays are sorted alphabetically; tests assert
`sorted(actual) == sorted(expected)`.

(The full fixture file lives at `tests/fixtures/wallet_layer5_mapping_vectors.json`. Each vector
gives the dict explicitly; placeholders are not allowed.)

- [ ] **Step 3: Commit fixture file alone (no code yet)**

```bash
git add tests/fixtures/wallet_layer5_mapping_vectors.json
git commit -m "test(layer5): mapping fixture vectors per Phase 2 § 8 #6"
```

### Task A4: build `wallet_layer5_mapping.py` driven by the fixtures

**Files:**
- Create: `sniper/analysis/wallet_protocol/wallet_layer5_mapping.py`
- Test: `tests/test_wallet_layer5_mapping.py`

- [ ] **Step 1: Write the parametrized failing test**

```python
# tests/test_wallet_layer5_mapping.py
import json
from pathlib import Path
import pytest
from sniper.analysis.wallet_protocol.wallet_layer5_mapping import (
    map_protocol_report_to_decision,
    Layer5Decision,
    MIN_CUSUM_N,
)
from sniper.analysis.wallet_protocol.types import ProtocolReport, BranchReport, \
    HypothesisResult, StationarityResult, IndependenceCheckResult

FIXTURES = json.loads(
    Path("tests/fixtures/wallet_layer5_mapping_vectors.json").read_text()
)


def _build_report(d: dict) -> ProtocolReport:
    """Build a ProtocolReport from a fixture dict.
    Helper at module bottom of this test file — decodes nested branches.
    """
    # ... (builds nested dataclasses from dict)
    raise NotImplementedError  # filled in step below


@pytest.mark.parametrize("vec", FIXTURES, ids=lambda v: v["name"])
def test_mapping_vector(vec):
    pr = _build_report(vec["protocol_report"])
    decision = map_protocol_report_to_decision(pr)
    assert decision.action_decision == vec["expected_action_decision"]
    assert sorted(decision.reason_codes) == sorted(vec["expected_reason_codes"])
    assert decision.confidence_band == vec["expected_confidence_band"]


def test_mapping_is_pure_fail_on_call(monkeypatch):
    """Mapping must NOT call sqlite3.connect, os.getenv, os.environ.__getitem__,
    or time.time. Any call raises AssertionError so the test fails loudly."""
    import os, time, sqlite3

    def _trip(*a, **kw):
        raise AssertionError(
            "mapping called a forbidden side-effecting function "
            "(sqlite3.connect / os.getenv / os.environ / time.time)"
        )

    monkeypatch.setattr(sqlite3, "connect", _trip)
    monkeypatch.setattr(os, "getenv", _trip)
    monkeypatch.setattr(time, "time", _trip)
    # os.environ.__getitem__ via a guarded mapping
    class _GuardedEnv(dict):
        def __getitem__(self, key):
            _trip()
        def get(self, key, default=None):
            _trip()
    monkeypatch.setattr(os, "environ", _GuardedEnv())

    # Run every fixture; if any helper reads state, this raises.
    for vec in FIXTURES:
        pr = _build_report(vec["protocol_report"])
        d1 = map_protocol_report_to_decision(pr)
        d2 = map_protocol_report_to_decision(pr)
        assert d1 == d2  # determinism check on top of purity


def test_mapping_totality_5x4():
    """Phase 2 § 8 #2: mapping is total over OverallVerdict × GateResult.
    Every (verdict, gate) cell yields exactly one Layer5Decision (not a raise).
    TradingMode is pass-through and not switched on outside edge_detected."""
    from sniper.analysis.wallet_protocol.types import (
        OverallVerdict, GateResult, ProtocolReport
    )
    overall_values = (
        "edge_detected", "no_edge", "underpowered",
        "data_insufficient", "operator_review",
    )
    gate_values = (
        "pass", "reject_hedge_archetype",
        "reject_insufficient_sample", "reject_dormant",
    )
    n_cells = 0
    for verdict in overall_values:
        for gate in gate_values:
            pr = ProtocolReport(
                wallet="0xtotal",
                gate_result=gate,
                trading_mode="held_analyst_only",
                overall_verdict=verdict,
                primary_edge_type="entry_timing" if verdict == "edge_detected" else None,
            )
            decision = map_protocol_report_to_decision(pr)
            assert decision.action_decision in {
                "validated_copy_candidate", "reverse_engineer_candidate",
                "study", "monitor", "exclude_for_copy", "anti_pattern",
            }, f"{verdict=} {gate=} → {decision.action_decision}"
            assert decision.confidence_band in {"high", "medium", "low", "underpowered", "n/a"}
            n_cells += 1
    assert n_cells == 5 * 4


def test_emitted_codes_in_taxonomy():
    """Phase 2 § 8 #3 (one-way): every reason code emitted by the mapping
    function across the fixture set appears in the spec § 4.4 stable list.

    NOT tested here: the reverse direction (every taxonomy code is
    reachable from at least one fixture). Some codes in § 4.4 — e.g.
    `tag_market_stats_unavailable`, `payback_capitalization_warning`,
    `activity_cap_hit_subgraph_used`, `tag_grain_disagreement_leaf_vs_nav` —
    are emitted upstream by the analyzer pipeline (Layer 3 / 4) before the
    mapping function sees a `ProtocolReport`. The mapping never produces
    them, so a reverse-coverage check would always fail. Reverse coverage
    belongs to a Phase 4 test once the upstream emission paths exist.
    """
    import re
    from pathlib import Path

    spec = Path(
        "docs/superpowers/specs/2026-04-28-wallet-analyzer-output-contract.md"
    ).read_text()
    # Extract ` `code` ` between the §4.4 header and its closing horizontal rule.
    sec_start = spec.index("### 4.4 Reason-code taxonomy")
    sec_end = spec.index("\n---\n", sec_start)
    sec = spec[sec_start:sec_end]
    spec_codes = set(re.findall(r"`([a-zA-Z0-9_:<>]+)`", sec))
    # Strip parametric placeholders like H<n>_underpowered:<reason>
    stable_codes = {c for c in spec_codes if "<" not in c}

    # Run every fixture, collect every emitted code (non-parametric).
    emitted = set()
    for vec in FIXTURES:
        pr = _build_report(vec["protocol_report"])
        d = map_protocol_report_to_decision(pr)
        for code in d.reason_codes:
            # Replace H1_underpowered:n_below_threshold etc. with stem
            if re.match(r"^[FH]+\d*_underpowered:", code):
                continue  # parametric, covered by stem entry in spec
            emitted.add(code)
    # Each emitted code must appear in the spec stable list.
    missing_from_spec = emitted - stable_codes
    assert not missing_from_spec, f"emitted but not in § 4.4: {missing_from_spec}"


def test_min_cusum_n_locked():
    assert MIN_CUSUM_N == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_wallet_layer5_mapping.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `wallet_layer5_mapping.py`**

```python
"""Pure-function mapping from ProtocolReport to operational action decision.

Spec: docs/superpowers/specs/2026-04-28-wallet-analyzer-output-contract.md § 4.2

This module has zero side effects. No DB reads, no env reads, no clock reads.
The single constant MIN_CUSUM_N is module-local and drives _stationary_and_robust.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Final
from sniper.analysis.wallet_protocol.types import ProtocolReport, BranchReport

MIN_CUSUM_N: Final[int] = 30


@dataclass(frozen=True)
class Layer5Decision:
    action_decision: str
    reason_codes: list[str]
    confidence_band: str


# ---------- Helpers (pure) ----------

def _dw_negative(pr: ProtocolReport) -> bool:
    return pr.dw_roi_point is not None and pr.dw_roi_point < 0


def _dw_vs_ew_diverges(pr: ProtocolReport) -> bool:
    """DISPLAY-ONLY descriptor; never authorizes (Phase 2 § 4.2)."""
    if pr.dw_roi_point is None or pr.ew_roi_point is None:
        return False
    if pr.dw_roi_point * pr.ew_roi_point < 0:
        return True
    return abs(pr.dw_roi_point - pr.ew_roi_point) > 2 * max(abs(pr.ew_roi_point), 0.05)


def _active_branches(pr: ProtocolReport) -> list[BranchReport]:
    return [b for b in (pr.held_analyst_branch, pr.flipper_branch) if b is not None]


def _h7_signaled(pr: ProtocolReport) -> bool:
    return any(
        b.stationarity is not None and b.stationarity.signaled
        for b in _active_branches(pr)
    )


def _independence_concentrated(pr: ProtocolReport) -> bool:
    return any(
        b.independence_check is not None and b.independence_check.flag == "concentrated"
        for b in _active_branches(pr)
    )


def _stationary_and_robust(pr: ProtocolReport) -> bool:
    branches = _active_branches(pr)
    if not branches:
        return False  # empty-set guard (per spec § 4.2 helper docstring)
    for b in branches:
        if b.stationarity is None or b.stationarity.signaled:
            return False
        if b.stationarity.pre_regime_n < MIN_CUSUM_N:
            return False
        # primary hypothesis check (H1 for held, FH1 for flipper)
        primary_name = "FH1" if b.branch == "flipper" else "H1"
        primary = next((h for h in b.hypotheses if h.name == primary_name), None)
        if primary is None:
            return False
        if primary.underpowered_reason == "n_below_threshold":
            return False
    return True


def _powered_reason_codes(pr: ProtocolReport) -> list[str]:
    out = []
    for b in _active_branches(pr):
        for h in b.hypotheses:
            if h.verdict == "underpowered" and h.underpowered_reason:
                out.append(f"{h.name}_underpowered:{h.underpowered_reason}")
    return out


def _branch_conflict_codes(pr: ProtocolReport) -> list[str]:
    """Locked vocabulary; emits one code per active branch's primary hypothesis."""
    out = []
    for b in _active_branches(pr):
        primary_name = "FH1" if b.branch == "flipper" else "H1"
        primary = next((h for h in b.hypotheses if h.name == primary_name), None)
        if primary is None:
            continue
        prefix = "flipper_branch" if b.branch == "flipper" else "held_branch"
        if primary.verdict == "supported":
            out.append(f"{prefix}_supported")
        elif primary.verdict == "refuted":
            out.append(f"{prefix}_refuted")
        elif primary.verdict == "inconclusive":
            out.append(f"{prefix}_inconclusive")
        elif primary.verdict == "underpowered":
            out.append(f"{prefix}_underpowered")
    return out


# ---------- Mapping ----------

def map_protocol_report_to_decision(pr: ProtocolReport) -> Layer5Decision:
    # Stage 1 hard rejects (precedence: gate_result dominates verdict)
    if pr.gate_result == "reject_hedge_archetype":
        return Layer5Decision("anti_pattern", ["reject_hedge_archetype"], "n/a")
    if pr.gate_result == "reject_dormant":
        return Layer5Decision("exclude_for_copy", ["reject_dormant"], "n/a")
    if pr.gate_result == "reject_insufficient_sample":
        return Layer5Decision("exclude_for_copy", ["reject_insufficient_sample"], "n/a")

    flags = set(pr.data_quality_flags or [])

    # Caveat reason codes (display-only)
    caveats: list[str] = []
    if _dw_negative(pr):
        caveats.append("dw_negative")
    if _dw_vs_ew_diverges(pr):
        caveats.append("dw_vs_ew_divergent")
    if _h7_signaled(pr):
        caveats.append("H7_regime_change_signaled")
    if "F3_recent_regime" in flags:
        caveats.append("F3_recent_regime")
    if _independence_concentrated(pr):
        caveats.append("independence_concentrated")
        caveats.append("domain_specialization_caveat")

    # Hard anti-pattern combinations (cross-verdict)
    hard_ap: list[str] = []
    if "F1_sloppy_operator" in flags and "F2_concentration_top3" in flags:
        hard_ap.append("F1_F2_combined")
    if "F4_heavy_ctf" in flags and _dw_negative(pr):
        hard_ap.append("F4_with_dw_negative")

    if hard_ap and pr.overall_verdict != "edge_detected":
        return Layer5Decision(
            "anti_pattern",
            [pr.overall_verdict] + caveats + sorted(hard_ap),
            "low",
        )

    # Verdict-specific routing (no edge_detected)
    if pr.overall_verdict == "no_edge":
        return Layer5Decision("exclude_for_copy", ["no_edge"] + caveats, "low")
    if pr.overall_verdict == "underpowered":
        return Layer5Decision(
            "study",
            ["underpowered"] + _powered_reason_codes(pr) + caveats,
            "underpowered",
        )
    if pr.overall_verdict == "data_insufficient":
        return Layer5Decision("monitor", ["data_insufficient"] + caveats, "underpowered")
    if pr.overall_verdict == "operator_review":
        return Layer5Decision(
            "reverse_engineer_candidate",
            ["operator_review"] + _branch_conflict_codes(pr) + caveats,
            "medium",
        )

    # edge_detected
    if pr.overall_verdict == "edge_detected":
        edge_codes = [
            "edge_detected",
            f"primary_edge_type:{pr.primary_edge_type or 'unspecified'}",
        ]
        downshift: list[str] = []
        if "F2_concentration_top3" in flags:
            downshift.append("F2_concentration_top3")
        if "F3_recent_regime" in flags:
            downshift.append("F3_recent_regime")
        if pr.trading_mode == "mixed_mode":
            downshift.append("mode_mixed")
        if _independence_concentrated(pr):
            downshift.append("independence_concentrated")

        if hard_ap:
            return Layer5Decision(
                "reverse_engineer_candidate",
                edge_codes + caveats + sorted(hard_ap),
                "medium",
            )
        if downshift:
            return Layer5Decision(
                "reverse_engineer_candidate",
                edge_codes + caveats + sorted(set(downshift)),
                "medium",
            )

        band = "high" if _stationary_and_robust(pr) else "medium"
        return Layer5Decision("validated_copy_candidate", edge_codes + caveats, band)

    raise NotImplementedError(pr.overall_verdict)
```

- [ ] **Step 4: Fill in the `_build_report` helper in the test file**

The helper translates the fixture's nested dict into `ProtocolReport` + `BranchReport` +
`HypothesisResult` + `StationarityResult` + `IndependenceCheckResult` instances per
`sniper/analysis/wallet_protocol/types.py`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_wallet_layer5_mapping.py -v`
Expected: PASS (14 vectors + 2 invariant tests).

- [ ] **Step 6: Commit**

```bash
git add sniper/analysis/wallet_protocol/wallet_layer5_mapping.py tests/test_wallet_layer5_mapping.py
git commit -m "feat(layer5): pure mapping function with fixture-driven tests"
```

### Task A5: build `persistence.py` — atomic per spec § 3.1

**Files:**
- Create: `sniper/analysis/wallet_protocol/persistence.py`
- Test: extend `tests/test_wallet_layer5_persistence.py`

**Atomicity contract (locked).** Two public write functions, both standalone-atomic:

1. `persist_successful_run_with_decision(conn, *, pr, decision, started_ts, finished_ts,
   duration_ms, invocation_source, args_json, fetch_quality_flags_json, activity_rows_fetched,
   activity_cap_hit, subgraph_fallback_used, analysis_version=DEFAULT_ANALYSIS_VERSION,
   report_markdown="") -> str`. **One** `BEGIN IMMEDIATE … COMMIT` containing:
   - INSERT into `analyzer_runs` (success=1).
   - UPDATE `wallet_layer5_decision SET is_current=0` for the wallet's other versions.
   - INSERT … ON CONFLICT(wallet, analysis_version) DO UPDATE on `wallet_layer5_decision`
     (is_current=1).
   On exception → rollback. The atomicity invariant is: no `success=1` `analyzer_runs` row exists
   without a matching live `wallet_layer5_decision`, and no live decision exists without its
   originating run.
2. `persist_failed_run(conn, *, wallet, started_ts, finished_ts, duration_ms, error_class,
   error_message, invocation_source, args_json, fetch_quality_flags_json,
   activity_rows_fetched, activity_cap_hit, subgraph_fallback_used,
   analysis_version=DEFAULT_ANALYSIS_VERSION) -> str`. **One** `BEGIN IMMEDIATE … COMMIT`
   containing only the `analyzer_runs` insert (success=0). No decision write.

The reader `get_wallet_layer5_decision(conn, wallet)` and the per-position writer
`persist_position_signal_decision(...)` are unchanged (they don't touch the multi-table
invariant).

There is **no** standalone `persist_analyzer_run()` that commits a `success=True` row by
itself — that primitive is what created the prior atomicity defect.

- [ ] **Step 1: Append failing tests**

```python
# tests/test_wallet_layer5_persistence.py — append
import json
import sqlite3
import time
from sniper.analysis.wallet_protocol.persistence import (
    persist_successful_run_with_decision,
    persist_failed_run,
    get_wallet_layer5_decision,
    persist_position_signal_decision,
    DEFAULT_ANALYSIS_VERSION,
)
from sniper.analysis.wallet_protocol.types import ProtocolReport
from sniper.analysis.wallet_protocol.wallet_layer5_mapping import Layer5Decision


@pytest.fixture
def db(tmp_path):
    from db import init_db
    p = tmp_path / "t.db"
    init_db(str(p))
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 5000")
    yield c
    c.close()


def _make_pr(wallet="0xabc"):
    return ProtocolReport(
        wallet=wallet, analyzed_at=int(time.time()),
        overall_verdict="edge_detected", trading_mode="held_analyst_only",
        gate_result="pass", primary_edge_type="entry_timing",
    )


def _make_decision():
    return Layer5Decision(
        "validated_copy_candidate",
        ["edge_detected", "primary_edge_type:entry_timing"],
        "high",
    )


def test_success_path_round_trip(db):
    run_id = persist_successful_run_with_decision(
        db, pr=_make_pr(), decision=_make_decision(),
        started_ts=int(time.time()), finished_ts=int(time.time()),
        duration_ms=100, invocation_source="test",
        args_json="{}", fetch_quality_flags_json="[]",
        activity_rows_fetched=120, activity_cap_hit=False,
        subgraph_fallback_used=False,
    )
    runs = list(db.execute("SELECT * FROM analyzer_runs WHERE run_id=?", (run_id,)))
    assert len(runs) == 1 and runs[0]["success"] == 1
    got = get_wallet_layer5_decision(db, "0xabc")
    assert got is not None and got["is_current"] == 1
    assert got["analyzer_run_id"] == run_id


def test_failure_path_run_only(db):
    run_id = persist_failed_run(
        db, wallet="0xabc", started_ts=int(time.time()),
        finished_ts=int(time.time()), duration_ms=10,
        error_class="ValueError", error_message="boom",
        invocation_source="test", args_json="{}",
        fetch_quality_flags_json="[]", activity_rows_fetched=None,
        activity_cap_hit=None, subgraph_fallback_used=False,
    )
    runs = list(db.execute("SELECT * FROM analyzer_runs WHERE run_id=?", (run_id,)))
    assert len(runs) == 1 and runs[0]["success"] == 0
    assert runs[0]["error_class"] == "ValueError"
    assert get_wallet_layer5_decision(db, "0xabc") is None


def test_atomic_rollback_on_decision_failure(db, monkeypatch):
    """If the decision insert raises mid-transaction, the analyzer_runs row
    must NOT survive. This is the atomicity guarantee."""
    pr = _make_pr()
    decision = _make_decision()

    # Force the decision INSERT to fail by feeding an invalid value into a
    # NOT NULL column via monkeypatch on the helper.
    from sniper.analysis.wallet_protocol import persistence as P
    real_branch_to_json = P._branch_to_json
    def _boom(branch):
        raise RuntimeError("synthetic failure mid-transaction")
    monkeypatch.setattr(P, "_branch_to_json", _boom)

    with pytest.raises(RuntimeError):
        persist_successful_run_with_decision(
            db, pr=pr, decision=decision,
            started_ts=int(time.time()), finished_ts=int(time.time()),
            duration_ms=100, invocation_source="test",
            args_json="{}", fetch_quality_flags_json="[]",
            activity_rows_fetched=120, activity_cap_hit=False,
            subgraph_fallback_used=False,
        )

    monkeypatch.setattr(P, "_branch_to_json", real_branch_to_json)
    runs = list(db.execute("SELECT * FROM analyzer_runs"))
    decisions = list(db.execute("SELECT * FROM wallet_layer5_decision"))
    assert len(runs) == 0, f"analyzer_runs should be empty after rollback, got {len(runs)}"
    assert len(decisions) == 0


def test_version_bump_flips_is_current(db):
    """Old-version row's is_current flips to 0 when new-version row lands."""
    pr = _make_pr()
    d = _make_decision()
    persist_successful_run_with_decision(
        db, pr=pr, decision=d,
        started_ts=int(time.time()), finished_ts=int(time.time()),
        duration_ms=10, invocation_source="t", args_json="{}",
        fetch_quality_flags_json="[]", activity_rows_fetched=0,
        activity_cap_hit=False, subgraph_fallback_used=False,
        analysis_version="wpv2.0",
    )
    persist_successful_run_with_decision(
        db, pr=pr, decision=d,
        started_ts=int(time.time()), finished_ts=int(time.time()),
        duration_ms=10, invocation_source="t", args_json="{}",
        fetch_quality_flags_json="[]", activity_rows_fetched=0,
        activity_cap_hit=False, subgraph_fallback_used=False,
        analysis_version="wpv2.1",
    )
    rows = list(db.execute(
        "SELECT analysis_version, is_current FROM wallet_layer5_decision "
        "WHERE wallet='0xabc' ORDER BY analysis_version"))
    by_v = {r["analysis_version"]: r["is_current"] for r in rows}
    assert by_v == {"wpv2.0": 0, "wpv2.1": 1}


def test_in_place_rerun_same_version_overwrites(db):
    """Rerunning the same version produces ONE row, not three."""
    pr = _make_pr()
    d = _make_decision()
    for _ in range(3):
        persist_successful_run_with_decision(
            db, pr=pr, decision=d,
            started_ts=int(time.time()), finished_ts=int(time.time()),
            duration_ms=10, invocation_source="t", args_json="{}",
            fetch_quality_flags_json="[]", activity_rows_fetched=0,
            activity_cap_hit=False, subgraph_fallback_used=False,
        )
    n = list(db.execute("SELECT COUNT(*) AS c FROM wallet_layer5_decision "
                        "WHERE wallet='0xabc'"))[0]["c"]
    assert n == 1


def test_get_returns_none_for_unknown_wallet(db):
    assert get_wallet_layer5_decision(db, "0xnope") is None


def test_pragmas_enforced(db):
    """Persistence presumes foreign_keys=ON and busy_timeout>=5000."""
    fk = list(db.execute("PRAGMA foreign_keys"))[0][0]
    bt = list(db.execute("PRAGMA busy_timeout"))[0][0]
    assert fk == 1
    assert bt >= 5000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_wallet_layer5_persistence.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `persistence.py`**

```python
"""Persistence layer for wallet_layer5_decision + analyzer_runs.

Two atomic public writers:
  - persist_successful_run_with_decision(): one BEGIN IMMEDIATE that inserts
    the analyzer_runs row (success=1), flips stale is_current flags, and
    upserts the decision. The atomicity invariant: no success=1 run row
    exists without its decision; no live decision exists without its run.
  - persist_failed_run(): one BEGIN IMMEDIATE that inserts a success=0
    analyzer_runs row. No decision write.

Both rely on caller's connection having PRAGMA foreign_keys = ON and
PRAGMA busy_timeout >= 5000. The reader functions return at most one row
filtered on is_current=1 (canonical "latest" query per spec § 3.1).
"""
from __future__ import annotations
import json
import sqlite3
import uuid
from typing import Any, Optional
from sniper.analysis.wallet_protocol.types import ProtocolReport
from sniper.analysis.wallet_protocol.wallet_layer5_mapping import Layer5Decision

DEFAULT_ANALYSIS_VERSION = "wpv2.0"


def _bool_to_int(v: Optional[bool]) -> Optional[int]:
    if v is None:
        return None
    return 1 if v else 0


def persist_successful_run_with_decision(
    conn: sqlite3.Connection,
    *,
    pr: ProtocolReport,
    decision: Layer5Decision,
    started_ts: int,
    finished_ts: int,
    duration_ms: int,
    invocation_source: str,
    args_json: str,
    fetch_quality_flags_json: str,
    activity_rows_fetched: Optional[int],
    activity_cap_hit: Optional[bool],
    subgraph_fallback_used: bool,
    analysis_version: str = DEFAULT_ANALYSIS_VERSION,
    report_markdown: str = "",
) -> str:
    """Atomic insert of analyzer_runs row + flip stale is_current + upsert
    decision. Returns the new run_id. On any exception inside the
    transaction, rolls back (no analyzer_runs row, no decision row,
    no stale-flip side effect)."""
    run_id = str(uuid.uuid4())
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO analyzer_runs (
                run_id, analysis_version, wallet, started_ts, finished_ts,
                duration_ms, success, error_class, error_message,
                invocation_source, args_json, fetch_quality_flags_json,
                activity_rows_fetched, activity_cap_hit, subgraph_fallback_used
            ) VALUES (?, ?, ?, ?, ?, ?, 1, NULL, NULL, ?, ?, ?, ?, ?, ?)""",
            (run_id, analysis_version, pr.wallet, started_ts, finished_ts,
             duration_ms, invocation_source, args_json,
             fetch_quality_flags_json, activity_rows_fetched,
             _bool_to_int(activity_cap_hit), 1 if subgraph_fallback_used else 0),
        )
        conn.execute(
            "UPDATE wallet_layer5_decision SET is_current = 0, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE wallet = ? AND analysis_version != ?",
            (pr.wallet, analysis_version),
        )
        conn.execute(
            """INSERT INTO wallet_layer5_decision (
                wallet, analysis_version, analyzer_run_id,
                as_of_ts, gate_result, trading_mode, overall_verdict,
                primary_edge_type, flip_rate,
                n_closed_positions, n_held_to_resolution,
                n_open_positions, n_open_dead,
                net_pnl_usdc, dw_roi_point, dw_roi_ci_lower,
                dw_roi_ci_upper, ew_roi_point,
                activity_cap_hit, archetype_layer1,
                action_decision, action_reason_codes_json,
                confidence_band, is_current,
                held_branch_summary_json, flipper_branch_summary_json,
                tag_grain_summary_json, capital_ledger_summary_json,
                data_quality_flags_json, applicable_limitations_json,
                open_questions_json, copyability_note,
                report_markdown
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet, analysis_version) DO UPDATE SET
                analyzer_run_id = excluded.analyzer_run_id,
                as_of_ts = excluded.as_of_ts,
                gate_result = excluded.gate_result,
                trading_mode = excluded.trading_mode,
                overall_verdict = excluded.overall_verdict,
                primary_edge_type = excluded.primary_edge_type,
                flip_rate = excluded.flip_rate,
                n_closed_positions = excluded.n_closed_positions,
                n_held_to_resolution = excluded.n_held_to_resolution,
                n_open_positions = excluded.n_open_positions,
                n_open_dead = excluded.n_open_dead,
                net_pnl_usdc = excluded.net_pnl_usdc,
                dw_roi_point = excluded.dw_roi_point,
                dw_roi_ci_lower = excluded.dw_roi_ci_lower,
                dw_roi_ci_upper = excluded.dw_roi_ci_upper,
                ew_roi_point = excluded.ew_roi_point,
                activity_cap_hit = excluded.activity_cap_hit,
                archetype_layer1 = excluded.archetype_layer1,
                action_decision = excluded.action_decision,
                action_reason_codes_json = excluded.action_reason_codes_json,
                confidence_band = excluded.confidence_band,
                is_current = 1,
                held_branch_summary_json = excluded.held_branch_summary_json,
                flipper_branch_summary_json = excluded.flipper_branch_summary_json,
                tag_grain_summary_json = excluded.tag_grain_summary_json,
                capital_ledger_summary_json = excluded.capital_ledger_summary_json,
                data_quality_flags_json = excluded.data_quality_flags_json,
                applicable_limitations_json = excluded.applicable_limitations_json,
                open_questions_json = excluded.open_questions_json,
                copyability_note = excluded.copyability_note,
                report_markdown = excluded.report_markdown,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                pr.wallet, analysis_version, run_id,
                finished_ts, pr.gate_result, pr.trading_mode, pr.overall_verdict,
                pr.primary_edge_type, pr.flip_rate,
                pr.n_closed_positions, pr.n_held_to_resolution,
                pr.n_open_positions, pr.n_open_dead,
                pr.net_pnl, pr.dw_roi_point, pr.dw_roi_ci_lower,
                pr.dw_roi_ci_upper, pr.ew_roi_point,
                1 if pr.activity_cap_hit else 0, pr.archetype_layer1,
                decision.action_decision,
                json.dumps(decision.reason_codes),
                decision.confidence_band,
                _branch_to_json(pr.held_analyst_branch),
                _branch_to_json(pr.flipper_branch),
                _tag_grain_to_json(pr),
                _capital_ledger_to_json(pr.capital_ledger),
                json.dumps(pr.data_quality_flags or []),
                json.dumps(pr.applicable_limitations or []),
                json.dumps(pr.open_questions or []),
                pr.copyability_note,
                report_markdown,
            ),
        )
        conn.commit()
        return run_id
    except Exception:
        conn.rollback()
        raise


def persist_failed_run(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    started_ts: int,
    finished_ts: int,
    duration_ms: int,
    error_class: str,
    error_message: str,
    invocation_source: str,
    args_json: str,
    fetch_quality_flags_json: str,
    activity_rows_fetched: Optional[int],
    activity_cap_hit: Optional[bool],
    subgraph_fallback_used: bool,
    analysis_version: str = DEFAULT_ANALYSIS_VERSION,
) -> str:
    """Standalone txn: insert analyzer_runs with success=0. No decision write."""
    run_id = str(uuid.uuid4())
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO analyzer_runs (
                run_id, analysis_version, wallet, started_ts, finished_ts,
                duration_ms, success, error_class, error_message,
                invocation_source, args_json, fetch_quality_flags_json,
                activity_rows_fetched, activity_cap_hit, subgraph_fallback_used
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, analysis_version, wallet, started_ts, finished_ts,
             duration_ms, error_class, error_message[:500] if error_message else None,
             invocation_source, args_json, fetch_quality_flags_json,
             activity_rows_fetched, _bool_to_int(activity_cap_hit),
             1 if subgraph_fallback_used else 0),
        )
        conn.commit()
        return run_id
    except Exception:
        conn.rollback()
        raise


def get_wallet_layer5_decision(
    conn: sqlite3.Connection, wallet: str
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM wallet_layer5_decision "
        "WHERE wallet = ? AND is_current = 1",
        (wallet,),
    ).fetchone()
    return dict(row) if row else None


def persist_position_signal_decision(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    condition_id: str,
    outcome: str,
    analysis_version: str,
    wallet_action_decision: str,
    primary_tag: Optional[str],
    tag_fit_score: Optional[float],
    tag_fit_rank: Optional[int],
    tag_fit_peer_count: Optional[int],
    tag_fit_confidence_flag: Optional[str],
    signal_reason_codes_json: str,
    analyzed_ts: int,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO position_signal_decision (
            wallet, condition_id, outcome, analysis_version,
            wallet_action_decision, primary_tag,
            tag_fit_score, tag_fit_rank, tag_fit_peer_count,
            tag_fit_confidence_flag, signal_reason_codes_json,
            analyzed_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (wallet, condition_id, outcome, analysis_version,
         wallet_action_decision, primary_tag,
         tag_fit_score, tag_fit_rank, tag_fit_peer_count,
         tag_fit_confidence_flag, signal_reason_codes_json,
         analyzed_ts),
    )
    conn.commit()


# ---------- JSON helpers ----------

def _branch_to_json(branch) -> str:
    if branch is None:
        return "{}"
    return json.dumps({
        "branch": branch.branch,
        "is_active": branch.is_active,
        "skip_reason": branch.skip_reason,
        "hypotheses": [
            {"name": h.name, "verdict": h.verdict, "n": h.n,
             "ci_lower": h.ci_lower, "ci_upper": h.ci_upper,
             "p_value_bh_adjusted": h.p_value_bh_adjusted,
             "underpowered_reason": h.underpowered_reason}
            for h in branch.hypotheses
        ],
        "stationarity": (
            {"signaled": branch.stationarity.signaled,
             "h_used": branch.stationarity.h_used,
             "pre_regime_n": branch.stationarity.pre_regime_n,
             "post_regime_n": branch.stationarity.post_regime_n}
            if branch.stationarity else None
        ),
    })


def _tag_grain_to_json(pr) -> str:
    grains = []
    for b in (pr.held_analyst_branch, pr.flipper_branch):
        if b is None:
            continue
        for g in b.tag_grains:
            grains.append({
                "branch": b.branch, "grain": g.grain, "tag": g.tag,
                "n_at_grain": g.n_at_grain, "metric_value": g.metric_value,
                "verdict": g.verdict,
            })
    return json.dumps(grains)


def _capital_ledger_to_json(ledger) -> str:
    if ledger is None:
        return "{}"
    return json.dumps(ledger.__dict__ if hasattr(ledger, "__dict__") else {})
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m pytest tests/test_wallet_layer5_persistence.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/wallet_protocol/persistence.py tests/test_wallet_layer5_persistence.py
git commit -m "feat(layer5): persistence layer with BEGIN IMMEDIATE write-time sequence"
```

### Task A6: wire `run_wallet_protocol.py` to write outputs

**Files:**
- Modify: `scripts/run_wallet_protocol.py`
- Test: `tests/test_run_wallet_protocol_persist.py` (NEW)

**CLI shape (verified).** `scripts/run_wallet_protocol.py:43` defines `wallet` as a *positional*
argument (`ap.add_argument("wallet")`). All command examples and tests use the positional form
`run_wallet_protocol.py <wallet>`, never `--wallet <wallet>`.

- [ ] **Step 1: Add `--persist` flag (positional wallet remains)**

In `scripts/run_wallet_protocol.py`, after the existing `add_argument("wallet")` block:

```python
ap.add_argument("--persist", action="store_true",
                help="Write the run + decision into the sandbox/live DB.")
```

- [ ] **Step 2: Write failing integration test**

```python
# tests/test_run_wallet_protocol_persist.py
"""Smoke test: run_wallet_protocol --persist writes one success row + one
live decision row, fully atomic. Uses a pre-fetched WalletData fixture so
the test does not hit live APIs."""
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from db import init_db
from sniper.analysis.wallet_protocol.types import (
    ProtocolReport, BranchReport, HypothesisResult, StationarityResult,
)


def _fake_protocol_report(wallet="0xfeedface") -> ProtocolReport:
    """Hand-built ProtocolReport that maps to validated_copy_candidate / high."""
    branch = BranchReport(
        branch="held_analyst",
        is_active=True,
        hypotheses=[HypothesisResult(name="H1", verdict="supported", n=80,
                                      ci_lower=0.05, ci_upper=0.30)],
        stationarity=StationarityResult(
            cusum_status="calibrated", h_used=25.0, k=0.5,
            signaled=False, signal_index=None,
            pre_regime_n=60, post_regime_n=20,
        ),
    )
    return ProtocolReport(
        wallet=wallet, analyzed_at=int(time.time()),
        n_closed_positions=87, n_held_to_resolution=64,
        net_pnl=12345.0, gate_result="pass",
        trading_mode="held_analyst_only", overall_verdict="edge_detected",
        primary_edge_type="entry_timing",
        held_analyst_branch=branch,
    )


def _no_fetch(*a, **kw):
    """Stand-in for fetch_wallet_data — returns a minimal WalletData. The
    test does not exercise fetch; it exercises persist plumbing."""
    from sniper.analysis.wallet_protocol.types import WalletData
    return WalletData(
        wallet="0xfeedface",
        closed_positions=[], open_positions=[], activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1="directional_discretionary",
        execution_profile=None,
        fetched_at=int(time.time()),
    )


def test_persist_flag_writes_atomic_run_and_decision(tmp_path, monkeypatch):
    db_path = tmp_path / "sandbox.db"
    init_db(str(db_path))
    monkeypatch.setenv("SNIPER_DB_PATH", str(db_path))

    # Patch BOTH the prefetch AND analyze_wallet so the CLI does not hit live
    # APIs. The current CLI (scripts/run_wallet_protocol.py:107, :110) calls
    # fetch_wallet_data() before analyze_wallet(); patching only analyze_wallet
    # would still trigger a live HTTP fetch.
    import scripts.run_wallet_protocol as mod
    monkeypatch.setattr(mod, "fetch_wallet_data", _no_fetch)
    monkeypatch.setattr(mod, "analyze_wallet", lambda *a, **kw: _fake_protocol_report())

    rc = mod.main(["0xfeedface", "--persist"])  # main() takes argv list
    assert rc == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    runs = list(conn.execute("SELECT * FROM analyzer_runs"))
    decisions = list(conn.execute(
        "SELECT * FROM wallet_layer5_decision WHERE is_current=1"))
    assert len(runs) == 1 and runs[0]["success"] == 1
    assert len(decisions) == 1
    assert decisions[0]["analyzer_run_id"] == runs[0]["run_id"]


def test_persist_flag_records_failure_with_no_decision(tmp_path, monkeypatch):
    db_path = tmp_path / "sandbox.db"
    init_db(str(db_path))
    monkeypatch.setenv("SNIPER_DB_PATH", str(db_path))

    import scripts.run_wallet_protocol as mod
    monkeypatch.setattr(mod, "fetch_wallet_data", _no_fetch)
    def _boom(*a, **kw):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(mod, "analyze_wallet", _boom)

    rc = mod.main(["0xfeedface", "--persist"])
    assert rc == 1  # failure-path returns 1 after recording the audit row

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    runs = list(conn.execute("SELECT * FROM analyzer_runs"))
    decisions = list(conn.execute("SELECT * FROM wallet_layer5_decision"))
    assert len(runs) == 1 and runs[0]["success"] == 0
    assert runs[0]["error_class"] == "RuntimeError"
    assert len(decisions) == 0


def test_fetch_failure_recorded_as_failed_run(tmp_path, monkeypatch):
    """Fetch raising pre-analyze must still produce a success=0 audit row.
    This catches the regression where only analyze_wallet was patched."""
    db_path = tmp_path / "sandbox.db"
    init_db(str(db_path))
    monkeypatch.setenv("SNIPER_DB_PATH", str(db_path))

    import scripts.run_wallet_protocol as mod
    def _fetch_boom(*a, **kw):
        raise ConnectionError("simulated fetch failure")
    monkeypatch.setattr(mod, "fetch_wallet_data", _fetch_boom)

    rc = mod.main(["0xfeedface", "--persist"])
    assert rc == 1  # fetch-failure path also returns 1, after audit row

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    runs = list(conn.execute("SELECT * FROM analyzer_runs"))
    assert len(runs) == 1 and runs[0]["success"] == 0
    assert runs[0]["error_class"] == "ConnectionError"
```

**Notes for implementer (Step 4 below):** the failure-path persist must catch exceptions
*around* the fetch call too, not only around `analyze_wallet`. The v2 plan body suggested a
`try/except` around `analyze_wallet` only. Widen it to wrap fetch + analyze together; if the
exception arose inside fetch, `report` is undefined and the failure path uses `args.wallet`,
`error_class=type(e).__name__`, etc. — same as for analyze failure.

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_run_wallet_protocol_persist.py -v`
Expected: FAIL — `--persist` flag missing or `main()` doesn't accept argv list.

- [ ] **Step 4: Modify `scripts/run_wallet_protocol.py` to call persistence atomically**

Refactor `main()` to accept an optional `argv` list (so tests can drive it without
`subprocess`). The exception handler must wrap **both** `fetch_wallet_data` and
`analyze_wallet` — fetch is the more common failure surface (network, subgraph hiccup) and
must produce a `success=0` audit row.

```python
def main(argv=None) -> int:
    args = ap.parse_args(argv)
    started = int(time.time())
    try:
        data = fetch_wallet_data(args.wallet, ...)        # existing call
        report = analyze_wallet(args.wallet, pre_fetched_data=data, ...)
    except Exception as e:
        if args.persist:
            import sqlite3, os
            from sniper.analysis.wallet_protocol.persistence import persist_failed_run
            conn = sqlite3.connect(os.getenv("SNIPER_DB_PATH", "sniper_tracker.db"))
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA busy_timeout = 5000")
                persist_failed_run(
                    conn, wallet=args.wallet,
                    started_ts=started, finished_ts=int(time.time()),
                    duration_ms=int((time.time() - started) * 1000),
                    error_class=type(e).__name__,
                    error_message=str(e),
                    invocation_source="cli",
                    args_json=json.dumps({"capital_ledger": args.capital_ledger}),
                    fetch_quality_flags_json="[]",
                    activity_rows_fetched=None,
                    activity_cap_hit=None,
                    subgraph_fallback_used=False,
                )
            finally:
                conn.close()
        return 1  # failure path returns 1 (audit row written; do not raise)

    if args.persist:
        import sqlite3, os
        from sniper.analysis.wallet_protocol.persistence import (
            persist_successful_run_with_decision,
        )
        from sniper.analysis.wallet_protocol.wallet_layer5_mapping import (
            map_protocol_report_to_decision,
        )
        from sniper.analysis.wallet_protocol.report import render_report
        finished = int(time.time())
        conn = sqlite3.connect(os.getenv("SNIPER_DB_PATH", "sniper_tracker.db"))
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            decision = map_protocol_report_to_decision(report)
            persist_successful_run_with_decision(
                conn, pr=report, decision=decision,
                started_ts=started, finished_ts=finished,
                duration_ms=int((finished - started) * 1000),
                invocation_source="cli",
                args_json=json.dumps({"capital_ledger": args.capital_ledger}),
                fetch_quality_flags_json=json.dumps(
                    getattr(report, "fetch_quality_flags", []) or []
                ),
                activity_rows_fetched=report.activity_rows_fetched,
                activity_cap_hit=report.activity_cap_hit,
                subgraph_fallback_used=False,  # set by fetch.py if applicable
                report_markdown=render_report(report),
            )
        finally:
            conn.close()
    return 0
```

The `try/except` around `fetch_wallet_data` + `analyze_wallet` is the only place the
failure-path persists; the mainline never partially-writes. `main()` returns `1` on the
failure path (after writing the audit row) and `0` on success — it never re-raises. The
module's `if __name__ == "__main__":` shim is `raise SystemExit(main())` so shell exit codes
are preserved while tests can assert on the return value.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_run_wallet_protocol_persist.py -v`
Expected: PASS (3/3): success path, analyze-failure path, fetch-failure path.

- [ ] **Step 6: Commit**

```bash
git add scripts/run_wallet_protocol.py tests/test_run_wallet_protocol_persist.py
git commit -m "feat(layer5): run_wallet_protocol --persist with atomic success/failure paths"
```

### Task A7: position_signal_decision schema lock + integration

**Note:** Only the schema is locked here. The atomic dual-write with `position_market_state` is
implemented in a follow-up task once Workstream B's column report is in.

**Files:**
- (none new — schema already in `db_layer5.py` from A1)

- [ ] **Step 1: Confirm Workstream B's column report has merged**

Check: `git fetch origin && git log origin/main --oneline -- docs/research/scopes/2026-04-29-position-signals-column-validation.md`
Expected: at least one commit. If absent, this task waits.

- [ ] **Step 2: Read B's report**

- [ ] **Step 3: Decide whether `position_market_state` schema can land in Phase 3 or defers**

If B's KEEP set is small and well-scoped → land schema. Otherwise → defer to Phase 4 plan and
note in A's commit message.

- [ ] **Step 4: If landing, add `ensure_position_market_state` to `db_layer5.py` + tests**

- [ ] **Step 5: If landing, wire into `init_db`**

- [ ] **Step 6: Commit**

```bash
git add sniper/persistence/db_layer5.py db.py tests/test_wallet_layer5_persistence.py
git commit -m "feat(layer5): position_market_state schema landed per Workstream B report"
```

### Task A8: v3 amendment A — H2_sub080 time-split in held_analyst.py

**Files:**
- Modify: `sniper/analysis/wallet_protocol/held_analyst.py`
- Test: `tests/test_held_analyst_h2_sub080_time_split.py` (NEW)

- [ ] **Step 1: Read current H2 implementation in held_analyst.py**

Note the existing H1 / H2 / H2_sub080 paths and the existing `time_split` dict keyed by
hypothesis name.

- [ ] **Step 2: Write failing test — strong asserts, not key-presence**

The test must assert the failure mode the spec describes, not just that a key exists. The
fixture is an eatpraylove-shaped held-only wallet:
- ≥ 60 closed positions held to resolution
- full H2 verdict = `refuted` (held WR significantly *below* market-implied)
- H2_sub080 verdict = `supported` (held WR among entries < 0.80 significantly above sub080 baseline)

```python
# tests/test_held_analyst_h2_sub080_time_split.py
import pytest
from sniper.analysis.wallet_protocol.held_analyst import run_held_branch
from sniper.analysis.wallet_protocol.types import WalletData


def _eatpraylove_shaped_walletdata():
    """Hand-built fixture mimicking the failure mode the v3-A amendment fixes:
    full H2 refuted, sub-0.80 H2 supported, n_held high enough for time-split."""
    # ... build closed_positions with two clusters:
    #   ~40 entries at price >= 0.80, low WR (drives full H2 refuted)
    #   ~40 entries at price < 0.80, high WR (drives sub080 supported)
    # ... return a WalletData with these positions populated.
    raise NotImplementedError("Filled by Phase 3 implementer per fixture spec")


def test_full_h2_refuted_sub080_supported(_eatpraylove_shaped_walletdata):
    """Sanity: the fixture exercises the documented failure mode."""
    branch = run_held_branch(_eatpraylove_shaped_walletdata, ...)
    h2 = next(h for h in branch.hypotheses if h.name == "H2")
    h2_sub = next(h for h in branch.hypotheses if h.name == "H2_sub080")
    assert h2.verdict == "refuted"
    assert h2_sub.verdict == "supported"


def test_h2_sub080_time_split_runs_when_full_refuted_sub080_supported(
        _eatpraylove_shaped_walletdata):
    """v3-A fix: time_split must include H2_sub080 with a powered fold result."""
    branch = run_held_branch(_eatpraylove_shaped_walletdata, ...)
    assert "H2_sub080" in branch.time_split, \
        "v3-A regression: H2_sub080 time-split was not run"
    ts = branch.time_split["H2_sub080"]
    # The time-split must be powered (n_total >= MIN, both train and test n > 0,
    # and at least one fold not 'underpowered').
    assert ts.holdout_status in {"survived", "failed"}, \
        f"time-split was underpowered: {ts.holdout_status}"
    assert ts.n_train > 0 and ts.n_test > 0
    assert ts.n_folds_total >= 1
    assert ts.n_folds_underpowered < ts.n_folds_total


def test_h2_full_refuted_does_not_block_sub080_time_split(
        _eatpraylove_shaped_walletdata):
    """The pre-fix bug: time-split was guarded on 'H2 is the primary verdict',
    skipping when H2 was refuted but sub080 was promoted to primary."""
    branch = run_held_branch(_eatpraylove_shaped_walletdata, ...)
    # If full H2 is refuted and sub080 supported, sub080's time-split must run.
    h2 = next(h for h in branch.hypotheses if h.name == "H2")
    assert h2.verdict == "refuted"
    assert "H2_sub080" in branch.time_split  # the assertion that breaks pre-fix
```

The fixture is a hand-built mock — we don't ship the real wallet's data. The Phase 3 implementer
must build closed_positions arrays with two clusters whose distributional properties produce the
required `(refuted, supported)` verdict pair under the existing H2 / H2_sub080 logic. This is
verified by `test_full_h2_refuted_sub080_supported` first; only then are the v3-A asserts
meaningful.

- [ ] **Step 3: Run test — expect FAIL**

`test_h2_sub080_time_split_runs_when_full_refuted_sub080_supported` and
`test_h2_full_refuted_does_not_block_sub080_time_split` fail with KeyError `"H2_sub080"`
not in `branch.time_split` (the pre-fix bug).

- [ ] **Step 4: Implement H2_sub080 time-split loop in held_analyst.py**

Locate the existing time-split block (driven by H2). Add a parallel time-split loop for
H2_sub080 when `H2_sub080` ran and H2 was refuted. Persist into `branch.time_split["H2_sub080"]`
as a `TimeSplitResult` populated from the rolling-origin folds (default k=5 per `types.py:151`).

- [ ] **Step 5: Run tests — expect PASS**

`python3 -m pytest tests/test_held_analyst_h2_sub080_time_split.py -v`

- [ ] **Step 6: Commit**

```bash
git add sniper/analysis/wallet_protocol/held_analyst.py tests/test_held_analyst_h2_sub080_time_split.py
git commit -m "feat(v3-A): H2_sub080 time-split runs when full H2 refuted"
```

### Task A9: v3 amendment B — F2 from flag to gate in stage1_gates.py

**Files:**
- Modify: `sniper/analysis/wallet_protocol/stage1_gates.py`
- Test: `tests/test_stage1_gates_f2_promotion.py` (NEW)

- [ ] **Step 1: Read current F2 implementation**

- [ ] **Step 2: Write failing test**

When `F2_concentration_top3` flag is present AND top-3 fraction > FB threshold, expect Stage-1
to flag wallet for `H1_sub_top3` diagnostic AND emit `EDGE_CONDITIONAL_ON_TOP3_EXCLUSION` flag.

- [ ] **Step 3: Run test — expect FAIL**

- [ ] **Step 4: Implement F2 promotion logic**

- [ ] **Step 5: Run tests — expect PASS**

- [ ] **Step 6: Commit**

```bash
git add sniper/analysis/wallet_protocol/stage1_gates.py tests/test_stage1_gates_f2_promotion.py
git commit -m "feat(v3-B): promote F2 caution flag to Stage-1 gate"
```

### Task A10: build `wallet_layer5_responder.py` + round-trip test

**Files:**
- Create: `sniper/analysis/wallet_protocol/wallet_layer5_responder.py`
- Test: `tests/test_wallet_layer5_responder.py` (NEW)

**Why this task exists:** Phase 2 § 8 #7 demands a `WalletDecisionResponse` round-trip
producing `status: "ok"` for the seeded set. Building it in Phase 3 (without the HTTP route)
satisfies the acceptance now and lets Phase 4 plug the route in cleanly. The responder is a
pure builder over the persisted row.

- [ ] **Step 1: Failing test**

```python
# tests/test_wallet_layer5_responder.py
import json
import sqlite3
import time
import pytest
from db import init_db
from sniper.analysis.wallet_protocol.persistence import (
    persist_successful_run_with_decision, DEFAULT_ANALYSIS_VERSION,
)
from sniper.analysis.wallet_protocol.wallet_layer5_mapping import Layer5Decision
from sniper.analysis.wallet_protocol.types import (
    ProtocolReport, BranchReport, HypothesisResult, StationarityResult,
)
from sniper.analysis.wallet_protocol.wallet_layer5_responder import (
    build_wallet_decision_response,
)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    init_db(str(p))
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 5000")
    yield c
    c.close()


def _seed(db):
    branch = BranchReport(
        branch="held_analyst", is_active=True,
        hypotheses=[HypothesisResult(name="H1", verdict="supported", n=80,
                                      ci_lower=0.05, ci_upper=0.30)],
        stationarity=StationarityResult(
            cusum_status="calibrated", h_used=25.0, k=0.5,
            signaled=False, signal_index=None,
            pre_regime_n=60, post_regime_n=20,
        ),
    )
    pr = ProtocolReport(
        wallet="0xabc", analyzed_at=int(time.time()),
        n_closed_positions=87, n_held_to_resolution=64,
        net_pnl=12345.0, gate_result="pass",
        trading_mode="held_analyst_only", overall_verdict="edge_detected",
        primary_edge_type="entry_timing",
        held_analyst_branch=branch,
    )
    decision = Layer5Decision(
        "validated_copy_candidate",
        ["edge_detected", "primary_edge_type:entry_timing"],
        "high",
    )
    persist_successful_run_with_decision(
        db, pr=pr, decision=decision,
        started_ts=int(time.time()), finished_ts=int(time.time()),
        duration_ms=100, invocation_source="test", args_json="{}",
        fetch_quality_flags_json="[]", activity_rows_fetched=120,
        activity_cap_hit=False, subgraph_fallback_used=False,
    )


def test_response_status_ok_for_seeded_wallet(db):
    _seed(db)
    resp = build_wallet_decision_response(db, "0xabc")
    assert resp["status"] == "ok"
    assert resp["wallet"] == "0xabc"
    assert resp["analysis_version"] == DEFAULT_ANALYSIS_VERSION
    assert resp["is_current"] is True
    assert resp["action_decision"] == "validated_copy_candidate"
    assert resp["confidence_band"] == "high"
    assert resp["overall_verdict"] == "edge_detected"
    assert resp["gate_result"] == "pass"
    assert resp["trading_mode"] == "held_analyst_only"
    # Required fields per Phase 2 § 6.1 — none null
    for k in ("action_reason_codes", "data_quality_flags",
              "applicable_limitations", "open_questions"):
        assert isinstance(resp[k], list), f"{k} must be a list, got {type(resp[k])}"
    # numeric fields are direct measurements
    assert resp["n_closed_positions"] == 87
    assert resp["net_pnl_usdc"] == 12345.0


def test_response_status_never_analyzed(db):
    resp = build_wallet_decision_response(db, "0xnope")
    assert resp == {"wallet": "0xnope", "status": "never_analyzed"}


def test_response_status_stale_version(db, monkeypatch):
    """Persist at wpv2.0, then the responder is asked for wpv2.1 — returns
    stale_version with the wpv2.0 body and is_current=False."""
    _seed(db)
    monkeypatch.setattr(
        "sniper.analysis.wallet_protocol.wallet_layer5_responder.CURRENT_ANALYSIS_VERSION",
        "wpv2.1",
    )
    resp = build_wallet_decision_response(db, "0xabc")
    assert resp["status"] == "stale_version"
    assert resp["analysis_version"] == "wpv2.0"
    assert resp["is_current"] is False  # the stored row's is_current flag is True,
    # but the responder downgrades when asked for a newer version.
```

- [ ] **Step 2: Implement `wallet_layer5_responder.py`**

```python
"""Builds Phase 2 § 6.1 WalletDecisionResponse from the persisted row.

Pure read-side. No side effects. Used by Phase 4 HTTP and MCP routes.
"""
from __future__ import annotations
import json
import sqlite3
from typing import Any, Optional
from sniper.analysis.wallet_protocol.persistence import (
    DEFAULT_ANALYSIS_VERSION, get_wallet_layer5_decision,
)

CURRENT_ANALYSIS_VERSION: str = DEFAULT_ANALYSIS_VERSION


def build_wallet_decision_response(
    conn: sqlite3.Connection, wallet: str
) -> dict[str, Any]:
    row = get_wallet_layer5_decision(conn, wallet)
    if row is None:
        # Could be never-analyzed OR analyzed only at older versions (where
        # is_current was flipped to 0). Distinguish by re-querying.
        any_row = conn.execute(
            "SELECT * FROM wallet_layer5_decision WHERE wallet = ? "
            "ORDER BY as_of_ts DESC LIMIT 1",
            (wallet,),
        ).fetchone()
        if any_row is None:
            return {"wallet": wallet, "status": "never_analyzed"}
        row = dict(any_row)
        return _to_response(row, status="stale_version")

    if row["analysis_version"] != CURRENT_ANALYSIS_VERSION:
        return _to_response(row, status="stale_version")

    return _to_response(row, status="ok")


def _to_response(row: dict[str, Any], *, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "wallet": row["wallet"],
        "analysis_version": row["analysis_version"],
        "is_current": (status == "ok"),
        "as_of_ts": row["as_of_ts"],

        "action_decision": row["action_decision"],
        "action_reason_codes": json.loads(row["action_reason_codes_json"] or "[]"),
        "confidence_band": row["confidence_band"],

        "overall_verdict": row["overall_verdict"],
        "primary_edge_type": row["primary_edge_type"],
        "trading_mode": row["trading_mode"],
        "gate_result": row["gate_result"],

        "n_closed_positions": row["n_closed_positions"],
        "n_held_to_resolution": row["n_held_to_resolution"],
        "n_open_positions": row["n_open_positions"],
        "n_open_dead": row["n_open_dead"],
        "net_pnl_usdc": row["net_pnl_usdc"],
        "dw_roi_point": row["dw_roi_point"],
        "dw_roi_ci_lower": row["dw_roi_ci_lower"],
        "dw_roi_ci_upper": row["dw_roi_ci_upper"],
        "ew_roi_point": row["ew_roi_point"],
        "activity_cap_hit": bool(row["activity_cap_hit"]),
        "archetype_layer1": row["archetype_layer1"],

        "data_quality_flags": json.loads(row["data_quality_flags_json"] or "[]"),
        "applicable_limitations": json.loads(row["applicable_limitations_json"] or "[]"),
        "open_questions": json.loads(row["open_questions_json"] or "[]"),
        "copyability_note": row["copyability_note"],

        # Phase 4 owns the actual report endpoint; URL is a placeholder for now.
        "report_markdown_url": f"/api/wallet-decision/{row['wallet']}/report.md",
    }
```

- [ ] **Step 3: Run tests — expect PASS**

`python3 -m pytest tests/test_wallet_layer5_responder.py -v`

- [ ] **Step 4: Commit**

```bash
git add sniper/analysis/wallet_protocol/wallet_layer5_responder.py tests/test_wallet_layer5_responder.py
git commit -m "feat(layer5): WalletDecisionResponse builder with status enum"
```

### Task A11: ship `tag_market_stats_reader.py` (Phase-3-minimal helpers)

**Files:**
- Create: `sniper/analysis/wallet_protocol/tag_market_stats_reader.py` (NEW)
- Test: `tests/test_tag_market_stats_reader.py` (NEW)

**Scope (Codex-revised: minimal Phase 3).** This task ships **only** the read helpers and the
availability flag against the `tag_market_stats` schema landed in Task A1. It makes **no claim**
about analyzer behavior change — `tag_market_stats_unavailable` reason-code emission is *not*
touched in Phase 3 because the analyzer code path that should emit it (Layer 3
`market_selection_edge` heuristic in `wallet_archetype_extractor`) is itself partial today
(see migration plan § A.2 row "wallet_archetype_extractor — Layers 3-4 stubbed"). Wiring the
emission belongs in Phase 4 alongside the Layer 3/4 stub-fill.

**Why this scoping (per Codex review):**
- `tag_market_stats` is per-tag *market metadata* (n_markets, yes_share, volume, co-tags). It is
  **not** the per-(wallet, tag) peer baseline. H5/FH4 baselines flow through `peer_baseline_fn`
  callable injected into `held_analyst.run_held_branch` (`held_analyst.py:1089`) and currently
  read `wallet_tag_pnl` via `db.peer_baseline_for_grain` (`db.py:6293`) — a different table.
- "Wire into `run_h5_tag_edge` flow" was wrong in the v2 plan. Removing that claim.
- The `tag_market_stats_unavailable` reason code is currently **unreachable** from the mapping
  in § 4.2; it is intended for the Layer 3 path. Keeping it stable-listed in the spec § 4.4 is
  fine (forward-declared); Phase 3 just doesn't fire it.

**No D dependency.** Helpers return `None` / `False` against an empty table. Once Workstream D
populates the table, the helpers' returns change naturally, with no re-deploy of A required.
A11 ships independent of D's merge order.

- [ ] **Step 1: Failing test**

```python
# tests/test_tag_market_stats_reader.py
"""Phase-3 minimal helpers over the tag_market_stats schema. No analyzer
behavior assertions — those belong to Phase 4 along with Layer 3 stub-fill."""
import sqlite3
import time
import pytest
from db import init_db
from sniper.analysis.wallet_protocol.tag_market_stats_reader import (
    is_tag_market_stats_available,
    fetch_tag_market_stats,
)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    init_db(str(p))
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_unavailable_when_table_empty(db):
    assert is_tag_market_stats_available(db, "geo") is False
    assert fetch_tag_market_stats(db, "geo") is None


def test_available_when_row_seeded(db):
    db.execute(
        "INSERT INTO tag_market_stats "
        "(tag, n_markets, n_resolved, resolved_yes_count, yes_share, "
        " avg_resolved_price_yes, median_market_volume_24h, top_co_tags_json, "
        " computed_at, inputs_version) "
        "VALUES ('geo', 200, 180, 90, 0.5, 0.55, 1000.0, '[]', ?, 'v1')",
        (int(time.time()),),
    )
    db.commit()
    assert is_tag_market_stats_available(db, "geo") is True
    stats = fetch_tag_market_stats(db, "geo")
    assert stats is not None
    assert stats["tag"] == "geo"
    assert stats["n_markets"] == 200
    assert stats["yes_share"] == 0.5
    assert stats["inputs_version"] == "v1"


def test_fetch_returns_none_for_unknown_tag(db):
    assert fetch_tag_market_stats(db, "absolutely_nonexistent") is None


def test_no_side_effects(db):
    """Reader does not mutate the table (no INSERT, UPDATE, DELETE)."""
    db.execute(
        "INSERT INTO tag_market_stats "
        "(tag, n_markets, n_resolved, resolved_yes_count, yes_share, "
        " avg_resolved_price_yes, median_market_volume_24h, top_co_tags_json, "
        " computed_at, inputs_version) "
        "VALUES ('geo', 200, 180, 90, 0.5, 0.55, 1000.0, '[]', ?, 'v1')",
        (int(time.time()),),
    )
    db.commit()
    before = list(db.execute("SELECT * FROM tag_market_stats"))
    is_tag_market_stats_available(db, "geo")
    fetch_tag_market_stats(db, "geo")
    fetch_tag_market_stats(db, "missing")
    after = list(db.execute("SELECT * FROM tag_market_stats"))
    assert [dict(r) for r in before] == [dict(r) for r in after]
```

- [ ] **Step 2: Run test — expect FAIL** (module missing)

`python3 -m pytest tests/test_tag_market_stats_reader.py -v`

- [ ] **Step 3: Implement `tag_market_stats_reader.py`**

```python
"""Phase-3 minimal read helpers over tag_market_stats.

The table is populated by Workstream D (Codex). This module is the
Claude-side reader. It carries no analyzer-behavior obligations in
Phase 3 — Layer 3 / Layer 4 stub-fill in `wallet_archetype_extractor`
is the right time to wire `tag_market_stats_unavailable` emission, and
that lands in Phase 4 (see migration plan § A.2).
"""
from __future__ import annotations
import sqlite3
from typing import Any, Optional


def is_tag_market_stats_available(conn: sqlite3.Connection, tag: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM tag_market_stats WHERE tag = ? LIMIT 1", (tag,),
    ).fetchone()
    return row is not None


def fetch_tag_market_stats(
    conn: sqlite3.Connection, tag: str,
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM tag_market_stats WHERE tag = ?", (tag,),
    ).fetchone()
    return dict(row) if row else None
```

- [ ] **Step 4: Run tests — expect PASS**

`python3 -m pytest tests/test_tag_market_stats_reader.py -v`

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/wallet_protocol/tag_market_stats_reader.py tests/test_tag_market_stats_reader.py
git commit -m "feat(layer5): tag_market_stats reader helpers (Phase-3 minimal)"
```

### Task A12: regen brain index

- [ ] **Step 1: Run regen**

Run: `python3 scripts/brain_index.py generate`
Expected: clean diff including `wallet_layer5_decision`, `position_signal_decision`,
`analyzer_runs`, `tag_market_stats`.

- [ ] **Step 2: Run schema validator**

Run: `python3 scripts/validate_schema_refs.py`
Expected: clean.

- [ ] **Step 3: Commit regenerated index**

```bash
git add index/schema_registry.json index/dependency_index.json
git commit -m "chore(index): regenerate after Phase 3 layer5 tables"
```

### Task A13: full suite + push

- [ ] **Step 1: Full pytest run**

Run: `python3 -m pytest tests/ -q`
Expected: green.

- [ ] **Step 2: Push branch**

```bash
git push origin feat/phase3-analyzer-core
```

- [ ] **Step 3: Open PR; request review from platform-reviewer agent**

---

## 4. Workstream B — `position_signals` column trace (Codex)

**Owner:** Codex.
**Branch:** `docs/phase3-position-signals-trace`.

**Files allowed (write):**
- `docs/research/scopes/2026-04-29-position-signals-column-validation.md`

**Files forbidden:** all code files.

**Scope:** For every column in `position_signals` (run `python3 scripts/brain_index.py schema
position_signals` to enumerate), trace the writer in `sniper/persistence/db_signals.py:upsert_
position_signal` and any caller in `analyze_users.py`. Classify each column as one of:
- `raw_market` — direct from CLOB / Polymarket API field
- `derived_market` — computed from raw market data only, no scoring input
- `score_coupled` — derived from `insider_score`, `speculator_score`, `classification`, etc.

For each column, recommend disposition for `position_market_state`: `KEEP`, `DROP`, or
`VERIFY-WITH-OWNER`.

**Acceptance:** all 53 columns appear with file:line writer cite + classification + disposition.
Spot-check three columns (`position_classification`, `entry_anchor_ts`, `activity_state`) in the
review pass.

**Verification:** `grep -E "INSERT INTO position_signals|UPDATE position_signals" $(git ls-files
'*.py')` matches the writer set Codex cites.

---

## 5. Workstream C — `tag_nav_category` backfill (Codex)

**Owner:** Codex.
**Branch:** `feat/phase3-tag-backfill`.

**Files allowed (write):**
- `scripts/sync_polymarket_tags.py` (NEW)
- `tests/test_sync_polymarket_tags.py` (NEW)

**Files forbidden:**
- `sniper/analysis/wallet_protocol/`
- `sniper/persistence/`
- `db.py`
- anything outside `scripts/` and `tests/test_sync_polymarket_*.py`

**Scope:** Build idempotent script that walks gamma `event_slug → category` mappings and seeds
`tag_nav_category` from ~50 → ≥80% of frequent tags (defined as tags with ≥10 rows in
`wallet_tag_pnl`). Mock gamma in tests. Produce a brain finding documenting the unmapped tags
for human resolution.

**Acceptance:**
- Script idempotent (re-run produces zero net inserts).
- ≥80% of frequent tags have non-`other` `nav_category`.
- Tests cover happy path + gamma error path.

**Verification:**
```bash
python3 -m pytest tests/test_sync_polymarket_tags.py -v
# Two consecutive runs:
python3 scripts/sync_polymarket_tags.py --db /tmp/sandbox.db
python3 scripts/sync_polymarket_tags.py --db /tmp/sandbox.db
# Row count should be identical after #2 vs #1.
```

---

## 6. Workstream D — `compute_tag_market_stats` persistence (Codex)

**Owner:** Codex.
**Branch:** `feat/phase3-tag-market-stats`.
**Sequenced:** after Workstream A merges (consumes `ensure_tag_market_stats` schema).

**Files allowed (write):**
- `sniper/analysis/tag_analytics.py` (extend)
- `tests/test_tag_analytics.py` (extend)

**Files forbidden:**
- `sniper/analysis/wallet_protocol/` (Claude-only — Workstream A Task A11 ships the
  read helpers in a new `wallet_protocol/tag_market_stats_reader.py`. No other
  `wallet_protocol/` file is touched for tag-stats consumption in Phase 3.)
- `sniper/persistence/`, `db.py`
- `scripts/`

**Scope:** Add `persist_tag_market_stats(conn, stats: list[TagMarketStats]) -> None` that writes
`compute_tag_market_stats` output to the `tag_market_stats` table (schema in A's
`db_layer5.py`). Add a reader `get_tag_market_stats(conn, tag) -> Optional[TagMarketStats]`.
Tests exercise the round-trip.

**Acceptance (rescoped per Codex review):**
- Existing tests in `tests/test_tag_analytics.py` still green.
- New round-trip tests pass: `compute_tag_market_stats` → `persist_tag_market_stats` →
  `get_tag_market_stats` returns the same values for every tag in the input.
- Idempotent: running `persist_tag_market_stats` twice with the same input produces the same
  row count and identical content (use `INSERT … ON CONFLICT(tag) DO UPDATE`).

**NOT in this acceptance (rescoped per Codex v2 review):** the analyzer-level behavior change
("`analyze_wallet` invocations no longer emit `tag_market_stats_unavailable`"). The emission
site for that reason code does not exist in the analyzer today — it belongs to the Layer 3
`market_selection_edge` heuristic in `wallet_archetype_extractor`, which is currently stubbed
(see migration plan § A.2). Wiring belongs in Phase 4 alongside Layer 3/4 stub-fill. Workstream
A Task A11 ships only minimal read helpers (`tag_market_stats_reader.py`) so D's writes are
*queryable*; behavior-change wiring waits for Phase 4.

**Verification:**
```bash
python3 -m pytest tests/test_tag_analytics.py -v
```

---

## 7. Workstream E — `signal_history` parquet snapshot (Codex)

**Owner:** Codex.
**Branch:** `feat/phase3-history-snapshot`.

**Files allowed (write):**
- `scripts/snapshot_signal_history.py` (NEW)
- `tests/test_snapshot_signal_history.py` (NEW)
- `archive/snapshots/.gitkeep` if absent
- `archive/snapshots/2026-04-XX-signal-history.parquet` (committed plain if <5 MB; otherwise
  coordinate storage with the user before commit)

**Files forbidden:** anything else.

**Scope:** Export `signal_history` to parquet. Required by Phase 8 for replay reproducibility.

**Acceptance:**
- Round-trip: `pd.read_parquet(snapshot).shape[0] == SELECT COUNT(*) FROM signal_history`.
- All scoring columns survive (`position_insider_score`, `position_speculator_score`,
  `position_classification`, `entry_anchor_ts`).
- Idempotent re-run produces identical SHA256.

**Verification:**
```bash
python3 -m pytest tests/test_snapshot_signal_history.py -v
sha256sum archive/snapshots/2026-04-XX-signal-history.parquet  # twice, identical
```

---

## 8. Workstream F — Backfill (joint, sequenced last)

**Owner:** Claude runs, Codex spot-checks.
**Sequenced:** after A + B + C + D + E merge.

**Preflight (must pass before pull):**

```bash
ssh sniper@188.166.91.20
cd ~/sniper

# 1. Foreign-WIP guard. If working tree is dirty, park to wip/foreign-… per
#    CLAUDE.md before pulling. Do not pull on top of foreign edits.
git status --short
# Expect EMPTY output. If anything appears, branch + commit + return to main:
#   git checkout -b wip/foreign-$(date +%Y-%m-%d)-vps-pre-backfill
#   git add -A && git commit -m "wip: park VPS-side WIP for recovery"
#   git checkout main

# 2. Timer-collision guard. The VPS schedules sniper-world-analyst.timer
#    (06:00 + 18:00 UTC) and sniper-finance-analyst (4h interval). Make sure
#    none are about to fire during the backfill window (~30-60 min).
systemctl --user list-timers --no-pager
# Inspect 'NEXT' for any timer firing in the next hour. If yes, either (a)
# wait for that timer to run+complete, or (b) systemctl --user stop <timer>
# for the duration, restart at end.

# 3. Confirm CI / migration is green for the latest main commit.
git fetch origin
git log --oneline origin/main -1   # should match HEAD after the pull below
```

**Backfill steps:**

```bash
# 4. Pull latest main on VPS.
git pull origin main

# 5. Restart sniper-web so init_db runs and creates the new tables.
systemctl --user restart sniper-web

# 6. Re-check working tree post-pull (defensive; should still be clean).
git status --short
# Expect EMPTY.

# 7. Run backfill. Wallet is a positional argument (verified: scripts/
#    run_wallet_protocol.py:43 — `ap.add_argument("wallet")`).
for w in $(.venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect('sniper_tracker.db')
wallets = set()
for row in c.execute('SELECT wallet FROM followed_wallets'): wallets.add(row[0])
for row in c.execute('SELECT wallet FROM watchlist_wallets'): wallets.add(row[0])
for w in sorted(wallets): print(w)
"); do
  echo "=== analyzing $w ==="
  .venv/bin/python3 scripts/run_wallet_protocol.py "$w" --persist || \
    echo "FAILED $w (recorded as success=0 in analyzer_runs)"
done

# 8. Verify the row counts match.
.venv/bin/python3 - <<'PY'
import sqlite3
c = sqlite3.connect('sniper_tracker.db')
followed = {r[0] for r in c.execute('SELECT wallet FROM followed_wallets')}
watch = {r[0] for r in c.execute('SELECT wallet FROM watchlist_wallets')}
expected = len(followed | watch)
got = c.execute(
    "SELECT COUNT(*) FROM wallet_layer5_decision WHERE is_current = 1"
).fetchone()[0]
print(f"followed+watchlist={expected}  is_current=1 rows={got}")
assert got == expected, f"mismatch: expected {expected}, got {got}"

# Required-fields nullness check.
nulls = c.execute("""
    SELECT wallet FROM wallet_layer5_decision
    WHERE is_current = 1 AND (
        action_decision IS NULL OR
        confidence_band IS NULL OR
        overall_verdict IS NULL OR
        gate_result IS NULL OR
        trading_mode IS NULL
    )
""").fetchall()
assert not nulls, f"nulls in required fields for: {nulls}"

# Failed-run audit (non-blocking; report only).
fails = list(c.execute("""
    SELECT wallet, error_class, error_message
    FROM analyzer_runs WHERE success = 0 ORDER BY started_ts DESC
"""))
if fails:
    print(f"WARNING: {len(fails)} failed runs in analyzer_runs:")
    for w, cls, msg in fails:
        print(f"  {w} {cls}: {msg[:80]}")
PY
```

**Codex spot-check (after step 8 prints OK):**

Compare 5 wallets — Theo4, SwissMiss, eatpraylove, and two random sampled from the followed
set — against existing brain `wallet-dossier/<addr>` memories. For each:
- Read the existing dossier verdict tier (validated / reverse_engineer / study / monitor / etc).
- Read the new `wallet_layer5_decision.action_decision`.
- Flag any wallet whose new decision is **more than one band away** from the dossier's prior
  expectation. Root-cause before declaring Phase 3 done. (One band drift is acceptable —
  e.g. dossier said "study", new says "monitor" — both pre-copy. More than one band warrants
  reading the rendered report and confirming the analyzer didn't misroute.)

**Acceptance:** every (followed ∪ watchlist) wallet has an `is_current=1` row; no nulls in
required columns; failed runs (if any) recorded in `analyzer_runs` with `success=0` and a
non-empty `error_message`; Codex spot-check produces zero >1-band drifts.

---

## 9. Merge order

1. Pre-phase plan (this doc) → user sign-off + Codex review.
2. **Workstream B's research report** (read-only — does not block A from starting; A consumes
   it at Task A7).
3. **Workstream A merges to main** — all of A1–A13 land in one branch. A11 ships only
   `tag_market_stats_reader.py` helpers (no analyzer-behavior change), so it has no
   dependency on Workstream D and no feature flag.
4. **Workstreams C, D, and E merge to main** (any time after A; mutually independent).
   D's writes immediately become readable through A11's helpers — no post-merge edit in
   `wallet_protocol/` needed in Phase 3.
5. **Workstream F runs on VPS** (preflight per § 8).
6. Phase 3 declared done; Phase 4 plan begins (which will include the Layer-3/4 wiring of
   `tag_market_stats_unavailable` emission).

---

## 10. Verification checklist (after all workstreams merge)

- [ ] `python3 -m pytest tests/ -q` green on main.
- [ ] `python3 scripts/validate_schema_refs.py` green.
- [ ] `python3 scripts/brain_index.py generate` — diff matches committed.
- [ ] `wallet_layer5_decision`, `position_signal_decision`, `analyzer_runs`, `tag_market_stats`
      exist; `python3 scripts/brain_index.py schema <table>` confirms each.
- [ ] `tag_nav_category` covers ≥80% of frequent tags.
- [ ] `archive/snapshots/2026-04-XX-signal-history.parquet` exists and round-trips.
- [ ] All ~50 currently-followed/watchlisted wallets have `is_current=1` row, no required-field
      nulls.
- [ ] No legacy score column added; `confidence_band`, `tag_fit_score`, `tag_fit_rank` continue
      to be context-only.
- [ ] 24 h soak on VPS with `sniper-paper`, `sniper-stream`, `sniper-analyzer`, `sniper-web`
      Active and no exceptions in journals.

---

## 11. Risks + open questions

Status reflects Codex's 2026-04-29 review of v1 of this plan.

1. **Concurrency policy (§ 0.1).** `BEGIN IMMEDIATE` per atomic transaction +
   `PRAGMA busy_timeout = 5000`. **Codex agreed** with the policy after the A5 atomicity fix.
   Closed.
2. **`tag_market_stats` persistence path (§ 0.2).** New dedicated table.
   **Codex confirmed** the dedicated-table choice over compute-on-read or JSON-in-row.
   Closed.
3. **`position_market_state` deferral (§ 0.3, Task A7).** May defer to Phase 4 if Workstream B's
   `KEEP` set is too thin. Goal/acceptance language was loosened in the header to say A only
   *lands the schema* unconditionally; the dual-write loop is conditional on A7 landing.
   Closed-pending-B.
4. **v3-A test fixture realism (Task A8).** Strengthened: A8 now ships three asserts —
   sanity (full H2 refuted, sub080 supported), the v3-A fix (sub080 in time_split with powered
   fold status), and the regression test (full-H2-refuted does not block sub080 time-split).
   Key-presence alone is insufficient. Closed.
5. **Backfill on VPS (Workstream F).** Cron-collision preflight added: `systemctl --user
   list-timers` + foreign-WIP `git status --short` BEFORE `git pull`, with named-branch parking
   if needed. Pull-then-status-check pattern from v1 is fixed. Closed.
6. **Foreign-WIP risk on VPS.** Same fix as #5: status check is the first preflight step,
   pull only happens after the working tree is clean. Closed.

**Phase-4 follow-up (non-blocking, flagged by Codex 2026-04-29 review):** Workstream D adds
`get_tag_market_stats(conn, tag)` to `sniper/analysis/tag_analytics.py` while A11 adds
`fetch_tag_market_stats(conn, tag)` to `sniper/analysis/wallet_protocol/tag_market_stats_reader.py`.
Two public readers for the same table is acceptable in Phase 3 — D's lives next to its writer,
A's lives in the analyzer module that will read it — but Phase 4's read-path wiring should pick
one canonical import surface and re-export from there. Pick when wiring the Layer-3
`market_selection_edge` heuristic.

If new risks surface during execution, append them here with a date and resolution note.

---

## 12. Self-review checklist (before sign-off)

- [ ] Every architecture decision in § 0 is locked or explicitly deferred to a numbered open
      question in § 11.
- [ ] No two workstreams write the same file.
- [ ] Every workstream has owner / files-allowed / files-forbidden / acceptance / verification.
- [ ] TDD steps in Workstream A produce failing tests first, then minimal implementations.
- [ ] No placeholders (`TBD`, "implement later", "similar to Task N", commented-out steps).
- [ ] Phase 2 spec § 8 acceptance coverage map (one column per criterion):
      - § 8 #1 (schemas in § 3.1/§ 3.2/§ 3.3 land + pass schema-architect review):
        Workstream A Task A1 (schemas) + post-merge schema-architect review on `feat/phase3-
        analyzer-core` PR.
      - § 8 #2 (mapping totality over `OverallVerdict × GateResult` 5×4): Task A4
        `test_mapping_totality_5x4`.
      - § 8 #3 (no orphan reason codes — one-way: emitted ⊆ taxonomy): Task A4
        `test_emitted_codes_in_taxonomy`. Reverse coverage (taxonomy ⊆ reachable)
        is deferred to Phase 4 because some § 4.4 codes are produced by upstream
        analyzer code (Layer 3 / 4), not by the mapping under test here.
      - § 8 #4 (no field added authorizes by itself): § 0 invariants + § 6.4 grep guardrail
        in spec; verified at A12 (brain index regen) and A13 (full pytest including
        `test_no_synthetic_scores_program_wide.py` if shipped — Phase 8 guardrail is
        scaffolded but not necessarily green in Phase 3).
      - § 8 #5 (HTTP/MCP signoff by platform-reviewer): **already satisfied during the Phase 2
        spec sign-off pass; the route implementation lands in Phase 4, not here**.
      - § 8 #6 (fixture vectors exist with the listed coverage): Task A3 fixture file +
        Task A4 vector test; vectors enumerate sign-flip emit, F1+F2 across three verdicts,
        flipper_only mode, mixed_mode, MIN_CUSUM_N power floor, all three Stage-1 rejects.
      - § 8 #7 (`WalletDecisionResponse` round-trip with `status: "ok"`): Task A10
        `wallet_layer5_responder.py` + tests; Workstream F backfill provides the seeded set.
- [ ] Brain index regen is exactly one task (A12), owned by Claude.
- [ ] Foreign-WIP rule from `CLAUDE.md` cited (§ 1 #8 + Workstream F preflight).
- [ ] CLI shape verified: `wallet` is positional, never `--wallet` (§ Task A6, § 8 step 7).
- [ ] Persistence is atomic per spec § 3.1: A5 ships two atomic writers
      (`persist_successful_run_with_decision`, `persist_failed_run`); the prior
      `persist_analyzer_run`-then-`persist_wallet_layer5_decision` two-commit pattern is
      removed.
- [ ] Mapping purity test fails on call to `sqlite3.connect`, `os.getenv`,
      `os.environ.__getitem__`, `time.time` (Task A4 `test_mapping_is_pure_fail_on_call`).

---

**Pre-execution gate:** This plan must be approved by Codex before Workstream A starts. The
review brief: confirm § 0 decisions, find missing edge cases in the TDD steps, propose stricter
ownership lines if any cross-track edits look possible.
