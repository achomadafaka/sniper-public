# Crypto5m drift-corrected first-passage reachability — SHADOW model (design)

**Date:** 2026-06-26
**Status:** design, pre-implementation
**Branch (proposed):** `feat/crypto5m-drift-reachability`
**Supersedes premise of:** the two-sided / exit-aware / memory taker design (killed 2026-06-26 — see `finding/feature-study/crypto5m-reachability-viability-2026-06-26`)

---

## 1. Why this exists (the kill that motivates it)

The empirical viability study (2026-06-26) killed three taker ideas and, crucially, told us *why* the current `ev-reach-v1` gate is decoration:

- **Two-sided scan is EV-neutral by construction.** The gate's signal terms `A=φ(z)·k·conviction/√t_left`, `B=φ(z)·|z|/(2·t_left)` are symmetric in `z` (`φ(−z)=φ(z)`), so `gain` is identical for Up and Down. The model has **no directional asymmetry** — `conviction` enters only as a magnitude, never as a *direction*.
- **The gate doesn't predict realized odds-moves.** `gain` vs `realized_mid_move`: Spearman rho **+0.024**, clustered p **0.76** (n=1185 / 197 windows). The passing bucket nets +0.002 with a ±0.11c CI.

The theory research named the fix precisely: the current model is **driftless**, and a driftless first-passage problem is symmetric — it *cannot* produce a directional edge. The **Girsanov drift correction** `exp(2μb/σ²)` is the only thing that breaks Up/Down symmetry. So the redesign is not "more machinery on the gate" — it is **replacing `conviction`-as-a-fake-magnitude with a real, measured drift `μ`, in the proper drift-corrected first-passage formula**, and proving (in shadow) whether *that* predicts realized moves.

**v1 hypothesis (the one thing this tests):** a reachability model fed a *measured directional drift* `μ` predicts realized favorable odds-moves materially better than the driftless `ev-reach-v1` null (rho ≈ 0).

If true → a later phase earns the right to wire it live. If false → taker reachability is dead for good, honestly, and we stop.

## 2. Boundary (what v1 is and is NOT)

**IN:**
- A pure first-passage / drift-corrected reachability module (no I/O).
- A pure, pre-registered **drift estimator** `μ̂` from signals already on the desk (tape OFI, EMA slope, kline flow).
- **Instrumentation**: log intra-trade / per-tick mid-prices so realized favorable-move is measurable (the study couldn't test exits because we don't log this).
- A **shadow log** of the drift model's prediction next to the live `ev-reach-v1`, resolved with the realized move.
- An offline **validation harness** + a pre-registered milestone gate, mirroring the brain-v2 pattern.

**OUT (explicitly, to prevent scope drift):**
- **No live-decision change.** The engine keeps deciding on `ev-reach-v1`. Zero behavior change, enforced like brain-v2.
- **No two-sided scan** (proven EV-neutral) and **no exit-EV gate** (untestable until the instrumentation accrues; revisit only with data).
- **No maker / Avellaneda–Stoikov** work (that's the other fork; separate spec if chosen later).
- **No drift-calibration overfitting**: the v1 estimator is a simple, pre-registered mapping with heavy shrinkage; coefficients are fixed before looking at the holdout, fit only on a chronological train split.

## 3. The math (grounded in the first-passage research)

Notation: `S₀` window-start BTC, `S` now, `σ` per-second log-return vol, `τ` seconds left, `z = ln(S/S₀)/(σ√τ)`, fair value `p = Φ(z)`.

### 3.1 Measured drift `μ̂` (the crux — replaces fake `conviction`)
`μ̂` is an estimate of BTC's **per-second log-price drift** over the next ~`t_hold` seconds, from real flow signals. v1 (pre-registered, simple, shrunk):

```
μ̂ = k_drift · shrink · g(signals)            # log-price units per second
g(signals) = standardized blend of: tape_ofi_10s, ema_slope (per s), volume_dir
```

- `k_drift` and `shrink ∈ (0,1]` are **fixed constants** (documented; chosen conservative so the model can't manufacture confidence). The walk-forward tape finding (+0.21 OOS corr) is the prior that flow has *some* drift signal; v1 tests whether even a simple shrunk blend beats the driftless null.
- Degenerate / missing signals → `μ̂ = 0` (fail-closed → the model collapses to the driftless baseline, never worse than today).

### 3.2 Drift-corrected first-passage (reachability of a favorable move)
Favorable barrier distance `b > 0` in log-price units (how far BTC must travel for the held/considered side's odds to reach a bankable target). Touch probability before expiry (Girsanov-corrected reflection principle):

```
P_touch(b, σ, τ, μ) = Φ(−(b − μτ)/(σ√τ)) + exp(2μb/σ²)·Φ(−(b + μτ)/(σ√τ))
```

At `μ=0` this is the symmetric `2·Φ(−b/(σ√τ))` — the driftless baseline. `μ>0` (drift toward the barrier) raises it; `μ<0` lowers it. **This is where Up and Down stop being identical.**

### 3.3 Drift-aware expected favorable move (the validated quantity)
The directly testable prediction (vs `realized_mid_move`) is the **expected odds-move over `t_hold` under drift**:

```
E[Δp_fav] ≈ delta · μ̂ · t_hold      # delta = φ(z)/(σ√τ) = odds gained per unit log-move
```

The driftless `ev-reach-v1` had no `μ̂` term here (its `gain` used the √t diffusion magnitude × `conviction`, not a signed drift). The redesign's prediction is **signed and asymmetric**. We log both `E[Δp_fav]` and `P_touch` per tick and resolve against the realized move.

### 3.4 Exit heuristic (logged only, not acted on in v1)
From the gamma-theta identity: `max efficient hold ≈ 4τ/(z²σ²)`. Logged as a diagnostic so the eventual exit phase has data; **does not gate anything in v1.**

**Honest caveats (documented in-code):**
- The barrier `z*` moves as `τ→0` (the price polarizes); `P_touch` uses a fixed-barrier reflection approximation — fine for a *shadow* prediction over a ≤30s `t_hold`, flagged as an approximation.
- **Jumps** violate the continuous-path assumption (touch prob is then an under-estimate); `regime_vol.jump_flag` is logged alongside so the validation can bucket jump vs no-jump.
- **The drift estimate is the entire bet.** If `μ̂` has no signal, the model correctly collapses toward the driftless null — and the validation will say so.

## 4. Components

| Module | Type | Responsibility |
|---|---|---|
| `sniper/algos/polymarket_crypto5m/drift_reachability.py` | NEW, pure | `drift_estimate(signals)→μ̂`; `touch_prob(b,σ,τ,μ)`; `expected_fav_move(z,σ,τ,μ,t_hold)`; `max_hold_s(z,σ,τ)`. No I/O. |
| `live_desk.build_live_desk` | EXTEND | compute `μ̂` + the drift-reachability fields in SHADOW (like the brain-v2 block at `live_desk.py:368-380`); fail-closed to None. |
| `run_polymarket_crypto5m_active_trader.py` | EXTEND | (a) **instrument**: log per-tick mid for any open position + the resolve-at-t_hold mid; (b) write the drift-reachability shadow row each confirmed tick. No decision change. |
| `drift_reach_log.py` | NEW | shadow table `crypto5m_drift_reach` (prediction + realized), mirroring `ev_observation_log` / `z_shadow_log`. |
| `scripts/crypto5m_drift_reach_validation.py` | NEW | offline: does `E[Δp_fav]` predict `realized_mid_move`, clustered by window, beating the driftless null? Chronological split; pre-registered milestone. |
| `scripts/crypto5m_drift_reach_progress.py` | NEW | hourly one-line readout (mirror `crypto5m_brain_progress.py`) + cron. |

## 5. Instrumentation (the no-regret piece, fixes the study's blind spot)
Add to `crypto5m_active_positions` (idempotent ALTERs) and/or a `crypto5m_position_marks` table: per open position, the mid at t = entry, +5s, +10s, +20s, +30s, and at exit. This makes the exit-timing counterfactual (study Q2, "CANNOT TEST") computable, and feeds the realized leg of the drift-reachability shadow. Logging only — zero decision change.

## 6. Shadow log schema (`crypto5m_drift_reach`)
Per confirmed tick: `engine_version, window_start_ts, tick_ts, condition_id, side, z, vol_per_s, seconds_left, mu_hat, b_barrier, touch_prob, exp_fav_move, exp_fav_move_driftless, max_hold_s, jump_flag, side_ask, side_bid, side_mid, mid_at_entry` → resolved at `t_hold`: `mid_at_thold, realized_mid_move, status`. (`exp_fav_move_driftless` = the same formula with `μ=0`, so every row is a paired drift-vs-driftless comparison.)

## 7. Validation (pre-registered, mirrors brain-v2)
- **Primary:** clustered (by `window_start_ts`) Spearman of `exp_fav_move` vs `realized_mid_move`. WIN = rho CI excludes 0 **and** exceeds the driftless paired baseline (`exp_fav_move_driftless`, today's ~0).
- **Guard:** the drift model must not be *worse* than driftless on calm/no-drift ticks (no-regress).
- **Split:** chronological 70/30; the `μ̂` shrinkage/scale is the only fitted quantity, fit on train only.
- **Milestones:** 150 / 300 / 600 resolved confirmed-tick predictions (instrumentation must be live to accrue the realized leg).
- **Fee honesty:** realized-move is net-of-nothing (it's a calibration test, not a PnL claim); a PnL overlay logs both fee scenarios as today.

## 8. TDD cases (write first, must be red)
1. `drift_estimate`: missing/degenerate signals → `μ̂=0`; sign of `μ̂` follows the dominant flow sign; magnitude bounded by `k_drift·shrink`.
2. `touch_prob`: at `μ=0` equals `2·Φ(−b/(σ√τ))`; monotone increasing in `μ`; in `[0,1]`; fail-closed None on degenerate inputs.
3. `expected_fav_move`: `μ=0` → 0 directional drift term; sign follows `μ̂`; scales with `t_hold`.
4. `max_hold_s`: decreases as `|z|` grows; finite/positive; capped to `τ`.
5. **Symmetry-break regression** (the whole point): for a fixed `z,σ,τ` with `μ>0`, `touch_prob(Up favorable) ≠ touch_prob(Down favorable)` — proving we escaped the φ-symmetry trap that killed the prior design.
6. Shadow invariance: with the new fields present, `decide()` returns the identical action it would without them (zero live-decision change), under both an open-position and flat state.

## 9. Implementation increments
1. `drift_reachability.py` pure module + tests (1–5 above). No wiring.
2. Instrumentation (§5) + `crypto5m_position_marks` + tests.
3. `drift_reach_log.py` + desk shadow fields + runner shadow write + test 6 (invariance).
4. Validation harness + progress cron.
5. Deploy shadow; accrue; report. No live wiring in v1.

## 10. Success / kill
- **Promote (later phase):** drift `exp_fav_move` clears the primary at the 600 milestone with the guard intact → earns a *separate* spec to wire live (still then shadow-A/B'd).
- **Kill:** rho CI includes 0 at 600, or no improvement over driftless → taker reachability is dead; document and stop. Either outcome is a real result.
