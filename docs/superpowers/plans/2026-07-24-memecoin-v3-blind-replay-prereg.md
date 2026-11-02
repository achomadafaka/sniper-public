# v3 blind replay read — PRE-REGISTRATION (frozen before any result)

**Date:** 2026-07-24 · **Engine under test:** `exit-brain-v3` (frozen prereg, same file) ·
**Run once, no re-slicing, no constant may change because of this read.**

## Eligible set
Closed positions, `network_id='4663'`, opened inside the tape window
(2026-07-23T08:55 → tape end), arms `control` and `real_shadow` (reported separately, never
pooled). A position is skipped (and counted) when its pool is unresolvable in `pool_meta`, has
fewer than `MIN_STRUCTURE_POINTS` swap-blocks in its window, or its headers cannot be fetched.

## Method (one code path)
Per position: tape rows for its pool from entry to entry+12h (or tape end = censored). Walk
block-by-block; at each block call the PRODUCTION `compute_state` on the tape-so-far (no
lookahead) and `propose_v3` with the position's live-policy contract. Apply:
RIDE→hold; DERISK/TAKE_PROFIT→sell the proposed fraction at the NEXT block's VWAP;
CUT→full exit at the NEXT block's VWAP; DEFER→envelope only (catastrophe −80% on the same
next-block fill basis; timecap 12 h). `exact_quote_misses` is not simulatable offline — the
unsellable CUT is reported as not-exercised. Remainder at tape end closes at last block VWAP,
flagged censored.

## Fill honesty
Nominal per-block VWAP with three haircut variants: 0%, 10%, 20% (the measured
executable-vs-chart band from the gap probe). No same-block fills. All three reported; claims
use the 10% row.

## Report (all of it, no selection)
Per arm: n, v3 PnL vs actual arm PnL on the SAME entries; per-action counts and per-action PnL
(RIDE/DERISK/TP/CUT/DEFER share); capability/defer rate; skipped + censored counts;
drop-top-1 and drop-top-2 winner sensitivity.

## Branches (frozen)
- Safety: any replayed decision that violates monotonic risk-reduction → v3 has a bug; fix the
  bug, not the constants.
- The PnL comparison is DESCRIPTIVE evidence for the operator's canary decision. It does NOT
  authorize editing any v3 constant (that needs a new prereg) and does NOT substitute for the
  registered live gate (n≥30 live, §3 of the v3 prereg).
- If v3 underperforms both arms at the 10% haircut: report it plainly; canary decision stays
  with the operator.
