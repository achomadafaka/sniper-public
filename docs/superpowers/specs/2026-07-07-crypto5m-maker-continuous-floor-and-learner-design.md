<!-- schema:external=maker_windows -->
# Crypto5m maker — continuous vol-floor + adaptive-floor learner design

**Date:** 2026-07-07
**Status:** design + verification. No build launched. Follows the tournament finding
(`2026-07-07-crypto5m-maker-tournament-finding-regime-gated-design.md`) and the critique
that regime-gated-v1's floor switch is hardcoded and binary.

> Paper-only throughout. No live orders, no real money, no production DB.

---

## 1. Verification (pure floor lever, engine held constant)

The tournament finding used the frozen-vs-**control** contrast, which conflates the floor
(2e-5 vs 1e-4) with the engine (adaptive-v3 vs base). The clean experiment holds the engine
constant: **v3.2f (adaptive-v3 @ 2e-5) minus v3.1 (adaptive-v3 @ 1e-4)**, paired on N=169
identical forward windows. delta>0 ⇒ honest floor better; delta<0 ⇒ wide floor better.

| \|fair−0.5\| | n | delta (2e-5 − 1e-4) $/w | 95% bootstrap CI | verdict |
|---|---|---|---|---|
| 0.00–0.05 | 20 | −3.06 | [−7.4, +1.4] | ns |
| 0.05–0.10 | 15 | **−7.24** | [−12.1, −3.2] | **wide wins (sig)** |
| 0.10–0.15 | 14 | −2.17 | [−7.5, +3.0] | ns |
| 0.15–0.20 | 15 | −1.41 | [−4.9, +1.5] | ns |
| 0.20–0.25 | 25 | +2.28 | [−0.1, +4.7] | ns |
| 0.25+ | 80 | **+4.85** | [+3.6, +6.0] | **honest wins (sig)** |

**Confirmed:** direction (honest floor wins one-sided, wide wins contested), crossover
~0.19–0.20, monotone smooth ramp (no cliff). **Odds, not vol** — the one-sided-minus-contested
spread stays positive within every vol tercile (+5.7 / +8.3 / +9.2). **Temporally stable** —
first half (+4.3 / −4.3) ≈ second half (+4.2 / −2.6).

**Corrections (honesty):**
- The floor lever is **modest**: −7 to +5 $/w, ~half the frozen-vs-control numbers. The rest was
  the engine (adaptive-v3 vs base) — a separate, possibly larger lever worth its own study.
- The crossover is **fuzzy**: only the extreme bins are significant; 0.10–0.25 is noise at N=169.
  "0.20" is a point estimate in a wide band; regime-gated-v1's 0.10 threshold is mildly
  mis-placed (~1–2 $/w cost, not significant), not a "defect."

**Implication:** the response is a smooth, stable, odds-driven ramp of modest amplitude. That
argues for a continuous floor (no cliff to mis-place) but warns against over-engineering a small
lever.

---

## 2. What CAN and CANNOT be backtested

- **CAN (done above):** the regime *shape* — which floor wins as a function of odds — because we
  compare floors we *actually ran*, paired on identical windows.
- **CANNOT:** the optimal floor *magnitude*. Fills are endogenous to the floor; realized
  PnL-per-share is fixed by the quotes actually posted, so replaying historical fills at a
  counterfactual floor is invalid (the ladder structural limit, already hit once). We observe
  only 2e-5 and 1e-4. The optimum could be 5e-5 or 3e-4 and no existing data can say.
- **To get magnitudes:** a forward floor **sweep** (run extra floor levels, unconfounded, slow) or
  a **fill-model simulator** from the capture tape (high power, but fill-assumption risk — optimistic
  fills have burned us before; treat any simulator number as a hypothesis, not evidence).

---

## 3. Design A — deterministic continuous floor (regime-gated-v2)

Replace the binary switch with a smooth ramp of the floor in odds-extremity `x = |fair − 0.5|`:

```
floor(x) = wide            for x <= x_lo          (contested: protect)
         = honest          for x >= x_hi          (one-sided: harvest)
         = loginterp(wide, honest, (x - x_lo)/(x_hi - x_lo))   between
```

- Anchors: `wide = 1e-4`, `honest = 2e-5` — the two floors we have evidence for. Do NOT extrapolate
  beyond them; that is the sweep's job (Design C).
- Knots bracket the fuzzy crossover: `x_lo = 0.15`, `x_hi = 0.25` (the transition zone where CIs
  include zero) so the ramp is steepest through the uncertain region and commits only at the
  significant extremes.
- Interpolate in **log-floor** space (floors span 5×; log is the natural scale).
- Spread gate: **dropped.** 159/163 windows are already tight (<0.015); the 0.03 gate is near-inert.
  Keep a single hard guard: if spread is untradeable (>0.08) or fair is unknown, use `wide` (protect).
- Parameters: 4 (`wide`, `honest`, `x_lo`, `x_hi`), all interpretable, none fit to noise.

**Isolation/impl:** same as regime-gated-v1 — reuse the per-tick hook, swap `select_gated_floor`
for `ramp_floor(x)`. Per-process global; flag-gated; existing campaigns untouched.

**Forward test:** paired vs regime-gated-v1 (binary) AND frozen-f. The question v2 answers: does a
smooth ramp + the moved crossover beat the binary out-of-sample? Gate: 300 forward windows.

---

## 4. Design B — adaptive-floor learner (only if drift is proven)

The v3 learner failed for specific reasons (censored per-fill reward; high-dim discrete context;
~2 rewards/context starvation; optimized settlement Brier). A floor learner is a **different animal**
and avoids each of those — but is only justified if the response surface **drifts** (else the frozen
ramp in Design A already captures it at zero exploration cost, per the v3 lesson that freezing beat
learning).

**Architecture — contextual continuous-armed bandit (Bayesian):**
- **Context** `x`: odds-extremity (start 1-D; optionally add vol, time-to-resolution later).
- **Action** `a`: the vol floor — a small grid {1e-5, 2e-5, 5e-5, 1e-4, 2e-4} or continuous in log space.
- **Reward** `r`: **window-paired** PnL delta vs a frozen baseline on the same window (v3.3's fix —
  the reward that was actually right), not censored per-fill.
- **Model**: Gaussian-process (or Bayesian linear) regression of `r` on `(x, a)`. **This is why it
  can work where v3 could not:** the smoothness kernel pools information across nearby `(x,a)` — every
  window informs the whole surface — so it is NOT starved at ~2 samples/cell the way v3's discrete
  contexts were. N=169 already fits a 1-D ramp.
- **Policy**: Thompson sampling — draw a surface from the posterior, play `a* = argmax_a surface(x,a)`
  for the live `x`, with a capped exploration budget. This IS the adaptive forward sweep: it spends
  exploration where the surface is most uncertain, and it can only learn floors it actually posts
  (respects the counterfactual limit).
- **Non-stationarity**: recency-decay the observations (like v3.3's 36 h half-life) so the surface
  tracks drift — the ONE thing a learner does that a frozen ramp cannot.

**Why it is plausible here (vs v3):** 1-D continuous context + smoothness prior kills the
data-starvation; window-paired reward fixes the objective; the action is the lever we *know* matters,
not an opaque arm.

**Why it might still be a tax:** the lever is modest (±5–7 $/w) and the surface looks stationary
(verification §1). Exploration cost (posting suboptimal floors) could exceed the adaptation benefit.
If the ramp is stable, the learner pays to relearn a constant.

---

## 5. Staged gate — the learner must EARN its place

1. **v2 (deterministic ramp)** — build and forward-test vs binary-v1 and frozen-f (300 windows).
2. **Drift monitor** — fit the ramp's optimal crossover in rolling multi-day windows. §1 shows it is
   stable over two halves; extend to weeks and across vol regimes. **If the crossover does not move,
   STOP — no learner.** A frozen ramp is the answer.
3. **Learner (Design B)** — build ONLY if drift is demonstrated. Launch paired against a **frozen-ramp
   control** with an explicit exploration budget and a kill gate: if (learner PnL − frozen-ramp PnL)
   CI-upper < 0 at n≥200, kill. Same discipline that retired the v3 learners.

## 6. Forward floor sweep (Design C, parallel, cheap)

Run 2–3 extra fixed-floor paper campaigns (e.g. 5e-5, and one wide 5e-4) on the same windows, same
engine. Purpose: trace the floor→PnL dose-response the existing data cannot (the counterfactual limit).
Feeds both the v2 anchors and the learner's action grid with real, unconfounded magnitudes.

## 7. Heteroskedasticity (VERIFIED) — and the participation lever it exposes

Reward noise is strongly regime-dependent. PnL variance by odds regime, per campaign:

| campaign (floor) | one-sided m / sd / (m/sd) | contested m / sd / (m/sd) | var ratio |
|---|---|---|---|
| control (1e-4) | −6.31 / 9.50 / −0.66 | +9.33 / 18.13 / +0.51 | **3.6×** |
| frozen-f (2e-5) | +3.22 / 6.05 / **+0.53** | −1.91 / 8.52 / −0.22 | 2.0× |
| v3.2f (2e-5) | +2.11 / 4.78 / +0.44 | −0.14 / 7.02 / −0.02 | 2.2× |
| v3.1 (1e-4) | −2.13 / 3.76 / −0.57 | +3.32 / 7.83 / +0.42 | **4.4×** |

Two consequences:
1. **GP modeling (Codex #2, confirmed):** the learner's GP must use **input-dependent (heteroskedastic)
   noise** — a constant-noise GP would over-trust the 2–4× noisier contested region. Our verification
   CIs are already robust (bootstrap resamples within bin), so §1's significance verdicts stand; the
   contested bins read "ns" because they are noisier to *measure*, not because the effect is absent.
2. **It validates the floor switch risk-adjusted (correcting a first-pass over-reach).** Read the
   contested column *by floor*, not lumped: the **wide** floor makes contested profitable
   risk-adjusted (v3.1 +0.42, control +0.51 m/sd) while the **honest** floor does not (v3.2f −0.02,
   frozen-f −0.22). So contested is not a coin-flip "regardless of floor" — the floor is exactly what
   rescues it. Switching to the wide floor in contested is therefore the right lever on a
   **risk-adjusted** basis, not just on mean. A "reduce participation in contested" idea is NOT
   supported by this — the wide floor already earns a decent Sharpe there; forgoing it via a pause
   would drop +0.42 m/sd of real risk-adjusted return. Participation-by-regime remains untested and is
   at most a minor future side-experiment, not a reprioritization.

Net: the maker's steadiest edge is the one-sided regime (frozen-f +0.53 m/sd), and the wide floor
turns the noisy contested regime into a second positive-Sharpe cell (+0.42). The continuous floor
ramp (§3) captures both. Heteroskedasticity changes the learner's noise model, not the ramp's logic.

**Learner refinements folded in (Codex, valid):** budget exploration in **expected-PnL-loss**, not
window count (#1); **clip the action** to a pre-approved floor range, no extrapolated GP picks (#4);
keep the paired **baseline genuinely frozen** during evaluation (#5); **start 1-D** context — N=169 is
marginal for a 2-D surface (#6).

## 8. Falsifiers

- v2's ramp does not beat binary-v1 out-of-sample → continuity does not pay; keep the binary (or a
  better-placed binary).
- The sweep shows PnL flat across floor levels within a regime → the floor is not a real lever at all
  (the §1 effect was engine/selection); drop the whole line.
- Drift monitor shows a stable crossover → do not build the learner; ship the frozen ramp.
- Learner cannot beat the frozen-ramp control net of exploration → same verdict as v3: freezing wins.
