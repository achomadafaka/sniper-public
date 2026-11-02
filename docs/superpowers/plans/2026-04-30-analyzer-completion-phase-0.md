# Analyzer Completion — Phase 0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the system from "legacy services running and writing to legacy tables" to "legacy services stopped, disabled, masked, and audited; new analyzer's import/DB/smoke independence proven; Phase 0 baseline captured." This is the prerequisite for Phase 0.5 entrypoint guards.

**Architecture:** Read-only audit + administrative VPS commands + one local fixture-recorder script. No production code changes. All changes land via `git mv` / new doc files / one-shot script + tests. The end-state is contamination-free: no legacy writers can run, all analyzer-relevant write surfaces are classified, and a regression baseline exists.

**Tech Stack:** systemd (user units), `systemctl --user stop/disable/mask`, `pgrep`, Python 3 (`scripts/brain_index.py`, `scripts/validate_schema_refs.py`), pytest, sqlite3 (read-only). No new dependencies.

**Parent spec:** [`docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md`](../specs/2026-04-30-analyzer-completion-and-archive-design.md) at commit `720df35`. Codex Gate 1 passed at `docs/audits/codex/2026-04-30-gate-1-rev3.md`.

**Out of scope for Phase 0:** module guards (Phase 0.5), gap closure (Phase 1), archive (Phase 2), perf (Phase 3), deploy (Phase 4).

---

## Parallel-work plan (Claude + Codex)

Phase 0 splits cleanly into two lanes with explicit synchronization points. Two operators (Claude main loop + Codex) can work the lanes concurrently. The lanes touch disjoint files and surfaces — only Sync points require both to be present.

| Lane | Owner | Surface | Tasks | Files touched (no overlap with other lane) |
|---|---|---|---|---|
| **A — VPS admin** | Claude (or whichever party holds the SSH session) | `ssh sniper@188.166.91.20`, `systemctl --user`, `STATUS.md`, `PAUSED_TIMERS.md`, `docs/audits/2026-04-30-vps-unit-matrix.md` | T1, T2, T3, T4, T5 | matrix doc, STATUS.md, PAUSED_TIMERS.md |
| **B — local audits + baseline** | Codex | `scripts/audit_*.py`, `tests/test_audit_*.py`, `docs/audits/2026-04-30-{import-graph,db-write-surface}-audit.md`, `tests/calibration/baselines/swissmiss_phase0*.json` | T6, T7, T8, T10 | both audit scripts + their tests, both audit docs, baseline JSON |
| **Sync** | Both | shared workspace | S1 (after A and B both complete), then T9, T11, T12 | none |

**Concurrency rules:**

1. **No two tasks edit the same file** — verified above.
2. **VPS access is single-writer.** Only Lane A SSHes for Tasks 1–4. Codex must NOT run `systemctl --user stop/disable/mask`. Read-only VPS introspection (`pgrep`, `list-units`) is allowed for verification at any time.
3. **Each lane commits to `main` independently.** No merge conflicts because of (1). Both lanes pull before pushing.
4. **Sync point S1 blocks T9 (smoke).** T9 requires both lanes' commits visible on `main`. Whichever party arrives at S1 second runs T9.
5. **Failure handling:** if a task in Lane A fails (e.g., a service won't stop), Lane B continues — Lane B's work is still valid. Lane A documents the failure and the operator decides whether to abort Phase 0 or proceed without that unit.

**Recommended split given current state:**
- **Claude takes Lane A** (SSH already established this session; VPS state is fresh).
- **Codex takes Lane B — but only T6, T7, T8 immediately.** T10 (baseline capture) is deferred until Lane A finishes T4 (services masked + processes verified absent). Rationale: T10 invokes `scripts/run_wallet_protocol.py` which hits live Polymarket / Goldsky APIs; running it while legacy writers are still hot risks contaminating the read paths or producing a baseline that mixes writer-active and writer-frozen states. Cleaner to capture baseline against a quiet system.

**Suggested handoff prompt for Codex (Lane B):**

> Execute Lane B of `docs/superpowers/plans/2026-04-30-analyzer-completion-phase-0.md` in parallel with Claude's Lane A. Tasks: T6, T7, T8, T10. Files touched: `scripts/audit_import_graph.py`, `scripts/audit_db_write_surface.py`, `tests/test_audit_*.py`, `docs/audits/2026-04-30-{import-graph,db-write-surface}-audit.md`, `tests/calibration/baselines/swissmiss_phase0*.json`. Do NOT SSH the VPS for write operations. Pull main before each commit. Hand off Sync point S1 status (commit SHA) when complete.

---

## Phase 0 done definition (≡ Gate 1.5 entry conditions):
1. `docs/audits/2026-04-30-vps-unit-matrix.md` exists with verdict-per-unit for every live VPS unit + every repo `systemd/*.{service,timer}` file.
2. `docs/audits/2026-04-30-db-write-surface-audit.md` exists with classified writers for the table list in spec §4.3.
3. `docs/audits/2026-04-30-import-graph-audit.md` records the AST audit result (expected: `OK`).
4. VPS state: every `retire`-classed unit stopped (process verified absent via `pgrep`), disabled, and masked. STATUS.md and PAUSED_TIMERS.md reflect this.
5. Phase 0 smoke green: pytest passes, `validate_schema_refs.py` clean, `brain_index.py generate` clean, all 7 active root modules importable.
6. Baseline JSON committed at `tests/calibration/baselines/swissmiss_phase0.json` (fixture-driven) OR `tests/calibration/baselines/swissmiss_phase0_informational.json` (live-API fallback).
7. Brain memo `phase-0-shutdown-and-audit-2026-04-30` persisted to Supabase brain.

---

## File Structure

| Path | Type | Responsibility |
|---|---|---|
| `docs/audits/2026-04-30-vps-unit-matrix.md` | new doc | Verdict-per-unit table reconciling spec §4.1 against live VPS + repo `systemd/`. |
| `docs/audits/2026-04-30-db-write-surface-audit.md` | new doc | Classified writers for every table in spec §4.3. Lock the §5.1 Pattern B function list (consumed by Phase 0.5). |
| `docs/audits/2026-04-30-import-graph-audit.md` | new doc | AST audit result + reproduction command. |
| `STATUS.md` | modify | Append Phase 0 state line + link to unit matrix. |
| `PAUSED_TIMERS.md` | modify | Append every retire-classed unit with mask date and rationale. |
| `tests/calibration/baselines/swissmiss_phase0.json` | new (conditional) | Fixture-driven verdict baseline. Falls back to `swissmiss_phase0_informational.json` if recorder is deferred. |
| `scripts/audit_db_write_surface.py` | new script | Generates audit doc by walking brain_index `deps --table` for spec §4.3 table list + grepping for Supabase memory writes. |
| `scripts/audit_import_graph.py` | new script | Promotes the inline AST audit from spec §4.2 into a reproducible script with exit code. |
| Brain memo `phase-0-shutdown-and-audit-2026-04-30` | new | Persists outcomes + verification command. |

---

## Task 1: Snapshot live VPS state (read-only)

**Lane:** A (VPS admin) — Claude. **Prereqs:** none. **Blocks:** T2, T3.

**Files:**
- Create: `/tmp/vps_live_units_2026-04-30.txt` (local scratch)
- Create: `/tmp/vps_unit_files_2026-04-30.txt` (local scratch)
- Create: `/tmp/vps_running_legacy_pids_2026-04-30.txt` (local scratch)

- [ ] **Step 1: Snapshot live unit state**

```bash
ssh sniper@188.166.91.20 'systemctl --user list-units --all --no-pager 2>&1 | grep "^\s*sniper-"' \
    > /tmp/vps_live_units_2026-04-30.txt
wc -l /tmp/vps_live_units_2026-04-30.txt
```

Expected: ≥9 lines (the units actively known: stream, analyzer, paper, alerter, web, bot, sim-collector, sim-meta, tennis-inverse-paper@44 minimum).

- [ ] **Step 2: Snapshot installed unit files**

```bash
ssh sniper@188.166.91.20 'systemctl --user list-unit-files --no-pager 2>&1 | grep "^sniper-"' \
    > /tmp/vps_unit_files_2026-04-30.txt
wc -l /tmp/vps_unit_files_2026-04-30.txt
```

Expected: ≥40 lines (matching the 47 unit files in repo `systemd/`).

- [ ] **Step 3: Snapshot running legacy processes**

```bash
ssh sniper@188.166.91.20 'pgrep -af "stream_low_buys|analyze_users|paper_trader|alerter\.py"' \
    > /tmp/vps_running_legacy_pids_2026-04-30.txt
cat /tmp/vps_running_legacy_pids_2026-04-30.txt
```

Expected: 4 lines (one PID line each for stream_low_buys, analyze_users, paper_trader, alerter). If a process is missing, note it — do not stop yet.

- [ ] **Step 4: No commit (scratch files in /tmp)**

These artifacts are inputs to Task 2; the matrix doc captures the decisions.

---

## Task 2: Author the unit-matrix audit doc

**Lane:** A — Claude. **Prereqs:** T1. **Blocks:** T4 step 6 (post-mask delta), T5.

**Files:**
- Create: `docs/audits/2026-04-30-vps-unit-matrix.md`

- [ ] **Step 1: Write the matrix doc**

```markdown
---
memory_name: audit/vps-unit-matrix-2026-04-30
memory_type: reference
memory_description: Verdict-per-unit table reconciling spec §4.1 against live VPS state and repo systemd/ inventory. Records exact retire/keep/review classifications, live ActiveState/UnitFileState pairs, and post-mask deltas. Authoritative input to Phase 0.5 and §STATUS.md updates.
memory_tags: [audit, systemd, vps, phase-0, freeze]
---

# VPS Unit Matrix — Phase 0

**Date:** 2026-04-30
**Inputs:** /tmp/vps_live_units_2026-04-30.txt, /tmp/vps_unit_files_2026-04-30.txt, /tmp/vps_running_legacy_pids_2026-04-30.txt, repo `systemd/` listing (47 files), spec §4.1.

## Pre-mask state

(Populate from the three input files. One row per unit. Columns: unit_name, ActiveState, SubState, UnitFileState, repo_path_present, verdict.)

| Unit | ActiveState | SubState | UnitFileState | In repo systemd/ | Verdict |
|---|---|---|---|---|---|
| ... | ... | ... | ... | ... | ... |

## Verdicts

- **retire** (stop+disable+mask): list units.
- **keep** (active and required): list units.
- **review** (default-keep, classify after Phase 0.5 audit if needed): list units.
- **not-installed** (in repo, not on VPS): list units. No action.

## Reconciliation

- Every line in `vps_live_units_2026-04-30.txt` mapped to one verdict above.
- Every file in repo `systemd/` (47) mapped to one verdict above.
- Any unit not classified by spec §4.1 — list explicitly with newly-assigned verdict and rationale.

## Post-mask delta (filled in by Task 4)

| Unit | Pre-mask UnitFileState | Post-mask UnitFileState |
|---|---|---|
| ... | ... | ... |
```

- [ ] **Step 2: Populate by hand from the scratch files**

Manually transcribe each line from the three `/tmp/vps_*` files into the table above. For each, look up the spec §4.1 verdict; for any unit not in the spec, assign a verdict and note "newly classified" in the rationale.

- [ ] **Step 3: Commit pre-mask state**

```bash
git add docs/audits/2026-04-30-vps-unit-matrix.md
git commit -m "audit: phase-0 vps unit matrix (pre-mask snapshot)"
```

---

## Task 3: Stop legacy services and verify quiet

**Lane:** A — Claude. **Prereqs:** T1, T2 (matrix verdicts known). **Blocks:** T4.

**Files:**
- VPS systemd state (no local file changes)

- [ ] **Step 1: Stop the four active legacy services**

```bash
ssh sniper@188.166.91.20 'systemctl --user stop sniper-stream.service sniper-analyzer.service sniper-paper.service sniper-alerter.service'
```

Expected: empty output (success).

- [ ] **Step 2: Wait 5 seconds for cleanup**

```bash
sleep 5
```

- [ ] **Step 3: Verify processes are gone**

```bash
ssh sniper@188.166.91.20 'pgrep -af "stream_low_buys|analyze_users|paper_trader|alerter\.py"'
```

Expected: empty output. If any line returns, the daemon ignored SIGTERM. STOP — investigate `journalctl --user -u <unit> -n 30` before continuing. Do not proceed to Task 4.

- [ ] **Step 4: Verify ActiveState=inactive on each**

```bash
ssh sniper@188.166.91.20 'for u in sniper-stream sniper-analyzer sniper-paper sniper-alerter; do systemctl --user is-active $u; done'
```

Expected: `inactive` four times (`failed` is also acceptable if exit was non-zero; both mean not running).

---

## Task 4: Disable and mask retire-classed units

**Lane:** A — Claude. **Prereqs:** T3. **Blocks:** T5, S1.

**Files:**
- VPS systemd state

- [ ] **Step 1: Disable all retire units**

```bash
ssh sniper@188.166.91.20 'systemctl --user disable \
    sniper-stream.service sniper-analyzer.service sniper-paper.service sniper-alerter.service \
    sniper-agent-insider-copy.service sniper-agent-insider-copy.timer \
    sniper-agent-insider-analyst.service sniper-agent-insider-analyst.timer \
    sniper-hl-stream.service sniper-stink-bid-shadow.service'
```

Expected: per-unit disable lines or "not enabled" (idempotent — disabled-already is OK).

- [ ] **Step 2: Mask all retire units (including templated stink-bid)**

```bash
ssh sniper@188.166.91.20 'systemctl --user mask \
    sniper-stream.service sniper-analyzer.service sniper-paper.service sniper-alerter.service \
    sniper-agent-insider-copy.service sniper-agent-insider-copy.timer \
    sniper-agent-insider-analyst.service sniper-agent-insider-analyst.timer \
    sniper-hl-stream.service sniper-stink-bid-shadow.service sniper-stink-bid@.service'
```

Expected: per-unit "Created symlink … /dev/null" or "already masked" lines.

- [ ] **Step 3: Verify mask state**

```bash
ssh sniper@188.166.91.20 'systemctl --user list-unit-files --no-pager | grep -E "^(sniper-stream|sniper-analyzer|sniper-paper|sniper-alerter|sniper-agent-insider-copy|sniper-agent-insider-analyst|sniper-hl-stream|sniper-stink-bid-shadow|sniper-stink-bid@)\."'
```

Expected: every line ends in `masked`.

- [ ] **Step 4: Final no-process verification**

```bash
ssh sniper@188.166.91.20 'pgrep -af "stream_low_buys|analyze_users|paper_trader|alerter\.py"'
```

Expected: empty output.

- [ ] **Step 5: Snapshot post-mask state**

```bash
ssh sniper@188.166.91.20 'systemctl --user list-unit-files --no-pager | grep "^sniper-"' \
    > /tmp/vps_unit_files_post_mask_2026-04-30.txt
diff /tmp/vps_unit_files_2026-04-30.txt /tmp/vps_unit_files_post_mask_2026-04-30.txt | head -40
```

Expected: diff shows the retire units transitioning from `disabled`/`enabled` → `masked`.

- [ ] **Step 6: Update matrix doc with post-mask delta**

Manually fill in the "Post-mask delta" table in `docs/audits/2026-04-30-vps-unit-matrix.md` from the diff output.

- [ ] **Step 7: Commit post-mask state**

```bash
git add docs/audits/2026-04-30-vps-unit-matrix.md
git commit -m "audit: phase-0 vps unit matrix (post-mask delta)"
```

---

## Task 5: Update STATUS.md and PAUSED_TIMERS.md

**Lane:** A — Claude. **Prereqs:** T4. **Blocks:** S1.

**Files:**
- Modify: `STATUS.md`
- Modify: `PAUSED_TIMERS.md`

- [ ] **Step 1: Append Phase 0 state to STATUS.md**

```bash
cat >> STATUS.md <<'EOF'

## 2026-04-30 — Phase 0 freeze

Legacy pipeline frozen. Per `docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md`.

- `sniper-stream.service`, `sniper-analyzer.service`, `sniper-paper.service`, `sniper-alerter.service` — stopped, disabled, masked.
- `sniper-agent-insider-copy.{service,timer}`, `sniper-agent-insider-analyst.{service,timer}` — masked.
- `sniper-hl-stream.service`, `sniper-stink-bid-shadow.service`, `sniper-stink-bid@.service` — masked.
- `sniper-web.service` remains active (dashboard reads `user_analysis` stale until D-scope).
- `sniper-sim-*` services unaffected (sim platform independent).
- See `docs/audits/2026-04-30-vps-unit-matrix.md` for full matrix.

Active legacy processes verified absent via `pgrep -af "stream_low_buys|analyze_users|paper_trader|alerter\.py"` returning empty.
EOF
```

- [ ] **Step 2: Append entries to PAUSED_TIMERS.md**

```bash
cat >> PAUSED_TIMERS.md <<'EOF'

## 2026-04-30 Phase 0 — legacy freeze

| Unit | Action | Reason |
|---|---|---|
| `sniper-stream.service` | stop + disable + mask | Legacy CLOB streamer, retire; replaced by new analyzer's intake queue. |
| `sniper-analyzer.service` | stop + disable + mask | Legacy P&L-first scorer, retire; replaced by `sniper.analysis.wallet_protocol`. |
| `sniper-paper.service` | stop + disable + mask | Reads legacy `position_signals`/`paper_sell_signals`; archived in Phase 2. |
| `sniper-alerter.service` | stop + disable + mask | Reads legacy `user_analysis`/`flagged_users`; archived in Phase 2. |
| `sniper-agent-insider-copy.{service,timer}` | mask | Legacy follow logic. |
| `sniper-agent-insider-analyst.{service,timer}` | mask | Legacy analyst loop. |
| `sniper-hl-stream.service` | mask | Retired HL strategy. |
| `sniper-stink-bid-shadow.service` | mask | Retired strategy. |
| `sniper-stink-bid@.service` | mask | Retired templated strategy. |
EOF
```

- [ ] **Step 3: Commit STATUS + paused-timers update**

```bash
git add STATUS.md PAUSED_TIMERS.md
git commit -m "docs: phase-0 freeze status + paused-timers update"
```

---

## Task 6: Promote import-graph audit into a script

**Lane:** B (local audits) — Codex. **Prereqs:** none. **Blocks:** T7.

Runs in parallel with Lane A (Tasks 1–5). No file overlap.

**Files:**
- Create: `scripts/audit_import_graph.py`
- Create: `tests/test_audit_import_graph.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_audit_import_graph.py
"""Smoke test: the import-graph audit script imports and runs cleanly."""
from pathlib import Path

REPO = Path(__file__).parent.parent


def test_audit_script_exists():
    assert (REPO / "scripts/audit_import_graph.py").is_file()


def test_audit_script_has_main():
    """The script exposes a callable main() returning exit code."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "audit_import_graph", REPO / "scripts/audit_import_graph.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.main)


def test_audit_returns_zero_on_clean_repo():
    """Run the audit; expected exit code 0 (no legacy imports)."""
    import subprocess
    result = subprocess.run(
        ["python3", "scripts/audit_import_graph.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_audit_import_graph.py -v
```

Expected: FAIL with `FileNotFoundError` or `assert (...) is_file()`.

- [ ] **Step 3: Write the script**

```python
# scripts/audit_import_graph.py
"""
Audits that the new wallet-analyzer pipeline does not import any legacy module.

Targets (must NOT be imported by the new path):
  analyze_users, stream_low_buys, db_discovery, bot_detector, classify_accounts

Scanned (the new path):
  scripts/run_wallet_protocol.py
  scripts/run_wallet_protocol_batch.py
  sniper/analysis/wallet_protocol/**/*.py

Exit codes:
  0 — clean (prints "OK")
  1 — at least one legacy import found (prints offending file:module pairs)

Spec ref: docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md §4.2
"""
import ast
import pathlib
import sys


LEGACY_TOP_LEVEL = {
    "analyze_users",
    "stream_low_buys",
    "db_discovery",
    "bot_detector",
    "classify_accounts",
}


def collect_imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module.split(".")[0])
    return out


def main() -> int:
    repo = pathlib.Path(__file__).parent.parent
    targets: list[pathlib.Path] = [
        repo / "scripts/run_wallet_protocol.py",
        repo / "scripts/run_wallet_protocol_batch.py",
    ]
    targets.extend((repo / "sniper/analysis/wallet_protocol").rglob("*.py"))

    violations: list[tuple[str, set[str]]] = []
    for path in targets:
        if not path.is_file():
            continue
        offending = collect_imports(path) & LEGACY_TOP_LEVEL
        if offending:
            violations.append((str(path.relative_to(repo)), offending))

    if violations:
        print("FAIL — legacy imports detected:")
        for path, names in violations:
            print(f"  {path}: {sorted(names)}")
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_audit_import_graph.py -v
```

Expected: PASS (3 tests).

- [ ] **Step 5: Run the audit standalone to capture output**

```bash
python3 scripts/audit_import_graph.py
echo "exit=$?"
```

Expected: `OK` and `exit=0`.

- [ ] **Step 6: Commit**

```bash
git add scripts/audit_import_graph.py tests/test_audit_import_graph.py
git commit -m "audit: phase-0 import-graph audit script + test"
```

---

## Task 7: Author the import-graph audit doc

**Lane:** B — Codex. **Prereqs:** T6. **Blocks:** S1.

**Files:**
- Create: `docs/audits/2026-04-30-import-graph-audit.md`

- [ ] **Step 1: Write the audit doc**

```bash
cat > docs/audits/2026-04-30-import-graph-audit.md <<'EOF'
---
memory_name: audit/import-graph-2026-04-30
memory_type: reference
memory_description: AST-based audit confirming the new wallet-analyzer pipeline (sniper.analysis.wallet_protocol + scripts/run_wallet_protocol*.py) imports zero legacy modules. Reproduction via `python3 scripts/audit_import_graph.py`. Phase 0 deliverable.
memory_tags: [audit, imports, phase-0, analyzer]
---

# Import-Graph Audit — Phase 0

**Date:** 2026-04-30
**Result:** OK (zero legacy imports detected).

## Method

`scripts/audit_import_graph.py` walks the AST of every `.py` file under:
- `scripts/run_wallet_protocol.py`
- `scripts/run_wallet_protocol_batch.py`
- `sniper/analysis/wallet_protocol/**/*.py`

For each file it collects every top-level imported module. If any matches one of:

```
analyze_users, stream_low_buys, db_discovery, bot_detector, classify_accounts
```

the audit fails (exit 1, prints offending file:module pairs).

## Reproduction

```
python3 scripts/audit_import_graph.py
echo "exit=$?"
```

Expected: `OK` and `exit=0`.

## Outcome (2026-04-30)

`OK` — no legacy imports.

## Implication

The new analyzer can be exercised in isolation. Phase 0.5 entrypoint guards
on the legacy modules will not break the new path.
EOF
```

- [ ] **Step 2: Commit**

```bash
git add docs/audits/2026-04-30-import-graph-audit.md
git commit -m "audit: phase-0 import-graph audit doc"
```

---

## Task 8: Build the DB write-surface audit script

**Lane:** B — Codex. **Prereqs:** none (independent of T6/T7). **Blocks:** S1.

Step 6 (hand-classify writers) produces the canonical Pattern B function list consumed by Phase 0.5 §5.1; quality of that list is critical.

**Files:**
- Create: `scripts/audit_db_write_surface.py`
- Create: `tests/test_audit_db_write_surface.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_audit_db_write_surface.py
"""Smoke test: db-write-surface audit script runs cleanly."""
import subprocess
from pathlib import Path

REPO = Path(__file__).parent.parent


def test_script_exists():
    assert (REPO / "scripts/audit_db_write_surface.py").is_file()


def test_script_runs_and_produces_output():
    result = subprocess.run(
        ["python3", "scripts/audit_db_write_surface.py", "--out", "/tmp/audit_test.md"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    out = Path("/tmp/audit_test.md").read_text()
    # Spec §4.3 mandates these tables appear in the audit:
    for tbl in (
        "low_buy_trades", "wallet_analysis_queue", "discovery_events",
        "user_analysis", "wallet_layer5_decision", "analyzer_runs",
        "wallet_analysis_batch_item", "wallet_profiles",
        "tag_nav_category", "market_outcomes",
    ):
        assert f"`{tbl}`" in out, f"table {tbl} missing from audit output"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_audit_db_write_surface.py -v
```

Expected: FAIL.

- [ ] **Step 3: Write the script**

```python
# scripts/audit_db_write_surface.py
"""
Generates the Phase 0 DB write-surface audit by walking brain_index `deps --table`
for every analyzer-relevant table from spec §4.3. Output: a markdown audit doc.

Spec ref: docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md §4.3
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
from datetime import datetime, timezone


# Spec §4.3 table list, with classification class.
TABLES: list[tuple[str, str]] = [
    # legacy (writers archived in Phase 2)
    ("low_buy_trades", "legacy"),
    ("wallet_analysis_queue", "legacy"),
    ("discovery_events", "legacy"),
    ("candidate_users", "legacy"),
    ("user_analysis", "legacy"),
    ("position_signals", "legacy"),
    ("paper_sell_signals", "legacy"),
    ("flagged_users", "legacy"),
    ("score_history", "legacy"),
    ("signal_history", "legacy"),
    # analyzer-owned (must have writers ONLY from sniper.analysis.wallet_protocol.*)
    ("wallet_analyzer_intake_queue", "analyzer-owned"),
    ("wallet_analysis_batch", "analyzer-owned"),
    ("wallet_analysis_batch_item", "analyzer-owned"),
    ("analyzer_runs", "analyzer-owned"),
    ("wallet_layer5_decision", "analyzer-owned"),
    ("position_signal_decision", "analyzer-owned"),
    ("tag_market_stats", "analyzer-owned"),
    ("position_market_state", "analyzer-owned"),
    # support
    ("wallet_tag_pnl", "support"),
    ("wallet_tag_fit", "support"),
    ("tag_nav_category", "support"),
    ("wallet_profiles", "support"),
    ("wallet_directory", "support"),
    ("followed_wallets", "support"),
    ("followed_wallet_activity", "support"),
    # external-feed-derived
    ("market_metadata", "external-feed-derived"),
    ("market_outcomes", "external-feed-derived"),
    ("market_tags", "external-feed-derived"),
]


def deps_for(table: str) -> str:
    """Return raw stdout from `brain_index.py deps --table <table>`. Empty on miss."""
    result = subprocess.run(
        ["python3", "scripts/brain_index.py", "deps", "--table", table],
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip()


def grep_supabase_writes() -> list[str]:
    """Find every call site that writes to Supabase brain (memories table)."""
    result = subprocess.run(
        ["grep", "-rn",
         "-e", "save_finding(",
         "-e", "save_memory(",
         "-e", ".table('memories')",
         "--include=*.py",
         "."],
        capture_output=True, text=True, timeout=30,
    )
    return [ln for ln in result.stdout.splitlines() if ".venv/" not in ln and "tests/" not in ln]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/audits/2026-04-30-db-write-surface-audit.md")
    args = parser.parse_args()

    lines: list[str] = []
    lines.append("---")
    lines.append("memory_name: audit/db-write-surface-2026-04-30")
    lines.append("memory_type: reference")
    lines.append('memory_description: Per-table writer classification for every legacy/analyzer-owned/support/external-feed-derived table. Generated by scripts/audit_db_write_surface.py. Phase 0 deliverable; locks the §5.1 Pattern B function list for Phase 0.5.')
    lines.append("memory_tags: [audit, db, write-surface, phase-0, analyzer]")
    lines.append("---")
    lines.append("")
    lines.append("# DB Write-Surface Audit — Phase 0")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"**Reproduce:** `python3 scripts/audit_db_write_surface.py`")
    lines.append("")
    for table, klass in TABLES:
        lines.append(f"## `{table}` ({klass})")
        lines.append("")
        lines.append("```")
        lines.append(deps_for(table) or "(no entries from brain_index)")
        lines.append("```")
        lines.append("")
        lines.append("**Writers classified:** _populate by hand from the deps output_")
        lines.append("")
    lines.append("## External write paths (out of brain_index scope)")
    lines.append("")
    lines.append("### Supabase brain memory writes")
    lines.append("")
    lines.append("```")
    for ln in grep_supabase_writes()[:200]:
        lines.append(ln)
    lines.append("```")
    lines.append("")
    lines.append("Classified per-call: `analyzer-owned-opt-in` (gated by `--persist-memo`),")
    lines.append("`agent-driven` (subagent runs persisting findings), `unrelated`.")
    lines.append("")
    lines.append("### Per-run JSONL log emitters")
    lines.append("")
    lines.append("Listed once Phase 1's runner-emitter pattern is implemented. None today.")
    lines.append("")
    lines.append("### Goldsky subgraph endpoints")
    lines.append("")
    lines.append("Read-only. Listed for completeness:")
    lines.append("- `https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/activity-subgraph/0.0.4/gn`")
    lines.append("- `https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/orderbook-subgraph/0.0.1/gn`")
    lines.append("")

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_audit_db_write_surface.py -v
```

Expected: PASS (2 tests).

- [ ] **Step 5: Generate the audit doc**

```bash
python3 scripts/audit_db_write_surface.py
ls -la docs/audits/2026-04-30-db-write-surface-audit.md
```

Expected: file exists, ≥2 KB.

- [ ] **Step 6: Hand-classify each writer**

Open `docs/audits/2026-04-30-db-write-surface-audit.md` and replace each `_populate by hand_` placeholder with the per-writer verdict. For every writer call site under each table:

- `legacy` table writer in `analyze_users.py` / `stream_low_buys.py` / `db_discovery.py` → mark **retired** (archived in Phase 2).
- `analyzer-owned` writer outside `sniper.analysis.wallet_protocol.*` or `scripts/run_wallet_protocol*.py` → **flag CONTAMINATION** and STOP. This is the Phase 0 acceptance gate.
- `support` writer in active code → **support** (kept).
- `external-feed-derived` writer → **kept**.

For the `db_discovery.py` rows (legacy table writers), produce the canonical write-function list under a section titled "Pattern B function list (locks Phase 0.5 §5.1)" — exact function names sourced from the deps output.

- [ ] **Step 7: Commit**

```bash
git add scripts/audit_db_write_surface.py tests/test_audit_db_write_surface.py docs/audits/2026-04-30-db-write-surface-audit.md
git commit -m "audit: phase-0 db write-surface audit + script"
```

- [ ] **Step 8: Acceptance gate**

If any `analyzer-owned` writer was flagged CONTAMINATION in step 6, STOP. The remediation is a separate spec edit before Phase 0 can close. If clean, proceed to Task 9.

---

## Sync Point S1 — Lanes converge

**Both lanes commit and push their work.** Whichever party arrives second:

```bash
git fetch origin
git pull origin main --rebase
```

Verify both lanes' deliverables present:

```bash
test -f docs/audits/2026-04-30-vps-unit-matrix.md && echo "lane A OK"
test -f docs/audits/2026-04-30-import-graph-audit.md && echo "lane B (T6/T7) OK"
test -f docs/audits/2026-04-30-db-write-surface-audit.md && echo "lane B (T8) OK"
test -f scripts/audit_import_graph.py && test -f scripts/audit_db_write_surface.py && echo "lane B scripts OK"
# T10 deliverable accepts EITHER the JSON baseline OR the documented .error.txt
# fallback (per T10 Step 2 — fetch failures are a documented Phase 0 outcome
# that Phase 1 step 1 retries via the fixture recorder).
( ls tests/calibration/baselines/swissmiss_phase0*.json 2>/dev/null \
  || ls tests/calibration/baselines/swissmiss_phase0_informational.error.txt 2>/dev/null \
) | head -1 && echo "lane B (T10) OK"
grep -q "Phase 0 freeze" STATUS.md && echo "lane A STATUS OK"
```

Expected: 6 `OK` lines. Any missing → return to the owning lane's task. Otherwise proceed to T9.

---

## Task 9: Phase 0 smoke

**Lane:** Sync (after S1). Either party. **Prereqs:** all of T1–T8, T10.

**Files:**
- No code changes — reads only.

- [ ] **Step 1: Pytest baseline**

```bash
python3 -m pytest tests/ -q --tb=line 2>&1 | tail -20
```

Expected: green or "previously-known failures only" (compare to prior session). Record the pass count.

- [ ] **Step 2: Schema validator**

```bash
python3 scripts/validate_schema_refs.py
echo "exit=$?"
```

Expected: `No schema violations found.` and `exit=0`.

- [ ] **Step 3: Brain-index regen**

```bash
python3 scripts/brain_index.py generate
git status --short index/
```

Expected: either no changes (index already current) or only `index/*.json` modified.

- [ ] **Step 4: Import smoke (Phase 0 — pre-archive — every current root module importable)**

```bash
python3 -c "import alerter, paper_trader, web_server, dashboard, strategy_runner, sniper_mcp, sync"
echo "import-smoke=$?"
```

Expected: `exit=0`. (Phase 2 changes this; see spec §9.2.)

- [ ] **Step 5: Commit any regenerated index changes**

```bash
git add index/
git diff --cached --stat
git commit -m "audit: refresh brain index (phase-0 smoke)" 2>/dev/null || echo "no index changes — clean"
```

---

## Task 10: Capture Phase 0 baseline JSON

**Lane:** B — Codex. **Prereqs:** Lane A T4 (services masked + processes verified absent — Codex's review noted that running this against a still-hot legacy pipeline could contaminate the baseline). **Blocks:** S1.

**Files:**
- Create (one of): `tests/calibration/baselines/swissmiss_phase0.json` OR `tests/calibration/baselines/swissmiss_phase0_informational.json`

- [ ] **Step 1: Decide path — fixture or informational**

```bash
test -x scripts/record_fetch_snapshot.py && echo "fixture path" || echo "informational path"
```

The recorder script does not yet exist (Phase 1 step 1 authors it). Therefore: **informational path**. Phase 1 step 1 promotes to fixture-driven.

- [ ] **Step 2: Run live-API SwissMiss baseline**

```bash
mkdir -p tests/calibration/baselines
python3 scripts/run_wallet_protocol.py 0xdbade4c82fb72780a0db9a38f821d8671aba9c95 \
    --no-exec --no-history --no-tags --no-peers --no-archetype \
    --n-iter 500 --json \
    > tests/calibration/baselines/swissmiss_phase0_informational.json
ls -la tests/calibration/baselines/swissmiss_phase0_informational.json
```

Expected: file ≥5 KB. If the run errors, capture stderr to `tests/calibration/baselines/swissmiss_phase0_informational.error.txt` and document the failure in `docs/audits/2026-04-30-import-graph-audit.md` as a known data-fetch issue. Phase 1 step 1 reattempts with the fixture recorder.

- [ ] **Step 3: Verify the JSON has decision fields**

```bash
python3 -c "
import json
d = json.load(open('tests/calibration/baselines/swissmiss_phase0_informational.json'))
needed = ('overall_verdict', 'primary_edge_type', 'n_closed_positions', 'dw_roi_point', 'ew_roi_point')
print({k: d.get(k) for k in needed})
"
```

Expected: dict with non-None entries. If any is `None`, the run was underpowered or skipped — document in the audit, do not block Phase 0.

- [ ] **Step 4: Commit the informational baseline**

```bash
git add tests/calibration/baselines/swissmiss_phase0_informational.json
git commit -m "audit: phase-0 informational baseline (swissmiss)"
```

---

## Task 11: Persist Phase 0 brain memo

**Lane:** Sync. Either party. **Prereqs:** T9 green.

**Files:**
- Brain memory `phase-0-shutdown-and-audit-2026-04-30` (Supabase)

- [ ] **Step 1: Compose the memo body**

Prepare a markdown blob with:

```markdown
# Phase 0 — Shutdown + Audit (2026-04-30)

**Spec:** `docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md` (commit 720df35)
**Plan:** `docs/superpowers/plans/2026-04-30-analyzer-completion-phase-0.md`
**Closure commit:** _<replace with HEAD SHA after Task 11 step 3 commits>_

## What changed

- VPS: `sniper-stream`, `sniper-analyzer`, `sniper-paper`, `sniper-alerter` stopped, disabled, masked.
- VPS: `sniper-agent-insider-{copy,analyst}.{service,timer}`, `sniper-hl-stream`, `sniper-stink-bid-shadow`, `sniper-stink-bid@.service` masked.
- Local: STATUS.md + PAUSED_TIMERS.md updated.
- Local: 3 new audit docs (unit matrix, import-graph, DB write-surface).
- Local: 2 new scripts (`audit_import_graph.py`, `audit_db_write_surface.py`) with tests.
- Local: informational baseline `tests/calibration/baselines/swissmiss_phase0_informational.json`.

## What did NOT change

- No production code modified (no module-level guards yet — that's Phase 0.5).
- No DB rows mutated.
- No new analyzer service installed.
- No archive moves (Phase 2).
- `sniper-web` and sim platform untouched.

## Verification

```
python3 scripts/audit_import_graph.py             # OK
python3 -m pytest tests/test_audit_import_graph.py tests/test_audit_db_write_surface.py -v
ssh sniper@188.166.91.20 'pgrep -af "stream_low_buys|analyze_users|paper_trader|alerter\\.py"'  # empty
```

## Gate 1.5 entry conditions met

- [x] unit matrix doc with verdict-per-unit
- [x] DB write-surface doc with classified writers
- [x] import-graph audit OK
- [x] retire units stopped/masked, processes verified absent
- [x] Phase 0 smoke green
- [x] baseline JSON committed
- [x] this brain memo
```

- [ ] **Step 2: Persist via Supabase MCP**

Use `mcp__supabase__execute_sql` (or the agent's preferred path) to INSERT into `memories`:

```sql
INSERT INTO memories (name, type, description, content, tags, status)
VALUES (
    'phase-0-shutdown-and-audit-2026-04-30',
    'finding',
    'Phase 0 of analyzer completion: VPS legacy units retired, audits committed, baseline captured. Closes Gate 1.5 entry conditions.',
    '<memo body from Step 1>',
    ARRAY['phase-0', 'analyzer', 'freeze', 'audit', 'shutdown'],
    'active'
);
```

(If the brain has a different schema, use `bot.supabase_helper.save_finding()` with equivalent fields.)

- [ ] **Step 3: Verify the memo is queryable**

```bash
python3 -c "
from bot.supabase_helper import read_memories, search_memories
hits = search_memories('phase-0-shutdown-and-audit-2026-04-30', limit=1)
print(hits[0]['name'] if hits else 'NOT FOUND')
"
```

Expected: `phase-0-shutdown-and-audit-2026-04-30`.

- [ ] **Step 4: No git commit needed (brain memos are external)**

Brain memo body is duplicated into the closure commit message in Task 12 for paper trail.

---

## Task 12: Close Phase 0

**Lane:** Sync. Either party. **Prereqs:** T11.

**Files:**
- No code changes — final commit aggregates the phase.

- [ ] **Step 1: Verify all Phase 0 done-conditions**

```bash
# Check each deliverable file exists.
test -f docs/audits/2026-04-30-vps-unit-matrix.md && echo "OK: unit matrix"
test -f docs/audits/2026-04-30-db-write-surface-audit.md && echo "OK: db audit"
test -f docs/audits/2026-04-30-import-graph-audit.md && echo "OK: import audit"
ls tests/calibration/baselines/swissmiss_phase0*.json | head -1
test -f scripts/audit_import_graph.py && echo "OK: import audit script"
test -f scripts/audit_db_write_surface.py && echo "OK: db audit script"
grep -q "Phase 0 freeze" STATUS.md && echo "OK: STATUS.md"
grep -q "2026-04-30 Phase 0" PAUSED_TIMERS.md && echo "OK: PAUSED_TIMERS.md"
```

Expected: 8 lines of `OK:`. Any missing → return to the corresponding task.

- [ ] **Step 2: Final VPS verification**

```bash
ssh sniper@188.166.91.20 'pgrep -af "stream_low_buys|analyze_users|paper_trader|alerter\.py"'
ssh sniper@188.166.91.20 'systemctl --user list-unit-files | grep -E "^(sniper-stream|sniper-analyzer|sniper-paper|sniper-alerter)"'
```

Expected: first command empty; second shows all 4 as `masked`.

- [ ] **Step 3: Push branch (no merge — main is the working branch)**

```bash
git log --oneline -10
git push origin main
```

Expected: push succeeds. Main contains the Phase 0 commits ahead of `48a29a3`.

- [ ] **Step 4: Hand to Codex for Gate 1.5**

Send the suggested prompt from the spec §16 Gate 1.5 checklist:

> Phase 0 closure for review. Commit: `<HEAD>`.
> Confirm against `docs/audits/codex/2026-04-30-gate-1-rev3.md` Gate 1.5 entry conditions.
> Reproduce: `python3 scripts/audit_import_graph.py` (expect OK), `python3 -m pytest tests/ -q`, `ssh sniper@... pgrep ...` (expect empty).
> Spot-check the unit matrix and DB audit.
> Verdict: pass / partial / block.
> Output: `docs/audits/codex/2026-04-30-gate-1.5.md`.

Phase 0 closes when Codex returns `pass`.

---

## Self-review

**Spec coverage check:**

| Spec section | Implemented in task |
|---|---|
| §4.1 unit matrix (full inventory + verdicts + step-1 commands) | Tasks 1, 2, 3, 4 |
| §4.2 import-graph audit | Tasks 6, 7 |
| §4.3 DB write-surface audit | Task 8 |
| §4.4 smoke (pytest, validator, brain-index, import smoke) | Task 9 |
| §4.4 baseline (fixture or informational) | Task 10 |
| §4.5 STATUS.md + PAUSED_TIMERS.md updates | Task 5 |
| §4.5 brain memo | Task 11 |
| §5.1 Pattern B function list (locked from §4.3) | Task 8 step 6 (under heading "Pattern B function list") |
| Phase 0 closure / Gate 1.5 handoff | Task 12 |

No gaps.

**Placeholder scan:** none. Every step has concrete commands, code, or doc text.

**Type/name consistency:** `audit_import_graph.main()` returns `int`, called from `__main__` via `sys.exit(main())`. `audit_db_write_surface.main()` returns `int`, called via `raise SystemExit(main())`. Both consistent. The `LEGACY_TOP_LEVEL` set in `audit_import_graph.py` matches the spec's §4.2 list.

**Out-of-scope confirmation:** no module-level guards, no archive moves, no analyzer code edits, no new systemd unit installation. All deferred to later phases as documented.

**Parallel-work check:**
- Lane A (T1–T5): VPS admin + matrix doc + STATUS.md + PAUSED_TIMERS.md.
- Lane B (T6–T8, T10): two audit scripts, two audit docs, baseline JSON, all under disjoint paths.
- Sync at S1 (after both lanes commit). Then T9 (smoke), T11 (memo), T12 (closure) by either party.
- Zero file overlap between lanes verified by inspecting the file tables in each task.
- Concurrency rule "VPS write-access = single writer" enforced by Lane B's restriction to read-only VPS introspection.

Commit the plan, then offer execution choice.
