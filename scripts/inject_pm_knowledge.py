#!/usr/bin/env python3
"""
Inject professional prediction-market risk management knowledge into Supabase brain.
Run once: python scripts/inject_pm_knowledge.py
These entries are loaded by brain.py on every session start so Claude automatically
has professional PM best-practices context without being asked.
"""
import sys
import os
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import dotenv_values

for _key in ("SUPABASE_URL", "SUPABASE_ANON_KEY"):
    _val = dotenv_values(Path(__file__).resolve().parent.parent / ".env").get(_key)
    if _val and not os.environ.get(_key):
        os.environ[_key] = _val

from bot.supabase_helper import upsert_memory

ENTRIES = [
    {
        "name": "pm_stop_loss_best_practices",
        "description": "Professional prediction-market stop-loss framework: tiered by entry price",
        "content": """PROFESSIONAL PM STOP-LOSS FRAMEWORK (2026-03-16, implemented in paper_trader.py)

CORE INSIGHT: Flat percentage stops are WRONG for prediction markets.
A 0.10 entry has ±30-50% normal noise from liquidity/spreads alone.
A flat 15% stop on a 0.10 entry exits on noise, not signal.

TIERED STOP BY ENTRY PRICE (current implementation):
  entry ≤ 0.20 → NO stop loss    (speculative range — stop = noise, not information)
  entry 0.20–0.45 → 28% stop     (moderate prob, needs room to breathe)
  entry 0.45–0.70 → 22% stop     (balanced range)
  entry > 0.70 → 15% stop        (high conviction, exit fast on adverse drift)

TRAILING STOP:
  Once position is up ≥ 50% from entry, trail at -25% below peak.
  Locks in profits on big winners without premature exit.

MINIMUM DOLLAR GATE:
  Don't stop if absolute loss < $3. Prevents closing on bid/ask spread noise.

AGE GATE: 2 days minimum (first 48h = opening noise, not real signal)
RESOLUTION SKIP: 5 days before resolution (pre-resolution dips are normal and common)

WHY PROS USE THESE RULES:
1. Low-prob markets (< 20 cents) are binary/lottery-like. Price can go 0.05→0.03
   on zero new information — just thin order books. A stop here = noise exit.
2. High-prob markets (> 70 cents) have tight spreads and informed traders.
   If a 0.80 position drops to 0.68, that's -15% AND highly informative.
   Something changed in market consensus. Exit fast.
3. Trailing stops prevent giving back gains on long-duration political trades
   that ran from 0.30 → 0.65 and then collapse back to 0.25 on news.
   Without trailing stop: entry 0.30, peak 0.65, no exit → resolves at 0.02 = -93%.
   With trailing stop: exit at 0.65*(1-0.25) = 0.49 = +63% locked in.

KELLY CRITERION APPLICATION:
  Kelly fraction = (p*b - q) / b  where p=win_rate, b=net_odds, q=1-p
  For PM copy trading with 65% WR and 2:1 avg win/loss: Kelly = (0.65*2 - 0.35)/2 = 47.5%
  Half-Kelly is safer: 23.75% of bankroll per position.
  At $500 paper bankroll → $119 max per position. We use $25 = very conservative ✓
  This gives room to be wrong 70%+ of the time and still survive.

POSITION SIZING BEST PRACTICE:
  Scale size by insider_score, not flat sizing:
  - score 70-79: 0.75× base = $18.75
  - score 80-89: 1.0× base = $25
  - score 90+:   1.5× base = $37.50 (elite signal, size up)
  NEVER exceed 5% of total portfolio per position (concentration risk).""",
    },
    {
        "name": "pm_copy_trading_risk_management",
        "description": "Professional copy-trading risk rules for Polymarket: concentration, correlation, dormancy",
        "content": """PROFESSIONAL COPY-TRADING RISK MANAGEMENT (Polymarket)

CONCENTRATION LIMITS:
  - Max 1 position per market (condition_id) regardless of how many wallets signal it
    → Already implemented via deduplication (2026-03-16)
  - Max 5 positions per market category (sports, politics, crypto) simultaneously
    → Prevents category-specific event risk (e.g. "all sports lost on same day")
  - Max 3 positions copied from any single wallet at once
    → If one whale goes cold / starts gambling, limits your exposure to them

CORRELATION RISK:
  - Related markets (e.g. "Israel invades Lebanon YES" + "Netanyahu stays PM YES")
    are HIGHLY correlated. Treat them as ONE position for sizing purposes.
  - Crypto markets: all highly correlated to BTC. Don't hold 3 crypto positions.
  - Sports: mostly uncorrelated. Can hold multiple sports positions safely.

DORMANCY HANDLING:
  - If a wallet has 0 trades in last 14 days, treat any new signal skeptically
    (hits_14d = 0 is a dormancy flag, not automatically a bad signal)
  - True dormancy: wallet was active, suddenly goes quiet, THEN enters a position
    → This is actually a STRONG signal. Dormant whales who wake up are acting on
    specific information, not habit. Increase insider_score weighting for these.
  - Execution bot dormancy: wallet shows burst pattern then silence → degraded edge,
    don't copy until fresh activity pattern reestablishes over 7+ days

WALLET CLASS PRIORITY (what to copy, in order of edge reliability):
  1. insider_whale (conviction + information): highest trust, full size
  2. smart_money (consistent edge): high trust, full size
  3. early_bird (timing edge): medium trust, 0.8× size
  4. speculator_bot (algorithmic): only if position_score ≥ 60 AND not execution-bot burst
  5. arb_fund: skip (arb edge ≠ directional information)
  6. noise_trader: skip

EDGE DECAY PATTERNS ON POLYMARKET:
  - Copy-trade edge decays over time as wallets become known/tracked by competitors
  - If a wallet's WR drops from 75%→60% over 30 days: decay in progress, reduce size
  - New wallets (< 30 trades): may have hot streak, not edge. Size at 0.5× until 50+ trades
  - Elite wallets (100+ trades, WR > 68%, consistent across categories): full trust

WHAT PROS DO THAT AMATEURS DON'T:
  1. Don't copy the entry — copy the THESIS. If you understand WHY the whale is in,
     you can judge whether new information invalidates the thesis vs. just moves price.
  2. Track edge decay: calculate rolling 30-day WR vs. all-time WR for each wallet.
     If 30d WR < all-time WR by > 10pp, that wallet's edge is degrading.
  3. Size inversely to price: lower entry price = smaller size (not larger).
     Amateurs size up on cheap contracts. Pros size up on conviction, not cheapness.
  4. Exit when the THESIS breaks, not when the PRICE moves. If "Iran ceasefire" whale
     exits a position 3 weeks early on a big news day, that's information — exit with them.
  5. Never average down on copy trades. If a whale doubled down, they have info you don't.
     If they didn't, the original thesis is failing — either way, don't add.""",
    },
    {
        "name": "pm_market_selection_edge",
        "description": "Which Polymarket market types have edge, which to avoid, and why",
        "content": """POLYMARKET MARKET SELECTION: WHERE EDGE EXISTS (2026-03-16)

MARKETS WITH DEMONSTRATED POSITIVE EDGE IN OUR SYSTEM:
  ✅ Politics/Geopolitics: 62% WR, +12% avg ROI across resolved paper trades
     → Informed insiders have genuine information advantages (access, networks)
     → Long duration gives time for information to mature
     → Less noise from random punters; markets are liquid but not arb-dominated
  ✅ Sports (major): 58% WR, +18% avg ROI
     → Sharp sports bettors have deep domain edge (injury info, lineup access)
     → Markets resolve quickly (same-day/next-day) → fast capital recycling
     → Less correlation risk across positions

MARKETS WITH DEMONSTRATED NEGATIVE EDGE IN OUR SYSTEM:
  ❌ Crypto/commodity price targets: 40% WR, -43.5% avg ROI (5 closed trades)
     → "Will BTC hit $100k by March?" markets: pure speculation, no insider advantage
     → Random walk + long duration = high variance, no information edge to copy
     → Wallets copying "crypto" are usually trend-followers, not insiders
  ❌ Very short duration (< 6h): usually arb/execution bots, no copy edge
  ❌ New market categories with < 10 resolved trades: insufficient data for edge assessment

MARKET SELECTION HEURISTICS:
  1. DURATION: Sweet spot is 3-30 days. Too short = arb, too long = high variance
  2. LIQUIDITY: Min $10k volume. Below that, your entry moves the price.
  3. BINARY: YES/NO markets are simplest. Multi-outcome need portfolio adjustment.
  4. QUESTION SPECIFICITY: "Will X happen by Y date?" better than "How many Z?"
     → Specific binary questions have cleaner information signals
  5. NOVELTY: First time a market type appears on Polymarket → no base rate data.
     Copy trades on novel markets should use 0.5× sizing until resolved count ≥ 5.

TIMING YOUR ENTRIES:
  - Best entry: within 6 hours of wallet's original entry (before price impact spreads)
  - Acceptable: same day as wallet entry
  - Risky: > 2 days after wallet entry (thesis may have already played out or failed)
  - Never: when market is within 5 days of resolution (risk/reward collapsed)

POLYMARKET-SPECIFIC MECHANICS THAT AFFECT STOPS:
  - Prices don't update in real-time during resolution — "pending resolution" can
    show 0.98 while awaiting official settlement, or 0.55 during dispute period
  - Sports markets can show stale prices after game conclusion (resolution lag)
  - Political markets often spike/crash on news then revert → don't stop-loss on spikes
  - Liquidity dries up 1-2 days before resolution: wider spreads = more noise""",
    },
    {
        "name": "pm_edge_sources_decay",
        "description": "2026-03-25 update on Polymarket copy-trade edge sources, decay, and surviving strategies",
        "content": """POLYMARKET COPY-TRADE EDGE: SOURCES, DECAY, DETECTION - UPDATED 2026-03-25

NEW CONTEXT:
1. Whale-level copy trading is generally weak; top-whale activity is not automatically copyable
2. Our internal score audit showed the old wallet score selected losers, not winners

REVISED EDGE SOURCES:
1. Information edge:
   Hard to detect directly from wallet behavior alone
2. Analytical edge:
   More copyable; often appears in profitable systematic traders
3. Timing edge:
   Position-level insider_score can still capture this on individual trades
4. Systematic edge:
   speculator_bot wallets can be highly profitable and should not be auto-penalized

EDGE DECAY SIGNALS:
- recent win rate drops materially
- lifetime P&L growth stalls or reverses
- activity shifts into unfamiliar market domains
- formerly active profitable wallets go dormant

SURVIVING STRATEGIES AFTER THE AUDIT:
- Political NO value
- high_winrate_review wallets
- Gate 0 hard exclusions
- position-level timing signals with strong local evidence

POST-AUDIT PRIORITY:
1. wallets with strong lifetime P&L and recent activity
2. high_winrate_review wallets
3. position-level timing/context
""",
    },
    {
        "name": "pm_portfolio_risk_rules",
        "description": "Portfolio-level risk rules for prediction market copy trading",
        "content": """PORTFOLIO-LEVEL RISK MANAGEMENT FOR PM COPY TRADING

PORTFOLIO CONSTRUCTION:
  - Max concurrent positions: 25 (current setting ✓)
  - Max portfolio concentration in one category: 40% (e.g. max 10 politics positions)
  - Max exposure to single-wallet copies: 20% of portfolio (5 positions from same wallet)
  - Target: balanced across politics/sports/other for diversification

DRAWDOWN MANAGEMENT:
  - Daily loss limit: if paper PnL drops > 10% of portfolio in one day → PAUSE entries
    for 24h. Review what went wrong before resuming.
  - Rolling 7-day drawdown: if > 15% → reduce position size by 50% until recovery
  - Max drawdown before strategy review: 25% → full stop, read brain, reassess

WIN-RATE THRESHOLDS BY INSIDER SCORE:
  - score 70-79: expect 55-60% WR. Below 50% on 10+ trades → stop copying that score tier
  - score 80-89: expect 60-65% WR. Below 55% on 10+ trades → review
  - score 90+: expect 65-70% WR. Below 60% on 10+ trades → something is wrong with signals

BANKROLL MANAGEMENT (Kelly-based):
  At 65% WR and 2:1 win/loss odds:
  - Full Kelly: 47.5% of bankroll per trade (NEVER use full Kelly — variance too high)
  - Half Kelly: 23.75% (safer, still aggressive for PM)
  - Quarter Kelly: ~12% (recommended for copy trading with parameter uncertainty)
  - Our actual sizing ($25 of $500 = 5%): VERY conservative, well below Quarter Kelly
  - This is appropriate for early-stage strategy validation; increase as WR confirms

WHEN TO OVERRIDE BRAIN SIGNALS:
  1. Breaking news that changes a market fundamentally → exit manually, don't wait for algo
  2. Wallet you're copying exits before expected → follow immediately (they know something)
  3. Multiple unrelated correlated events happening same day → reduce all exposure
  4. Your own conviction contradicts the signal → skip (gut check has value when informed)

RECORD KEEPING (Sniper DB):
  - Every closed trade: record exit_classification (resolution_win/loss/early_exit/stale)
  - Stop-loss exits: record stop_pct_tier so we can tune thresholds from data
  - Monthly: calculate WR by (entry_tier, market_category, wallet_class) to tune parameters
  - Quarterly: review edge decay for all followed wallets, drop dormant/degraded ones

EXIT DISCIPLINE:
  - NEVER hold through resolution hoping for reversal at < 0.05 price
    ($1 → $25 is 25x, but probability is already pricing near-zero for a reason)
  - If thesis is invalidated by news, exit regardless of current loss
  - If source wallet exits: follow within one monitoring cycle (15 min).
    Don't wait for "better price" — they exited for a reason.""",
    },
]

def main():
    print(f"Injecting {len(ENTRIES)} professional PM knowledge entries into Supabase brain...")
    success = 0
    for entry in ENTRIES:
        try:
            result = upsert_memory(
                type="knowledge",
                name=entry["name"],
                description=entry["description"],
                content=entry["content"],
                source="vps",
            )
            print(f"  ✅ {entry['name']}")
            success += 1
        except Exception as e:
            print(f"  ❌ {entry['name']}: {e}")

    print(f"\nDone: {success}/{len(ENTRIES)} entries injected.")
    if success == len(ENTRIES):
        print("Brain now contains professional PM risk management knowledge.")
        print("Run 'python scripts/brain_briefing.py --quick' to verify.")


if __name__ == "__main__":
    main()
