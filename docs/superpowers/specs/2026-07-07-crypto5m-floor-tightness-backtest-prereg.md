# PRE-REGISTRATION — crypto5m floor-tightness tape-replay backtest

Committed BEFORE building the replay or computing any floor comparison. Purpose: find the optimal
vol-floor tightness (and whether it should vary by market state) over the full 12-day capture, using a
tape replay — valid because the paper maker places NO real orders (zero market impact → the recorded
tape is exogenous to any hypothetical floor).

## Fidelity gate (MANDATORY — no floor number is trusted until this passes)
Data-presence verified 2026-07-07: forward windows have Binance (median 188/win), book snapshots
(138/win), prints (2040/win), chainlink (289/win); but 98% of windows carry `book_stale` gaps.
Therefore, before ANY floor comparison:
1. **Reconstruction match**: replay ≥30 windows for which a live campaign (frozen-f, floor 2e-5)
   recorded actual quotes. Recompute the maker's fair + quote prices from the capture tape at 2e-5 and
   compare to the campaign's recorded `maker_quotes`. PASS = median per-tick fair error ≤ 1 tick (0.01)
   AND ≥90% of recorded fills reproduced by the replay. If it fails, the backtest is INVALID and we
   report that, not floor numbers.
2. Windows failing reconstruction (excess `book_stale`, missing feed) are excluded and the excluded
   fraction is reported (no silent drops).

## Floor grid (fixed now)
`{1e-5, 2e-5, 3e-5, 5e-5, 1e-4, 2e-4}` per-second vol floor. 2e-5 = frozen-f incumbent; 1e-4 = control.

## Metric (fixed)
Per floor, replayed on the SAME windows (paired): (a) realized PnL/window; (b) filled-share count;
(c) **haircut-adjusted PnL at 0 / 1c / 2c per share** — the decision-grade output, because absolute
PnL is modeled (dies at ~2c, per the 2026-07-07 haircut finding). A floor "wins" on haircut-adjusted
PnL at 1c, not raw PnL.

## Market-state conditioning (fixed)
Neutral odds `p` = capture-book up-mid over the first 90 s (same feature as the clean-feature prereg,
verified independent + calibrated). Bins `|p−0.5| ∈ [0,0.05,0.10,0.15,0.20,0.25,0.5]`. Test whether the
optimal floor differs by state — specifically whether it should be TIGHTER in extreme-odds windows.
BH-FDR q=0.10 across bins. NOTE: the clean recompute already refuted wide-in-contested; the only
state-dependence entertained here is refinement in the tight direction.

## Rebate model (fixed)
Add the maker rebate as a separate line: per filled share against a taker, rebate ≈ 20% × taker_fee,
taker_fee = 0.07 × p(1−p) (Polymarket docs 2026-07-07), scaled by our modeled fill share. Reported as
an upper-bound tailwind (we do not know our real liquidity share), NEVER folded into the primary PnL.

## Decision rule (fixed BEFORE results)
- Fidelity gate fails → report "backtest not trustworthy on this capture"; fall back to the forward sweep.
- Gate passes → pick the floor maximizing 1c-haircut-adjusted PnL. If a tighter floor than 2e-5 wins
  AND survives the haircut, propose it as a forward challenger (pre-registered), do NOT switch a live
  campaign on backtest alone.
- State-dependence only claimed if FDR-significant on the clean feature.
- If NO floor is positive after the 1c haircut, the honest conclusion is the modeled edge is an
  execution-cost artifact and the maker is not real-money viable — report that plainly.

---

# FIDELITY GATE RESULT — FAILED (2026-07-07)

Before building the full replay, ran Stage 1 (book-feed reconstruction) against frozen-f's recorded
per-tick book (15,217 up-quote ticks with recorded best_bid/best_ask).

**Verification chain (did not rush):**
1. Confirmed frozen-f records `fair` = v3 blended `q_used` (0.99), NOT raw `fair_value_up` (1.0) — so a
   naive fair comparison would have been wrong. Pivoted to the book, the shared upstream input.
2. Capture WS book vs maker's recorded REST book: **median mid error = 1.00c (one full tick), mean 1.94c,
   only 40% exact / 48% within a tick**; 20% of ticks had no capture snapshot within 2s.
3. Ruled out timing: binned error by |time gap|. Error is **FLAT (~1c median, ~46% within-tick) even at
   ≤0.25s alignment** — it does not shrink with tighter matching. So it is a STRUCTURAL REST(maker)-vs-
   WS(capture) book difference, not a sampling-time artifact (consistent with the "book ordering REST≠WS"
   finding in memory).

**Verdict:** the capture cannot faithfully reproduce the book the live maker traded on. On a 1c-tick
market a 1c book error propagates directly into quotes and fills, so a tape-replay floor comparison would
be measuring a different market than the maker ran on. **Gate FAILS → the tape-replay backtest is
abandoned. Floor-tightness is answered by the FORWARD SWEEP** (1e-5/2e-5/5e-5/5e-4, already running on the
live REST book — faithful by construction), which just needs windows to accrue.

Bonus implication: if the WS book (capture) is the fresher one, the live maker's REST-book fills may
themselves be based on a ~1c-stale book — an independent reason the paper fills are optimistic.
