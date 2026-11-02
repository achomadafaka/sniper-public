# HL Analyzer tab + 24/7 copy algo + engine algorithm — master scope

Date: 2026-06-22 · Branch: research/hl-measurement-audit-leadlag · Autonomous build (no questions).

Three asks, scoped:

## 1. Dedicated HL Analyzer tab (mirror the Polymarket Analyzer)
The Polymarket Analyzer tab = header + paste-a-wallet input + two columns (analyzed-wallets list
left, detail right). Mirror it for HL:
- Nav item `data-tab="hl-analyzer" data-venue="hyperliquid"` ("HL Analyzer"); `tab-hl-analyzer` section.
- Left: the screened cohort — ALL deep-screened traders, graded (copyable + rejected with reason),
  verdict badge, archetype, taker%, PF, WR, n, month-ROI. Click → detail.
- Right detail: archetype + skill metrics + per-coin/side edge-location breakdown (reuse
  `hl_wallet_breakdown` logic) + copyability reason + a **"Copy this wallet 24/7"** button.
- Paste-an-address input → on-demand deep-screen of any HL wallet.
- Backend: persist ALL screened verdicts (not just copyable) to `hl_screened_traders`; routes
  `/api/hyperlive/analyzer` (list), `/api/hyperlive/trader/<wallet>` (live assess+breakdown),
  `/api/hyperlive/copy/set` (POST mark target), `/api/hyperlive/copy` (book+PnL).

## 2. Engine algorithm from how pros pick markets + time entries (research → buildable)
Research (4-agent synthesis, this session) verdict against the ~9bps taker wall: micro signals are
taker-dead; the fee-clearing edges are (a) **regime gating** (free, makes everything else
net-positive — build FIRST), (b) **OI-divergence continuation** (price↑+OI↑ = new-money
conviction; hours horizon; 50-150bps; computable from metaAndAssetCtxs), (c) **relative-strength**
leader/laggard (big alt moves, price-only), (d) liquidation cascade (building separately), and the
strongest structural edge: **smart-money follow** (= ask 3). Algorithm v1 = a **regime gate** +
**OI-divergence setup** wired into the engine's SWING track, EV-after-cost gated, honestly
experimental/accruing. Full design spec: `2026-06-22-hl-engine-algorithm-design.md` (separate).

## 3. Simple 24/7 mirror copy-trading algo (picked from the analyzer)
"Follow a determined wallet 24/7, do exactly what he does." This is a full-book MIRROR (distinct
from `run_hl_follow`'s lagged entry-counterfactual): maintain a paper book that tracks the target's
current positions in real time.
- `copy_sim.py` (pure): from target positions + our book + marks → mirror actions + mark-to-market.
- `intel_store`: `hl_copy_targets` (wallet, active, added_ts), `hl_copy_book` (target, coin, side,
  entry_mark, status, exit_mark, pnl_bps).
- `run_hl_copy.py`: each cycle fetch target `clearinghouseState` → open paper mirrors for new
  positions at current mark, close mirrors the target has exited, mark unrealized. Watchdog + cron.
- Picked via the analyzer "Copy this wallet" button → `/api/hyperlive/copy/set`.
- Honest: paper only; enters at the mark when WE detect (lagged), so it measures realistic mirror
  PnL, not the target's own PnL. Not financial advice, no real orders.

## Build order
copy algo (3, self-contained, highest-EV per research) → analyzer-tab backend + persist-all →
analyzer tab UI → regime gate + OI setup (2). TDD all pure logic. Restart services, verify UI.

## Honesty rules (all three)
Paper/read-only; survivorship-flagged; "copyable" is not yet the price-band counterfactual;
fills cap at 2000 (recent slice), funding not yet in PnL; mirror/follow PnL accrues forward.
