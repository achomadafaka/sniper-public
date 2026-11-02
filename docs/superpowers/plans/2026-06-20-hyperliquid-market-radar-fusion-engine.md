# Hyperliquid Market Radar Fusion Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed-asset Hyperliquid microstructure lane with a live paper Market Radar + Fusion Engine that selects markets, fuses catalyst/liquidation/microstructure inputs, paper-trades, and displays a clear cockpit UI.

**Architecture:** Keep `sniper/algos/hl_microstructure/` as the package, but add focused modules for market selection, catalysts, liquidation stress, and fusion scoring. Extend existing persistence/API/UI rather than creating a parallel app.

**Tech Stack:** Python 3.13, SQLite WAL, Flask JSON API, existing static `app.js` UI, public Hyperliquid REST/WS only.

## Global Constraints

- Paper only.
- No private keys.
- No live order endpoint.
- No production DB mutation.
- Missing/stale critical feeds fail closed.
- No single source family can force a trade.
- UI must state paper-only and explain entry/exit thesis in plain language.

---

### Task 1: Market Radar Core

**Files:**
- Create: `sniper/algos/hl_microstructure/radar.py`
- Test: `tests/test_hl_market_radar.py`

**Interfaces:**
- Produces: `rank_markets(markets: list[dict], *, max_assets: int = 6) -> list[RadarCandidate]`
- Produces: `RadarCandidate(asset, score, rank, reasons, volume_usdc, open_interest, funding, premium, selected)`

- [ ] Write tests for rejecting delisted/zero-volume/missing-mid markets and ranking high-volume/high-funding candidates.
- [ ] Run `python3 -m pytest tests/test_hl_market_radar.py -q` and verify red.
- [ ] Implement `RadarCandidate` and `rank_markets`.
- [ ] Run the focused test and verify green.

### Task 2: Catalyst Radar

**Files:**
- Create: `sniper/algos/hl_microstructure/catalysts.py`
- Test: `tests/test_hl_catalysts.py`

**Interfaces:**
- Produces: `CatalystSignal(asset, direction, score, headline, source, catalyst_type, reason, received_ts_ms)`
- Produces: `match_catalysts(news_items: list[dict], symbols: set[str], *, now_ms: int) -> dict[str, CatalystSignal]`

- [ ] Write tests for token-specific headlines, stale headlines, and generic crypto headlines that should not match a small-cap token.
- [ ] Run focused test and verify red.
- [ ] Implement deterministic matching and scoring.
- [ ] Run focused test and verify green.

### Task 3: Liquidation Stress Adapter

**Files:**
- Create: `sniper/algos/hl_microstructure/liquidation.py`
- Test: `tests/test_hl_liquidation_stress.py`

**Interfaces:**
- Produces: `LiquidationStress(asset, direction, score, notional_at_risk, nearest_distance_pct, reason, stale)`
- Produces: `stress_from_positions(rows: list[dict], *, now_ms: int, max_age_ms: int = 300000) -> dict[str, LiquidationStress]`

- [ ] Write tests for stale rows, near-liquidation long/short rows, and cross/unsupported rows contributing quality penalties.
- [ ] Run focused test and verify red.
- [ ] Implement the adapter without importing archived strategy logic directly.
- [ ] Run focused test and verify green.

### Task 4: Fusion Scorer and Dynamic Exit Plan

**Files:**
- Create: `sniper/algos/hl_microstructure/fusion.py`
- Modify: `sniper/algos/hl_microstructure/policies.py`
- Test: `tests/test_hl_fusion.py`

**Interfaces:**
- Produces: `FusionContext(catalysts, liquidation_stress, radar_candidates)`
- Produces: `evaluate_fusion_policy(snapshot, context) -> PolicyDecision`
- Produces: `dynamic_exit_plan(snapshot, entry_features, side) -> dict`

- [ ] Write tests proving two agreeing source families are required.
- [ ] Write tests proving catalyst-only and liquidation-only do not enter.
- [ ] Write tests proving strong flow + book + catalyst can enter.
- [ ] Run focused test and verify red.
- [ ] Implement fusion scoring and dynamic exit plan fields.
- [ ] Run focused test and verify green.

### Task 5: Persistence/API Extensions

**Files:**
- Modify: `sniper/algos/hl_microstructure/persistence.py`
- Modify: `sniper/routes/api_hl_microstructure.py`
- Test: `tests/test_api_hl_microstructure.py`

**Interfaces:**
- Adds table: `hlm_radar_snapshots`
- Adds API fields: `radar`, `source_votes`, `thesis`, `exit_plan`, `paper_status`

- [ ] Write API test that seeds radar rows and expects cockpit payload fields.
- [ ] Run focused test and verify red.
- [ ] Add schema, record/query helpers, and API payload.
- [ ] Run focused test and verify green.

### Task 6: Dynamic Runner

**Files:**
- Modify: `scripts/run_hl_microstructure_paper.py`
- Test: `tests/test_hl_microstructure_cli.py`

**Interfaces:**
- Adds CLI flags: `--auto-select-assets`, `--max-assets`, `--asset-refresh-seconds`
- Uses `rank_markets`, catalyst signals, liquidation stress, and fusion policy.

- [ ] Write fixture-mode CLI test proving auto-select persists radar rows and decisions on the selected synthetic asset.
- [ ] Run focused test and verify red.
- [ ] Implement auto-selection in fixture and live modes.
- [ ] Run focused test and verify green.

### Task 7: Cockpit UI

**Files:**
- Modify: `templates/index.html`
- Modify: `static/app.js`
- Test: `tests/test_api_hl_microstructure.py` for payload shape, plus `node --check static/app.js`

**Interfaces:**
- Replaces raw Hyperliquid table-first rendering with cockpit sections backed by `/api/hl-microstructure/status`.

- [ ] Update navigation labels to Market Radar.
- [ ] Render paper status, radar assets, open thesis cards, source votes, exit plans, and skip reasons.
- [ ] Remove legacy spot diagnostic from the primary Hyperliquid cockpit.
- [ ] Run `node --check static/app.js`.

### Task 8: Verification and Local Runtime

**Files:**
- No new files.

- [ ] Run focused Hyperliquid tests.
- [ ] Run schema validator.
- [ ] Run `node --check static/app.js`.
- [ ] Start/restart paper runner with `--auto-select-assets`.
- [ ] Confirm API returns radar and paper status.
- [ ] Confirm UI loads the Market Radar tab.
