# Crypto5m Brain v2 — Stabilized Probability (Design)

**Date:** 2026-06-25
**Status:** Design — approved in brainstorming, pending spec review → writing-plans
**Scope:** v1 only (shadow-mode stabilized probability + observability + offline validation). v2/v3 deferred (see §10).
**Supersedes/extends:** the live diffusion fair value in `sniper/algos/polymarket_crypto5m/live_desk.py` (`diffusion_z` / `fair_value_up`) — but v1 changes **no** live behavior; it computes a second probability alongside the current one.

---

## 1. Problem

The engine's probability `P(Up) = Φ(z)`, `z = ln(BTC_now/BTC_start) / (σ·√t_left)`, is mis-calibrated during sharp price moves. Root cause is the denominator `σ` (`vol_per_s`): a **180s equal-weight realized-vol estimate**, floored (`live_desk.py:33` `VOL_LOOKBACK_S=180`, `:30` `VOL_FLOOR_PER_S=2e-5`, `:116-126` `realized_vol_per_s`).

- On a sharp move the **numerator reacts instantly** (the log-return jumps now) but the **denominator reacts over ~3 minutes** (one big 1s return is ~1/180 of an equal-weight window). So `z` overshoots → `P(Up)` is most overconfident exactly when it should be least.
- After the move, the jump **stays in the 180s window**, so `σ` is now too high for ~3 minutes → subsequent genuine moves **under-react**. A lagging-vol artifact in both directions.

Independent evidence the probability is the weak link: the market midpoint is a well-calibrated, regime-aware probability we currently use only as cost/target. (The Phase-2 sklearn model's Brier 0.256 vs 0.57+ was the *model's* failure, not the diffusion's — the diffusion's specific proven defect is the sharp-move overshoot above.)

The taker is **cost-bound (verified live)** — so this work is **calibration + maker-enablement, not taker-rescue.** A correct, regime-aware, market-anchored probability is the foundation the eventual maker quotes around (a maker resting on an overshooting `P(Up)` is adversely selected on every sharp move) and is what makes the EV measurement honest.

## 2. Goal / Non-goals

**Goal (v1):** produce a stabilized probability `z_v2` that tempers the sharp-move overshoot, computed every tick **in shadow** alongside the current `z`, persisted with settlement truth, and proven better **offline** before anything touches the live decision path.

**Non-goals (v1):**
- No change to live decisions. The engine keeps deciding on the current `z`; `z_v2` is shadow-only.
- No counter-move/stepback state, no path-excursion features, no decision-wiring (→ v2).
- No maker (→ v3).
- Not trying to *beat* the market — `market_z` is the reference ceiling, not the target. v1's claim is "beat our **current** `z` on sharp windows without regressing on calm/persistent ones."

## 3. Architecture decision: diffusion primary, market regularizes

Chosen over "market as the prior" and "ours alone." Rationale: making the market the prior would wash out the one thing that justifies the engine — our **independent** BTC read and its ~1s lead on the book. We keep the diffusion as primary and let the market **regularize only where our model is provably broken** (the sharp-move overshoot). Fix the bug; don't surrender the model.

Operationalized as a **precision-motivated blend in z-space** (a logarithmic opinion pool):

```
z_v2 = w·z_ours + (1−w)·z_market        then   P_v2 = Φ(z_v2)
```

- `z_ours` = `ln(BTC_now/BTC_start) / (vol_slow·√t_left)` — the diffusion z computed with the **jump-excluded** regime vol (so jumps can't inflate the denominator for 3 min).
- `z_market` = `probit(clamp(up_mid, ε, 1−ε))` — the market-implied z on the **P(Up) axis** (up side). `ε` prevents the probit blow-up at the bounds; deep-moneyness reads are common.
- `w` = weight on our model, **decreasing in regime sharpness** (see §6). Calm → `w≈1` (our z dominates = primary). Sharp → `w→0` (cede to the market = regularize). The blend is in **z-space** because z is the Gaussian-space variable — that is the form a precision weighting takes (not a probability-space mixture). The *operational* `w` is a fitted function, not literal inverse-variance weights — see §6.3 for why (binary settlement makes z-error variance non-estimable).

**The bug is the lagging vol, not the market** — the market is the *stabilizer*, applied through `w`.

## 4. Success test (the validation gate)

Calibration, not edge. The new probability is "better" iff it has **lower Brier against the 0/1 settlement on sharp-move windows, without regressing on calm/persistent ones.**

- Metric: **per-tick Brier** `(Φ(z) − y)²` (`y` = 0/1 settlement of the window), **paired** per window (the same windows scored by both `z` and `z_v2`), tested **window-clustered** (within-window ticks are autocorrelated).
  - **Gate aggregation is over the sharp ticks within each window** (ticks where `divergence`/`jump_flag` is elevated — already columns in `crypto5m_z_shadow`), **not** the whole-window mean. The overshoot fix lives in the handful of sharp ticks (~seconds of a ~150-tick window); averaging over all ticks dilutes it and a real fix could fail to clear 0 purely from calm ticks washing it out — a power failure masquerading as a null. The window remains the clustering unit (independence); only the per-window statistic is restricted to its sharp ticks. The whole-window mean stays as a **secondary diagnostic**.
- Buckets: **calm / sharp-revert / sharp-continue** (§7).
- Win condition: `z_v2` Brier **beats current-`z` on sharp-revert** (where the overshoot is toward the side that *lost* → ceding to the market helps) **AND does not regress on calm + sharp-continue.** The **sharp-continue** bucket is the under-reaction guard — on a real, continuing move current-`z`'s overshoot is toward the *winning* side and is fine, so a merely-tamer `z_v2` that hedged it would lose there; requiring no-regress catches exactly that. We do **not** expect (or require) a win on sharp-continue.
- Raw `|z|` overshoot reduction is a **diagnostic column only**, never the gate (a z that under-reacts to everything would game it).
- `market_z` Brier printed as the **ceiling** reference per bucket.

## 5. Shadow-first principle (the zero-live-risk invariant)

v1 is a pure **measurement + observability + validation** layer. It computes `z_v2` next to `z`, persists both, and proves the win offline. The flip that makes `z_v2` primary (feeding `fair_value` + the EV gate + entry/hold/exit) is a **later spec**, gated on the harness win. v1 **cannot** alter a live decision — enforced by an integration test (§8.E).

## 6. Components & data flow

All compute is pure functions + one table + one offline harness + a display-only graph. Nothing touches the decision path.

### 6.1 `regime_vol(klines, now)` — pure
Returns:
- `vol_slow` — jump-**excluded** realized vol over ~180s (kills the 3-min linger). Becomes the `z_ours` denominator.
- `vol_fast` — ~10s EWMA, jump-**included** (it *must* spike on a jump — that spike is the regime signal that drives the cede).
- `jump_flag` — a 1s `|log-return|` > `κ·`(running 1s σ), `κ≈4`. Excluded from `vol_slow`; flagged for the var cap (§6.2).
- `divergence` `d = vol_fast / vol_slow` (≈1 calm, ≫1 sharp).

> **Correction #1 (locked):** `vol_slow` excludes jumps, `vol_fast` includes them. If both excluded, a jump produces no divergence → no cede → diffusion z over a jumped numerator with a calm denominator → overconfident right after the jump (the original bug *reversed*).

Computable entirely from the per-poll kline window (`build_live_desk` already fetches ~400×1s klines, `live_desk.py:270`) — **no in-process buffer** (the persisted series is the only memory v1 needs; this also makes it restart-safe).

### 6.2 `stabilized_z(btc_now, btc_start, seconds_left, regime_vol_out, market_mid, weight_fn=prior)` — pure
- `z_ours = diffusion_z(btc_now, btc_start, vol_slow, seconds_left)` (reuses `live_desk.diffusion_z`).
- `z_market = probit(clamp(market_mid, ε, 1−ε))`.
- `w = weight_fn(divergence)` — see §6.3; **capped low for a decaying window when `jump_flag` is set** (immediate cede on the one-tick jump before `vol_fast`'s 10s EWMA fully absorbs it), then `w` decays back to its divergence-implied value as `vol_fast` normalizes.
- `z_v2 = w·z_ours + (1−w)·z_market`.
- Returns `z_v2`, `z_ours`, `z_market`, `w`, `divergence`, `jump_flag`, and diagnostics `var_ours_diag`/`var_market_diag` (§6.3).

**Degenerate handling (correction #3):**
- Our inputs degenerate (`btc_*`≤0, `seconds_left`≤0, `vol_slow` None/≤0) → `z_v2 = None`.
- Market thin/missing (`up_mid` None or one-sided book) → `z_v2 = z_ours` (`w=1`), **not** None — so the series has no gaps in the illiquid windows we most want to study.
- `market_mid` at/beyond `(ε, 1−ε)` → clamped before `probit`.
- `up_mid` missing but `down_mid` present → **`z_ours` fallback** (deliberate v1 simplification; v1 does **not** derive `market_z` from `1−down_mid`).

### 6.3 Weight `w(divergence; θ)` and the variance fit (correction #2 + §3-A)
Precision-weighting is the **motivation**; the **operational** weight is a regularized monotone function fit to the data — because z-error *variance* can't be estimated from binary settlement (the per-window "true z" is ±∞), so the literal inverse-variance formula isn't estimable and we don't claim it for the live form.

- Form: `w = sigmoid(a − b·log(divergence))`, `b ≥ 0` (monotone decreasing in sharpness), `θ=(a,b)`.
- Fit: choose `θ` to **minimize holdout Brier** of `Φ(w·z_ours+(1−w)·z_market)` (the exact quantity we're graded on), fit on the **train** split, scored on **holdout** (§7). Non-circular by construction.
- **Bootstrap prior (pre-fit, the path that actually runs first):** before `N_sharp` accrues there is no fit, so `w` uses a prior `w_prior = var_market / (var_ours + var_market)` with `var_market ∝ spread²` and `var_ours ∝ divergence²`. This already obeys calm→primary / sharp→cede; the fit refines it.
- `var_ours_diag = Brier(Φ(z_ours), y)` and `var_market_diag = Brier(Φ(z_market), y)` **per divergence bin** are reported as **diagnostics** (graph + sanity), not the live weight formula.

### 6.4 `crypto5m_z_shadow` table — one row per tick
Written by the active engine each tick; resolved at window close. Single table = graph source + validation substrate. Lifecycle mirrors `crypto5m_ev_observations` (pending → resolved → incomplete; reuse the resolver pattern).

<!-- schema:external=crypto5m_z_shadow -->

| column | meaning |
|---|---|
| `id` | pk |
| `engine_version` | blank-slate counting (e.g. `brain-v2-shadow-v1`) |
| `window_start_ts`, `tick_ts` | window + tick time (Binance server time) |
| `condition_id` | the 5m market |
| `btc_now`, `btc_start`, `seconds_left` | diffusion inputs |
| `vol_slow`, `vol_fast`, `jump_flag`, `divergence` | regime-vol outputs |
| `z` | current live z (the baseline) |
| `z_ours` | diffusion z with jump-excluded `vol_slow` |
| `z_market` | `probit(clamp(up_mid))` |
| `market_mid` | `up_mid` used for `z_market` |
| `z_v2`, `weight` | blended z and the weight used |
| `status` | `pending` \| `resolved` \| `incomplete` |
| `settlement` | 0/1 outcome (Up won), stamped at close |
| `sharp_label` | `calm` \| `sharp_revert` \| `sharp_continue` (stamped at close, independent of `z_v2`) |
| `resolved_ts` | when settlement was stamped |

### 6.5 `scripts/crypto5m_z_validation.py` — offline cron, read-only
Buckets resolved windows; computes paired window-clustered Brier deltas; reports go/no-go vs the milestone ladder. **Routes through `crypto5m_lane_eval.py`'s existing window-clustered bootstrap + min-n + Holm/BH machinery** — no new stats path. Proposes only; never mutates thresholds or the live path.

### 6.6 Graph — display-only
UI plots `z` vs `z_v2` vs `z_market` over the window from the shadow series. Display-only JS, **verified by screenshot** like the prior desk/EV panels — **out of the TDD set.**

### Data flow
`build_live_desk` (already holds klines + market price) computes `regime_vol` + `stabilized_z`, exposing `z_v2` + components on the desk next to the existing `z`. The active engine runner writes a `crypto5m_z_shadow` row each tick and resolves settlement + stamps labels at window close (reusing the ev-observation resolver). The engine **still decides on the current `z`**. The jump-excluded vol is shadow-only — the live `z` keeps its 180s vol until the flip spec.

## 7. Sharp-window labeling & split (corrections #3-B, #3-D)

- **Sharp label (independent of `z_v2`):** a window is `sharp` iff `max(|1s log-return|)` over the window ≥ **both** `k×median(|1s log-return|)` (relative — adapts to regime) **and** an **absolute bps floor** (so a dead-calm window with a tiny blip isn't labeled sharp). Computed from the BTC path, **never** from `z_v2`'s `jump_flag`. `k` and the floor calibrated from the BTC 1s-return distribution (pinned in the plan).
- **revert vs continue:** by the sign of the **max single-tick** move vs the settlement direction — `sharp_revert` if they differ, `sharp_continue` if they agree. (Multi-sharp-tick windows reduce to the largest tick's sign — acceptable v1 simplification, noted.) **Settlement-defined, post-hoc, scoring-only** — the live `w` uses `divergence` only and never sees this label.
- **Split:** **chronological / walk-forward** (train = earlier windows, holdout = strictly-later), never random (random K-fold leaks future regime structure into training). Walk-forward (expanding train, score next block) preferred to use scarce sharp windows efficiently.
- **Milestone ladder (pre-registered):** counted on **holdout sharp-revert windows** — the binding bucket where the win must appear — `N_sharp_revert_holdout` of **60 (monitor) / 120 (monitor) / 240 (first confirmatory)**. The gate = paired Brier-delta CI (window-clustered bootstrap) **excludes 0 favorably on sharp-revert** with **no adverse CI on calm + sharp-continue** (the guards). **If the CI hasn't cleared 0 by the confirmatory look, accrue more** — the CI width decides, not the calendar (no pre-promised sufficient N). sharp-revert is rarer than sharp overall, so accrual is slower; that is expected and is why the ladder is monitor/monitor/confirmatory rather than a fixed deadline. Multiplicity across looks handled by the existing Holm/BH discipline.

## 8. Testing strategy (TDD — named cases lock the four corrections)

### A. Invariants (the corrections themselves)
- `vol_slow` excludes a jump, `vol_fast` doesn't (inject 4σ tick → `vol_slow`≈unchanged, `vol_fast` spikes, `jump_flag` set).
- **Post-jump not-overconfident** (the single most important test): jumped numerator + otherwise-calm path → `z_v2` not extreme; pulls toward `z_market`.
- **jump-flag-floor decay (un-cede)** — the dual: a few seconds after one jump with no new jumps, `z_v2` returns toward `z_ours` as the cap decays and `vol_fast` normalizes. (Prevents a one-way valve where one tick makes us track the market forever.)
- **calm → primary, sharp → cede**: calm → `z_v2≈z_ours` (`w≈1`); sharp → `z_v2→z_market` (`w→0`).
- **Degenerate split**: our inputs bad → `None`; market thin/missing → `z_ours` (not None); `market_mid` at bounds → clamped, no blow-up.
- **Blend space**: `z_v2 == w·z_ours+(1−w)·z_market` exactly (z-space then Φ).

### B. Per-component units (pure, deterministic, hand-built klines; no network)
- `regime_vol`, `stabilized_z`, `w(divergence;θ)`.
- Fit reads **train rows only**, scores holdout (non-circularity structural & tested).
- **Symmetric fit**: fit recovers `w→0` where the market is reliable (sharp) **and** `w→1` where our model's historical Brier beats the market's (calm) — both directions.

### C. Harness / gate
- **Bootstrap/accruing mode**: below milestone, the gate returns `accruing` (no go/no-go), and `z_v2` from the **bootstrap priors** (no fit yet) is still sane and obeys calm→primary / sharp→cede. (This is the path that runs first.)
- Bucketing correctness; **paired** per-window Brier delta (same windows, both estimators); **chronological split** assertion (every holdout window strictly later than every train window); seeded determinism; routes through `crypto5m_lane_eval` bootstrap (not a new stats path).

### D. Sharp-label
- Relative-AND-absolute (dead-calm + tiny blip → not sharp; clears both → sharp); revert/continue by max-tick sign vs settlement; computed from BTC path, never `z_v2`; revert/continue unreachable on the live path.

### E. Zero-live-risk integration + lifecycle
- Drive the runner over a scripted desk sequence **with and without** the shadow logger → trade ledger / decisions **identical** AND `crypto5m_z_shadow` got its rows.
- **Restart safety**: `z_v2` after a simulated reconnect equals `z_v2` computed fresh (free — features are pure functions of per-poll klines).
- **Settlement-unresolved handling**: a window where Gamma fails at close stays `pending`/`incomplete`, is **excluded** from the Brier (not counted as 0, no crash); harness scores **resolved only**. Reuse the ev-observation resolver lifecycle.

### F. Real-window golden fixture
One test from a **real resolved window** (real klines + book + 0/1 settlement) asserting the pipeline yields sane `z` / `z_v2` / `z_market` and a correct label (per the "E2E test real ProtocolReport" lesson; synthetic alone hides schema/distribution surprises).

### G. CI hygiene
`crypto5m_z_shadow` is a new table → run `scripts/validate_schema_refs.py` and `python3 scripts/brain_index.py generate` + commit (the stale-index gate fails CI otherwise).

## 9. Fail-closed

Every pure fn returns `None` on degenerate inputs and never raises; a null `z_v2` logs nulls, never crashes; the engine is unaffected regardless. No network in tests (scripted desks / hand-built klines / golden fixture).

## 10. v1 boundary & deferred work

**v1 (this spec):** jump-aware regime vol + market-regularized `z_v2` (shadow), the `crypto5m_z_shadow` series, the offline validation harness, the display-only graph.

**Deferred (later specs, build on v1's features):**
- **v2:** path-excursion features (efficiency ratio, MAE/MFE) + the **counter-move standing state** wired into entry/hold/exit. v1 already produces the raw materials (`jump_flag` + fast/slow divergence), so v2 is a thin interpreter, not new measurement. A behavior change → rides on a probability we've already proven.
- **v3:** stepback entry-timing; the **maker pivot** (the stabilized, market-anchored probability is what the maker quotes around).

Keeping v2/v3 out of v1 means v1 ships with **zero live risk** and a clean validation gate.

## 11. File structure (proposed; finalized in the plan)

- `sniper/algos/polymarket_crypto5m/regime_vol.py` — `regime_vol` (pure).
- `sniper/algos/polymarket_crypto5m/stabilized_z.py` — `stabilized_z`, `w(divergence;θ)`, prior + fit (pure / read-only fit).
- `sniper/algos/polymarket_crypto5m/z_shadow_log.py` — `crypto5m_z_shadow` schema + write/resolve/label (mirrors `ev_observation_log.py`).
- `scripts/crypto5m_z_validation.py` — offline harness (reuses `crypto5m_lane_eval` bootstrap).
- `live_desk.py` — compute + expose `z_v2` on the desk (shadow).
- `scripts/run_polymarket_crypto5m_active_trader.py` — write shadow rows + resolve at close (shadow-only).
- UI desk panel + `/api/crypto5m/...` series read (display-only).
- Tests per §8.

## 12. Resolved during brainstorming (rationale for the plan author)

- **Combine choice:** diffusion primary, market regularizes (not market-as-prior; not ours-alone) — preserve the independent BTC read + book lead, borrow the market's calibration only where ours is broken.
- **Where the fix lives:** both — jump-aware regime vol **and** market clamp; jump-vs-diffusion split so a single 4σ tick is treated as a jump (excluded from `vol_slow`), not as evidence the diffusion got more volatile.
- **Success = direct overshoot reduction**, made gameable-proof by scoring bucketed Brier vs settlement with the sharp-continue under-reaction guard; raw overshoot is a diagnostic only.
- **No free knobs:** precision-weighting *motivates* the cede behavior, but the live weight `w(divergence)` is a low-parameter monotone function **fit to minimize holdout Brier** (not literal inverse-variance — binary settlement makes z-error variance non-estimable). The pre-fit prior is `var_market/(var_ours+var_market)` with `var_market ∝ spread²`, `var_ours ∝ divergence²`; per-regime Brier of each estimator is a diagnostic that validates the fit. Nothing is hand-tuned to the overshoot metric.
