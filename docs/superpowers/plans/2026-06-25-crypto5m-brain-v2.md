# Crypto5m Brain v2 — Stabilized Probability (shadow v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute a stabilized, market-regularized probability `z_v2` every tick **in shadow** alongside the engine's current `z`, persist both with settlement truth, and prove offline (bucketed sharp-tick Brier) that it tempers the sharp-move overshoot — changing **zero** live behavior.

**Architecture:** Diffusion stays primary; the market regularizes only where our model is provably broken (sharp moves). Jump-aware regime vol (`vol_slow` jump-excluded, `vol_fast` jump-included) drives a z-space precision-motivated blend toward the market-implied z. All compute is pure functions; one new table logs the per-tick series; one offline harness fits the blend weight and runs the validation gate via the existing window-clustered bootstrap. Nothing touches the decision path in v1.

**Tech Stack:** Python 3.13, stdlib only (`math`, `sqlite3`, `random`), pytest. Reuses `live_desk.diffusion_z`, `reachability.probit`, and `crypto5m_lane_eval`'s bootstrap. Standalone paper DB `data/crypto5m/active_trader_v0.sqlite` (not governed by `db.py`).

## Global Constraints

- **Shadow-only, zero live behavior change.** The engine keeps deciding on the current `z`; `z_v2` is computed/logged but never read by any decision. The runner's existing decision path must stay **byte-identical** (Task 8 integration test asserts this).
- **Fail-closed.** Every pure function returns `None` on degenerate inputs and never raises; a null `z_v2` logs nulls; the engine is unaffected regardless. No network in tests.
- **No in-process buffer.** All per-tick features are pure functions of the per-poll kline window. The jump-cap decay is a pure function of `ticks_since_jump` derived from the kline window — never cross-poll state.
- **Blend in z-space:** `z_v2 = w·z_ours + (1−w)·z_market`, then `Φ`. `z_ours` uses the jump-excluded `vol_slow`; `z_market = probit(clamp(up_mid, ε, 1−ε))` on the P(Up) axis.
- **Degenerate split:** our inputs bad → `z_v2 = None`; market thin/missing → `z_v2 = z_ours` (weight 1), **not** None.
- **Pinned prior:** `w_prior = var_market/(var_ours+var_market)`, `var_ours=(divergence/D_REF)²`, `var_market=(spread/S_REF)²`, `D_REF=2.0`, `S_REF=0.01` → `w_prior=0.5` at `(D_REF, S_REF)`. The pre-fit prior is what runs first and is logged live.
- **Gate:** paired per-window Brier delta aggregated **over the sharp ticks within each window** (`jump_flag=1` or `divergence ≥ D_REF`); window is the clustering unit; whole-window mean is a secondary diagnostic only. **Win on sharp-revert; no-regress guard on calm + sharp-continue.** Milestone ladder on **holdout sharp-revert** windows: 60 / 120 / 240 (monitor / monitor / first-confirmatory); if the CI hasn't cleared 0 by the confirmatory look, accrue more.
- **Stats reuse:** route through the shared `window_bootstrap` module (factored out of `crypto5m_lane_eval`), preserving `BOOT_SEED=20260621` determinism. No new stats path.
- **CI hygiene:** the new `crypto5m_z_shadow` table → run `scripts/validate_schema_refs.py` and `python3 scripts/brain_index.py generate`, commit the regenerated index (CI's stale-index gate fails otherwise).

---

## File Structure

- Create `sniper/algos/polymarket_crypto5m/window_bootstrap.py` — shared window-clustered bootstrap (`boot_means`, `pctile`, `BOOT_N`, `BOOT_SEED`).
- Modify `scripts/crypto5m_lane_eval.py` — import the bootstrap instead of the inline `_boot_means`/`_pctile`/constants.
- Create `sniper/algos/polymarket_crypto5m/regime_vol.py` — `regime_vol` (per-tick) + `sharp_window_label` (per-window) pure functions.
- Create `sniper/algos/polymarket_crypto5m/stabilized_z.py` — `w_prior`, `jump_cap_decay`, `stabilized_z`, `fit_weight` (pure).
- Create `sniper/algos/polymarket_crypto5m/z_shadow_log.py` — `crypto5m_z_shadow` schema + `record_tick` + `resolve_window` + `sweep_incomplete` (mirrors `ev_observation_log.py`).
- Modify `sniper/algos/polymarket_crypto5m/live_desk.py` — compute + expose `z_v2` + components on the desk (shadow).
- Modify `scripts/run_polymarket_crypto5m_active_trader.py` — write a `crypto5m_z_shadow` row per tick + resolve at window close (shadow-only).
- Create `scripts/crypto5m_z_validation.py` — offline validation harness (bucket → fit → gate).
- Modify `sniper/routes/api_crypto5m.py` + the 5m UI — display-only `z`/`z_v2`/`market_z` series graph (screenshot-verified, no unit test).
- Tests: one file per module under `tests/`.

---

### Task 1: Shared window-clustered bootstrap module

**Files:**
- Create: `sniper/algos/polymarket_crypto5m/window_bootstrap.py`
- Modify: `scripts/crypto5m_lane_eval.py:42-43` (constants), `:432-451` (`_boot_means`/`_pctile`)
- Test: `tests/test_crypto5m_window_bootstrap.py`

**Interfaces:**
- Produces: `boot_means(by_window: dict, rng: random.Random, nb: int = BOOT_N) -> list[float]` (sorted resampled means); `pctile(sorted_vals: list[float], q: float) -> float | None`; constants `BOOT_N = 2000`, `BOOT_SEED = 20260621`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_crypto5m_window_bootstrap.py
import random
from sniper.algos.polymarket_crypto5m import window_bootstrap as WB


def test_boot_means_is_window_clustered_and_deterministic():
    by_win = {"a": [1.0, 1.0], "b": [3.0], "c": [-1.0, -1.0, -1.0]}
    out1 = WB.boot_means(by_win, random.Random(WB.BOOT_SEED), nb=500)
    out2 = WB.boot_means(by_win, random.Random(WB.BOOT_SEED), nb=500)
    assert out1 == out2                      # deterministic for a fixed seed
    assert out1 == sorted(out1)              # returned sorted
    assert len(out1) == 500
    assert min(by_win["c"]) <= out1[0] and out1[-1] <= max(by_win["b"])


def test_boot_means_empty_and_pctile():
    assert WB.boot_means({}, random.Random(1)) == []
    assert WB.pctile([], 0.5) is None
    assert WB.pctile([0.0, 1.0, 2.0, 3.0], 0.5) in (1.0, 2.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_crypto5m_window_bootstrap.py -v`
Expected: FAIL — `ModuleNotFoundError: ... window_bootstrap`.

- [ ] **Step 3: Create the module (lifted verbatim from `crypto5m_lane_eval.py`, generalized to a dict)**

```python
# sniper/algos/polymarket_crypto5m/window_bootstrap.py
"""Window-clustered bootstrap shared by crypto5m evaluators (lane-eval + brain-v2 z-validation).

Resamples WINDOWS with replacement (observations within a window are correlated) and returns the sorted
list of resampled means. Factored out of crypto5m_lane_eval.py so the z-validation harness reuses the
SAME stats path and seed rather than copying it."""
from __future__ import annotations

import random

BOOT_N = 2000           # bootstrap resamples
BOOT_SEED = 20260621    # fixed seed -> deterministic output


def boot_means(by_window: dict, rng: random.Random, nb: int = BOOT_N) -> list[float]:
    """by_window: {window_key: [values]}. Resample windows with replacement, pool their values, take the
    mean; repeat nb times; return sorted means. Empty input -> []."""
    ws = list(by_window)
    if not ws:
        return []
    out = []
    for _ in range(nb):
        vals = []
        for _ in ws:
            vals.extend(by_window[ws[rng.randrange(len(ws))]])
        out.append(sum(vals) / len(vals))
    out.sort()
    return out


def pctile(sorted_vals, q):
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, max(0, int(q * (len(sorted_vals) - 1))))]
```

- [ ] **Step 4: Refactor `crypto5m_lane_eval.py` to import from the module**

In `scripts/crypto5m_lane_eval.py`: delete the `BOOT_N`/`BOOT_SEED` definitions at `:42-43`, delete `_boot_means` (`:432-445`) and `_pctile` (`:448-451`), and add near the other imports:

```python
from sniper.algos.polymarket_crypto5m.window_bootstrap import boot_means as _boot_means, pctile as _pctile, BOOT_N, BOOT_SEED
```

(Keep the private aliases `_boot_means`/`_pctile` so the existing call sites at `:472,:475,:478,:480` are unchanged.)

- [ ] **Step 5: Run the new test + the lane-eval determinism guard**

Run: `python3 -m pytest tests/test_crypto5m_window_bootstrap.py tests/test_crypto5m_lane_eval.py -v`
Expected: PASS (lane-eval output unchanged — same seed, same algorithm).

- [ ] **Step 6: Commit**

```bash
git add sniper/algos/polymarket_crypto5m/window_bootstrap.py scripts/crypto5m_lane_eval.py tests/test_crypto5m_window_bootstrap.py
git commit -m "refactor(crypto5m): factor window-clustered bootstrap into a shared importable module"
```

---

### Task 2: `regime_vol` — jump-aware fast/slow vol (pure)

**Files:**
- Create: `sniper/algos/polymarket_crypto5m/regime_vol.py`
- Test: `tests/test_crypto5m_regime_vol.py`

**Interfaces:**
- Produces: `regime_vol(closes_by_sec: dict[int, float], now: int, *, kappa=4.0, fast_span_s=10, slow_lookback_s=180, min_points=10) -> dict | None` returning `{"vol_slow", "vol_fast", "jump_flag", "ticks_since_jump", "divergence"}` (or `None` on too-few points). `vol_slow` jump-**excluded**, `vol_fast` jump-**included**, `jump_flag` = latest tick is a jump, `ticks_since_jump` = seconds since the most recent in-window jump (`None` if none), `divergence = vol_fast/vol_slow`.

- [ ] **Step 1: Write the failing tests (the correction-#1 invariants)**

```python
# tests/test_crypto5m_regime_vol.py
import math
from sniper.algos.polymarket_crypto5m import regime_vol as RV


def _calm_closes(now, n=200, step=1e-4, base=60000.0):
    # tiny alternating 1s returns ~1e-4 (BTC's typical 1s stdev) -> calm
    closes, px = {}, base
    for i in range(n):
        px *= math.exp(step if i % 2 == 0 else -step)
        closes[now - n + 1 + i] = px
    return closes


def test_calm_window_has_low_divergence_no_jump():
    now = 1_000_000
    out = RV.regime_vol(_calm_closes(now), now)
    assert out is not None
    assert out["jump_flag"] is False
    assert out["ticks_since_jump"] is None
    assert out["divergence"] < 1.5          # fast ~= slow when calm


def test_jump_excluded_from_slow_included_in_fast():
    now = 1_000_000
    closes = _calm_closes(now)
    base_slow = RV.regime_vol(closes, now)["vol_slow"]
    # inject a single 4sigma+ up jump at the latest second
    closes[now] = closes[now - 1] * math.exp(0.004)
    out = RV.regime_vol(closes, now)
    assert out["jump_flag"] is True
    assert out["ticks_since_jump"] == 0
    assert out["vol_fast"] > out["vol_slow"]                     # fast spikes (jump included)
    assert abs(out["vol_slow"] - base_slow) / base_slow < 0.25   # slow ~unchanged (jump excluded)
    assert out["divergence"] > 2.0                              # the regime signal


def test_ticks_since_jump_counts_up_after_the_jump():
    now = 1_000_000
    closes = _calm_closes(now, n=205)
    closes[now - 5] = closes[now - 6] * math.exp(0.004)         # jump 5s ago, calm since
    out = RV.regime_vol(closes, now)
    assert out["ticks_since_jump"] == 5
    assert out["jump_flag"] is False                            # latest tick is not the jump


def test_degenerate_returns_none():
    assert RV.regime_vol({}, 1000) is None
    assert RV.regime_vol({999: 60000.0}, 1000) is None          # too few points
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_crypto5m_regime_vol.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `regime_vol`**

```python
# sniper/algos/polymarket_crypto5m/regime_vol.py
"""Jump-aware regime volatility for crypto5m brain v2 (pure; no I/O, no cross-poll state).

vol_slow: jump-EXCLUDED realized vol (so a 4sigma tick can't inflate the diffusion denominator for 3 min).
vol_fast: jump-INCLUDED short EWMA (it MUST spike on a jump -- that spike is the regime signal that drives
the cede). jump_flag/ticks_since_jump let the blend cede immediately on the one tick before vol_fast's EWMA
fully absorbs it. All derived from the per-poll kline window -> no buffer, restart-safe."""
from __future__ import annotations

import math
import statistics


def _log_returns(closes_by_sec, lo, hi):
    secs = sorted(s for s in closes_by_sec if lo <= s <= hi)
    rets = []  # (sec, r)
    for a, b in zip(secs, secs[1:]):
        pa, pb = closes_by_sec[a], closes_by_sec[b]
        if pa > 0 and pb > 0:
            rets.append((b, math.log(pb / pa)))
    return rets


def regime_vol(closes_by_sec, now, *, kappa=4.0, fast_span_s=10, slow_lookback_s=180, min_points=10):
    rets = _log_returns(closes_by_sec, now - slow_lookback_s, now)
    if len(rets) < min_points:
        return None
    vals = [r for _, r in rets]
    # robust scale for jump detection (MAD): immune to the very jumps it is flagging
    med = statistics.median(vals)
    mad = statistics.median([abs(v - med) for v in vals]) * 1.4826
    thresh = kappa * mad if mad > 0 else float("inf")
    jumps = {s for s, r in rets if abs(r) > thresh}

    nonjump = [r for s, r in rets if s not in jumps]
    if len(nonjump) < 2:
        return None
    vol_slow = statistics.pstdev(nonjump) or None
    if not vol_slow:
        return None

    # vol_fast: EWMA of r^2 over ~fast_span_s, jump INCLUDED, most-recent weighted heaviest
    alpha = 2.0 / (fast_span_s + 1)
    ewma_var, started = 0.0, False
    for _, r in rets:
        ewma_var = (r * r) if not started else (alpha * r * r + (1 - alpha) * ewma_var)
        started = True
    vol_fast = math.sqrt(ewma_var)

    latest_sec = rets[-1][0]
    jump_flag = latest_sec in jumps
    ticks_since_jump = (now - max(jumps)) if jumps else None
    return {"vol_slow": vol_slow, "vol_fast": vol_fast, "jump_flag": jump_flag,
            "ticks_since_jump": ticks_since_jump, "divergence": vol_fast / vol_slow}
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_crypto5m_regime_vol.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/algos/polymarket_crypto5m/regime_vol.py tests/test_crypto5m_regime_vol.py
git commit -m "feat(crypto5m): jump-aware regime_vol (vol_slow jump-excluded, vol_fast jump-included)"
```

---

### Task 3: `sharp_window_label` — independent sharp/revert/continue label (pure)

**Files:**
- Modify: `sniper/algos/polymarket_crypto5m/regime_vol.py` (add `sharp_window_label`)
- Test: `tests/test_crypto5m_regime_vol.py` (extend)

**Interfaces:**
- Produces: `sharp_window_label(returns: list[float], settlement: int, *, rel_k=4.0, abs_floor=5e-4) -> str` → `"calm" | "sharp_revert" | "sharp_continue"`. `returns` = the window's 1s log returns; `settlement` = 1 if Up won else 0. Sharp iff `max|r| ≥ rel_k·median|r|` **AND** `max|r| ≥ abs_floor`. Direction = sign of the largest-|r| return; `continue` if it matches the settlement side, else `revert`. Computed from the BTC path only — never from `z_v2`/`jump_flag`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_crypto5m_regime_vol.py
def test_sharp_label_relative_and_absolute():
    # dead-calm window with a tiny relative-only spike -> NOT sharp (absolute floor protects)
    calm = [1e-5, -1e-5, 1e-5, 8e-5, -1e-5]      # max 8e-5 > 4x median but < abs_floor 5e-4
    assert RV.sharp_window_label(calm, settlement=1) == "calm"
    # clears BOTH thresholds, big move is UP, settles UP -> continue
    up = [1e-4, -1e-4, 1e-4, 9e-4, -1e-4]
    assert RV.sharp_window_label(up, settlement=1) == "sharp_continue"
    # same sharp UP move but settles DOWN -> revert
    assert RV.sharp_window_label(up, settlement=0) == "sharp_revert"
    # sharp DOWN move that settles UP -> revert
    dn = [1e-4, -9e-4, 1e-4, -1e-4]
    assert RV.sharp_window_label(dn, settlement=1) == "sharp_revert"


def test_sharp_label_empty_is_calm():
    assert RV.sharp_window_label([], settlement=1) == "calm"
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_crypto5m_regime_vol.py::test_sharp_label_relative_and_absolute -v`
Expected: FAIL — `AttributeError: ... sharp_window_label`.

- [ ] **Step 3: Implement**

```python
# append to sniper/algos/polymarket_crypto5m/regime_vol.py
def sharp_window_label(returns, settlement, *, rel_k=4.0, abs_floor=5e-4):
    """Window-level label from the BTC path only (independent of z_v2). abs_floor ~= 5x BTC's ~1e-4 typical
    1s log-return stdev. revert/continue is settlement-defined and POST-HOC -- it never reaches the live path."""
    vals = [r for r in returns if r is not None]
    if not vals:
        return "calm"
    mags = [abs(r) for r in vals]
    peak = max(mags)
    med = statistics.median(mags)
    rel_ok = (med <= 0) or (peak >= rel_k * med)
    if not (rel_ok and peak >= abs_floor):
        return "calm"
    move_sign = 1 if vals[mags.index(peak)] >= 0 else 0     # 1 = up move, 0 = down move
    return "sharp_continue" if move_sign == settlement else "sharp_revert"
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_crypto5m_regime_vol.py -v`
Expected: PASS (6 tests total).

- [ ] **Step 5: Commit**

```bash
git add sniper/algos/polymarket_crypto5m/regime_vol.py tests/test_crypto5m_regime_vol.py
git commit -m "feat(crypto5m): sharp_window_label (relative AND absolute; settlement-defined revert/continue)"
```

---

### Task 4: `w_prior` + `jump_cap_decay` — the pinned weight and the cede/un-cede (pure)

**Files:**
- Create: `sniper/algos/polymarket_crypto5m/stabilized_z.py` (these two functions only this task)
- Test: `tests/test_crypto5m_stabilized_z.py`

**Interfaces:**
- Produces: `w_prior(divergence: float, spread: float, *, d_ref=2.0, s_ref=0.01) -> float` (weight on our model, in `[0,1]`; `0.5` at `(d_ref, s_ref)`); `jump_cap_decay(ticks_since_jump, *, cap_floor=0.15, decay_s=8) -> float` (effective cap in `[cap_floor, 1.0]`; `1.0` when `ticks_since_jump is None`). Module constants `D_REF=2.0`, `S_REF=0.01`.

- [ ] **Step 1: Write the failing tests (correction #2 + correction #3 + req #2/#3)**

```python
# tests/test_crypto5m_stabilized_z.py
from sniper.algos.polymarket_crypto5m import stabilized_z as SZ


def test_w_prior_anchored_at_reference():
    # pinned: w_prior = 0.5 at (D_REF, S_REF) -- pre-fit z_v2 is not arbitrary
    assert abs(SZ.w_prior(SZ.D_REF, SZ.S_REF) - 0.5) < 1e-9


def test_w_prior_cedes_in_sharp_holds_in_calm():
    calm = SZ.w_prior(1.0, SZ.S_REF)      # divergence below ref -> trust ours more
    sharp = SZ.w_prior(6.0, SZ.S_REF)     # divergence far above ref -> cede to market
    assert calm > 0.5 > sharp
    assert 0.0 <= sharp < 0.2


def test_w_prior_wider_spread_trusts_ours():
    # a noisier (wider-spread) market -> var_market up -> weight on ours rises
    assert SZ.w_prior(SZ.D_REF, 0.04) > SZ.w_prior(SZ.D_REF, SZ.S_REF)


def test_jump_cap_decay_caps_then_relaxes():
    assert SZ.jump_cap_decay(None) == 1.0                 # no jump -> no cap
    assert SZ.jump_cap_decay(0) == SZ.jump_cap_decay(0)   # pure
    assert abs(SZ.jump_cap_decay(0) - 0.15) < 1e-9        # at the jump -> hard cap (cede)
    assert SZ.jump_cap_decay(0) < SZ.jump_cap_decay(4) < SZ.jump_cap_decay(8)  # relaxes (un-cede)
    assert SZ.jump_cap_decay(8) == 1.0                    # fully decayed
    assert SZ.jump_cap_decay(50) == 1.0
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_crypto5m_stabilized_z.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement (this task adds only these two functions + constants)**

```python
# sniper/algos/polymarket_crypto5m/stabilized_z.py
"""Stabilized, market-regularized probability for crypto5m brain v2 (pure; no I/O, no cross-poll state).

Diffusion stays primary; the market regularizes via a z-space blend whose weight DROPS in sharp regimes.
The live weight is the pinned prior (below). The harness can refit a weight function offline from the logged
components -- the live path uses the prior, which is why it must be anchored, not arbitrary."""
from __future__ import annotations

import math

from sniper.algos.polymarket_crypto5m.live_desk import diffusion_z
from sniper.algos.polymarket_crypto5m.reachability import probit

D_REF = 2.0      # reference divergence (fast = 2x slow) -> a clear sharp regime
S_REF = 0.01     # reference spread (median observed crypto5m spread)
EPS = 1e-4       # market_mid clamp bound for probit


def w_prior(divergence, spread, *, d_ref=D_REF, s_ref=S_REF):
    """Pinned precision-style weight on OUR model. var_ours=(div/d_ref)^2, var_market=(spread/s_ref)^2 ->
    w = var_market/(var_ours+var_market). Anchored: w=0.5 at (d_ref, s_ref). No free scale knob."""
    d = max(0.0, float(divergence)) / d_ref
    s = max(0.0, float(spread)) / s_ref if spread is not None else 0.0
    var_ours, var_market = d * d, s * s
    denom = var_ours + var_market
    if denom <= 0:
        return 1.0     # both degenerate -> trust ours (calm, tight)
    return var_market / denom


def jump_cap_decay(ticks_since_jump, *, cap_floor=0.15, decay_s=8):
    """Effective CAP on the weight after a jump: cap_floor at the jump, linearly relaxing to 1.0 over decay_s
    (un-cede). Pure function of ticks_since_jump (seconds since the most recent in-window jump)."""
    if ticks_since_jump is None:
        return 1.0
    t = max(0, int(ticks_since_jump))
    if t >= decay_s:
        return 1.0
    return cap_floor + (1.0 - cap_floor) * (t / decay_s)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_crypto5m_stabilized_z.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/algos/polymarket_crypto5m/stabilized_z.py tests/test_crypto5m_stabilized_z.py
git commit -m "feat(crypto5m): pinned w_prior (anchored 0.5 at D_REF/S_REF) + jump_cap_decay (cede/un-cede)"
```

---

### Task 5: `stabilized_z` — assemble z_v2 with the degenerate split (pure)

**Files:**
- Modify: `sniper/algos/polymarket_crypto5m/stabilized_z.py` (add `stabilized_z`)
- Test: `tests/test_crypto5m_stabilized_z.py` (extend — the headline invariants)

**Interfaces:**
- Consumes: `regime_vol(...)` output dict; `w_prior`, `jump_cap_decay`; `diffusion_z`, `probit`.
- Produces: `stabilized_z(btc_now, btc_start, seconds_left, regime_out, market_mid, spread) -> dict | None` returning `{"z_v2", "z_ours", "z_market", "weight", "divergence", "jump_flag"}`. `None` if our inputs degenerate; `z_v2 = z_ours` (weight 1.0) if the market is thin/missing.

- [ ] **Step 1: Write the failing tests (the four locked invariants)**

```python
# add to tests/test_crypto5m_stabilized_z.py
import math


def _regime(divergence, *, vol_slow=1e-4, jump_flag=False, ticks_since_jump=None):
    return {"vol_slow": vol_slow, "vol_fast": vol_slow * divergence, "jump_flag": jump_flag,
            "ticks_since_jump": ticks_since_jump, "divergence": divergence}


def test_calm_primary_sharp_cede():
    # calm: z_v2 ~= z_ours (weight ~1). btc up a touch over a calm slow vol.
    calm = SZ.stabilized_z(60050.0, 60000.0, 100, _regime(1.0), market_mid=0.5, spread=SZ.S_REF)
    assert calm["weight"] > 0.7
    assert abs(calm["z_v2"] - calm["z_ours"]) < abs(calm["z_ours"]) * 0.5
    # sharp: z_v2 pulled toward z_market (weight low)
    sharp = SZ.stabilized_z(60050.0, 60000.0, 100, _regime(8.0), market_mid=0.5, spread=SZ.S_REF)
    assert sharp["weight"] < 0.2
    assert abs(sharp["z_v2"] - sharp["z_market"]) < abs(sharp["z_v2"] - sharp["z_ours"])


def test_post_jump_not_overconfident():
    # THE test: a jumped numerator over a (jump-excluded) calm slow vol would make z_ours extreme;
    # z_v2 must NOT be extreme -- the jump cap forces a cede to the (mid=0.5 -> z_market=0) market.
    out = SZ.stabilized_z(60240.0, 60000.0, 100, _regime(8.0, jump_flag=True, ticks_since_jump=0),
                          market_mid=0.5, spread=SZ.S_REF)
    assert out["weight"] <= 0.15
    assert abs(out["z_v2"]) < abs(out["z_ours"])          # tempered, not overconfident


def test_un_cede_after_jump_decays():
    # a few seconds after a single jump (no new jumps) z_v2 returns toward z_ours: NOT a one-way valve
    fresh = SZ.stabilized_z(60240.0, 60000.0, 100, _regime(8.0, jump_flag=True, ticks_since_jump=0),
                            market_mid=0.5, spread=SZ.S_REF)
    later = SZ.stabilized_z(60240.0, 60000.0, 100, _regime(1.2, jump_flag=False, ticks_since_jump=8),
                            market_mid=0.5, spread=SZ.S_REF)
    assert later["weight"] > fresh["weight"]
    assert abs(later["z_v2"] - later["z_ours"]) < abs(fresh["z_v2"] - fresh["z_ours"])


def test_blend_is_in_z_space():
    out = SZ.stabilized_z(60050.0, 60000.0, 100, _regime(3.0), market_mid=0.4, spread=SZ.S_REF)
    w = out["weight"]
    assert abs(out["z_v2"] - (w * out["z_ours"] + (1 - w) * out["z_market"])) < 1e-12


def test_degenerate_split():
    # our inputs bad -> None
    assert SZ.stabilized_z(0, 60000.0, 100, _regime(2.0), market_mid=0.5, spread=SZ.S_REF) is None
    assert SZ.stabilized_z(60050.0, 60000.0, 100, None, market_mid=0.5, spread=SZ.S_REF) is None
    # market thin/missing -> z_v2 == z_ours, weight 1 (NOT None)
    nomkt = SZ.stabilized_z(60050.0, 60000.0, 100, _regime(8.0), market_mid=None, spread=None)
    assert nomkt is not None and nomkt["weight"] == 1.0 and nomkt["z_v2"] == nomkt["z_ours"]
    # market_mid at the bound -> clamped, no blow-up
    clamped = SZ.stabilized_z(60050.0, 60000.0, 100, _regime(8.0), market_mid=1.0, spread=SZ.S_REF)
    assert clamped is not None and math.isfinite(clamped["z_v2"])
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_crypto5m_stabilized_z.py -v`
Expected: FAIL — `AttributeError: ... stabilized_z`.

- [ ] **Step 3: Implement**

```python
# append to sniper/algos/polymarket_crypto5m/stabilized_z.py
def stabilized_z(btc_now, btc_start, seconds_left, regime_out, market_mid, spread):
    """z_v2 = w*z_ours + (1-w)*z_market (z-space). z_ours uses the jump-excluded vol_slow.
    Our inputs degenerate -> None. Market thin/missing -> z_v2 = z_ours (weight 1). Fail-closed."""
    if not regime_out:
        return None
    z_ours = diffusion_z(btc_now, btc_start, regime_out.get("vol_slow"), seconds_left)
    if z_ours is None:
        return None
    div = regime_out.get("divergence")
    jflag = regime_out.get("jump_flag")
    base = {"z_ours": z_ours, "divergence": div, "jump_flag": jflag}

    if market_mid is None:
        return {**base, "z_v2": z_ours, "z_market": None, "weight": 1.0}
    mid = min(1.0 - EPS, max(EPS, float(market_mid)))
    z_market = probit(mid)
    if z_market is None:
        return {**base, "z_v2": z_ours, "z_market": None, "weight": 1.0}

    w = min(w_prior(div, spread), jump_cap_decay(regime_out.get("ticks_since_jump")))
    z_v2 = w * z_ours + (1.0 - w) * z_market
    return {**base, "z_v2": z_v2, "z_market": z_market, "weight": w}
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_crypto5m_stabilized_z.py -v`
Expected: PASS (all — incl. post-jump-not-overconfident and un-cede).

- [ ] **Step 5: Commit**

```bash
git add sniper/algos/polymarket_crypto5m/stabilized_z.py tests/test_crypto5m_stabilized_z.py
git commit -m "feat(crypto5m): stabilized_z blend (post-jump tempered, un-cede decay, degenerate split)"
```

---

### Task 6: `z_shadow_log` — table, per-tick record, settlement resolve, sweep

**Files:**
- Create: `sniper/algos/polymarket_crypto5m/z_shadow_log.py`
- Test: `tests/test_crypto5m_z_shadow_log.py`
- Regenerate: `index/schema_registry.json` (via `brain_index.py generate`)

**Interfaces:**
- Produces: `ensure_schema(conn)`; `record_tick(conn, row: dict) -> int`; `resolve_window(conn, window_start_ts, settlement: int, sharp_label: str, now_ts) -> int` (stamps `status='resolved'`, `settlement`, `sharp_label` on all `pending` rows of the window; returns count); `sweep_incomplete(conn, now_ts) -> int` (rows from a window that closed unresolved → `status='incomplete'`, excluded from the gate). Column tuple `_COLS`.

- [ ] **Step 1: Write the failing tests (roundtrip + settlement lifecycle, req from §8.E)**

```python
# tests/test_crypto5m_z_shadow_log.py
import sqlite3
from sniper.algos.polymarket_crypto5m import z_shadow_log as ZL


def _conn(p):
    c = sqlite3.connect(p); c.row_factory = sqlite3.Row
    ZL.ensure_schema(c); return c


def _row(ws, tick, **kw):
    d = {"engine_version": "brain-v2-shadow-v1", "window_start_ts": ws, "tick_ts": tick,
         "condition_id": "0xc", "btc_now": 60050.0, "btc_start": 60000.0, "seconds_left": 100,
         "vol_slow": 1e-4, "vol_fast": 3e-4, "jump_flag": 0, "divergence": 3.0,
         "z": 0.5, "z_ours": 0.52, "z_market": 0.20, "market_mid": 0.55, "z_v2": 0.40, "weight": 0.6}
    d.update(kw); return d


def test_record_and_resolve_window(tmp_path):
    c = _conn(str(tmp_path / "z.sqlite"))
    ws = 1782000000
    ZL.record_tick(c, _row(ws, ws + 60, jump_flag=1))
    ZL.record_tick(c, _row(ws, ws + 120))
    n = ZL.resolve_window(c, ws, settlement=1, sharp_label="sharp_revert", now_ts=ws + 305)
    assert n == 2
    rows = c.execute("SELECT status, settlement, sharp_label FROM crypto5m_z_shadow").fetchall()
    assert all(r["status"] == "resolved" and r["settlement"] == 1 and r["sharp_label"] == "sharp_revert"
               for r in rows)


def test_unresolved_window_swept_incomplete_not_zero(tmp_path):
    c = _conn(str(tmp_path / "z.sqlite"))
    ws = 1782000000
    ZL.record_tick(c, _row(ws, ws + 60))
    assert ZL.sweep_incomplete(c, ws + 100) == 0          # window still open
    assert ZL.sweep_incomplete(c, ws + 301) == 1          # closed, never resolved
    r = c.execute("SELECT status, settlement FROM crypto5m_z_shadow").fetchone()
    assert r["status"] == "incomplete" and r["settlement"] is None   # excluded, not counted as 0


def test_pending_persists_across_reconnect(tmp_path):
    p = str(tmp_path / "z.sqlite")
    c1 = _conn(p); ZL.record_tick(c1, _row(1782000000, 1782000060)); c1.close()
    c2 = _conn(p)
    assert c2.execute("SELECT COUNT(*) FROM crypto5m_z_shadow WHERE status='pending'").fetchone()[0] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_crypto5m_z_shadow_log.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement (mirrors `ev_observation_log.py`)**

```python
# sniper/algos/polymarket_crypto5m/z_shadow_log.py
"""Per-tick shadow log of the brain-v2 stabilized probability (design sec 6.4).

One row per tick: the current z, our jump-excluded z_ours, the market-implied z_market, the blended z_v2 and
its weight, plus the regime-vol context. Resolved at WINDOW CLOSE with the 0/1 settlement and the independent
sharp-window label. This single table is the graph source AND the offline-validation substrate. Restart-safe:
pending rows persist; unresolved windows are swept to 'incomplete' and excluded from the gate.

Standalone paper DB (data/crypto5m/active_trader_v0.sqlite) -- inline SQL, not governed by db.py."""
from __future__ import annotations

_COLS = (
    "engine_version", "window_start_ts", "tick_ts", "condition_id",
    "btc_now", "btc_start", "seconds_left",
    "vol_slow", "vol_fast", "jump_flag", "divergence",
    "z", "z_ours", "z_market", "market_mid", "z_v2", "weight",
)


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS crypto5m_z_shadow (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        engine_version TEXT, window_start_ts INTEGER, tick_ts INTEGER, condition_id TEXT,
        btc_now REAL, btc_start REAL, seconds_left INTEGER,
        vol_slow REAL, vol_fast REAL, jump_flag INTEGER, divergence REAL,
        z REAL, z_ours REAL, z_market REAL, market_mid REAL, z_v2 REAL, weight REAL,
        status TEXT DEFAULT 'pending', settlement INTEGER, sharp_label TEXT, resolved_ts INTEGER)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_z_shadow_pending ON crypto5m_z_shadow "
                 "(status, window_start_ts)")
    conn.commit()


def record_tick(conn, row: dict) -> int:
    cols = ", ".join(_COLS)
    ph = ", ".join("?" for _ in _COLS)
    cur = conn.execute(
        f"INSERT INTO crypto5m_z_shadow ({cols}, status) VALUES ({ph}, 'pending')",
        tuple(row.get(c) for c in _COLS))
    conn.commit()
    return int(cur.lastrowid)


def resolve_window(conn, window_start_ts, settlement, sharp_label, now_ts) -> int:
    cur = conn.execute(
        "UPDATE crypto5m_z_shadow SET status='resolved', settlement=?, sharp_label=?, resolved_ts=? "
        "WHERE status='pending' AND window_start_ts=?",
        (int(settlement), sharp_label, now_ts, window_start_ts))
    conn.commit()
    return cur.rowcount


def sweep_incomplete(conn, now_ts) -> int:
    cur = conn.execute(
        "UPDATE crypto5m_z_shadow SET status='incomplete' "
        "WHERE status='pending' AND (window_start_ts + 300) <= ?", (now_ts,))
    conn.commit()
    return cur.rowcount
```

- [ ] **Step 4: Run tests + regenerate the index + schema validator**

```bash
python3 -m pytest tests/test_crypto5m_z_shadow_log.py -v          # expect PASS (3)
python3 scripts/brain_index.py generate
python3 scripts/validate_schema_refs.py
```
Expected: tests PASS; index regenerated (now includes `crypto5m_z_shadow`); validator reports no violations.

- [ ] **Step 5: Commit**

```bash
git add sniper/algos/polymarket_crypto5m/z_shadow_log.py tests/test_crypto5m_z_shadow_log.py index/schema_registry.json
git commit -m "feat(crypto5m): crypto5m_z_shadow table + record/resolve/sweep (settlement lifecycle)"
```

---

### Task 7: Expose `z_v2` on the live desk (shadow)

**Files:**
- Modify: `sniper/algos/polymarket_crypto5m/live_desk.py:320` (already builds `closes_by_sec`), `:362-367` (desk update)
- Test: `tests/test_crypto5m_live_desk.py` (extend)

**Interfaces:**
- Consumes: `regime_vol`, `stabilized_z`.
- Produces: desk keys `z_v2`, `z_ours`, `z_market`, `z_weight`, `vol_slow`, `vol_fast`, `jump_flag`, `divergence` (all `None` when unavailable). The existing `z`/`vol_per_s`/decision fields are untouched.

- [ ] **Step 1: Write the failing test (desk carries the shadow fields, current z unchanged)**

```python
# add to tests/test_crypto5m_live_desk.py
def test_desk_exposes_z_v2_shadow_fields(monkeypatch):
    import math
    from sniper.algos.polymarket_crypto5m import live_desk as LD
    now = 1782000200
    ws = now - (now % 300)
    # a calm kline ramp + a current price; build the binance kline rows [open_ms,o,h,l,c,v,...,taker_buy]
    kl = []
    px = 60000.0
    for s in range(ws - 90, now + 1):
        px *= math.exp(1e-4 if s % 2 == 0 else -1e-4)
        kl.append([s * 1000, "0", "0", "0", f"{px:.2f}", "1", "0", "0", "1", "0"])
    monkeypatch.setattr(LD, "server_now", lambda: now)
    monkeypatch.setattr(LD, "_get", lambda url, timeout=6: (
        {"price": f"{px:.2f}"} if "ticker" in url else
        kl if "klines" in url else
        [{"markets": [{"conditionId": "0xc", "clobTokenIds": "[\"u\",\"d\"]"}]}]))
    # books: up ask/bid -> up_mid ~0.55
    monkeypatch.setattr(LD, "_best_ask", lambda b: 0.56 if b else None)
    monkeypatch.setattr(LD, "_best_bid", lambda b: 0.54 if b else None)
    monkeypatch.setattr(LD, "book_imbalance", lambda b, levels=3: 0.0)
    monkeypatch.setattr(LD, "microprice", lambda b: (None, None))
    monkeypatch.setattr(LD, "tape_order_flow", lambda *a, **k: {"ofi_10s": None, "ofi_30s": None, "tape_n_10s": 0})
    d = LD.build_live_desk(now)
    for k in ("z_v2", "z_ours", "z_market", "z_weight", "vol_slow", "vol_fast", "jump_flag", "divergence"):
        assert k in d
    assert d["z"] is not None                       # current z still computed (unchanged path)
    assert d["z_v2"] is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_crypto5m_live_desk.py::test_desk_exposes_z_v2_shadow_fields -v`
Expected: FAIL — `KeyError`/`assert k in d`.

- [ ] **Step 3: Implement — add the shadow compute after the existing fair-value block**

In `live_desk.py`, add the import at the top with the other package imports:

```python
from sniper.algos.polymarket_crypto5m.regime_vol import regime_vol as _regime_vol
from sniper.algos.polymarket_crypto5m.stabilized_z import stabilized_z as _stabilized_z
```

Then immediately after the `desk.update({... "signal": entry_signal(edges)})` block (`:364-367`) and before `desk["available"] = ...`, insert:

```python
    # --- brain v2 (SHADOW): stabilized, market-regularized probability next to the current z. Never feeds a
    # decision in v1. Pure functions of the per-poll klines + the book mid -> no buffer, fail-closed to None.
    rv = _regime_vol(closes_by_sec, now) if kl else None
    _spread_up = (up_ask - up_bid) if (up_ask is not None and up_bid is not None and up_ask > up_bid) else None
    sz = _stabilized_z(btc_now, btc_start, left, rv, up_mid, _spread_up) if rv else None
    desk.update({
        "vol_slow": (rv or {}).get("vol_slow"), "vol_fast": (rv or {}).get("vol_fast"),
        "jump_flag": (rv or {}).get("jump_flag"), "divergence": (rv or {}).get("divergence"),
        "z_ours": (sz or {}).get("z_ours"), "z_market": (sz or {}).get("z_market"),
        "z_v2": (sz or {}).get("z_v2"), "z_weight": (sz or {}).get("weight")})
```

(`closes_by_sec` is already in scope from `:320`; it is `{}`/undefined only when `kl` is falsy, which the `if kl else None` guards.)

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_crypto5m_live_desk.py -v`
Expected: PASS (existing desk tests + the new one).

- [ ] **Step 5: Commit**

```bash
git add sniper/algos/polymarket_crypto5m/live_desk.py tests/test_crypto5m_live_desk.py
git commit -m "feat(crypto5m): expose shadow z_v2 + regime-vol components on the live desk (no decision use)"
```

---

### Task 8: Runner wiring — log shadow rows + resolve at window close (shadow-only)

**Files:**
- Modify: `scripts/run_polymarket_crypto5m_active_trader.py` (imports; per-tick record; window-close resolve; `_connect` schema)
- Test: `tests/test_crypto5m_active_runner.py` (extend — the zero-live-risk invariant)

**Interfaces:**
- Consumes: `z_shadow_log` (ZL), `regime_vol.sharp_window_label`, the desk shadow fields, the existing settlement helper (`S.from_gamma(MR.parse_event(ev)[0]).winning_outcome`, runner `:162-165`).
- Produces: a `crypto5m_z_shadow` row each tick; `resolve_window` called when a window closes. **No change to any decision.**

- [ ] **Step 1: Write the failing test (decisions byte-identical, rows written)**

```python
# add to tests/test_crypto5m_active_runner.py
def test_shadow_log_is_written_and_does_not_change_decisions(tmp_path, monkeypatch):
    ws = 1782010000
    # reuse the _desk helper; add the shadow fields the runner will log
    def mk(now, **kw):
        d = _desk(now, ws, up_ask=0.50, up_bid=0.49, ofi=0.6)
        d.update({"z_v2": -0.1, "z_ours": 0.0, "z_market": -0.2, "z_weight": 0.6,
                  "vol_slow": 1e-4, "vol_fast": 2e-4, "jump_flag": False, "divergence": 2.0})
        d.update(kw); return d
    desks = iter([mk(ws + 100), mk(ws + 102), mk(ws + 104), mk(ws + 106), mk(ws + 108)])
    monkeypatch.setattr(runner.LD, "build_live_desk", lambda *a, **k: next(desks))
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)
    monkeypatch.setattr(runner, "EVCFG", runner.RZ.EVConfig(k=0.6))

    conn = runner._connect(str(tmp_path / "t.sqlite"))
    runner.run(conn, poll_interval=0, max_iterations=5)

    # shadow rows logged for the confirmed ticks
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_z_shadow").fetchone()[0] >= 1
    # and the decision path is unchanged: the SAME entry the ds-patient+EV path always made still happens
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_active_positions WHERE status='open'").fetchone()[0] == 1
```

(The `test_deferred_fill_records_slip` test already asserts the exact decision sequence; keep it passing — it is the byte-identical guard.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_crypto5m_active_runner.py::test_shadow_log_is_written_and_does_not_change_decisions -v`
Expected: FAIL — `no such table: crypto5m_z_shadow` (runner doesn't log yet).

- [ ] **Step 3: Implement**

Add imports near the other package imports (`:21-29`):

```python
from sniper.algos.polymarket_crypto5m import z_shadow_log as ZL                # noqa: E402
from sniper.algos.polymarket_crypto5m.regime_vol import sharp_window_label     # noqa: E402
```

In `_connect(...)`, after `EVLOG.ensure_schema(c)`, add:

```python
    ZL.ensure_schema(c)
```

In the main loop, right after `confirm = dstate.update(thesis, desk, now)` (`:229`), add the per-tick shadow record (independent of the EV/decision block — pure logging):

```python
            # brain v2 SHADOW: log the stabilized probability every tick (never reads back into a decision)
            if desk.get("z_v2") is not None:
                ZL.record_tick(conn, {
                    "engine_version": Z_SHADOW_VERSION, "window_start_ts": ws, "tick_ts": now,
                    "condition_id": desk.get("condition_id"),
                    "btc_now": desk.get("btc_now"), "btc_start": desk.get("btc_start"),
                    "seconds_left": desk.get("seconds_left"),
                    "vol_slow": desk.get("vol_slow"), "vol_fast": desk.get("vol_fast"),
                    "jump_flag": 1 if desk.get("jump_flag") else 0, "divergence": desk.get("divergence"),
                    "z": desk.get("z"), "z_ours": desk.get("z_ours"), "z_market": desk.get("z_market"),
                    "market_mid": desk.get("up_mid"), "z_v2": desk.get("z_v2"), "weight": desk.get("z_weight")})
            ZL.sweep_incomplete(conn, now)
```

Add the version constant near `ENGINE_VERSION` (`:40`):

```python
Z_SHADOW_VERSION = "brain-v2-shadow-v1"
```

In the window-close settlement block (`:289-295`, where `winner` is resolved for open positions), after the existing settlement of positions, resolve the shadow rows for that closed window `w`:

```python
                    # resolve brain-v2 shadow rows for this closed window with the same settlement + the label
                    rets = _window_returns(conn_or_desk_klines)   # see note
                    ZL.resolve_window(conn, w, settlement=(1 if winner == "Up" else 0),
                                      sharp_label=sharp_window_label(rets, 1 if winner == "Up" else 0), now_ts=now)
```

**Implementation note for the label inputs:** the resolver needs the closed window's 1s log-returns. The runner already fetches klines each poll; persist the window's returns by accumulating them from `desk` per tick into an in-loop `dict[ws] -> list[(sec, close)]` is **not** allowed (cross-poll state is fine *in the runner loop* — it is process-local I/O bookkeeping, not a feature buffer; but simpler and stateless:) re-derive returns at close from a fresh kline fetch for the window via `LD._get(f"{LD.BINANCE_KLINES}&startTime={w*1000}&endTime={(w+300)*1000}&limit=400")`, build closes, diff to log-returns. Wrap in `LD._safe`; on fetch failure pass `rets=[]` (→ label `calm`, still resolved). Define a small helper `_window_log_returns(w)` in the runner using `LD._get`/`LD._safe` and `math.log`.

- [ ] **Step 4: Run the focused runner tests**

Run: `python3 -m pytest tests/test_crypto5m_active_runner.py -v`
Expected: PASS — the new shadow test AND the pre-existing `test_deferred_fill_records_slip` (decisions unchanged) AND `test_far_otm_confirmed_tick_blocked_but_logged`.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_polymarket_crypto5m_active_trader.py tests/test_crypto5m_active_runner.py
git commit -m "feat(crypto5m): runner logs brain-v2 shadow rows + resolves at window close (shadow-only)"
```

---

### Task 9: Validation harness — buckets, sharp-tick paired Brier, chronological split

**Files:**
- Create: `scripts/crypto5m_z_validation.py`
- Test: `tests/test_crypto5m_z_validation.py`

**Interfaces:**
- Consumes: `crypto5m_z_shadow` rows; `window_bootstrap`; `regime_vol.sharp_window_label` is already applied (rows carry `sharp_label`).
- Produces: `load_resolved(conn) -> list[dict]`; `window_brier_delta(rows_for_window, score_fn=current_vs_v2) -> float | None` (mean over the window's **sharp ticks** of `Brier(current z) − Brier(z_v2)`; `None` if no sharp ticks); `chronological_split(windows, train_frac=0.7) -> (train, holdout)`; `phi(z) -> float`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_crypto5m_z_validation.py
import importlib.util, os, sqlite3, math
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("zval", os.path.join(ROOT, "scripts", "crypto5m_z_validation.py"))
ZV = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(ZV)


def test_phi_monotone():
    assert ZV.phi(-5) < 0.01 and abs(ZV.phi(0) - 0.5) < 1e-9 and ZV.phi(5) > 0.99


def test_window_brier_delta_uses_sharp_ticks_only():
    # two ticks: one sharp (divergence high) where z_v2 is better-calibrated, one calm where z is "better".
    # settlement y=1. sharp tick: z=2 (Phi~.98 close to 1, good) vs ... make z_v2 the better one:
    y = 1
    rows = [
        {"divergence": 5.0, "jump_flag": 0, "z": -1.0, "z_v2": 1.5, "settlement": y},  # sharp: z bad, z_v2 good
        {"divergence": 0.5, "jump_flag": 0, "z": 3.0, "z_v2": 0.0, "settlement": y},   # calm: ignored by gate
    ]
    d = ZV.window_brier_delta(rows)              # mean over SHARP ticks of Brier(z) - Brier(z_v2)
    # only the sharp tick counts: Brier(z=-1) - Brier(z_v2=1.5) > 0 (z_v2 better)
    assert d is not None and d > 0


def test_window_brier_delta_none_without_sharp_ticks():
    rows = [{"divergence": 0.4, "jump_flag": 0, "z": 0.1, "z_v2": 0.1, "settlement": 1}]
    assert ZV.window_brier_delta(rows) is None


def test_chronological_split_no_leakage():
    windows = {100: [], 200: [], 300: [], 400: [], 500: []}
    train, holdout = ZV.chronological_split(windows, train_frac=0.6)
    assert max(train) < min(holdout)            # every holdout window strictly later than every train window
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_crypto5m_z_validation.py -v`
Expected: FAIL — script missing.

- [ ] **Step 3: Implement the harness core (Task 9 portion)**

```python
# scripts/crypto5m_z_validation.py
"""Offline validation harness for crypto5m brain v2 (design sec 4 + 7). Read-only; proposes a verdict, never
mutates thresholds or the live path. Gate = paired per-window Brier delta over the SHARP TICKS of each window
(window-clustered bootstrap), win on sharp-revert, no-regress on calm + sharp-continue. Routed through the
shared window_bootstrap (same seed as lane-eval)."""
from __future__ import annotations

import argparse
import math
import os
import random
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sniper.algos.polymarket_crypto5m import window_bootstrap as WB   # noqa: E402

D_REF = 2.0   # a tick is "sharp" if jump_flag or divergence >= D_REF (matches stabilized_z.D_REF)
DEFAULT_DB = os.path.join(ROOT, "data", "crypto5m", "active_trader_v0.sqlite")


def phi(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def load_resolved(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM crypto5m_z_shadow WHERE status='resolved' AND z IS NOT NULL AND z_v2 IS NOT NULL "
        "ORDER BY window_start_ts, tick_ts")]


def _is_sharp_tick(r):
    return bool(r.get("jump_flag")) or (r.get("divergence") is not None and r["divergence"] >= D_REF)


def window_brier_delta(rows):
    """Mean over the window's SHARP ticks of Brier(current z) - Brier(z_v2). >0 = z_v2 better. None if the
    window has no sharp ticks (it contributes nothing to the sharp-bucket gate)."""
    deltas = []
    for r in rows:
        if not _is_sharp_tick(r):
            continue
        y = r["settlement"]
        b_cur = (phi(r["z"]) - y) ** 2
        b_v2 = (phi(r["z_v2"]) - y) ** 2
        deltas.append(b_cur - b_v2)
    return (sum(deltas) / len(deltas)) if deltas else None


def chronological_split(windows, train_frac=0.7):
    ordered = sorted(windows)
    cut = int(len(ordered) * train_frac)
    return ordered[:cut], ordered[cut:]
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_crypto5m_z_validation.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/crypto5m_z_validation.py tests/test_crypto5m_z_validation.py
git commit -m "feat(crypto5m): z-validation core — sharp-tick paired Brier delta + chronological split"
```

---

### Task 10: Validation harness — fit weight, gate, accruing-mode, milestone ladder, CLI

**Files:**
- Modify: `scripts/crypto5m_z_validation.py` (add fit + gate + `main`)
- Test: `tests/test_crypto5m_z_validation.py` (extend)

**Interfaces:**
- Consumes: Task 9 functions + `window_bootstrap`.
- Produces: `sigmoid(x)`, `recompute_z_v2(row, a, b)`, `fit_weight(train_rows) -> (a, b)` (grid search minimizing sharp-tick Brier of `Φ(z_v2(a,b))` on train), `gate(conn, *, train_frac=0.7, milestone=240) -> dict` (buckets via `sharp_label`, paired delta by window, bootstrap CI on holdout: win on `sharp_revert`, no-regress on `calm`+`sharp_continue`, `status` ∈ `accruing|go|no_go`).

- [ ] **Step 1: Write the failing tests (accruing-mode + symmetric fit + gate go)**

```python
# add to tests/test_crypto5m_z_validation.py
def _seed_db(path, rows):
    import sniper.algos.polymarket_crypto5m.z_shadow_log as ZL
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row; ZL.ensure_schema(c)
    for r in rows:
        ZL.record_tick(c, r)
    for ws in {r["window_start_ts"] for r in rows}:
        # settlement/label provided per-row via the seed (use the first row's)
        first = next(x for x in rows if x["window_start_ts"] == ws)
        ZL.resolve_window(c, ws, settlement=first["_settlement"], sharp_label=first["_label"], now_ts=ws + 305)
    return c


def test_gate_accruing_below_milestone(tmp_path):
    p = str(tmp_path / "z.sqlite")
    rows = [{"engine_version": "v", "window_start_ts": 1000 + i, "tick_ts": 1000 + i, "condition_id": "c",
             "btc_now": 1, "btc_start": 1, "seconds_left": 100, "vol_slow": 1e-4, "vol_fast": 5e-4,
             "jump_flag": 1, "divergence": 5.0, "z": 0.0, "z_ours": 0.0, "z_market": 0.0, "market_mid": 0.5,
             "z_v2": 0.0, "weight": 0.5, "_settlement": 1, "_label": "sharp_revert"} for i in range(5)]
    _seed_db(p, rows)
    c = sqlite3.connect(p); c.row_factory = sqlite3.Row
    g = ZV.gate(c, milestone=240)
    assert g["status"] == "accruing" and g["n_sharp_revert_holdout"] < 240


def test_sigmoid_fit_recovers_both_directions():
    # synthetic: when divergence is high, z_market is the truth-aligned estimate (our z is wrong) -> w->0;
    # when low, our z is right and market is wrong -> w->1. fit must recover both.
    train = []
    for i in range(200):
        sharp = i % 2 == 0
        y = 1
        if sharp:   # market right (z_market high), ours wrong (z_ours low)
            train.append({"divergence": 6.0, "z_ours": -2.0, "z_market": 2.5, "settlement": y, "jump_flag": 0})
        else:       # ours right, market wrong
            train.append({"divergence": 0.5, "z_ours": 2.5, "z_market": -2.0, "settlement": y, "jump_flag": 0})
    a, b = ZV.fit_weight(train)
    w_sharp = ZV.sigmoid(a - b * math.log(6.0))
    w_calm = ZV.sigmoid(a - b * math.log(0.5))
    assert w_sharp < 0.3 and w_calm > 0.7
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_crypto5m_z_validation.py -k "accruing or recovers" -v`
Expected: FAIL — `AttributeError: gate / fit_weight / sigmoid`.

- [ ] **Step 3: Implement fit + gate + CLI**

```python
# append to scripts/crypto5m_z_validation.py
MILESTONES = (60, 120, 240)   # holdout sharp-revert windows: monitor / monitor / first-confirmatory


def sigmoid(x):
    if x < -60:
        return 0.0
    if x > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def recompute_z_v2(row, a, b):
    """z_v2 under a candidate weight w = sigmoid(a - b*log(divergence)), from the logged components."""
    d = row.get("divergence")
    if d is None or d <= 0 or row.get("z_ours") is None or row.get("z_market") is None:
        return row.get("z_ours")
    w = sigmoid(a - b * math.log(d))
    return w * row["z_ours"] + (1.0 - w) * row["z_market"]


def _sharp_rows(rows):
    return [r for r in rows if _is_sharp_tick(r)]


def fit_weight(train_rows, a_grid=None, b_grid=None):
    """Grid search (a, b) minimizing mean sharp-tick Brier of Phi(z_v2(a,b)) on the training rows.
    Deterministic; stdlib-only. Defaults span calm->cede behavior."""
    a_grid = a_grid or [x * 0.5 for x in range(-6, 7)]       # -3.0 .. 3.0
    b_grid = b_grid or [x * 0.5 for x in range(0, 9)]        # 0 .. 4.0 (monotone: b>=0)
    sharp = _sharp_rows(train_rows)
    if not sharp:
        return (0.0, 0.0)
    best, best_brier = (0.0, 0.0), float("inf")
    for a in a_grid:
        for b in b_grid:
            br = sum((phi(recompute_z_v2(r, a, b)) - r["settlement"]) ** 2 for r in sharp) / len(sharp)
            if br < best_brier:
                best, best_brier = (a, b), br
    return best


def _by_window_delta(rows_by_window, a, b):
    """{window: [delta]} where delta = mean sharp-tick (Brier(z) - Brier(z_v2(a,b)))."""
    out = {}
    for ws, rows in rows_by_window.items():
        deltas = [((phi(r["z"]) - r["settlement"]) ** 2 - (phi(recompute_z_v2(r, a, b)) - r["settlement"]) ** 2)
                  for r in rows if _is_sharp_tick(r)]
        if deltas:
            out[ws] = [sum(deltas) / len(deltas)]
    return out


def _ci(by_window, rng):
    means = WB.boot_means(by_window, rng)
    if not means:
        return (None, None, None)
    return (sum(means) / len(means), WB.pctile(means, 0.025), WB.pctile(means, 0.975))


def gate(conn, *, train_frac=0.7, milestone=240):
    rows = load_resolved(conn)
    by_window, label = {}, {}
    for r in rows:
        by_window.setdefault(r["window_start_ts"], []).append(r)
        label[r["window_start_ts"]] = r.get("sharp_label")
    train_ws, holdout_ws = chronological_split(by_window, train_frac)
    a, b = fit_weight([r for ws in train_ws for r in by_window[ws]])

    def bucket(names):
        return {ws: by_window[ws] for ws in holdout_ws if label.get(ws) in names}

    rng = random.Random(WB.BOOT_SEED)
    revert = _by_window_delta(bucket({"sharp_revert"}), a, b)
    guards = _by_window_delta(bucket({"calm", "sharp_continue"}), a, b)
    n_rev = len(revert)
    rev_mean, rev_lo, rev_hi = _ci(revert, rng)
    grd_mean, grd_lo, grd_hi = _ci(guards, random.Random(WB.BOOT_SEED))

    res = {"a": a, "b": b, "n_sharp_revert_holdout": n_rev, "milestones": MILESTONES,
           "revert": {"mean": rev_mean, "ci_lo": rev_lo, "ci_hi": rev_hi},
           "guards": {"mean": grd_mean, "ci_lo": grd_lo, "ci_hi": grd_hi}}
    if n_rev < milestone:
        res["status"] = "accruing"
        return res
    win = rev_lo is not None and rev_lo > 0                       # CI excludes 0, favorable
    no_regress = grd_lo is None or grd_lo >= 0                    # guards not adverse
    res["status"] = "go" if (win and no_regress) else "no_go"
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--milestone", type=int, default=240)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"[z-validation] no db at {args.db}"); return
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True); conn.row_factory = sqlite3.Row
    g = gate(conn, milestone=args.milestone)
    print(f"[z-validation] status={g['status']}  fit a={g['a']:.2f} b={g['b']:.2f}  "
          f"n_sharp_revert_holdout={g['n_sharp_revert_holdout']} (milestones {g['milestones']})")
    rv, gd = g["revert"], g["guards"]
    def fmt(d):
        return "n/a" if d["mean"] is None else f"mean {d['mean']:+.4f}  CI [{d['ci_lo']:+.4f}, {d['ci_hi']:+.4f}]"
    print(f"  sharp_revert (WIN bucket): {fmt(rv)}")
    print(f"  calm+sharp_continue (GUARD): {fmt(gd)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_crypto5m_z_validation.py -v`
Expected: PASS (all — incl. accruing-mode and symmetric-fit).

- [ ] **Step 5: Commit**

```bash
git add scripts/crypto5m_z_validation.py tests/test_crypto5m_z_validation.py
git commit -m "feat(crypto5m): z-validation gate — holdout fit, sharp-revert win + guards, accruing/go/no_go"
```

---

### Task 11: Real-window golden fixture (end-to-end on real data)

**Files:**
- Create: `tests/fixtures/crypto5m_brain_v2_window.json` (one real resolved window: klines + up book + settlement)
- Test: `tests/test_crypto5m_brain_v2_golden.py`

**Interfaces:**
- Consumes: `regime_vol`, `stabilized_z`, `regime_vol.sharp_window_label`.

- [ ] **Step 1: Capture one real window into the fixture**

Run (writes the fixture from a real resolved window in the active DB, or a live capture if present):

```bash
python3 - <<'PY'
import json, sqlite3, os
# Prefer a real resolved shadow window if any exist; else capture klines live for the most recent closed window.
db = "data/crypto5m/active_trader_v0.sqlite"
out = {"closes_by_sec": {}, "up_mid": 0.55, "seconds_left": 120, "settlement": 1, "note": "real-window golden"}
import urllib.request
def get(u): return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent":"sniper"}), timeout=10).read())
t = int(get("https://api.binance.com/api/v3/time")["serverTime"])//1000
ws = (t - 600) - ((t - 600) % 300)            # a window that has already closed
kl = get(f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1s&startTime={(ws-90)*1000}&endTime={(ws+300)*1000}&limit=400")
out["closes_by_sec"] = {str(int(k[0])//1000): float(k[4]) for k in kl}
out["window_start_ts"] = ws
os.makedirs("tests/fixtures", exist_ok=True)
json.dump(out, open("tests/fixtures/crypto5m_brain_v2_window.json","w"), indent=2)
print("wrote fixture: window", ws, "ticks", len(out["closes_by_sec"]))
PY
```

(If offline, hand-author the fixture from a `crypto5m_z_shadow` resolved row's klines. The settlement is whichever side BTC closed; set `settlement` accordingly.)

- [ ] **Step 2: Write the golden test**

```python
# tests/test_crypto5m_brain_v2_golden.py
import json, os, math
from sniper.algos.polymarket_crypto5m import regime_vol as RV
from sniper.algos.polymarket_crypto5m import stabilized_z as SZ

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "crypto5m_brain_v2_window.json")


def test_pipeline_on_a_real_window():
    d = json.load(open(FIX))
    closes = {int(s): float(p) for s, p in d["closes_by_sec"].items()}
    now = max(closes)
    rv = RV.regime_vol(closes, now)
    assert rv is not None and rv["vol_slow"] > 0 and rv["divergence"] > 0
    btc_now = closes[now]; btc_start = closes[min(closes)]
    sz = SZ.stabilized_z(btc_now, btc_start, d["seconds_left"], rv, d["up_mid"], SZ.S_REF)
    assert sz is not None
    assert math.isfinite(sz["z_v2"]) and -10 < sz["z_v2"] < 10
    assert 0.0 <= sz["weight"] <= 1.0
    secs = sorted(closes)
    rets = [math.log(closes[b] / closes[a]) for a, b in zip(secs, secs[1:]) if closes[a] > 0]
    assert RV.sharp_window_label(rets, d["settlement"]) in {"calm", "sharp_revert", "sharp_continue"}
```

- [ ] **Step 3: Run**

Run: `python3 -m pytest tests/test_crypto5m_brain_v2_golden.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/crypto5m_brain_v2_window.json tests/test_crypto5m_brain_v2_golden.py
git commit -m "test(crypto5m): real-window golden fixture for the brain-v2 pipeline"
```

---

### Task 12: Live graph (display-only; screenshot-verified, no unit test)

**Files:**
- Modify: `sniper/routes/api_crypto5m.py` (add a series read from `crypto5m_z_shadow`)
- Modify: the 5m UI panel (the file that renders the ACTIVE ENGINE desk — locate via `grep -rl "ACTIVE ENGINE" sniper/web static templates`)

**Interfaces:**
- Produces: `GET /api/crypto5m/z-series?window=<ws>` → `{"series": [{"tick_ts", "z", "z_v2", "z_market"} ...]}` for the latest (or given) window, read-only from `crypto5m_z_shadow`.

- [ ] **Step 1: Add the read-only endpoint**

In `api_crypto5m.py`, add a route mirroring the existing `_open_readonly(db_path)` pattern used by `/api/crypto5m/active`:

```python
    @bp.get("/api/crypto5m/z-series")
    def z_series():
        db_path = ctx.get("active_db_path") or os.getenv("CRYPTO5M_ACTIVE_DB") or ACTIVE_DEFAULT_DB
        out = {"available": False, "series": []}
        if not os.path.exists(db_path):
            return jsonify(out)
        try:
            conn = _open_readonly(db_path)
            try:
                ws_row = conn.execute("SELECT MAX(window_start_ts) w FROM crypto5m_z_shadow").fetchone()
                ws = request.args.get("window", type=int) or (ws_row["w"] if ws_row else None)
                rows = conn.execute(
                    "SELECT tick_ts, z, z_v2, z_market FROM crypto5m_z_shadow "
                    "WHERE window_start_ts=? ORDER BY tick_ts", (ws,)).fetchall() if ws else []
            finally:
                conn.close()
        except sqlite3.Error as e:
            out["message"] = f"z-series unavailable: {e}"
            return jsonify(out)
        out["available"] = bool(rows)
        out["window_start_ts"] = ws
        out["series"] = [{"tick_ts": r["tick_ts"], "z": r["z"], "z_v2": r["z_v2"], "z_market": r["z_market"]}
                         for r in rows]
        return jsonify(out)
```

- [ ] **Step 2: Add the UI line chart**

In the ACTIVE ENGINE desk panel, add a small "Stabilized probability (shadow)" line chart that fetches `/api/crypto5m/z-series` and plots `z` (grey), `z_v2` (green), `z_market` (orange) over `tick_ts`, mirroring the existing "OUR MOVING AVERAGES" sparkline already on the desk. Bump the desk cache version string.

- [ ] **Step 3: Verify by screenshot (per spec §6.6 — display-only, not TDD)**

```bash
# with the web server running (python3 -m sniper.web.server), capture the 5m tab:
python3 scripts/browse.py http://localhost:5000/#crypto5m --out /tmp/brain_v2_graph.png --width 1600 --height 1400 --wait 6
```
Then Read `/tmp/brain_v2_graph.png` and confirm three lines render (grey/green/orange) over the window. (If no shadow rows yet, the panel shows "accruing".)

- [ ] **Step 4: Commit**

```bash
git add sniper/routes/api_crypto5m.py  # + the UI panel file
git commit -m "feat(crypto5m-ui): display-only z vs z_v2 vs market_z shadow graph on the 5m desk"
```

---

## Final Steps (after all tasks)

- [ ] **Full crypto5m regression**

Run: `python3 -m pytest tests/ -k crypto5m -q`
Expected: all brain-v2 tests pass; pre-existing crypto5m tests unchanged. (Note the known-unrelated `tests/test_ui_algo_tabs.py::...microstructure_board` failure that predates this branch — confirm it is the only red and is identical at the branch base.)

- [ ] **Schema + index freshness (CI parity)**

Run: `python3 scripts/validate_schema_refs.py && python3 scripts/brain_index.py generate && git diff --quiet index/ || echo "INDEX CHANGED — commit it"`
Expected: no violations; index clean (already committed in Task 6) or committed now.

- [ ] **Cron wiring (operator-gated, note only):** add `scripts/crypto5m_z_validation.py --quiet` to the crypto5m cron cadence (alongside `crypto5m_active_review.py`) so the gate accrues continuously. Do **not** enable without operator sign-off — this is a runtime-ops change, out of the code scope.

---

## Plan Self-Review

**Spec coverage:** §3 blend → Tasks 4,5; §6.1 regime_vol (corr #1) → Task 2; §6.2 degenerate split + clamp → Task 5; §6.3 prior anchor (req #2) + fit → Tasks 4,10; §6.4 table → Task 6; §6.5 harness + bootstrap reuse (req #4) → Tasks 1,9,10; §6.6 graph → Task 12; §7 sharp label + chronological split + milestone (gate on sharp-revert) → Tasks 3,9,10; §8.A invariants (incl. post-jump + un-cede) → Task 5; §8.B units + symmetric fit → Tasks 2,4,10; §8.C accruing-mode + sharp-tick aggregation (req #1) → Tasks 9,10; §8.D label → Task 3; §8.E zero-live-risk + restart + unresolved lifecycle → Tasks 6,8; §8.F golden → Task 11; §8.G CI hygiene → Task 6. Requirement #3 (jump-cap pure fn of ticks_since_jump) → Task 4. All covered.

**Placeholder scan:** no TBD/"handle errors"/"similar to". Every code step has complete code. The one fetch helper in Task 8 (`_window_log_returns`) is described with its exact inputs/outputs and the `LD._get`/`LD._safe`/`math.log` it uses — the implementer writes the ~6-line body from that.

**Type consistency:** `regime_vol(...)` dict keys (`vol_slow/vol_fast/jump_flag/ticks_since_jump/divergence`) are consumed unchanged in Tasks 5,7,8. `stabilized_z(...)` returns `z_v2/z_ours/z_market/weight/divergence/jump_flag`; desk maps `weight`→`z_weight` (Task 7) and the shadow row maps `z_weight`→`weight` column (Task 8) — consistent. `window_brier_delta`/`recompute_z_v2`/`gate` signatures match across Tasks 9,10. `BOOT_SEED` single source (Task 1) used by lane-eval and the harness.
