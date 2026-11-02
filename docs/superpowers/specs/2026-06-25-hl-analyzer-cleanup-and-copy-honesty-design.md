# HL Analyzer Tab Cleanup + Copy/Info Honesty (design)

Date: 2026-06-25
Status: DRAFT (brainstorming output, pre-plan)
Branch: research/hl-measurement-audit-leadlag
Related: `2026-06-22-hl-trader-intelligence-design.md`,
`2026-06-22-hl-analyzer-tab-and-copy-algo-scope.md`, [[project-hyperlive-scanner]]

## 1. Problem

Two things, paired because they touch the same surface:
1. **Cleanup** — the HL Analyzer tab + its routes/JS have concrete defects (verified 2026-06-25).
2. **Honesty** — "copyable" currently means *phase-1 only* (archetype + skill metrics). It has NOT
   survived a price-band counterfactual, so the label overstates. This is the same trap that killed
   every Polymarket "copyable" wallet at phase-2. The copy/info system is the fork-fallback if the
   cascade work nulls out, so it must be honest before we lean on it.

## 2. Verified defects (Explore pass, 2026-06-25)

| # | Location | Defect | Severity |
|---|---|---|---|
| 1 | `static/app.js:5541-5548` `hlAnalyzer._bind()` | re-attaches click listeners to `.analyzer-report-item` on every `load()`; no `_b` guard like buttons → listener multiplication on tab re-entry | MED (UX/perf) |
| 2 | `static/app.js:5588-5594` `toggleCopy()` | awaits `load()` + `detail()` without the abort signal → stale render race on tab switch (walletAnalyzer uses signal-wrapped `Promise.allSettled` at 6019-6021) | LOW |
| 3 | `static/app.js:5505,5555` | stale persisted `/analyzer` list vs fresh `/trader` detail → contradictory verdicts, no staleness indicator | LOW |
| 4 | `api_hyperlive.py:36-60` `_trader_breakdown()` | per-coin/side breakdown duplicated vs Polymarket `_compute_outcome_side_breakdown` | LOW (different shapes; only dedupe if cheap) |

### Test gaps
- `/api/hyperlive/trader/<wallet>` endpoint untested (6/7 endpoints covered).
- No end-to-end follow test (detect entry → open → close) and no end-to-end copy reconciliation test.
- `trader_intel` archetype edge cases untested: zero fills, all-loss closes, funding-included vs
  funding-unflagged, mixed long/short.

## 3. Copyability honesty (the substantive part)

Current gate (`trader_intel.py:103-124`): low_sample (n<20) → market_maker (maker_frac≥0.6) →
degen_leverage (single_fill≥0.5 or top_coin≥0.9) → unprofitable (PF<1.1) → **copyable** (directional,
taker≥0.5, PF≥1.1) → mixed. The reason string already says "phase-1: pre price-band counterfactual."

**Decision:** "copyable" must mean *survived the counterfactual*, not just *looks like a directional
taker*. Add a phase-2 gate and rename phase-1 output so the UI never shows an unqualified "copyable."

### D1 — Two-tier verdict (and the data-feed bug it would otherwise cause)
- Persist a **stable boolean `archetype_pass`** (directional/taker/PF/breadth) on tracked/screened
  rows, **independent of the display verdict string**.
- Display verdict becomes two-tier: `proven` (survived forward-follow test) vs `watch`
  (archetype_pass=1 but unproven) vs the existing reject verdicts. The string "copyable" is retired
  in favor of `proven`.

**CRITICAL — the follow runner must key off `archetype_pass`, not the display verdict.**
`run_hl_follow.py:43` currently does `[t for t in IS.get_tracked(conn) if t.get("verdict") ==
"copyable"]`. If we rename phase-1 `copyable` → `watch`, this filter matches nothing and **forward-
follow collection silently stops — making phase-2 unprovable (circular: can't prove without
following).** Fix: the follow runner selects `archetype_pass == 1` (i.e. the `watch` ∪ `proven`
cohort). We follow the candidates we are *trying to prove*; promotion to `proven` is a read off the
accrued follow PnL, never the follow-selection key.

- Phase-2 promotion: `archetype_pass` trader → `proven` only once forward-follow net is positive over
  a pre-registered n with clustered-t > 2 (constants in D3); else stays `watch`.
**Why follow-PnL instead of a Polymarket-style price-band counterfactual:** on HL we can't easily
reconstruct the unselected-candidate counterfactual per trade, but we *do* have the realistic-
follower net (enter at the lagged mark, mirror exit). That is the honest "what we'd actually get"
metric and it's already accruing. The price-band counterfactual remains a later option, not a blocker.

### D1b — Migration / backward-compat
- Existing `hl_tracked_traders` / `hl_screened_traders` rows with `verdict=="copyable"` are
  **phase-1 archetype-pass, NOT proven**. Migration: backfill `archetype_pass=1` for those rows and
  relabel their display verdict `copyable → watch` on the next screen run.
- `hl_follow_positions` rows are keyed by **wallet**, not verdict, so they keep accruing
  uninterrupted across the rename (no data loss). Document in code + release note: "historical
  `copyable` in the DB ≠ proven."

### D2 — UI tells the truth
- Cohort/list shows the two-tier verdict: `proven (n=…)` vs `watch (archetype-pass, unproven)` vs
  reject reasons. Never an unqualified "copyable" from archetype alone.
- **Copy action gating:** the **"Copy this wallet 24/7"** button only appears for `proven` wallets.
  For `watch`, replace it with a non-actionable **"Track (unproven — follow accruing, n=…)"** label
  (or a disabled button with that tooltip). We never offer a copy action on archetype-only signal.
- Surface the per-wallet follow counterfactual prominently (it is the truth metric, currently
  buried): n, mean net bps, clustered-t, single-asset concentration warning (the current 13-close,
  HYPE-carried sample is exactly the fat-tail trap to flag in-UI).

### D3 — Per-wallet follow stats + pre-registered phase-2 constants
- **API/query:** `follow_summary()` (`intel_store.py:224`) is **global only**. Add
  `follow_stats_by_wallet(conn)` → per-wallet `{n_closed, mean_net_bps, clustered_t,
  top_asset_pnl_frac, top_asset_count_frac, last_close_ts}`. Wire into the analyzer/intel payloads;
  it feeds both the phase-2 gate and the UI.
- **Phase-2 constants (FROZEN before anyone is promoted):**
  - min closed follows per wallet: **≥ 30**
  - clustered stat dimension: cluster by **asset × day** (so one coin's hot run can't mint a t-stat)
  - concentration cap: **≤40% of a wallet's follow PnL** AND **≤40% of its closes** from one asset
  - stale threshold: if a wallet's last follow close is **> 14 days** old, mark `stale` and do not
    display it as live-`proven` (require fresh accrual to re-confirm).

## 4. Components

| Unit | File | Change |
|---|---|---|
| Listener fix | `static/app.js:5541-5548` | event-delegation (one listener on the list container) or `_b` guard |
| Race fix | `static/app.js:5588-5594` | thread the abort signal through `load`/`detail` |
| Staleness | `static/app.js` + `/api/hyperlive/analyzer` | timestamp the screen run; show "screened Nm ago"; refresh list after detail |
| **Follow-selection fix** | `run_hl_follow.py:43` | select `archetype_pass==1` (watch ∪ proven), NOT `verdict=="copyable"` — **must land with/before the verdict rename or follow data stops** |
| `archetype_pass` column | `intel_store.py` schema + screen writer | persist stable boolean; backfill prior `copyable` rows |
| Phase-2 gate | `sniper/hyperlive/trader_intel.py` | `copyability_phase2(per_wallet_follow_stats)` → proven/watch |
| Per-wallet follow stats | `intel_store.py` | `follow_stats_by_wallet(conn)` (D3) |
| Verdict + copy-action wiring | `api_hyperlive.py` analyzer/intel routes, `static/app.js` | two-tier verdict + gate the 24/7 copy button to `proven` |
| Tests | `tests/test_api_hyperlive.py`, `test_hyperlive_trader_intel.py`, new follow/copy integration tests | close the gaps in §2 + follow-selection regression |

## 5. Data flow

screen run → phase-1 `archetype_pass` → forward follow accrues net-of-fee → `copyability_phase2`
→ two-tier verdict → analyzer/intel routes → UI (truthful labels + follow counterfactual).

## 6. Testing

- Pure: `copyability_phase2` (positive forward net + sig + within concentration cap → proven;
  thin/negative/over-concentrated/stale → watch).
- Pure: `follow_stats_by_wallet` (per-wallet aggregation, concentration fractions, clustered-t).
- **Regression: follow selection** — a trader with `archetype_pass=1, verdict="watch"` IS selected
  by the follow runner (guards the rename-breaks-data-feed bug).
- Migration: a pre-rename `verdict="copyable"` row backfills to `archetype_pass=1, verdict="watch"`
  and its existing `hl_follow_positions` rows survive.
- Route: `/api/hyperlive/trader/<wallet>` (mocked HL fills), analyzer payload carries two-tier verdict.
- Integration: follow open→close→grade; copy reconcile open→close.
- Archetype edge cases: zero fills, all-loss, funding-unflagged, mixed long/short.
- JS: listener-leak fix (no duplicate handlers after repeated `load()`); copy button absent for
  `watch`, present for `proven`; if no JS harness exists, document a manual DevTools check.

## 7. Sequencing within this spec

1. Cleanup defects 1–3 + test gaps (mechanical, low-risk) — land first, independent.
2. **`archetype_pass` column + follow-selection fix + migration** — MUST land **together** (and
   before/with any verdict rename), or forward-follow collection breaks. This unblocks phase-2.
3. Phase-2 honesty gate + per-wallet stats + UI truthfulness + copy-action gating — lands after (2).
Defect 4 (dedupe) only if it falls out cheaply.

## 8. Risks / honest framing

- Phase-2 will likely demote most current "copyable" traders (expected — every Polymarket copyable
  died at phase-2). That is the point: stop overclaiming. A near-empty `proven` set is a valid result.
- Follow-PnL is fat-tailed at small n (HYPE-carried). The gate's clustered-t + concentration cap must
  prevent two lucky trades from minting a "proven" trader.
- This spec hardens an analysis/selection surface. It is NOT copy-trade advice or a live trigger.
