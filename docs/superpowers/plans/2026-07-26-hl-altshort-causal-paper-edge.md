# HL Alt-Short Causal Evidence and Forward Paper Arm Implementation Plan

> Execute locally, task by task, with RED-first tests. Do not delegate because the
> repository instructions reserve Codex subagents for explicit requests.

**Goal:** Determine whether HL wallet-selected alt shorts retain positive value after
causal regime selection, matched alt-beta controls, realistic entry/exit timing, fixed
capital, costs, concentration, and open-book marking; then create a provenance-complete
paper arm that can collect a valid forward sample.

**Architecture:** A pure evidence core owns point-in-time features, benchmarks, exit
counterfactuals, portfolio accounting, inference, and verdicts. A cache-only runner
binds those functions to immutable manifests. The paper loop persists an immutable arm
identity and per-copy entry provenance; the reporter reads only stored observations.
Funded execution is unchanged.

**Spec:** `docs/superpowers/specs/2026-07-26-hl-altshort-causal-paper-edge-design.md`

**Tech stack:** Python 3.13, stdlib `sqlite3/csv/json/hashlib/random/statistics`,
existing pandas/numpy only where the current cache format requires them, pytest.

## Global constraints

- No funded DB mutation, funding, order submission, real-gate override, or funded
  process restart.
- Historical study is cache-only. Network calls are forbidden in tests and study runs.
- Every decision feature stops at the last closed bar before the event.
- Primary entry is next tradeable bar open; same-close is diagnostic only.
- Primary beta control is the exact-interval leave-one-out alt basket.
- Primary inference clusters by UTC entry day; IID trade bootstrap is diagnostic only.
- Open positions must be marked; missing/stale marks block promotion.
- A paper arm only includes rows detected after its immutable UTC activation epoch.
- One logical change per commit. Run `git diff --check` before every commit.

---

### Task 1: Repair the known runtime correctness failures

**Files:**

- Modify: `sniper/settings/hl_real.py`
- Modify: `scripts/run_hl_real.py`
- Modify: `.env.example`
- Test: `tests/test_hl_real_settings.py`
- Test: `tests/test_run_hl_real.py`

**Steps:**

- [ ] Add RED tests proving `HL_REAL_ALT_SHORT_ONLY` accepts trimmed,
  case-insensitive true/false values and defaults true.
- [ ] Add a RED runner test using a minimal config double without
  `HL_REAL_ALT_SHORT_ONLY`; expected behavior is the historical default `False` for the
  call seam, not `AttributeError`.
- [ ] Run:

  ```bash
  python3 -m pytest -n 0 tests/test_hl_real_settings.py tests/test_run_hl_real.py -q
  ```

  Confirm the new tests fail and the existing six `AttributeError` failures reproduce.

- [ ] Parse the setting through `_b` and access it in the runner with
  `getattr(cfg, "HL_REAL_ALT_SHORT_ONLY", False)`.
- [ ] Add the setting and paper-only warning to `.env.example`.
- [ ] Rerun the focused tests, then:

  ```bash
  python3 -m pytest -n 0 tests/test_hl_altshort_filter.py tests/test_hl_real_exec.py tests/test_hl_real_settings.py tests/test_run_hl_real.py -q
  git diff --check
  ```

- [ ] Commit: `fix(hl): harden alt-short real config seam`

---

### Task 2: Establish one strategy taxonomy

**Files:**

- Create: `sniper/hyperlive/altshort_universe.py`
- Modify: `sniper/hyperlive/copy_filters.py`
- Modify: `sniper/hyperlive/copy_decomp.py`
- Modify: `scripts/hl_altshort_arm.py`
- Test: `tests/test_hl_altshort_universe.py`
- Modify: `tests/test_hl_altshort_filter.py`
- Modify: relevant decomposition tests found by `rg -l "major_vs_alt" tests`

**Interfaces:**

- `ALT_SHORT_EXCLUDED = frozenset({"BTC", "ETH", "SOL"})`
- `normalize_coin(value) -> str | None`
- `is_alt_short(coin, side) -> bool`
- `strategy_bucket(coin) -> "core" | "alt" | "unknown"`

**Steps:**

- [ ] Write RED tests for whitespace/case normalization, missing symbols, the exact
  exclusion set, and consistent filtering/decomposition buckets.
- [ ] Run the taxonomy/filter/decomposition tests and confirm the 12-coin versus 3-coin
  mismatch fails.
- [ ] Implement the module. Re-export compatibility names only where needed; all new
  code imports the canonical interfaces.
- [ ] Rerun focused tests and `git diff --check`.
- [ ] Commit: `refactor(hl): canonicalize alt-short universe`

---

### Task 3: Implement the causal evidence core — features and beta controls

**Files:**

- Create: `sniper/hyperlive/altshort_evidence.py`
- Create: `tests/test_hl_altshort_evidence.py`
- Create: `tests/fixtures/hl_altshort_causal/` small CSV/JSON fixtures

**Interfaces:**

- `causal_regime_features(bars_by_coin, *, decision_ts_ms, universe, funding=None)`
- `next_tradeable_price(bars, *, decision_ts_ms)`
- `matched_alt_basket_return(trade, bars_by_coin, *, universe, costs)`
- `passive_alt_basket(bars_by_coin, *, start_ms, end_ms, universe, costs)`

**Steps:**

- [ ] Create a fixture with a deliberately extreme bar after the decision timestamp.
- [ ] Write RED tests proving that future bar cannot alter a feature or label.
- [ ] Add RED tests for next-bar-open entry, missing next bar, leave-one-out exclusion,
  short-return sign, exact interval matching, 80% coverage, costs, and passive-basket
  drawdown/turnover.
- [ ] Run:

  ```bash
  python3 -m pytest -n 0 tests/test_hl_altshort_evidence.py -q
  ```

- [ ] Implement immutable result dataclasses and pure functions. Unknown/missing data
  produces explicit coverage/reason fields, never zero returns.
- [ ] Rerun focused tests and `git diff --check`.
- [ ] Commit: `feat(hl): add causal alt-short features and beta controls`

---

### Task 4: Implement exits, fixed-slot portfolio, inference, and verdicts

**Files:**

- Modify: `sniper/hyperlive/altshort_evidence.py`
- Create: `tests/test_hl_altshort_portfolio.py`
- Create: `tests/test_hl_altshort_verdict.py`

**Interfaces:**

- `simulate_exit(trade, bars, *, rule, costs)`
- `select_exit_rule(train, validation, *, candidates)`
- `evaluate_frozen_exit(holdout, *, rule)`
- `fixed_slot_portfolio(trades, *, slots, initial_equity, cost_model, marks, now_ms)`
- `block_bootstrap_mean(rows, *, cluster_key, replications, seed)`
- `concentration_report(rows)`
- `altshort_verdict(evidence, thresholds) -> Verdict`

**Steps:**

- [ ] Write RED tests for fixed horizons, TP/SL/cap priority, trailing giveback, and
  exit-reason attribution.
- [ ] Write RED tests that a holdout result cannot influence selected parameters.
- [ ] Write RED tests for overlapping admissions, deterministic slot rejection,
  chronological capital reuse, costs/funding, open MTM, stale marks, equity drawdown,
  and utilization.
- [ ] Write RED tests proving day clusters stay intact and seeded output is stable.
- [ ] Write table-driven verdict tests for pass, reject, continue, missing provenance,
  stale marks, insufficient clusters, cost stress, and concentration failures.
- [ ] Implement the smallest pure code that passes.
- [ ] Run:

  ```bash
  python3 -m pytest -n 0 tests/test_hl_altshort_evidence.py tests/test_hl_altshort_portfolio.py tests/test_hl_altshort_verdict.py -q
  git diff --check
  ```

- [ ] Commit: `feat(hl): add alt-short exits portfolio and verdicts`

---

### Task 5: Build the immutable historical study runner

**Files:**

- Create: `scripts/hl_altshort_study.py`
- Create: `data/research/altshort_study/manifest.v2.json`
- Create: `tests/test_hl_altshort_study.py`
- Modify: `data/research/altshort_study/PREREG.md`
- Modify: `data/research/altshort_study/MEMO.md` only after rerun

**CLI:**

```text
python3 scripts/hl_altshort_study.py \
  --manifest data/research/altshort_study/manifest.v2.json \
  --output-dir data/research/altshort_study/results/v2
```

**Steps:**

- [ ] Write RED tests for manifest schema, SHA mismatch, dirty-code flag, deterministic
  output, forbidden overlapping windows, missing cache, and sealed expected report.
- [ ] Implement cache readers and manifest validation. The script imports only pure
  evidence functions and contains no URL/network client.
- [ ] Produce `report.json`, `events.csv`, `portfolio.csv`, `exclusions.json`, and
  `run_meta.json`; sort rows and JSON keys for byte-stable reruns.
- [ ] Run fixture integration twice and compare hashes.
- [ ] Run `git diff --check`.
- [ ] Commit: `feat(hl): add reproducible causal alt-short study`

---

### Task 6: Add immutable forward-arm persistence

**Files:**

- Modify: `sniper/hyperlive/intel_store.py`
- Modify: `scripts/run_hl_copy.py`
- Create: `tests/test_hl_altshort_arm_store.py`
- Modify: `tests/test_run_hl_copy_selection.py`

**Interfaces in `intel_store.py`:**

- `create_altshort_arm(conn, manifest, *, activated_ts_ms, code_sha)`
- `get_altshort_arm(conn, arm_id)`
- `record_altshort_observation(conn, *, arm_id, copy_id, regime, benchmark, now_ms)`
- `update_altshort_observation_mark(conn, *, copy_id, mark, ts_ms)`
- `close_altshort_observation(conn, *, copy_id, exit_mark, exit_ts_ms, benchmark)`
- `list_altshort_observations(conn, *, arm_id)`
- `record_altshort_regime_marks(conn, marks, *, ts_ms)`
- `load_altshort_regime_bars(conn, *, start_ts_ms, end_ts_ms)`

**Steps:**

- [ ] Write RED schema/store tests for immutable arms, conflicting reinsert, exact epoch
  eligibility, idempotent identical observation, target/filter rejection, mark freshness,
  close attribution, and legacy DB migration.
- [ ] Add the three external-table DDL blocks in `ensure_schema`; the hourly regime
  tape is the causal lookback source and must not append every runner cycle.
- [ ] Implement parameterized store functions; transaction boundaries must leave a
  general paper row valid if arm metadata fails while emitting an explicit exclusion.
- [ ] Add `--arm-manifest` parsing to the copy runner. No manifest means capture can run
  but no row can claim forward-arm provenance.
- [ ] Write RED runner tests for exact entry-time feature snapshot and no adoption of
  old opens. Fake every API input.
- [ ] Implement entry, mark, and close hooks.
- [ ] Run:

  ```bash
  python3 -m pytest -n 0 tests/test_hl_altshort_arm_store.py tests/test_run_hl_copy_selection.py tests/test_hyperlive_intel_store.py -q
  python3 scripts/validate_schema_refs.py --warn-unknown-tables
  git diff --check
  ```

- [ ] Commit: `feat(hl): persist immutable alt-short paper provenance`

---

### Task 7: Harden watchdog, heartbeat, and arm reporting

**Files:**

- Modify: `scripts/hl_copy_watchdog.sh`
- Modify: `scripts/run_hl_copy.py`
- Rewrite: `scripts/hl_altshort_arm.py`
- Create: `tests/test_hl_copy_watchdog.py`
- Create: `tests/test_hl_altshort_arm_report.py`

**Steps:**

- [ ] Write RED watchdog tests using fake `/proc` command lines. Require exact DB,
  `--alt-short-only`, and `--arm-manifest`; reject the old unfiltered process.
- [ ] Write RED report tests for exact UTC epoch, closed PnL, open MTM, stale marks,
  matched beta residual, concentration, arm/provenance identity, and blocked verdicts.
- [ ] Extend heartbeat JSON with code SHA, dirty flag, arm ID, epoch, policy hash,
  normalized targets, taxonomy version, and filter state.
- [ ] Make the report consume stored observations only. Remove local-midnight parsing;
  accept `--arm-id` or exact RFC3339/epoch input for diagnostics.
- [ ] Run focused watchdog/report/runner tests and `git diff --check`.
- [ ] Commit: `feat(hl): make alt-short paper arm provenance visible`

---

### Task 8: Rerun the causal retrospective study

**Files:**

- Generate/modify: `data/research/altshort_study/results/v2/*`
- Modify: `data/research/altshort_study/MEMO.md`

**Steps:**

- [ ] Resolve and hash every required local input. If point-in-time universe, funding,
  or candles are missing, record exact gaps; do not fetch live data silently.
- [ ] Run the pinned study command twice and verify byte-identical compact outputs.
- [ ] Independently recompute the W2 leave-one-out benchmark from `events.csv` and
  compare it to `report.json`.
- [ ] Inspect train/validation/holdout exit selection, fixed-slot equity, cost stress,
  open MTM, concentration, exclusion counts, and day-block CI.
- [ ] Set the deterministic retrospective verdict. `paper_promote` means forward paper
  only. A missing required feed yields `blocked_missing_feed`.
- [ ] Update `MEMO.md` with observations versus verdicts and exact limitations.
- [ ] Run `git diff --check` and commit:
  `research(hl): record causal alt-short retrospective verdict`

---

### Task 9: Broad verification and review

**Steps:**

- [ ] Run the full relevant suite:

  ```bash
  python3 -m pytest -n 0 \
    tests/test_hl_altshort_filter.py \
    tests/test_hl_altshort_universe.py \
    tests/test_hl_altshort_evidence.py \
    tests/test_hl_altshort_portfolio.py \
    tests/test_hl_altshort_verdict.py \
    tests/test_hl_altshort_study.py \
    tests/test_hl_altshort_arm_store.py \
    tests/test_hl_altshort_arm_report.py \
    tests/test_hl_copy_watchdog.py \
    tests/test_hyperlive_copy_filters.py \
    tests/test_run_hl_copy_selection.py \
    tests/test_hyperlive_copy_scorecard.py \
    tests/test_hyperlive_intel_store.py \
    tests/test_hl_real_exec.py \
    tests/test_hl_real_settings.py \
    tests/test_run_hl_real.py -q
  python3 scripts/validate_schema_refs.py --warn-unknown-tables
  git diff --check
  ```

- [ ] Review the complete branch diff against the spec. Check for future leakage,
  accidental network access, funded-path behavior changes, and untracked evidence.
- [ ] Run a read-only arm report against a copy of the operational DB, never the source
  DB during tests.
- [ ] Fix every certain defect RED-first and rerun the smallest proving suite.

---

### Task 10: Activate and verify the forward paper arm

This task is paper-only and covered by the user's standing approval. It must still fail
closed on any mismatch.

**Steps:**

- [ ] Confirm clean pinned SHA, passing suite, valid manifest, normalized target set,
  zero funded configuration changes, and current paper PID/cmdline.
- [ ] Back up the paper SQLite DB and current heartbeat using recoverable copies.
- [ ] Use the repository's paper watchdog path to restart with the exact manifest.
- [ ] Record corrected UTC activation epoch and immutable arm identity.
- [ ] Verify process freshness, exact cmdline, heartbeat SHA/arm/epoch/policy/targets,
  and authenticated/read-only status endpoint if present.
- [ ] Observe one full cycle. Confirm no pre-epoch copy was adopted and new qualifying
  rows receive entry provenance; absence of a new signal is acceptable.
- [ ] Run the read-only reporter. State `paper_continue` until the forward sample gate
  fires, regardless of retrospective result.
- [ ] Do not touch the real-leg process, funded account, keys, allowlist, or orders.

---

### Task 11: Knowledge sync and branch close

**Steps:**

- [ ] Run the Knowledge Sync Gate in audit-only mode.
- [ ] Record the required knowledge delta for the new spec and research verdict. Any
  Supabase write requires the already granted task scope plus configured access; if
  unavailable, disclose it as unresolved rather than fabricating completion.
- [ ] Ensure the eventual PR body ends with the 11-question Knowledge Delta Report.
- [ ] Report branch status and commits. Do not push, merge, or open a PR unless the user
  explicitly requests those GitHub actions.
