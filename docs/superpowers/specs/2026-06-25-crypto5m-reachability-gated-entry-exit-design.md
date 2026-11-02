# Crypto5m reachability-gated entry (EV gate) — design

**Date:** 2026-06-25
**Status:** design (brainstorming complete; pending writing-plans)
**Scope:** the live ACTIVE scalper for the Polymarket BTC Up/Down 5-minute binary
(`sniper/algos/polymarket_crypto5m/`). Entry logic only in v1; exits untouched.
**Supersedes nothing.** Builds on the direction-state scalper (`ds-patient-v1`,
commit `6d24dff` line) documented in memory `project-crypto5m-active-engine`.

---

## 1. Problem

The live scalper (`scalp_trader.py` + `direction_state.py`) enters on a *confirmed
direction* (persistence ≥3 ticks, ret_15s+ret_30s agree, tape order-flow agrees,
conviction ≥0.50) and rides to a take-profit / patience-stop / reversal exit. It is
**cost-bound and net-negative** (−$180 / 388 paper trades lifetime; recent
direction-state trades ~break-even).

The diagnostic failure mode (operator-observed, e.g. a −$7.27 trade): the engine bought
**Up at 0.11** — a deep underdog far below the window target with little time left — because
the directional read said "up." BTC may even have ticked up, but:

1. a 0.11 binary barely responds to a small BTC move (**low delta**), and
2. with the clock running out, P(reach target) decays to 0 regardless of direction
   (**theta / time-decay**).

Direction was confirmed; the odds were unreachable. **The directional read is necessary
but not sufficient** — a correct read only pays if the binary's odds can actually move
more than the round-trip cost in the time left. The current gate never asks that question.

## 2. Core idea — gate entries on expected odds-move EV, not just direction

The engine already computes a driftless-diffusion fair value
(`live_desk.fair_value_up`): `P(Up) = Φ(z)`, `z = ln(BTC_now / BTC_start) / (vol·√t_left)`.
That single model yields the two missing quantities analytically:

- **delta** (odds gained per unit BTC log-move): `δ = φ(z) / (vol·√t_left)`
- **theta** (odds bled per second of time passing): `|θ| = φ(z)·|z| / (2·t_left)`

The directional read supplies the expected *signed* move. Modeling expected move as a
shrunk fraction of the diffusion scale:

```
E[signed log move over horizon t_hold] ≈ k · conviction · vol · √t_hold
```

where `conviction ∈ [0,1]` is the engine's existing `|score|·agreement`, `vol·√t` is the
(robust, live) diffusion scale, and `k` is one shrinkage constant ("how much of the vol
scale our weak signal actually predicts"; biased low — see §6).

The expected-value of an entry held for `t_hold`, per share, in odds (price) units:

```
EV(t_hold) = A·√t_hold − B·t_hold − cost
  A = δ · k · conviction · vol  =  φ(z)·k·conviction / √t_left   (gain coefficient)
  B = |θ|                       =  φ(z)·|z| / (2·t_left)          (decay/sec)
  cost = spread + fee                                            (round-trip crossing + entry fee)
```

`A·√t_hold` (gain) grows sublinearly; `B·t_hold` (decay) grows linearly → `EV` is
**concave in t_hold** with a closed-form optimum:

```
t* = (A / 2B)²  =  k²·conviction²·t_left / z²
EV* = A²/(4B) − cost  =  φ(z)·k²·conviction² / (2·|z|) − cost      (the interior case)
```

**Enter iff EV(t_hold) > 0** (in addition to the existing confirm gate).

### 2.1 The hold cap (why EV\* alone is wrong)

`EV*` diverges as `z→0` (ATM): `|θ|→0`, `t*→∞`. That is unphysical — you cannot hold past
the window close. The always-valid form evaluates EV at a **capped hold**:

```
t_hold = clamp(t*, t_min, min(seconds_left − EXIT_BUFFER_S, T_CEILING_S))
EV     = A·√t_hold − B·t_hold − cost
```

Behaviour:
- **ATM with room** → `t*` huge → cap binds → `EV = A·√t_cap − B·t_cap − cost` (finite, large) → pass.
- **far-OTM near expiry** (the 0.11) → `t*` tiny, `A√t` small, `cost` ~2c → `EV < 0` → reject.
- **interior** (`t* < cap`) → reduces to the clean `EV* = φ(z)·k²·conviction²/(2|z|) − cost`.

Time-left re-enters the decision **through the cap**: when the clock caps the hold below
`t*`, EV drops, so near-expiry-far-OTM dies while mid-window-with-room survives — exactly
the operator's "4 minutes left → there's a chance" intuition, falling out of the cap.

`T_CEILING_S` (~30–60s) also bounds the constant-σ / linear-θ approximation: we never claim
more diffusion than a short hold supports. (Longer-hold EV would need the exact Φ
difference rather than the δ/θ linearization — out of scope for v1.)

### 2.2 Equivalent gate form (k as the strictness knob)

Rearranging the interior test:

```
enter iff   conviction² · φ(z)/|z|  >  2·(spread + fee) / k²
```

`cost` stays explicit (measured live per trade). `k` is the **single strictness knob**, not
a physics constant we must measure. Because `EV* ∝ k²`:
- **k too low → gate too strict** (rejects marginal trades) — the *safe* failure mode.
- **k too high → fabricates EV** (admits junk) — the *dangerous* one.
So `k` is biased low (default `0.10`, low end of the `0.10–0.15` anchor from the live
fusion's measured OOS corr ~0.09–0.21).

The gate is **quadratic in conviction** (the `conviction²` from `EV* ∝ A²`), so it
concentrates hard on the strongest reads — steeper selectivity than a linear bar, which is
what we want.

### 2.3 Side, moneyness, and the price band

- `z` is computed **from the diffusion directly** (`z = ln(now/start)/(vol·√t_left)`, already
  inside `fair_value_up`) — not the probit of the market price. Avoids an inverse-normal and
  keeps δ/θ consistent with the same model. `φ(z)` and `|z|` are symmetric in side, so the
  gate statistic is a property of **moneyness**, not side; the **side is still chosen by the
  directional thesis**. fair≈market for this efficient binary, so model-z ≈ market-z.
  We additionally **log the market-probit-z** (`Φ⁻¹(side_mid)`) alongside model-z
  (logging only — not in the live decision path) to validate fair≈market empirically;
  if they diverge materially we switch to market-probit-z in v2 (see risk 3).
- `φ(z)/|z|` peaks at-the-money and falls off fast, so the EV gate **emerges its own
  conviction-adaptive price band** — it subsumes the current hardcoded `0.08–0.92` ask
  bounds. A wide hard price band (`[0.05, 0.95]`) is retained only as a catastrophic
  guardrail against `k` miscalibration, not as the operative filter.

## 3. Scope decisions (locked in brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Entry mechanism | **EV gate** (capped `EV > 0`) on top of the existing confirm gate | Correct objective; subsumes the dead-underdog veto for free |
| Exit in v1 | **Unchanged** (ds-patient: reversal / trailing give-back / patience-stop / catastrophe / timeout) | One lever per `ENGINE_VERSION`; the entry gate already prevents the bleed at the source (you never hold the 0.11) |
| Hold horizon | **EV-maximizing `t*`, capped** to the window | Analytic optimum, no arbitrary fixed hold; entry-only (not wired to exit → no entry↔exit coupling) |
| k | **Fixed `0.10`, biased low**, refit from live logs later | No offline capture; low-k errs strict/safe; logs make a near-zero admit *decisive* not ambiguous |
| Admit rate | **Reported as a diagnostic**, never a setter | Targeting an admit fraction would decouple k from cost-clearance |

**Explicitly out of scope for v1** (deferred, each its own clean experiment):
- **EV-collapse exit** (v3): bail when *continuation*-EV flips negative mid-hold — a
  good-EV entry (ATM, time left) that drifts against us into now-OTM-less-time without a
  sharp reversal or a deep-enough adverse move to trip the ds-patient stops. The legit
  residual the entry gate cannot cover. Logged for v3, not built in v1.
- **Exit re-tune.** The EV gate changes the held-position distribution (concentrated near
  ATM with room); the ds-patient exit params (trailing give-back, patience-stop) were
  implicitly tuned on the old junk-entry distribution and may be slightly miscalibrated for
  the cleaner one. **Not re-tuned in v1** — but the exit-kind mix is watched in the readout
  and flagged for a later version if the give-back leaks.

## 4. Architecture

```
live_desk.build_live_desk()                # already computes z, vol_per_s, seconds_left,
   └─ expose: z (diffusion moneyness)       #   fair_value_up, up/down ask+bid
                                            #   → add `z` to the desk dict
direction_state.DirectionState.update()    # UNCHANGED — confirm gate (signal reality)
reachability.entry_ev(...)                 # NEW pure module (economic gate)
   in:  z, vol_per_s, seconds_left, conviction, side_ask, side_bid,
        k, fee_rate, t_min, exit_buffer_s, t_ceiling_s, price_band
   out: {passed, ev, t_hold, t_star, delta, theta, gate_stat, cost, reject_reason}
scalp_trader.decide(...)                   # consume entry_ev AFTER confirm gate:
   confirm.confirmed AND entry_ev.passed → enter; else wait (reason carries EV detail)
run_polymarket_crypto5m_active_trader.py   # plumb desk scalars into decide;
   ENGINE_VERSION = "ev-reach-v1"           #   + the counterfactual EV logger (§5)
```

**Separation of concerns:** `direction_state` answers *"is the direction real?"*;
`reachability` answers *"is it worth trading economically?"*. Both must pass. The new
economic gate lives in its own pure, unit-testable module (no I/O), mirroring how the
confirm gate is isolated.

## 5. Counterfactual logging (the most important non-obvious requirement)

For **every confirmed-direction tick — whether the EV gate passes or not** — persist:

- inputs: `window_start_ts`, `tick_ts`, `side`, `z` (model), `market_probit_z`
  (`Φ⁻¹(side_mid)`, logging-only — §2.3), `conviction`, `vol_per_s`, `seconds_left`,
  `side_ask`, `side_bid`, `side_mid`, `spread`, `k`
- **dual-fee gate outputs:** `delta`, `theta`, `t_hold`, `t_star`, `gate_stat`, plus
  EV **and** the admit decision under **both** fees — `ev_007`, `passed_007` (documented
  0.07) and `ev_realfee`, `passed_realfee` (realized ~0) — and `reject_reason`. **Which
  fee gates the LIVE decision is a config selector (`gate_on_realized`), decoupled from
  what's logged — both fee columns are ALWAYS logged regardless.** v1 launches gating on
  the **realized (~0) fee** (paper's true cost is the spread; the 0.07 fee isn't charged on
  paper, so gating on it would freeze the engine for a phantom cost), while the documented
  0.07 column is retained as the **real-money-readiness overlay**. The cost-bound/maker
  verdict (§5 below, §7) is read off `*_realfee`; real-money readiness is read off `*_007`.
  Flip `gate_on_realized=False` to gate on documented 0.07 with no data change.
- **realized counterfactual move:** the side's `mid`, `bid`, `ask` at `tick_ts` and again
  at `tick_ts + t_hold`. The **mid-move** (`mid@t_hold − mid@t`) is what calibrates `k`
  (`k` fits `E[odds move]` = mid move; logging the ask→bid crossing would double-count the
  spread that `cost` already carries → biased-low `k`). `bid`/`ask` at both stamps let us
  separately reconstruct the realized crossing for **cost validation**. If the window closes
  before `tick_ts + t_hold`, record the realized move **to close** and mark the row
  `incomplete` (partial is still signal for the k fit — never drop it).

This is what makes the run self-calibrating and a near-zero admit rate **decisive rather
than ambiguous**:

- It lets us **refit `k`** to its true in-distribution value from realized moves over the
  whole live horizon (more samples than any short pre-launch shadow window could give).
- If low-k rejects ~everything → refit from the logs. If the **refit true-k still rejects
  ~everything → that is the cost-bound proof at the entry level** (no confirmed tick's
  expected odds-move clears spread+fee) → the taker cannot work → the **maker pivot is the
  real path**. A result, not a dead engine. **Crucially, this verdict is read off the
  `*_realfee` (realized ~0) columns, not the live `0.07` gate** — at 0.07 (≈2× the real
  cost) the gate could reject a genuinely +EV set and send us to maker for the wrong reason.
  If even at realized-0 the refit-k gate rejects ~everything → the taker is truly dead →
  maker. If realized-0 admits a +EV set → the taker works at the real fee, and **resolving
  the fee-conflict (0.07 vs 0) becomes the gating question instead** — a very different
  outcome than "go maker."

**Implementation note (the bulk of the work):** `t_hold` is ~10–40s = several poll ticks
ahead, so the realized-move capture needs a **pending-observation buffer** keyed by
`(window_start_ts, tick_ts)`, resolved when `now ≥ obs_ts + t_hold` by reading the live
side price; restart-safe (persist pending rows). New table (proposed) holding the columns
above; reuse the existing `data/crypto5m/active_trader_v0.sqlite`.

The **admit fraction** (passed / confirmed) at the live `k` is computed from these logs and
surfaced in the readout as a diagnostic (are we trading 30% of confirmed ticks, or 0.3%?).

## 6. Calibration & defaults

| Constant | Default | Notes |
|---|---|---|
| `k` (shrinkage / strictness) | `0.10` | biased low; refit from §5 logs for the next version |
| `documented_fee_rate` | `0.07` | per-share `0.07·p·(1−p)`; **always logged** (`ev_007`/`passed_007`) as the real-money-readiness overlay |
| `realized_fee_rate` | `0.0` | paper's true cost (spread only); **always logged** (`ev_realfee`/`passed_realfee`); the cost-bound/maker verdict reads this |
| `gate_on_realized` | `True` (v1) | selects which fee drives the LIVE `passed`; v1 gates on realized-0 so the engine trades; `False` = gate on documented 0.07 (real-money mode), no data change |
| `t_min` | `5 s` | floor on modeled hold |
| `EXIT_BUFFER_S` | `20 s` | mirror `MIN_SECONDS_LEFT`; never model holding into close |
| `T_CEILING_S` | `30 s` | low end — matches the seconds-long ds-patient hold distribution; bounds the constant-σ/linear-θ approximation; a higher ceiling inflates the `A·√t_hold` gain → biases permissive, so we hold it low |
| hard price band | `[0.05, 0.95]` | catastrophic guardrail only; EV gate is the operative band |

All exposed as config (env/CLI) with these defaults; `k`, `EXIT_BUFFER_S`, `T_CEILING_S`
are the ones we expect to touch.

## 7. A/B method

- New `ENGINE_VERSION = "ev-reach-v1"`; trades tagged by version in the existing campaign DB.
- Compare `ev-reach-v1` forward window vs the `ds-patient-v1` baseline using the
  per-version counting already built. Primary metric is **PnL/EV per trade after fee**
  (not win rate); secondary: exit-kind mix (watch for give-back leak, §3), admit fraction,
  and realized-vs-modeled odds-move (k validation).
- Pre-registered: the entry gate's job is to make the **average admitted trade clear
  spread+fee**. Success = `ev-reach-v1` avg-PnL/trade > `ds-patient-v1` and > 0 after fee
  over a meaningful sample; near-zero admits with the refit k = the cost-bound result —
  **but evaluated at the realized (~0) fee, not the live 0.07 gate** (§5). The maker pivot
  is only triggered if the realized-fee refit-k gate still rejects ~everything.

## 8. Testing

Pure-module unit tests (`tests/test_crypto5m_reachability.py`):
- the 0.11-far-OTM-near-expiry case → `EV < 0`, rejected (regression for the −$7.27).
- ATM-with-room → cap binds, finite positive `EV`, passed.
- interior case → `EV` matches the closed-form `φ(z)·k²·conviction²/(2|z|) − cost`.
- monotonicity: ↑conviction → ↑EV (quadratically); ↑cost → ↓EV; ↓time-left → ↓EV via cap.
- `k→0` rejects all; large `k` admits the interior set (strictness-knob behaviour).
- `t_hold` clamping at both ends (`t_min`, `seconds_left − buffer`, `T_CEILING`).
- guardrail price band rejects outside `[0.05,0.95]` regardless of EV.
Plus a deterministic test that `scalp_trader.decide` enters only when **both** confirm and
EV gate pass, and a restart-safe test for the pending-observation logger.

## 9. Risks / honest caveats

1. **k is a fitted knob with weak signal** (R²~0.04). Mitigated by biasing low and by the
   refit-from-logs loop; a wrong-low k under-trades (safe), and the logs make that
   distinguishable from a true cost-bound result.
2. **Constant-σ / linear-θ approximation** only holds for short holds → bounded by
   `T_CEILING_S`; longer holds would need the exact Φ difference (deferred).
3. **model-z ≈ market-z assumption** — relies on fair≈market for this binary. Two
   structural caveats, both observable from the logs (we record model-z, market-probit-z,
   and book prices): (a) model-z's *strike* is the **Binance window-start proxy** — the
   Chainlink start is `None` live (known gotcha), so the strike is an approximation of the
   settlement reference; (b) model-z depends on the **noisy live `vol` estimate**, whereas
   market-price is a direct observable. If model-z and market-probit-z diverge materially
   we switch to market-probit-z in v2.
4. **fee conflict** (documented 0.07 vs realized 0) — using documented makes the gate
   conservative (slightly too strict), the safe direction.
5. **Exit-distribution mismatch** (§3) — watched in the readout, not fixed in v1.
6. **This is paper-only.** The gate improves *selection*; it does not address taker
   execution at size (queue/slippage) — that remains the separate, later question, and the
   maker pivot is the explicit fallback if the entry gate proves the taker cost-bound.

## 10. Implementation notes (for writing-plans, not spec blockers)

1. **Live path uses the capped `EV(t_hold)` form only — never the `÷|z|` interior
   closed-form.** At z→0 (ATM, BTC≈start) `|z|→0` makes `EV* = φ(z)k²conv²/(2|z|)` divide
   by zero, but the capped form is well-behaved there (`B=|θ|→0`, no decay, finite EV via
   the cap). The `÷|z|` closed-form (§2.1/§2.2) is for **tests and intuition only**.
2. **Fail-closed on degenerate inputs.** `vol_per_s` 0/None, `seconds_left ≤ 0`, missing
   `z`, or non-positive `btc_start` → `passed=False` with a `reject_reason`; never divide
   by them. (Mirrors `fair_value_up`'s own guards.)
3. **Observation-buffer resolution at window close.** If the window closes before
   `tick_ts + t_hold`, resolve the pending observation with the realized move **to close**
   and mark it `incomplete` (don't drop — partial is still signal for the k fit). Buffer is
   restart-safe (persist pending rows; re-resolve on restart).
