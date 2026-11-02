# Polymarket Crypto5m Maker Regime Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a paper-only maker regime router that runs shadow-side-by-side before any live paper cutover.

**Architecture:** Keep `maker_policy.py` as the base quote engine. Add a pure `maker_regime.py` router that converts known-at-time market state into conservative policy multipliers and side enablement. Integrate only after pure routing, persistence, and shadow comparison are test-covered.

**Tech Stack:** Python 3.13, stdlib dataclasses/sqlite/json/hashlib, existing crypto5m runner and pytest.

## Global Constraints

- Paper-only. No live orders.
- No private keys, authenticated order endpoints, or production DB mutation.
- Do not replace current `maker-unified-v4` immediately.
- Shadow first for 20-30 windows as mechanical sanity only; profitability verdict remains `n >= 200` settled windows.
- Write only to separate paper/shadow namespace or campaign-partitioned rows.
- Do not merge regime-v1 results into v4.
- Do not use final outcome, post-resolution data, or future book state in routing.
- Preserve current print-only fill model.
- Keep Binance feature-only; Polymarket settlement truth remains independent.

---

## Corrected Design

The prior proposal is accepted with these corrections:

- Shadow-first is mandatory. The current v4 sample is too small for replacement.
- `hard_invalid` must pull all quotes; it must not request a hard cut when executable book/fair is missing.
- Use price/tick units, not bps, for binary-token margins and edges.
- Fee state is split by execution type: passive maker fills do not pay taker fee, hard-cuts/crosses do.
- Inventory cap precedes opportunity regimes: inventory risk is a hard state.
- Extreme odds are tiered, not all-or-none: severe extremes pull; moderate extremes quote tiny/wide or reduce-only.
- Shadow comparison owns its own paper book. It cannot reuse v4 actual fills as if path-independent.
- Add a durable decision table; quote/fill rows alone cannot explain no-quote decisions.
- Track gross inventory, net directional inventory, and complete sets separately.

## Regime Precedence

First match wins:

1. `hard_invalid`
2. `late_window`
3. `inventory_saturated`
4. `extreme_odds`
5. `jump`
6. `trend_confirmed`
7. `mid_chop`
8. `normal`

Hard-risk states dominate opportunity states. A late-window extreme-odds jump is late-window/reduce-only, not a jump opportunity.

## Policy Interface

Create `sniper/algos/polymarket_crypto5m/maker_regime.py`.

Pure compute:

```python
def route_maker_regime(snapshot: MakerRegimeSnapshot, inventory: MakerRegimeInventory,
                       config: MakerRegimeConfig = MakerRegimeConfig()) -> MakerRegimePolicy:
    ...
```

Return:

```python
@dataclass(frozen=True)
class MakerRegimePolicy:
    regime_id: str
    policy_id: str
    regime_confidence: str
    margin_multiplier: float
    margin_add: float
    clip_multiplier: float
    max_shares_multiplier: float
    min_edge: float
    min_seconds_quote: float
    bid_enabled: bool
    ask_enabled: bool
    exit_only: bool
    risk_action: str
    can_hard_cut: bool
    quote_ttl_ms: int
    reason: str
    warnings: tuple[str, ...]
```

Valid `regime_confidence`: `high`, `medium`, `low`.

Valid `risk_action`: `normal`, `pull_all`, `reduce_only`, `widen`, `observe_only`.

Low confidence widens or disables quotes.

## File Map

- Create: `sniper/algos/polymarket_crypto5m/maker_regime.py` - pure router data types and precedence logic.
- Create: `tests/test_crypto5m_maker_regime.py` - pure router tests.
- Modify later: `scripts/run_polymarket_crypto5m_maker.py` - flag-gated shadow/active integration only after pure router passes.
- Modify later: `sniper/routes/api_crypto5m.py` and `static/app.js` - per-regime reporting after persistence exists.
- Modify later: `tests/test_crypto5m_maker_runner.py` and `tests/test_api_crypto5m.py` - persistence/reporting tests.

## Task Board

- [x] Task 0: Create isolated worktree and baseline focused tests.
- [x] Task 1: Write corrected design/task tracker.
- [x] Task 2: Add pure router tests.
- [x] Task 3: Implement pure `maker_regime.py`.
- [x] Task 4: Add decision/persistence schema tests.
- [x] Task 5: Add decision table and optional regime metadata writer support.
- [x] Task 6: Integrate router/decision logging into runner behind `--maker-regime-router shadow|off|active`.
  - [x] Task 6a: Add stable input hash, effective knob, and side-gate helpers.
  - [x] Task 6b: Add CLI flag and decision logging in `shadow` mode.
  - [x] Task 6c: Add separate shadow `WindowBook` fill path.
- [x] Task 7: Add per-regime analytics/reporting.
- [x] Task 8: Add UI/status surfacing for current/shadow regime.
- [x] Task 9: Run focused and crypto5m test slices.
- [x] Task 10: Commit completed slice.

---

### Task 2: Pure Router Tests

**Files:**
- Create: `tests/test_crypto5m_maker_regime.py`
- Read: `sniper/algos/polymarket_crypto5m/maker_policy.py`

**Interfaces:**
- Consumes: none.
- Produces expected imports:
  - `MakerRegimeSnapshot`
  - `MakerRegimeInventory`
  - `MakerRegimeConfig`
  - `route_maker_regime`

- [x] **Step 1: Write failing tests for precedence and core outputs**

```python
from sniper.algos.polymarket_crypto5m.maker_regime import (
    MakerRegimeConfig,
    MakerRegimeInventory,
    MakerRegimeSnapshot,
    route_maker_regime,
)


def snap(**kw):
    data = {
        "fair": 0.50,
        "best_bid": 0.49,
        "best_ask": 0.51,
        "seconds_left": 180,
        "jump_flag": False,
        "divergence": 1.0,
        "vol_fast": 0.00002,
        "vol_slow": 0.00002,
        "confirmed_side": None,
        "direction_confidence": 0.0,
        "book_stale": False,
        "book_quarantined": False,
        "fee_conflict": False,
    }
    data.update(kw)
    return MakerRegimeSnapshot(**data)


def inv(**kw):
    data = {"token_inventory": 0.0, "opposite_inventory": 0.0, "max_shares": 50.0}
    data.update(kw)
    return MakerRegimeInventory(**data)


def test_hard_invalid_pulls_all_without_hard_cut():
    p = route_maker_regime(snap(fair=None), inv())
    assert p.regime_id == "hard_invalid"
    assert p.risk_action == "pull_all"
    assert p.bid_enabled is False
    assert p.ask_enabled is False
    assert p.can_hard_cut is False


def test_late_window_beats_jump_and_trend():
    p = route_maker_regime(
        snap(seconds_left=18, jump_flag=True, confirmed_side="Up", direction_confidence=0.8),
        inv(token_inventory=10),
    )
    assert p.regime_id == "late_window"
    assert p.exit_only is True
    assert p.bid_enabled is False
    assert p.ask_enabled is True


def test_inventory_saturated_beats_jump_and_disables_more_bids():
    p = route_maker_regime(snap(jump_flag=True), inv(token_inventory=50.0, max_shares=50.0))
    assert p.regime_id == "inventory_saturated"
    assert p.bid_enabled is False
    assert p.ask_enabled is True
    assert p.risk_action == "reduce_only"


def test_extreme_odds_uses_tiny_reduce_only_policy():
    p = route_maker_regime(snap(fair=0.91, best_bid=0.90, best_ask=0.92), inv(token_inventory=8))
    assert p.regime_id == "extreme_odds"
    assert p.bid_enabled is False
    assert p.ask_enabled is True
    assert p.clip_multiplier < 1.0
    assert p.margin_multiplier > 1.0


def test_jump_widens_and_shrinks_clip():
    p = route_maker_regime(snap(jump_flag=True, divergence=2.4), inv())
    assert p.regime_id == "jump"
    assert p.margin_multiplier > 1.0
    assert p.clip_multiplier < 1.0
    assert p.quote_ttl_ms < MakerRegimeConfig().base_quote_ttl_ms


def test_trend_confirmed_is_maker_posture_not_taker_signal():
    p = route_maker_regime(snap(confirmed_side="Up", direction_confidence=0.85), inv())
    assert p.regime_id == "trend_confirmed"
    assert p.risk_action == "widen"
    assert p.can_hard_cut is True


def test_mid_chop_is_tightest_normal_maker_regime():
    p = route_maker_regime(snap(fair=0.51), inv())
    assert p.regime_id == "mid_chop"
    assert p.margin_multiplier <= 1.0
    assert p.bid_enabled is True
    assert p.ask_enabled is True


def test_normal_fallback_for_moderate_non_chop_state():
    p = route_maker_regime(snap(fair=0.68, best_bid=0.66, best_ask=0.70), inv())
    assert p.regime_id == "normal"
    assert p.policy_id == "maker-regime-v1"
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_crypto5m_maker_regime.py -q`

Expected: `ModuleNotFoundError` or import failure for `maker_regime`.

### Task 3: Pure Router Implementation

**Files:**
- Create: `sniper/algos/polymarket_crypto5m/maker_regime.py`
- Test: `tests/test_crypto5m_maker_regime.py`

**Interfaces:**
- Consumes: test API from Task 2.
- Produces: pure routing policy with no I/O.

- [x] **Step 1: Implement dataclasses and first-match routing**

Use frozen dataclasses for snapshot, inventory, config, and policy. Keep all thresholds in config.

- [x] **Step 2: Run pure router tests**

Run: `python3 -m pytest tests/test_crypto5m_maker_regime.py -q`

Expected: all tests pass.

- [x] **Step 3: Run adjacent focused tests**

Run: `python3 -m pytest tests/test_crypto5m_maker_runner.py tests/test_api_crypto5m.py tests/test_crypto5m_maker_regime.py -q`

Expected: all tests pass.

---

## Deferred Tasks

Do not start these until Tasks 2-3 are green:

- Persistence schema: add `maker_regime_decisions`; add regime metadata to quotes/fills without touching v4 data.
- Shadow mode: separate `WindowBook` state for regime router, same tape, same print-only fill model.
- Analytics: per-regime fills, quotes, fill rate, PnL, spread capture, adverse selection, MFE/MAE, hard-cuts, dwell time.
- UI: current regime, shadow-vs-v4 summary, and per-regime disable candidates.

## Progress Log

- 2026-07-04: Created worktree `feat/crypto5m-maker-regime-router`.
- 2026-07-04: Baseline `python3 -m pytest tests/test_crypto5m_maker_runner.py tests/test_api_crypto5m.py -q` passed: 36 tests.
- 2026-07-04: Corrected plan written with shadow-first sequencing and risk fixes.
- 2026-07-04: Added pure router tests; red verified with `ModuleNotFoundError: No module named 'sniper.algos.polymarket_crypto5m.maker_regime'`.
- 2026-07-04: Implemented pure router in `maker_regime.py`; `tests/test_crypto5m_maker_regime.py` passed 11 tests.
- 2026-07-04: Adjacent focused suite passed: `tests/test_crypto5m_maker_runner.py tests/test_api_crypto5m.py tests/test_crypto5m_maker_regime.py` passed 47 tests.
- 2026-07-04: Broader crypto5m suite passed: `tests/test_crypto5m*.py tests/test_api_crypto5m.py tests/test_ui_algo_tabs.py` passed 697 tests.
- 2026-07-04: Added red persistence tests for `maker_regime_decisions` and regime metadata on `maker_quotes`/`maker_fills`; red verified with missing writer/args failures.
- 2026-07-04: Implemented decision table and optional metadata writers; focused persistence tests passed 3 tests and full maker runner tests passed 32 tests.
- 2026-07-04: Persistence slice adjacent suite passed 50 tests; broader crypto5m suite passed 700 tests.
- 2026-07-04: Added regime helper tests; red verified missing helper attributes, then green with 4 helper tests and 36 maker runner tests.
- 2026-07-04: Helper slice adjacent suite passed 54 tests; broader crypto5m suite passed 704 tests.
- 2026-07-04: Added `--maker-regime-router off|shadow|active`; shadow smoke confirmed decision rows are written while v4 quote rows stay unstamped.
- 2026-07-04: Shadow-flag slice passed 38 runner tests, 56 adjacent tests, and 706 broader crypto5m tests.
- 2026-07-04: Added `maker_shadow_windows` and separate shadow book path; red tests verified missing shadow persistence/fill path, then 40 runner tests and 58 adjacent tests passed.
- 2026-07-04: Shadow-book slice broader crypto5m suite passed 708 tests.
- 2026-07-04: Added `/api/crypto5m/maker.regime_shadow` analytics for shadow windows, decisions, quotes, fills, cash delta, and edge value; red API test verified the missing block, then `tests/test_api_crypto5m.py` passed 8 tests.
- 2026-07-04: Added maker-desk Regime shadow panel for shadow campaign/windows/by-regime stats; red UI test verified the missing surface, then `tests/test_ui_algo_tabs.py` passed.
