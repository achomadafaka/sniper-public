# Exit engine v3 — quant policy PRE-REGISTRATION (frozen)

**Date frozen:** 2026-07-24 · **Status:** FROZEN · **Engine:** `exit-brain-v3` · **Features:** v1 (EVM) / v2 (Solana)
**Companions:** `2026-07-23-memecoin-real-exit-brain-prereg.md` (v1 features, EVM),
`2026-07-24-memecoin-real-exit-brain-solana-prereg.md` (v2 features, Solana),
`docs/superpowers/specs/2026-07-24-memecoin-quant-exit-fastlane-design.md` (§2.3 decision set).

## 0. Attestation

- **No outcome has been read.** Nothing below is fitted on the 88-close cohort, on replay, or on any
  shadow/paper result. Every constant is `[inherited]` (verbatim from the live funded policy or a
  prior study already in the brain), `[mechanical]` (forced by the data's structure), or `[bound]`
  (a round, answer-independent choice fixed before any read).
- v1/v2 **feature** definitions are unchanged. This registers only the **decision policy** that
  consumes them. Feature versions are NOT bumped.
- Unfreezing requires a new dated prereg + an engine-version bump. Editing a constant here is a
  protocol violation.

## 1. Why v3 exists (the priors it is built on)

| Prior | Measured | Source |
|---|---|---|
| Signal edge is front-loaded | up-fraction 53% @15m → 46% @1h → 39% @6h → 31% @24h; avg 24h **−9%** | feed decay study, n≈12k marks |
| Winners peak early | **≥50% of gains peak <3 h** | activity/drought study 07-11 |
| Holding is the failure | measured mean real hold **605 min** — ~40× past the edge horizon | this cohort, 2026-07-24 |
| The winning shape exists | `momentum_a`: low hit rate, uncapped winners, fast momentum cut → **+$5.59/trade** armed-Solana (n=329, 41.0% WR) vs +$2.00 non-armed at an identical win rate | 2026-07-24 measurement |
| Capping winners is the leak | real best win **ever** $40.13 against uncapped paper winners | brief §2 |

v3's thesis, stated so it can be falsified: **the edge is in leaving fast and never capping the
right tail.** Every rule below either shortens the hold or protects the tail. None adds size.

## 2. Frozen decision set

`propose_v3(contract, state, cfg) -> CandidateDecision`. Pure. Proposes only; never submits.

**Action vocabulary:** `RIDE`, `DERISK`, `TAKE_PROFIT`, `CUT`, `DEFER_TO_LEGACY`.

### 2.0 Absolute preconditions (unchanged from v1 §2.1)
1. `state.candidate_capable()` false ⇒ **`DEFER_TO_LEGACY`**. No exception.
2. **Monotonic risk-reduction only.** Every v3 action reduces or holds risk: `RIDE` takes no action,
   `DERISK`/`TAKE_PROFIT` are partial exits, `CUT` is a full exit. None widens a stop, raises a TP,
   adds size, resets a peak, or re-enters. Enforced by property test, not by discipline.
3. The catastrophe floor (80%) and the envelope rails remain absolute and are never scaled by v3.

### 2.1 CUT — precedence over everything (evaluated first)
| Trigger | Rule | Basis |
|---|---|---|
| LP pull | `depth_pull_flag is True` ⇒ **CUT, full** | `[mechanical]` the liquidity provider has left; the market the position depends on is gone. Live-verified signal (Solana prereg §0.1, EVM `v2_burn`) |
| Structure break | latest price `< prior_low` **AND** `net_flow_quote < 0` ⇒ **CUT, full** | `[inherited]` v1 §2.2, sign-only flow confirmation, no fitted magnitude |
| Unsellable | `exact_quote_misses >= MEMECOIN_REAL_UNSELLABLE_SWEEPS` ⇒ **CUT, emergency** | `[inherited]` measured 2026-07-24: a live pool returned $2.82 on a $300 chart-implied bag |

A break with `net_flow >= 0` is **rejected**, not escalated (v1 §2.2, unchanged).

### 2.2 TAKE_PROFIT — sell into strength, never at a fixed +X%
Fires only on a climax: `arrival_accel > 0` **AND** vol blowoff **AND** `higher_low_state is True`.

- **vol blowoff** = `realized_vol >= T/100` where `T` is the position's LIVE policy trail percent.
  `[mechanical]` — the same parameter-free comparison registered in v1 §2.3: when one window's
  realized vol reaches the trail distance, the move is outside its own noise. **No new constant.**
- **Fraction: 0.5** `[inherited]` — `MEMECOIN_REAL_PARTIAL_DERISK_FRACTION`, the live funded value.
- Rationale: liquidity is deepest during a blowoff. This banks half **into the bid while it exists**
  and leaves half riding, so it shortens the hold without capping the tail.

### 2.3 DERISK — bank half, keep the tail
Fires on **either**:
1. **first lower-high with vol expansion**: `lower_high_state is True` AND `realized_vol >= T/100`
   (same `[mechanical]` comparison as §2.2), or
2. **edge-duration prior**: `held_s >= EDGE_DURATION_S`.

- `EDGE_DURATION_S = 10800` (3 h) `[inherited]` — "≥50% of gains peak <3 h". Not fitted: it is the
  published horizon of the prior, used verbatim.
- **Fraction: 0.5** `[inherited]`, as §2.2.
- Fires at most once per position (the ledger's existing `derisked_at` guard).

### 2.4 RIDE — the default while the trend is intact
`higher_low_state is True` AND `net_flow_quote >= 0` ⇒ **RIDE**: propose no change, explicitly.

**There is no take-profit cap in v3.** Capping is the measured leak (best real win ever $40.13).
RIDE is a positive assertion "the tail is still alive", distinct from `DEFER_TO_LEGACY` ("we have
nothing to say"), so the two are separable in every read.

### 2.5 Otherwise
`DEFER_TO_LEGACY` with a reason code. The envelope rails remain authoritative.

## 3. Gate reads (unchanged discipline)

- **Minimum sample: n ≥ 30** eligible closes / divergent decisions before ANY verdict. Chain-split
  (Solana and EVM never pooled — refusal #6).
- Report separately: engine-driven vs envelope-driven exits; censored cases; feature-quality and
  fallback rate; per-action outcome (RIDE/DERISK/TAKE_PROFIT/CUT).
- **Branch on pass:** proceed to the N=5 canary (operator sign-off, spec §2.5).
  **Branch on fail:** v3 is retired; the envelope alone stays. No re-slicing.

## 4. Explicitly NOT registered (and therefore not permitted)

Any learned model or fitted threshold; any size increase; any re-entry; any widening of a stop or
raising of a TP; a fixed-percentage take-profit; a magnitude cut on flow; a DERISK trigger other
than the two in §2.3. Adding any of these requires a new dated prereg.

## 5. Change control

| Change | Requires |
|---|---|
| any constant or rule in §2 | new dated prereg + engine-version bump |
| enabling v3 on funded exits | operator sign-off: envelope policy version + N=5 canary (spec §2.4–2.5) |
| reading a gate below n≥30 | not permitted |

## 6. Envelope reference scale (2026-07-24, before any outcome read)

Under engine-envelope-v1 (`memecoin_funded_policy.py`), the EXECUTED trail (`FundedExitPolicy.trail_pct`)
is 0/off by design — the engine, not the legacy trail rail, owns the sell decision. §2.2/§2.3's
vol-vs-trail comparison (`_vol_blowoff`, `scaled_trail_pct`) was registered parameter-free because
it compares realized vol against the position's LIVE trail `T`. Under this authority inversion
there is no live executed trail to compare against, so the real-runner adapter passes the
position's coin-band v1 anchor as a REFERENCE scale only —
`memecoin_funded_policy.band_anchor_trail_pct()`, the SAME `[inherited]` value the non-envelope
policy for that band already uses as its executed trail (`MEMECOIN_REAL_PARTIAL_TRAIL_PCT` for
nano/micro, `MEMECOIN_REAL_TRAIL_PCT` for small, `MEMECOIN_REAL_MID_TRAIL_PCT` for mid — the same
anchors already registered `[inherited]` in the 2026-07-23 prereg §2.3). No new constant, no fitted
value, and it never reaches an executable rail: the envelope's executed trail stays 0, so no
`real_trail` exit can fire from the legacy giveback check regardless of this reference — only an
engine CUT (§2.1) or DERISK/TAKE_PROFIT (§2.2/§2.3) decision can. Attested: no outcome has been
read; this is a scale-reference clarification of an already-frozen comparison, not a new rule or
constant, so no engine-version bump is required.
