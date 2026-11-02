<!-- schema:external=maker_windows -->
<!-- schema:external=maker_regime_decisions -->
# Crypto5m maker — tournament finding + regime-gated challenger design

**Date:** 2026-07-07
**Status:** finding recorded; challenger `maker-regime-gated-v1` pre-registered (paper-only)
**Supersedes framing of:** the learner tournament preregistration
(`2026-07-06-crypto5m-learner-tournament-preregistration.md`) — that test asked "does the
learner beat the fixed rule?" This finding answers it (no) and redirects the program from
"fix the learner" to "match quote posture to market regime."

> Guardrails unchanged: paper-only, no live orders, no real money, no production DB, no
> service restart implied. `maker-regime-gated-v1` is an ADDITIVE new campaign; it replaces
> nothing and mutates no existing campaign history.

---

## 1. Tournament table (forward common window set)

All campaigns paired on the **identical** forward windows since anchor `1783355700`
(engine `window_start_ts`; the host clock is +7h fast — never anchor on `time.time()`).
Common set capped by the youngest campaign (v3.3L): **N = 137 windows**.

| Campaign | mean $/w | WR | Δ vs control $/w | t-stat | Δ ex-best |
|---|---|---|---|---|---|
| frozen-f (no learner) | **+1.59** | 50% | **+2.35** | 1.68 | +288 |
| v3.3L (rebuilt learner) | +1.47 | 55% | +2.23 | 1.55 | +272 |
| v3.2f (old learner) | +1.11 | 47% | +1.87 | 1.39 | +225 |
| v3.2r (markout reward) | +0.12 | 34% | +0.88 | 0.74 | +96 |
| v3.1 (learner closure) | −0.08 | 31% | +0.69 | 0.63 | +70 |
| v2 (profile learner) | −0.18 | 28% | +0.58 | 0.96 | +66 |
| control (regime-v1) | −0.76 | 26% | — | — | — |

Retired 2026-07-07 (portfolio review): **v2, v3.1, v3.2r** — negative-to-marginal absolute,
only "beat" an underwater control. Surviving fleet: control, v3.2f, frozen-f, v3.3L, +
this challenger.

**Note on "v3.2f is the strongest performer":** that impression comes from the live board's
SETTLED/TOTAL column, which counts since v3.2 launch and includes pre-anchor windows. v3.2f
has more windows (219) than frozen (143) or v3.3L (117), so its early lead is baked in. On the
apples-to-apples forward set it is **third**, behind frozen-f and v3.3L.

---

## 2. Caveats — this is descriptive, not final proof

- N = 137 common windows; the sub-cells that drive the design are smaller (mid-odds 40,
  jump 12). Read them as direction, not significance.
- No paired Δ-vs-control **t-stat clears 2.0** (best is frozen-f at 1.68). The *direction*
  (f-family beats control; freezing beats learning) is now consistent across three
  independent reads — the pre-compaction culprit finding, the attribution report, and this
  cross-tab — but the *magnitude* is not yet significance-grade.
- Control is negative on the forward set, so "Δ vs control" is partly "control is bad here."
  The absolute mean/w column is the honest anchor.
- This finding is what justifies a **new forward-paper campaign**, not a promotion.

---

## 3. Mechanism — one variable dictates performance

Raw mean PnL/w by market regime (N=137). odds_bucket = whether the up-fair is outside
[0.35, 0.65]; it collapses onto spread and print count (extreme-odds windows are 68%
tight-spread, 62% low-print — one axis, not three):

| regime | frozen-f (honest floor 2e-5) | control (inflated floor 1e-4) |
|---|---|---|
| QUIET (extreme-odds & tight-spread, n=66) | **+3.60** | **−6.73** |
| CONTESTED (rest, n=71) | −0.28 | **+4.79** |

They are **mirror images**. And the split is robust where it matters: the QUIET edge is
diffuse (frozen +237 total, ex-best +219 — no single window carries it); frozen's mid-odds
loss spreads across both days (−55.9 then −12.1), not a cluster.

**Why (verified against the code):** the only lever between the two is
`live_desk.VOL_FLOOR_PER_S` (control 1e-4, frozen 2e-5), floored at `max(raw_vol, floor)`.
- Quiet/one-sided market → true vol is low → the honest 2e-5 floor lets the maker quote
  *tight* around the Binance-led fair and actually earn the spread. The inflated 1e-4 floor
  quotes too wide, doesn't fill (or fills adversely), and its "claimed edge" is a mirage
  (claims +$67, realizes −$54 in calm — pre-compaction finding).
- Contested/volatile market → a wider quote is *protective* and the inflated floor's width
  earns real realized PnL (+4.79/w for control, diffuse over 71 windows).

**Jump is NOT a separate regime — it is a mislabel for mid-odds.** Splitting the 12 jump
windows by odds: jump∩mid-odds (n=8) control +21.6/w; jump∩extreme-odds (n=4) control
**−3.7/w** and frozen fine. Control's entire "wide quoting wins in jumps" story is its
mid-odds edge in a jump costume. A dedicated jump regime (as an earlier 4-regime proposal
had) is pure overfit and is **rejected** by this data.

---

## 4. Why the learner is currently not adding value

- frozen-f > v3.3L > v3.2f on the forward set: **freezing the launch prior beats both the
  old and the rebuilt learner.** The vol floor / quote posture is the edge; online adaptation
  is at best neutral, historically a tax.
- Diagnostic: at the alpha cell (extreme-odds) frozen +2.94, v3.3L +2.92 (parity), but the
  old learner v3.2f only +1.32 — it drifted *away* from the exposure that makes money.
- Making the learner "regime-aware" is the wrong fix on this data volume: it splits each
  context by regime → `context × regime` cells → even fewer observations per cell, and the
  learner is already starved (median ~2 rewards/context). The regime IS the signal and can
  be **hand-encoded for free**. A within-regime learner is revisitable only once some regime
  accrues enough volume to learn anything finer — not close.

---

## 5. The challenger — `maker-regime-gated-v1` (deterministic, 2-state)

Same engine as frozen-f (v3 frozen: default fair blend, base_small arm, no learner writes).
The **only** change: `VOL_FLOOR_PER_S` is selected per window from the live-classified regime
instead of a fixed global.

| Live regime (causal, known at quote time) | Floor | Rationale (verified in §3) |
|---|---|---|
| **QUIET:** odds_bucket=extreme AND spread_state=tight | 2e-5 (honest, tight) | frozen wins here +3.60/w |
| **CONTESTED:** everything else | 1e-4 (inflated, wide) | control wins here +4.79/w |

- Two states only. No jump regime (rejected §3). No mid-odds micro-policy (do not overfit 40
  windows). Standard late-window reduce-only hygiene stays as-is in the base engine.
- `odds_bucket` and `spread_state` are already computed per window by `maker_regime`; both are
  functions of the live book (current up-fair, current spread), so the classification is
  causal — no look-ahead.
- Isolation: separate campaign DB (`maker_regime_gated_v1.sqlite`), separate campaign id,
  its own process. `VOL_FLOOR_PER_S` is a per-process module global — switching it inside
  this process cannot affect control/frozen-f (separate interpreters).

**Ceiling (not a claim):** if the switch cleanly captured each in-regime edge, the paired PnL
would be ~ 66·3.60 + 71·4.79 ≈ +577 over 137 windows (+4.2/w) vs frozen +1.59. Composition is
not guaranteed (imperfect live classification, no hindsight assignment) — that is exactly what
the forward test measures.

---

## 6. Forward validation plan

- **Primary comparison: `maker-regime-gated-v1` vs `frozen-f`** (the current best), paired on
  identical windows. Secondary: vs control.
- Per-regime breakdown reported (quiet vs contested legs each shown separately).
- Minimum **300 forward common windows** before any binding read (larger than the 200 used
  before, because the design was fit by looking at N=137 — pay the OOS tax).
- One read at the gate; no optional stopping. Anchor on engine `window_start_ts`.
- Concentration guard: result must survive dropping the single best window; no single
  day/window cluster may carry it.

---

## 7. What would falsify it

- **Quiet leg fails:** regime-gated-v1's quiet-window PnL does not match frozen-f's quiet
  PnL → the classification is misfiring or the edge was a fit artifact. Kill.
- **Contested leg fails:** the contested (1e-4) leg does not beat frozen-f's contested
  (−0.28/w) → the "wide protects/earns in contested" half was control-specific luck (its
  +4.79 was somewhat concentrated: ex-best +290 of +339). Fallback: contested → **pause**
  (forgo the leg, keep only the proven quiet harvest).
- **Switch cost dominates:** if regime misclassification churns the floor and the maker
  gets adversely filled on transitions such that overall paired PnL < frozen-f, the 2-state
  switch is not worth it and frozen-f remains champion.
- **Overall:** if after 300 forward windows regime-gated-v1 is not materially better than
  frozen-f and not worse than control, the honest conclusion is that the honest vol floor
  (frozen-f) was the whole edge and no regime switch improves it.
