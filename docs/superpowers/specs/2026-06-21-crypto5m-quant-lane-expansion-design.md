<!-- schema:external=crypto5m_quant_matrix -->
<!-- schema:external=crypto5m_lane_results -->
# Crypto5m quant-lane expansion — design (2026-06-21)

## Goal
Expand the `crypto5m_lane_eval.py` candidate-lane harness with (A) a grid of **favorite × quant-agreement**
combinations and (B) **underdog quant lanes** — pre-registered, OOS-scored, while data accumulates. The
hard part is methodological: a grid of ~20 lanes will produce a fake "winner" by chance unless the readout
corrects for multiple comparisons. This design specifies the lanes AND the correction that makes them valid.

## What the data already established (the priors these lanes test)
- C1 (buy the favorite at 240s) is +EV after fee (82.9% WR, +6.1% net, n=82, persistent).
- The quant LOSES overall (45.5%) — dragged entirely by **underdog bets** (entry <0.50: 17–33% WR, −25 to
  −50%; anti-predictive). Its favorite bets ≈ C1.
- Weak in-sample combine signal (n=44): cheap favorites (<0.75) the quant **agrees** with returned +13.5%
  vs +8.5% when it doubted. Already encoded as v3 `fav_240_cheap_qagree`.
- The quant's structural disagreement is anti-predictive on favorites (`dog_240_structdis` −58%). So
  "quant disagrees with the market favorite" is a NEGATIVE signal, not an upset detector.

## Available per-window signals (from `crypto5m_quant_matrix.matrix_json`, all known-at-decision)
`q_market` (book favorite), `q_trade` (ensemble conservative), `q_struct_realized`, `q_mc`,
`structural_minus_market`, `up/down_order_flow_imbalance` (OFI), `vol_ratio` (realized/implied),
`up/down_best_ask` (entry prices). Checkpoints 120/180/240/270.
"Agreement" of signal S with the favorite = `sign(S − 0.5) == sign(q_market − 0.5)`; residual magnitude =
how much more confident S is than the market, in the favorite's direction.

## Family A — favorite × quant-agreement grid (theory-motivated, ~8 lanes)
The favorite is the base (+EV); the quant signals are SELECTORS. Never an underdog picker here.
1. `fav_qm` — plain favorite (= C1 control / anchor).
2. `fav_qtrade_agree` — favorite + ensemble agrees (residual ≥ 0). *(shipped v3)*
3. `fav_struct_agree` — favorite + diffusion structural agrees. *(shipped: `fav_240_struct`)*
4. `fav_mc_agree` — favorite + Monte-Carlo agrees. **(new)**
5. `fav_consensus` — favorite + ALL of {q_trade, q_struct, q_mc} agree (strongest conviction). **(new)**
6. `fav_consensus_cheap` — `fav_consensus` AND entry < 0.75 (highest-conviction cheap entry — the cell the
   in-sample sweet spot predicts should be best). **(new)**
7. `fav_qtrade_strong` — favorite + ensemble residual > 0.03 (strength tier, not just sign). **(new)**
8. `fav_alldisagree` — favorite when ALL quant signals DISAGREE (control: data says it still wins ~80%;
   tests whether quant disagreement actually hurts the favorite or is just noise). **(new)**

Rationale: this isolates WHICH quant component (ensemble vs structural vs MC vs consensus) adds selection
value over plain C1, and whether STRENGTH of agreement matters. Anchored by `fav_qm` (must beat C1, not 0).

## Family B — underdog quant lanes (honest −EV-prior tests, ~6 lanes)
**The constraint that governs every underdog lane:** an underdog at price `p` is +EV gross only if its
win rate exceeds `p` (and exceeds `p + fee` net). Favorites win ~83%, so blanket underdogs win ~17% — far
below typical underdog prices → blanket underdog LOSES (confirmed). An underdog lane is only worth testing
if it targets a CONDITION that plausibly lifts the upset rate above the price paid. Each lane below names
that condition; the strong prior is most FAIL, and that is a valid pre-registered result.
1. `dog_weakfav` — underdog only in WEAK-favorite windows (favorite 0.50–0.65, i.e., dog 0.35–0.50, where
   the outcome is genuinely uncertain and upset rate ~35–45% can exceed the dog price). Control for B.
2. `dog_weakfav_hotvol` — `dog_weakfav` AND `vol_ratio` high (terminal-vol / turn-of-candle upset; the
   research's only upset mechanism). The honest version of `dog_240_hotvol` restricted to where it can pay.
3. `dog_ofi_reversal` — underdog when OFI strongly favors the UNDERDOG token (buy pressure on the dog =
   the move may be reversing) AND dog price < 0.50.
4. `dog_struct_weakfav` — underdog when the structural model disagrees with the market AND the favorite is
   WEAK (<0.65). (On strong favorites this is anti-predictive; weak-favorite windows are the open question.)
5. `dog_hotvol_270` — underdog at the 270s checkpoint when vol is hot (late reversal, last 30s).
6. `dog_consensus` — underdog when ALL quant signals agree on the dog (the quant's strongest underdog
   conviction). Strong prior: −EV (this is exactly what lost). Pre-registered to quantify how wrong.

## Methodology — what makes a 20-lane grid valid (the core of this design)
Without this, the grid is p-hacking. The eval (`crypto5m_lane_eval.py` readout) must add:
1. **Pre-registration + freeze.** Bump `LANE_VERSION` and `FREEZE_TS` when the set lands; only windows with
   `decision_ts ≥ FREEZE_TS` count as OOS. In-sample stays clearly labeled "context only." Be explicit: the
   lane SET was derived from in-sample exploration (n=44), so these are post-exploration pre-registered
   hypotheses — the OOS column is the only evidence.
2. **Min-n gate.** No significance verdict for a lane until ≥ **40 resolved OOS picks** (lanes that select
   rarely — consensus, underdog conditions — accrue slowly; do not read them early). Underdog lanes in
   particular may sit at "insufficient n" for a long time — that is an honest state, not a failure.
3. **Family-A test = SKIPPED-COMPLEMENT, not subset-vs-full (self-review fix).** A Family-A lane's picks
   ARE C1's picks on those windows (both buy the favorite); the lane only differs by SKIPPING windows. So
   the right question is: are the windows the lane SKIPS worse than the ones it keeps? Report, per lane:
   `kept_roi` (the lane), `skipped_roi` (C1's pick on the windows the lane dropped), and `gap = kept −
   skipped` with a window-bootstrap CI. A selector adds value iff `gap > 0` with the CI clear of 0. (Plain
   "beat full-C1 ROI" is reported too, but the kept-vs-skipped gap is the load-bearing evidence.) Family-B
   (underdog) lanes are net-new exposure → benchmark **0**.
4. **Window-clustered bootstrap.** Picks within a window/day are correlated; resample by WINDOW (block
   bootstrap) for honest CIs on `kept_roi`, the `gap`, and dog-lane ROI-vs-0.
5. **Multiple-comparison screen (Benjamini-Hochberg FDR) — with a caveat.** Apply BH-FDR (q=0.10) across the
   per-lane p-values as a guard against the worst p-hacking. CAVEAT: the lanes are NOT independent (most are
   nested favorite-subsets), so BH is an approximate screen, not an exact false-discovery rate — the
   kept-vs-skipped gap CI (point 3) is the more robust per-lane signal. A lane is "promising" only if it
   clears min-n AND its gap CI is clear of 0 AND it survives the FDR screen.
6. **No silent winners.** The report prints every lane's n, kept/skipped ROI, gap + CI, raw p, FDR verdict,
   and min-n status — so a lucky small-n lane can't read as an edge.

## UI surfacing (must be clear + easy to navigate)
The 5m Binary tab already leads with the quant engine in plain language. Add a **"Quant lanes (what's being
tested)"** panel, fed by a new `GET /api/crypto5m/lanes` endpoint reading `data/crypto5m/lane_eval.sqlite`:
- Two clearly-labeled groups: **A — Favorite + quant selector** and **B — Underdog (long shots)**, each with
  a one-line plain-English description of the hypothesis.
- Per lane row: name, plain description, **OOS** n / win% / net-ROI (the real test) with the IN-SAMPLE number
  shown smaller/greyed as "context, not evidence", the kept-vs-skipped **gap** (Family A), a status chip
  — `accruing (n<40)` / `no edge` / `promising (survives screen)` — and the benchmark (C1 or 0).
- A header line stating the discipline in one sentence: "In-sample inspired these lanes; only the OOS column
  counts, after a window-bootstrap + FDR screen." So a viewer instantly knows what they're looking at.
- Keep it read-only and visually subordinate to the live forward C1 result (the incumbent), so nobody
  mistakes an accruing lane for a deployable edge.

## Implementation plan
- `scripts/crypto5m_lane_eval.py`: extract `q_mc`, `q_trade`, `up/down_order_flow_imbalance` in
  `load_windows` (q_trade/q_struct/q_mc/vol already partly there); add the Family-A and Family-B lane
  functions; add the bootstrap + BH-FDR readout (new `significance` section in the report + persisted to
  `crypto5m_lane_results` with bootstrap CI + fdr_verdict columns). Bump to a new `LANE_VERSION`/`FREEZE_TS`.
- Tests: pure-function tests for each new lane selector (agreement logic, weak-favorite band, OFI reversal,
  consensus); a bootstrap/FDR test on synthetic lanes (a known-good lane survives, a noise lane doesn't).
- Keep the `:22` cron; the new lanes accrue OOS automatically.

## Out of scope (named so it's not silently assumed)
- No real-money or paper-entry wiring — lanes are read-only scoring over the recorded matrix.
- No change to the frozen forward campaign (C0–R3) or the quant-shadow runner's decision logic.
- The model (Phase 2) is separate; these lanes are rule-based selectors, not a trained model.

## Honest expectation
Family A most-likely outcome: one or two quant-confirmation lanes marginally beat C1 OOS, most don't — and
the honest result may be "C1 is the ceiling; the quant adds nothing that survives FDR." Family B most-likely
outcome: all underdog lanes fail (underdogs are well-priced), with `dog_weakfav_hotvol` the only one with a
real shot. The design's value is a RIGOROUS answer to "can the quant beat the favorite / can we ever bet the
underdog," not an assumption that it can.
