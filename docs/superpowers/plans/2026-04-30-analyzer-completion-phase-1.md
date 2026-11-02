# Analyzer Completion Phase 1 Implementation Plan

> **For Codex workers:** implement this plan task-by-task. Track progress by checking boxes in this file. If work is parallelized, use exactly one Codex branch/worktree per workstream.

**Goal:** Close analyzer C-level gaps before any analyzer production run, legacy archive, performance pass, or deploy.

**Architecture:** Six Codex-owned workstreams. Each workstream lands TDD tests first, then implementation, then focused verification, then a merge-ready audit note. Core analysis stays deterministic and pure; side effects stay in runner/CLI layers. Live calls are allowed only for read-only verification where a fixture cannot prove the claim.

**Tech Stack:** Python 3, pytest, SQLite, existing wallet protocol modules under `sniper/analysis/wallet_protocol/`, existing runners under `scripts/`, docs under `docs/superpowers/`, calibration fixtures under `tests/calibration/`.

**Parent spec:** `docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md` §7-§8.

---

## Hard Constraints

- No analyzer production runs until Phase 1 is complete and Gate 2 passes.
- No `scripts/run_wallet_protocol_batch.py --from-queue` against production DB.
- No `scripts/run_wallet_protocol.py --persist` against production DB.
- No legacy pipeline resurrection. Do not unmask or restart `sniper-stream`, `sniper-analyzer`, `sniper-paper`, or `sniper-alerter`.
- No low-buy or discovery queue intake. Do not write `low_buy_trades`, `wallet_analysis_queue`, `discovery_events`, `candidate_users`, `user_analysis`, `position_signals`, `paper_sell_signals`, `flagged_users`, `score_history`, or `signal_history`.
- One branch/worktree per Codex workstream if work is parallelized.
- VPS is out of scope except read-only verification. Allowed: `ssh ... systemctl --user status`, `journalctl`, `sqlite3` read-only queries, read-only file checks. Not allowed: `systemctl stop/start/restart/enable/disable/mask/unmask`, deploy, pull, or service edits.
- Phase 1 ends with a Codex Gate 2 audit at `docs/audits/codex/2026-04-30-gate-2.md`.

## Phase 1 Entry Gate

Before any workstream starts:

```bash
git status --short
git rev-parse --short HEAD
test -f docs/audits/codex/2026-04-30-gate-1.5b.md
rg -n "Verdict: \\*\\*pass\\*\\*" docs/audits/codex/2026-04-30-gate-1.5b.md
python3 -m pytest tests/test_legacy_guards_helper.py tests/test_legacy_entrypoint_guards.py tests/test_legacy_write_path_guards.py -q
```

Acceptance:

- Gate 1.5b exists and records `pass`.
- Legacy guard tests pass.
- If the tree is dirty, record which files are unrelated before making a branch.

## Codex Branches And Merge Order

Recommended branch/worktree names:

| Workstream | Branch | Worktree | Merge order |
|---|---|---|---|
| W1 H2/M2/M5/M3b statistical gap closures | `codex/phase1-stat-gaps` | `/home/bigpapi/Desktop/Sniper-phase1-stat-gaps` | 1 |
| W2 M4 cross-event correlation | `codex/phase1-m4-correlation` | `/home/bigpapi/Desktop/Sniper-phase1-m4-correlation` | 2 |
| W3 C1/C2/C3b/C3c wiring | `codex/phase1-wiring` | `/home/bigpapi/Desktop/Sniper-phase1-wiring` | 3 |
| W4 observability, failure taxonomy, resume idempotency | `codex/phase1-observability` | `/home/bigpapi/Desktop/Sniper-phase1-observability` | 4 |
| W5 replay determinism | `codex/phase1-replay` | `/home/bigpapi/Desktop/Sniper-phase1-replay` | 5 |
| W6 calibration set, labeling protocol, I3 SOP | `codex/phase1-calibration-i3` | `/home/bigpapi/Desktop/Sniper-phase1-calibration-i3` | 6 |

Create worktrees only when running in parallel:

```bash
git fetch origin
git worktree add -b codex/phase1-stat-gaps /home/bigpapi/Desktop/Sniper-phase1-stat-gaps origin/main
git worktree add -b codex/phase1-m4-correlation /home/bigpapi/Desktop/Sniper-phase1-m4-correlation origin/main
git worktree add -b codex/phase1-wiring /home/bigpapi/Desktop/Sniper-phase1-wiring origin/main
git worktree add -b codex/phase1-observability /home/bigpapi/Desktop/Sniper-phase1-observability origin/main
git worktree add -b codex/phase1-replay /home/bigpapi/Desktop/Sniper-phase1-replay origin/main
git worktree add -b codex/phase1-calibration-i3 /home/bigpapi/Desktop/Sniper-phase1-calibration-i3 origin/main
```

Merge rule:

```bash
git checkout main
git pull --ff-only origin main
git merge --no-ff codex/phase1-stat-gaps
python3 -m pytest <workstream verification command>
```

Repeat in merge order. W6 runs the full calibration harness after W1-W5 are merged.

## Shared-File Collision Rules

These rules are mandatory before implementation starts:

| File or surface | Owner | Collision rule |
|---|---|---|
| `sniper/analysis/wallet_protocol/statistics.py` | W1 | W2/W5 may read only. Any helper needed outside W1 goes into W1 first. |
| `sniper/analysis/wallet_protocol/held_analyst.py` | W1 | W3 may touch tag-grain call sites only after W1 merges. |
| `sniper/analysis/wallet_protocol/flipper.py` | W1 | W3 may touch FH4 peer-baseline plumbing only after W1 merges. |
| `sniper/analysis/wallet_protocol/independence.py` | W2 | W1 may read only. Cross-event clustering API lands in W2. |
| `sniper/analysis/wallet_protocol/types.py` | W3 | W1/W2 avoid dataclass shape changes. If W1/W2 need new result fields, add to `details` dict instead. |
| `sniper/analysis/wallet_protocol/fetch.py` | W3 | W4/W5 read only until W3 merges. |
| `db.py` | W3 | W4/W6 do not edit `db.py` in parallel. They add new ensure helpers in owned modules, then integrate after W3 merge. |
| `sniper/persistence/db_layer5.py` | W4 | W6 does not edit this file. W6 puts operator decisions in `sniper/persistence/db_operator_decisions.py`. |
| `scripts/run_wallet_protocol.py` | W4 | W5 rebases after W4 and adds replay flags/hooks only after JSONL/memo hooks exist. |
| `scripts/run_wallet_protocol_batch.py` | W4 | W5/W6 read only. |
| `tests/calibration/**` | W6 | W5 may create replay fixtures under `tests/replay/`, not `tests/calibration/`. |
| `docs/superpowers/specs/wallet-protocol-bulletproofing.md` | W1 | W2 appends only the M4 closure section after W1 merges. W3-W6 link to closure notes but do not rewrite gap statuses. |
| `docs/runbooks/**` | W4/W5/W6 | W4 owns run-log schema. W5 owns replay runbook. W6 owns operator review SOP and calibration labeling protocol. |

If two workstreams must edit the same file, stop that workstream at a failing test and rebase after the owner merges. Do not solve merge conflicts by deleting another workstream's tests.

## W1: H2/M2/M5/M3b Statistical Gap Closures

**Files:**

- Modify: `.claude/agents/feature-research-analyst.md`
- Modify: `.claude/agents/wallet-dossier-writer.md`
- Modify: `sniper/analysis/wallet_protocol/statistics.py`
- Modify: `sniper/analysis/wallet_protocol/held_analyst.py`
- Modify: `sniper/analysis/wallet_protocol/flipper.py`
- Modify: `docs/superpowers/specs/wallet-protocol-bulletproofing.md`
- Create: `tests/test_agent_source_trace_contract.py`
- Create: `tests/stress/test_h1_heavy_tail.py`
- Create: `tests/stress/test_flipper_kfold_stress.py`
- Create: `scripts/verify_redeem_resolution_order.py`
- Create: `tests/test_verify_redeem_resolution_order.py`
- Create: `docs/audits/2026-04-30-h2-source-trace-verification.md`
- Create: `docs/audits/2026-04-30-m5-redeem-resolution-order.md`

### Task W1.1: H2 Agent Source-Trace Contract

- [ ] Write `tests/test_agent_source_trace_contract.py`.

Required assertions:

```python
from pathlib import Path

AGENT_FILES = [
    Path(".claude/agents/feature-research-analyst.md"),
    Path(".claude/agents/wallet-dossier-writer.md"),
]

def test_agents_require_source_trace_columns_for_cited_numbers():
    for path in AGENT_FILES:
        text = path.read_text()
        assert "source_trace" in text
        assert "field name" in text
        assert "row count" in text
        assert "filter" in text
        assert "Every cited number" in text
```

- [ ] Run the test and confirm it fails for any missing contract phrase.

```bash
python3 -m pytest tests/test_agent_source_trace_contract.py -q
```

- [ ] Update both agent prompt files with a mandatory `source_trace` table for every cited number. Required columns: `claim`, `field_name`, `row_count`, `filter`, `tool_or_query`, `as_of`.
- [ ] Re-run:

```bash
python3 -m pytest tests/test_agent_source_trace_contract.py -q
```

Acceptance:

- Test passes.
- `docs/audits/2026-04-30-h2-source-trace-verification.md` exists.
- The audit doc contains one generated sample output for each updated agent prompt.
- The audit doc records three cited numbers per sample, with reproduction command/query and matching observed value.
- W1 cannot close on prompt-contract tests alone.

### Task W1.2: M2 Heavy-Tail H1 Robust Statistic

- [ ] Write `tests/stress/test_h1_heavy_tail.py`.

Required fixture:

```python
def contaminated_sample(rng, mu: float, n: int = 200):
    normal = rng.normal(loc=mu, scale=1.0, size=n)
    tail = rng.standard_t(df=3, size=n) * 5.0 + mu
    use_tail = rng.random(n) < 0.05
    return np.where(use_tail, tail, normal)
```

Required assertions:

- `mu=0.5`, `n_trials=1000`, `n=200`, fixed seed: chosen H1 statistic recovers positive sign at `>= 0.95`.
- `mu=0.0`, same fixture: false positive rate `<= 0.05`.
- Result details include `tested_location_statistic` with one of `winsorized_mean` or `median_iqr_bootstrap`.

- [ ] Run and confirm failure against current implementation if the bar is not met:

```bash
python3 -m pytest tests/stress/test_h1_heavy_tail.py -q
```

- [ ] Measure both candidates in the test file: `clip_frac=0.10` winsorized mean and median+IQR bootstrap.
- [ ] Implement the lower-risk passing candidate in `statistics.py` and thread the choice through `held_analyst.run_h1_net_pnl`.
- [ ] Update H1 `details` to name the location statistic and clip/bootstrap settings.
- [ ] Re-run:

```bash
python3 -m pytest tests/stress/test_h1_heavy_tail.py tests/test_wallet_protocol_statistics.py tests/test_wallet_protocol_held_analyst.py -q
```

Acceptance:

- Stress test passes both power and false-alarm bars.
- H1 report details do not claim raw-mean testing when the statistic is a robust location statistic.

### Task W1.3: M3b Flipper K-Fold Time Split

- [ ] Write `tests/stress/test_flipper_kfold_stress.py`.

Required assertions:

- `run_flipper_branch` populates `TimeSplitResult.per_fold_statuses` for `FH1` and `FH2`.
- `n_folds_total >= 5` when `n_flips >= 100`.
- `holdout_status == "survived"` only when `decision_rule_majority_supported(per_fold_statuses) == "survived"` and `n_folds_powered >= MIN_POWERED_FOLDS`.
- Synthetic one-good-fold/four-underpowered-fold case returns `underpowered`, not `survived`.

- [ ] Run and confirm current `time_split_70_30` path fails:

```bash
python3 -m pytest tests/stress/test_flipper_kfold_stress.py -q
```

- [ ] Replace `time_split_70_30` use in `flipper.py` with `time_split_kfold` + `decision_rule_majority_supported`, mirroring the held branch.
- [ ] Remove unused `time_split_70_30` import from `flipper.py`.
- [ ] Re-run:

```bash
python3 -m pytest tests/stress/test_flipper_kfold_stress.py tests/test_wallet_protocol_flipper.py tests/stress/test_time_split_kfold_stress.py -q
```

Acceptance:

- FH1/FH2 use k-fold majority rule.
- No flipper branch can survive on fewer than `MIN_POWERED_FOLDS=3` powered folds.

### Task W1.4: M5 REDEEM Timestamp Semantics

- [ ] Write `scripts/verify_redeem_resolution_order.py`.

CLI contract:

```bash
python3 scripts/verify_redeem_resolution_order.py --wallet 0x... --db sniper_tracker.db --json
python3 scripts/verify_redeem_resolution_order.py --wallet-file tests/calibration/redeem_wallets.txt --db sniper_tracker.db --json
```

Output JSON fields:

- `wallet`
- `n_redeems`
- `n_matched_to_market_outcomes`
- `n_pre_resolution_redeems`
- `pre_resolution_examples`
- `status`: `pass` or `fail`

- [ ] Write `tests/test_verify_redeem_resolution_order.py` with SQLite fixture rows for post-resolution pass and pre-resolution fail.
- [ ] Run and confirm failure before the script exists:

```bash
python3 -m pytest tests/test_verify_redeem_resolution_order.py -q
```

- [ ] Implement the script using existing `subgraph_reader.fetch_redemptions` and read-only `market_outcomes.resolution_ts` lookup.
- [ ] Run:

```bash
python3 -m pytest tests/test_verify_redeem_resolution_order.py tests/test_wallet_protocol_subgraph_reader.py -q
```

- [ ] Perform read-only live verification for three wallets and save the result:

```bash
python3 scripts/verify_redeem_resolution_order.py --wallet 0xdbade4c82fb72780a0db9a38f821d8671aba9c95 --db sniper_tracker.db --json > /tmp/m5_swissmiss.json
python3 scripts/verify_redeem_resolution_order.py --wallet 0x1cc16713196d456f86fa9c7387dd326a7f73b8df --db sniper_tracker.db --json > /tmp/m5_eatpraylove.json
python3 scripts/verify_redeem_resolution_order.py --wallet 0x5bffcf561bcae83af680ad600cb99f1184d6ffbe --db sniper_tracker.db --json > /tmp/m5_yatsen.json
```

- [ ] Write `docs/audits/2026-04-30-m5-redeem-resolution-order.md` with exact counts.

Acceptance:

- All three live read-only runs have `status=pass`.
- If any run has `n_pre_resolution_redeems > 0`, W1 is blocked until `flip_reconstruction.py` is fixed and re-tested.

### Task W1.5: W1 Closure

Run:

```bash
python3 -m pytest tests/test_agent_source_trace_contract.py tests/stress/test_h1_heavy_tail.py tests/stress/test_flipper_kfold_stress.py tests/test_verify_redeem_resolution_order.py -q
python3 -m pytest tests/test_wallet_protocol_statistics.py tests/test_wallet_protocol_held_analyst.py tests/test_wallet_protocol_flipper.py -q
```

Acceptance:

- All W1 tests pass.
- `wallet-protocol-bulletproofing.md` marks H2, M2, M3b, and M5 closed with reproduction commands.

## W2: M4 Cross-Event Correlation

**Files:**

- Modify: `sniper/analysis/wallet_protocol/independence.py`
- Modify: `sniper/analysis/wallet_protocol/analyze_wallet.py`
- Modify: `docs/superpowers/specs/wallet-protocol-bulletproofing.md`
- Create: `tests/test_wallet_protocol_cross_event_correlation.py`

### Task W2.1: Graph-Cut Clustering API

- [ ] Write `tests/test_wallet_protocol_cross_event_correlation.py`.

Required cases:

- Same `eventSlug` clusters together.
- Same `nav_category` within 24h clusters together.
- Same `nav_category` outside 24h does not cluster.
- Different `nav_category` inside 24h does not cluster.
- Synthetic Iran-crisis wallet with Iran nuclear, Russia sanctions, Venezuela election, and unrelated sports events does not transitive-cluster all geopolitics into one component.
- Missing `eventSlug` and missing nav category become singleton clusters.

- [ ] Run and confirm failure:

```bash
python3 -m pytest tests/test_wallet_protocol_cross_event_correlation.py -q
```

- [ ] Add `cluster_by_event_or_news_window(positions, nav_lookup, *, window_seconds=86400)` to `independence.py`.
- [ ] Return the same shape as `ClusterReductionResult` so callers can reuse concentration logic.
- [ ] Keep `cluster_by_eventslug` unchanged for backwards compatibility.
- [ ] Re-run:

```bash
python3 -m pytest tests/test_wallet_protocol_cross_event_correlation.py tests/test_wallet_protocol_independence.py -q
```

Acceptance:

- Cross-event clustering handles the 24h+same-nav rule without global transitive over-clustering.

### Task W2.2: Wire M4 Into Held Independence Check

- [ ] Add a test asserting `analyze_wallet` uses cross-event clustering when `nav_lookup` exists.
- [ ] Implement the call in `analyze_wallet.py` or the held branch boundary without changing verdict semantics.
- [ ] Surface a data-quality flag when cross-event concentration exceeds the existing threshold.

Run:

```bash
python3 -m pytest tests/test_wallet_protocol_cross_event_correlation.py tests/test_wallet_protocol_orchestrator.py -q
```

Acceptance:

- Existing eventSlug-only behavior stays available.
- Report limitations no longer say cross-event correlation is completely uncaptured when nav lookup is present.
- `wallet-protocol-bulletproofing.md` marks M4 closed, not deferred.

## W3: C1/C2/C3b/C3c Wiring

**Files:**

- Modify: `sniper/analysis/wallet_protocol/types.py`
- Modify: `sniper/analysis/wallet_protocol/fetch.py`
- Modify: `sniper/analysis/wallet_protocol/analyze_wallet.py`
- Modify: `sniper/analysis/wallet_protocol/report.py`
- Modify: `sniper/analysis/wallet_protocol/persistence.py`
- Modify: `db.py`
- Modify: `scripts/run_wallet_protocol.py`
- Modify: `scripts/seed_tag_nav_category.py`
- Create/modify: `tests/test_db_peer_baseline.py`
- Create: `tests/test_wallet_protocol_nav_peer_baseline.py`
- Create: `tests/test_run_wallet_protocol_subgraph_persist_flags.py`
- Create: `docs/audits/2026-04-30-c1-c2-c3-wiring.md`

### Task W3.1: C1 Capital Ledger Acceptance Lock

- [ ] Extend existing C1 tests to assert `ProtocolReport.capital_ledger` is persisted into `wallet_layer5_decision.capital_ledger_summary_json`.
- [ ] Add a no-live fixture for accounting-tie pass/fail at ±2%.

Run:

```bash
python3 -m pytest tests/test_wallet_protocol_accounting_tie.py tests/test_wallet_protocol_capital_ledger_integration.py tests/test_run_wallet_protocol_persist.py -q
```

Acceptance:

- C1 data is visible in report and persistence.
- Live read-only sanity tie is recorded for SwissMiss plus three calibration wallets in `docs/audits/2026-04-30-c1-c2-c3-wiring.md`.

### Task W3.2: C2 Subgraph Fallback Persistence Flag

- [ ] Add `subgraph_fallback_used: bool = False` to `WalletData`.
- [ ] Set it in `fetch.py` when REST cap triggers and subgraph rows replace capped REST rows.
- [ ] Update `scripts/run_wallet_protocol.py` persistence helpers to pass `report` or `data` truth, not hard-coded `False`.
- [ ] Add `tests/test_run_wallet_protocol_subgraph_persist_flags.py`.

Run:

```bash
python3 -m pytest tests/test_wallet_protocol_fetch_subgraph_fallback.py tests/test_run_wallet_protocol_subgraph_persist_flags.py tests/test_run_wallet_protocol_persist.py -q
```

Acceptance:

- Capped REST fallback writes `subgraph_fallback_used=1` in `analyzer_runs`.
- Non-capped REST writes `subgraph_fallback_used=0`.
- One read-only live >20K wallet verification is recorded in the C1/C2/C3 audit doc.

### Task W3.3: C3b Nav Grain Peer Baseline

- [ ] Change `tests/test_db_peer_baseline.py::test_nav_grain_unsupported_returns_none` into nav-grain acceptance.
- [ ] Create `tests/test_wallet_protocol_nav_peer_baseline.py`.
- [ ] Implement `db.peer_baseline_for_grain(conn, "nav", nav_category, exclude_wallet=...)` by joining `wallet_tag_pnl.tag` to `tag_nav_category.tag`.
- [ ] Exclude `confidence IN ('skip', 'ambiguous')`.
- [ ] Keep leaf behavior unchanged.
- [ ] Run seeder against local DB if needed:

```bash
python3 scripts/seed_tag_nav_category.py --db sniper_tracker.db
python3 scripts/seed_tag_nav_category.py --db sniper_tracker.db --report-only
```

Run:

```bash
python3 -m pytest tests/test_db_peer_baseline.py tests/test_wallet_protocol_nav_peer_baseline.py tests/test_wallet_protocol_h5.py -q
```

Acceptance:

- At least one fixture wallet returns a non-`inference_not_run` nav-grain verdict.
- Live read-only smoke for one wallet shows at least one non-`inference_not_run` nav grain.

### Task W3.4: C3c Peer Pool Growth

- [ ] Add a dry-run/report mode test for the B1 backfill command used to grow `wallet_tag_pnl`.
- [ ] Identify the existing script that populates `wallet_tag_pnl` before editing. Use `rg -n "wallet_tag_pnl|compute_wallet_tag_pnl|build_wallet_tag" scripts sniper tests`.
- [ ] Extend only that script if it cannot process ≥150 wallets deterministically.
- [ ] Run backfill on local DB only:

```bash
python3 <wallet_tag_pnl_backfill_script> --db sniper_tracker.db --limit 150
sqlite3 sniper_tracker.db "SELECT tag, COUNT(*) FROM wallet_tag_pnl WHERE n_held_to_resolution >= 10 GROUP BY tag HAVING COUNT(*) >= 10 ORDER BY COUNT(*) DESC;"
```

Acceptance:

- `wallet_tag_pnl` has ≥150 distinct wallets in local verification.
- Tags with peer_count ≥10 grow to ≥10.
- Target tags include `foreign-policy`, `politics`, and `crypto` at peer_count ≥10.
- If any target tag remains below 10, W3 is blocked until the backfill source is expanded or the parent spec is revised by a separate Codex review.

## W4: Observability, Failure Taxonomy, Resume Idempotency

**Files:**

- Modify: `sniper/persistence/db_layer5.py`
- Modify: `scripts/run_wallet_protocol.py`
- Modify: `scripts/run_wallet_protocol_batch.py`
- Modify: `scripts/brain_health.py`
- Create: `sniper/analysis/wallet_protocol/run_events.py`
- Create: `sniper/analysis/wallet_protocol/batch_failures.py`
- Create: `sniper/analysis/wallet_protocol/memo_emitter.py`
- Create: `tests/test_wallet_protocol_run_events.py`
- Create: `tests/test_analyze_wallet_no_side_effects.py`
- Create: `tests/test_wallet_protocol_batch_failures.py`
- Create: `tests/test_run_wallet_protocol_batch_resume_idempotency.py`
- Create: `docs/runbooks/analyzer-run-log-schema.md`

### Task W4.1: Per-Run JSONL Log

- [ ] Write tests for `JsonlEmitter` that assert one JSON object per line with `run_id`, `wallet`, `event`, `ts`, and `payload`.
- [ ] Add `--run-log-dir runs` to `scripts/run_wallet_protocol.py`.
- [ ] Runner creates `runs/<wallet>/<run_id>.jsonl`.
- [ ] `analyze_wallet` may accept an observation hook, but it must not open files.
- [ ] Add `tests/test_analyze_wallet_no_side_effects.py` asserting no filesystem writes when hook is `None`.

Run:

```bash
python3 -m pytest tests/test_wallet_protocol_run_events.py tests/test_analyze_wallet_no_side_effects.py -q
```

Acceptance:

- JSONL schema documented in `docs/runbooks/analyzer-run-log-schema.md`.
- Core analyzer remains pure.

### Task W4.2: Opt-In Brain Memo Emission

- [ ] Implement `memo_emitter.py` with hard gates: `--persist-memo`, Supabase env present, `PYTEST_CURRENT_TEST` absent.
- [ ] Add CLI flag `--persist-memo`, default off.
- [ ] Add tests for skipped memo when flag absent, env absent, and pytest present.

Run:

```bash
python3 -m pytest tests/test_wallet_protocol_run_events.py -q
```

Acceptance:

- Missing memo env warns to stderr and exits 0.
- No test writes Supabase memory.

### Task W4.3: Failure Taxonomy

- [ ] Add `failure_category TEXT` to `wallet_analyzer_intake_queue` via `ensure_wallet_analyzer_intake_queue`.
- [ ] Implement `batch_failures.classify(error)` with allowed values: `timeout`, `data_cap_hit`, `api_error`, `protocol_bug`, `gate_skip`.
- [ ] Update batch failure paths to set `error_class`, `error_message`, and `failure_category`.
- [ ] Add brain-health query for failure-category breakdown.

Run:

```bash
python3 -m pytest tests/test_wallet_protocol_batch_failures.py tests/test_run_wallet_protocol_batch.py tests/test_brain_health.py -q
python3 scripts/validate_schema_refs.py
```

Acceptance:

- Fault-injection tests populate every failure category.
- Existing queue status transitions still pass.

### Task W4.4: Pause/Resume Idempotency

- [ ] Write replay test: create a batch with three wallets; mark one completed with an analyzer run; simulate pause mid-batch; run `--resume-batch`; assert completed wallet has unchanged analyzer-run count.
- [ ] Fix resume query to select only paused/unfinished rows.
- [ ] Make completion idempotent: `(batch_id, wallet)` cannot spawn duplicate analyzer runs on resume.

Run:

```bash
python3 -m pytest tests/test_run_wallet_protocol_batch_resume_idempotency.py tests/test_run_wallet_protocol_batch.py -q
```

Acceptance:

- Resume over a partial batch produces zero duplicate `analyzer_runs` for already-completed wallets.

## W5: Replay Determinism

**Files:**

- Modify: `scripts/run_wallet_protocol.py`
- Modify: `sniper/analysis/wallet_protocol/statistics.py` only after W1 merge and only for seed plumbing defects.
- Create: `scripts/record_fetch_snapshot.py`
- Create: `scripts/replay_wallet_run.py`
- Create: `sniper/analysis/wallet_protocol/replay.py`
- Create: `tests/test_replay_determinism.py`
- Create: `tests/replay/fixtures/swissmiss_fetch_snapshot.json`
- Create: `docs/runbooks/analyzer-replay.md`

### Task W5.1: Fetch Snapshot Recorder

- [ ] Implement `scripts/record_fetch_snapshot.py`.

CLI:

```bash
python3 scripts/record_fetch_snapshot.py 0xdbade4c82fb72780a0db9a38f821d8671aba9c95 --out tests/replay/fixtures/swissmiss_fetch_snapshot.json --seed 1
```

Snapshot must include:

- wallet
- fetched_at
- closed_positions
- open_positions
- activity_rows
- activity_cap_hit
- fetch_quality_flags
- capital_ledger summary when requested

- [ ] Add a fixture-load test that reconstructs `WalletData`.

Run:

```bash
python3 -m pytest tests/test_replay_determinism.py -q
```

Acceptance:

- Fixture path can run without live APIs.

### Task W5.2: Fixture-Driven Single Wallet Run

- [ ] Add `--from-fixture <path>` and `--no-persist` to `scripts/run_wallet_protocol.py`.
- [ ] `--from-fixture` bypasses live fetch and creates `WalletData`.
- [ ] `--no-persist` must override `--persist` with exit code 2 if both are present.

Run:

```bash
python3 -m pytest tests/test_replay_determinism.py tests/test_run_wallet_protocol_persist.py -q
```

Acceptance:

- Fixture mode writes no DB rows unless explicitly covered by a temp DB test.

### Task W5.3: Replay Script

- [ ] Implement `scripts/replay_wallet_run.py <run_id> --db <db>`.
- [ ] It loads `analyzer_runs.args_json`.
- [ ] It replays from JSONL with `--from-log` when the original run had non-deterministic live fetch.
- [ ] It exits non-zero if verdict or details differ beyond `1e-9`.

Run:

```bash
python3 -m pytest tests/test_replay_determinism.py -q
```

Acceptance:

- SwissMiss twice with same fixture and seed yields identical verdict and details floats to `1e-9`.
- Replay runbook documents live-fetch non-determinism and log replay.

## W6: Calibration Set, Labeling Protocol, I3 SOP

**Files:**

- Modify: `db.py` after W3/W4 merge only.
- Create: `sniper/persistence/db_operator_decisions.py`
- Create: `docs/runbooks/calibration-labeling-protocol.md`
- Create: `docs/runbooks/operator-review-sop.md`
- Create: `tests/test_operator_decisions_schema.py`
- Create: `tests/calibration/labels.yaml`
- Create: `tests/calibration/test_calibration_set.py`
- Create: `scripts/calibration_report.py`
- Create: `docs/audits/2026-04-30-calibration-label-selection.md`

### Task W6.1: I3 Operator Decisions Schema

- [ ] Write `tests/test_operator_decisions_schema.py`.
- [ ] Implement `sniper/persistence/db_operator_decisions.py` with:

```sql
CREATE TABLE IF NOT EXISTS operator_decisions (
    wallet TEXT NOT NULL,
    analyzer_run_id TEXT NOT NULL,
    decided_at_ts INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('accept', 'reject', 'defer')),
    reasoning TEXT NOT NULL DEFAULT '',
    operator TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (wallet, analyzer_run_id)
);
```

- [ ] Wire the ensure helper into `db.init_db` after W3/W4 merge.

Run:

```bash
python3 -m pytest tests/test_operator_decisions_schema.py -q
python3 scripts/validate_schema_refs.py
```

Acceptance:

- Schema exists in fresh DB.
- Invalid decisions fail the CHECK constraint.

### Task W6.2: I3 Operator Review SOP

- [ ] Write `docs/runbooks/operator-review-sop.md`.
- [ ] Include exact `operator_review` trigger conditions, diagnostic command set, checklist, and `INSERT INTO operator_decisions` workflow.
- [ ] Add one synthetic case with expected `accept` and one with expected `reject`.

Acceptance:

- A reviewer can decide without changing `wallet_layer5_decision`.
- SOP explicitly says operator decisions are audit overlays, not analyzer-owned verdict mutations.

### Task W6.3: Calibration Labeling Protocol

- [ ] Write `docs/runbooks/calibration-labeling-protocol.md` before assigning labels.
- [ ] Create `tests/calibration/labels.yaml` with 10 wallets:
  - High evidence exact verdict: SwissMiss `0xdbade4c82fb72780a0db9a38f821d8671aba9c95`.
  - High evidence exact verdict: eatpraylove `0x1cc16713196d456f86fa9c7387dd326a7f73b8df`.
  - High evidence exact verdict: YatSen `0x5bffcf561bcae83af680ad600cb99f1184d6ffbe`.
  - Two more high-evidence wallets selected from `knowledge/wallets/` with substantive dossiers.
  - Five medium-evidence wallets selected from local `user_analysis` snapshot across archetypes: insider, speculator_bot, arb_fund, low_signal, high_winrate_review.
- [ ] Record selection rationale and evidence source for each wallet in `docs/audits/2026-04-30-calibration-label-selection.md`.

Required label fields:

```yaml
- wallet: "0x..."
  label_type: "exact"
  expected: "edge_detected"
  accepted_band: []
  evidence:
    - source: "knowledge/wallets/..."
      claim: "..."
  rationale: "..."
```

Acceptance:

- No label uses protocol output as evidence.
- Medium-evidence labels use `accepted_band`, not a fake exact verdict.

### Task W6.4: Calibration Harness

- [ ] Implement `tests/calibration/test_calibration_set.py`.
- [ ] Implement `scripts/calibration_report.py`.
- [ ] Fixture mode:

```bash
python3 scripts/run_wallet_protocol.py <wallet> --from-fixture tests/calibration/fixtures/<wallet>_2026-04-30.json --seed 1 --n-iter 2000 --json --no-persist
```

- [ ] If fixture recording is not complete for a wallet, the test skips that wallet with reason `fixture_missing`, and Phase 1 cannot close until skips are zero.

Run:

```bash
python3 -m pytest tests/calibration/test_calibration_set.py -q
python3 scripts/calibration_report.py --labels tests/calibration/labels.yaml --out /tmp/calibration_report.md
```

Acceptance:

- 10/10 fixtures present.
- Agreement ≥9/10.
- Report shows wallet, expected label, protocol output, match status, and evidence note.

## Final Phase 1 Verification

After all workstreams merge in order:

```bash
python3 -m pytest tests/test_agent_source_trace_contract.py tests/stress/test_h1_heavy_tail.py tests/stress/test_flipper_kfold_stress.py tests/test_verify_redeem_resolution_order.py -q
python3 -m pytest tests/test_wallet_protocol_cross_event_correlation.py tests/test_wallet_protocol_independence.py -q
python3 -m pytest tests/test_wallet_protocol_accounting_tie.py tests/test_wallet_protocol_capital_ledger_integration.py tests/test_wallet_protocol_fetch_subgraph_fallback.py tests/test_db_peer_baseline.py tests/test_wallet_protocol_nav_peer_baseline.py -q
python3 -m pytest tests/test_wallet_protocol_run_events.py tests/test_analyze_wallet_no_side_effects.py tests/test_wallet_protocol_batch_failures.py tests/test_run_wallet_protocol_batch_resume_idempotency.py -q
python3 -m pytest tests/test_replay_determinism.py -q
python3 -m pytest tests/test_operator_decisions_schema.py tests/calibration/test_calibration_set.py -q
python3 scripts/validate_schema_refs.py
python3 scripts/brain_index.py generate
```

Optional but recommended before Gate 2:

```bash
python3 -m pytest tests/ -q --tb=line
```

Final acceptance:

- H2, M2, M3b, M4, M5 closed in `wallet-protocol-bulletproofing.md`.
- C1, C2, C3b, C3c accepted in `docs/audits/2026-04-30-c1-c2-c3-wiring.md`.
- Per-run JSONL, opt-in memo, failure taxonomy, and resume idempotency tested.
- Replay determinism tested from fixture/log.
- Calibration set frozen with 10 fixtures and ≥9/10 agreement.
- I3 SOP and `operator_decisions` schema landed.
- No production analyzer run or legacy queue intake occurred during Phase 1.
- Gate 2 audit written at `docs/audits/codex/2026-04-30-gate-2.md` with verdict `pass` before Phase 2 starts.
