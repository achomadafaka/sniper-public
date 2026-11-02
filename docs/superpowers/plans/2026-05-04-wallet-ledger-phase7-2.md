# Wallet Ledger Phase 7.2 Implementation Plan

> **Status:** v2 — closes Codex BLOCK on v1. Two fixes:
> (1) D6 helper signature corrected to
> `db.get_followed_wallets(status="active")` (verified at
> `sniper/persistence/db_followed.py:2008`); the column is `status`,
> not `enabled`. (2) `--auto-window` source corrected to read
> `capture_runs` from the **wallet warehouse**
> (`<ledger_root>/<wallet>.duckdb`), not the index. `capture_runs` is
> declared inside `ensure_wallet_schema` at
> `sniper/analysis/wallet_protocol/ledger/schema.py:18` and keyed by
> `(wallet, run_id)`. Reading per-wallet matches the schema and
> avoids any central-index drift surface.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take Phase 7.1's single-wallet manual CLI to a hands-off
runtime. Three pieces: batch wallet sources, window auto-derivation
from `index.duckdb` capture-run state, cron/timer wiring. End state:
a systemd user timer fires on a schedule, picks up every wallet with
an unanalyzed capture run, and writes one row set per wallet via the
landed Slice 6 path.

**Architecture:** All three pieces are extensions of
`scripts/run_ledger_pipeline.py`. Batch and auto-window are new flags
on the same CLI (no new entry points). Cron is a systemd unit that
invokes the CLI with the new flags; the unit lives under
`systemd/user/` per the existing Sniper convention.

**Tech Stack:** Python, argparse, DuckDB (read-only), systemd user
units.

---

## Locked Decisions

### D1. Batch sources — three flags, mutually exclusive with `--wallet`

* `--wallet <addr>` (existing) — single-wallet mode; required when
  no batch flag is supplied.
* `--from-list <path>` — newline-delimited file of `0x...` addresses.
  Comments (`#`) and blank lines are skipped. Invalid addresses log
  and skip with a per-line stderr warning; the run continues.
* `--from-followed` — reads the local SQLite `followed_wallets` table
  via `db.get_followed_wallets()`. Only wallets where
  `enabled = 1` participate.

`--wallet`, `--from-list`, `--from-followed` are mutually exclusive
(argparse's `add_mutually_exclusive_group(required=True)`).

### D2. Per-wallet error isolation — skip on failure, never fail-fast

A batch run analyzes N wallets. One wallet failing (Supabase HTTP
hiccup, missing warehouse, malformed activity row) must NOT abort
the remaining N-1.

Each per-wallet invocation runs inside a `try / except`. On
exception:

* Log to stderr: `wallet=<addr> ERROR class=<exc class> msg=<message>`.
* Increment a failure counter.
* Continue.

Final stderr line summarizes the batch:
`batch: ok=<N_ok> failed=<N_failed> skipped=<N_skipped>
total=<N_total>`

Exit code: 0 if every wallet completed (any verdict, including
`data_insufficient`); 1 if at least one wallet hit an exception.
This lets cron alert on real failures without alerting on
"no edge yet" verdicts.

### D3. Window auto-derivation — `--auto-window` flag

When `--auto-window` is set, the CLI ignores `--window-id`,
`--start-ts`, `--end-ts`, `--capture-run-id`, `--source-version` and
derives them per wallet from that wallet's own warehouse
(`<ledger_root>/<wallet>.duckdb`):

* `capture_runs` is part of the **wallet** schema (declared in
  `ensure_wallet_schema`,
  `sniper/analysis/wallet_protocol/ledger/schema.py:18`), keyed by
  `(wallet, run_id)`. Reading per-wallet matches the schema; there
  is no central index containing capture runs.
* For each wallet, open `<ledger_root>/<wallet>.duckdb` read-only
  and query `capture_runs` for the most recent `status='completed'`
  row, ordered by `finished_at DESC, started_at DESC`.
* `capture_run_id`: that row's `run_id`.
* `start_ts`, `end_ts`: that row's `started_at` (inclusive) and
  `finished_at` (inclusive).
* `window_id`: the literal `f"auto-{capture_run_id}"`. Stable —
  re-deriving for the same capture run produces the same window_id.
* `source_versions`: parsed from the row's `source_versions_json`
  column, tupled in deterministic key-sorted order.

If the wallet warehouse file is missing, the CLI logs
`wallet=<addr> SKIP reason=warehouse_missing` and counts toward
`skipped`.

If the warehouse exists but holds no `status='completed'` capture
row, the CLI logs
`wallet=<addr> SKIP reason=no_completed_capture` and counts toward
`skipped` (not `failed`). Skipped wallets do NOT bubble to exit code
1 — they are an expected steady state for newly-followed wallets.

Manual override remains possible: omitting `--auto-window` keeps the
explicit-flag path from Phase 7.1.

### D4. `run_id` derivation under `--auto-window`

Cron-driven runs need a deterministic `run_id` so re-runs hit
idempotency. When `--auto-window` is set AND `--run-id` is omitted,
`run_id` defaults to the same `capture_run_id` that drove the window.

Rationale: the analysis identity for a given capture-run is the
capture-run itself. A re-fired cron tick re-derives the same window
and the same `run_id`, hits insert-ignore on every writer, and
prints `wrote results=0 findings=0 artifacts=0`.

`--run-id <slug>` remains an explicit override for operators who
want a fresh slug.

### D5. Cron / timer — systemd user unit, hourly tick

Two unit files under `systemd/user/`:

* `sniper-ledger-pipeline.service`: oneshot type. ExecStart invokes
  `python3 scripts/run_ledger_pipeline.py --from-followed
  --auto-window --write`. Pulls
  `LEDGER_RUNTIME_SUPABASE_URL` / `_KEY` from `.env` via the
  in-CLI `load_dotenv()` (no `EnvironmentFile=` because `.env` is
  not a systemd-spec file).
* `sniper-ledger-pipeline.timer`: `OnCalendar=hourly`,
  `Persistent=true`, `RandomizedDelaySec=300` (5-minute jitter so
  multiple Sniper services don't all hit Supabase at the same
  instant).

Logs land in `journalctl --user -u sniper-ledger-pipeline.service`.
The unit writes the same stdout/stderr the manual CLI does — JSON
report stdout is captured by journald for ad-hoc inspection.

The plan does NOT enable the timer automatically. Operator runs
`systemctl --user enable --now sniper-ledger-pipeline.timer` once
the unit files land and they're satisfied with the behavior.

### D6. `db.get_followed_wallets()` is the only Sniper-DB touchpoint

`--from-followed` is the only path that reads Sniper's SQLite. Use
the existing accessor:

```python
db.get_followed_wallets(status: str = "active") -> list[sqlite3.Row]
```

Defined at `sniper/persistence/db_followed.py:2008`; backing column
is `status` (the `wallet_memberships` / `followed_wallets` schema
uses `status='active'` for enabled, not `enabled=1`). The CLI calls
`db.get_followed_wallets(status="active")` and pulls the `wallet`
field from each returned row.

No raw SQL in the new CLI per the project convention.

---

## File Structure

| Path | Role |
|---|---|
| `scripts/run_ledger_pipeline.py` | Modify: add `--from-list`, `--from-followed`, `--auto-window`, mutually-exclusive group, batch loop with error isolation. |
| `tests/test_run_ledger_pipeline_cli.py` | Modify: tests for batch sources (file + followed), `--auto-window`, error isolation, run_id default under auto-window. |
| `systemd/user/sniper-ledger-pipeline.service` | Create: oneshot unit invoking the CLI. |
| `systemd/user/sniper-ledger-pipeline.timer` | Create: hourly OnCalendar timer + 5-min jitter. |
| `tests/test_systemd_units_sniper_ledger.py` | Create: lint the unit files (Type=oneshot, OnCalendar=hourly, Persistent=true, ExecStart matches the documented invocation). |

No changes to ledger modules. No changes to `db.py` (read-only via
existing accessor).

---

## Tasks

### Task 0: Pre-flight — locked, no work needed

Both questions that v1 deferred to preflight are now resolved
inline:

* `db.get_followed_wallets(status="active")` exists at
  `sniper/persistence/db_followed.py:2008`. Returns
  `list[sqlite3.Row]` with a `wallet` column on each row.
* `capture_runs` is per-wallet (wallet warehouse), not central. See
  D3 for the read pattern.

No code, no commit. Proceed directly to Task 1.

### Task 1: Batch — `--from-list` source

**Files:**
- Modify: `scripts/run_ledger_pipeline.py`
- Modify: `tests/test_run_ledger_pipeline_cli.py`

- [ ] **Failing test:** `--from-list /path/to/file.txt` reads the
  file, calls `run_analysis_pipeline` once per address, with the
  same window flags applied to all of them.
- [ ] **Failing test:** comment lines (`#`) and blank lines in the
  list file are skipped.
- [ ] **Failing test:** invalid addresses (anything that fails
  `normalize_wallet`) log a warning and skip — they do NOT abort
  the batch.
- [ ] **Failing test:** `--wallet` and `--from-list` together are
  rejected by argparse (mutually-exclusive group).
- [ ] **Failing test:** the per-wallet `try / except` catches a
  raised pipeline error, logs it, and continues to the next wallet.
  Final exit code is 1 (any failure → 1) but the surviving wallets
  did get processed.
- [ ] **Failing test:** the batch summary line is printed last
  (regex match `^batch: ok=\d+ failed=\d+ skipped=\d+ total=\d+$`
  on the final stderr line).
- [ ] Implement.
- [ ] **Commit:** `feat(wallet-ledger): add --from-list batch
  source to Phase 7 CLI`.

### Task 2: Batch — `--from-followed` source

**Files:**
- Modify: `scripts/run_ledger_pipeline.py`
- Modify: `tests/test_run_ledger_pipeline_cli.py`

- [ ] **Failing test:** `--from-followed` calls
  `db.get_followed_wallets(status="active")` and feeds the
  returned rows' `wallet` field into the same batch loop as
  Task 1.
- [ ] **Failing test:** an empty followed-wallets list prints
  `batch: ok=0 failed=0 skipped=0 total=0` and exits 0 — empty is
  not a failure.
- [ ] **Failing test:** `--wallet`, `--from-list`, and
  `--from-followed` form a single mutually-exclusive group; pairing
  any two is rejected.
- [ ] Implement (db accessor patch on the CLI module so the test
  can stub it without touching SQLite).
- [ ] **Commit:** `feat(wallet-ledger): add --from-followed batch
  source to Phase 7 CLI`.

### Task 3: `--auto-window` — derive AnalysisWindow from capture_runs

**Files:**
- Modify: `scripts/run_ledger_pipeline.py`
- Modify: `tests/test_run_ledger_pipeline_cli.py`

- [ ] **Failing test:** `--auto-window` opens
  `<ledger_root>/<wallet>.duckdb` read-only (per-wallet — see D3),
  queries `capture_runs` for the most recent `status='completed'`
  row ordered by `finished_at DESC, started_at DESC`, and constructs
  an `AnalysisWindow` whose `capture_run_ids == (row.run_id,)`,
  `start_ts == row.started_at`, `end_ts == row.finished_at`,
  `window_id == f"auto-{row.run_id}"`, and `source_versions` is
  parsed from `row.source_versions_json` in key-sorted order.
- [ ] **Failing test:** when the wallet warehouse file is missing,
  the CLI prints `wallet=<addr> SKIP reason=warehouse_missing` and
  counts toward `skipped`. Exit code 0.
- [ ] **Failing test:** when the warehouse exists but holds no
  `status='completed'` capture row, the CLI prints
  `wallet=<addr> SKIP reason=no_completed_capture` and counts
  toward `skipped`. Exit code 0.
- [ ] **Failing test:** `--auto-window` makes `--window-id`,
  `--start-ts`, `--end-ts`, `--capture-run-id`,
  `--source-version` optional (parser still accepts them; CLI
  ignores them and prints a one-line warning when both are passed).
- [ ] **Failing test:** without `--run-id`, auto-window mode sets
  `run_id == capture_run_id` (D4). With `--run-id <slug>` the
  override wins.
- [ ] **Failing test:** auto-window + `--from-list` works — the
  derivation is per-wallet, not batch-shared. Each wallet gets
  the most recent completed capture from ITS own warehouse.
- [ ] Implement. Per-wallet open is necessary (each wallet has its
  own `.duckdb` file). Use a tight try/finally so the connection
  closes even on derivation errors.
- [ ] **Commit:** `feat(wallet-ledger): derive AnalysisWindow from
  capture_runs under --auto-window`.

### Task 4: systemd unit + timer

**Files:**
- Create: `systemd/user/sniper-ledger-pipeline.service`
- Create: `systemd/user/sniper-ledger-pipeline.timer`
- Create: `tests/test_systemd_units_sniper_ledger.py`

- [ ] **Failing test:** parse the `.service` file and assert:
  * `[Service]` section has `Type=oneshot`.
  * `ExecStart=` invokes
    `python3 scripts/run_ledger_pipeline.py --from-followed
    --auto-window --write` (path-relative-to-`WorkingDirectory`).
  * `WorkingDirectory=` matches the repo path on the VPS
    (`/home/sniper/sniper`).
  * `StandardOutput=journal`, `StandardError=journal`.
- [ ] **Failing test:** parse the `.timer` file and assert:
  * `OnCalendar=hourly`.
  * `Persistent=true`.
  * `RandomizedDelaySec=300`.
  * `[Install]` `WantedBy=timers.target`.
- [ ] **Failing test:** the README inside `systemd/user/` (or a
  one-line comment in each unit) names the operator command to
  enable: `systemctl --user enable --now
  sniper-ledger-pipeline.timer`. Pin the exact string so a future
  silent rename surfaces.
- [ ] Implement the unit files. Use the existing
  `sniper-news-intel.timer` as a syntactic template (same OnCalendar
  shape).
- [ ] Operator note (no code): enabling the timer is operator work;
  the plan ships the units only.
- [ ] **Commit:** `feat(wallet-ledger): add systemd timer for
  Phase 7.2 cron path`.

### Task 5: Phase 7.2 verification

- [ ] `python3 -m pytest tests/test_run_ledger_pipeline_cli.py
  tests/test_systemd_units_sniper_ledger.py
  tests/test_wallet_ledger_run_pipeline.py -q` — green.
- [ ] `python3 -m pytest tests/ -q` — green (no regression).
- [ ] `python3 scripts/validate_schema_refs.py` — clean.
- [ ] `python3 scripts/brain_index.py generate` — no diff.
- [ ] Module guard: `scripts/run_ledger_pipeline.py` still has no
  `bot.supabase_helper`, `requests`, `httpx`, `urllib.request`
  imports.

---

## Out Of Scope (Phase 7.3 +)

* Concurrency / parallel wallet analysis. Serial is fine at the
  current followed-wallet count; multi-process is a Phase 7.3
  knob if cron tick runs longer than the timer interval.
* Cross-wallet aggregation views. Phase 8.
* Phase 2D LLM input layer. Still gated on real `wallet_findings`
  rows — Phase 7.2 is what produces them.
* Backfill of pre-Phase-7 capture runs. The first cron tick on a
  given wallet is the first row written for that wallet.
* Auto-prune of `superseded` rows. Operator decision; brain-hygiene
  cadence.

---

## Risk Notes

* **Idempotent re-run risk under cron.** D4's `run_id ==
  capture_run_id` means re-firing the timer for an unchanged capture
  run is a no-op insert across all three writers. Verified end-to-end
  in Phase 7.1's smoke test (`b94da16`).
* **Failed-capture handling.** A capture-run that landed
  `status='failed'` is NOT picked up by the auto-window query (D3
  filters to `'completed'`). This is intentional — analyzing a
  partial capture would write half-truth findings. The tradeoff:
  failed captures stay un-analyzed until the operator re-runs the
  capture. Acceptable.
* **Supabase quota under cron.** Hourly tick × N followed wallets ×
  3 row sets per wallet. At today's 12 followed wallets that's 864
  rows/day worst case (most are no-op on idempotency); the project's
  500MB free tier swallows this comfortably.
* **Time skew.** `--auto-window`'s `start_ts == row.started_at`
  inclusive, `end_ts == row.finished_at` inclusive. A capture that
  spans midnight does not get artificially split — the window is the
  capture-run identity, not a calendar day.
