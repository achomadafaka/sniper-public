# Memecoin Funded Entry Contract and Deterministic Regime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every new funded memecoin reservation carry a deterministic, immutable entry classification and exit contract; use exact route quotes to refuse confirmed post-peak collapses; enforce one cross-venue portfolio envelope; and expose the frozen decision in the real-money cockpit.

**Architecture:** Pure classifier and policy modules build canonical JSON before reservation. The Solana/EVM risk transactions freeze the contract on the attempt and pending row, executors copy it byte-for-byte to confirmed positions, and exit evaluation reads frozen rails with operator overrides layered on top. Existing positions without a contract retain the deployed legacy path. No new network calls are introduced.

**Tech Stack:** Python 3, SQLite, Flask, vanilla JavaScript, pytest, systemd/pinned runtime deployment.

## Global Constraints

- Execute inline in this worktree; the operator explicitly prohibited subagents.
- Use strict TDD: add one failing behavior test, confirm the expected failure, implement the smallest production change, rerun focused tests, then commit.
- One logical behavior per commit. Do not mix unrelated cockpit work from another branch.
- Coin identity is `(network_id, mint)`; Robinhood is `4663`.
- `global_feed` and `follow_activity` remain distinct facts.
- Use corrected real UTC supplied by the runner. Do not sample host wall time for decisions.
- Add no funded hot-path network request. Reuse existing stats, local SQLite observations, and the executor's normalized exact quote.
- Market state may keep or reduce a clip only. It may not veto, enlarge size, loosen exits, suppress exits, re-enter, or add size.
- Nominal post-peak state is descriptive until a normalized exact route quote confirms the drawdown before signing, approval, pre-transaction, or broadcast.
- Keep chain-local bankroll and gas-reserve math. Apply only concurrency and total-exposure limits globally.
- Preserve reservation, write-ahead signature/nonce, no-double-submit, balance-delta fill, kill-switch, daily-loss, exact-exit confirmation, and reconciliation invariants.
- Existing positions without a funded contract continue through current rollover/cohort logic unchanged.
- New contract columns are additive. Prior releases may read the DB, but rollback must not run old exit logic against new contract positions without an operator decision.

---

### Task 1: Corrected-clock snapshots and additive contract storage

**Files:**
- Modify: `sniper/services/memecoin_features.py`
- Modify: `sniper/services/memecoin_copy_trader.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `tests/test_memecoin_entry_snapshot.py`
- Modify: `tests/test_memecoin_observation_schema.py`
- Create: `tests/test_memecoin_funded_contract_storage.py`

**Interfaces:**

```python
def build_snapshot(
    sig,
    *,
    mcap,
    liquidity,
    token_age_s,
    consensus_n_60m,
    execution_lane,
    decided_at: str,
) -> dict: ...

def canonical_funded_json(value: dict) -> tuple[str, str]:
    """Return compact sorted JSON and lowercase SHA-256 hex digest."""

def set_entry_attempt_classification(
    conn,
    attempt_id: str,
    *,
    classification_json: str,
    classification_hash: str,
    classification_version: int,
    commit: bool = True,
) -> bool: ...
```

Schema additions on `memecoin_entry_attempts`:

```text
classification_version INTEGER
classification_hash TEXT
classification_json TEXT
funded_contract_version INTEGER
funded_contract_hash TEXT
funded_contract_json TEXT
```

Schema additions on `memecoin_pending_entries` and `memecoin_positions`:

```text
funded_contract_version INTEGER
funded_contract_hash TEXT
funded_contract_json TEXT
```

- [x] Add a snapshot test passing `decided_at="2026-07-18T12:00:00+00:00"` while host time is different; assert stored `decided_at` equals the supplied value.
- [x] Add schema tests asserting all nine additive columns exist after `ensure_memecoin_tables()` on a fresh and upgraded DB.
- [x] Add storage tests proving canonicalization is independent of dict insertion order, non-finite values are rejected, same-hash attempt writes are idempotent, and a different second hash raises without changing the first row.
- [x] Run `python3 -m pytest tests/test_memecoin_entry_snapshot.py tests/test_memecoin_observation_schema.py tests/test_memecoin_funded_contract_storage.py -q`; expect the supplied-clock assertion, schema assertions, and import to fail.
- [x] Change `build_snapshot` to serialize `decided_at` directly and change `_log_entry_snapshot` to pass its corrected `now_iso`.
- [x] Add the columns through the existing additive migration loops, canonical JSON helper, immutable attempt write, and pending/position contract-copy helpers. Reject booleans, `NaN`, and infinity during canonical validation.
- [x] Rerun the focused tests and `python3 scripts/validate_schema_refs.py --warn-unknown-tables`.
- [x] Commit `feat(memecoin): persist immutable funded contracts`.

### Task 2: Deterministic entry classification

**Files:**
- Create: `sniper/services/memecoin_market_state.py`
- Create: `tests/test_memecoin_market_state.py`
- Modify: `sniper/settings/memecoin.py`
- Create: `tests/test_memecoin_real_policy_validation.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class EntryClassification:
    classification_version: int
    network_id: str
    mint: str
    signal_source: str
    decision_at: str
    coin_band: str
    lifecycle: str
    liquidity_class: str
    momentum_phase: str
    market_regime: str
    data_quality: str
    risk_scale: float
    reason_codes: tuple[str, ...]
    facts: dict[str, object]

def classify_funded_entry(
    conn,
    *,
    network_id: str,
    mint: str,
    decision_at: str,
    signal_source: str,
    effective_mcap: float | None,
    assumed_micro: bool,
    market_stats: dict,
    base_clip_usd: float | None,
    consensus_n_60m: int | None,
) -> EntryClassification: ...
```

Settings, with these deployed defaults:

```text
MEMECOIN_FUNDED_CLASSIFICATION_VERSION=1
MEMECOIN_FUNDED_REGIME_RISK_SCALE=0.75
MEMECOIN_FUNDED_TAPE_WINDOW_S=900
MEMECOIN_FUNDED_TAPE_MIN_OBSERVATIONS=3
MEMECOIN_FUNDED_TAPE_MIN_SPAN_S=30
MEMECOIN_FUNDED_POST_PEAK_RUNUP_PCT=20
MEMECOIN_FUNDED_POST_PEAK_GIVEBACK_PCT=30
MEMECOIN_FUNDED_REGIME_BUCKET_S=900
MEMECOIN_FUNDED_REGIME_BASELINE_BUCKETS=23
MEMECOIN_FUNDED_REGIME_MIN_NONEMPTY_BUCKETS=8
MEMECOIN_FUNDED_REGIME_MIN_BASELINE_BUYS=30
MEMECOIN_FUNDED_REGIME_MAX_STALENESS_S=180
```

- [x] Add pure boundary tests for `assumed_nano`, `nano`, `micro`, `small`, `mid`, and audit-only `out_of_scope`; assert unresolved mcap is never serialized as zero and `out_of_scope` cannot be reserved.
- [x] Add lifecycle tests at one hour and 24 hours, including `revival` only when usable momentum runup is at least 20%.
- [x] Add liquidity tests around `20x` and `100x` base clip, and `unknown` when clip/liquidity is unavailable or non-positive.
- [x] Add momentum tests for fewer than three rows, less than 30 seconds, future-row exclusion, exact-chain identity, `flat`, `climbing`, `extended`, and `post_peak`. Only `quality IN ('ok','probe_ok')` and positive prices count.
- [x] Add market-regime tests for stale/insufficient `unknown`, `hot_broad`, `hot_narrow`, `thin`, and `normal`; assert current/future rows never enter the completed-bucket baseline and `signal_source` is preserved.
- [x] Add fail-open read-error coverage: classification returns explicit `classification_read_failed`, `market_regime='unknown'`, and `risk_scale=0.75` instead of raising.
- [x] Add validation tests for booleans, non-finite/negative values, scale outside `(0,1]`, bad thresholds, and non-positive bucket/tape settings.
- [x] Run `python3 -m pytest tests/test_memecoin_market_state.py tests/test_memecoin_real_policy_validation.py -q`; expect module import and setting validation failures.
- [x] Implement bounded SELECT-only helpers using `julianday(...)`, corrected `decision_at`, exact `(network_id,mint)`, deterministic medians, stable reason-code ordering, and no writes/network calls.
- [x] Extend `validate_real_exit_policy()` to fail closed on every new numeric setting and threshold ordering error.
- [x] Rerun the focused tests.
- [x] Commit `feat(memecoin): classify funded entry state`.

### Task 3: Frozen funded policy resolver and mid-band correction

**Files:**
- Create: `sniper/services/memecoin_funded_policy.py`
- Create: `tests/test_memecoin_funded_policy.py`
- Modify: `tests/test_memecoin_micro_policy.py`
- Modify: `tests/test_memecoin_real_runner.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class FundedExitPolicy:
    policy_id: str
    policy_version: int
    tp_pct: float
    derisk_pct: float
    derisk_fraction: float
    trail_pct: float
    trail_arm_pct: float
    stop_pct: float
    catastrophe_pct: float
    stop_debounce_sweeps: int
    max_hold_s: float
    unquotable_exit_s: float
    mirror_on_sell: bool

def resolve_funded_policy(*, coin_band: str, network_id: str) -> FundedExitPolicy: ...

def build_funded_contract(
    *,
    classification: EntryClassification,
    base_clip_usd: float,
    approved_clip_usd: float,
    policy: FundedExitPolicy,
    origin_event_id: str | None,
) -> dict: ...
```

- [x] Add resolver tests pinning all bands: assumed/nano `40/40/-60`, micro `50/50/-60`, small current rollover defaults, mid `30/35/-60`; pin chain-specific normal stop, catastrophe, debounce, max hold, unquotable, and mirror facts.
- [x] Add contract tests for exact identity/source/time, base and approved clip, expectation and invalidation objects, all policy values, version, canonical JSON, and stable hash.
- [x] Run `python3 -m pytest tests/test_memecoin_funded_policy.py tests/test_memecoin_micro_policy.py tests/test_memecoin_real_runner.py -q`; expect module import and mid-hook failures.
- [x] Implement the pure resolver from current deployed settings. Keep legacy `assign_micro_policy` readable but stop using it as authority for new contract rows.
- [x] Build expectation text from exact numeric policy facts, never expected PnL.
- [x] Rerun focused tests.
- [x] Commit `feat(memecoin): freeze funded exit policy`.

### Task 4: Global portfolio envelope with regime clip scaling

**Files:**
- Modify: `sniper/services/memecoin_risk.py`
- Modify: `sniper/services/memecoin_evm_risk.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `tests/test_memecoin_risk_context.py`
- Modify: `tests/test_memecoin_evm_risk.py`
- Modify: `tests/test_memecoin_fraction_sizing.py`
- Modify: `tests/test_memecoin_funded_contract_storage.py`

**Interfaces:**

```python
def funded_book_snapshot(conn) -> dict:
    """All open real positions plus status='reserved' pending entries, counted once."""

def scaled_funded_clip(*, base_clip_usd: float, risk_scale: float, floor_usd: float) -> float:
    return min(base_clip_usd, max(floor_usd, base_clip_usd * risk_scale))

def reserve_entry(..., classification: EntryClassification | None = None): ...
def reserve_entry_evm(..., classification: EntryClassification | None = None): ...
```

- [x] Add DB tests with Solana, `8453`, and `4663` open rows plus reservations; assert global count/notional includes each unresolved reservation once and excludes released/confirmed rows.
- [x] Add Solana risk tests proving EVM rows consume global slots/exposure but do not increase required SOL exit reserve or Solana sizing equity.
- [x] Add EVM tests proving other chains consume global slots/exposure but do not increase the chain-local approval/exit gas reserve or EVM sizing equity.
- [x] Add scale tests for `100*1.0=100`, `100*0.75=75`, floor-neutralized `50*0.75=50` when floor is 50, never above base, and rejection of invalid scales.
- [x] Add transaction tests proving reservation notional equals approved scaled clip, the attempt and pending hashes match in the same commit, and a contract construction error returns `invalid_funded_contract` without a pending row.
- [x] Run `python3 -m pytest tests/test_memecoin_risk_context.py tests/test_memecoin_evm_risk.py tests/test_memecoin_fraction_sizing.py tests/test_memecoin_funded_contract_storage.py -q`; expect cross-chain count and scaled-notional failures.
- [x] Split each risk context into chain-local bankroll/gas facts and global cap facts. Pass global count/notional to `RiskContext`; retain chain-local pending notional for available-USDC math and chain-local count for gas reserve.
- [x] In each `BEGIN IMMEDIATE` reservation transaction: compute base clip, finalize classification with that clip, apply scale, validate/build/canonicalize contract, immutably persist attempt classification/contract, then insert pending with the same bytes/hash.
- [x] Keep legacy callers working when `classification is None`; they receive current behavior until the funded hook wiring lands in Task 5.
- [x] Rerun focused tests.
- [x] Commit `feat(memecoin): enforce global funded portfolio cap`.

### Task 5: End-to-end contract wiring and pending-to-position copy

**Files:**
- Modify: `sniper/services/memecoin_real_runner.py`
- Modify: `sniper/services/memecoin_real_leg.py`
- Modify: `sniper/services/memecoin_evm_leg.py`
- Modify: `sniper/execution/solana_swap_executor.py`
- Modify: `sniper/execution/evm_swap_executor.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `tests/test_memecoin_entry_trace.py`
- Modify: `tests/test_memecoin_entry_executor.py`
- Modify: `tests/test_memecoin_evm_entry_executor.py`
- Modify: `tests/test_memecoin_real_runner.py`

**Interfaces:**

```python
def real_entry(..., classification: EntryClassification | None = None): ...
def evm_real_entry(..., classification: EntryClassification | None = None): ...

def open_position(...,
                  funded_contract_version=None,
                  funded_contract_hash=None,
                  funded_contract_json=None): ...
```

- [x] Add full-hook tests for all four resolved bands plus assumed nano. Assert the draft uses resolved identity/mcap/stats/source and the reservation finalizes liquidity from the balance-aware base clip.
- [x] Add a full `make_real_hook` regression with `effective_mcap=1_500_000` that reaches a confirmed fill and asserts the persisted contract has `policy_id='funded-mid-v1'`, de-risk 30, trail 35, stop 60. Confirm it fails against the current `micro_class in ('nano','micro')` hook.
- [x] Add early-terminal tests proving a classification draft is written to the attempt for mcap/consensus/impact refusal with unavailable facts explicitly unknown.
- [x] Add Solana and `4663` executor tests asserting confirmed position JSON/hash/version exactly equal the pending row and entry attempt. Include crash-recovery confirmation paths.
- [x] Add a new-cohort invariant test: confirmed fill with missing/mismatched pending contract raises and leaves reconciliation-visible state; pre-contract pending rows keep legacy compatibility.
- [x] Run `python3 -m pytest tests/test_memecoin_entry_trace.py tests/test_memecoin_entry_executor.py tests/test_memecoin_evm_entry_executor.py tests/test_memecoin_real_runner.py -q`; expect missing propagation/copy failures.
- [x] Build the classification draft in `make_real_hook` after identity/mcap/stats resolution and pass it through the real legs to the reservation functions. Persist it before any terminal return where an attempt exists.
- [x] Copy pending contract columns directly into `open_position` on confirmed balance-delta fill. Do not parse and rebuild JSON in the executor.
- [x] Remove post-fill `assign_micro_policy` authority for contract rows; keep legacy assignment for old/no-contract calls only.
- [x] Rerun the focused tests.
- [x] Commit `feat(memecoin): wire funded contracts through fills`.

### Task 6: Exact-route post-peak refusal before broadcast

**Files:**
- Modify: `sniper/services/memecoin_market_state.py`
- Modify: `sniper/execution/solana_swap_executor.py`
- Modify: `sniper/execution/evm_swap_executor.py`
- Modify: `sniper/services/memecoin_entry_trace.py`
- Modify: `tests/test_memecoin_entry_executor.py`
- Modify: `tests/test_memecoin_evm_entry_executor.py`
- Modify: `tests/test_memecoin_entry_trace.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class PostPeakConfirmation:
    nominated: bool
    confirmed: bool
    peak_price: float | None
    display_price: float | None
    exact_price: float | None
    exact_giveback_pct: float | None
    reason: str

def confirm_post_peak(
    classification: EntryClassification | dict,
    *,
    exact_quote_price: float | None,
) -> PostPeakConfirmation: ...
```

- [x] Add pure tests: no nomination never blocks; nominal `post_peak` without exact price fails open; incomparable/non-positive/non-finite exact price fails open; exact giveback below 30% passes; exact giveback at/above 30% confirms.
- [x] Add Solana executor tests proving confirmation runs after quote normalization/impact checks but before wallet signing/broadcast. Assert `post_peak_confirmed`, reservation release, attempt trace facts, zero signer calls, and zero send calls.
- [x] Add EVM `4663` tests proving confirmation happens before token approval, pre-transactions, nonce/signing, and broadcast. Assert all mutation call counters remain zero.
- [x] Add missing-exact-price tests proving existing safety gates still execute and a display quote alone cannot reject.
- [x] Run `python3 -m pytest tests/test_memecoin_entry_executor.py tests/test_memecoin_evm_entry_executor.py tests/test_memecoin_entry_trace.py -q`; expect post-peak tests to fail.
- [x] Read the frozen pending classification, use existing normalized `quote_fields(quote)['quote_price']`, append display/exact facts to the attempt trace, and release before any irreversible executor step only on confirmed drawdown.
- [x] Rerun focused executor, retry, ambiguity, and direct-swap tests.
- [x] Commit `feat(memecoin): reject exact confirmed post peak entries`.

### Task 7: Frozen rails in nomination and executable confirmation

**Files:**
- Modify: `sniper/services/memecoin_funded_policy.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Modify: `tests/test_memecoin_real_rails_v2.py`
- Modify: `tests/test_memecoin_real_exit_override.py`
- Modify: `tests/test_memecoin_micro_policy.py`

**Interfaces:**

```python
def frozen_policy_from_position(pos: dict) -> FundedExitPolicy | None: ...

def effective_funded_policy(
    pos: dict,
    *,
    override: dict | None,
) -> tuple[FundedExitPolicy | None, dict]:
    """Return immutable original plus evaluated overlay; never mutate original JSON."""
```

- [x] Add a restart/config-drift test: freeze a small contract, monkeypatch global TP/de-risk/trail/arm/stop/timecap settings, and assert both `alpha_b_real_exit_reason` and `executable_exit_allowed` still use the frozen values.
- [x] Add nano/micro/mid tests for frozen de-risk, trail, and scoped stop selection in nomination and confirmation.
- [x] Add override tests proving operator values change the effective decision while the stored JSON/hash stay byte-for-byte unchanged.
- [x] Add legacy tests proving positions with null contract retain current rollover cutoff, assigned micro policy, and chain-stop behavior.
- [x] Add malformed-contract tests that fail closed at startup/reconciliation rather than silently falling back for a post-cutover row.
- [x] Run `python3 -m pytest tests/test_memecoin_real_rails_v2.py tests/test_memecoin_real_exit_override.py tests/test_memecoin_micro_policy.py -q`; expect config-drift and contract parsing failures.
- [x] Route all contract-cohort rail reads through `frozen_policy_from_position` in nomination, de-risk, trail, stop, timecap, unquotable, and executable confirmation. Overlay `effective_exit_override` last.
- [x] Leave the exact legacy branches intact for null-contract positions.
- [x] Rerun the focused exit suite and the 4663 blind-preview audit cases.
- [x] Commit `fix(memecoin): honor frozen exit rails end to end`.

### Task 8: API and cockpit observability

**Files:**
- Modify: `sniper/routes/api_memecoin.py`
- Modify: `static/app.js`
- Modify: `tests/test_memecoin_real_status.py`
- Modify: `tests/test_memecoin_exit_override_api.py`
- Modify: `tests/test_memecoin_ui.py`
- Modify: `tests/test_memecoin_ui_honest_copy.py`

**API shape:**

```json
{
  "entry_contract": {
    "version": 1,
    "hash": "...",
    "classification": {},
    "sizing": {"base_clip_usd": 100, "risk_scale": 0.75, "approved_clip_usd": 75},
    "policy": {},
    "expectation": {},
    "invalidations": []
  },
  "effective_exit_policy": {},
  "policy_source": "funded_contract|legacy",
  "operator_override_active": false
}
```

- [x] Add API tests for a complete contract, explicit unknown fields, active override separated from frozen policy, malformed JSON rendered safely, and a pre-contract `legacy` row.
- [x] Add UI tests pinning `TYPE`, `STATE`, `WHY`, `EXPECT`, `INVALID`, `SIZE`, and `POLICY` text hooks plus `legacy policy` fallback and unknown-as-unknown rendering.
- [x] Add a status-copy test replacing the false `Solana cap / EVM unbounded` claim with a global slot/exposure description.
- [x] Run `python3 -m pytest tests/test_memecoin_real_status.py tests/test_memecoin_exit_override_api.py tests/test_memecoin_ui.py tests/test_memecoin_ui_honest_copy.py -q`; expect missing API fields/copy failures.
- [x] Parse contract JSON server-side without deriving it from current settings. Return original and effective policy separately. On parse error, return hash/version plus `parse_error` without changing runtime behavior.
- [x] Add compact badges and an expandable detail block to `_realCard`; escape every string and render missing numeric values as `unknown`/`—`, never zero.
- [x] Update accounting/status copy to state that slots and total exposure are cross-venue while balances/gas remain chain-local.
- [x] Run focused tests and `node --check static/app.js`.
- [x] Commit `feat(memecoin): show funded entry contracts`.

### Task 9: Doctrine, reconciliation, broad verification, and branch close

**Files:**
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `.env.example`
- Modify: `scripts/deploy_memecoin_runtime.sh`
- Modify: `tests/test_memecoin_runtime_deploy.py`
- Modify: `docs/superpowers/specs/2026-07-18-memecoin-funded-entry-contract-regime-design.md` only if implementation evidence requires a precise correction
- Modify: `docs/superpowers/plans/2026-07-18-memecoin-funded-entry-contract-regime.md` to check completed tasks

**Reconciliation contract:**

```python
def validate_funded_contract_reconciliation(conn, *, cutover_at: str) -> list[str]:
    """Return blocking errors for post-cutover pending/position/attempt hash mismatches."""
```

- [x] Add deploy tests proving release validation blocks post-cutover missing/mismatched contracts, ignores pre-cutover existing bags, validates settings before switch, and preserves the prior symlink on failure.
- [x] Run `python3 -m pytest tests/test_memecoin_runtime_deploy.py -q`; expect reconciliation tests to fail.
- [x] Add reconciliation to the pinned deploy gate before switching `.runtime/memecoin/current`. Keep it read-only and scoped by corrected-UTC cutover.
- [x] Document four doctrine invariants in both agent files: frozen funded contract, regime cannot veto, exact post-peak confirmation, and cross-venue cap with chain-local gas/bankroll.
- [x] Document deployed settings: `MEMECOIN_MAX_CONCURRENT=7`, `MEMECOIN_MAX_TOTAL_EXPOSURE_USD=700`, classifier version/thresholds, and cutover timestamp.
- [x] Rerun the deploy tests.
- [x] Run `python3 scripts/validate_schema_refs.py --warn-unknown-tables`.
- [x] Run the focused funded suite:

```bash
python3 -m pytest \
  tests/test_memecoin_entry_snapshot.py \
  tests/test_memecoin_observation_schema.py \
  tests/test_memecoin_funded_contract_storage.py \
  tests/test_memecoin_market_state.py \
  tests/test_memecoin_funded_policy.py \
  tests/test_memecoin_risk_context.py \
  tests/test_memecoin_evm_risk.py \
  tests/test_memecoin_fraction_sizing.py \
  tests/test_memecoin_entry_trace.py \
  tests/test_memecoin_entry_executor.py \
  tests/test_memecoin_evm_entry_executor.py \
  tests/test_memecoin_real_runner.py \
  tests/test_memecoin_real_rails_v2.py \
  tests/test_memecoin_real_exit_override.py \
  tests/test_memecoin_micro_policy.py \
  tests/test_memecoin_real_status.py \
  tests/test_memecoin_exit_override_api.py \
  tests/test_memecoin_ui.py \
  tests/test_memecoin_ui_honest_copy.py \
  tests/test_memecoin_runtime_deploy.py -q
```

- [x] Run `python3 -m pytest tests/test_memecoin*.py -q`; note that repository `pytest.ini` excludes integration tests by default.
- [x] Run executor/runtime tests that are not named `test_memecoin*.py` if `rg` shows funded imports.
- [x] Run `python3 -m compileall -q sniper scripts`, `node --check static/app.js`, and `git diff --check origin/main...HEAD`.
- [x] Review `git diff --stat origin/main...HEAD` and `git diff origin/main...HEAD` line by line for future leakage, network calls, nominal-price authority, config fallback on contract rows, EVM mutation before post-peak confirmation, double-counted reservations, and unrelated work.
- [x] Run the Knowledge Sync Gate in audit-only mode. Include the full 11-question Knowledge Delta Report in the PR body. Record that Supabase memory publication was not run because it needs a separate explicit invocation.
- [ ] Push the feature branch, open the PR, wait for required CI, and merge without force-push/rebase/squash/amend.

### Task 10: Pinned funded deployment and live verification

**Files:**
- Modify outside Git after merge: operator `.env` only for the approved funded settings and corrected-UTC cutover.
- Execute: `scripts/deploy_memecoin_runtime.sh`

- [ ] Fetch the merged `origin/main` SHA and verify the mutable main checkout has no unrelated required runtime change.
- [ ] Snapshot read-only live state: runner PID/start time, `.runtime/memecoin/current` target, open real positions, unresolved reservations, daily latch, balances, and current config. Confirm existing bags are before cutover.
- [ ] Back up the operator environment through the deploy script's existing backup path.
- [ ] Run the pinned deploy script with the exact merged SHA and `--activate-rollover`. Its coordinated transaction stops old code, rechecks zero reservations, captures `real_utc_now_iso()`, and atomically writes it as both `MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT` and the rollover anchor with `MEMECOIN_MAX_CONCURRENT=7`, `MEMECOIN_MAX_TOTAL_EXPOSURE_USD=700`, and the classifier defaults from Task 2.
- [ ] Do not hand-write the contract cutoff while old code is live and do not launch from the mutable checkout.
- [ ] Verify `.runtime/memecoin/current` resolves to the exact merged release, the funded runner process start time is after the switch, and startup logs show policy/settings/contract reconciliation success with no traceback.
- [ ] Probe at least one authenticated health/status endpoint and `/api/memecoin/real/positions`; verify existing rows report `legacy`, new-contract fields parse, global cap is `$700`, and no existing position was sold/reclassified on restart.
- [ ] Verify reservation and execution workers are alive, no stale reservation was created by deploy, and the pinned release can accept a new eligible real event without a paper-data gate.
- [ ] If deployment verification fails, restore the prior pinned symlink and environment backup, restart the prior runtime, and report the exact blocking invariant. Do not let prior code manage any already-created contract position without explicit operator direction.
- [ ] Record exact merge SHA, release target, process PID/start time, endpoint status, test totals, and remaining empirical risk in the handoff.
