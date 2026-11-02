# Crypto5m Reachability-Gated Entry (EV Gate) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an expected-odds-move EV gate to the live crypto5m scalper so it only enters when a correct directional read can actually move the binary's odds more than spread+fee in the time left — killing the dead-underdog / theta-collapse entries (the 0.11 −$7.27 case).

**Architecture:** A new pure module `reachability.py` computes the capped-hold EV from the diffusion the engine already runs (`z`, `vol`, `seconds_left`) plus the thesis `conviction`. `scalp_trader.decide` consumes a precomputed EV dict and blocks entry when it fails (off when `ev=None`, so `ds-patient` behavior is untouched). The runner computes the EV for every *confirmed* tick, gates entry on it, and logs the full EV + a counterfactual realized-move (book at `t` and `t+t_hold`) under **both** fees to a new `crypto5m_ev_observations` table — the make-or-break calibration instrument.

**Tech Stack:** Python 3.13, stdlib `math` + `sqlite3`, pytest. No new dependencies.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-25-crypto5m-reachability-gated-entry-exit-design.md`. Every task implements part of it.
- **Entry-only v1.** Do NOT touch exit logic (`update_exit_state`, the holding branch of `decide`, settlement). One lever per `ENGINE_VERSION`.
- **Both fee columns are ALWAYS logged** (documented `0.07` → `ev_007`/`passed_007`; realized `0.0` → `ev_realfee`/`passed_realfee`). **v1 LIVE decision gates on the realized fee** (`gate_on_realized=True`) so the engine actually trades on paper (where the real cost is the spread); documented `0.07` is the real-money-readiness overlay. Cost-bound/maker verdict reads `*_realfee`; flip `gate_on_realized=False` for real-money mode (no data change).
- **`k = 0.10`, biased low** (EV\* ∝ k², low = strict = safe). Single strictness knob.
- **Defaults:** `t_min=5s`, `EXIT_BUFFER_S=20s`, `T_CEILING_S=30s`, price-band guardrail `[0.05, 0.95]`.
- **Live path uses the capped `EV(t_hold)` form only** — never the `÷|z|` interior closed-form (div-by-zero at ATM). The `÷|z|` form is for tests/intuition.
- **Fail closed on degenerate inputs** (`vol_per_s` 0/None, `seconds_left ≤ 0`, `z` None, non-finite) → `passed=False` with a `reject_reason`; never divide by them.
- **All prices in `[0,1]`.** Clamp/guard before division.
- **`conviction = abs(score)·agreement ∈ [0,1]`** (`direction.py:96`); the confirm gate already requires `≥0.50`, so the EV gate sees `conviction ∈ [0.5, 1]`.
- **`ENGINE_VERSION = "ev-reach-v1"`** (bumped from `"ds-patient-v1"`) — blank-slate counting resets.
- **DB:** the active engine writes its own standalone paper sqlite (`data/crypto5m/active_trader_v0.sqlite`) with inline `CREATE TABLE` in the runner — the new table follows that existing pattern (NOT `db.py`, which governs the main tracker DB only).
- **Test commands:** focused `pytest` per task; full crypto5m sweep at the end: `python3 -m pytest tests/test_crypto5m_reachability.py tests/test_crypto5m_ev_observation_log.py tests/test_crypto5m_direction_scalp.py tests/test_crypto5m_active_runner.py -q`.

---

### Task 1: `reachability.py` — pure EV gate

**Files:**
- Create: `sniper/algos/polymarket_crypto5m/reachability.py`
- Test: `tests/test_crypto5m_reachability.py`

**Interfaces:**
- Produces:
  - `EVConfig(k=0.10, documented_fee_rate=0.07, realized_fee_rate=0.0, gate_on_realized=True, t_min=5.0, exit_buffer_s=20.0, t_ceiling_s=30.0, price_floor=0.05, price_ceiling=0.95)` (frozen dataclass). Both fees always feed the logged columns; `gate_on_realized` selects which drives the live `passed`.
  - `normal_pdf(z: float) -> float`
  - `probit(p: float) -> float | None` (Acklam inverse-normal; logging only)
  - `entry_ev(*, z, vol_per_s, seconds_left, conviction, side_ask, side_bid, cfg=EVConfig()) -> dict` with keys: `passed` (==`passed_007`), `passed_007`, `passed_realfee`, `ev_007`, `ev_realfee`, `delta`, `theta`, `t_hold`, `t_star`, `gain`, `gate_stat`, `spread`, `cost_007`, `cost_realfee`, `reject_reason`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_crypto5m_reachability.py
import math
import pytest
from sniper.algos.polymarket_crypto5m import reachability as RZ


def test_far_otm_near_expiry_rejected():
    # the 0.11 Up near expiry: z≈Φ⁻¹(0.11)≈-1.23, little time. Rejected — and at the REALIZED fee too
    # (it's a reachability/theta failure, not a fee failure).
    r = RZ.entry_ev(z=-1.23, vol_per_s=0.0002, seconds_left=40, conviction=0.6,
                    side_ask=0.11, side_bid=0.10)
    assert r["passed"] is False
    assert r["ev_007"] < 0 and r["ev_realfee"] < 0
    assert r["reject_reason"] == "ev_negative"


def test_atm_strong_read_dual_fee_split():
    # strong ATM read with room: blocked at documented 0.07 but PASSES at realized-0. v1 gates on realized,
    # so the LIVE decision (passed) follows passed_realfee.
    r = RZ.entry_ev(z=0.0, vol_per_s=0.0002, seconds_left=120, conviction=1.0,
                    side_ask=0.50, side_bid=0.495)
    assert r["passed_007"] is False        # documented 0.07 overlay too strict (real-money-readiness)
    assert r["passed_realfee"] is True     # realized-0 admits it
    assert r["passed"] is True             # v1 gate_on_realized=True -> live decision follows realized
    assert r["ev_realfee"] > r["ev_007"]


def test_gate_selector_documented_vs_realized():
    # the SAME read: gated on realized (default) it passes; gated on documented 0.07 it's blocked. Both fee
    # columns are logged regardless of which one gates.
    kw = dict(z=0.0, vol_per_s=0.0002, seconds_left=120, conviction=1.0, side_ask=0.50, side_bid=0.495)
    assert RZ.entry_ev(cfg=RZ.EVConfig(gate_on_realized=True), **kw)["passed"] is True
    r = RZ.entry_ev(cfg=RZ.EVConfig(gate_on_realized=False), **kw)
    assert r["passed"] is False
    assert r["ev_007"] is not None and r["ev_realfee"] is not None


def test_atm_cap_binds_no_divzero():
    # z->0: |z|->0 would divide by zero in the interior form; the capped form is finite.
    r = RZ.entry_ev(z=0.0, vol_per_s=0.0002, seconds_left=200, conviction=0.8,
                    side_ask=0.50, side_bid=0.49)
    assert r["t_star"] is None             # unbounded optimum -> reported as None
    assert r["t_hold"] == pytest.approx(30.0)   # cap (T_CEILING) binds
    assert r["gain"] is not None and math.isfinite(r["gain"])


def test_conviction_monotonic():
    base = dict(z=-0.3, vol_per_s=0.0002, seconds_left=120, side_ask=0.45, side_bid=0.44,
                cfg=RZ.EVConfig(k=0.4))
    lo = RZ.entry_ev(conviction=0.6, **base)["ev_007"]
    hi = RZ.entry_ev(conviction=0.9, **base)["ev_007"]
    assert hi > lo


def test_fee_monotonic():
    r = RZ.entry_ev(z=-0.2, vol_per_s=0.0002, seconds_left=120, conviction=0.9,
                    side_ask=0.45, side_bid=0.44, cfg=RZ.EVConfig(k=0.4))
    assert r["ev_realfee"] > r["ev_007"]   # lower fee -> higher EV, always


def test_delta_rises_near_expiry_raises_ev_at_fixed_moneyness():
    # At a FIXED moneyness z, the binary's delta = phi(z)/(vol*sqrt(t_left)) is LARGER near expiry, and a
    # capped hold captures a larger fraction sqrt(t_hold/t_left) of the shorter remaining diffusion. So less
    # time left -> higher per-trade EV. (The "last seconds -> no trade" case is handled by no_hold_room, not
    # by EV falling with time.)
    base = dict(z=-0.3, vol_per_s=0.0002, conviction=0.9, side_ask=0.45, side_bid=0.44,
                cfg=RZ.EVConfig(k=0.4))
    much_time = RZ.entry_ev(seconds_left=120, **base)["ev_007"]
    little_time = RZ.entry_ev(seconds_left=40, **base)["ev_007"]
    assert little_time > much_time


def test_degenerate_inputs_fail_closed():
    for bad in (dict(vol_per_s=0.0), dict(vol_per_s=None), dict(seconds_left=0), dict(z=None)):
        kw = dict(z=0.0, vol_per_s=0.0002, seconds_left=120, conviction=0.8,
                  side_ask=0.5, side_bid=0.49)
        kw.update(bad)
        r = RZ.entry_ev(**kw)
        assert r["passed"] is False and r["reject_reason"] == "degenerate_inputs"


def test_price_band_guardrail():
    r = RZ.entry_ev(z=0.0, vol_per_s=0.0002, seconds_left=120, conviction=0.9,
                    side_ask=0.97, side_bid=0.96)
    assert r["passed"] is False and r["reject_reason"] == "price_band"


def test_no_hold_room():
    r = RZ.entry_ev(z=0.0, vol_per_s=0.0002, seconds_left=22, conviction=0.9,
                    side_ask=0.5, side_bid=0.49)
    assert r["passed"] is False and r["reject_reason"] == "no_hold_room"


def test_probit_roundtrip_and_bounds():
    from math import erf, sqrt
    for z in (-1.5, -0.3, 0.0, 0.7, 1.9):
        p = 0.5 * (1 + erf(z / sqrt(2)))
        assert RZ.probit(p) == pytest.approx(z, abs=1e-3)
    assert RZ.probit(0.0) is None and RZ.probit(1.0) is None and RZ.probit(None) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_crypto5m_reachability.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'sniper.algos.polymarket_crypto5m.reachability'`.

- [ ] **Step 3: Write the module**

```python
# sniper/algos/polymarket_crypto5m/reachability.py
"""Reachability / expected-odds-move EV gate for the Polymarket BTC 5m scalper.

The directional read says WHICH way BTC is moving; this module says whether a correct read can actually
MOVE THE ODDS more than it costs, given how far the binary is from target (moneyness z) and how much time
is left. A deep-underdog near expiry barely responds to a move and bleeds to 0 (theta) — direction
confirmed, odds unreachable. This gate rejects it.

Math (the driftless diffusion the engine already runs; z = ln(now/start)/(vol*sqrt(t_left))):
    delta = phi(z)/(vol*sqrt(t_left))           odds gained per unit BTC log-move
    theta = phi(z)*|z|/(2*t_left)               odds bled per second of time passing
    E[signed log move over t_hold] ~ k*conviction*vol*sqrt(t_hold)
    EV(t_hold) = A*sqrt(t_hold) - B*t_hold - cost   A = phi(z)*k*conviction/sqrt(t_left), B = theta
EV is concave in t_hold -> t* = (A/2B)^2, CAPPED to the window. cost = spread + fee. Pure / no I/O.

LIVE PATH USES THE CAPPED EV(t_hold) ONLY — never the phi(z)/|z| interior closed-form (div-by-zero at ATM).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class EVConfig:
    k: float = 0.10                    # shrinkage / strictness — biased LOW (EV* ~ k^2; low = strict = safe)
    documented_fee_rate: float = 0.07  # ALWAYS logged -> ev_007/passed_007 (real-money-readiness overlay)
    realized_fee_rate: float = 0.0     # ALWAYS logged -> ev_realfee/passed_realfee (paper cost = spread)
    gate_on_realized: bool = True      # v1: LIVE `passed` follows the realized-fee EV (paper fee is 0); the
                                       # 0.07 overlay stays logged but does NOT gate. False = gate on 0.07.
    t_min: float = 5.0                 # floor on modeled hold (s)
    exit_buffer_s: float = 20.0        # never model holding past window_close - this (mirrors MIN_SECONDS_LEFT)
    t_ceiling_s: float = 30.0          # cap on modeled hold — bounds linear-theta/const-sigma approx; low = strict
    price_floor: float = 0.05          # catastrophic price-band guardrail (the EV gate is the operative band)
    price_ceiling: float = 0.95


def normal_pdf(z: float) -> float:
    return INV_SQRT_2PI * math.exp(-0.5 * z * z)


def probit(p):
    """Inverse normal CDF (Acklam's rational approximation). LOGGING ONLY (market-probit-z). None outside (0,1)."""
    if p is None or not (0.0 < p < 1.0):
        return None
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def _blank(reason):
    return {"passed": False, "passed_007": False, "passed_realfee": False, "ev_007": None, "ev_realfee": None,
            "delta": None, "theta": None, "t_hold": None, "t_star": None, "gain": None, "gate_stat": None,
            "spread": None, "cost_007": None, "cost_realfee": None, "reject_reason": reason}


def entry_ev(*, z, vol_per_s, seconds_left, conviction, side_ask, side_bid, cfg: EVConfig = EVConfig()):
    """Expected-odds-move EV for entering `side` THIS tick, per share, in odds (price) units.
    LIVE decision = `passed` (== passed_007). `*_realfee` feeds the cost-bound/maker verdict.
    Fail-closed: degenerate inputs -> passed=False + reject_reason (never divides by zero/None)."""
    if (z is None or vol_per_s is None or vol_per_s <= 0 or seconds_left is None or seconds_left <= 0
            or conviction is None or side_ask is None or side_bid is None
            or not math.isfinite(z)):
        return _blank("degenerate_inputs")
    if not (cfg.price_floor <= side_ask <= cfg.price_ceiling):
        return _blank("price_band")
    upper = min(seconds_left - cfg.exit_buffer_s, cfg.t_ceiling_s)
    if upper < cfg.t_min:
        return _blank("no_hold_room")

    phi = normal_pdf(z)
    absz = abs(z)
    A = phi * cfg.k * conviction / math.sqrt(seconds_left)
    B = phi * absz / (2.0 * seconds_left)                       # = |theta|
    # EV(t)=A*sqrt(t)-B*t is concave; t*=(A/2B)^2. z->0 => B->0 => t*->inf => the cap binds (no div-by-zero;
    # we evaluate the CAPPED EV, never the phi(z)/|z| interior form).
    t_star = math.inf if (B <= 0 or absz < 1e-9) else (A / (2.0 * B)) ** 2
    t_hold = min(max(t_star, cfg.t_min), upper)
    gain = A * math.sqrt(t_hold) - B * t_hold
    spread = max(0.0, side_ask - side_bid)

    def _cost(fee_rate):
        return spread + fee_rate * side_ask * (1.0 - side_ask)

    cost_007 = _cost(cfg.documented_fee_rate)
    cost_real = _cost(cfg.realized_fee_rate)
    ev_007 = gain - cost_007
    ev_real = gain - cost_real
    gate_stat = (conviction * conviction * phi / absz) if absz > 1e-9 else math.inf  # sec 2.2 diagnostic only
    delta = phi / (vol_per_s * math.sqrt(seconds_left))
    passed_007 = ev_007 > 0
    passed_realfee = ev_real > 0
    passed = passed_realfee if cfg.gate_on_realized else passed_007   # LIVE decision (v1: realized fee)
    return {"passed": passed, "passed_007": passed_007, "passed_realfee": passed_realfee,
            "ev_007": round(ev_007, 6), "ev_realfee": round(ev_real, 6),
            "delta": round(delta, 6), "theta": round(B, 8), "t_hold": round(t_hold, 3),
            "t_star": (None if math.isinf(t_star) else round(t_star, 3)), "gain": round(gain, 6),
            "gate_stat": (None if math.isinf(gate_stat) else round(gate_stat, 6)),
            "spread": round(spread, 6), "cost_007": round(cost_007, 6), "cost_realfee": round(cost_real, 6),
            "reject_reason": None if passed else "ev_negative"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_crypto5m_reachability.py -q`
Expected: PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/algos/polymarket_crypto5m/reachability.py tests/test_crypto5m_reachability.py
git commit -m "feat(crypto5m): reachability EV gate (pure module) — capped-hold EV, dual-fee"
```

---

### Task 2: Expose diffusion `z` + side mids on the desk

**Files:**
- Modify: `sniper/algos/polymarket_crypto5m/live_desk.py` (add `diffusion_z`; add `z`, `up_mid`, `down_mid` to the desk dict)
- Test: `tests/test_crypto5m_live_desk.py` (append; create if missing)

**Interfaces:**
- Consumes: nothing new.
- Produces: `live_desk.diffusion_z(btc_now, btc_start, vol_per_s, seconds_left) -> float | None`; `build_live_desk()` dict now includes keys `z`, `up_mid`, `down_mid`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_crypto5m_live_desk.py  (append)
import math
from sniper.algos.polymarket_crypto5m import live_desk as LD


def test_diffusion_z_matches_fair_value():
    # Phi(diffusion_z) must equal fair_value_up for the same inputs.
    args = dict(btc_now=100100.0, btc_start=100000.0, vol_per_s=0.0002, seconds_left=120)
    z = LD.diffusion_z(**args)
    assert z is not None
    assert 0.5 * (1 + math.erf(z / math.sqrt(2))) == LD.fair_value_up(**args)


def test_diffusion_z_guards():
    assert LD.diffusion_z(btc_now=0, btc_start=100, vol_per_s=0.0002, seconds_left=120) is None
    assert LD.diffusion_z(btc_now=100, btc_start=100, vol_per_s=0.0, seconds_left=120) is None
    assert LD.diffusion_z(btc_now=100, btc_start=100, vol_per_s=0.0002, seconds_left=0) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_crypto5m_live_desk.py::test_diffusion_z_matches_fair_value -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'diffusion_z'`.

- [ ] **Step 3: Add `diffusion_z` and wire it into the desk**

In `live_desk.py`, add the function directly above `fair_value_up` (around line 42) and refactor `fair_value_up` to reuse it:

```python
def diffusion_z(btc_now, btc_start, vol_per_s, seconds_left):
    """Diffusion moneyness z = ln(now/start)/(vol*sqrt(t_left)). None on degenerate inputs."""
    if not btc_now or not btc_start or btc_start <= 0 or not vol_per_s or vol_per_s <= 0 or seconds_left <= 0:
        return None
    return math.log(btc_now / btc_start) / (vol_per_s * math.sqrt(seconds_left))


def fair_value_up(btc_now, btc_start, vol_per_s, seconds_left) -> float | None:
    """P(BTC_end >= BTC_start) under a driftless diffusion: Phi(z). 'Where BTC already is' projected to close."""
    z = diffusion_z(btc_now, btc_start, vol_per_s, seconds_left)
    if z is None:
        return None
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
```

Then in `build_live_desk`, set `up_mid`/`down_mid` where the books are read (after line 308, inside the `if len(toks) == 2:` block, compute mids) and add `z` to the final `desk.update` (near line 317). Add to the book `desk.update` (line 310-313):

```python
    up_mid = (round((up_ask + up_bid) / 2, 4) if (up_ask is not None and up_bid is not None) else None)
    down_mid = (round((down_ask + down_bid) / 2, 4) if (down_ask is not None and down_bid is not None) else None)
    desk.update({"condition_id": condition_id, "up_ask": up_ask, "up_bid": up_bid,
                 "down_ask": down_ask, "down_bid": down_bid, "book_imbalance": imbalance_up,
                 "up_mid": up_mid, "down_mid": down_mid,
                 "microprice_gap": micro_gap_up, "tape_ofi_10s": flow.get("ofi_10s"),
                 "tape_ofi_30s": flow.get("ofi_30s"), "tape_n_10s": flow.get("tape_n_10s")})
```

And add `z` to the fair-value `desk.update` (line 317):

```python
    desk.update({"fair_value_up": (round(fair_up, 4) if fair_up is not None else None),
                 "z": diffusion_z(btc_now, btc_start, vol, left),
                 "market_up": (round(1 - down_ask, 4) if down_ask else (round(up_ask, 4) if up_ask else None)),
                 "edges": edges, "signal": entry_signal(edges)})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_crypto5m_live_desk.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/algos/polymarket_crypto5m/live_desk.py tests/test_crypto5m_live_desk.py
git commit -m "feat(crypto5m): expose diffusion z + side mids on the live desk (EV-gate inputs)"
```

---

### Task 3: `scalp_trader.decide` consumes the EV gate

**Files:**
- Modify: `sniper/algos/polymarket_crypto5m/scalp_trader.py` (`decide` signature + entry branch)
- Test: `tests/test_crypto5m_direction_scalp.py` (append)

**Interfaces:**
- Consumes: the `ev` dict produced by `reachability.entry_ev` (Task 1).
- Produces: `decide(..., ev=None)` — when `ev is None`, behavior is unchanged (ds-patient). When `ev` is provided and `ev["passed"]` is falsy, entry is blocked with an EV reason. The `enter` action dict gains an `"ev"` key (passthrough for logging).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_crypto5m_direction_scalp.py  (append)
from sniper.algos.polymarket_crypto5m import scalp_trader as ST

_CONFIRM = {"confirmed": True, "confirmed_side": "Up", "persistence_ticks": 3,
            "horizons_agree": True, "flow_agree": True}
_THESIS = {"side": "Up", "score": 0.6, "conviction": 0.6}
_BOOK = {"up_ask": 0.50, "up_bid": 0.49, "down_ask": 0.51, "down_bid": 0.50}


def test_ev_none_enters_unchanged():
    a = ST.decide(thesis=_THESIS, book=_BOOK, position=None, now_ts=1000, seconds_left=200,
                  confirm=_CONFIRM, ev=None)
    assert a["action"] == "enter" and a["side"] == "Up"


def test_ev_pass_enters_and_carries_ev():
    ev = {"passed": True, "ev_007": 0.01, "t_hold": 12.0, "reject_reason": None}
    a = ST.decide(thesis=_THESIS, book=_BOOK, position=None, now_ts=1000, seconds_left=200,
                  confirm=_CONFIRM, ev=ev)
    assert a["action"] == "enter" and a["ev"] is ev


def test_ev_fail_blocks_entry():
    ev = {"passed": False, "ev_007": -0.02, "t_hold": 5.0, "reject_reason": "ev_negative"}
    a = ST.decide(thesis=_THESIS, book=_BOOK, position=None, now_ts=1000, seconds_left=200,
                  confirm=_CONFIRM, ev=ev)
    assert a["action"] == "wait" and "EV gate" in a["reason"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_crypto5m_direction_scalp.py -q -k "ev_"`
Expected: FAIL — `decide() got an unexpected keyword argument 'ev'`.

- [ ] **Step 3: Modify `decide`**

Change the signature (line 72) to add `ev=None`:

```python
def decide(*, thesis, book, position, now_ts, seconds_left, stake_usdc=10.0, confirm=None, ev=None):
```

Then, inside the `if position is None:` block, AFTER the ask-band check (current line 85-87) and BEFORE the `return {"action": "enter", ...}` (line 88), insert the EV gate, and add `"ev": ev` to the enter dict:

```python
        if ev is not None and not ev.get("passed"):
            _ev007 = ev.get("ev_007")
            _th = ev.get("t_hold")
            return {"action": "wait", "best_side": side, "score": thesis.get("score"),
                    "reason": f"confirmed {side} but EV gate blocks ({ev.get('reject_reason')}): "
                              f"ev={'—' if _ev007 is None else f'{_ev007:+.4f}'} "
                              f"t_hold={'—' if _th is None else f'{_th:.0f}s'}",
                    "ev": ev}
        return {"action": "enter", "side": side, "price": ask, "kind": "entry",
                "shares": round(stake_usdc / ask, 4), "score": thesis.get("score"),
                "conviction": thesis.get("conviction"), "entry_prob": thesis.get("score"),
                "persistence": conf.get("persistence_ticks"), "ev": ev,
                "reason": f"CONFIRMED {side} (held {conf.get('persistence_ticks')} ticks, horizons+flow agree); "
                          f"take @ {ask:.2f}, ride until the direction turns"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_crypto5m_direction_scalp.py -q`
Expected: PASS (existing tests + 3 new; `ev=None` path unchanged).

- [ ] **Step 5: Commit**

```bash
git add sniper/algos/polymarket_crypto5m/scalp_trader.py tests/test_crypto5m_direction_scalp.py
git commit -m "feat(crypto5m): scalp_trader.decide consumes the EV gate (off when ev=None)"
```

---

### Task 4: `ev_observation_log.py` — counterfactual EV logging

**Files:**
- Create: `sniper/algos/polymarket_crypto5m/ev_observation_log.py`
- Test: `tests/test_crypto5m_ev_observation_log.py`

**Interfaces:**
- Produces (all take a `sqlite3.Connection`):
  - `ensure_schema(conn)`
  - `record_pending(conn, obs: dict) -> int` — inserts a `status='pending'` row; `obs` carries all input/gate fields + `mid_at_entry`/`bid_at_entry`/`ask_at_entry` + `resolve_at_ts`. Returns the new row id.
  - `due_pending(conn, window_start_ts, now_ts) -> list[sqlite3.Row]` — pending rows for that window with `resolve_at_ts <= now_ts`.
  - `complete_observation(conn, obs_id, *, mid, bid, ask, realized_ts, status='complete')` — fills `*_at_thold`, `realized_mid_move = mid - mid_at_entry`, `realized_ts`, `status`.
  - `sweep_incomplete(conn, now_ts) -> int` — marks still-`pending` rows whose window has closed (`window_start_ts + 300 <= now_ts`) as `status='incomplete'` (realized fields stay null). Returns count.
  - `admit_fraction(conn, engine_version) -> dict` — `{n, n_pass_007, frac_007, n_pass_realfee, frac_realfee}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_crypto5m_ev_observation_log.py
import sqlite3
from sniper.algos.polymarket_crypto5m import ev_observation_log as EVLOG


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    EVLOG.ensure_schema(c)
    return c


def _obs(ws, tick, *, resolve_at, mid, passed_007=0, passed_realfee=1):
    return {"engine_version": "ev-reach-v1", "campaign_id": "active-paper-v0", "window_start_ts": ws,
            "tick_ts": tick, "side": "Up", "condition_id": "0xc", "token_id": "t",
            "z_model": -0.2, "market_probit_z": -0.21, "conviction": 0.7, "vol_per_s": 0.0002,
            "seconds_left": 200, "side_ask": 0.45, "side_bid": 0.44, "side_mid": mid, "spread": 0.01,
            "delta": 1.2, "theta": 0.001, "t_star": 9.0, "t_hold": 12.0, "gain": 0.012, "gate_stat": 0.05,
            "cost_007": 0.027, "cost_realfee": 0.01, "ev_007": -0.015, "ev_realfee": 0.002,
            "passed_007": passed_007, "passed_realfee": passed_realfee,
            "mid_at_entry": mid, "bid_at_entry": 0.44, "ask_at_entry": 0.45, "resolve_at_ts": resolve_at}


def test_record_due_complete_roundtrip(tmp_path):
    c = _conn(str(tmp_path / "ev.sqlite"))
    oid = EVLOG.record_pending(c, _obs(1782000000, 1782000100, resolve_at=1782000112, mid=0.45))
    assert EVLOG.due_pending(c, 1782000000, 1782000110) == []          # not due yet
    due = EVLOG.due_pending(c, 1782000000, 1782000113)
    assert len(due) == 1 and due[0]["id"] == oid
    EVLOG.complete_observation(c, oid, mid=0.52, bid=0.51, ask=0.53, realized_ts=1782000114)
    row = c.execute("SELECT * FROM crypto5m_ev_observations WHERE id=?", (oid,)).fetchone()
    assert row["status"] == "complete"
    assert abs(row["realized_mid_move"] - 0.07) < 1e-9                  # 0.52 - 0.45


def test_pending_persists_across_reconnect(tmp_path):
    p = str(tmp_path / "ev.sqlite")
    c1 = _conn(p)
    EVLOG.record_pending(c1, _obs(1782000000, 1782000100, resolve_at=1782000112, mid=0.45))
    c1.close()
    c2 = _conn(p)                                                       # simulate restart
    assert len(EVLOG.due_pending(c2, 1782000000, 1782000113)) == 1


def test_sweep_incomplete_on_window_close(tmp_path):
    c = _conn(str(tmp_path / "ev.sqlite"))
    EVLOG.record_pending(c, _obs(1782000000, 1782000100, resolve_at=1782000112, mid=0.45))
    assert EVLOG.sweep_incomplete(c, 1782000000 + 100) == 0            # window not closed
    assert EVLOG.sweep_incomplete(c, 1782000000 + 301) == 1            # closed -> incomplete
    row = c.execute("SELECT status FROM crypto5m_ev_observations").fetchone()
    assert row["status"] == "incomplete"


def test_admit_fraction(tmp_path):
    c = _conn(str(tmp_path / "ev.sqlite"))
    EVLOG.record_pending(c, _obs(1, 11, resolve_at=99, mid=0.45, passed_007=0, passed_realfee=1))
    EVLOG.record_pending(c, _obs(1, 12, resolve_at=99, mid=0.45, passed_007=1, passed_realfee=1))
    af = EVLOG.admit_fraction(c, "ev-reach-v1")
    assert af["n"] == 2 and af["n_pass_007"] == 1 and af["frac_007"] == 0.5
    assert af["n_pass_realfee"] == 2 and af["frac_realfee"] == 1.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_crypto5m_ev_observation_log.py -q`
Expected: FAIL — `ModuleNotFoundError: ... ev_observation_log`.

- [ ] **Step 3: Write the module**

```python
# sniper/algos/polymarket_crypto5m/ev_observation_log.py
"""Counterfactual EV observation log for the crypto5m EV-gate (sec 5 of the design).

For EVERY confirmed-direction tick (whether the gate passes or not) we record the EV inputs/outputs under
BOTH fees plus the side's book at the decision tick, then resolve the realized MID move at tick_ts + t_hold.
That counterfactual is what (a) refits k to its true in-distribution value and (b) makes a near-zero admit
rate DECISIVE (cost-bound proof at realized fee) rather than ambiguous. Restart-safe: pending rows persist;
due rows are resolved from the live desk while the window is still current.

Standalone paper DB (data/crypto5m/active_trader_v0.sqlite) — inline SQL matches the existing active-engine
tables (this DB is not governed by db.py).
"""
from __future__ import annotations

_COLS = (
    "engine_version", "campaign_id", "window_start_ts", "tick_ts", "side", "condition_id", "token_id",
    "z_model", "market_probit_z", "conviction", "vol_per_s", "seconds_left",
    "side_ask", "side_bid", "side_mid", "spread",
    "delta", "theta", "t_star", "t_hold", "gain", "gate_stat",
    "cost_007", "cost_realfee", "ev_007", "ev_realfee", "passed_007", "passed_realfee",
    "mid_at_entry", "bid_at_entry", "ask_at_entry", "resolve_at_ts",
)


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS crypto5m_ev_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        engine_version TEXT, campaign_id TEXT, window_start_ts INTEGER, tick_ts INTEGER,
        side TEXT, condition_id TEXT, token_id TEXT,
        z_model REAL, market_probit_z REAL, conviction REAL, vol_per_s REAL, seconds_left INTEGER,
        side_ask REAL, side_bid REAL, side_mid REAL, spread REAL,
        delta REAL, theta REAL, t_star REAL, t_hold REAL, gain REAL, gate_stat REAL,
        cost_007 REAL, cost_realfee REAL, ev_007 REAL, ev_realfee REAL,
        passed_007 INTEGER, passed_realfee INTEGER,
        mid_at_entry REAL, bid_at_entry REAL, ask_at_entry REAL,
        resolve_at_ts INTEGER, status TEXT DEFAULT 'pending',
        mid_at_thold REAL, bid_at_thold REAL, ask_at_thold REAL,
        realized_mid_move REAL, realized_ts INTEGER)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ev_obs_pending ON crypto5m_ev_observations "
                 "(status, window_start_ts, resolve_at_ts)")
    conn.commit()


def record_pending(conn, obs: dict) -> int:
    cols = ", ".join(_COLS)
    ph = ", ".join("?" for _ in _COLS)
    cur = conn.execute(
        f"INSERT INTO crypto5m_ev_observations ({cols}, status) VALUES ({ph}, 'pending')",
        tuple(obs.get(c) for c in _COLS))
    conn.commit()
    return int(cur.lastrowid)


def due_pending(conn, window_start_ts, now_ts):
    return conn.execute(
        "SELECT * FROM crypto5m_ev_observations WHERE status='pending' AND window_start_ts=? "
        "AND resolve_at_ts <= ? ORDER BY id", (window_start_ts, now_ts)).fetchall()


def complete_observation(conn, obs_id, *, mid, bid, ask, realized_ts, status="complete"):
    row = conn.execute("SELECT mid_at_entry FROM crypto5m_ev_observations WHERE id=?", (obs_id,)).fetchone()
    entry_mid = row["mid_at_entry"] if row is not None else None
    move = (mid - entry_mid) if (mid is not None and entry_mid is not None) else None
    conn.execute(
        "UPDATE crypto5m_ev_observations SET mid_at_thold=?, bid_at_thold=?, ask_at_thold=?, "
        "realized_mid_move=?, realized_ts=?, status=? WHERE id=?",
        (mid, bid, ask, move, realized_ts, status, obs_id))
    conn.commit()


def sweep_incomplete(conn, now_ts) -> int:
    cur = conn.execute(
        "UPDATE crypto5m_ev_observations SET status='incomplete' "
        "WHERE status='pending' AND (window_start_ts + 300) <= ?", (now_ts,))
    conn.commit()
    return cur.rowcount


def admit_fraction(conn, engine_version) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(passed_007),0) p7, COALESCE(SUM(passed_realfee),0) pr "
        "FROM crypto5m_ev_observations WHERE engine_version=?", (engine_version,)).fetchone()
    n = row["n"] or 0
    p7 = row["p7"] or 0
    pr = row["pr"] or 0
    return {"n": n, "n_pass_007": p7, "frac_007": (p7 / n if n else 0.0),
            "n_pass_realfee": pr, "frac_realfee": (pr / n if n else 0.0)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_crypto5m_ev_observation_log.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/algos/polymarket_crypto5m/ev_observation_log.py tests/test_crypto5m_ev_observation_log.py
git commit -m "feat(crypto5m): EV observation log (counterfactual realized-move, dual-fee, restart-safe)"
```

---

### Task 5: Wire the EV gate + logging into the runner

**Files:**
- Modify: `scripts/run_polymarket_crypto5m_active_trader.py`
- Test: `tests/test_crypto5m_active_runner.py` (update the existing entry test for the gate; add an EV-gate/logging test)

**Interfaces:**
- Consumes: `reachability.entry_ev` / `EVConfig` (Task 1), `live_desk` desk keys `z`/`up_mid`/`down_mid` (Task 2), `scalp_trader.decide(..., ev=)` (Task 3), `ev_observation_log` (Task 4).
- Produces: module-level `EVCFG = RZ.EVConfig()` (monkeypatchable); `ENGINE_VERSION="ev-reach-v1"`; every confirmed tick writes a `crypto5m_ev_observations` row and resolves due rows.

- [ ] **Step 1: Update the existing runner test for the gate; add the EV-logging test**

Anchor edits on content, not line numbers (the file shifted this session). First, the shared `_desk` helper doesn't carry the EV-gate inputs — add `z`, `up_mid`, `down_mid` to its returned dict (`vol_per_s` is already present):

```python
            "z": 0.0, "up_mid": round((up_ask + up_bid) / 2, 4),
            "down_mid": round(((1 - up_bid) + (1 - up_ask)) / 2, 4),
```

The existing `test_deferred_fill_records_slip` read would be blocked by the strict default `k`; monkeypatch a passing `EVCFG` (high `k`; default `gate_on_realized=True` so cost = spread). Add right after the `monkeypatch.setattr(runner.time, "sleep", ...)` line:

```python
    monkeypatch.setattr(runner, "EVCFG", runner.RZ.EVConfig(k=0.6))  # strong ATM read clears the realized-fee gate
```

Then add a new test — a confirmed but far-OTM read (BTC ticking Up yet still a deep underdog: the 0.11 case) must be BLOCKED by the gate yet still LOGGED:

```python
# tests/test_crypto5m_active_runner.py  (append)
def test_far_otm_confirmed_tick_blocked_but_logged(tmp_path, monkeypatch):
    ws = 1782000000
    def desk(now):
        d = _desk(now, ws, up_ask=0.12, up_bid=0.11, ofi=0.6)   # Up signals confirm, but price is deep-underdog
        d.update({"z": -1.3, "up_mid": 0.115, "down_mid": 0.885, "vol_per_s": 0.0002, "seconds_left": 200})
        return d
    desks = iter([desk(ws + 100), desk(ws + 102), desk(ws + 104), desk(ws + 106)])
    monkeypatch.setattr(runner.LD, "build_live_desk", lambda *a, **k: next(desks))
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)
    monkeypatch.setattr(runner, "EVCFG", runner.RZ.EVConfig())   # defaults: gate on realized -> far-OTM fails
    conn = runner._connect(str(tmp_path / "active.sqlite"))
    runner.run(conn, poll_interval=2, max_iterations=4)
    # the confirmed tick was logged (gain computable) but the gate blocked entry
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_ev_observations").fetchone()[0] >= 1
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_active_positions").fetchone()[0] == 0
    row = conn.execute("SELECT passed_realfee FROM crypto5m_ev_observations LIMIT 1").fetchone()
    assert row["passed_realfee"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_crypto5m_active_runner.py -q`
Expected: FAIL — `AttributeError: module 'active_runner' has no attribute 'RZ'` / no `crypto5m_ev_observations` table.

- [ ] **Step 3: Wire the runner**

(a) Imports (after line 27):

```python
from sniper.algos.polymarket_crypto5m import reachability as RZ          # noqa: E402
from sniper.algos.polymarket_crypto5m import ev_observation_log as EVLOG  # noqa: E402
```

(b) Bump the version + add the config (replace line 35; add `EVCFG` below it):

```python
ENGINE_VERSION = "ev-reach-v1"   # reachability EV gate on entry (2026-06-25) — blank-slate count resets
# EV gate config. v1 gates on the REALIZED fee (gate_on_realized=True, default) so the engine trades its
# selective +EV-over-spread set on paper; the documented 0.07 column is ALWAYS logged as the real-money-
# readiness overlay. Set gate_on_realized=False to gate on 0.07 (real-money mode), no data change.
EVCFG = RZ.EVConfig()
```

(c) In `_connect` (before `c.commit()` at line 62): `EVLOG.ensure_schema(c)`.

(d) In the loop, right after `confirm = dstate.update(thesis, desk, now)` (line 220), compute the EV for confirmed ticks, log it, and resolve due observations:

```python
            ev = None
            if confirm.get("confirmed") and confirm.get("confirmed_side"):
                cside = confirm["confirmed_side"]
                s_ask = desk.get("up_ask") if cside == "Up" else desk.get("down_ask")
                s_bid = desk.get("up_bid") if cside == "Up" else desk.get("down_bid")
                s_mid = desk.get("up_mid") if cside == "Up" else desk.get("down_mid")
                ev = RZ.entry_ev(z=desk.get("z"), vol_per_s=desk.get("vol_per_s"),
                                 seconds_left=desk.get("seconds_left", 0),
                                 conviction=thesis.get("conviction"), side_ask=s_ask, side_bid=s_bid, cfg=EVCFG)
                obs = {"engine_version": ENGINE_VERSION, "campaign_id": CAMPAIGN_ID, "window_start_ts": ws,
                       "tick_ts": now, "side": cside, "condition_id": desk.get("condition_id"), "token_id": None,
                       "z_model": desk.get("z"), "market_probit_z": RZ.probit(s_mid),
                       "conviction": thesis.get("conviction"), "vol_per_s": desk.get("vol_per_s"),
                       "seconds_left": desk.get("seconds_left"), "side_ask": s_ask, "side_bid": s_bid,
                       "side_mid": s_mid, "spread": ev.get("spread"), "delta": ev.get("delta"),
                       "theta": ev.get("theta"), "t_star": ev.get("t_star"), "t_hold": ev.get("t_hold"),
                       "gain": ev.get("gain"), "gate_stat": ev.get("gate_stat"), "cost_007": ev.get("cost_007"),
                       "cost_realfee": ev.get("cost_realfee"), "ev_007": ev.get("ev_007"),
                       "ev_realfee": ev.get("ev_realfee"),
                       "passed_007": 1 if ev.get("passed_007") else 0,
                       "passed_realfee": 1 if ev.get("passed_realfee") else 0,
                       "mid_at_entry": s_mid, "bid_at_entry": s_bid, "ask_at_entry": s_ask,
                       "resolve_at_ts": now + int(ev.get("t_hold") or 0)}
                if ev.get("gain") is not None:   # log only computable-EV ticks (skip degenerate/no-room/band blanks)
                    EVLOG.record_pending(conn, obs)
            # resolve any EV observations whose t_hold has elapsed (same window, book still live)
            for o in EVLOG.due_pending(conn, ws, now):
                rs = o["side"]
                EVLOG.complete_observation(conn, o["id"],
                    mid=(desk.get("up_mid") if rs == "Up" else desk.get("down_mid")),
                    bid=(desk.get("up_bid") if rs == "Up" else desk.get("down_bid")),
                    ask=(desk.get("up_ask") if rs == "Up" else desk.get("down_ask")), realized_ts=now)
            EVLOG.sweep_incomplete(conn, now)
```

(e) Pass `ev` into `decide` (modify line 226-227):

```python
            action = ST.decide(thesis=thesis, book=book, position=pos, now_ts=now,
                               seconds_left=desk.get("seconds_left", 0), stake_usdc=STAKE, confirm=confirm, ev=ev)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_crypto5m_active_runner.py -q`
Expected: PASS (updated deferred-fill test + new EV-logging test).

- [ ] **Step 5: Commit**

```bash
git add scripts/run_polymarket_crypto5m_active_trader.py tests/test_crypto5m_active_runner.py
git commit -m "feat(crypto5m): runner wires EV gate + counterfactual logging; ENGINE_VERSION=ev-reach-v1"
```

---

### Task 6: Surface admit-fraction + realized-vs-modeled in the review

**Files:**
- Modify: `scripts/crypto5m_active_review.py` (add an EV-gate section to the cron'd readout)

**Interfaces:**
- Consumes: `ev_observation_log.admit_fraction` (Task 4) and the `crypto5m_ev_observations` table.
- Produces: review output lines for the EV gate (admit fraction at both fees; mean realized mid-move vs modeled gain for completed rows — the k-validation signal).

- [ ] **Step 1: Add the EV-gate readout block**

Locate where `crypto5m_active_review.py` opens the active-engine sqlite connection and prints its summary; add this block using that same connection (`conn`), guarded so it degrades if the table is absent:

```python
    try:
        from sniper.algos.polymarket_crypto5m import ev_observation_log as EVLOG
        af = EVLOG.admit_fraction(conn, "ev-reach-v1")
        print(f"\n[EV gate ev-reach-v1] confirmed ticks logged: {af['n']}  "
              f"admit@0.07: {af['n_pass_007']} ({100*af['frac_007']:.1f}%)  "
              f"admit@realized-0: {af['n_pass_realfee']} ({100*af['frac_realfee']:.1f}%)")
        row = conn.execute(
            "SELECT COUNT(*) n, AVG(realized_mid_move) rm, AVG(gain) g "
            "FROM crypto5m_ev_observations WHERE status='complete'").fetchone()
        if row and row["n"]:
            print(f"[EV gate k-fit] completed obs: {row['n']}  mean realized mid-move: {row['rm']:+.5f}  "
                  f"mean modeled gain: {row['g']:+.5f}  (realized/modeled ~ k-scale check)")
        else:
            print("[EV gate k-fit] no completed observations yet")
    except Exception as exc:  # noqa: BLE001 — review must not crash on an absent/empty EV table
        print(f"[EV gate] readout unavailable: {exc}")
```

- [ ] **Step 2: Smoke-run the review against the live paper DB**

Run: `python3 scripts/crypto5m_active_review.py 2>&1 | tail -20`
Expected: prints the existing summary plus the `[EV gate ...]` lines (admit fractions; "no completed observations yet" until the engine has run).

- [ ] **Step 3: Commit**

```bash
git add scripts/crypto5m_active_review.py
git commit -m "feat(crypto5m): EV-gate admit-fraction + k-fit signal in the active review readout"
```

---

### Task 7: Index refresh + full crypto5m regression

**Files:**
- Modify (generated): `index/schema_registry.json`, `index/dependency_index.json` if the new table/files changed them.

- [ ] **Step 1: Regenerate the brain index (new table + new modules)**

Run: `python3 scripts/brain_index.py generate`

- [ ] **Step 2: Validate schema refs (spec + plan name columns)**

Run: `python3 scripts/validate_schema_refs.py --files docs/superpowers/specs/2026-06-25-crypto5m-reachability-gated-entry-exit-design.md docs/superpowers/plans/2026-06-25-crypto5m-reachability-gated-entry.md`
Expected: `No schema violations found.`

- [ ] **Step 3: Run the full crypto5m regression**

Run: `python3 -m pytest tests/test_crypto5m_reachability.py tests/test_crypto5m_ev_observation_log.py tests/test_crypto5m_live_desk.py tests/test_crypto5m_direction_scalp.py tests/test_crypto5m_active_runner.py tests/test_crypto5m_direction_state.py -q`
Expected: PASS (all).

- [ ] **Step 4: Commit any index changes**

```bash
git add index/schema_registry.json index/dependency_index.json
git commit -m "chore(crypto5m): regen brain index for EV-gate table + modules" || echo "no index changes"
```

---

## Operational notes (read before launching the live engine)

- **v1 gates on the realized fee → the engine trades.** `gate_on_realized=True` (default) makes the live decision use the realized-fee EV (cost = spread), so the engine takes its selective +EV-over-spread set on paper and produces a live forward stream. The documented `0.07` column is ALWAYS logged as the real-money-readiness overlay (`passed_007`/`ev_007`). Because every confirmed tick logs the counterfactual realized move (passed or not), the EV log is a **concurrent selection test** on the same market data — no sequential live A/B needed to answer "does the gate pick +EV ticks"; live fills test execution on top. For real-money readiness, read the `*_007` columns / set `gate_on_realized=False` (no data change).
- **Restart-safe:** pending observations persist; due rows resolve from the live desk while the window is current; stragglers past close are swept to `incomplete`.
- **Do not re-tune the exit** in this version (one lever per `ENGINE_VERSION`). Watch the exit-kind mix in the readout; if the give-back leaks on the cleaner (near-ATM) position distribution, flag it for the next version.
- **The maker pivot is only triggered** if, at the refit `k` AND the realized (~0) fee, the gate still rejects ~everything (read off the `*_realfee` columns / `admit@realized-0`).

## Self-Review

- **Spec coverage:** EV gate (Task 1) ✓ · z exposure (Task 2) ✓ · gate consumed by decide, entry-only/exits-untouched (Task 3) ✓ · counterfactual dual-fee logging, mid-move-for-k, incomplete-on-close, restart-safe (Task 4) ✓ · runner wiring + ENGINE_VERSION + market-probit-z + capped-form-only + fail-closed (Task 5) ✓ · admit-fraction readout (Task 6) ✓ · index/regression (Task 7) ✓. The v3 EV-collapse exit and any exit re-tune are explicitly out of scope (spec §3) — no task, by design.
- **Placeholder scan:** none — every code/test step has complete content.
- **Type consistency:** `entry_ev` returns the dict consumed verbatim by `decide` (`ev.get("passed")`) and by the runner's `obs` builder (keys `spread/delta/theta/t_star/t_hold/gain/gate_stat/cost_007/cost_realfee/ev_007/ev_realfee/passed_007/passed_realfee`); `EVConfig` field names (`k`, `live_fee_rate`, `t_ceiling_s`, …) match across Tasks 1/5; `ev_observation_log` `_COLS` match the `obs` dict keys built in Task 5 and the schema in Task 4.
