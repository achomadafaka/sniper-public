# V6_BREAK_MANAGER_V0 — pre-registration (FROZEN 2026-07-25)

> **SUPERSEDED 2026-07-26 as the evaluation/activation contract.** V6 is now tested by owning
> realistic live-paper positions after their modeled fills; there is no V5 comparison arm and no
> replay acceptance harness. See
> `docs/superpowers/specs/2026-07-26-memecoin-v6-live-paper-engine-design.md`. The frozen rules below
> remain historical discovery evidence and the initial break-manager seed, not the complete live
> V6 position engine.

**Chain scope: 4663 only. Replay/paper arms only. Funded rails untouched. Solana excluded until it
has its own tape and its own prereg.** Any edit to this policy = version bump (V0→V1) and the
prospective cohort restarts. No mid-cohort changes.

## Discovery basis (all consumed — not reusable for evaluation)

33 unique-coin 4663 episodes, tape 2026-07-23 08:55 → 07-25. Everything below was chosen from that
sample; the sample can never test it. Evidence trail: HANDOFF 2026-07-25j–n, brain
`finding/memecoin-v5-feature-study-2026-07-25`, `scripts/research/memecoin_v5_blind_replay.py`,
`memecoin_v5_feature_study.py`, `memecoin_v6_action_replay.py` (LOCO), commits `fc85d476`…`22e9ed28`.

Measured facts the design leans on (episode-grouped AUC for catastrophe label = forward executable
drawdown ≤ −50% from the break, base rate 18.4%; LOEO ranges in brackets):
- early break (held_s, flipped): **0.793** [0.781–0.819]
- arrival_rate at break: **0.732** [0.716–0.762]
- buy_share at break: 0.666 [0.650–0.685]
- own-vol ratio: 0.649 [0.629–0.673]
- depth-below-peak: 0.548 — pooled 0.690 was an episode-composition artifact → EXCLUDED.
- V5's TP/vol-DERISK gate (`realized_vol ≥ trail`) is unreachable (0.00% of 17,861 points) → replaced.
- LOCO of a 2-threshold conditional collapsed to cut-everything on bleed-regime data (30/33 folds
  tied) → the conditional is untestable on the discovery window; hence prospective-only evaluation.

## Policy (V5 = crash cutter stays frozen as its own arm; V6 below is the ONE staged candidate)

Hard rails — immutable, outrank everything, identical to funded doctrine:
envelope catastrophe (−80% from entry), unsellable CUT, decoded LP removal (degenerate
catastrophic until WO-7 registers bounds), 12h timecap. The source-wallet prior NEVER touches these.

At a **nominated break** (production condition: exec price below prior confirmed swing low AND
negative decoded net flow):

Crash score — one point per condition, all known-at-time, constants FROZEN here:
1. `early`: held_s < 10,800 s
2. `frenzy`: arrival_rate ≥ 0.10 events/s
3. `crowded`: buy_share ≥ 1/3 (buys / (buys+sells), decoded window)
4. `vol_shock`: coin's realized_vol_5m ≥ its own trailing p90 (requires ≥12 prior observations;
   fallback: chain-level p90 of the trailing hour; if neither computable → condition unavailable = 0)

Action:
- score ≥ 2 → EXIT_FULL
- score 1 → DERISK_50 (if partial budget remains, else EXIT_FULL)
- score 0 → DERISK_25 (first break only)

Staged confirmation (after any partial, before terminal exit):
- CONFIRMATION = second nominated break, OR negative decoded net flow on ≥2 consecutive sweeps,
  OR material route degradation, OR source wallet full exit → DERISK additional 50% of remainder.
- RECOVERY = executable price reclaims the break level before confirmation → no second derisk;
  remainder rides (rails always on).
- Partial budget: max 2 tranches, max 75% cumulative before a terminal exit. Replay/paper only —
  the funded one-partial CAS mechanic is not modified by this document.

Source-wallet prior (V0, modifier only):
- Shrunk copy edge per wallet: `w = n_eff/(n_eff+κ)`, κ=20 effective (recency-weighted) closes,
  hierarchy wallet×chain → chain → global.
- Effect: exactly ONE action-tier adjustment. Strong positive prior AND source still holding AND
  route healthy → one tier less selling (e.g. DERISK_50→DERISK_25). Weak/stale/negative prior OR
  source exited → one tier more selling. Never converts any state into HOLD past a score ≥2, and
  never blocks the hard rails.

TAKE_PROFIT: **shadow-only on 4663.** Log candidates (vol_shock + large executable MFE + arrival
acceleration rollover + higher-low intact + route healthy); zero execution authority this version.

## Prospective evaluation (the only test)

Implementation audit 2026-07-26: the discovery action replay cannot observe historical
point-in-time route degradation or source-wallet state. The exact `V6_BREAK_MANAGER_V0` arm therefore
reports `UNSCORABLE`; those terms are not neutralized. The prior “V6 beats cut” result is retracted
after correcting fills from the next 30-second decision point to the next priced block. In the final
sealed bundle, the n=28 single-mapped-pool subset (formerly mislabeled exact) gives cut_all and the
discovery diagnostic `v6_cond` both −$245 at h10; exact V6 is unscorable because its point-in-time
inputs were never captured. No discovery episode has entry-pool-exact identity. The clock restarts at
`2026-07-26T06:44:09.219985+00:00` after the evaluator/input-binding remediation; outcomes remain
hidden until at least 50 eligible episodes.

- Cohort: the NEXT ≥50 eligible 4663 episodes after this commit (target 100), replayed/paper-run
  under V5_CRASH_CUTTER and V6_BREAK_MANAGER_V0 on identical entries. Nothing from the discovery
  window enters the cohort.
- Primary: episode-level paired delta of net executable PnL at the 10% haircut row (0%/20%
  reported as sensitivity). Descriptive companions: exact paired sign test, 20% trimmed mean,
  drop-top-1/2 contribution.
- Guardrails (V6 must not lose these to claim success): catastrophe rate not worse than V5;
  worst-decile episode loss not worse; winner-retention (capture on oracle-max>+50% episodes)
  improved; no single episode >20% of the total improvement.
- Fidelity classes reported separately: entry-pool-exact, single-mapped-pool, and
  dominant-pool-proxy. A single mapped pool is not proof of the executable entry venue; claims cite
  entry-pool-exact first and cannot relabel single-mapped evidence as exact.
- Verdict routing: pass → V6 earns a paper arm (still not funded); fail/mixed → V0 is retired and
  any V1 needs a new prereg on fresh data.

## Explicitly out of scope this version

ML/boosted/neural models, online learning, continuous action sizing, >2 tranches, TP execution,
LP-rule weighting (needs decoder coverage first), cross-chain parameter transfer, any Solana
application, any funded-rail change.
