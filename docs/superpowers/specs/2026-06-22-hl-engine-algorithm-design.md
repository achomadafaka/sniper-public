# HL Engine — First Fee-Clearing Trading Algorithm (Design Spec)

**Date:** 2026-06-22
**Branch:** `codex/hl-microstructure-runtime-exit` (worktree `Sniper-leadlag`)
**Status:** Design — buildable. No production code in this doc.
**Scope:** Define the Hyperlive engine's first algorithm that can clear the ~9 bps round-trip taker fee. Three components, phased: (1) a real **regime gate** (free leverage, build first), (2) an **OI-divergence continuation** setup, (3) a **relative-strength** setup. Each ships with a pre-registered forward-test audit and is *not trusted live* until held-out evidence clears the fee with a significant t-stat.

---

## 0. Why this, and the cost wall

The HL taker round trip is `ROUND_TRIP_FEE_BPS ≈ 9.0` (`models.py`: `TAKER_FEE = 0.00045`, ×2 legs ×1e4). Add spread (crossed once each leg) and the *effective* hurdle a setup must clear is ~9 bps **plus** `spread_bps`. The strategist already encodes this: `candidates_for()` computes `cost = spread_bps + 9.0` and the EV gate `p·target − (1−p)·stop − cost > EV_MARGIN_BPS` is structural — it cannot emit a negative-EV trade.

This session's research already concluded what does and does not clear that wall:

- **TAKER-DEAD (proven ~1–5 bps, < 9 bps cost):** OFI, cross-venue lead-lag (Binance→HL basis), CVD, microprice. These are real signals but the edge is smaller than the fee. They stay as *context/selection* features (already wired into `universe.py` / `scan_audit.basis_direction_edge`), **not triggers**. Do not build a taker entry on them.
- **FEE-CLEARING candidates (the build list):**
  - **(a) Regime gate** — free. Turns every other setup net-positive by only allowing it in the regime where it has positive expectancy, and refusing to trade in regimes where the engine would otherwise force losers. Highest leverage; build first.
  - **(b) OI-divergence continuation** — price↑ + OI↑ = new-money conviction → continuation; price↑ + OI↓ = exhaustion/short-covering → fade. Hours horizon, 50–150 bps moves. This is the first real *trigger* with margin over the fee.
  - **(c) Relative-strength leader/laggard** — rank alts vs BTC; in big directional regimes the leader continues / the laggard mean-reverts toward the cohort. Price-only, hours horizon.
  - **(d) liquidation-cascade magnet** — separate build (`cluster_audit.py` already scaffolds the forward test).
  - **(e) smart-money follow** — separate build (`intel_store.py` / `trader_intel.py` scaffold).
- **Funding / basis = context and sizing, never the trigger.** Positive funding → longs are crowded → trim long sizing / prefer the short side of an otherwise-symmetric setup. It is an input to `p_win`/sizing, not its own entry.

**Honesty constraint that governs every number below:** crypto backtests run **5–10× optimistic out-of-sample**. Every target/stop/`p_win` here is a *hypothesis*, not a measured edge. Nothing trades live money on these numbers; everything accrues forward through its audit (§4) and only the EV-after-fee held-out verdict promotes it. Momentum-style setups (b, c) additionally carry **crash risk** — they pay small and often, then give it back in one regime flip; the regime gate (§1) is the primary defense, and the stop is the backstop.

---

## 1. Regime gate (build FIRST)

### 1.1 What's wrong with the current classifier

`regime.classify()` today is a single-snapshot, intra-second classifier: it reads `vol_bps`, `ret60_bps`, `ret300_bps`, `liq_rate`, `depth_ratio` from one `MarketSnapshot` and returns `calm | trending | ranging | dislocation | cascade`. It works for fast-track microstructure but has three gaps for a fee-clearing algorithm:

1. **No realized-vol *percentile*.** `vol < 4.0 → calm` is an absolute threshold; 4 bps/step means very different things for BTC vs a thin alt. A regime gate needs a *per-asset percentile* of realized vol so "high vol" is relative to that asset's own recent history.
2. **No proper trend-vs-range classifier.** `trending` is currently "r60 and r300 agree in sign and |r300| ≥ 2·vol" — a weak two-bar persistence check. The research calls for an **efficiency ratio** (Kaufman) or **return-autocorrelation sign** over a real lookback window, which separates *directional* tapes (where continuation/RS pay) from *choppy* tapes (where they bleed the fee).
3. **No multi-bar history.** `classify()` only sees one snapshot. The new gate needs a rolling price series per asset (we already keep one in `compute.RollingState.bbo`, and `hyperlive_scan_log.mid_px` is an even longer per-asset series at refresh cadence).

### 1.2 The new regime module — `sniper/hyperlive/regime_gate.py`

A **separate, additive module** (do not break `regime.classify`; the fast track keeps using it). The gate produces a *per-asset* `RegimeState` from a price-history window, and the strategist consults it to decide which **swing** setups are allowed.

```
RegimeState = {
    "regime": "trend" | "range" | "high_vol",     # the 3-way the research asks for
    "direction": +1 | -1 | 0,                       # sign of net drift over the window
    "vol_pct": 0.0..1.0,                            # realized-vol percentile vs this asset's history
    "efficiency_ratio": 0.0..1.0,                   # Kaufman ER over the window
    "ac1_sign": +1 | -1 | 0,                         # sign of lag-1 return autocorrelation
    "n": int,                                       # bars used (confidence)
}
```

**Inputs.** A per-asset deque of `(ts_ms, mid)` at refresh cadence (~150 s; the same cadence `hyperlive_scan_log` already logs). Keep `REGIME_WINDOW = 64` bars (~2.7 h of 150 s refreshes) plus a longer `VOL_HISTORY = 256` bars for the percentile baseline. Both fit in `RollingState` as new deques (`regime_mid_hist`, fed from the ctx refresh, not the fast bbo stream).

**Encodable definitions (pre-registered constants):**

```
returns r_i      = ln(mid_i / mid_{i-1})                 over the window
realized_vol     = stdev(r_i) * sqrt(len)                 # window realized vol, bps
vol_pct          = percentile_rank(realized_vol, VOL_HISTORY of prior window-vols)

# Kaufman efficiency ratio: net move / summed absolute moves. 1 = perfectly
# directional, 0 = pure chop.
efficiency_ratio = |mid_last - mid_first| / sum(|mid_i - mid_{i-1}|)

# lag-1 autocorrelation sign of returns: +1 trending (momentum), -1 mean-reverting.
ac1              = corr(r_i, r_{i-1});  ac1_sign = sign(ac1) with |ac1| < AC_DEADBAND -> 0

direction        = sign(mid_last - mid_first)
```

**Classification (deterministic, thresholds pre-registered):**

```
ER_TREND      = 0.35     # >= this and ac1 not mean-reverting -> trend
AC_DEADBAND   = 0.05     # |ac1| below this is "no signal"
VOL_HIGH_PCT  = 0.85     # realized vol in the top 15% of its own history -> high_vol/crisis
MIN_BARS      = 24       # below this, regime = "range" (default-safe: fewest setups)

if n < MIN_BARS:                      regime = "range"      # not enough history -> conservative
elif vol_pct >= VOL_HIGH_PCT:         regime = "high_vol"   # crisis/cascade veto (see allow-map)
elif efficiency_ratio >= ER_TREND and ac1_sign >= 0:  regime = "trend"
else:                                 regime = "range"
```

`high_vol` is checked **first** (after the bars floor) so a crisis tape never reads as a clean trend — this is the momentum-crash guard.

### 1.3 Allow-map — which setups in which regime

This is the whole point: free expectancy from *not* trading the wrong setup in the wrong tape. The swing track consults `regime_gate.allowed_swing_setups(regime_state)`; the fast track is unchanged.

| `regime_gate` regime | OI-divergence continuation | OI-divergence fade | relative-strength | (carry: funding_reversion) |
|---|:--:|:--:|:--:|:--:|
| **trend**     | ✅ (direction must match) | ❌ | ✅ (leader-continuation) | ✅ |
| **range**     | ❌ | ✅ (price+OI exhaustion fade) | ❌ | ✅ |
| **high_vol**  | ❌ | ❌ | ❌ | ❌ (veto-all: stand aside) |

<!-- schema:external=hyperlive_scan_log -->

Rules encoded:
- **Continuation only in `trend`,** and only when the setup's side matches `regime_state.direction` (no countertrend continuation).
- **OI-divergence *fade* only in `range`** — fading exhaustion into a trending tape is how you get run over.
- **Relative-strength only in `trend`** (needs a directional cohort to rank against).
- **`high_vol` vetoes everything** on the swing track. The fast track may still run its own dislocation/cascade setups via the existing `regime.classify`; that is intentional and separate.

### 1.4 Test obligations (extend `tests/test_hyperlive_regime.py`, new `test_hyperlive_regime_gate.py`)

- Pure-directional synthetic series (monotone drift) → `regime == "trend"`, `efficiency_ratio` near 1, `direction` correct.
- Saw-tooth / mean-reverting series → `regime == "range"`, `ac1_sign == -1`, low ER.
- A series whose window vol sits in the top decile of its history → `regime == "high_vol"` regardless of ER.
- `n < MIN_BARS` → `regime == "range"` (conservative default).
- `allowed_swing_setups("high_vol") == set()`.
- `allowed_swing_setups("trend")` excludes `oi_divergence_fade`; `allowed_swing_setups("range")` excludes `oi_divergence_cont` and `relative_strength`.

---

## 2. OI-divergence continuation setup

### 2.1 Thesis (the four quadrants)

Open interest is contract count, not price. The ΔOI × Δprice sign pair tells you *who* is driving:

| ΔPrice | ΔOI | Interpretation | Action |
|:--:|:--:|---|---|
| **↑** | **↑** | new longs opening — new-money conviction | **continuation long** |
| **↓** | **↑** | new shorts opening — new-money conviction | **continuation short** |
| **↑** | **↓** | shorts covering (no new money) — exhaustion | **fade (short)** |
| **↓** | **↓** | longs capitulating (no new money) — exhaustion | **fade (long)** |

The two **continuation** quadrants (price and OI move together) are the new-money conviction trades; the two **divergence** quadrants (price and OI move opposite) are exhaustion fades. The current `continuation` setup in `setups.py` *only* uses the OI-confirm sign as a veto on a fast 60 s drive — it does not trade the divergence quadrants and it is a 120 s fast trade, not the hours-horizon swing the research describes. This is a **new swing setup**, `oi_divergence`, living alongside the existing ones.

### 2.2 Inputs (already computed)

From `compute.RollingState.snapshot()` features and `hyperlive_scan_log`:
- `oi_delta` (300 s OI change fraction; already in features) — and for the swing horizon, a refresh-cadence `oi_delta_pct` (already logged as `hyperlive_scan_log.oi_delta`, and `change_signals.oi_delta_pct`).
- `ret300_bps` for the contemporaneous price move; for the swing variant, the refresh-to-refresh `mid_return_bps` (`change_signals`).
- `funding`, `basis` for sizing/side-bias context (not trigger).

**New feature to add in `compute.snapshot()`** (additive, one line each):
```
oi_div_quadrant: "cont_long" | "cont_short" | "fade_short" | "fade_long" | None
oi_div_strength_bps: |ret300_bps|        # magnitude of the price leg, the move we ride
```
computed as:
```
dP = ret300_bps ; dOI = oi_delta          # both signed
if |dP| < OI_DIV_MIN_MOVE_BPS or |dOI| < OI_DIV_MIN_OI_PCT: quadrant = None
elif dP>0 and dOI>0: "cont_long"
elif dP<0 and dOI>0: "cont_short"
elif dP>0 and dOI<0: "fade_short"
elif dP<0 and dOI<0: "fade_long"
```

### 2.3 The setup in the `setups.py` framework

`propose()` already returns `(side, target_bps, stop_bps, horizon_ms, rationale)` or `None`. Add a new branch and register the track + regime mapping.

```python
# setups.py — pre-registered constants
OI_DIV_MIN_MOVE_BPS = 30.0     # the price leg must be a real move, not noise
OI_DIV_MIN_OI_PCT   = 0.01     # >=1% OI change over the window = meaningful positioning
OI_DIV_RR_MIN       = 1.8      # target:stop must clear this (research bar)
OI_DIV_HORIZON_MS   = 3_600_000   # ~1h swing — where the 50-150bps move grows past the fee

def propose(setup, s):
    ...
    if setup == "oi_divergence":
        q = s.features.get("oi_div_quadrant")
        if q is None:
            return None
        move = _f(f, "oi_div_strength_bps")            # the price leg magnitude
        if move < OI_DIV_MIN_MOVE_BPS:
            return None
        # CONTINUATION quadrants ride WITH the move; FADE quadrants ride AGAINST it.
        if q == "cont_long":   side, mult = "long",  1.2   # conviction continues; ride further
        elif q == "cont_short":side, mult = "short", 1.2
        elif q == "fade_short":side, mult = "short", 0.7   # exhaustion reverts; smaller target
        elif q == "fade_long": side, mult = "long",  0.7
        else: return None
        tgt  = max(MIN_TARGET_BPS, mult * move)            # >= 22.5 bps floor (2.5x fee)
        stop = round(tgt / OI_DIV_RR_MIN, 1)               # R:R fixed at >= 1.8 by construction
        rationale = f"OI-divergence {q} (dP={move:.0f}bps, OI-confirmed)"
        return side, round(tgt, 1), stop, OI_DIV_HORIZON_MS, rationale
```

**Track + regime gating:**
```python
SETUP_TRACK["oi_divergence"] = "swing"                 # hours horizon
# regime_gate (§1.3): continuation quadrants only allowed in "trend" & side==direction;
# fade quadrants only allowed in "range". Enforced in the strategist, see §2.4.
```

### 2.4 EV gate, R:R, and fee margin (the part that must be honest)

- **R:R ≥ 1.8 by construction:** `stop = target / 1.8`. The strategist's existing EV `p·target − (1−p)·stop − cost` then needs a modest `p_win` to clear: with `target = 60`, `stop = 33`, `cost = 9 + spread`, break-even `p_win ≈ (stop + cost) / (target + stop) ≈ (33 + 11) / 93 ≈ 0.47`. So this setup needs to win **just under half the time** to be EV-positive — plausible for a real continuation signal, and the calibration (`Calibration.p_win(setup, regime)`) measures whether it actually does. **It does not assume it.**
- **Fee margin:** `MIN_TARGET_BPS = 22.5` (2.5× fee) is the floor; the swing horizon is chosen precisely because the 50–150 bps move grows *past* the fixed 9 bps fee over an hour, where a 1–5 bps micro-signal never could.
- **`p_win` feeds the EV gate through calibration, not a hardcode.** On day 1 the cell `(oi_divergence, trend)` has `n = 0`, so `Calibration.p_win` returns the Beta(1,1) prior `0.5` and `confidence ≈ 0`. The UCB exploration bonus (`UCB_C = 6.0 · sqrt(1/(n+1))`) lets it get *sampled* in paper, but it does not get *trusted* until §4's audit clears it on held-out data. The two are separate: paper-sampling builds the calibration; the audit is the promotion gate.
- **Sizing/side context (funding):** if `funding > 0` (longs crowded) and the proposed side is `long`, the strategist may shade size down or require a slightly higher EV margin. Encode as an optional `EV_MARGIN_BPS` bump, not a hard veto. This is the one place funding enters — never as a trigger.

### 2.5 Momentum-crash caveat (must be in the code comment)

Continuation/momentum setups pay small-and-often then surrender it in one regime flip. The regime gate (`high_vol` veto, `trend`-only) is the primary guard; the stop is the backstop; and the audit's **t-stat ≥ 2** requirement on the *mean aligned move* (not hit-rate) is the statistical guard against "70% hit rate, negative expectancy" — a classic momentum trap where the rare loss is huge.

---

## 3. Relative-strength setup (leader/laggard)

### 3.1 Thesis

In a directional crypto tape, alts do not move uniformly. Ranking each alt's return *residual to BTC* over a window separates **leaders** (high residual — leading the cohort, tends to continue in a `trend` regime) from **laggards** (low/negative residual — catching up or lagging). Price-only, no microstructure, hours horizon. Two encodable signals:

- **Ratio momentum:** `r_alt − r_btc` over the window (excess return vs the benchmark).
- **Residual return:** regress `r_alt` on `r_btc` (single-factor), the residual `r_alt − β·r_btc` is the idiosyncratic move; rank by it.

Start with the simpler **ratio momentum** (`r_alt − r_btc`); add β-residual only if the audit says ratio momentum clears the fee but is noisy.

### 3.2 Inputs (already collected)

`hyperlive_scan_log` logs `mid_px` per asset per refresh — that *is* a synchronized cross-sectional price panel. BTC is always in the universe (`scan_audit.horse_race` already extracts `btc_move` per `scan_ts` this exact way). So the relative-strength rank is computable from already-collected data with zero new ingest.

**New cross-sectional feature** (computed in the scanner/strategist, not per-snapshot, because it needs the whole universe at one `ts`):
```
rs_excess_bps[asset] = ret_window_bps[asset] - ret_window_bps["BTC"]
rs_rank[asset]       = percentile rank of rs_excess across the tradeable universe
```
Window = the regime window (§1, ~ several refreshes). Compute over the *tradeable* pool only (`day_ntl_vlm >= MIN_VOLUME_USD`).

### 3.3 The setup

```python
# setups.py — pre-registered
RS_LEADER_PCT   = 0.80     # top 20% excess return = leader
RS_LAGGARD_PCT  = 0.20     # bottom 20% = laggard
RS_MIN_EXCESS_BPS = 40.0   # the alt must actually be diverging from BTC, not noise
RS_HORIZON_MS   = 3_600_000
RS_RR_MIN       = 1.8

def propose(setup, s):
    ...
    if setup == "relative_strength":
        rank   = _f(f, "rs_rank", default=-1.0)        # 0..1, -1 = not computed
        excess = _f(f, "rs_excess_bps")
        if rank < 0 or abs(excess) < RS_MIN_EXCESS_BPS:
            return None
        # LEADER in an up-tape continues up; in a down-tape the strongest (least-down) leads the bounce.
        if rank >= RS_LEADER_PCT:    side = "long"  if excess > 0 else "short"
        elif rank <= RS_LAGGARD_PCT: return None    # laggard mean-reversion is a SEPARATE, later hypothesis
        else: return None
        tgt  = max(MIN_TARGET_BPS, 0.8 * abs(excess))
        stop = round(tgt / RS_RR_MIN, 1)
        return side, round(tgt, 1), stop, RS_HORIZON_MS, f"rel-strength leader (excess {excess:+.0f}bps vs BTC, rank {rank:.2f})"

SETUP_TRACK["relative_strength"] = "swing"
# regime_gate: allowed only in "trend" (needs a directional cohort); side follows the cohort direction.
```

**Phase the laggard side deliberately.** Ship **leader-continuation only** first (it's the cleaner trend hypothesis). Laggard mean-reversion (buy the bottom-quintile expecting catch-up) is a *different* bet with worse crash properties — defer it to its own audit pass, do not bundle. The `return None` on the laggard branch above makes that explicit.

### 3.4 EV / fee margin

Same skeleton as OI-divergence: R:R ≥ 1.8 by construction, `MIN_TARGET_BPS` floor, `p_win` via calibration cell `(relative_strength, trend)`, hours horizon so the excess move clears the 9 bps fee. Honest expectation: relative-strength is the **weakest** of the three (most crowded, most arbed); treat a positive audit verdict with extra skepticism and the largest discount for OOS optimism.

---

## 4. Forward-testing — no live trust without held-out evidence

Every setup mirrors the **exact** pattern already proven in `scan_audit.basis_direction_edge` and `cluster_audit.cluster_cascade_edge`: read a perishable forward-accruing time series, build *aligned* forward moves (signal-direction × forward move), require **EV-after-fee > 0 AND t-stat > 2 AND n ≥ min_n**, return `{status: insufficient | ok, ...}` and accrue forward. **No setup trades the strategist's live capital weighting until its audit returns `edge: true` on held-out data.**

### 4.1 OI-divergence audit — `oi_divergence_edge()` in a new `oi_audit.py`

Reads `hyperlive_scan_log` (which already has `mid_px`, `oi_delta`, `open_interest`, `prior_open_interest`, `day_ntl_vlm` per asset per refresh — **data is already collected**, this audit can run as soon as enough refreshes accrue).

```
for each (asset, scan_ts) with |ret_window| >= OI_DIV_MIN_MOVE and |oi_delta| >= OI_DIV_MIN_OI:
    quadrant = classify(sign(ret_window), sign(oi_delta))
    signal_side = +1 if continuation-long or fade-long else -1
    forward_move_bps = (mid[scan_ts + H] - mid[scan_ts]) / mid[scan_ts] * 1e4   # nearest within TOL_MS
    aligned = signal_side * forward_move_bps
mean, std, t = aligned stats ; after_fee = mean - 9.0
edge = (after_fee > 0 and t > 2.0)
# report continuation and fade quadrants SEPARATELY — they are different hypotheses.
return {status, n, hit_rate, mean_aligned_bps, after_fee_bps, t_stat, edge,
        per_quadrant: {...}}
```
Constants: `H = 3_600_000` (and a `1_800_000` secondary), `TOL_MS = TOL` from scan_audit, `MIN_N = 40`, `FEE_BPS = 9.0`. Restrict to the tradeable pool (`day_ntl_vlm >= MIN_VOLUME_USD`) exactly like the other audits.

### 4.2 Relative-strength audit — `relative_strength_edge()` in `rs_audit.py`

Reads `hyperlive_scan_log.mid_px` as a cross-sectional panel (BTC extracted per `scan_ts`, same as `horse_race`).

```
for each scan_ts:
    compute ret_window_bps[asset] and rs_excess[asset] = ret[asset]-ret["BTC"]
    rank assets by rs_excess (tradeable pool)
    for each leader (rank >= RS_LEADER_PCT, |excess| >= RS_MIN_EXCESS):
        signal_side = sign(excess)
        forward_move_bps = (mid[ts+H][asset] - mid[ts][asset]) / mid[ts][asset] * 1e4
        aligned = signal_side * forward_move_bps
mean/std/t/after_fee/edge as above ; H = 3_600_000 ; MIN_N = 40 ; FEE_BPS = 9.0
```

### 4.3 Regime-gate audit (the meta-test)

The gate's value is *conditional*: does each setup's `mean_aligned_bps` improve when restricted to its allowed regime vs run unconditionally? Add a `by_regime` split to §4.1/§4.2 (label each `scan_ts` with `regime_gate.classify` from that asset's prior-window mids, which the panel already contains). **Promotion claim for the gate** = "setup X's after-fee mean and t-stat are materially higher inside its allowed regime than across all regimes." If gating doesn't improve expectancy, the gate is not earning its complexity — say so.

### 4.4 Verdict lifecycle (mirror the existing audits)

`insufficient` (n < min_n) → accruing → `ok` with `edge: true/false`. Surface in the UI panel the same way `basis_direction_edge` / `cluster_cascade_edge` results already surface. A setup with `edge: false` after `n ≥ min_n` is **refuted** and should be disabled in the allow-map (or kept paper-only as a control). The 5–10× OOS discount means: even `edge: true` in-sample is provisional until it holds on a *later* held-out block — wire a train/test split (like `horse_race`'s `train_frac`) into the OI and RS audits before any real-money consideration.

---

## 5. Phasing, data, and what's already collected

| Phase | Component | New code | Data needed | Already collected? |
|---|---|---|---|---|
| **P1** | Regime gate | `regime_gate.py`, `RollingState.regime_mid_hist`, allow-map in `strategist.select`, tests | per-asset refresh-cadence mid history | **Yes** — `hyperlive_scan_log.mid_px` (panel) + `compute` ctx stream. Audit runs immediately. |
| **P2** | OI-divergence | `oi_divergence` branch in `setups.py`, `oi_div_quadrant` feature in `compute.py`, `oi_audit.py`, `(oi_divergence, regime)` calib cells, tests | per-asset price + OI deltas per refresh | **Yes** — `hyperlive_scan_log` already logs `open_interest`, `prior_open_interest`, `oi_delta`, `mid_px`. Audit runs immediately on accrued rows. |
| **P3** | Relative-strength | `relative_strength` branch + `rs_excess`/`rs_rank` cross-sectional feature, `rs_audit.py`, calib cells, tests | synchronized cross-asset mid panel + BTC | **Yes** — `hyperlive_scan_log.mid_px` is exactly this panel; BTC always present. Audit runs immediately. |

**Order is non-negotiable:** regime gate first (it changes the *expectancy* of P2/P3 by gating them), then OI-divergence (the first real trigger with fee margin), then relative-strength (weakest, most arbed, build last). Each phase: write the audit alongside the setup, let it accrue, read the held-out verdict, *then* let the strategist weight it live.

**No new ingest is required for any phase** — the scan log already captures the price/OI panel at refresh cadence. The only new *live* compute is: the regime window deques (P1), the `oi_div_quadrant` feature (P2), and the cross-sectional `rs_rank` (P3). Everything else is reading data that's already accruing.

### 5.1 Definition of done (per phase)

1. Setup proposes correctly on synthetic snapshots (unit tests, both sides, the `None` rejections).
2. Strategist gates it to the right regime(s) via the allow-map (integration test through `select`).
3. EV gate provably cannot emit it negative-EV (the structural property — assert on a low-`p_win` calibration).
4. Its audit returns `insufficient` on empty data, computes aligned-move/after-fee/t-stat correctly on a fixture, and respects the `min_n` gate.
5. Forward accrual begins; **no real-money weighting** until the audit shows `edge: true` on a *held-out* block, discounted for OOS optimism.

---

## 6. Files touched (implementer's map)

- **New:** `sniper/hyperlive/regime_gate.py`, `sniper/hyperlive/oi_audit.py`, `sniper/hyperlive/rs_audit.py`, `tests/test_hyperlive_regime_gate.py`, `tests/test_hyperlive_oi_audit.py`, `tests/test_hyperlive_rs_audit.py`.
- **Edit:** `setups.py` (two new `propose` branches + `SETUP_TRACK` + constants), `compute.py` (`oi_div_quadrant`/`oi_div_strength_bps` feature; feed `regime_mid_hist`), `strategist.py` (consult `regime_gate.allowed_swing_setups` for the swing track), `models.py` (extend `Candidate.setup` doc-comment to list the two new setups), `tests/test_hyperlive_regime.py` (unchanged behavior assertion).
- **Unchanged:** `engine.py` state machine (the new setups flow through the existing SCANNING→…→EVALUATING loop with no structural change — they're just new candidates with a swing horizon), `calibration.py` (new `(setup, regime)` cells appear automatically).

---

## 7. Pre-registered constants (single source, copy into code)

```
# regime_gate.py
REGIME_WINDOW = 64 ; VOL_HISTORY = 256 ; MIN_BARS = 24
ER_TREND = 0.35 ; AC_DEADBAND = 0.05 ; VOL_HIGH_PCT = 0.85

# setups.py — OI-divergence
OI_DIV_MIN_MOVE_BPS = 30.0 ; OI_DIV_MIN_OI_PCT = 0.01
OI_DIV_RR_MIN = 1.8 ; OI_DIV_HORIZON_MS = 3_600_000

# setups.py — relative-strength
RS_LEADER_PCT = 0.80 ; RS_LAGGARD_PCT = 0.20 ; RS_MIN_EXCESS_BPS = 40.0
RS_RR_MIN = 1.8 ; RS_HORIZON_MS = 3_600_000

# audits (oi_audit.py / rs_audit.py)
FEE_BPS = 9.0 ; MIN_N = 40 ; TOL_MS = 75_000 ; train_frac = 0.6
H_PRIMARY_MS = 3_600_000 ; H_SECONDARY_MS = 1_800_000
```

These are **hypotheses set before looking at outcomes** (the same discipline as `ACCEL_LIFT_BAR_PP`, `CROSS_VENUE_MIN_MOVERS`, `DIR_MIN_N` already in `scan_audit.py`). Changing one after seeing a verdict is p-hacking and must be logged as such.
