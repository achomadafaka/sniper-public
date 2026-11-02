# Proven-Bad Memecoin Wallet Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a dry-run-first, atomic, backed-up wallet exclusion utility using the canonical honest copy-evidence metric.

**Architecture:** One importable CLI module loads canonical wallet stats, filters watch-only targets,
backs up the existing SQLite ledger, then revalidates and mutates under one immediate transaction.
Subprocess tests exercise the real CLI and temporary SQLite databases.

**Tech Stack:** Python 3, argparse, sqlite3, pathlib, pytest.

## Global Constraints

- Never open live or production databases during tests.
- Never create a database when the requested path does not exist.
- Never mutate without `--apply --expect-count N --expect-digest SHA256`.
- Never target `real_approved=1` wallets.
- Keep the canonical metric in `db_memecoin.alpha_b_copy_stats_all()`.

---

### Task 1: Pin the operator contract with failing CLI tests

**Files:**
- Create: `tests/test_cleanup_proven_bad_memecoin_wallets.py`
- Create: `scripts/cleanup_proven_bad_memecoin_wallets.py`

**Interfaces:**
- Consumes: temporary databases initialized through `db_memecoin.ensure_memecoin_tables()`.
- Produces: CLI behavior for dry-run, apply confirmation, backup, manifest, and rollback.

- [ ] **Step 1: Write failing subprocess tests**

Create fixtures with one qualifying watch-only wallet, one positive wallet, and one qualifying but
real-approved wallet. Assert:

```python
assert dry_run.returncode == 0
assert "bad-watch" in dry_run.stdout
assert "real-bad" not in dry_run.stdout
assert row_after_dry_run["excluded"] == 0
```

Also assert a missing DB path is not created, apply without count/digest confirmation fails unchanged,
count or target-digest mismatch fails unchanged, exact apply writes one backup and manifest, and an
aborting SQLite trigger rolls back both targets.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m pytest tests/test_cleanup_proven_bad_memecoin_wallets.py -q
```

Expected: FAIL because the script is absent from `scripts/`.

### Task 2: Implement the hardened utility

**Files:**
- Create: `scripts/cleanup_proven_bad_memecoin_wallets.py`
- Test: `tests/test_cleanup_proven_bad_memecoin_wallets.py`

**Interfaces:**
- `open_existing_db(path: Path, *, writable: bool) -> sqlite3.Connection`
- `load_targets(conn, *, min_closes: int, min_pnl: float) -> list[dict]`
- `backup_database(conn, db_path: Path, timestamp: str) -> tuple[Path, Path]`
- `apply_targets(conn, targets: list[dict], *, min_closes: int, min_pnl: float) -> int`

- [ ] **Step 1: Implement existing-file URI opens**

Use `Path.resolve().as_uri()` with SQLite `mode=ro` for dry-run and `mode=rw` for apply. Set
`PRAGMA query_only=ON` in dry-run mode.

- [ ] **Step 2: Implement canonical target selection**

Call `alpha_b_copy_stats_all(conn)` and join those results to active watch-only rows from
`memecoin_wallets`. Filter with `n_closes >= min_closes` and `pnl <= -min_pnl`.

- [ ] **Step 3: Implement backup and manifest**

Use `sqlite3.Connection.backup()` into an exclusive new path. Reopen the backup read-only and require
`PRAGMA integrity_check` to return exactly `ok`. Atomically write the planned JSON manifest.

- [ ] **Step 4: Implement atomic apply**

Run `BEGIN IMMEDIATE`, reload the targets, compare exact source-user-ID sets with the preview, then
execute one guarded `UPDATE ... WHERE source_user_id IN (...)`. Require `rowcount == len(targets)`;
otherwise rollback and raise. Commit only after every check passes.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
python3 -m pytest tests/test_cleanup_proven_bad_memecoin_wallets.py tests/test_memecoin_exclusion.py -q
python3 scripts/validate_schema_refs.py --warn-unknown-tables
git diff --check
```

Expected: all tests pass, no schema violations, clean diff.

- [ ] **Step 6: Commit and push**

Stage only the script, tests, design, and plan. Commit:

```bash
git commit -m "feat(memecoin): add audited bad-wallet cleanup"
git push origin main
```
