# HL Cross-Sectional Rollover Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, cache-only backtest and falsification report for the frozen 24-hour cross-sectional winner-rollover hypothesis.

**Architecture:** A pure `rollover_research` module owns policy validation, causal features, signal admission, sampled exits, matched-basket residuals, clustered inference, and diagnostics. A thin research CLI loads and hashes the existing cache, invokes the pure module, and atomically writes versioned outputs. The existing alt-short studies and paper runtimes remain untouched.

**Tech Stack:** Python 3.13, pandas, numpy, stdlib JSON/CSV/hashlib/random/pathlib, pytest.

## Global Constraints

- Implement `docs/superpowers/specs/2026-07-27-hl-cross-sectional-rollover-backtest-design.md` exactly.
- Historical analysis is cache-only; no HTTP, SDK, S3, SQLite, service, paper-arm, or funded imports.
- Rule v1 values are immutable inputs, not optimizer defaults.
- Every feature ends at the decision close; entry is the next hourly open.
- Exit checks sampled hourly closes only.
- Full 96-hour maturity is required; censored events are excluded and counted.
- Primary inference clusters by UTC entry day.
- One logical change per commit; stage named files only.

---

### Task 1: Freeze and validate the research policy

**Files:**
- Create: `sniper/hyperlive/rollover_research.py`
- Create: `tests/test_hl_rollover_research.py`
- Create: `data/research/hl_rollover_backtest/policy.v1.json`

**Interfaces:**
- `RolloverPolicy.from_dict(value: dict) -> RolloverPolicy`
- `RolloverPolicy.to_dict() -> dict`
- `policy_sha256(policy: RolloverPolicy) -> str`

- [ ] **Step 1: Write failing policy tests**

  Assert the exact exclusions, 720-hour/80% eligibility, percentile `0.95`, rollover `0`, quota `5`, TP `200`, cap `96`, and cost `39`. Reject missing, unknown, non-finite, or changed values when `policy_version="rollover-v1"`.

- [ ] **Step 2: Verify RED**

  ```bash
  python3 -m pytest -n 0 tests/test_hl_rollover_research.py -q
  ```

  Expected: import failure for `sniper.hyperlive.rollover_research`.

- [ ] **Step 3: Implement immutable policy parsing and canonical hashing**

  Use a frozen dataclass. Hash compact sorted-key JSON plus a trailing newline. The committed JSON must round-trip byte-for-byte to `to_dict()` values.

- [ ] **Step 4: Verify GREEN and commit**

  ```bash
  python3 -m pytest -n 0 tests/test_hl_rollover_research.py -q
  git diff --check
  git add sniper/hyperlive/rollover_research.py tests/test_hl_rollover_research.py data/research/hl_rollover_backtest/policy.v1.json
  git commit -m "feat(hl): freeze rollover research policy"
  ```

---

### Task 2: Implement causal signals, sampled exits, and matched beta

**Files:**
- Modify: `sniper/hyperlive/rollover_research.py`
- Modify: `tests/test_hl_rollover_research.py`

**Interfaces:**
- `build_causal_frames(candles: dict[str, DataFrame], policy: RolloverPolicy) -> CausalFrames`
- `simulate_rollover(frames: CausalFrames, funding: dict[str, DataFrame], policy: RolloverPolicy, *, max_open: int | None = None) -> list[dict]`
- `attach_matched_basket(events: list[dict], frames: CausalFrames, funding: dict[str, DataFrame], policy: RolloverPolicy) -> list[dict]`

- [ ] **Step 1: Write RED causality tests**

  Fixtures must prove: a future extreme bar cannot change eligibility or ranks; a same-close spike cannot become the entry; a level does not retrigger; simultaneous crossings rank by 24-hour return; rolling quota and one-position-per-coin hold; entries without 96 future hours are counted as censored.

- [ ] **Step 2: Write RED accounting tests**

  Prove short-return sign, sampled-close TP, TP-before-cap ordering, exact `[entry, exit)` funding, 39 bps costs, leave-signal-coin-out basket construction, and 80% benchmark coverage.

- [ ] **Step 3: Verify RED**

  ```bash
  python3 -m pytest -n 0 tests/test_hl_rollover_research.py -q
  ```

- [ ] **Step 4: Implement minimal pure functions**

  Reindex to an hourly UTC grid. Eligibility uses only 720 prior slots with at least 576 populated. Do not forward-fill prices or funding. Missing funding invalidates an event when the coin was listed and an expected settlement is absent.

- [ ] **Step 5: Verify GREEN and commit**

  ```bash
  python3 -m pytest -n 0 tests/test_hl_rollover_research.py -q
  git diff --check
  git add sniper/hyperlive/rollover_research.py tests/test_hl_rollover_research.py
  git commit -m "feat(hl): simulate causal rollover events"
  ```

---

### Task 3: Add inference, time localization, and tail falsification

**Files:**
- Modify: `sniper/hyperlive/rollover_research.py`
- Modify: `tests/test_hl_rollover_research.py`

**Interfaces:**
- `block_bootstrap(events: list[dict], field: str, *, seed: int, replications: int) -> dict`
- `period_report(events: list[dict], *, period: str) -> list[dict]`
- `causal_regime_report(events: list[dict]) -> list[dict]`
- `continuation_feature_report(events: list[dict]) -> list[dict]`
- `historical_verdict(report: dict) -> dict`

- [ ] **Step 1: Write RED inference tests**

  Prove complete UTC days remain intact, seeded output is stable, sparse periods are descriptive, top-coin removal is recomputed, and the verdict cannot become `candidate_continue` without positive residual CI, two positive calendar months, positive absolute mean under doubled per-side slippage, coverage, and valid provenance. Matched residual does not change when the same flat cost is charged to both signal and basket.

- [ ] **Step 2: Write RED tail-feature tests**

  Build a fixture where cap severity differs while hit rate does not. Assert features are copied from the decision row only and that missing OI/liquidation/basis/book fields are reported as unavailable rather than synthesized.

- [ ] **Step 3: Verify RED, implement, and verify GREEN**

  ```bash
  python3 -m pytest -n 0 tests/test_hl_rollover_research.py -q
  git diff --check
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add sniper/hyperlive/rollover_research.py tests/test_hl_rollover_research.py
  git commit -m "feat(hl): report rollover persistence and tail risk"
  ```

---

### Task 4: Build the deterministic cache-only runner

**Files:**
- Create: `scripts/research/hl_rollover_backtest.py`
- Create: `tests/test_hl_rollover_backtest.py`
- Create: `data/research/hl_rollover_backtest/README.md`

**CLI:**

```text
python3 scripts/research/hl_rollover_backtest.py \
  --cache-dir /absolute/path/to/altshort_study/cache \
  --policy data/research/hl_rollover_backtest/policy.v1.json \
  --output-dir /absolute/path/to/new/results-directory
```

- [ ] **Step 1: Write RED runner tests**

  Use a temporary fixture cache. Assert no network modules are imported; input hashes and scoped code SHA are present; incomplete cache, dirty research code, invalid policy, output collision, and funding gaps fail explicitly; two clean runs produce byte-identical files except an excluded wall-clock field.

- [ ] **Step 2: Verify RED**

  ```bash
  python3 -m pytest -n 0 tests/test_hl_rollover_backtest.py -q
  ```

- [ ] **Step 3: Implement loader and atomic writer**

  Write `events.csv`, `periods.csv`, `regimes.csv`, `feature_falsification.csv`, `report.json`, and `run_meta.json` into a temporary sibling directory, then rename it. Refuse an existing destination.

- [ ] **Step 4: Run focused tests**

  ```bash
  python3 -m pytest -n 0 tests/test_hl_rollover_research.py tests/test_hl_rollover_backtest.py -q
  python3 scripts/validate_schema_refs.py --warn-unknown-tables
  git diff --check
  ```

- [ ] **Step 5: Commit**

  ```bash
  git add scripts/research/hl_rollover_backtest.py tests/test_hl_rollover_backtest.py data/research/hl_rollover_backtest/README.md
  git commit -m "feat(hl): add reproducible rollover backtest"
  ```

---

### Task 5: Execute the local historical run and publish the result

**Files:**
- Create: `data/research/hl_rollover_backtest/results/v1/events.csv`
- Create: `data/research/hl_rollover_backtest/results/v1/periods.csv`
- Create: `data/research/hl_rollover_backtest/results/v1/regimes.csv`
- Create: `data/research/hl_rollover_backtest/results/v1/feature_falsification.csv`
- Create: `data/research/hl_rollover_backtest/results/v1/report.json`
- Create: `data/research/hl_rollover_backtest/results/v1/run_meta.json`
- Create: `data/research/hl_rollover_backtest/MEMO.v1.md`

- [ ] **Step 1: Run against the existing immutable local cache**

  ```bash
  python3 scripts/research/hl_rollover_backtest.py \
    --cache-dir /home/bigpapi/Desktop/Sniper/data/research/altshort_study/cache \
    --policy data/research/hl_rollover_backtest/policy.v1.json \
    --output-dir data/research/hl_rollover_backtest/results/v1
  ```

- [ ] **Step 2: Re-run to a temporary destination and compare hashes**

  Expected: byte-identical research outputs after excluding the documented run timestamp.

- [ ] **Step 3: Write the memo from generated fields only**

  Lead with absolute and residual confidence intervals, monthly localization, cap-loss severity, unavailable feeds, and `older_history=blocked_paid_archive`. Do not convert results into a trade recommendation.

- [ ] **Step 4: Run the focused verification and commit**

  ```bash
  python3 -m pytest -n 0 tests/test_hl_rollover_research.py tests/test_hl_rollover_backtest.py -q
  git diff --check
  git add data/research/hl_rollover_backtest/results/v1 data/research/hl_rollover_backtest/MEMO.v1.md
  git commit -m "research(hl): publish rollover backtest v1"
  ```

## Self-review

- Spec coverage: all frozen-rule, causality, benchmark, inference, tail, provenance, and safety requirements map to Tasks 1–5.
- Placeholder scan: no deferred implementation text or unspecified interface remains.
- Type consistency: policy, frames, event, report, and runner interfaces are introduced before consumption.
- Scope: one cache-only research subsystem; archive acquisition and paper-arm design remain separate.
