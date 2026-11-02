# Scanner Closeout — ship it, move to SELECTING

Get the universe scanner to "incorporated, working, self-improving" with the minimum
defensible, then move on. The scanner is not finished — it has a logging spine, honest docs,
and a signal that promotes itself from data in the background. Live selection untouched.

## Definition of done — MET
- [x] `scan_log` persists all ~230 markets per refresh: score components, shadow rate
      signals (incl. basis_change/accel, oi_delta/accel, mid_return, premium_change),
      selection state, prior-OI. Verified: 230 assets/refresh, full fields.
- [x] Forward-label job (`scan_audit`) over the log: 150s/300s for all (context cadence),
      finer only where deep feeds exist; conservative cost-clearing flag, never PnL.
- [x] Honest one-liner + known-gaps footer (below).
- [x] Self-promoting signal: `recall_lift` runs the acceleration component through a
      pre-set recall bar; promotes ONLY from held-out data, else stays shadow.

## First live recall read (10 refreshes)
recall@12 ≈ 9–11% of cost-clearing movers; precision@12 ≈ 60%. Mark-oracle **acceleration**
lift = −0.1pp → **stays shadow** (bar 5pp). Caveat: 10bps cost-clearing label is loose at
this cadence and likely catches small-move noise on markets we deliberately gate out; the
low recall is under investigation (loose label vs real miss), not yet an action.

## Honest one-liner (corrected)
The score ranks markets by **signals we believe precede fee-clearing moves** (mark-oracle
dislocation, funding/OI crowding, premium, 24h move) — a **hypothesis**, not a measured
probability, until the recall audit confirms it. Ranking ≠ explanation; `lead_reason` is UI
only and never defines the model.

## Known gaps (parked, not bugs)
- Top-12 is a hard cap; market #13 is invisible (no miss-rate gate yet — recall audit will size it).
- Cross-venue lead + liquidation-cluster signals live in Tier 2 (per-cycle) only → a cascade
  on an unwatched market isn't seen until a Tier-1 level signal pulls it into the watch set.
- ΔOI needs two refreshes (~150s) to register — slow.
- $5M dayNtlVlm is a 24h-volume proxy for tradability, not current fillability.
- Scoring weights are hand-set; self-calibration via `scan_log` is pending (Phase 4 horse-race).

## Hysteresis — PARKED
Only needed if a rate signal (acceleration) is promoted to live (it thrashes the watchlist).
Acceleration stayed shadow → hysteresis parked with it. Promote path: rank≤12 to add, demote
only after rank>18 for two refreshes, + min_watch_duration + cooldown.

## Parked backlog (do NOT build now; triggers)
- Liq-cluster proximity + emergency-promote → trigger: confirm `hl_liquidations` globally
  queryable cheaply; if per-market/subscription-gated, can't live in the cheap pass.
- BTC→alt internal lead → trigger: next signal through the `recall_lift` harness once accel settles.
- Cross-venue sentinel → trigger: Binance receive-time capture (separate spec).
- Score horse-race / self-calibration → trigger: after SELECTING v0; `scan_log` accrues meanwhile.

## Green light
DoD met. The scanner logs honestly, labels forward moves, and promotes its one upgrade from
data on its own. **Proceed to SELECTING.** Run `python3 scripts/run_scan_audit.py` anytime
for the live recall verdict; acceleration promotes itself if/when it clears the bar.
