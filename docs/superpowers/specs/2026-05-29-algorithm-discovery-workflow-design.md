# Algorithm Discovery Workflow — design

**Date:** 2026-05-29
**Status:** design (approved to proceed under standing autonomy directive)
**Surface version:** `algo_discovery_v1`
**Parent doctrine:** `AGENTS.md` analyzer pipeline — `wallet intelligence → strategy extraction → thesis formation → thesis-specific dataset → thesis-specific simulation → paper/live validation`

## 1. Purpose & scope

Every stage of the per-wallet "discover the algorithm a wallet runs, then prove it" pipeline already exists on `main` as a separate module/CLI, but **nothing chains them** and the final (paper-mirror) seam has real bugs. This project builds the missing **orchestrator** that runs the chain end-to-end on a wallet, applies a **pre-registered decision gate** to the backtest, and (operator-gated) hands a validated rule to the shadow-copy paper-mirror — plus the seam fixes needed to make the last stage actually work.

**In scope (the per-wallet spine):**
1. (optional) pre-screen a candidate wallet list → ranked shortlist
2. fetch wallet data → run wallet-protocol → `ProtocolReport`
3. build dossier (`DossierTables`)
4. extract `wallet_rule_v1` (concrete entry/sizing/timing/exit/market-filter parameters)
5. OOS backtest the rule
6. **decision gate** (new): pre-registered kill-criteria → `proceed_to_paper | reject | insufficient_data`
7. (operator-gated, `--enroll`) register a `shadow_copy_v1` strategy that paper-mirrors the wallet

**Optional aggregate branch (when ≥2 dossiers exist):** SPA cross-wallet thesis audit → thesis validation plan. This is a *complementary* analysis (finds theses shared across wallets); it is not on the per-wallet critical path and is wired as an opt-in step.

**Out of scope:** live/real-money execution (the shadow-copy plugin hard-asserts `execution_mode='paper'`); walk-forward optimization (named future improvement, §9); pre-resolution-exit backtesting (the harness supports `held` exit only — §9).

**Doctrine guard:** this is strategy-intelligence, not a copy bot. The orchestrator does not emit live triggers, sizing for real money, or expected PnL. "proceed_to_paper" means *eligible for paper validation*, nothing more.

## 2. Current state (verified 2026-05-29 by a 7-agent code map)

| # | Stage | Entry point (file:line) | State |
|---|---|---|---|
| 1 | Pre-screen | `dossier_prescreen.screen_wallet_row` / `scripts/screen_wallets_for_dossier.py` | ✅ reads frozen `user_analysis` (scoped exception) |
| 2 | Protocol run | `analyze_wallet.analyze_wallet` → `ProtocolReport` (types.py:268) | ✅ |
| 3 | Dossier build | `wallet_dossier.builder.build_dossier(WalletData json, ProtocolReport json)` → `DossierTables` | ✅ |
| 4 | SPA cross-wallet audit | `strategy_pattern_audit.dossier_adapter.compute_dossier_strategy_pattern_audit(≥2 dossiers)` | ✅ |
| 5 | Thesis validation plan | `validation_workflow.build_thesis_validation_workflow` | ✅ planner only |
| 6 | Rule extraction | `wallet_parameter_extractor.extract_wallet_rule(DossierTables)` → `wallet_rule_v1` | ✅ |
| 7 | OOS backtest | `backtest_harness.backtest_rule_on_wallet(rule, WalletData)` → `BacktestResult` | ✅ **held-exit only**, no kill-criteria |
| 8 | Shadow-copy paper-mirror | `strategies/shadow_copy_v1.py` + `scripts/poll_shadow_copy_targets.py` | ⚠️ 4 real bugs |

### Verified gaps & bugs (the work)
- **No orchestrator** — each seam is a manual CLI invocation.
- **Seam impedance:** the batch runner persists the report as markdown-in-DB; the dossier builder needs a `ProtocolReport` **JSON file** + a `WalletData` fixture **JSON file**. No shared handoff. No `wallet_layer5_decision → ProtocolReport` deserialization path.
- **No decision gate** — the harness emits raw metrics (`oos_simulated_pnl_usdc`, `oos_simulated_win_rate`, `oos_match_count`); pass/fail is the caller's job and nothing does it.
- **shadow_copy_v1 bugs (block the live seam):**
  - B1: `enter(self, conn, signal, strategy)` diverges from `base.py` ABC and the `strategy_runner.py` call site `enter(conn, strategy, signal, open_positions)` → args misread at runtime.
  - B2: `mark_pending_processed` is never called → the `status='processed'` dedup guard never fires → duplicate paper trades on re-buys.
  - B3: `poll_shadow_copy_targets._fetch_wallet_activity` calls `fetch_user_activity(wallet=…, since_ts=…)` — kwargs match no overload → `TypeError` when `--enable-live-api`.
  - B4: `max_hold` exit reads `opened_at_ts`/`opened_at` but `strategy_positions` rows store `opened_ts` → the hold-time exit never fires.
- **Data:** real promotions/interpretable grains need fixtures **with redemption data**; committed CI fixtures lack them. Live fetch may be geo-blocked on the Amsterdam VPS (per brain note) — local fetch is the expected path.

> Correction logged: one mapping agent claimed the backtest harness "doesn't exist / deferred task #31" — that read a stale 2026-05-27 handoff. The harness was merged after (commit a119546); stage-7 mapping read the real `backtest_rule_on_wallet`. Trust code over docs.

## 3. Architecture — orchestrator (Approach A: artifact-passing, resumable)

New package `sniper/analysis/algo_discovery/` + CLI `scripts/run_algo_discovery.py`.

The orchestrator is a **thin sequential driver**. Each stage is a small adapter function `(prev_artifact, run_ctx) -> artifact` that wraps an existing entry point and writes its output to a per-run directory:

```
runs/algo_discovery/<wallet>_<run_ts>/
  00_prescreen.json        (optional)
  01_wallet_data.json      (WalletData fixture — also the backtest input)
  02_protocol_report.json  (asdict(ProtocolReport))
  03_dossier/              (DossierTables JSON export dir)
  04_wallet_rule.json      (wallet_rule_v1)
  05_backtest.json         (BacktestResult, include_per_position=True)
  06_decision.json         (DecisionVerdict — NEW)
  07_enrollment.json       (shadow_copy strategy params; only if --enroll & proceed)
  run_manifest.json        (per-stage status + timings + input hashes)
```

**Resumability:** a stage is skipped if its artifact exists and its upstream input hash is unchanged (recorded in `run_manifest.json`); `--force` / `--from-stage N` override. This makes the expensive fetch/dossier reusable across iterations — matters because fetch is rate-limited and dossier build is heavy.

**Why Approach A** (vs B = full DAG/state-machine, C = single in-memory function): A keeps every seam artifact inspectable (essential for debugging a 7-stage chain and for the user to audit), is resumable without a heavy state engine, and maps to repo conventions (CLI + module, JSON artifacts like the dossier export). B's machinery is YAGNI for v1; C is undebuggable and re-runs expensive fetch every time. The `run_manifest.json` gives 80% of B's resumability at 10% of the cost.

**Live vs fixture:** `--from-fixture <path>` skips stage 1 fetch and uses a committed/staged `WalletData` JSON; default (no fixture) fetches live via `fetch.fetch_wallet_data`. Determinism for tests comes from fixtures; live is for real runs.

## 4. Component designs

### 4.1 Stage adapters (`pipeline.py`)
Each adapter is pure-ish: reads its input artifact (or in-memory object), calls the existing entry point with explicit kwargs (notably `walk_full_history=True` to match CLI behavior — the Python default is `False`), writes its output artifact, returns it. Adapters own the **impedance fixes**:
- `02→03`: serialize `ProtocolReport` via `dataclasses.asdict` to the JSON file `build_dossier` expects; pass the stage-01 `WalletData` fixture path as the other input.
- `03→04`: pass the in-memory `DossierTables` directly to `extract_wallet_rule` (no disk round-trip needed; the JSON export is for inspection).
- `04→05`: pass `wallet_rule_v1` + the stage-01 `WalletData` to `backtest_rule_on_wallet(include_per_position=True)`.

### 4.2 Decision gate (`decision_gate.py` — NEW)
Pure function `evaluate(backtest_result, rule, *, config) -> DecisionVerdict`. Pre-registered kill-criteria (thresholds in `web_settings.py`, not inline). Verdict ∈ `proceed_to_paper | reject | insufficient_data`, plus `reason_codes: list[str]` and the metrics it judged.

Criteria (grounded in OOS-validation literature + prediction-market domain):
1. **Disqualifying caveats** → `reject`/`insufficient_data`: `unsupported_exit_mode:*`, `rule_has_no_market_filter`, `no_oos_matches_for_rule`, `no_closed_positions`. `rule_size_missing_using_10usdc_default` → not fatal but recorded.
2. **Sample adequacy** (`n_decided = oos_simulated_wins + oos_simulated_losses`): `< MIN_OOS_DECIDED (20)` → `insufficient_data`; `< 100` → proceed-but-flag `low_confidence_sample`. (Web: 100+ reasonable, 200+ strong; <20 is noise.)
3. **Profitability:** `oos_simulated_pnl_usdc > 0`.
4. **Edge vs domain null (key refinement):** the no-edge null win-rate = **mean entry price** of the OOS matched positions (from `per_position_results[*].sim_entry_price`), NOT 0.5. Require the observed OOS win-rate to beat that null at a one-sided binomial **z ≥ 1.96**. This is why we pass `include_per_position=True`. Evaluating edge vs the price-implied baseline (not 0.5) avoids the longshot-looks-like-a-loser fallacy.
5. **Slippage robustness:** re-run the backtest at a stressed slippage (`SLIPPAGE_STRESS_PCT`, default 0.015); require OOS PnL still `> 0`. A sign flip → `reject` (`edge_slippage_fragile`).
6. **Sim-vs-wallet fidelity (soft):** if `oos_simulated_pnl_usdc` wildly exceeds `oos_actual_pnl_usdc_on_same_positions` (e.g. >3×), record `sim_exceeds_wallet_fidelity_warn` — the rule may be capturing something the wallet didn't (look-ahead / labeling leak). Soft flag, not a hard kill in v1.

`proceed_to_paper` emits the shadow-copy enrollment params (target_wallet, market_filter from the rule, sizing from rule sizing clamped to paper caps).

### 4.3 shadow_copy_v1 seam fixes
- B1: align `enter()` signature to the ABC `enter(self, conn, strategy, signal, open_positions)`; fix internal references. Regression test asserting the runner call site works.
- B2: call `mark_pending_processed(conn, pending_id, strategy_id)` after a successful enter (in the plugin's post-enter hook or the runner integration point); test that a second identical pending row is deduped.
- B3: fix `_fetch_wallet_activity` to call `fetch_user_activity` with the correct positional `session` + supported params; test with a stubbed client.
- B4: read `opened_ts` (the actual `strategy_positions` column) in the `max_hold` exit; test the hold-time exit fires.

These are prerequisites for stage 7 (`--enroll`); they are independently correct fixes regardless of the orchestrator.

### 4.4 Data / fixtures
- Locate or build a `WalletData` fixture **with redemptions** for a held-exit wallet (SwissMiss `0xdbade4…` underdog-accumulator or the `$0.99` scraper — both hold to resolution → backtestable). Without redemptions, grains are non-interpretable → empty rule (documented edge case).
- Commit a trimmed e2e fixture under `tests/calibration/algo-discovery-fixtures/` for the deterministic e2e test.

## 5. Data flow

```
[wallet | wallet-list]
  └─(opt prescreen)→ shortlist
        └→ fetch → WalletData ──┬─────────────────────────────┐
              └→ analyze_wallet → ProtocolReport               │
                    └→ build_dossier(WalletData, Report) → DossierTables
                          ├→ extract_wallet_rule → wallet_rule_v1
                          │       └→ backtest_rule_on_wallet(rule, WalletData) → BacktestResult
                          │             └→ decision_gate → verdict
                          │                   └─(proceed & --enroll)→ shadow_copy enrollment
                          └─(opt, ≥2 dossiers)→ SPA audit → validation plan
```

## 6. Error handling
- Each stage wrapped; on exception the adapter writes `run_manifest.json` stage status=`failed` with the traceback summary and the orchestrator stops with a non-zero exit and a clear "failed at stage N" message. No silent partial success.
- Stage 2's known silent-failure modes (Supabase marker lines, SQLite-busy) are surfaced: the orchestrator parses the `[PERSIST_*]` markers and treats `failed` markers as stage failures rather than trusting exit code.
- A `reject`/`insufficient_data` verdict is a **successful run** (exit 0) — it's a valid analytical outcome, not an error.

## 7. Edge cases (per piece)

**Orchestrator:** wallet address normalized lowercase; fixture-wallet ≠ requested-wallet → hard error (don't silently analyze the wrong wallet); `--enroll` without `proceed_to_paper` → no-op + log; resume with changed upstream artifact → re-runs downstream; run dir collision → timestamped, never overwrites.
**Fetch/protocol:** gate `reject_*` → no dossier attempted (rule extraction needs interpretable grains); activity cap (3000) hit → flagged, flipper reconstruction degraded.
**Dossier:** no redemptions → all `unresolved` exit_mode → non-interpretable grains → empty rule → orchestrator records `no_interpretable_grain` and stops with `insufficient_data` (not a crash).
**Rule extractor:** `rejection_reason='no_interpretable_grain'` → decision_gate short-circuits to `insufficient_data`; grain with `n_episodes=0` selected → guard with a `MIN_EPISODES` floor in the orchestrator before backtest.
**Backtest:** `exit_rule.mode != 'held'` → `unsupported_exit_mode` caveat → decision_gate `reject` with `exit_mode_unsupported` (NOT a crash; this is the expected outcome for flipper wallets in v1); `oos_match_count==0` → `insufficient_data`.
**Decision gate:** `per_position_results` empty (include flag off or no matches) → cannot compute null-WR → `insufficient_data`; all OOS positions unresolved → `n_decided==0` → `insufficient_data`.
**Shadow-copy:** empty `target_wallets` → no-op; `strategy_id=0` → dedup disabled (guard); `data_unavailable` market fields → filter rejects (documented).

## 8. Testing strategy
- **Unit, per piece:** decision_gate (each criterion + the null-WR math + slippage sweep) with synthetic `BacktestResult` dicts; each shadow_copy bug-fix gets a regression test that fails on the current code; stage adapters with stubbed entry points.
- **E2E (deterministic):** one held-exit wallet fixture → full chain → assert a populated `wallet_rule_v1`, a `BacktestResult` with `oos_match_count>0`, and a `DecisionVerdict` with coherent reason_codes. Runs offline from the committed fixture.
- **Negative E2E:** a no-redemption fixture → asserts graceful `insufficient_data` (no crash).
- Full suite + `validate_schema_refs.py` + brain-index freshness before PR.

## 9. Named gaps / limitations / future
- **Held-exit only:** backtest can't replay pre-resolution selling; flipper/hybrid wallets (e.g. SmallFish) are out until a sell-aware replay exists. Orchestrator returns `reject:exit_mode_unsupported`, honestly.
- **Single-split OOS, not walk-forward:** v1 backtest is one chronological 75/25 split → "false confidence" risk per the literature. The shadow-copy **paper-forward is the true never-touched holdout**, which mitigates this; walk-forward is a named v2 improvement.
- **Geo-restriction:** live fetch may fail from a blocked region; fixtures + local runs are the supported path until the permitted-region migration.
- **SPA aggregate needs ≥2 dossiers** (min_supporting_wallets clamped to 2); single-wallet runs skip it.
- **No deserialization** of persisted reports → orchestrator keeps its own JSON artifacts rather than reading `wallet_layer5_decision`.

## 10. Approaches considered
- **A (chosen):** thin sequential orchestrator, on-disk artifact per stage, manifest-based resume.
- **B:** full DAG/state-machine — rejected as YAGNI for v1.
- **C:** single in-memory function — rejected (undebuggable, re-runs expensive fetch).
