# Memecoin Pattern Study Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, read-only study that classifies memecoin chart shapes, measures conviction and thesis context, and compares frozen exit policies without changing live execution.

**Architecture:** Pure modules under `sniper/research/memecoin_strategy/` compute path metrics, event context, and policy outcomes. A CLI adapter reads either legacy or v2 tables through a read-only SQLite URI, never pools lanes, and writes manifest/CSV/Markdown artifacts. Existing exit-search gates remain authoritative for optimization.

**Tech Stack:** Python 3, standard-library `dataclasses`, `datetime`, `csv`, `hashlib`, `json`, `sqlite3`, and pytest.

## Global Constraints

- No imports from `sniper.execution`, `memecoin_real_leg`, or `memecoin_real_runner`.
- Open source databases with SQLite `mode=ro`; no SQL writes.
- Never pool legacy and v2 rows in one rate, comparison, or evidence hash.
- Sparse horizon marks are descriptive only; policy simulation requires ordered dense paths.
- Solana and entry market cap below $5 million are the primary population.
- Repeated sightings form features within one `(network_id, mint, UTC day)` episode.
- All timestamps must be timezone-aware.
- Existing 21-day/200-path/100-coin/90%-coverage exit-search gate is unchanged.

---

### Task 1: Path Metrics And Archetypes

**Files:**
- Create: `sniper/research/memecoin_strategy/pattern_paths.py`
- Create: `tests/test_memecoin_pattern_paths.py`

**Interfaces:**
- Produces: `PathQuote`, `PathMetrics`, `analyze_path(entry_price, signal_at, quotes, *, complete) -> PathMetrics`, and `classify_path(metrics) -> str`.
- Consumes: no repository state; only primitive values and ordered quote dictionaries.

- [ ] **Step 1: Write failing tests for validation and path ordering**

```python
def test_analyze_path_rejects_naive_time_and_ignores_bad_quotes():
    with pytest.raises(ValueError, match="timezone aware"):
        analyze_path(1.0, "2026-07-11T00:00:00", [], complete=True)
    metrics = analyze_path(1.0, START, [
        quote(1, 60, 100.0, quality="ratio_outlier"),
        quote(2, 120, 1.6),
    ], complete=True)
    assert metrics.max_gain_pct == pytest.approx(60)
```

- [ ] **Step 2: Run the path tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_pattern_paths.py -q`

Expected: collection fails because `pattern_paths` does not exist.

- [ ] **Step 3: Implement typed quote normalization and continuous metrics**

Implement timezone-aware parsing, positive finite price checks, stable sorting by `(observed_at, price, id)`, and metrics for gain, drawdown, peak/trough timing, recovery, new high after trough, terminal return, duration, and completeness. Bad-quality quotes remain counted as excluded but never affect prices.

- [ ] **Step 4: Add failing boundary tests for all six archetypes**

```python
@pytest.mark.parametrize(("prices", "expected"), [
    ([1.0, 1.61, 1.30], "immediate_runner"),
    ([1.0, 1.4, 0.6, 1.5], "deep_recovery_new_high"),
    ([1.0, 1.4, 0.6, 1.1], "deep_recovery"),
    ([1.0, 1.3, 0.7, 0.8], "pump_fade"),
    ([1.0, 0.8, 0.6], "terminal_failure"),
    ([1.0, 1.1, 1.05], "slow_or_unresolved"),
])
def test_archetypes_are_deterministic(prices, expected):
    assert classify_path(path_from_prices(prices)) == expected
```

- [ ] **Step 5: Implement deterministic mutually-exclusive classification**

Use the order and thresholds in the approved design. Return `incomplete` when `complete=False`; never infer a terminal archetype from a partial path.

- [ ] **Step 6: Run tests and commit**

Run: `python3 -m pytest tests/test_memecoin_pattern_paths.py -q`

Expected: all pass.

```bash
git add sniper/research/memecoin_strategy/pattern_paths.py tests/test_memecoin_pattern_paths.py
git commit -m "feat(memecoin): classify price path archetypes"
```

### Task 2: Conviction, Thesis, And Re-Entry Features

**Files:**
- Create: `sniper/research/memecoin_strategy/pattern_context.py`
- Create: `tests/test_memecoin_pattern_context.py`

**Interfaces:**
- Consumes: episode signal time, normalized feed rows, thesis rows, and post-stop quote rows.
- Produces: `conviction_windows(...) -> dict`, `thesis_context(...) -> dict`, and `find_fresh_conviction_reentry(...) -> ReentrySignal | None`.

- [ ] **Step 1: Write failing tests for rolling distinct-wallet activity**

Test 5/15/30/60-minute boundaries, buy/sell notional, repeat buyers, unknown cohort flags, EVM mint normalization, and v2 curated-only counts.

- [ ] **Step 2: Run context tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_pattern_context.py -q`

Expected: collection fails because `pattern_context` does not exist.

- [ ] **Step 3: Implement point-in-time conviction features**

Order rows by observation time and stable id. Count distinct users, not events. Legacy returns only `observed_activity`; v2 returns both observed and `was_curated=1` features. Preserve unknown cohort-row counts.

- [ ] **Step 4: Write failing tests for thesis chronology**

Verify first-thesis lag from episode buy, poster-own-buy lag, `thesis_stance()` buckets, missing poster buy, post-thesis forward return, and no use of receipt rows after the report as-of time.

- [ ] **Step 5: Implement thesis context**

Reuse `sniper.analysis.memecoin_thesis.thesis_stance`. Emit counts and nullable lags; never turn missing evidence into zero.

- [ ] **Step 6: Write failing tests for fresh-conviction re-entry**

Verify two distinct curated buyers after the stop, exclusion of an original-wallet-only echo, 30-minute window, 20% reclaim from the post-stop low, first quote at or after the confirming event, and no fill at the low.

- [ ] **Step 7: Implement re-entry detection and commit**

Run: `python3 -m pytest tests/test_memecoin_pattern_context.py -q`

Expected: all pass.

```bash
git add sniper/research/memecoin_strategy/pattern_context.py tests/test_memecoin_pattern_context.py
git commit -m "feat(memecoin): measure conviction and thesis context"
```

### Task 3: Frozen Policy Simulation And Matched Controls

**Files:**
- Create: `sniper/research/memecoin_strategy/pattern_policies.py`
- Create: `tests/test_memecoin_pattern_policies.py`

**Interfaces:**
- Consumes: dense path dictionaries, frozen `POLICIES`, modeled basis-point costs, and optional `ReentrySignal`.
- Produces: `PolicyResult`, `simulate_policy(...)`, `match_past_control(...)`, and `rank_policies(...)`.

- [ ] **Step 1: Write failing first-touch and cost tests**

Cover conservative same-timestamp ordering, four consecutive stop confirmations, exact observed-price fills, time caps, entry/exit costs, and incomplete-path rejection.

- [ ] **Step 2: Run policy tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_pattern_policies.py -q`

Expected: collection fails because `pattern_policies` does not exist.

- [ ] **Step 3: Implement fixed full-exit policies**

Wrap the existing `simulate_exit_path` contract for baseline TP/SL/time-cap policies and retain the reason, exit time, capital, gross PnL, net PnL, and net return.

- [ ] **Step 4: Write failing principal-plus-runner tests**

At +60%, sell enough quantity at the actual observed crossed quote to recover the original notional, then test +150%, stop, and 24-hour exits on the remaining quantity. Verify that total realized proceeds and both exit costs are used.

- [ ] **Step 5: Implement principal-plus-runner and one-re-entry accounting**

Re-entry adds its notional to capital deployed. Combined return is combined net PnL divided by original plus re-entry capital. Never allow more than one re-entry.

- [ ] **Step 6: Write failing past-only control and ranking tests**

Controls must match lane, network, market-cap band, liquidity band, initial-return band, use a different mint, and occur no later than the case. Ranking must require at least 20 distinct coins, nonnegative median, and a 10th-percentile loss no worse than baseline.

- [ ] **Step 7: Implement controls/ranking and commit**

Run: `python3 -m pytest tests/test_memecoin_pattern_policies.py -q`

Expected: all pass.

```bash
git add sniper/research/memecoin_strategy/pattern_policies.py tests/test_memecoin_pattern_policies.py
git commit -m "feat(memecoin): compare frozen path policies"
```

### Task 4: Read-Only Loaders, Artifacts, And CLI

**Files:**
- Create: `scripts/research/memecoin_pattern_study.py`
- Create: `tests/test_memecoin_pattern_study.py`
- Modify: `scripts/research/normalize_clock_domains.py`
- Modify: `docs/superpowers/specs/2026-07-11-memecoin-pattern-study-design.md`

**Interfaces:**
- Consumes: SQLite path, `legacy|v2` lane, as-of timestamp, cost basis points, output directory.
- Produces: deterministic JSON manifest, episode CSV, policy CSV, and Markdown report.

- [ ] **Step 1: Write failing in-memory loader tests**

Seed minimal legacy and v2 schemas independently. Verify episode dedup, clock exclusions, Solana/sub-$5M primary eligibility, dense-path completeness, sparse-path descriptive status, point-in-time cohort preservation, and no lane pooling.

- [ ] **Step 2: Run study tests and confirm RED**

Run: `python3 -m pytest tests/test_memecoin_pattern_study.py -q`

Expected: import fails because the CLI module does not exist.

- [ ] **Step 3: Implement lane-specific loaders and pure orchestration**

Use parameterized SQL and explicit column lists. Validate capture version 2 through `assert_capture_version`. Detect duplicate episode keys before analysis. Return records rather than writing to SQLite.

- [ ] **Step 4: Write failing artifact and reproducibility tests**

Run the command twice against the same fixture DB and fixed `--asof`. Assert byte-identical CSV analytical rows and equal evidence hashes. Assert blocked claims for thin data and explicit exclusion counts.

- [ ] **Step 5: Implement JSON/CSV/Markdown writers and CLI guards**

Open SQLite through `file:<path>?mode=ro`. Reject naive `--asof`, invalid costs, unsupported lanes, and an output target that resolves to the source DB file. Include source-row bounds, SQL-lane label, parameters, exclusions, coverage, artifact hashes, and evidence hash.

- [ ] **Step 6: Document exact usage and claim boundary**

Add a `Usage` section to the design with:

```bash
python3 scripts/research/memecoin_pattern_study.py \
  --db sniper_tracker.db --lane legacy \
  --asof 2026-07-11T23:59:59+00:00 \
  --output-dir docs/research/assets/memecoin-pattern-study
```

State that legacy output discovers hypotheses only and v2 forward output cannot promote before existing gates.

- [ ] **Step 7: Run focused verification and commit**

Run:

```bash
python3 -m pytest \
  tests/test_memecoin_pattern_paths.py \
  tests/test_memecoin_pattern_context.py \
  tests/test_memecoin_pattern_policies.py \
  tests/test_memecoin_pattern_study.py -q
python3 scripts/validate_schema_refs.py
```

Expected: all tests pass and schema validation reports no violations.

```bash
git add scripts/research/memecoin_pattern_study.py tests/test_memecoin_pattern_study.py \
  docs/superpowers/specs/2026-07-11-memecoin-pattern-study-design.md
git commit -m "feat(memecoin): add reproducible pattern study"
```

### Task 5: Adversarial Review And Branch Verification

**Files:**
- Review only: all files changed since `14953e562108ae2c86584a546c62c5f6477df3a4`

**Interfaces:**
- Consumes: completed branch and test artifacts.
- Produces: a clean, reviewed feature branch; no merge or runtime change.

- [ ] **Step 1: Inspect the complete changed-file inventory and diff**

Run:

```bash
git diff --name-status 14953e562108ae2c86584a546c62c5f6477df3a4..HEAD
git diff --check 14953e562108ae2c86584a546c62c5f6477df3a4..HEAD
```

- [ ] **Step 2: Run import-boundary and read-only checks**

Run:

```bash
rg -n "sniper\.execution|memecoin_real_leg|memecoin_real_runner|INSERT|UPDATE|DELETE" \
  sniper/research/memecoin_strategy/pattern_*.py scripts/research/memecoin_pattern_study.py
```

Expected: no forbidden imports and no SQL mutation in the CLI.

- [ ] **Step 3: Run focused plus adjacent regression suites**

Run:

```bash
python3 -m pytest \
  tests/test_memecoin_pattern_paths.py \
  tests/test_memecoin_pattern_context.py \
  tests/test_memecoin_pattern_policies.py \
  tests/test_memecoin_pattern_study.py \
  tests/test_memecoin_capture_v2_research.py \
  tests/test_memecoin_strategy_exit_search_v0.py -q
python3 scripts/validate_schema_refs.py --warn-unknown-tables
```

- [ ] **Step 4: Run a read-only current-data smoke study**

Run the legacy and v2 commands with a fixed as-of and a temporary output directory. Confirm that the source database checksum, modification time, and row counts do not change.

- [ ] **Step 5: Apply the Knowledge Sync Gate**

The spec and plan are the knowledge delta. No Supabase write is needed because the feature introduces no validated research finding or strategy thesis. Record: `Knowledge delta: spec and plan added; no strategy verdict produced.`

- [ ] **Step 6: Report branch state without merging or restarting**

Run: `git status --short --branch`

Expected: clean feature branch, live checkout still on `main`, pinned runner untouched.
