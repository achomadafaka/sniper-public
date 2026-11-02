# Algorithm Discovery Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chain the 8 existing wallet-analysis stages into one resumable, end-to-end "discover a wallet's algorithm and prove it" workflow with a pre-registered decision gate, and fix the 4 shadow-copy seam bugs that block the paper-mirror stage.

**Architecture:** Thin sequential orchestrator (`sniper/analysis/algo_discovery/`) writes one JSON artifact per stage into a per-run directory and resumes via a manifest. A new pure `decision_gate` applies literature-grounded, prediction-market-correct kill-criteria to the backtest result. Existing entry points are wrapped by stage adapters; no existing analyzer logic is rewritten.

**Tech Stack:** Python 3.13, stdlib (dataclasses, json, hashlib, math, pathlib, argparse), pytest + pytest-xdist, existing `sniper.analysis.*` modules, `web_settings.py` for thresholds.

Spec: `docs/superpowers/specs/2026-05-29-algorithm-discovery-workflow-design.md`

---

## File Structure

- Create `sniper/analysis/algo_discovery/__init__.py` — package exports.
- Create `sniper/analysis/algo_discovery/decision_gate.py` — pure kill-criteria evaluator (Phase 2).
- Create `sniper/analysis/algo_discovery/artifacts.py` — run-dir + manifest read/write + input hashing (Phase 3).
- Create `sniper/analysis/algo_discovery/pipeline.py` — stage adapters + the driver (Phase 3).
- Create `scripts/run_algo_discovery.py` — CLI (Phase 3).
- Modify `sniper/strategies/shadow_copy_v1.py` — B1 (enter signature), B2 (dedup), B4 (opened_ts) (Phase 1).
- Modify `scripts/poll_shadow_copy_targets.py` — B3 (fetch_user_activity call) (Phase 1).
- Modify `web_settings.py` — decision-gate thresholds (Phase 2).
- Create `tests/algo_discovery/test_decision_gate.py`, `tests/algo_discovery/test_artifacts.py`, `tests/algo_discovery/test_pipeline.py`, `tests/algo_discovery/test_e2e.py` (Phases 2-4).
- Modify `tests/test_shadow_copy_v1.py` — bug regression tests (Phase 1).
- Create `tests/algo_discovery/fixtures/<wallet>_held.json` — committed e2e WalletData fixture with redemptions (Phase 4).

---

## Phase 1 — shadow_copy_v1 seam fixes (independent; prerequisite for `--enroll`)

> Read each target before editing (verify-before-planning): `sniper/strategies/shadow_copy_v1.py`, `sniper/strategies/base.py`, `sniper/services/strategy_runner.py` around the `.enter(` call, `scripts/poll_shadow_copy_targets.py`.

### Task 1.1: B1 — align `enter()` to the base ABC

**Files:**
- Modify: `sniper/strategies/shadow_copy_v1.py` (`enter` def ~:405)
- Test: `tests/test_shadow_copy_v1.py`

- [ ] **Step 1: Write the failing test** — calls `enter` the way `strategy_runner` does (positional `conn, strategy, signal, open_positions`) and asserts the decision is coherent.

```python
def test_enter_matches_base_abc_call_order(tmp_path):
    import sqlite3, inspect
    from sniper.strategies.shadow_copy_v1 import ShadowCopyV1Strategy
    from sniper.strategies import base
    plugin = ShadowCopyV1Strategy()
    # Signature must match the ABC exactly (conn, strategy, signal, open_positions)
    abc_params = list(inspect.signature(base.StrategyPlugin.enter).parameters)
    plugin_params = list(inspect.signature(plugin.enter).parameters)
    assert plugin_params == abc_params, (plugin_params, abc_params)
```

- [ ] **Step 2: Run to verify it fails**
Run: `python3 -m pytest tests/test_shadow_copy_v1.py::test_enter_matches_base_abc_call_order -v`
Expected: FAIL — current order is `(self, conn, signal, strategy)`.

- [ ] **Step 3: Fix** — reorder the `enter` signature to `enter(self, conn, strategy, signal, open_positions)`; update the body's references (the object currently named `signal` is the signal, `strategy` the dict); use `open_positions` if needed for the concurrency gate (or keep the existing COUNT query). Confirm `manage`/`exit` ABC alignment while here only if they also diverge (they take `conn, strategy, position` per the map — leave if matching).

- [ ] **Step 4: Run to verify it passes** (and the existing enter tests still pass)
Run: `python3 -m pytest tests/test_shadow_copy_v1.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add sniper/strategies/shadow_copy_v1.py tests/test_shadow_copy_v1.py
git commit -m "fix(shadow-copy): align enter() signature with StrategyPlugin ABC + runner call site"
```

### Task 1.2: B2 — call `mark_pending_processed` so dedup fires

**Files:**
- Modify: `sniper/strategies/shadow_copy_v1.py`
- Test: `tests/test_shadow_copy_v1.py`

- [ ] **Step 1: Failing test** — two discover passes over the same pending row must not both yield a fresh signal once the first is marked processed.

```python
def test_processed_pending_is_deduped(tmp_path):
    import sqlite3
    from sniper.strategies.shadow_copy_v1 import (
        ShadowCopyV1Strategy, insert_pending_signal, mark_pending_processed,
        ensure_shadow_copy_schema,  # use the real schema helper if named differently
    )
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    ensure_shadow_copy_schema(conn)
    wallet = "0x" + "a"*40
    pid = insert_pending_signal(conn, wallet=wallet, condition_id="0xc", asset_id="1",
        outcome="YES", side="BUY", price=0.4, size_usdc=200, market_category="x",
        market_type="binary", outcome_role="underdog", title="t", observed_ts=1_900_000_000)
    strategy = {"id": 7, "params": {"target_wallets": [wallet]}}
    plugin = ShadowCopyV1Strategy()
    sigs1 = plugin.discover(conn, strategy)
    assert len(sigs1) == 1
    mark_pending_processed(conn, pid, strategy_id=7)
    sigs2 = plugin.discover(conn, strategy)
    assert sigs2 == []  # already processed → deduped
```

- [ ] **Step 2: Run → FAIL** (dedup guard checks `status='processed'` which is never set).
- [ ] **Step 3: Fix** — call `mark_pending_processed(conn, signal.metadata["pending_id"], strategy_id=<id>)` at the correct lifecycle point. Cleanest in v1: have the plugin's `enter()` (now ABC-aligned) mark the row processed when it returns `should_enter=True`; the test calls it directly to assert the dedup path. If the runner records the position before calling a post-hook, mark inside `enter` right before returning the decision. Document the chosen call point in a comment.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `fix(shadow-copy): mark pending rows processed so the dedup guard fires`

### Task 1.3: B3 — fix the poller's `fetch_user_activity` call

**Files:**
- Modify: `scripts/poll_shadow_copy_targets.py` (`_fetch_wallet_activity` ~:65)
- Test: `tests/algo_discovery/test_pipeline.py` (or a poller test)

- [ ] **Step 1: Failing test** — monkeypatch `fetch_user_activity` and assert `_fetch_wallet_activity` calls it with a signature it actually accepts (positional session first).

```python
def test_poller_calls_fetch_user_activity_with_valid_signature(monkeypatch):
    import scripts.poll_shadow_copy_targets as mod
    captured = {}
    def fake_fetch(session, wallet, *args, **kwargs):
        captured["wallet"] = wallet; return []
    monkeypatch.setattr(mod, "fetch_user_activity", fake_fetch)
    mod._fetch_wallet_activity(object(), "0x"+"a"*40, since_ts=1_900_000_000)
    assert captured["wallet"] == "0x"+"a"*40
```

- [ ] **Step 2: Run → FAIL** (TypeError: unexpected kwargs).
- [ ] **Step 3: Fix** — read `fetch_user_activity`'s real signature in `sniper/markets/polymarket_api.py`; call it with the correct positional `session` and supported params; drop unsupported `since_ts` (filter client-side by `timestamp >= since_ts` after the call). Remove the `# type: ignore[call-arg]`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `fix(shadow-copy): poll_shadow_copy_targets calls fetch_user_activity with a valid signature`

### Task 1.4: B4 — `max_hold` reads the real `opened_ts` column

**Files:**
- Modify: `sniper/strategies/shadow_copy_v1.py` (`exit` ~:478)
- Test: `tests/test_shadow_copy_v1.py`

- [ ] **Step 1: Failing test** — a position dict with `opened_ts` older than `max_hold_days` must trigger `should_exit=True` (early_exit).

```python
def test_max_hold_exit_uses_opened_ts():
    import sqlite3
    from sniper.strategies.shadow_copy_v1 import ShadowCopyV1Strategy
    plugin = ShadowCopyV1Strategy()
    strategy = {"id": 1, "params": {"max_hold_days": 30}}
    now = 1_900_000_000
    position = {"opened_ts": now - 40*86400, "current_price": 0.5,
                "condition_id": "0xc", "asset_id": "1"}
    decision = plugin.exit(sqlite3.connect(":memory:"), strategy, position)
    assert decision.should_exit is True
    assert "hold" in (decision.exit_type or "").lower() or decision.exit_type == "early_exit"
```
(Use a fixed `now`; if `exit` reads wall-clock, inject via the position or a monkeypatched time source — match the plugin's actual time access.)

- [ ] **Step 2: Run → FAIL** (reads `opened_at_ts`/`opened_at`, gets None, gate never fires).
- [ ] **Step 3: Fix** — read `position.get("opened_ts")` (fallback to the old keys for safety: `position.get("opened_ts") or position.get("opened_at_ts") or position.get("opened_at")`).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `fix(shadow-copy): max_hold exit reads opened_ts (the real strategy_positions column)`

### Task 1.5: Phase-1 regression sweep
- [ ] Run `python3 -m pytest tests/test_shadow_copy_v1.py -q` → all green.
- [ ] Run `python3 -m pytest tests/strategies/ -q` (or the strategy_runner tests) to confirm the ABC change didn't break the runner integration.

---

## Phase 2 — decision gate (new pure module)

### Task 2.1: thresholds in `web_settings.py`

**Files:** Modify `web_settings.py`

- [ ] **Step 1:** Add (follow the file's existing env-var pattern; show actual lines):
```python
# Algorithm-discovery decision gate (algo_discovery_v1)
ALGO_DISCOVERY_MIN_OOS_DECIDED = int(os.getenv("ALGO_DISCOVERY_MIN_OOS_DECIDED", "20"))
ALGO_DISCOVERY_LOW_CONFIDENCE_OOS = int(os.getenv("ALGO_DISCOVERY_LOW_CONFIDENCE_OOS", "100"))
ALGO_DISCOVERY_EDGE_Z = float(os.getenv("ALGO_DISCOVERY_EDGE_Z", "1.96"))
ALGO_DISCOVERY_SLIPPAGE_STRESS_PCT = float(os.getenv("ALGO_DISCOVERY_SLIPPAGE_STRESS_PCT", "0.015"))
ALGO_DISCOVERY_SIM_FIDELITY_MULT = float(os.getenv("ALGO_DISCOVERY_SIM_FIDELITY_MULT", "3.0"))
```
- [ ] **Step 2:** `python3 -c "import web_settings"` → no error. **Commit.**

### Task 2.2: decision gate — disqualifying caveats + sample adequacy

**Files:** Create `sniper/analysis/algo_discovery/decision_gate.py`, `tests/algo_discovery/test_decision_gate.py`

- [ ] **Step 1: Failing tests**
```python
from sniper.analysis.algo_discovery.decision_gate import evaluate, DecisionVerdict

def _bt(**kw):
    base = dict(harness_version="backtest_v1", oos_simulated_pnl_usdc=50.0,
        oos_simulated_wins=30, oos_simulated_losses=10, oos_match_count=40,
        oos_simulated_win_rate=0.75, oos_actual_pnl_usdc_on_same_positions=40.0,
        caveats=[], per_position_results=[{"sim_entry_price":0.4,"final_price":1.0}]*40)
    base.update(kw); return base

def test_unsupported_exit_mode_rejects():
    v = evaluate(_bt(caveats=["unsupported_exit_mode:pre_resolution"]), {"exit_rule":{"mode":"pre_resolution"}})
    assert v.verdict == "reject"
    assert "exit_mode_unsupported" in v.reason_codes

def test_too_few_oos_is_insufficient():
    v = evaluate(_bt(oos_simulated_wins=5, oos_simulated_losses=3, oos_match_count=8), {})
    assert v.verdict == "insufficient_data"
    assert "oos_sample_below_floor" in v.reason_codes
```
- [ ] **Step 2: Run → FAIL** (module missing).
- [ ] **Step 3: Implement** the module (full code):
```python
from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Any, Mapping
import web_settings as ws

GATE_VERSION = "algo_discovery_decision_gate_v1"
_DISQUALIFYING = {
    "unsupported_exit_mode": "exit_mode_unsupported",
    "rule_has_no_market_filter": "no_market_filter",
    "no_oos_matches_for_rule": "no_oos_matches",
    "no_closed_positions": "no_closed_positions",
}

@dataclass
class DecisionVerdict:
    gate_version: str
    verdict: str            # proceed_to_paper | reject | insufficient_data
    reason_codes: list[str]
    metrics: dict[str, Any]

def _caveat_reason(caveats: list[str]) -> str | None:
    for cav in caveats or []:
        for prefix, code in _DISQUALIFYING.items():
            if str(cav).startswith(prefix):
                return code
    return None

def _binomial_z(wins: int, n: int, p0: float) -> float | None:
    if n <= 0: return None
    p0 = min(max(p0, 1e-6), 1 - 1e-6)
    se = math.sqrt(p0 * (1 - p0) / n)
    if se == 0: return None
    return (wins / n - p0) / se

def _null_win_rate(per_position: list[Mapping]) -> float | None:
    prices = [float(r["sim_entry_price"]) for r in (per_position or [])
              if r.get("sim_entry_price") is not None]
    return sum(prices) / len(prices) if prices else None

def evaluate(backtest: Mapping[str, Any], rule: Mapping[str, Any],
             *, stressed_backtest: Mapping[str, Any] | None = None) -> DecisionVerdict:
    reasons: list[str] = []
    wins = int(backtest.get("oos_simulated_wins") or 0)
    losses = int(backtest.get("oos_simulated_losses") or 0)
    n_decided = wins + losses
    pnl = float(backtest.get("oos_simulated_pnl_usdc") or 0.0)
    null_wr = _null_win_rate(backtest.get("per_position_results") or [])
    metrics = {"n_decided": n_decided, "oos_pnl_usdc": pnl,
               "oos_win_rate": (wins / n_decided if n_decided else None),
               "null_win_rate": null_wr}

    # 1. disqualifying caveats
    cav = _caveat_reason(list(backtest.get("caveats") or []))
    if cav:
        return DecisionVerdict(GATE_VERSION, "reject", [cav], metrics)
    if "rule_size_missing_using_10usdc_default" in (backtest.get("caveats") or []):
        reasons.append("synthetic_size_used")

    # 2. sample adequacy
    if n_decided < ws.ALGO_DISCOVERY_MIN_OOS_DECIDED:
        return DecisionVerdict(GATE_VERSION, "insufficient_data",
                               ["oos_sample_below_floor"], metrics)
    if n_decided < ws.ALGO_DISCOVERY_LOW_CONFIDENCE_OOS:
        reasons.append("low_confidence_sample")

    # 3. profitability
    if pnl <= 0:
        return DecisionVerdict(GATE_VERSION, "reject", reasons + ["oos_not_profitable"], metrics)

    # 4. edge vs price-implied null (NOT 0.5)
    if null_wr is None:
        return DecisionVerdict(GATE_VERSION, "insufficient_data",
                               reasons + ["no_per_position_for_null"], metrics)
    z = _binomial_z(wins, n_decided, null_wr)
    metrics["edge_z"] = z
    if z is None or z < ws.ALGO_DISCOVERY_EDGE_Z:
        return DecisionVerdict(GATE_VERSION, "reject",
                               reasons + ["edge_not_significant_vs_price_null"], metrics)

    # 5. slippage robustness (if a stressed run was supplied)
    if stressed_backtest is not None:
        spnl = float(stressed_backtest.get("oos_simulated_pnl_usdc") or 0.0)
        metrics["stressed_oos_pnl_usdc"] = spnl
        if spnl <= 0:
            return DecisionVerdict(GATE_VERSION, "reject",
                                   reasons + ["edge_slippage_fragile"], metrics)

    # 6. sim-vs-wallet fidelity (soft)
    actual = float(backtest.get("oos_actual_pnl_usdc_on_same_positions") or 0.0)
    if actual > 0 and pnl > ws.ALGO_DISCOVERY_SIM_FIDELITY_MULT * actual:
        reasons.append("sim_exceeds_wallet_fidelity_warn")

    return DecisionVerdict(GATE_VERSION, "proceed_to_paper", reasons or ["passed"], metrics)
```
- [ ] **Step 4: Run → PASS** the two tests above.
- [ ] **Step 5: Commit** `feat(algo-discovery): decision gate — disqualifying caveats + sample adequacy`

### Task 2.3: decision gate — profitability, edge-vs-null, slippage, fidelity

- [ ] **Step 1: Failing tests** (add to the same test file):
```python
def test_profitable_but_below_price_null_rejects():
    # 40 underdog buys at 0.40, 18 wins → WR 0.45 > null 0.40 but z small? construct n=40, wins=18
    v = evaluate(_bt(oos_simulated_wins=18, oos_simulated_losses=22, oos_match_count=40,
        oos_simulated_pnl_usdc=5.0,
        per_position_results=[{"sim_entry_price":0.40,"final_price":1.0}]*40), {})
    assert v.verdict == "reject" and "edge_not_significant_vs_price_null" in v.reason_codes

def test_strong_edge_proceeds():
    v = evaluate(_bt(oos_simulated_wins=34, oos_simulated_losses=6, oos_match_count=40,
        oos_simulated_pnl_usdc=120.0,
        per_position_results=[{"sim_entry_price":0.40,"final_price":1.0}]*40), {})
    assert v.verdict == "proceed_to_paper"

def test_slippage_fragile_rejects():
    bt = _bt(oos_simulated_wins=34, oos_simulated_losses=6, oos_match_count=40,
        per_position_results=[{"sim_entry_price":0.40,"final_price":1.0}]*40)
    v = evaluate(bt, {}, stressed_backtest=_bt(oos_simulated_pnl_usdc=-15.0))
    assert v.verdict == "reject" and "edge_slippage_fragile" in v.reason_codes
```
- [ ] **Step 2-4:** these should already pass against the Task-2.2 implementation; if any fails, fix the implementation (not the test). Verify the `test_strong_edge_proceeds` z math: wins=34,n=40,p0=0.40 → z=(0.85-0.40)/sqrt(0.4*0.6/40)=0.45/0.0775≈5.8 ≥1.96 ✓.
- [ ] **Step 5: Commit** `test(algo-discovery): decision gate edge/slippage/fidelity criteria`

---

## Phase 3 — orchestrator (new package)

### Task 3.1: artifacts + manifest

**Files:** Create `sniper/analysis/algo_discovery/artifacts.py`, `tests/algo_discovery/test_artifacts.py`

- [ ] **Step 1: Failing test** — round-trip an artifact, hash inputs, manifest records stage status; resume skips a stage whose artifact exists with an unchanged input hash.
```python
def test_manifest_skip_when_unchanged(tmp_path):
    from sniper.analysis.algo_discovery.artifacts import RunDir
    rd = RunDir(tmp_path / "run")
    assert rd.should_run("04_wallet_rule", input_hash="abc") is True
    rd.write_artifact("04_wallet_rule", {"x": 1}, input_hash="abc")
    assert rd.should_run("04_wallet_rule", input_hash="abc") is False  # cached
    assert rd.should_run("04_wallet_rule", input_hash="def") is True   # input changed
    assert rd.read_artifact("04_wallet_rule") == {"x": 1}
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `RunDir` (full): a class over a run directory with `write_artifact(stage, obj, input_hash)` (writes `<stage>.json` + updates `run_manifest.json` with status/timestamp/input_hash), `read_artifact(stage)`, `should_run(stage, input_hash)` (True unless artifact exists AND manifest input_hash matches AND not forced), `mark_failed(stage, err)`, and a `force`/`from_stage` flag. Use `json.dumps(..., sort_keys=True)` for deterministic hashing; `hashlib.sha256` for `hash_obj(obj)`.
- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat(algo-discovery): run-dir artifact store + resume manifest`

### Task 3.2: stage adapters

**Files:** Create `sniper/analysis/algo_discovery/pipeline.py`, `tests/algo_discovery/test_pipeline.py`

Each adapter wraps an existing entry point. Define them with stubbable seams so tests don't need live API. Signatures:
```python
def stage_fetch(wallet, ctx) -> dict            # WalletData json (live fetch.fetch_wallet_data or --from-fixture)
def stage_protocol(wallet, wallet_data, ctx) -> dict   # asdict(analyze_wallet(..., walk_full_history=True))
def stage_dossier(wallet, wallet_data, report, ctx) -> "DossierTables"  # build_tables(wallet=…, wallet_data=…, protocol_report=…)
def stage_rule(tables, ctx) -> dict             # extract_wallet_rule(tables)
def stage_backtest(rule, wallet_data, ctx, *, slippage_pct) -> dict     # backtest_rule_on_wallet(..., include_per_position=True)
def stage_decision(backtest, stressed, rule, ctx) -> dict               # asdict(decision_gate.evaluate(...))
def stage_enrollment(wallet, rule, decision, ctx) -> dict | None        # shadow_copy strategy params, only if proceed
```

- [ ] **Step 1: Failing test** — `stage_decision` wires backtest+stressed into the gate; `stage_enrollment` returns None unless `proceed_to_paper`; `stage_rule` short-circuits on `no_interpretable_grain`.
```python
def test_stage_enrollment_only_on_proceed():
    from sniper.analysis.algo_discovery.pipeline import stage_enrollment
    assert stage_enrollment("0xw", {"market_filter": {}, "sizing": {}},
                            {"verdict": "reject"}, ctx=None) is None
    out = stage_enrollment("0xw", {"market_filter": {"categories": ["x"]},
                            "sizing": {"median_usdc": 30}}, {"verdict": "proceed_to_paper"}, ctx=None)
    assert out["strategy_type"] == "shadow_copy_v1"
    assert out["params"]["target_wallets"] == ["0xw"]
```
- [ ] **Step 2: FAIL. Step 3: Implement** the adapters. `stage_backtest` is called twice by the driver (normal + stressed slippage) — the adapter takes `slippage_pct`. `stage_enrollment` maps `rule.market_filter` → params `market_filter`, `rule.sizing.median_usdc` → `max_position_size_usdc` clamped to a paper cap (e.g. min(rule_median, 25)), `target_wallets=[wallet]`. Use `dataclasses.asdict` on `BacktestResult`/`DecisionVerdict` where they return dataclasses (decision_gate returns a dataclass → asdict it).
- [ ] **Step 4: PASS. Step 5: Commit** `feat(algo-discovery): stage adapters wrapping each pipeline entry point`

### Task 3.3: the driver + CLI

**Files:** `pipeline.py` (`run_discovery`), `scripts/run_algo_discovery.py`

- [ ] **Step 1: Failing test** — `run_discovery` on a fixture path drives stages in order, writes artifacts, and returns a result dict with the final verdict; a `no_interpretable_grain` rule yields `insufficient_data` without reaching backtest.
```python
def test_run_discovery_insufficient_on_empty_rule(tmp_path, monkeypatch):
    import sniper.analysis.algo_discovery.pipeline as pl
    monkeypatch.setattr(pl, "stage_fetch", lambda w, ctx: {"wallet": w, "closed_positions": [], "activity_rows": []})
    monkeypatch.setattr(pl, "stage_protocol", lambda w, wd, ctx: {"wallet": w, "gate_result": "pass"})
    monkeypatch.setattr(pl, "stage_dossier", lambda w, wd, r, ctx: object())
    monkeypatch.setattr(pl, "stage_rule", lambda t, ctx: {"rejection_reason": "no_interpretable_grain", "exit_rule": None})
    res = pl.run_discovery("0x"+"a"*40, run_root=tmp_path, from_fixture={"wallet":"0x"+"a"*40})
    assert res["verdict"] == "insufficient_data"
```
- [ ] **Step 2: FAIL. Step 3: Implement** `run_discovery(wallet, *, run_root, from_fixture=None, enroll=False, force=False, from_stage=None)`: build `RunDir`; for each stage compute input_hash, `should_run`?, call adapter, `write_artifact`, on exception `mark_failed` + raise with "failed at stage". Short-circuit to `insufficient_data` when the rule has `rejection_reason` or `exit_rule` is None or `source_grain` n_episodes < `MIN_EPISODES`. Run backtest twice (normal `slippage_pct=0.005`, stressed `ws.ALGO_DISCOVERY_SLIPPAGE_STRESS_PCT`); pass both to `stage_decision`. Enroll only if `enroll and verdict=="proceed_to_paper"`. The CLI parses `--wallet`, `--from-fixture`, `--run-root`, `--enroll`, `--force`, `--from-stage`, prints the verdict + run-dir path, exit 0 even on reject/insufficient (valid outcomes), exit 1 only on stage failure.
- [ ] **Step 4: PASS. Step 5: Commit** `feat(algo-discovery): end-to-end driver + run_algo_discovery CLI`

---

## Phase 4 — fixtures + end-to-end + evaluation

### Task 4.1: held-exit e2e fixture

- [ ] **Step 1:** Obtain a `WalletData` JSON **with redemptions** for a held-to-resolution wallet. Source options, in order: (a) an existing staged fixture under `tests/calibration/` that has redemptions; (b) capture one locally via `run_wallet_protocol.py --wallet <held-wallet> --dump-fixture` if a dump path exists; (c) if neither, build a minimal synthetic fixture with ≥120 closed positions across ≥1 category resolving to known outcomes so grains are interpretable and exit_mode='held'. Commit under `tests/algo_discovery/fixtures/`.
- [ ] **Step 2:** Document in the fixture's sibling README which wallet/source and that it is held-exit.

### Task 4.2: positive + negative e2e

**Files:** `tests/algo_discovery/test_e2e.py`

- [ ] **Step 1: Tests**
```python
def test_e2e_held_fixture_produces_verdict(tmp_path):
    from sniper.analysis.algo_discovery.pipeline import run_discovery
    res = run_discovery(WALLET, run_root=tmp_path, from_fixture=load_fixture("held"))
    assert res["verdict"] in {"proceed_to_paper", "reject", "insufficient_data"}
    assert (tmp_path / ... / "04_wallet_rule.json").exists()
    assert res["backtest"]["oos_match_count"] >= 0

def test_e2e_no_redemption_fixture_is_insufficient(tmp_path):
    res = run_discovery(WALLET2, run_root=tmp_path, from_fixture=load_fixture("no_redemption"))
    assert res["verdict"] == "insufficient_data"  # graceful, no crash
```
- [ ] **Step 2-4:** Run; the held fixture must reach at least rule extraction + backtest without crashing. If `reject:exit_mode_unsupported` on a wallet believed to be held, inspect the extracted `exit_rule.mode` (a real finding about the wallet, not a bug).
- [ ] **Step 5: Commit** `test(algo-discovery): end-to-end positive + negative fixtures`

### Task 4.3: full-suite + evaluation
- [ ] Run `python3 -m pytest tests/ -q`, `python3 scripts/validate_schema_refs.py`, `python3 scripts/brain_index.py generate` (commit if changed).
- [ ] **Evaluate usefulness** (the deliverable judgement, written to the PR + a brain finding): run the workflow on SwissMiss / the $0.99 scraper fixtures; report whether the decision gate's verdict + reason_codes are actually informative, whether the extracted rule is faithful, and enumerate what's missing (walk-forward, pre-resolution exit replay, live-fetch geo path, multi-wallet SPA wiring).

---

## Self-Review

**Spec coverage:** §3 orchestrator → Phase 3; §4.2 decision gate → Phase 2; §4.3 shadow_copy fixes → Phase 1; §4.4 fixtures → Phase 4.1; §7 edge cases → encoded as tests (no_interpretable_grain, unsupported_exit_mode, too-few-OOS, no-redemption, slippage-fragile, enroll-without-proceed); §8 testing → Phases 2-4; §9 gaps → Task 4.3 evaluation writeup. SPA aggregate branch (§1 optional) is intentionally deferred from v1 build — noted as future, not a gap.

**Placeholder scan:** decision_gate is full code; artifacts/pipeline/CLI are specified by signature + behavior + exact entry-point calls + test code (the implementer has full repo context to fill bodies from the named real functions). Fixture sourcing (Task 4.1) has a concrete fallback ladder, not a TODO.

**Type consistency:** `DecisionVerdict` fields (gate_version, verdict, reason_codes, metrics) used consistently; `evaluate(backtest, rule, *, stressed_backtest=None)` signature matches Task-3.2 `stage_decision` call; verdict strings `proceed_to_paper|reject|insufficient_data` consistent across gate, adapters, driver, tests.

**Note for executor:** Phase 1 tasks edit existing files — read each target file region first (signatures change between versions). The plan's test code is the contract; transcribe exact before/after from the live file.
