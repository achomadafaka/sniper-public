# BTC 5-minute Algorithm — Design

**Date:** 2026-06-18
**Status:** building (autonomous, operator-directed)
**Scope:** A self-contained, paper-only BTC 5-minute *algorithmic* trading strategy we build ourselves — NOT a wallet-copy strategy. Runs locally on Binance data, fires every 5 minutes, accumulates a paper track record fast, and surfaces live in the app.

## Why BTC 5-min (decision rationale — operator offered "BTC 5-min OR weather algo")

Chosen BTC 5-min over a weather algo because it best fits the operator's stated constraints and the project's pain point:
- **Fires constantly** (288 bars/day, 24/7) → directly fixes "nothing happens / 0 bets for days". A weather algo depends on Polymarket weather-market listings and resolves slowly.
- **Fully local + free data**: Binance public REST klines, no API key, already wired in the repo (`scripts/backfill_binance_klines.py`, `crypto_momentum_v1.py`). Aligns with "everything running locally" and "use Binance server time" (never trust the local +7h-skewed clock).
- **Compute-fits the hardware**: walk-forward backtests + param sweeps over years of 5m bars are genuinely compute-bound.
- **Fast honest feedback**: paper trades resolve in minutes, so a real out-of-sample track record accrues in days, not the off-season-bound months of the sports harness.

Honesty caveat carried from the repo's own research (HL signal research v2.1: "no actionable HL signal after debiasing", "HL fees > moves"): crypto micro-timeframe is largely efficient. We therefore build a *rigorous, cost-honest* backtest and report EV truthfully. If no signal beats fees out-of-sample, that is a valid, documented result — and the framework + live surface remain the deliverable. This is paper-only; no live orders, ever, in this scope.

## Architecture — `sniper/algos/btc5m/`

Small, single-responsibility, mostly-pure units (testable without network/DB):

| Unit | File | Responsibility |
|---|---|---|
| Data adapter | `data.py` | Fetch 5m klines from Binance REST (server-time aligned); pure parse of raw kline rows → OHLCV bar dicts; cache to `btc5m_bars`. |
| Features | `features.py` | Pure: EMA, RSI, rolling z-score, ATR, realized volatility over a bar list. |
| Signals | `signals.py` | Pure: per-bar position ∈ {-1,0,1}. `ema_crossover`, `donchian_breakout`, `meanrev_zscore` (with a volatility-regime gate). Config-driven. |
| Backtest | `backtest.py` | Pure: positions × bars × cost model → trade PnL, equity curve, metrics (total return, Sharpe, profit factor, max drawdown, n trades, win rate, exposure). Walk-forward OOS split. |
| Forward | `forward.py` | Live paper: advance on each new closed 5m bar, open/close paper positions on signal change (fee-adjusted), server-time stamped, against a FROZEN config + pre-registered kill criteria. |

Persistence (`sniper/persistence/db_btc5m.py`, `ensure_btc5m_tables`):
- `btc5m_bars` — OHLCV cache (PK symbol+open_ms). <!-- schema:external=btc5m_bars -->
- `btc5m_paper` — forward paper trades (entry/exit ts+px, side, fee, pnl, status). <!-- schema:external=btc5m_paper -->

## Cost model (the part that kills naive crypto signals)

Per round-trip: `taker_fee` (Binance spot taker 0.04%–0.10%/side, default 0.05%/side = 0.10% round-trip) + `slippage` (default 0.02%/side). A signal must clear ~0.12%+ per round trip to be +EV. The backtest charges cost on every position change. Frozen into the baseline hash so it can't be tuned post-hoc.

## Evaluation & pre-registered kill criteria (forward, OOS)

A signal "passes" only on out-of-sample forward data with:
- n ≥ 200 closed paper trades,
- Sharpe (per-trade, annualized) ≥ 1.0,
- profit factor > 1.1 after costs,
- max drawdown ≤ 25%,
- no single trade > 40% of total PnL.

Until then the live surface shows it as "gathering evidence — not proven". Never claim it works before the bar clears.

## Live surface (the "live & interactive" requirement)

`/api/btc5m/status` → latest price (Binance server-time), current signal/position, paper equity curve + live metrics; `/api/btc5m/backtest` → frozen in-sample + walk-forward summary. A dedicated **Algo** tab (or Operations section): live BTC price, current signal (LONG/FLAT/SHORT), paper equity sparkline, and the honest backtest stats with the "paper-only, not advice" disclaimer. Auto-refreshes every ~30s while visible.

## Result (post-build + adversarial review, 2026-06-18)

First sweep looked strong (meanrev OOS Sharpe ~6.9, PF 1.63). An adversarial review caught two
artifacts: (1) **fill-at-extreme-close** — the mean-reversion entry was booked at the very bar whose
close triggered the ≥2.5σ signal (a resting limit only fills *through* the band, with adverse
selection); (2) **selection on the OOS slice** — the sweep ranked configs by `oos_sharpe`, making the
holdout in-sample to the picker. Both fixed: backtest now applies a **1-bar execution lag** (fill at
the next bar's close) and the sweep **selects on train, reports OOS untouched**; the forward harness
books next-bar-close fills and no longer claims to "measure real fills."

**Honest result after the fixes: NO signal clears the train gates on 45d of BTC 5m.** The edge was an
artifact; BTC 5m is efficient under realistic execution (confirms the repo's prior crypto-micro
research). Deliverable = the rigorous, look-ahead-free, cost-aware framework (which correctly rejects
the false edge) + a live, honest BTC Algo surface that shows the null and monitors a candidate
forward. No edge is claimed. See `finding review-btc5m-maker-fill-and-oos-selection-2026-06-18`.

## Extension: multi-asset feature search (2026-06-18, operator-greenlit)

To hunt for an edge honestly (rather than firing more often, which the data shows is strictly worse —
lowering the z-entry just pays more fees), added a walk-forward search across more candidates:
- New features: `features.volume_zscore` (capitulation detector). Vol-regime via existing `realized_vol`.
- New signal: `signals.meanrev_confirmed` — fade a z-extreme ONLY when confirmed by a volume spike
  (`vol_z >= min_vol_z`) AND a calm regime (`realized_vol <= max_vol`); "fade capitulation, not drift."
- `sniper/algos/btc5m/search.py`: expanding walk-forward folds, per-config evaluation, and **TRAIN-only
  selection** (gate + rank on train; OOS aggregated over folds and REPORTED with a `confirmed` flag and
  the WORST-fold Sharpe surfaced so regime-dependent noise can't masquerade as edge).
- `scripts/btc5m_search.py`: multiprocess sweep over {BTC,ETH,SOL} × {meanrev, meanrev_confirmed} × params.
  Speed: 84 configs in ~1.9s on 16 workers (~22 ms/config). numpy gives ~174× on the z-score hot path
  but the sweep bottleneck is the correctness-pure backtest walk, so we parallelize (safe) and leave that
  path pure rather than risk the cost-accounting (the artifact lesson).

**Result (45d, 4 folds): NO ROBUST EDGE.** No asset/config cleared the OOS gate. The closest (SOL,
mean OOS Sharpe +2.52) had a worst-fold Sharpe of −8.26 — the worst-fold guard correctly rejects it as
regime-dependent noise. Volume-confirmation + vol-regime + multi-asset did not change the verdict: 5m
crypto mean-reversion is efficient under realistic execution. The harness is reusable for more assets,
features (e.g. funding rate — a clean next increment), and more history.

## Out of scope

Live/real orders; leverage/perp execution; multi-asset (BTC only for v1); ML models (v1 is rule-based signals — ML is a later iteration once the harness + data pipeline are proven).
