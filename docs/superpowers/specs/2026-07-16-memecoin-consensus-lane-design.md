# Memecoin consensus lane (consensus_f) — rank-2 trigger, 1–30M band, PAPER ONLY

**Date:** 2026-07-16 · **Status:** designed, awaiting build approval · **Money:** paper only; funded
promotion requires the pre-registered gate below AND the honest-fill parity work.

## Why this coin/signal class

The armed lane trades one pocket: armed-wallet buys on sub-$1M coins. This lane trades a
**disjoint** pocket with the only other measured, confound-checked effect we have:

- **Signal:** the corpus study (WS4) confirmed multi-buyer consensus — hit20 41.9% for ≥2-buyer
  coins vs 13.8% solo — measured **in the 1–30M band**, surviving the price-already-moved confound.
- **Coin class:** $1M–$30M mcap, i.e. exactly what the funded ceiling ($1M) excludes. No overlap,
  no cannibalization, and the armed lane's evidence says nothing about it.
- **Known trap, designed around:** WS4 also REFUTED waiting-for-consensus vs rank-1 copies
  (rank-k loses 20/20 cells) — for *curated* wallets, where rank-1 exists. For the non-curated
  population there is no rank-1 lane: solo unfiltered buys are the 13.8% baseline. Rank-2 is the
  earliest trigger with evidence in this population.
- Volume measured 07-09→16: **27–47 signals/day, 12–18 distinct coins/day** → one-per-coin gives
  ~14 entries/day; the 40-close read gate fills in ~1 week.

Alternatives considered and rejected for now: 4663-native lane (paper history 100%
fomo-contaminated; needs the honest-fill replay first), momentum/second-wave (re-entry NO-GO
07-09), X-catalyst entries (intel is veto-only, unquantified).

## Trigger (all must hold, evaluated on the live global-feed poll)

1. `side='buy'`, `signal_source='global_feed'`, lag < 60s (latency cliff).
2. Concrete DexScreener-priceable chain: `solana`, `8453`, `56`, `1`. **4663 excluded** until
   honest paper fills exist there (its dexscreener-less fills would be fomo-fantasy — the exact
   contamination the alert audit purged).
3. Event mcap in **[1e6, 30e6)** (resolve via same-mint feed fallback, fail-closed outside band).
4. **Rank-2:** exactly 1 distinct *other* wallet bought the same `(network_id, mint)` in the prior
   60 min (`prior_buyers_60m` — helper exists). Rank-3+ does not trigger (late = the WS4 trap).
5. Neither buyer is excluded/blocklisted; the trigger wallet needs NO arming/curation.
6. First-seen chase guard: price ≤ 2× the coin's first-seen feed price (reuse
   `first_seen_chase_ok`; fail-open on missing data).
7. One position per coin, ever, per this arm (no re-entry).

## Position + exits

- Paper arm `consensus_f`, standard $100 clip, honest fill machinery (dexscreener stats fill +
  slippage model — never the fomo fallback; if unpriceable, skip and count it).
- Exits = **control rails verbatim** (mirror on the copied buyer's sell, stop −60%, TP +500%,
  no timecap): maximizes comparability with the best-performing known policy and avoids inventing
  a new exit confound (scalp_c lesson).

## Pre-registered evaluation gate (write once, no peeking)

Read when **≥40 closes** (~1 week at measured volume):
- CONTINUE/PROMOTE-CANDIDATE: total PnL > 0 AND drop-top-2 PnL > −$50 AND hit20 ≥ 25%.
- KILL: hit20 < 15% at n ≥ 60 signals (indistinguishable from the solo baseline), or total PnL
  < −$500 at any read point.
- Funded promotion is NOT automatic: it additionally requires the real_shadow honest-fill parity
  work and its own operator-approved sizing/gates.

## Implementation sketch (single small PR)

`sniper/services/memecoin_runner.py::run_once`, in the post-funded-hook section (same slot as
candidate-cohort registration — zero copy-path latency): evaluate the trigger per buy event,
open via the existing paper fill path with `strategy='consensus_f'`. Settings:
`MEMECOIN_CONSENSUS_F_ENABLED` (default 1), band bounds, window (3600s), rank (=2) —
all env-overridable. Add `consensus_f` to `MEMECOIN_ARMS` handling where arm lists are
enumerated. Tests: trigger unit tests (rank counting, band, chain exclusion, blocklist veto,
first-seen guard) + one run_once integration test. No schema changes
(`memecoin_positions.strategy` is TEXT).

<!-- schema:external=memecoin_positions -->
<!-- schema:external=memecoin_events -->

## What could go wrong → mitigation

- **Rank-2 in a 60min window on a days-old 1–30M coin is a different beast than a fresh launch**
  → the first-seen chase guard and the band bound it; the eval gate decides, not intuition.
- **Feed shows only fomo users, not the whole market** → same population WS4 measured; the effect
  is defined on this feed, so the lane tests exactly what was measured.
- **Arm sprawl / forking paths** → this replaces nothing and reads only at its pre-registered
  gate; matched-event comparison vs control where mints overlap.
