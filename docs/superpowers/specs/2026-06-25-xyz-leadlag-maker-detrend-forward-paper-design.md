# xyz Cross-Asset Lead-Lag — Maker + De-trend + Forward Paper (design)

Date: 2026-06-25
Status: DRAFT (brainstorming output, pre-plan)
Branch: research/hl-measurement-audit-leadlag
Related: [[project-hyperlive-scanner]], `2026-06-22-day-trading-techniques-sourced-research.md`,
`finding-crypto5m-binance-leads-book-maker-edge` (taker-dead / maker-edge precedent)

## 1. Problem

The xyz HIP-3 cross-asset lead-lag is the only HL paper line left running. Current audit
(`scripts/run_xyz_leadlag_audit.py` → `scan_audit.basis_direction_edge`) finds exactly **one**
cell that clears cost:

- `@1800s, |basis|≥20bps`: aligned +10.2 bps, **after-fee +1.2 bps**, hit 62%, t=3.29, n=463.

Everything shorter (150s/300s/900s) is "no edge." The thesis (perp lags the real underlying; a
1–2% underlying move clears the 9 bps taker fee) is alive but marginal. We want to give it a
fighting chance without fooling ourselves.

### What the data says (verified 2026-06-25, 44.6h of session data)

The |basis|≥20bps signal is **not a thin-name artifact**. Row counts by asset:
SP500 205, GOLD 175, SILVER 107, BRENTOIL 98, COPPER 64, MU 42, CL 41 — i.e. the liquid,
~24h commodity + index perps. Single-name equities contribute ~1 row each (session-gated,
tight-tracking). This is the *good* case: the edge lives in the deepest, most-tradeable,
nearly-24h markets.

Two caveats that shape the design:
1. **Thin sample**: 44.6h ≈ 2 trading sessions; n=463 is one t-test away from noise.
2. **Structural basis contamination**: SP500 and COPPER show avg |basis| ~100bps. A persistent
   ~1% premium is roll/contango/funding structure, **not** a lag. The current `MAX_SANE_BASIS_BPS`
   = 1000bps clamp does not remove a stable 100bps offset. Raw-basis signals partly measure
   convergence of a structural premium, which is not a repeatable intraday edge.

## 2. Design decisions (with alternatives)

### D1 — Trade only the 24h-clean universe; keep capturing the rest for research
**Decision:** The *tradeable* set is exactly these **10 canonical instruments**. Multiple xyz
aliases map to the same underlying — they collapse to ONE canonical instrument to avoid
pseudo-replication in the stats (an alias pair like SP500/US500 is one signal, not two).

| Canonical | xyz alias(es) | Yahoo ref | Class |
|---|---|---|---|
| WTI_CRUDE | CL, OIL | CL=F | commodity |
| BRENT | BRENTOIL | BZ=F | commodity |
| GOLD | GOLD | GC=F | commodity |
| SILVER | SILVER | SI=F | commodity |
| COPPER | COPPER | HG=F | commodity |
| NATGAS | GAS | NG=F | commodity |
| SP | SP500, USA500, US500 | ES=F | index |
| NASDAQ | USTECH, USA100 | NQ=F | index |
| EURUSD | EUR | EURUSD=X | fx |
| USDJPY | JPY | JPY=X | fx |

When more than one alias for a canonical instrument has a live HL market in a refresh, pick the
**single most-liquid alias by `day_ntl_vlm`** for that refresh; never sum or double-count
correlated aliases. All clustering, concentration caps, and signals operate on the **canonical**
key, not the raw alias.
**Why:** These carry the signal (verified: SP/GOLD/SILVER/BRENT/COPPER/CL dominate |basis|≥20),
are ~24h (more accrual, no cash-session gate), and are deepest.
**Out of the trading set (still captured for research):** all single-name equities (NVDA, TSLA,
AAPL, MSFT, GOOGL, AMZN, META, AMD, COIN, HOOD, PLTR, MSTR, NFLX, ORCL, MU, INTC, BABA, CRCL,
COST, LLY) — session-gated, tight-tracking, ~1 row each at |basis|≥20. Synthetic/private names
(SPCX, XYZ100, SKHX) have no public ref and are excluded entirely.
**Alternative rejected:** trade everything — dilutes with session-gated names and double-counts
same-ref aliases.

### D2 — De-trend the basis (signal = deviation from each asset's rolling-median basis)
**Decision:** `basis_dev = basis − prior_median`, edge test on `basis_dev`, not raw basis.
Pre-registered params (FROZEN before forward paper; any change → new run_id per D6):
- **Window:** time-bounded **last 60 minutes** of prior snapshots per canonical instrument (NOT
  last-N rows — a time bound stops a stale pre-close median bleeding across a market gap).
- **Min obs:** require **≥ 12 prior in-window snapshots** before any signal is allowed; otherwise
  skip (insufficient baseline, fail-closed).
- **Prior-only:** median computed over rows with `scan_ts < current scan_ts` strictly — the current
  row is **never** in its own baseline (no lookahead).
- **Update cadence:** recomputed every snapshot (rolling), per canonical instrument.
**Why:** Removes the persistent structural premium (the COPPER/SP ~100bps offset) so the test
measures *transient dislocation* (the actual lag), not convergence of a standing roll basis.
**Alternative rejected:** keep raw basis — conflates structural premium with lag; inflates the
"edge" with non-repeatable convergence. Last-N-rows window rejected — gaps poison it.

### D3 — Maker entry, not taker (the biggest lever)
**Decision:** Add a **maker-fill lane** to the audit and the forward paper: model entry as a passive
limit posted at/inside the lagging side, exit likewise. Maker round-trip ≈ 0 bps (or a small
rebate) vs the 9 bps taker round-trip.
**Why:** At a 30-min horizon the move is slow — there is time to be filled passively. The same
taker-dead/maker-edge result already holds for the crypto lead-lag
(`finding-crypto5m-binance-leads-book-maker-edge`). With a ~0bps maker round-trip the current
+1.2 bps after-taker-fee signal becomes ~+10 bps gross-of-fee headroom; even a partial-fill,
adverse-selection-discounted maker model should clear comfortably.
**Maker-fill model — pre-registered (FROZEN before forward paper; change → new run_id per D6):**
- **Quote side/price:** post a passive limit on the entry side at the **near touch** — long → join
  best **bid**; short → join best **ask**. (Posting at mid−ε is a variant; pick join-the-touch and
  freeze it.) Exit: post passive on the opposite side at the horizon target.
- **K (time-in-force):** entry K = **60s**, exit K = **60s**. Unfilled after K → cancel.
- **Touch-through (fill) definition:** entry counts as filled iff, within K, a **trade prints at or
  through our limit price** (or the opposite best crosses our limit). The mid merely touching our
  price is **not** a fill.
- **Cancel/expiry:** unfilled at K → cancel → the signal becomes **no-trade** (not a win, not a
  loss; excluded from PnL, counted in fill-rate).
- **Partial fills:** audit is **all-or-nothing** (conservative). Real partials only shrink size; the
  paper grades full size on a fill, no-trade otherwise. Document this bound.
- **Adverse selection is captured by construction:** fills happen only when price comes to us, and
  PnL is graded **from the fill price forward** — so we never count signals that didn't fill, and
  the realized entry already embeds the adverse move. Mid-to-mid is the optimistic bound; this
  fill-conditioned model is the realistic one.
- **Fee/round-trip:** maker-in = **0 bps** (no rebate assumed unless verified on HL HIP-3). Exit:
  if the passive exit doesn't fill within exit-K, **force a taker exit at +4.5 bps** (cross to
  close at the horizon). Pre-register round-trip = maker-in(0) + {maker-out(0) | forced taker-out(4.5)}.
**Alternative rejected:** taker only — barely clears at one cell; no margin for slippage/spread,
which `scan_audit` explicitly does not model.

### D4 — Forward paper lane (currently audit-only)
**Decision:** Add a forward paper runner that opens *paper* positions on the maker-gated,
de-trended, 30-min signal in the 24h-clean universe, and grades net-of-fee with the maker model.
Pre-registered, fail-closed, no live orders.
**Why:** The audit is a backward replay over accrued snapshots; a forward paper lane is the
out-of-sample test (same discipline as btc5m forward-paper-v0).

### D5 — Pre-registered evaluation
**Decision:** Milestones at n=200 / 500 / 1000 filled paper trades. Pass = ALL of:
- mean net > 0
- one-sided clustered-t (cluster by **canonical-instrument × hour**) p ≤ 0.05
- positive in ≥2 of 3 chronological thirds
- **≤40% of PnL** from any single canonical instrument
- **≤40% of filled signals (row count)** from any single canonical instrument ← concentration by
  count, not just PnL, so one always-firing instrument can't dominate the sample
Kill at n=500 if net CI upper bound < 0 (same rule that just killed microstructure b1).

### D6 — Run-id discipline (no tuning forward paper after seeing results)
**Decision:** A forward-paper **run_id / manifest** pins the full param set: canonical universe,
detrend window/min-obs, maker-fill params, basis threshold, horizon, fee model. **Any change to any
of these starts a NEW run_id**, and forward-paper trades are **never pooled across run_ids** —
each run_id is evaluated on its own from its own start. This makes it impossible to tune on
observed forward results and then claim the pooled record. Mirrors the microstructure manifest and
btc5m-forward-v0 discipline. The manifest (params + git commit) is written at runner start and
stored on every paper row.

## 3. Components

| Unit | File | Change |
|---|---|---|
| Basis de-trend | `sniper/hyperlive/xyz_leadlag.py` | add `detrend_basis(rows, window)` (pure) |
| Maker-fill model | `sniper/hyperlive/scan_audit.py` (or new `maker_model.py`) | `maker_fill(signal, touches, k_seconds)` → filled? entry/exit px |
| Audit grid | `scripts/run_xyz_leadlag_audit.py` | add detrended + maker lanes alongside the existing raw/taker lane (keep the control) |
| Forward paper | new `scripts/run_xyz_leadlag_paper.py` + watchdog | opens/closes/grades paper trades to a new table |
| Schema | `sniper/hyperlive/scan_log.py` or intel store | `xyz_paper_trades` (run_id, canonical, alias, side, detect_ts, fill_ts, entry_px, exit_px, exit_kind{maker,forced_taker}, horizon, basis_dev, net_bps, status{filled,no_trade,open}) |
| Manifest | new `xyz_paper_runs` (run_id, params_json, git_commit, started_ts) | one row per run_id (D6) |
| UI | route + app.js panel | surface forward-paper net + per-lane audit (raw-taker vs detrend-maker) |

## 4. Data flow

capture (unchanged) → snapshot rows in `hyperlive_scan_log` → **de-trend per asset** →
**maker-gated 30-min signal** in 24h-clean universe → forward paper opens/exits → net-of-fee grade
→ pre-registered eval + UI.

## 5. Testing

- Pure-function tests: `detrend_basis` (removes a constant offset; preserves a transient spike),
  `maker_fill` (fills only when touch trades through; unfilled = no-trade).
- Audit regression: the existing raw/taker cell still reproduces +1.2bps after-fee (don't break the
  control); detrended-maker lane produces a defined verdict.
- Forward-paper: one end-to-end test from a synthetic snapshot series (entry → exit → net).

## 6. Risks / honest framing

- Still a thin, long-horizon, partly session-gated edge. "Fighting chance," not a proven edge.
- Maker model is the load-bearing optimism. If maker-with-adverse-selection doesn't clear, the line
  is dead — that is a valid, documented outcome.
- 44.6h of data; real verdict needs the forward milestones. Do not promote on in-sample audit.

## 7. Out of scope

Live orders. Cross-asset hedging. Equity single-name trading. Anything beyond paper validation.
