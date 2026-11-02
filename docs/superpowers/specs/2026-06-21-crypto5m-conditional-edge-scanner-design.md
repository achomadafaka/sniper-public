# Crypto5m Conditional-Edge Scanner — design

Date: 2026-06-21
Status: design (pre-implementation)

## Why (the reframe)

We proved the active taker cannot beat the Polymarket book on **average calibration**: the full quant
ensemble (`q_ensemble`/`q_trade`) and every component (diffusion, Monte-Carlo, implied vol) all have a
worse pooled Brier than `q_market` (book 0.1386 vs best model 0.1413), at every checkpoint. So "compute a
better P(Up) than the book and trade the gap" is dead.

But Brier is the wrong test for *making money*. Average calibration ≠ profitability. A selective trader
trades a **subset** — it can be worse on average and still +EV if it only fires when a specific, repeatable
signal shifts the true probability away from the odds. And the odds are an **input** (a well-calibrated base
rate), not the target: we look for the **residual** signal the odds under-weight.

The right question, per candidate signal: **conditional on the signal firing, does buying the favored side
at the real executable price settle +EV?** This scanner answers that for a set of theses, individually and
combined, and reports the full landscape with confidence tags (operator decides what to forward-test).

## Goal & non-goals

- GOAL: a read-only research scanner over the captured resolved windows that measures, per thesis × params ×
  combination, the net settlement EV of trading the signal-favored side at the executable price, with n, WR,
  bootstrap CI, and an OOS-hold flag.
- NON-GOAL: it does **not** modify the live engine or place trades. Survivors graduate to **forward paper**
  first, then (only if they hold) to the engine. This scan proposes; forward validation confirms.

## The EV metric (exact, realistic)

At a decision point a thesis says "buy side S at the executable ask A_S". Outcome `y ∈ {0,1}` = did S win at
settlement (Polymarket winner; never Binance). Per-trade PnL on a 1-share buy:

```
pnl = (1 if S won else 0) − A_S − fee(A_S)        # executable ask, not the mid; fee = realized rate
net_edge = mean(pnl over all fired trades)          # +ve = the signal beats the price it pays
```

Use `up/down_executable_price` (or `*_fee_inclusive_cost`) so slippage/fee are real. Report net_edge with and
without fee (the fee is unresolved — Gamma 0.07 vs realized 0).

## Data sources (capture DB, read-only)

<!-- schema:external=crypto5m_book_snapshots --> <!-- schema:external=crypto5m_binance_observations -->
<!-- schema:external=crypto5m_chainlink_ticks --> <!-- schema:external=crypto5m_settlements -->
<!-- schema:external=crypto5m_markets -->
Per window: Binance 1s path (`crypto5m_binance_observations`), both-token book
(`crypto5m_book_snapshots`: best_bid/ask + bids/asks arrays for imbalance/microprice), Chainlink ticks
(`crypto5m_chainlink_ticks`), settlement (`crypto5m_settlements.winning_outcome`), market identity + start
(`crypto5m_markets`, `price_to_beat`). Reuse the `crypto5m_active_replay.py` window loader.

## Decision grid

Every `STEP` s (default 3–5s) within each window, from ~elapsed≥30s to seconds_left≥`MIN_LEFT`. Each
decision point is one candidate observation (a thesis may or may not fire there).

## Signal library (each a pure function `signal(feat) → (fires, side) | None`)

1. **mean-reversion-near-the-money** — fires when |short_return_z| > z AND the binary is near-the-money
   (|odds−0.5| < band). Side = the REVERSION side (opposite the recent move / the cheap side). Params: z,
   band, return horizon.
2. **odds-overshoot** — fires when |midpoint_change_Δs| > m (odds rushing). Side = AGAINST the odds move
   (fade). Params: Δ, m. (Pure market microstructure — needs no BTC.)
3. **settlement-lead** — fires late (seconds_left < L) when Binance moved (return_Ns > r) but the odds lag
   (odds vs Binance-implied diverge). Side = the Binance direction. Params: L, N, r.
4. **vol-conditioned-distance** — our diffusion P(Up) vs the odds, but only fire when the vol regime
   (realized_vol percentile) says the odds mis-weight reversion/continuation. Params: vol band, edge.
5. **microprice-lead** — fires when book microprice diverges from the mid by > d (book pressure). Side = the
   microprice direction. Params: d.

## Combinations

AND-combine any signals that co-fire at the same decision point (e.g., mean-reversion ∧ near-the-money ∧
quiet-odds). Test all pairs + a few triples that are theoretically coherent. Report each combo as its own row.

## Holding period (decided per-thesis by the scan)

For each fired trade, evaluate **multiple exits** and report each thesis's best:
- `settle` — hold to the 5-min close (the EV metric above; backtest-faithful, real outcome).
- `horizon` — exit at the signal's natural horizon (e.g., mean-reversion: exit when |return_z| reverts below
  a band or after K s; settlement-lead: exit at close). Modeled against the book bid at that later tick (less
  faithful — flag it).
Per-thesis winner = the exit with the best net_edge that is also backtest-trustworthy.

## Output

Ranked table: `thesis | params | combo | n | WR | net_edge (fee0 / feeDoc) | bootstrap 95% CI | OOS_hold`.
OOS_hold = sign of net_edge agrees on an older-half fit and newer-half test (informational tag, not a gate —
operator chooses what to forward-test). Written to `logs/` + printed.

## Architecture (clean units)

- `window loader` (reuse replay) → per-window arrays.
- `feature computer` → the focused feature set per decision point (returns/z, vol, VWAP-distance, imbalance,
  microprice, midpoint + Δ, basis, time). Reuse `live_desk`/`feature_registry` helpers where they exist.
- `signal library` → the 5 pure thesis functions + a registry.
- `evaluator` → fired trades → EV/WR/n/CI per exit.
- `combo + sweep driver` → param grids + AND-combinations.
- `reporter` → ranked table + confidence tags.

## Risks

- **Multiple comparisons / small n** (~100 windows, ~400 decisions): sweeping many signals throws false
  positives. Mitigation: report n + bootstrap CI + OOS_hold on every row; nothing goes live from the scan —
  only forward paper. Operator reads confidence, not just the top row.
- **Executable-price realism**: use the executable/fee-inclusive price, not the mid, so the edge is net of
  what we'd actually pay.
- **Look-ahead**: every feature uses data ≤ decision_ts; only the settlement label is future. Reuse the
  replay's audited cutoff discipline.
