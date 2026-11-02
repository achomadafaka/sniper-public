---
memory_name: spec/analyzer-completion-and-archive-2026-04-30
memory_type: knowledge
memory_description: End-to-end spec for completing wallet-analyzer protocol bulletproofing to C-level (correctness + observability + replay determinism), archiving the legacy stream/analyzer/discovery pipeline, performance-checking without compromising correctness, and deploying to VPS. Defines a 5-phase sequence with explicit unit-level shutdown matrix, DB write-surface audit, calibration labeling protocol, and 4 Codex review gates. Folder reorganization and UI/consumer integration are deferred.
memory_tags: [spec, analyzer, archive, bulletproofing, deployment, codex-review, phase3]
---

# Analyzer Completion + Legacy Archive + Deploy — Design

**Date:** 2026-04-30
**Author:** main-loop (Claude) with operator scope decisions
**Reviewer (gate 1):** Codex — initial verdict BLOCK on commit `c593501`; this revision addresses all 16 issues raised in `docs/audits/codex/2026-04-30-gate-1.md`.

**State assumed:**

| Layer | Commit | Notes |
|---|---|---|
| Local `main` (spec authoring) | `c593501` (this revision will create a new commit) | Up to date with `origin/main` apart from spec edits and untracked `.codex` directory. |
| `origin/main` | `48a29a3` | Per-wallet timeout merge. Will advance with this spec revision. |
| VPS HEAD | `48a29a3` | Pre-Phase-0 — no code changes pushed since. |
| VPS `.env` | has `LEGACY_STREAM_FROZEN=1` | Flag is set but **does not stop the legacy services from running** — guards only affect intake-queue writes inside helper functions, not the `sniper-stream`/`sniper-analyzer` daemons. See live-state note below. |

**Live VPS process state (verified 2026-04-30 SSH read-only):**
- `sniper-stream.service` — **ACTIVE running** (PID 2847968 → `stream_low_buys.py`)
- `sniper-analyzer.service` — **ACTIVE running** (PID 2786883 → `analyze_users.py`)
- `sniper-paper.service` — **ACTIVE running** (PID 2786888 → `paper_trader.py --interval 30`)
- `sniper-alerter.service` — **ACTIVE running** (PID 2786887 → `alerter.py`)
- `sniper-web.service` — ACTIVE running
- `sniper-bot.service` — inactive
- `sniper-sim-collector.service` / `sniper-sim-meta.service` — ACTIVE running (sim platform, separate from legacy)
- `sniper-tennis-inverse-paper@44.service` — ACTIVE running (strategy plugin)

**Implication:** the prior handoff (`docs/handoffs/2026-04-29-phase3-freeze-handoff.md`) describes a frozen state that no longer holds. The legacy pipeline is *currently writing* to `low_buy_trades`, `candidate_users`, `discovery_events`, `position_signals`, `paper_sell_signals`, `user_analysis`, `flagged_users`, `score_history`, `signal_history`. Phase 0 must therefore (a) verify live state at the start, (b) stop and confirm-quiet processes before any masking, (c) re-set `LEGACY_PIPELINE_DISABLED=1` after stopping.

**Other context:**
- New analyzer (`scripts/run_wallet_protocol*.py` + `sniper/analysis/wallet_protocol/*`) writes to `analyzer_runs`, `wallet_layer5_decision`, `wallet_analyzer_intake_queue`, `wallet_analysis_batch`, `wallet_analysis_batch_item`, `position_signal_decision`, `tag_market_stats`, `position_market_state`.
- Bulletproofing spec (`docs/superpowers/specs/wallet-protocol-bulletproofing.md`) shows 12 of 17 gaps closed; 5 OPEN + 6 wiring follow-ups remain.
- Legacy-remnant audit (`docs/audits/2026-04-29-legacy-remnant-audit.md`) classifies 4 retire / 3 isolate / 1 repurpose / 4 still-required-temporarily.
- Repo `systemd/` contains 47 unit files (`*.service` + `*.timer`). Full inventory drives the §4.1 matrix.

---

## 1. Goal

Three locked outcomes:

1. **Analyzer is C-level complete** — closes the 5 open bulletproofing gaps, the 6 wiring follow-ups, adds per-run observability, replay determinism, and a frozen calibration set with a written labeling protocol. No false certainty (G1), no silent coverage gaps (G2), no agent fabrication (G3) — to the verification bars defined in the bulletproofing spec.
2. **Legacy pipeline is removed from the active codebase** — `stream_low_buys.py`, `analyze_users.py`, `db_discovery.py`, plus the legacy reader services that depend on them (`alerter.py`, `paper_trader.py`, `bot_detector.py`), their tests, and their systemd units, archived to `archive/legacy-pipeline-2026-04-30/`. DB tables remain (immutable historical data). Remaining active code paths that read legacy tables (web dashboard's `/api/wallet-leaderboard`, `daily_briefing`, `brain_health`'s A5 check, `sync.py`) must not import-crash; stale rendered output is acceptable.
3. **System redeployed on VPS** — new analyzer reachable via systemd, sniper-web back to serving the dashboard, brain-health / news / world / finance / knowledge / prediction maintenance keep working independently, legacy units permanently masked.

## 2. Out of scope (deferred, separate specs later)

- **D-scope: UI / consumer integration.** What's deferred:
  - **Web dashboard `/api/wallet-leaderboard`** — stays alive; reads `user_analysis` directly. After Phase 2 the writer is gone, so the leaderboard renders the last-pre-archive snapshot until a future D-scope spec ports it onto `wallet_layer5_decision`. Stale display is accepted for the duration of this spec.
  - **Future replacements for the archived alerter and paper-trader** — `alerter.py` and `paper_trader.py` are archived in Phase 2 (not deferred here), so their *legacy* read paths (`check_for_new_insiders`, `check_for_position_signals`, `position_signals` consumer) leave with them. A future D-scope spec will design new alerting / copy-trading services that consume `wallet_layer5_decision` and `position_signal_decision` directly. There is no "continues reading" interim — the legacy services are gone.
  - **Real-money trader (`real_trader.py`)** — kept at root; not exercised in this push; not enabled.
- **Folder reorganization.** Phase 3 root reorg (`docs/superpowers/specs/2026-04-27-root-utilities-reorg-design.md`) shipped the utilities layer; the remaining 40 root .py files (services, domain modules, one-shots) stay where they are. Revisit after this push when legacy is gone — we will know which domain modules are truly active.
- **Real-money trading.** No `sniper-real` enablement. No bankroll changes. No live execution.
- **Performance optimization that touches correctness.** Only safe wins are in scope (parallelism that produces bit-identical verdicts under the same seed). Anything that requires re-validating verdict semantics is a separate spec.

## 3. Phase architecture

```
Gate 1      → Codex pre-plan spec review (initial commit c593501 BLOCKED;
              this revision addresses all 16 issues)
Phase 0     → Shutdown matrix + stop-and-verify-quiet + import-graph audit
              + DB write-surface audit + smoke
Phase 0.5   → Entrypoint-level guards on legacy daemons + write-path guards
              on legacy DB writers (no broader archive yet)
Gate 1.5    → Codex audit of Phase 0/0.5 (lightweight, blocks Phase 1 on breakage)
Phase 1     → Analyzer C-level: open-gap closure, wiring follow-ups,
              observability (runner-layer side effects only), replay determinism,
              calibration set + labeling protocol
Gate 2      → Codex audit of Phase 1 closure artifacts (most important gate)
Phase 2     → Broader legacy archive (alerter / paper_trader / bot_detector
              folded in) + reader compatibility tests + sync.py stub
Phase 3     → Performance pass (sequential default; parallelism opt-in only)
Phase 4     → Deploy: manual oneshot, no auto-restart, 5-wallet VPS calibration
Gate 3      → Codex audit of live VPS state
```

Total elapsed: ~3 weeks. Phase 1 dominates (~10 days). All four Codex gates required for "done."

## 4. Phase 0 — Shutdown + Audit

### 4.1 VPS unit matrix

Verdicts: **retire** (legacy/dead — stop, disable, mask), **review** (state to verify, default-keep), **keep** (active and required), **not-installed** (in repo `systemd/` but not deployed to VPS, no action needed).

Inventory: 47 unit files in repo `systemd/` plus live VPS state (verified 2026-04-30 SSH).

| Unit | Live state | Verdict | Action |
|---|---|---|---|
| `sniper-stream.service` | active running | **retire** | stop → confirm process gone → disable → mask |
| `sniper-analyzer.service` | active running | **retire** | same |
| `sniper-paper.service` | active running | **retire** | reads legacy `position_signals`/`paper_sell_signals`; same sequence |
| `sniper-alerter.service` | active running | **retire** | reads legacy `user_analysis`/`flagged_users`; same sequence |
| `sniper-agent-insider-copy.{service,timer}` | disabled | **retire** | mask |
| `sniper-agent-insider-analyst.{service,timer}` | disabled | **retire** | mask |
| `sniper-hl-stream.service` | disabled | **retire** | retired HL strategy; mask |
| `sniper-stink-bid@.service` (templated) | disabled | **retire** | retired strategy; mask |
| `sniper-stink-bid-shadow.service` | disabled (verify) | **retire** | mask |
| `sniper-real.service` | disabled (verify) | **review** | real-money executor; not in current scope but keep installed for later — confirm not running and document |
| `sniper-strategy.service` | disabled (verify) | **review** | strategy_runner; depends on whether it reads legacy outputs |
| `sniper-tennis-inverse-paper@44.service` | active running | **review** | confirm strategy plugin does not write to legacy tables; keep if independent |
| `sniper-web.service` | active running | **keep** | dashboard (stale display OK) |
| `sniper-bot.service` | inactive | **review** | Telegram relay; confirm whether it should be running and document |
| `sniper-sim-collector.service` | active running | **keep** | sim platform |
| `sniper-sim-meta.service` | active running | **keep** | sim platform |
| `sniper-sim-compact.{service,timer}` | static / enabled | **keep** | sim platform |
| `sniper-brain-health.{service,timer}` | per VPS verify | **review** | maintenance — keep if installed |
| `sniper-handoff-snapshot.{service,timer}` | per VPS verify | **review** | maintenance |
| `sniper-knowledge-compile.{service,timer}` | per VPS verify | **review** | maintenance |
| `sniper-news-intel.{service,timer}` | per VPS verify | **review** | independent intel — keep if active |
| `sniper-finance-analyst.{service,timer}` | per VPS verify | **review** | intel — keep if active |
| `sniper-world-analyst.{service,timer}` | per VPS verify | **review** | intel — keep if active |
| `sniper-predict-cleanup.{service,timer}` | per VPS verify | **review** | maintenance |
| `sniper-db-prune.{service,timer}` | per VPS verify | **review** | maintenance |
| `sniper-sports-runner.service` | per VPS verify | **review** | sports feed |
| `sniper-sync-state.{service,timer}` | per VPS verify | **review** | wallet sync to followed_positions |
| `sniper-agent-adversarial-reviewer.{service,timer}` | per VPS verify | **review** | research/governance agent |
| `sniper-agent-strategy-reviewer.{service,timer}` | per VPS verify | **review** | research/governance agent |
| `sniper-agent-wallet-scout.{service,timer}` | per VPS verify | **review** | **wallet discovery — verify whether it depends on `wallet_analysis_queue`/`discovery_events`. If yes, retire. If no, keep.** |
| `sniper-dashboard.service` | per VPS verify | **review** | second dashboard surface; classify by whether it duplicates `sniper-web` |
| `sniper-event-trigger.{service,timer}` | per VPS verify | **review** | event trigger maintenance |
| `sniper-outcomes.service` | per VPS verify | **review** | market outcome poller; classify by writes |

**Step 1 of Phase 0** (mandatory before any masking):

```bash
# 1. Inventory live state — full snapshot, no filtering
ssh sniper@188.166.91.20 'systemctl --user list-units --all --no-pager | grep "^\s*sniper-"' \
    > /tmp/vps_live_units_2026-04-30.txt
ssh sniper@188.166.91.20 'systemctl --user list-unit-files --no-pager | grep "^sniper-"' \
    > /tmp/vps_unit_files_2026-04-30.txt

# 2. Persist matrix verdict-per-unit (commit to repo)
#    Path: docs/audits/2026-04-30-vps-unit-matrix.md
#    Reconcile every line of (1) against this spec's matrix.
#    Any unit not in this spec's matrix MUST be classified before proceeding.

# 3. For every "retire" verdict (stop+disable+mask):
ssh sniper@188.166.91.20 'systemctl --user stop sniper-stream sniper-analyzer sniper-paper sniper-alerter'
# Confirm processes actually exited (not just stop signal sent):
ssh sniper@188.166.91.20 'pgrep -af "stream_low_buys|analyze_users|paper_trader|alerter" | head'
# Expected: empty output. If anything still running, investigate before continuing.

ssh sniper@188.166.91.20 'systemctl --user disable \
    sniper-stream.service sniper-analyzer.service sniper-paper.service sniper-alerter.service \
    sniper-agent-insider-copy.service sniper-agent-insider-copy.timer \
    sniper-agent-insider-analyst.service sniper-agent-insider-analyst.timer \
    sniper-hl-stream.service sniper-stink-bid-shadow.service'

ssh sniper@188.166.91.20 'systemctl --user mask \
    sniper-stream.service sniper-analyzer.service sniper-paper.service sniper-alerter.service \
    sniper-agent-insider-copy.service sniper-agent-insider-copy.timer \
    sniper-agent-insider-analyst.service sniper-agent-insider-analyst.timer \
    sniper-hl-stream.service sniper-stink-bid-shadow.service sniper-stink-bid@.service'

# 4. Re-verify processes still gone (mask should not auto-restart anything; sanity check):
ssh sniper@188.166.91.20 'pgrep -af "stream_low_buys|analyze_users|paper_trader|alerter"'
# Still empty.

# 5. Persist verified state to STATUS.md + PAUSED_TIMERS.md.
```

Any unit in the live inventory not classified by this spec gets a row added (and a verdict assigned) before §4.2 starts.

### 4.2 Import graph audit

Verify the new analyzer is import-independent of legacy modules. Run:

```bash
python3 -c "
import ast, pathlib
NEW = ['scripts/run_wallet_protocol.py', 'scripts/run_wallet_protocol_batch.py']
NEW += [str(p) for p in pathlib.Path('sniper/analysis/wallet_protocol').rglob('*.py')]
LEGACY = {'analyze_users', 'stream_low_buys', 'db_discovery', 'bot_detector', 'classify_accounts'}
def imports(path):
    tree = ast.parse(open(path).read())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module.split('.')[0])
    return out
violations = [(p, imports(p) & LEGACY) for p in NEW if imports(p) & LEGACY]
print('OK' if not violations else violations)
"
```

Acceptance: prints `OK`. Any violation = hidden coupling, classify as a migration task before Phase 2.

### 4.3 DB write-surface audit

For every legacy, analyzer-owned, support, and external-feed table that the analyzer depends on directly or transitively, classify every writer.

**Tables to audit:**

| Table | Class | Notes |
|---|---|---|
| `low_buy_trades` | legacy | written by stream |
| `wallet_analysis_queue` | legacy | written by db_discovery |
| `discovery_events` | legacy | written by stream/db_discovery |
| `candidate_users` | legacy | written by stream via `record_low_buy_trade` |
| `user_analysis` | legacy | written by analyze_users |
| `position_signals` | legacy | written by stream + analyze_users |
| `paper_sell_signals` | legacy | written by stream |
| `flagged_users` | legacy | written by analyze_users |
| `score_history` | legacy | written by analyze_users |
| `signal_history` | legacy | written by analyze_users |
| `wallet_analyzer_intake_queue` | analyzer-owned | new analyzer's queue |
| `wallet_analysis_batch` | analyzer-owned | batch tracking |
| `wallet_analysis_batch_item` | analyzer-owned | per-wallet batch row |
| `analyzer_runs` | analyzer-owned | run state machine |
| `wallet_layer5_decision` | analyzer-owned | final verdicts |
| `position_signal_decision` | analyzer-owned | per-position decisions |
| `tag_market_stats` | analyzer-owned | tag-grain peer baseline source |
| `position_market_state` | analyzer-owned | held vs flipped position state |
| `wallet_tag_pnl` | support | peer-baseline source for tag-grain inference |
| `wallet_tag_fit` | support | tag-archetype fit scoring |
| `tag_nav_category` | support | nav-grain peer baseline (currently empty / stubbed) |
| `wallet_profiles` | support | username/pseudonym lookup |
| `wallet_directory` | support | wallet alias / curation |
| `followed_wallets` | support | curated copy-pipeline scope |
| `followed_wallet_activity` | support | replay-source for curated wallets |
| `market_metadata` | external-feed-derived | written by stream + market_outcome_poller |
| `market_outcomes` | external-feed-derived | settlement source |
| `market_tags` | external-feed-derived | tag→market lookup |

```bash
for tbl in low_buy_trades wallet_analysis_queue discovery_events candidate_users \
          user_analysis position_signals paper_sell_signals flagged_users \
          score_history signal_history \
          wallet_analyzer_intake_queue wallet_analysis_batch wallet_analysis_batch_item \
          analyzer_runs wallet_layer5_decision position_signal_decision \
          tag_market_stats position_market_state \
          wallet_tag_pnl wallet_tag_fit tag_nav_category \
          wallet_profiles wallet_directory followed_wallets followed_wallet_activity \
          market_metadata market_outcomes market_tags; do
    echo "=== $tbl ==="
    python3 scripts/brain_index.py deps --table "$tbl" 2>/dev/null
done
```

**Also audit external write paths** (out of brain_index scope):
- **Supabase brain memory** — `bot.supabase_helper.save_finding()` and `save_memory()` call sites. Required gating: `--persist-memo` flag plus live-env detection. Listed in audit with one row per call site.
- **Per-run JSONL log files** — `runs/<wallet>/<run_id>.jsonl`. Listed with one row per emitter.
- **Goldsky subgraph endpoints** — read-only HTTP, no write surface. Listed for completeness.

Output saved to `docs/audits/2026-04-30-db-write-surface-audit.md`. Each writer classified: `retired` (archive in Phase 2) / `frozen` (kept as historical reader, no writes) / `analyzer-owned` (active, new pipeline) / `support` (keep) / `external-feed-derived` (keep) / `unrelated` (out of scope).

**Acceptance gate:** zero writers from outside `sniper.analysis.wallet_protocol.*`, `scripts/run_wallet_protocol*.py`, or explicitly approved support modules to any analyzer-owned table. Contamination paths must be fixed before Phase 1 starts.

### 4.4 Smoke tests

After matrix updates and audits, before any code changes:

```bash
python3 -m pytest tests/ -q --tb=line | tail -20
python3 scripts/validate_schema_refs.py
python3 scripts/brain_index.py generate

# Import smoke (Phase 0 — archive has not happened yet; every current root
# service module must still be importable):
python3 -c "import alerter, paper_trader, web_server, dashboard, strategy_runner, sniper_mcp, sync"
# (Phase 2 changes this acceptance — see §9.2.)
```

**Baseline regression anchor — fixture-driven, not live-API:**

A live-API run of `scripts/run_wallet_protocol.py` is not reproducible (clock-dependent fetches, time-windowed Polymarket queries that include "open positions at t=now"). The Phase 0 baseline therefore captures:

1. **Recorded fetch artifact** — `scripts/record_fetch_snapshot.py 0xdbade4c82fb72780a0db9a38f821d8671aba9c95 --out tests/calibration/fixtures/swissmiss_2026-04-30.json` (script to be authored in Phase 1; if not yet present, defer baseline capture to Phase 1 step 1).
2. **Replay-from-fixture verdict** — `scripts/run_wallet_protocol.py 0xdbade4c82fb72780a0db9a38f821d8671aba9c95 --from-fixture tests/calibration/fixtures/swissmiss_2026-04-30.json --seed 1 --n-iter 2000 --json > tests/calibration/baselines/swissmiss_phase0.json`.

The baseline captures (verdict, primary_edge_type, n_closed, dw_roi_point, ew_roi_point, per-hypothesis verdict). It does NOT need to be bit-identical across the whole `details` payload — only the spec-relevant decision fields. Replay determinism (§7.4) provides a stricter test for full-payload identity.

If the recorded-fixture path is not yet operational at Phase 0, Phase 0 records a *live* baseline JSON, marks it as informational only (`tests/calibration/baselines/swissmiss_phase0_informational.json`), and Phase 1 step 1 replaces it with the fixture-driven version. Any Phase-1 code change that breaks the spec-relevant decision fields against the informational baseline triggers manual review.

### 4.5 Phase 0 deliverables

- Updated `STATUS.md` + `PAUSED_TIMERS.md` with verdict-per-unit matrix.
- `docs/audits/2026-04-30-db-write-surface-audit.md`.
- One of: `tests/calibration/baselines/swissmiss_phase0.json` (if fixture recorder is operational and the recorded fixture is committed) **or** `tests/calibration/baselines/swissmiss_phase0_informational.json` (if recorder is deferred — Phase 1 step 1 replaces it with the fixture-driven version). The Phase 0 deliverable check requires *one* of the two paths exists; Phase 1 step 1 promotes the informational variant if it was used.
- VPS state: every unit in "stop+mask" or "stop+disable+mask" verdict actually masked. Confirmed with `systemctl --user list-unit-files | grep masked`.
- Brain memo: `phase-0-shutdown-and-audit-2026-04-30` recording final verdicts.

## 5. Phase 0.5 — Neutralize Executable Legacy Entrypoints

Goal: kill any code path that, if invoked as a service, would repopulate legacy tables. Do this *before* the 10-day Phase 1 work so contamination cannot happen during the window. Broader file archive is deferred to Phase 2.

**Important:** the guard MUST be entrypoint-level, not module-level. Other active code legitimately imports these modules:

- `alerter.py:19` imports `bot_detector.is_bot`
- `sync.py:709` imports `db_discovery.enqueue_wallets_for_analysis`
- `analyze_users.py` imports both `db_discovery` and `bot_detector`
- multiple tests import `db_discovery`, `analyze_users`, `bot_detector`, `classify_accounts`, `stream_low_buys`

A blanket `import os; if os.getenv(...): raise` at module top would break every active reader. The guard must fire only when the file is run as a script *or* when a specific writer entrypoint is called.

### 5.1 Entrypoint guards (not module guards)

**Pattern A — `__main__` block guard** for files that run as services (`stream_low_buys.py`, `analyze_users.py`):

```python
def main() -> int:
    # existing main body
    ...

if __name__ == "__main__":
    import os
    if os.getenv("LEGACY_PIPELINE_DISABLED") == "1" or os.getenv("LEGACY_STREAM_FROZEN") == "1":
        import sys
        print(
            "Legacy pipeline entrypoint blocked by LEGACY_PIPELINE_DISABLED.\n"
            "See docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md.",
            file=sys.stderr,
        )
        sys.exit(2)
    raise SystemExit(main())
```

A systemd `ExecStart=python3 stream_low_buys.py` exits non-zero before any DB write. Imports from elsewhere (e.g., a test importing `from stream_low_buys import _parse_event`) still work.

**Pattern B — write-path guard** for `db_discovery.py` write functions:

```python
_DEAD_ENTRYPOINTS = (
    "init_discovery_schema",
    "enqueue_high_value_wallets",
    "claim_wallets_for_analysis",
    "complete_wallet_analysis",
    "fail_wallet_analysis",
    "release_expired_leases",
    "record_discovery_event",
    "update_wallet_activity_hourly",
    "score_pending_events",
    "migrate_low_buy_trades",
)

def _check_disabled(name: str) -> None:
    import os
    if os.getenv("LEGACY_PIPELINE_DISABLED") == "1":
        raise RuntimeError(
            f"db_discovery.{name} called while LEGACY_PIPELINE_DISABLED=1. "
            "Either unset the flag (not recommended) or migrate the caller to "
            "wallet_analyzer_intake_queue / analyzer_runs."
        )

# At the top of each guarded function:
def enqueue_high_value_wallets(...):
    _check_disabled("enqueue_high_value_wallets")
    ...
```

`sync.py:709` calls `db_discovery.enqueue_wallets_for_analysis`. The §4.3 audit must produce the canonical writer-vs-reader function list before Phase 0.5 begins editing `db_discovery.py`; the guarded function list in Pattern B is then locked from that audit (writing-plans phase produces the final list as a precondition for the Phase 0.5 edit commit). Read-only helpers are not guarded.

`bot_detector.py` and `classify_accounts.py` are NOT guarded — they're feeder libraries with no daemon entrypoint and active readers. They get classified for archive in Phase 2 only after §4.2/§4.3 prove no remaining active readers.

`LEGACY_STREAM_FROZEN=1` (existing env-flag) is preserved as a backwards-compat alias.

### 5.2 Test compatibility

`tests/conftest.py` does NOT set `LEGACY_PIPELINE_DISABLED=1` globally. Tests that import from these modules continue to work because the guard is entrypoint/write-path, not import-time.

Tests that *invoke* a guarded writer must:

(a) `monkeypatch.delenv("LEGACY_PIPELINE_DISABLED", raising=False)` in the fixture for tests that intentionally exercise the writer (e.g., regression tests for the writer's logic), or
(b) Be updated to assert the new guard fires (e.g., `with pytest.raises(RuntimeError): enqueue_high_value_wallets(...)` ).

Subset of suite that expects guarded behavior: enumerated in `tests/conftest.py` comment block once classification is done in Phase 0.5.

### 5.3 Systemd unit handling on VPS

Phase 0 already stopped + masked the legacy units. Phase 0.5 does NOT move repo unit files yet — that's Phase 2 (broader archive).

What Phase 0.5 does for VPS:
- Verify mask still in place (`systemctl --user list-unit-files | grep masked`).
- Add a `[Service] Environment=LEGACY_PIPELINE_DISABLED=1` override drop-in to any legacy unit not already masked, as belt-and-suspenders. Path: `~/.config/systemd/user/sniper-{stream,analyzer,paper,alerter}.service.d/disabled.conf`. Ensures even an accidental unmask does not re-enter the legacy code path.

### 5.4 Phase 0.5 deliverables

- Entrypoint guards (Pattern A) on `stream_low_buys.py`, `analyze_users.py` — one commit each.
- Write-path guards (Pattern B) on confirmed writers in `db_discovery.py` — one commit, with audit-driven function list.
- Test classifications: tests that invoke guarded writers updated to either suppress flag or assert guard fires; document subset in `tests/conftest.py` comment.
- VPS drop-in `disabled.conf` files installed.
- Full test suite green.
- Brain memo: `phase-0.5-legacy-entrypoint-neutralization-2026-04-30`.

## 6. Gate 1.5 — Codex audit of Phase 0/0.5

Lightweight gate, blocks Phase 1.

**Codex deliverables:**
- Confirm import graph audit reproduced (zero legacy imports from new analyzer).
- Confirm DB write-surface audit reproduced (zero non-package writers to new tables).
- Confirm `LEGACY_PIPELINE_DISABLED=1` guard reachable for all 3 modules.
- Spot-check: attempt to call any guarded function, expect `RuntimeError`.
- Confirm full pytest suite green under the guard.
- Verdict: `pass` (Phase 1 may start) or `block` (specific issue with reproduction command).

Output stored in `docs/audits/codex/2026-04-30-gate-1.5.md`.

## 7. Phase 1 — Analyzer C-Level Completion (~10 days)

This is the bulk. Subdivided into four parallel tracks with explicit acceptance bars. Tracks may be dispatched to subagents in parallel where independent.

### 7.1 Open-gap closure (5 gaps from bulletproofing spec)

| Gap | Class | Acceptance |
|---|---|---|
| **H2** — agent fabrication, numbers without trace | H | All `feature-research-analyst` and `wallet-dossier-writer` outputs include source-trace columns (field name + row count + filter). Verified by re-running tool calls on ≥3 numbers per agent's output. |
| **M2** — heavy-tail H1 inconclusive | M | Synthetic stress test (`tests/stress/test_h1_heavy_tail.py`): finite-mean contaminated distribution `0.95·N(μ, 1) + 0.05·t(df=3, loc=μ, scale=5)` with `μ=0.5`, fixed seed. The Student-t with df=3 has finite mean (and variance) so `μ` is the true location; under heavy-tail contamination the protocol's robust statistic should recover the sign of `μ` at ≥95% power across n=1000 trials at sample size n=200. False alarm rate at `μ=0` ≤ 5%. **Implementation choice between (a) clip_frac=0.10 vs (b) median + IQR-bootstrap dual-statistic** decided after measuring both against the same stress fixture. The chosen statistic must report it tested *location* (median/winsorized mean), not raw mean. |
| **M3b** — flipper-branch k-fold | M | `flipper.py:519` `time_split_70_30` replaced with k-fold + `decision_rule_majority_supported` + `MIN_POWERED_FOLDS=3`. Stress test analogous to M3's `test_time_split_kfold_stress.py` for FH1/FH2. |
| **M4** — eventSlug independence misses cross-event | M | **TIMEBOX 2 days, binary acceptance.** Either (a) implemented + stress-tested (graph-cut clustering: shared eventSlug OR (24h AND same nav category); transitive over-clustering verified absent on a synthetic Iran-crisis test wallet) **OR** (b) explicitly deferred as documented limitation in `wallet-protocol-bulletproofing.md` with no blocker status — does not block Phase 2. |
| **M5** — flip_rate semantics not stress-tested | M | One agent run pulls `/activity?type=REDEEM` for 3 wallets; verify all REDEEM timestamps post-date corresponding `market_outcomes.resolution_ts`. Brain memo records result. If any pre-resolution REDEEM found, flip detector is silently wrong → fix flip detector before closing. |
| **I3** — operator_review SOP | I | Written SOP in `docs/runbooks/operator-review-sop.md`. Synthetic case applied: produces unambiguous accept/reject. SOP defines: when verdict is `operator_review`, operator runs deeper diagnostic (full per-grain H5 + flip-vs-held breakdown + checklist), then records decision in a new audit table (not `wallet_layer5_decision`, which is analyzer-owned and append-only).<br/><br/>**Audit table schema (defined here, not deferred):** `operator_decisions(wallet TEXT NOT NULL, analyzer_run_id TEXT NOT NULL, decided_at_ts INTEGER NOT NULL, decision TEXT NOT NULL CHECK (decision IN ('accept', 'reject', 'defer')), reasoning TEXT NOT NULL DEFAULT '', operator TEXT NOT NULL DEFAULT 'unknown', PRIMARY KEY (wallet, analyzer_run_id))`. Migration added in Phase 1; `init_db` ensures the table; brain_index regenerated. SOP runbook references this table and documents `INSERT` workflow. |

### 7.2 Wiring follow-ups (6 deferrals from bulletproofing spec)

| Item | Acceptance |
|---|---|
| **C1 wiring** — capital ledger surfaced on `ProtocolReport` | `ProtocolReport` gets `capital_ledger: CapitalLedger | None` field. Sanity-tie: `current_equity ≈ deposits − withdrawals + open_mtm + redemption_payouts` within ±2% on SwissMiss + 3 calibration wallets. |
| **C2 wiring** — Subgraph fallback in `fetch.py` | When `data.activity_cap_hit == True`, `fetch.py` retries via `subgraph_reader.fetch_order_filled_events`. Output normalized into the activity-row dict shape. Live verify on a >20K-activity wallet. |
| **C3b** — nav grain peer baseline | `peer_baseline_for_grain` extended to support `grain='nav'`. `tag_nav_category` is seeded by the existing `scripts/seed_tag_nav_category.py` (verified present in repo). Phase 1 step: run that seeder; if its source data is incomplete for the grains the protocol needs, extend it additively (do not duplicate). Live verify: 1 wallet returns ≥1 non-`inference_not_run` nav-grain verdict. |
| **C3c** — peer pool growth | B1 backfill expanded from 50 → ≥150 wallets. Tags qualifying at peer_count ≥10 grow from current 5 to ≥10 (target list: foreign-policy, politics, crypto, plus existing 5). |
| **M3b** — flipper k-fold | (covered in §7.1) |
| **I3 SOP** — (covered in §7.1) | |

### 7.3 Observability

Goal: every analyzer run is auditable from a structured artifact, not from log scraping. **All side effects (file writes, brain memo writes) are emitted by the runner/CLI layer, not by `analyze_wallet`.** The core analyzer function stays pure: input → `ProtocolReport`. Tests can run it without writing anything outside the unit-of-test fixtures.

- **Per-run JSON log:** the runner (`scripts/run_wallet_protocol.py`, `scripts/run_wallet_protocol_batch.py`) is responsible for emitting `runs/<wallet>/<run_id>.jsonl`. Each line carries one event: fetch start, fetch end (row count + cap status), hypothesis run, decision emit. The runner threads a `JsonlEmitter` (or `None`) to `analyze_wallet`'s observation hook — the analyzer publishes events through the hook but does not open files. Hook is `None` in tests; `tests/test_analyze_wallet_no_side_effects.py` asserts no filesystem writes when hook is `None`. Schema in `docs/runbooks/analyzer-run-log-schema.md`.
- **Per-run brain memo (opt-in, live-env-gated):** runner flag `--persist-memo` (default OFF) writes a `analyzer_run/<wallet>/<run_id>` memory to Supabase. Pre-conditions: (a) `--persist-memo` flag, (b) `SUPABASE_URL` + `SUPABASE_KEY` present, (c) not running under `pytest` (`os.getenv("PYTEST_CURRENT_TEST")` absent). On any pre-condition failure: warn to stderr, skip silently, exit 0. Memo content: verdict, primary_edge_type, n_closed_positions, gates fired, commit SHA, verification command, run timing. **Audit:** §4.3 includes Supabase brain `memories` table as an external write target with this gating documented; failure mode is "skip with warning," never "abort run."
- **Batch-runner failure taxonomy:** new column `failure_category` on `wallet_analyzer_intake_queue` (added via Phase-1 migration; existing `error_class` and `error_message` retained). Allowed values: `timeout`, `data_cap_hit`, `api_error`, `protocol_bug`, `gate_skip`. The runner sets both: `error_class` = exception class name (e.g., `subprocess.TimeoutExpired`), `failure_category` = taxonomy bucket. Mapping in `sniper.analysis.wallet_protocol.batch_failures.classify(error)` — pure function, fully unit-tested. Failure-mode breakdown query added to brain_health (`SELECT failure_category, COUNT(*) FROM wallet_analyzer_intake_queue WHERE status='failed' GROUP BY failure_category`).
- **Pause/resume idempotency:** `scripts/run_wallet_protocol_batch.py --resume` over a partially-completed batch must produce zero duplicate `analyzer_runs` rows for already-completed wallets. Verified by replay test: run batch → kill mid-batch → resume → assert `COUNT(*) FROM analyzer_runs WHERE wallet IN (already_completed_set)` unchanged.

### 7.4 Replay determinism

Goal: `scripts/replay_wallet_run.py <run_id>` reproduces verdict bit-for-bit (allowing for tolerated non-determinism, which is documented).

- **Audit RNG sources:** grep for `random.`, `numpy.random.`, bootstrap calls. Every site either takes a seed argument or threads through a top-level `seed` from `analyze_wallet(..., seed=...)`. Default seed = hash of `wallet + analysis_version + as_of_ts`.
- **Replay script:** loads `analyzer_runs.args_json` for the given `run_id`, re-fetches data (or replays from cached `runs/<wallet>/<run_id>.jsonl` if `--from-log` flag), reruns `analyze_wallet`, asserts verdict matches.
- **Documented non-determinism:** clock-dependent fetches (Polymarket Data API time-windowed queries that include "open positions at t=now") — these MUST be replayable from the per-run JSON log, not from re-fetching. Replay falls back to log-replay when fetch is non-deterministic.
- **Test:** `tests/test_replay_determinism.py` runs SwissMiss twice with same seed, asserts identical verdict + identical `details` floats to 1e-9.

### 7.5 Calibration set + labeling protocol

**Labeling protocol (written first, before any labels are assigned):**

| Element | Rule |
|---|---|
| **Labeler** | Operator (atodorov20@gmail.com), single-source-of-truth for the initial set. Disagreements with subsequent labelers (e.g., Codex) trigger §Disagreement rule. |
| **Allowed evidence** | Polymarket Data API (`/activity`, `/closed-positions`, `/positions`, `/value`), Goldsky activity-subgraph (capital ledger), Goldsky orderbook-subgraph (orderfill events), known wallet reputation from public sources (Polysights, Twitter, etc.) cited by URL. NOT allowed: protocol's own output (would be circular). |
| **Label types** | (a) **Exact verdict** (`edge_detected` / `operator_review` / `no_edge` / `underpowered`) for high-evidence wallets. (b) **Verdict band** (e.g., "edge_detected OR operator_review", "NOT no_edge") for medium-evidence wallets. |
| **High-evidence wallets** (3 anchors with confirmed addresses + dossiers, +2 to be selected at calibration kickoff from existing 47 dossiers in `knowledge/wallets/`) | Confirmed: SwissMiss = `0xdbade4c82fb72780a0db9a38f821d8671aba9c95`; eatpraylove = `0x1cc16713196d456f86fa9c7387dd326a7f73b8df`; YatSen = `0x5bffcf561bcae83af680ad600cb99f1184d6ffbe`. **2 more selected in Phase 1 step 1** by querying `knowledge/wallets/` for dossiers with substantive verdict-relevant content; chosen addresses recorded in `tests/calibration/labels.yaml` with rationale. No alias placeholders are committed to the spec until addresses are resolved. |
| **Medium-evidence wallets** (5, band label) | Selected at Phase 1 step 1 by querying `user_analysis` to ensure coverage across archetypes (1+ each: insider, speculator_bot, arb_fund, low_signal, high_winrate_review). Recorded in `labels.yaml` with archetype + selection rationale. |
| **Disagreement rule** | If protocol output ≠ label, triage into: (a) protocol bug (file gap entry, fix in next iteration), (b) label wrong/incomplete (operator updates label with new evidence + brain memo justifying change), (c) genuine ambiguity (label downgraded from exact → band). Do NOT silently update the label to match protocol. |
| **Fixture freeze** | Once all 10 labels are assigned, the set is committed under `tests/calibration/labels.yaml` with the assigning commit SHA. Future changes require a brain memo `calibration-label-change/<wallet>` justifying the change + reviewer (Codex). |
| **Acceptance bar** | Protocol agrees with the label (exact match for high-evidence, in-band match for medium-evidence) for ≥85% of wallets (≥9 of 10). Below 85%, gap analysis required before Phase 2. |

**Calibration run mode (declared explicitly):**
- **Persistence:** `--from-fixture` for replayability (no live API), with `--no-persist` to skip writes to `analyzer_runs` / `wallet_layer5_decision`. Fixtures committed under `tests/calibration/fixtures/<addr>_<date>.json`. (If fixture-record path is not yet operational at calibration kickoff, calibration runs use live APIs but emit `--no-persist` to avoid contamination of the analyzer-owned tables.)
- **Seed:** `--seed 1` for all calibration runs (default deterministic seed; locked across the whole calibration suite).
- **Per-wallet timeout:** 1800s (matches batch-runner default).
- **Iterations:** `--n-iter 2000` (sufficient for stable bootstrap CIs at calibration scale).
- **Expected artifact path:** `tests/calibration/runs/<addr>_<seed>.json` (per-wallet protocol output, used by harness for diffing).

**Calibration test harness:**
- `tests/calibration/test_calibration_set.py` reruns all 10 wallets with the run-mode parameters above (fixture-driven, seeded, no-persist), asserts agreement.
- `scripts/calibration_report.py` produces a markdown table: wallet → label → protocol output → match? → details. Run after every Phase-1 commit that changes statistics.

### 7.6 Phase 1 deliverables

- 5 OPEN gap closures committed individually with closure brain memos.
- 6 wiring follow-ups committed individually.
- Observability artifacts: per-run JSON log, brain-memo emission, failure taxonomy, idempotent resume, all tested.
- Replay determinism: `replay_wallet_run.py` script + test.
- Calibration set: 10 labels frozen at SHA, harness green at ≥85% agreement.
- Stress test artifacts under `tests/stress/`.
- Updated `wallet-protocol-bulletproofing.md`: status updated for closed gaps, M4 reflects binary outcome (closed or documented limitation), `Section 4 Open Questions` answered.
- Brain memo: `phase-1-analyzer-c-level-complete-2026-04-30`.

## 8. Gate 2 — Codex audit of Phase 1

Most important gate. Phase 2 blocked until pass.

**Codex deliverables (in `docs/audits/codex/2026-04-30-gate-2.md`):**
- Per-gap reproduction: each closure's verification command rerun, output compared to memo claim.
- Calibration harness: rerun, confirm ≥85% agreement, no silent label drift.
- Observability artifacts: confirm log emission, memo emission, failure taxonomy fields populated under fault injection.
- Replay determinism: rerun replay test, confirm bit-identical verdict.
- Adversarial test: pick 1 calibration wallet, predict via reading code only (without running protocol), compare prediction to label and to protocol output. Report disagreements.
- Verdict: `pass` (Phase 2 starts) / `block` (specific issue) / `partial` (closures OK but observability/calibration weak).

## 9. Phase 2 — Broader Legacy Archive (~2 days)

### 9.1 Module list (verified after §4.2 import audit + §4.3 write-surface audit)

**Known active import edges (must be addressed before archive):**
- `alerter.py:19` → `bot_detector.is_bot`
- `sync.py:709` → `db_discovery.enqueue_wallets_for_analysis`
- `analyze_users.py` → `db_discovery`, `bot_detector` (both archived together — internal edges fine)

| Module | Move to | Compat shim? | Notes |
|---|---|---|---|
| `stream_low_buys.py` | `archive/legacy-pipeline-2026-04-30/` | Root shim raising `ModuleNotFoundError` with clear pointer-to-archive message. | Active import sites: only tests (covered §5.2). Daemon entrypoint guarded in Phase 0.5. |
| `analyze_users.py` | `archive/legacy-pipeline-2026-04-30/` | Root shim raising `ModuleNotFoundError`. | No active production readers import the module (readers go via `db.py` table queries). Tests classified per §5.2. |
| `db_discovery.py` | `archive/legacy-pipeline-2026-04-30/` | **No raising shim** — instead, root file becomes a slim compat module re-exporting only the read-only helpers `sync.py` legitimately needs (e.g., a future read-only equivalent of `enqueue_wallets_for_analysis` if it turns out to be a no-op when intake table is empty, OR `sync.py` migrated to call the new analyzer's queue). Decision made in Phase 2 step 1 based on §4.3 write/read classification. Until then, **archive blocked on `sync.py` migration**. |
| `bot_detector.py` | **conditional — `alerter.py:19` is an active reader.** Either (a) archive both `bot_detector` and `alerter`'s consumer call (the entire alerter is `retire`-classed in §4.1), making the import dead at archive time; or (b) extract `is_bot` into `sniper/analysis/wallet_protocol/heuristics.py` as a small, kept utility. **Choice is (a)** since `alerter` is retired in this push and stale alerter is acceptable per operator. Archive `bot_detector` together with archiving alerter's reader path. | n/a if (a) | |
| `classify_accounts.py` | **conditional** — same logic; if no remaining active readers after archive of legacy services, archive. Otherwise extract used helpers. | | |
| `tests/test_stream_low_buys*.py` | `archive/legacy-pipeline-2026-04-30/tests/` | (n/a) | |
| `tests/test_analyze_users*.py` | same | (n/a) | |
| `tests/test_db_discovery*.py` | same | (n/a) | |
| Systemd unit files (after Phase 0.5 mask + drop-in) | `archive/legacy-pipeline-2026-04-30/systemd/` | (n/a) | Repo unit files moved here in Phase 2 — VPS already masked. |
| `agent_pipeline.py`, `agent_calibration.py` | **assess via §4.3** | classified per writes / readers. If writes only to legacy tables → archive. If writes to non-legacy or contains kept utilities → defer to later phase. | |

### 9.2 Reader compatibility — hard requirement

Per operator: stale display is acceptable, runtime crashes are NOT.

For each reader of an archived module:

| Reader | Imports | Behavior after archive |
|---|---|---|
| `alerter.py:19` | `from bot_detector import is_bot` | **Archived together** in this push (alerter is `retire`-classed in §4.1). After archive, `alerter.py` lives in `archive/legacy-pipeline-2026-04-30/` so its imports of `bot_detector` are within-archive. |
| `alerter.py` | `from db import get_user_analysis_for_alert` | (n/a — alerter archived) |
| `sync.py:709` | `from db_discovery import enqueue_wallets_for_analysis` | **CHANGE**. Either (a) replace with a no-op stub call returning `0` (sync still functions, simply doesn't enqueue legacy), or (b) port the call to write into `wallet_analyzer_intake_queue`. Decision: **(a)** — porting is D-scope; the stub keeps `sync.py` runnable and import-clean. Stub raises a one-time `logger.warning("legacy enqueue suppressed; migrate to wallet_analyzer_intake_queue")`. |
| `paper_trader.py` | reads `position_signals` via `db.py` | (n/a — paper_trader archived) |
| `routes/api_core.py` | `wallet_followable_leaderboard_rows` from `db_leaderboards.py` | No legacy module import. **OK** — leaderboard renders stale data from `user_analysis` table (table preserved). |
| `daily_briefing.py` | reads `wallet_analysis_queue` via raw SQL | Replace with try/except SELECT, returns `{}` if table empty/missing. **CHANGE**. |
| `brain_health.py` | reads `wallet_analysis_queue` for orphan-wallet check | Replace with try/except, A5 check returns INFO instead of FAIL when table missing. **CHANGE**. |
| `mcp_tools/read_tools.py:41` | reads `candidate_users` | Try/except, returns empty list when table missing. **CHANGE**. |
| Tests | various | Per §5.2, tests targeting archived modules move to `archive/legacy-pipeline-2026-04-30/tests/` or are removed. Existing globs covered by archive: `tests/test_alerter_*.py` (3 files: commands, refactor, thesis), `tests/test_paper_trader_*.py` (2 files: sizing, world_boosts). Glob lookups for `tests/test_bot_detector*.py`, `tests/test_stream_low_buys*.py`, `tests/test_analyze_users*.py`, `tests/test_db_discovery*.py` returned no matches at spec-authoring time — Phase 2 step 1 reconfirms with `ls tests/` and adds any newly-introduced files to the move list. New / updated tests cover kept-module changes: `sync.py` stub, `daily_briefing` graceful-degrade, `brain_health` A5 fallback, `mcp_tools/read_tools.py` try/except. |

**Acceptance gate (full set, Phase 2 cannot close until all pass):**

```bash
# 1. Full suite green
python3 -m pytest tests/ -q

# 2a. Archived modules expected ABSENT (find_spec returns None — module removed from sys.path):
python3 -c "import importlib.util; assert importlib.util.find_spec('alerter') is None, 'alerter still importable'"
python3 -c "import importlib.util; assert importlib.util.find_spec('paper_trader') is None, 'paper_trader still importable'"
python3 -c "import importlib.util; assert importlib.util.find_spec('bot_detector') is None, 'bot_detector still importable'"
python3 -c "import importlib.util; assert importlib.util.find_spec('stream_low_buys') is None, 'stream_low_buys still importable'"
python3 -c "import importlib.util; assert importlib.util.find_spec('analyze_users') is None, 'analyze_users still importable'"
python3 -c "import importlib.util; assert importlib.util.find_spec('db_discovery') is None, 'db_discovery still importable'"

# 2b. Active root modules expected IMPORTABLE (no ModuleNotFoundError, no other ImportError):
python3 -c "import sync, web_server, dashboard, strategy_runner, sniper_mcp"

# 3. brain_health green or graceful INFO
python3 scripts/brain_health.py --json | python3 -c "import sys, json; d=json.load(sys.stdin); print('PASS' if d.get('overall') in ('PASS','INFO') else 'FAIL')"

# 4. daily_briefing runs without raising
python3 scripts/daily_briefing.py --dry-run
```

The `paper_trader` and `alerter` modules being archived means the legacy reader services (already stopped + disabled + masked in Phase 0) cannot be restarted from the active codebase. That is expected and aligns with operator decision to redesign these later.

### 9.3 Archive layout

```
archive/legacy-pipeline-2026-04-30/
├── README.md            # what + why + when + recovery procedure + commit-SHA-at-archive
├── stream_low_buys.py
├── analyze_users.py
├── db_discovery.py
├── tests/
│   ├── test_stream_low_buys.py
│   └── ...
└── systemd/
    ├── sniper-stream.service
    ├── sniper-analyzer.service
    └── ...
```

`archive/legacy-pipeline-2026-04-30/README.md` documents:
- Archived at commit SHA `<TBD>` on 2026-04-30
- Why archived (legacy P&L-first wallet scoring superseded by `sniper.analysis.wallet_protocol`)
- Tables previously written (with cross-reference to retained DB rows)
- Recovery procedure: `git mv` back, set `LEGACY_PIPELINE_DISABLED=0`, restart units
- Pointer to this spec

### 9.4 Phase 2 deliverables

- Files moved via `git mv` (preserves git history).
- README in archive.
- Reader compatibility commits (`daily_briefing`, `brain_health` graceful-degrade).
- Full pytest suite green.
- Active root modules importable (`sync`, `web_server`, `dashboard`, `strategy_runner`, `sniper_mcp`). Archived modules confirmed absent (`alerter`, `paper_trader`, `bot_detector`, `stream_low_buys`, `analyze_users`, `db_discovery`).
- Brain memo: `phase-2-legacy-pipeline-archived-2026-04-30`.

## 10. Phase 3 — Performance Pass (~2 days, conditional)

Default: sequential execution stays. Parallelism is opt-in only, gated on the correctness checks below.

### 10.1 Measure

- Add `--profile` flag to `scripts/run_wallet_protocol.py` that wraps `analyze_wallet` in `cProfile` and emits a per-stage breakdown.
- Baseline: 10 wallets across volume tiers (low: <500 fills, mid: 500-5000, high: >5000 incl. SwissMiss). Run each 3 times, record p50/p95.
- Output: `tests/calibration/perf_baseline_2026-04-30.md` with per-stage timings.
- Identify hotspots ≥30% of total time.

### 10.2 Apply only safe wins

Each candidate optimization MUST:

1. Pass the **verdict-identity gate**: same wallet, same seed, before vs after — verdicts AND `details` floats must match to 1e-9.
2. Pass the **calibration set**: full 10-wallet harness still ≥85% agreement.
3. Pass the **regression suite**: full pytest green.

Candidate optimizations (only apply if hotspot warrants):
- **Subgraph maker/taker concurrent** — currently serial; both queries can run on a thread. Verify rate-limit headroom on Goldsky public endpoint first (target: <5 RPS sustained).
- **Capital-ledger lazy** — only fetch when I1's DW/EW divergence section is actually rendered. Skip fetch when the consumer doesn't need it.
- **Bootstrap parallelism** — `bootstrap_ci` resamples are embarrassingly parallel; under `n_iter ≥ 5000` could be threaded. Verify seed-determinism preserved when threaded.

### 10.3 Batch parallelism (opt-in only)

- `scripts/run_wallet_protocol_batch.py --concurrent N` flag, default `N=1`.
- When `N>1`:
  - SQLite write-contention check: every write to `analyzer_runs`, `wallet_layer5_decision`, etc. retried with `OperationalError: database is locked` backoff per existing `db.py` patterns.
  - API rate-limit check: Polymarket + Goldsky concurrent calls instrumented; if 429 rate at >1% over a 100-wallet run, abort.
- Default stays `N=1` for first VPS deploy. Operator opts into `N>1` after sustained green at `N=2`, then `N=4`.

### 10.4 If no safe wins surface

Phase 3 closes with a brain memo `phase-3-performance-no-changes-2026-04-30` recording the baseline and the decision to ship sequential. Phase 4 proceeds.

### 10.5 Phase 3 deliverables

- `--profile` flag and baseline doc.
- Any optimization commits (verdict-identity-gated).
- Decision memo on parallelism (default N=1, opt-in tested at N=2, deferred at N=4).
- Calibration harness still green.

## 11. Phase 4 — Deploy + VPS Calibration (~2 days)

### 11.1 Pre-flight

- Local main matches `origin/main`.
- All Phase 1+2+3 commits merged.
- Tagged release: `v3.0-analyzer-complete`.
- `STATUS.md` reflects expected post-deploy state.
- VPS: confirm Phase 0 unit matrix still in effect (no drift).

### 11.2 Deploy steps

**Deploy mode is locked: manual oneshot first, no auto-restart.** Timers are not enabled in this push. Operator runs the analyzer on demand; a recurring schedule is a follow-on decision after the manual mode is proven on VPS.

```bash
# VPS
cd ~/sniper && git fetch && git checkout v3.0-analyzer-complete
.venv/bin/pip install -r requirements.txt
python3 scripts/brain_index.py generate

# Install the new analyzer's oneshot service unit, but leave it DISABLED.
# Unit name distinct from any retired unit: sniper-wallet-analyzer.service
# Service file shape (committed in repo systemd/):
#   [Service] Type=oneshot ExecStart=/home/sniper/sniper/.venv/bin/python3 \
#     /home/sniper/sniper/scripts/run_wallet_protocol_batch.py --from-queue \
#     --limit 10 --per-wallet-timeout-s 1800 --no-persist-memo
#   ExecStartPre=/usr/bin/test -f /home/sniper/sniper/sniper_tracker.db
#   StandardOutput=append:/home/sniper/sniper/logs/wallet_analyzer.log
#   StandardError=append:/home/sniper/sniper/logs/wallet_analyzer.log
# (Final command flags decided in writing-plans phase.)

systemctl --user daemon-reload
systemctl --user restart sniper-web   # only re-restart what was already running
# DO NOT enable any new timer in this deploy. No auto-restart.
# Manual invocation pattern, run by operator:
systemctl --user start sniper-wallet-analyzer.service   # one-shot, exits when done
```

A timer (`sniper-wallet-analyzer.timer`) may be installed in a *disabled* state so the unit file lives alongside the service for future use — but it is NOT enabled in Phase 4. Enabling it is gated on (a) operator running ≥3 successful manual invocations, (b) seven-day stability window, (c) explicit decision recorded in a separate brain memo.

### 11.3 5-wallet VPS calibration

Run the 5 high-evidence calibration wallets on VPS using the same fixture-driven calibration mode as Phase 1 (§7.5). Wallet selection (3 confirmed + 2 selected in Phase 1 step 1) recorded in `tests/calibration/labels.yaml`; deploy phase reads from that file.

```bash
WALLETS=$(python3 -c "import yaml; d=yaml.safe_load(open('tests/calibration/labels.yaml')); print(' '.join(w['address'] for w in d['high_evidence']))")
mkdir -p /tmp/vps_calibration
for addr in $WALLETS; do
    python3 scripts/run_wallet_protocol.py "$addr" \
        --from-fixture "tests/calibration/fixtures/${addr}_2026-04-30.json" \
        --no-persist --no-persist-memo \
        --seed 1 --n-iter 2000 --json \
        > "/tmp/vps_calibration/${addr}.json"
done
python3 scripts/calibration_report.py --vps-runs /tmp/vps_calibration \
    --local-baseline tests/calibration/runs/ --out /tmp/vps_calibration/report.md
```

**Run-mode declaration (matches §7.5):**
- Persistence: `--no-persist` (no writes to `analyzer_runs` / `wallet_layer5_decision`)
- Memo: `--no-persist-memo` (no writes to Supabase brain)
- Seed: `1`
- Iterations: `2000`
- Per-wallet timeout: 1800s (default; explicit if needed via `--per-wallet-timeout-s 1800`)
- Source: `--from-fixture` (live API not used; reproducibility is the point)
- Expected artifact: `/tmp/vps_calibration/<addr>.json` per wallet, plus consolidated `report.md`

**Acceptance:** `report.md` shows match for each wallet's spec-relevant decision fields (verdict, primary_edge_type, n_closed_positions, dw_roi_point, ew_roi_point, per-hypothesis verdict) versus the Phase-1 baseline. Same seed → bit-identical on bootstrap-driven floats. Any divergence triggers Gate 3 BLOCK pending root-cause.

### 11.4 brain_health update

`scripts/brain_health.py` checks updated:
- A0 stream staleness → marks PASS if `LEGACY_PIPELINE_DISABLED=1` set.
- A5 orphan-wallet → graceful degrade to INFO when `wallet_analysis_queue` archived.
- New check: `A6 analyzer freshness` — most recent `analyzer_runs.finished_ts` < 24h or status INFO/WARN configurable.

### 11.5 Phase 4 deliverables

- Tagged release deployed.
- VPS calibration: 5-wallet match.
- `brain_health` updated.
- New runbook: `docs/runbooks/wallet-analyzer-operations.md`.
- Brain memo: `phase-4-deploy-and-verify-2026-04-30`.

## 12. Gate 3 — Live VPS Audit

**Codex deliverables:**
- SSH to VPS, confirm unit matrix matches Phase 0 verdicts.
- Run brain_health, expected output PASS or graceful INFO.
- Spot-check: `analyzer_runs` table has rows from VPS-side execution dated post-deploy.
- Confirm dashboard reachable, leaderboard renders (stale data OK).
- Confirm no legacy unit accidentally running (`systemctl --user status sniper-stream sniper-analyzer` → masked).
- Verdict: `pass` / `block`.

Output: `docs/audits/codex/2026-04-30-gate-3.md`.

## 13. Risks

| ID | Risk | Mitigation |
|---|---|---|
| R1 | M4 (cross-event correlation) is open-ended; could exceed 2-day timebox. | Binary acceptance: implemented + stress-tested OR documented limitation. Does not block Phase 2. |
| R2 | §4.2 import audit may surface that `bot_detector.py` / `classify_accounts.py` is read by new analyzer indirectly. | Narrow Phase 2 archive scope; those modules stay at root, classified for a later phase. |
| R3 | Calibration set requires 10 hand-labels; medium-evidence wallets may not have enough public information. | High-evidence 5 (3 confirmed: SwissMiss, eatpraylove, YatSen + 2 selected from existing dossiers in Phase 1 step 1) → exact verdict. Medium-evidence 5 → band only. ≥85% bar applies to whichever label type is assigned. Selection rationale and addresses recorded in `tests/calibration/labels.yaml` before any test runs. |
| R4 | Active code (web_server, sync, strategy_runner, etc.) may surface `ModuleNotFoundError` if any latent import of an archived module is missed. | §9.2 reader compat audit + Phase 2 acceptance gate: `find_spec` returns `None` for archived modules AND `python3 -c "import sync, web_server, dashboard, strategy_runner, sniper_mcp"` succeeds. Both halves required to close Phase 2. |
| R5 | Subgraph endpoint (Goldsky public) rate-limited or deprecated mid-Phase-1. | Subgraph reader has a `Session` parameter; deploy fallback to a paid endpoint without code change if needed. Document in §10.2. |
| R6 | Replay determinism test fails because bootstrap CI varies under same seed. | Audit `numpy.random` vs `random` usage; standardize on `numpy.random.default_rng(seed)`. |
| R7 | VPS deploy fails because new analyzer batch unit conflicts with archived legacy unit names. | Pick distinct unit name (`sniper-wallet-analyzer-batch.{service,timer}`) — non-overlapping with any legacy unit in Phase 0 matrix. |
| R8 | Codex disagrees with operator on a calibration label, breaking the ≥85% bar without protocol bug. | Disagreement rule (§7.5): triage as label dispute → operator updates label with brain memo OR downgrades to band. Does not silently shift the bar. |

## 14. Success criteria (overall done definition)

- All 5 OPEN bulletproofing gaps either closed (acceptance met) or documented as deferred limitations with operator sign-off.
- All 6 wiring follow-ups closed and verified.
- Observability: per-run JSON log + brain memo + failure taxonomy + idempotent resume — all tested.
- Replay determinism: SwissMiss replay matches bit-for-bit.
- Calibration set: 10 labels frozen, harness ≥85% agreement.
- Legacy pipeline archived; full pytest green; all active services importable.
- VPS deployed at `v3.0-analyzer-complete` tag; 5-wallet calibration matches local; brain_health PASS or graceful INFO; no legacy unit active.
- Four Codex audit reports filed: Gate 1 (`docs/audits/codex/2026-04-30-gate-1.md`, pre-plan spec review — already filed in this revision cycle), Gate 1.5 (`gate-1.5.md`, pre-Phase-1), Gate 2 (`gate-2.md`, post-Phase-1, most important), Gate 3 (`gate-3.md`, post-deploy).
- All four phase brain memos persisted.

## 15. References

| Item | Source |
|---|---|
| Bulletproofing gap taxonomy | `docs/superpowers/specs/wallet-protocol-bulletproofing.md` |
| Legacy remnant audit | `docs/audits/2026-04-29-legacy-remnant-audit.md` |
| Legacy taxonomy readers (companion to remnant audit) | `docs/audits/2026-04-24-legacy-taxonomy-readers.md` |
| Wallet protocol v2 spec | `docs/superpowers/specs/wallet-analysis-protocol-v2.md` |
| ADR-006 (P&L-first scoring) | CLAUDE.md |
| ADR-007 (two-score system) | CLAUDE.md |
| Phase 3 reorg parent | `docs/superpowers/specs/2026-04-27-root-utilities-reorg-design.md` |
| Phase 3 freeze handoff | `docs/handoffs/2026-04-29-phase3-freeze-handoff.md` |
| Per-wallet timeout ship | commit `48a29a3` |
| LEGACY_STREAM_FROZEN intake guards | commit `4fef2ae` |

## 16. Codex review checklist (per gate)

### Gate 1 — Spec review (this document, pre-Phase-0)
- Sequencing sound? Any phase too tightly or loosely scoped?
- Phase 0 unit matrix complete? Any unit on VPS not classified?
- DB write-surface audit list complete? Any analyzer-relevant table missing?
- Calibration labeling protocol unambiguous? Disagreement rule executable?
- M4 binary acceptance language watertight?
- Any open question or contradiction in the spec?

### Gate 1.5 — Phase 0/0.5 audit
- Import-graph audit reproduces.
- DB write-surface audit reproduces.
- `LEGACY_PIPELINE_DISABLED` guard reachable + tested.
- Pytest green under guard.

### Gate 2 — Phase 1 audit
- Each gap closure reproducible.
- Calibration ≥85% (no silent label drift).
- Observability artifacts present + populated under fault injection.
- Replay determinism bit-identical.
- Adversarial cross-check on 1 calibration wallet.

### Gate 3 — Live VPS audit
- Unit matrix preserved.
- brain_health PASS / graceful INFO.
- VPS-side analyzer_runs rows post-deploy.
- Dashboard reachable.
- No legacy unit active.
