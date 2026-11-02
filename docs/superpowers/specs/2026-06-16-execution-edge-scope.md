# Scope — Execution-edge (latency + rebate) on Polymarket

**Date:** 2026-06-16
**Status:** scope / design-only. NO build, NO live trading. Real-money is operator-gated per `CLAUDE.md`.
**Premise:** the rigorous investigation (2026-06-16, see finding `execution-not-rule-edge`) showed there is **no paper-rule-able source-lag edge** on accessible Polymarket inventory + free feeds. Liquid/major markets (good feeds) reprice efficiently; illiquid niche markets (plausible lag) have no accessible feed + opaque timing. The scrapers' real profit is therefore **execution-side**, not a static rule. This doc scopes what capturing that would actually require — so the cost/risk is visible before any commitment.

## 0. Validation results (2026-06-16) — what's measured

- **Taker latency-fill: CONFIRMED NO-GO.** 4 real BTC Up/Down candle closes captured (real-clock-aligned). In all 4, the determined WINNER's asks vanish post-close (unfillable for 120s+); only the LOSER is offered, collapsing to ~0.01 in ~3s. You cannot buy the known winner below fair as a taker — book-structure, not latency.
- **Maker/rebate edge: the ONE +EV signal, replicated.** Maker net = −(taker net) over resolved micro-market fills: **+1.04% of taker stake (18 mkts)** and **+1.08% (41 mkts / 18k fills / $217k)**, pre-rebate; liquidity rebates stack on top. The within-window split is small-sample noise (don't trust it); the ~+1% aggregate is stable. Modest per-unit, needs volume + rebates, faces competition + inventory risk. → **the maker build below is the validated direction; the latency-taker idea is dead.**

## 1. What the edge actually is (evidence-based)

Three overlapping execution edges, not a price rule:
1. **Resolution-latency arbitrage** — be faster than the market at knowing an outcome is decided. For micro-crypto Up/Down: read the Chainlink reference stream at the candle boundary and fill the determined side before the book reprices. For sports/macro: a low-latency results feed + fast fill.
2. **Maker-rebate / liquidity-reward capture** — Polymarket pays makers (see `docs.polymarket.com/market-makers/maker-rebates`, `.../liquidity-rewards`). A chunk of scraper PnL is quoting both sides on incentivized markets and collecting rebates, not directional edge.
3. **Selection + cadence** — knowing *which* markets and *when* (the 5-min crypto cadence, near-resolution windows) and only engaging where the latency/rebate math is positive.

The first is a latency arms race; the second is a quoting/inventory operation. Both need real-time infra.

## 2. Why the paper-rule approach can't capture it
- The edge lives in the seconds between resolution and reprice — sub-poll-interval. A manual/offline scorer observes the lag too late.
- Confirmed empirically: the only clean post-close test (micro-crypto) showed the *taker* favorite-scrape is −EV. The money is in *being the maker* / *being first*, which a paper rule on public REST data cannot represent.

## 3. Components required (if pursued)

| Component | What it is | Hard parts |
|---|---|---|
| **Real-time source feed** | Chainlink price streams (crypto) and/or a paid sports results feed (SportRadar/Flashscore-class) for niche events | Niche-sport feeds are paid/scraped; Chainlink streams need a node/stream subscription; latency budget in ms |
| **CLOB execution path** | Signed EIP-712 orders to Polymarket CLOB (`py-clob-client`), funded proxy wallet, allowances | Order signing, nonce/rate handling, fill confirmation, partial fills |
| **Latency loop** | feed→decision→signed order in the lag window | Competing against incumbents already doing this; ms matter |
| **Risk/capital** | bankroll, per-market caps, kill-switch | Real money; reuse `sniper/risk/` venue caps |
| **Sizing model** | reuse `public_truth_stale.pricing` depth-weighted + the scorer gates | Already built |

## 4. Hard blockers (must be solved first, honestly)
- **Geo-restriction.** Polymarket geo-blocks the current Amsterdam VPS (per MEMORY `copy-trading-investigation-state`). Live order placement needs a permitted-region host + compliant access. **This gates everything live.**
- **Latency competition.** Incumbent scrapers are already in the lag window; entering a latency race with retail infra likely loses. Edge must be measured net of the field, not in isolation.
- **Real-money gate.** `CLAUDE.md`: operator-gated, check MEMORY stats first; start $200–500, $5–50/trade.

## 5. Phased plan (measurement-first, no money until proven)
- **Phase 0 — measure the lag with a REAL-TIME feed (paper).** Subscribe to the Chainlink BTC stream; at each 5-min candle boundary, timestamp the determination and poll the CLOB book at sub-second cadence to measure the *actual* reprice latency + whether a maker/first-taker fill at a profitable price is *achievable* (not just visible). This is the go/no-go: if the determined side isn't fillable below fair before reprice, there is no edge. Aligns to real (exchange) time, not the +7h-skewed env clock.
- **Phase 1 — paper-execute** against the live book (simulate fills at observed asks, account rebates) over weeks. Net-of-field expectancy.
- **Phase 2 — live, operator-gated**, permitted-region host, tiny bankroll, kill-switch. Only if Phase 1 clears.

## 6. Decision gates
- After Phase 0: is the reprice latency long enough + the fill achievable? If no → stop (the edge is faster than us).
- Before Phase 2: geo-compliant host + operator sign-off + MEMORY stats check.

## 7. Reuse
`public_truth_stale.pricing` (depth-weighted sizing), the scorer gates, the ESPN feed prototype (majors), `sniper/risk/` venue caps, `sniper/execution/` order helpers. The new build is the **real-time feed + latency loop + CLOB signing** — not sizing or risk.

## Sources
- Polymarket maker-rebates / liquidity-rewards / CLOB docs.
- Investigation finding: `execution-not-rule-edge` (no paper-rule source-lag on free data).
