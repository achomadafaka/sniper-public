# Crypto5m Adaptive Maker V3.2 — Markout Reward + Vol-Floor Challenger Design

**Date:** 2026-07-06
**Status:** Approved for paper-only implementation
**Basis:** 2026-07-06 full readout of the five live paper campaigns (see memory
`project-crypto5m-active-engine`). Two findings drive this spec:

1. The V3/V3.1 reward is `settled_pnl − $1·hard_cut` only. The design formula's
   adverse-selection term was never implemented (`adverse_selection_10s_bps`
   inserted as NULL) — yet adverse selection is the documented v4 loss mechanism,
   and the V2 learner already computes 10s markouts on 97% of its reward rows
   (`maker_adaptive._reward_markouts`).
2. `VOL_FLOOR_PER_S = 1e-4` (live_desk.py) still binds ~84% of ticks (raw vol
   p50 = 5.1e-5) and compresses the structural fair toward 0.5. The V3.1
   fair-blend learner is voting `market_only` in nearly every context — it is
   learning to route around the bug. `vol_floor_shadow` (296 windows) shows a
   0.25-structural blend at a lower floor matches market-mid Brier with ~3×
   realized-per-share on fills.

## Decision

Launch TWO single-lever paper challenger campaigns off the V3.1 code, and
retire the two dominated campaigns to keep the process count flat:

```text
maker-adaptive-paper-v3.2r   lever: reward     (markout-penalized reward)
maker-adaptive-paper-v3.2f   lever: fair anchor (vol floor 2e-5, per-process)
```

Control for paired review: `maker-adaptive-paper-v3.1` (lever-isolated
baseline) plus `maker-regime-v1` (global control) — both keep running.

Retired (watchdog stanzas commented, DBs/history immutable, processes stopped):

```text
maker-adaptive-paper-v1   paired-worst vs every sibling (z=−2.06 vs v3)
maker-adaptive-paper-v3   superseded by v3.1 (same brain, inert learner loops)
```

## Hard Boundary

Same as V3/V3.1: no live orders, no private keys, no production DB, no mutation
of any prior campaign history. Paper only.

## V3.2r — Markout Reward

```text
reward_version = maker-reward-v3.2
reward = settled_fill_pnl
       − hard_cut_penalty ($1 per hard-cut fill)
       − adverse_selection_penalty
adverse_selection_penalty = max(0, −adverse_selection_10s_bps) / 10_000 × size
```

- Markouts come from the existing `maker_adaptive._reward_markouts` (future
  `maker_quotes` snapshot at t+10s, fill-direction signed) — same math the V2
  learner uses; no new measurement code.
- `adverse_selection_10s_bps` is stored on the reward row and accumulated into
  `crypto5m_maker_v3_action_arm_stats.adverse_sum`, which arms the existing
  (currently dead) `mean_adverse < −50bps` leg of the catastrophic-disable gate.
- The campaign's `reward_version` is read from `crypto5m_maker_v3_campaigns`
  (runner flag `--adaptive-reward-version`); V3.1's reward path is untouched —
  a V3.1 campaign continues producing `maker-reward-v3.1` rows byte-identically.
- Learner/selector logic unchanged (`learner_version` stays v3.1 semantics);
  the only delta is what the arms are scored on.

## V3.2f — Vol-Floor Fair

```text
--vol-floor 2e-5   (runner flag; sets live_desk.VOL_FLOOR_PER_S in-process)
```

- 2e-5 binds ~11% of ticks (vs 84% at 1e-4) per the vol-floor shadow.
- Per-process override only: each campaign is its own OS process, so the
  control's and siblings' fair values are untouched. The module constant is NOT
  edited.
- Everything else identical to V3.1 (reward stays maker-reward-v3.1, learner
  loops on). The fair-blend learner now sees an uncompressed `q_structural`;
  whether it starts trusting structural weight again IS the experiment.

## Evaluation (pre-registered, per campaign)

- Primary: paired same-window realized PnL diff vs `maker-adaptive-paper-v3.1`
  and vs `maker-regime-v1`.
- Kill: window-clustered bootstrap CI-upper < 0 at n ≥ 200 settled.
- Watch: print-fill share, hard-cut frequency, and for v3.2r the fraction of
  reward rows with non-null adverse markout (V2 reference: 97%).
- Do not pool with any prior campaign. Do not claim production edge.

## Scope Guards

- No hand-tuned per-context rules.
- No changes to V1/V2/V3/V3.1 campaign rows or reward rows.
- No UI work required (cockpit reads per-DB; new DBs are
  `data/crypto5m/maker_adaptive_v3_2r.sqlite` / `..._v3_2f.sqlite`).
