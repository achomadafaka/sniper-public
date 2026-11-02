# Wallet Ledger Phase 7 Implementation Plan

> **Status:** v2 — closes Codex BLOCK on v1. Three fixes:
> (1) `RunResult.write_counts` to surface writer return counts to the
> CLI (D9 + small change to `run_pipeline.py`); (2) Task 4 cleanup
> supersedes `wallet_findings` alongside `wallet_analysis_results`;
> (3) D3 loads `.env` via `python-dotenv` before reading env vars,
> matching `scripts/event_trigger.py` convention.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the landed Slice 6 ledger pipeline produce real
Supabase rows during normal operation by adding a manual CLI caller.
Cron wiring is Phase 7.2; this plan is Phase 7.1 only.

**Architecture:** A new `scripts/run_ledger_pipeline.py` CLI calls
`run_analysis_pipeline(...)` against the local DuckDB ledger root and
optionally writes to Supabase via an explicit-constructor
`RestSupabaseHttp`. Dry-run is the default. Window construction is
explicit-flag-driven in Phase 7.1; auto-derivation from capture-run
index is deferred to Phase 7.2.

**Tech Stack:** Python, argparse, existing Slice 5 / Slice 6 modules,
DuckDB (read-only), Supabase REST.

---

## Locked Decisions

### D1. New script, not extension of `run_wallet_protocol.py`

`scripts/run_wallet_protocol.py` is the protocol orchestrator + the
calibration harness's `--from-fixture` shim. Its argv shape is
fixture-centric and adding a `--from-ledger` mode there would tangle
the calibration test surface with a runtime path that nothing else
in `tests/` uses.

Phase 7.1 ships a sibling: `scripts/run_ledger_pipeline.py`. Its
job is single-purpose — materialize a wallet from the ledger, run the
protocol, optionally write the three Slice 6 row sets. No fixture
mode. No replay flags beyond what `--seed` / `--n-iter` already do.

### D2. Dry-run is the default; `--write` opts in

The CLI never writes to Supabase unless `--write` is passed. Without
it, the pipeline runs with `supabase=None` and prints the
`ProtocolReport` JSON + a count of derived findings to stdout.

This protects against accidental writes during exploration. It also
mirrors how `scripts/run_wallet_protocol.py --no-persist` behaves:
the operator opts into persistence rather than opting out of it.

### D3. Supabase env vars: dedicated runtime pair, separate from calibration

Two distinct env vars, named for clarity:

* `LEDGER_RUNTIME_SUPABASE_URL`
* `LEDGER_RUNTIME_SUPABASE_KEY`

Rationale: `CALIBRATION_LIVE_SUPABASE_URL` / `_KEY` are scoped to
test fixtures (Slice 6 Task 9). Reusing them for the runtime caller
would collapse two distinct authorities — calibration smoke vs
real-analysis writes — into one env-var pair, which is exactly the
coupling Slice 6 D6 paid to avoid.

The CLI loads `.env` via `python-dotenv` at startup before reading
either var, matching the existing
`scripts/event_trigger.py:229` convention
(`load_dotenv(REPO_ROOT / ".env")` — no-op when the var is already
set in the shell, so `EnvironmentFile=` semantics under systemd are
preserved). After loading, the CLI reads
`os.environ["LEDGER_RUNTIME_SUPABASE_URL"]` and `..._KEY` directly.

`--write` fails loudly with a clear error message if either var is
missing post-load. No `os.environ` reads inside any ledger module —
the CLI reads the vars and constructs
`RestSupabaseHttp(url=..., key=...)` explicitly.

### D4. Window construction: explicit flags in Phase 7.1

The CLI takes the four `AnalysisWindow` fields as explicit flags:

* `--window-id <slug>` (required)
* `--start-ts <unix>` (required)
* `--end-ts <unix>` (required)
* `--capture-run-id <id>` (one or more; required, at least one)
* `--source-version <key=value>` (zero or more; optional)

Auto-deriving the window from `index.duckdb`'s capture-runs table
is a useful Phase 7.2 convenience but adds a separate read path
that this plan does not need to test today. Manual flags first; the
operator runs with the same window the capture runbook produced.

### D5. Wallet source: single `--wallet` flag only

Phase 7.1 runs one wallet per invocation. Batch modes
(`--from-list <file>`, `--from-followed`) are Phase 7.2.

### D6. Run ID: explicit `--run-id` flag, no auto-uuid default

The `run_id` is the immutable identity of an analysis run (per
Slice 6 D2 idempotency). Auto-generating a uuid every invocation
would mask re-runs and prevent the writer's natural-key idempotency
from doing its job — every re-run would write a fresh row.

The CLI requires `--run-id <slug>` explicitly. Operators who want
fresh runs pass a fresh slug; operators who want to verify an earlier
run's idempotency pass the same slug and observe `inserted=0`.

### D7. Code version: pinned at runtime via `git rev-parse`

The CLI calls `git rev-parse --short HEAD` once at startup and
passes the resulting hash through to
`run_analysis_pipeline(code_version=...)`. Failure to read the hash
(e.g. running outside a git checkout) prints a one-line warning and
falls back to `"unknown"`.

This matches Slice 6 Task 3's `code_version` provenance contract:
the column is informational, not a primary key, and an "unknown"
value is the right "we don't know" signal.

### D8. Ledger root: `--ledger-root` flag with `LEDGER_ROOT` env fallback

`--ledger-root <path>` is required unless `LEDGER_ROOT` is set in
the environment, in which case the env var is used. Mirrors how
existing scripts handle paths (`MARKET_DATA_PATH` etc.).

### D9. `RunResult.write_counts` — surface writer return values

Slice 6's writers each return an integer count of new rows inserted
(0 on the idempotent re-run path). `run_analysis_pipeline` currently
discards those counts (`run_pipeline.py:83-118`). Phase 7's CLI
needs them to print the success line `wrote results=1 findings=N
artifacts=M` accurately — claiming `inserted=1` when the second
re-run actually inserted 0 would lie about idempotency.

Two paths considered:

* (a) Drop write counts from CLI stderr; print only `run_id` + a
  generic `wrote: ok` line.
* (b) Add a `write_counts: dict[str, int]` field to `RunResult` and
  capture each writer's return value in `run_analysis_pipeline`.

**Pick: (b).** Operators reading the CLI output need to know whether
a re-run was a no-op. A stderr line that says `wrote results=1
findings=3 artifacts=1` vs `wrote results=0 findings=0 artifacts=0`
is the primary user-facing signal that idempotency held.

Surface:

```python
@dataclass
class RunResult:
    report: ProtocolReport
    findings: list[Finding]
    artifact_paths: list[Path]
    replay_surface_meta: dict
    write_counts: dict[str, int] = field(default_factory=dict)
    # Keys when supabase is provided:
    #   "wallet_analysis_results", "wallet_findings", "artifact_registry"
    # Each value is the count of NEW rows the writer inserted.
    # Empty dict on dry-run.
```

The change is additive (new field with default), keeps the dry-run
contract intact (empty dict), and is byte-equal-safe for the existing
Slice 6 tests because nothing tests the field's absence — only its
contents on writes.

---

## File Structure

| Path | Role |
|---|---|
| `sniper/analysis/wallet_protocol/ledger/run_pipeline.py` | Modify: add `RunResult.write_counts` and capture writer return values per D9. |
| `tests/test_wallet_ledger_run_pipeline.py` | Modify: add tests asserting `write_counts` shape on dry-run vs `--write` paths. |
| `scripts/run_ledger_pipeline.py` | Phase 7.1 CLI. argparse + main loop. |
| `tests/test_run_ledger_pipeline_cli.py` | argv parsing, dry-run / `--write` branching, env-var gating, exit codes. |

No changes to other ledger modules. No changes to existing scripts.

---

## Tasks

### Task 0: `RunResult.write_counts` (per D9)

**Files:**
- Modify: `sniper/analysis/wallet_protocol/ledger/run_pipeline.py`
- Modify: `tests/test_wallet_ledger_run_pipeline.py`

- [ ] **Failing test:** dry-run invocation returns
  `result.write_counts == {}` (no writers fired).
- [ ] **Failing test:** `--write` equivalent (stub supabase passed
  through) returns `result.write_counts ==
  {"wallet_analysis_results": 1, "wallet_findings": <N>,
  "artifact_registry": <0 or 1>}` matching what the writers
  inserted on the first pass.
- [ ] **Failing test:** second pass with the same `(wallet, run_id,
  window.window_id)` returns
  `result.write_counts == {"wallet_analysis_results": 0,
  "wallet_findings": 0, "artifact_registry": 0}` — idempotency
  holds.
- [ ] **Failing test:** the existing `RunResult` callers (none in
  production yet, only tests) still work — `write_counts` defaults
  to `{}` so the dataclass change is additive.
- [ ] Implement: capture each writer's return value into a local
  dict and pass it through to `RunResult(...)`. Dry-run path leaves
  the field as `{}`.
- [ ] **Commit:** `feat(wallet-ledger): expose RunResult.write_counts
  for runtime callers`.

### Task 1: CLI shape + parser tests

**Files:**
- Create: `tests/test_run_ledger_pipeline_cli.py`

- [ ] **Failing test:** running with no args prints usage and exits
  non-zero. Pin via `pytest.raises(SystemExit)` on
  `mod.main([])`.
- [ ] **Failing test:** `--help` exits 0 and the help text mentions
  `--wallet`, `--window-id`, `--start-ts`, `--end-ts`,
  `--capture-run-id`, `--ledger-root`, `--write`, `--run-id`,
  `--seed`, `--n-iter`.
- [ ] **Failing test:** missing required flag (`--wallet`) exits
  non-zero with a stderr message naming the missing flag.
- [ ] **Failing test:** `--write` without
  `LEDGER_RUNTIME_SUPABASE_URL` / `LEDGER_RUNTIME_SUPABASE_KEY`
  exits non-zero with a stderr message naming the missing env vars.
  Use `monkeypatch.delenv(..., raising=False)` to ensure the env is
  clean.
- [ ] **Failing test:** `--ledger-root` is required when `LEDGER_ROOT`
  is unset; conversely the env-var fallback works when the flag is
  omitted.

### Task 2: Dry-run pipeline integration

**Files:**
- Create: `scripts/run_ledger_pipeline.py`

- [ ] **Failing test:** dry-run invocation against a synthetic
  warehouse (built inline in the test like Slice 5's integration
  fixtures) prints the `ProtocolReport` JSON to stdout and a
  `derived N findings` line to stderr. Exit code 0. No Supabase
  client constructed — verified by grepping the captured stderr for
  `dry-run`.
- [ ] **Failing test:** the JSON on stdout is round-trippable via
  `json.loads` and contains `wallet`, `overall_verdict`,
  `gate_result`.
- [ ] Implement `main(argv)` to:
  * Load `.env` at the repo root via `load_dotenv(REPO_ROOT /
    ".env")` BEFORE reading any env var. Match
    `scripts/event_trigger.py:229`. `python-dotenv` is already a
    project dep (other scripts use it).
  * Parse args.
  * Resolve `ledger_root` from `--ledger-root` or `LEDGER_ROOT`.
  * Resolve `code_version` via `git rev-parse --short HEAD`,
    falling back to `"unknown"` on any failure (subprocess timeout
    or non-git checkout).
  * Build `AnalysisWindow` from flags. `--source-version` flags are
    parsed `key=value` and tupled.
  * Call `run_analysis_pipeline(...)` with `supabase=None` when
    `--write` is absent.
  * Print `dataclasses.asdict(result.report)` JSON to stdout;
    `derived N findings` line to stderr.
  * Return 0.

### Task 3: Live-write integration (gated)

**Files:**
- Modify: `scripts/run_ledger_pipeline.py`

- [ ] **Failing test:** `--write` constructs `RestSupabaseHttp(url=
  env_url, key=env_key)` and passes it through to
  `run_analysis_pipeline`. Use `monkeypatch.setattr` on
  `scripts.run_ledger_pipeline.RestSupabaseHttp` to capture the
  constructor args; verify the writers are called.
- [ ] **Failing test:** `--write` with the two env vars set but
  pointing at a non-resolvable host fails fast with a non-zero exit
  code. Mock the HTTP transport to raise; assert exit code != 0.
- [ ] Implement: when `--write` is set, instantiate
  `RestSupabaseHttp(url=os.environ["LEDGER_RUNTIME_SUPABASE_URL"],
  key=os.environ["LEDGER_RUNTIME_SUPABASE_KEY"])` and pass to
  `run_analysis_pipeline(supabase=...)`. On any HTTP / writer
  exception, print the traceback and return non-zero.
- [ ] Final stderr line on success when `--write` is the literal
  template:
  `wrote results={result.write_counts["wallet_analysis_results"]}
  findings={result.write_counts["wallet_findings"]}
  artifacts={result.write_counts["artifact_registry"]}
  run_id={run_id}`. Pulls from `RunResult.write_counts` (D9), so a
  re-run with the same `--run-id` correctly prints `wrote results=0
  findings=0 artifacts=0 run_id=<slug>`. **Failing test:** asserts
  the re-run line uses the zeros, not the first-pass counts.

### Task 4: Smoke run + manual verification

- [ ] Manual: build a tiny seeded warehouse for a synthetic wallet
  (one closed position) using the existing
  `tests/test_wallet_ledger_slice5_integration.py` setup helpers as
  a reference. Run `python3 scripts/run_ledger_pipeline.py --wallet
  0xaaa --window-id smoke-1 --start-ts ... --end-ts ... --capture-run-id
  smoke --run-id smoke-1 --ledger-root /tmp/ledger-smoke` (no
  `--write`). Confirm stdout JSON.
- [ ] Manual: with `LEDGER_RUNTIME_SUPABASE_URL` /
  `LEDGER_RUNTIME_SUPABASE_KEY` set in `.env`, re-run with `--write`
  against the synthetic wallet. Verify rows appear in Supabase via
  `mcp__supabase__execute_sql`:

  ```sql
  SELECT wallet, run_id, window_id, overall_verdict, status
  FROM wallet_analysis_results
  WHERE run_id = 'smoke-1';

  SELECT count(*) FROM wallet_findings WHERE run_id = 'smoke-1';
  ```

  Cleanup respects D3 of Slice 6 (results + findings supersede;
  artifacts append-only stays). Run via a small inline Python
  invocation, not the CLI:

  ```python
  from sniper.analysis.wallet_protocol.ledger.findings import mark_superseded
  from sniper.analysis.wallet_protocol.ledger.supabase_io import RestSupabaseHttp

  sb = RestSupabaseHttp(url=..., key=...)
  natural_key = {"wallet": "0xaaa", "run_id": "smoke-1", "window_id": "smoke-1"}
  mark_superseded(
      table="wallet_analysis_results", natural_key=natural_key,
      superseded_by="phase7-smoke-cleanup", supabase=sb,
  )
  mark_superseded(
      table="wallet_findings", natural_key=natural_key,
      superseded_by="phase7-smoke-cleanup", supabase=sb,
  )
  # artifact_registry rows stay — append-only per Slice 6 D3. The
  # synthetic-namespace path is the only cleanup signal.
  ```
- [ ] No code commit for the manual verification — the test suite
  covers the assertions; the manual step proves end-to-end.

### Task 5: Phase 7 verification

- [ ] `python3 -m pytest tests/test_run_ledger_pipeline_cli.py
  tests/test_wallet_ledger_run_pipeline.py -q` — green (the latter
  covers the Task 0 `write_counts` change).
- [ ] `python3 -m pytest tests/ -q` — green (no regression).
- [ ] `python3 scripts/validate_schema_refs.py` — clean.
- [ ] `python3 scripts/brain_index.py generate` — no diff.
- [ ] No `bot.supabase_helper`, `requests`, `httpx` import in
  `scripts/run_ledger_pipeline.py`. `urllib.request` is allowed only
  through `RestSupabaseHttp`'s default transport — the script
  itself does not import it directly.

---

## Out Of Scope (Phase 7.2 +)

* Cron / systemd-timer wiring (`sniper-ledger-pipeline.timer`).
* Batch wallet sources (`--from-list`, `--from-followed`).
* Window auto-derivation from `index.duckdb` capture runs.
* Backfilling pre-Phase-7 wallets — first run on each wallet is the
  first row written.
* Phase 2D LLM input layer — explicit dependency on real
  `wallet_findings` rows from Phase 7 invocations, deferred until
  rows exist.

---

## Risk Notes

* **Idempotent re-run risk.** `--run-id` is operator-supplied, so
  passing the same slug twice is a no-op insert (D2 of Slice 6).
  The CLI prints `inserted=0` in that case, matching the writer
  return value. No data loss surface.
* **Env-var leakage.** `--write` requires both env vars; the CLI
  passes them to `RestSupabaseHttp(...)` and never logs them.
* **Network timeout.** `RestSupabaseHttp`'s default transport uses
  `urllib.request.urlopen(timeout=30.0)`. A slow Supabase round-trip
  fails with a non-zero exit but no retry; this is intentional for
  Phase 7.1 (the operator re-runs with the same `--run-id` to retry
  idempotently). Auto-retry is Phase 7.2 if needed.
