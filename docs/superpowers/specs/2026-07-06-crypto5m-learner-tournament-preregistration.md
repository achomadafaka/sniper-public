# Crypto5m maker learner tournament — pre-registration (2026-07-06)

Registered BEFORE frozen-f accrued a settled window; the pre-existing entrants (control,
learning-f, learning-r) already had history, which is why every gate below is FORWARD-ONLY
from its registration anchor — pre-anchor windows are exploration and never bind a verdict.
This document freezes the comparison set, metric, gates, and promotion rule so the winner
is not picked after looking (the v3.2f
lesson: its first 65 windows were both its selection sample and its headline, and the paired
edge decayed monotonically +3.86 → +2.35 → −0.23 $/window across thirds).

All anchors and window identity use the **engine/Binance `window_start_ts` timebase**. This
host's wall clock runs ~+7h fast — never derive anchors from `time.time()` / `date`
(bug found & fixed 2026-07-06: an anchor set from the local clock silently excluded ~7h ≈ 83
forward windows).

## Entrants (fixed set — adding an entrant re-opens this registration)

| line | campaign | learner | role |
|---|---|---|---|
| control | `maker-regime-v1` | none (fixed rules) | global control |
| learning-f | `maker-adaptive-paper-v3.2f` | v3 learner, live updates | incumbent challenger |
| frozen-f | `maker-adaptive-paper-v3.2f-frozen` | held at launch priors (`--adaptive-frozen`) | learner-harm ablation |
| learning-r | `maker-adaptive-paper-v3.2r` | v3 learner + markout reward | weaker sibling, keeps accruing |
| v3.3-learner | `maker-v33-learner-paper` (added 2026-07-06, BEFORE its first window) | rebuilt learner: window-level PAIRED reward (baseline = frozen-f, so reward ≡ value added by learning), frozen structural fair (no Brier blend learning), sufficiency-gated hierarchy (n≥30 windows per level), recency decay (36h half-life), evidence-gated deviation from the prior arm | the "improve" branch, racing in parallel instead of waiting for the gate |

`frozen-f` definition (code: `maker_adaptive_v3.py` `frozen=` paths): default fair blend
weight + zero residual (never reads `crypto5m_maker_v3_fair_blend_stats`), deterministic
launch-prior arm (first eligible in `NORMAL_ACTION_ORDER`, exactly what an empty-DB campaign
resolves to), epsilon forced 0, no arm-stat/blend-stat/disable-prefer updates. Reward rows are
still recorded (telemetry only). Engine risk logic (safety overrides, tail-risk governor,
vol floor 2e-5) identical to learning-f.

## Metric and gates

- Metric: **paired same-window realized PnL delta** (challenger − comparator), windows matched
  on `window_start_ts`, settled only.
- Primary comparisons: (1) frozen-f vs learning-f, (2) frozen-f vs control,
  (3) learning-f vs control (already gated, forward anchor 1783355700),
  (4) v3.3-learner vs frozen-f (the "does a correctly-built learner add value" read;
  registered 2026-07-06 before the entrant's first window; its gate is forward from its
  own launch by construction).
- Gate per comparison: **n ≥ 200 forward paired windows** and bootstrap 95% CI on the paired
  delta excluding zero. Forward = `window_start_ts ≥` the comparison's registration anchor.
  frozen-f anchor = its first quote (launched 2026-07-06, engine time ≈ 1783356300); the fleet
  board's global forward anchor 1783355700 predates it, so every frozen-f window is forward.
- Read cadence: **one read at the gate**, plus the automated campaign-guard kill test
  (existing `crypto5m_campaign_guard.py`, kill at n≥200 CI-upper<0). No manual peeks between.

## Pre-registered secondary analysis (added 2026-07-06 after exploratory conditioning at n=80; binds the FORWARD sample only)

Exploratory read: the v3.2f-vs-control edge concentrates in **pure-calm windows** (0% jump
ticks: delta +12.5/w, n=9, control *negative* −5.2/w there — mechanistically the honest
vol floor pricing calm correctly while control's inflated 1e-4 floor gets adversely
selected), and vanishes in active windows (delta +0.2 to +1.4). The Q4 "recovery" was the
regime cooling, not learner autocorrection (arm drift kept deepening: base_small 64%→54%).
Codex's independent replication (n=83) added the sign flip: at jump share ≥10% v3.2f
LOSES to control (−$2.19/w, n=27), and the claimed-vs-realized proof — in calm windows
control's fair claims +$66.91 edge and realizes −$53.75 (the inflated 1e-4 floor
manufactures fake edge in calm), while v3.2f claims +$41.86 and realizes +$64.72.
Therefore, at each gate read, ALSO report paired deltas split THREE ways by per-window
jump share — calm (0%), moderate (0–10%), high-jump (≥10%) — for every comparison.
PINNED SOURCE (codex review): jump share = fraction of `maker_regime_decisions` rows with
`regime_id='jump'` in the window (exists for every campaign incl. control); fallback to the
v3 policy-action context_key vol field ONLY when regime rows are retention-pruned, and the
report labels which source produced each row (`jump_source`). The two sources are NOT
interchangeable (context_key classifies ~2× more ticks as jump); the registered boundaries
bind on the regime_decisions definition. Analysis code:
`scripts/crypto5m_period_attribution_report.py` (frozen pre-gate; run only at the gate). Sharp prediction stated in
advance: frozen-f should match learning-f in calm windows (same floor, same prior arm
dominates there) and beat it in active windows (no learned timidity). If instead frozen-f
loses calm windows, the learner's drift was load-bearing and "autocorrect" gets revisited.
Hour-of-day is NOT a registered split — with one session of data it is collinear with
campaign age and cannot be separated yet.

## Decision rules (pre-committed)

- **frozen ≥ learning at the gate** (CI on frozen−learning delta excludes zero in frozen's
  favor, or overlaps zero with frozen ahead): the v3 learner is judged harmful-or-useless at
  current data density → scrapped or rebuilt; v3.3's fixed learner must justify itself against
  frozen-f, not against learning-f.
- **learning > frozen at the gate** (CI excludes zero in learning's favor): the drift was
  adaptation; keep the learner, fix data starvation (coarsen contexts / fewer arms) as
  incremental work.
- **Promotion**: no campaign is called "the engine" unless it (a) wins its sibling
  comparisons AND (b) clears its own forward gate vs `maker-regime-v1`. Winning among
  siblings alone is insufficient (best-of-N selection).
- No mid-campaign parameter/learner changes to any entrant. A change = new campaign + new DB
  + re-registration. Campaign data never pools across versions.

## Non-goals

Live orders, sizing, or real-money implications — paper measurement only, per the wallet/algo
doctrine (fail-closed, operator-gated).
