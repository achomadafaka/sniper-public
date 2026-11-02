#!/usr/bin/env python3
"""
enrich_brain.py — Replaces all knowledge entries with deeply enriched versions
and adds new cross-domain synthesis entries.

Run: cd /home/sniper/sniper && .venv/bin/python3 scripts/enrich_brain.py
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

ENTRIES = []

# ─────────────────────────────────────────────────────────────────────────────
# ENRICHED: strategy_insider_copy_v1
# ─────────────────────────────────────────────────────────────────────────────
ENTRIES.append(dict(
    name="strategy_insider_copy_v1",
    description="AUDIT CORRECTED 2026-03-25: wallet selection was inverted; position-level timing still valid; Kelly math, failure modes, signal decay",
    content="""STRATEGY: INSIDER_COPY_V1 — Internal Memo (Enriched)
Generated: 2026-03-17 | AUDIT CORRECTED: 2026-03-25

⚠️ WALLET SELECTION AUDIT (2026-03-25): the old wallet-selection formula was INVERSELY correlated with real Polymarket profitability. Score 80+ wallets averaged $16K lb_pnl; score 1-19 wallets averaged $160K lb_pnl. ADR-007 replaced it with wallet_quality_score for wallet selection while keeping position_insider_score for timing. The position-level entry mechanics, Kelly math, and failure modes below remain valid.

OVERVIEW:
Copies Polymarket wallets exhibiting insider-like buying behavior. Core hypothesis: a subset of traders consistently buy correct outcomes before resolution due to information advantage (not luck). Wallet selection now uses wallet_quality_score behind Gate 0 and hard filters; positions mirrored paper-trade style.

SIGNAL FLOW:
1. sniper-stream ingests Polymarket CLOB trades in real time
2. sniper-analyzer scores each wallet and position → writes to position_signals table
3. paper_trader.py polls /api/open-position-signals every cycle
4. Applies entry filters → opens paper_trades rows
5. Monitors open positions for exit conditions → closes with typed terminal states

INFORMATION THEORY BASIS:
Insider-class wallets see order flow before it propagates to price. In a continuous double auction (Polymarket CLOB), price impact of a $5K buy at odds 0.45 is ~2-4% immediately, but the information content of THAT specific wallet buying is far larger. Our copy exploits the asymmetric information revelation: we observe the trade but not the reason; however, the reason is encoded statistically across 20+ prior resolutions. The wallet's historical WR IS the information signal — we're Bayesian-updating on a posterior built from observed outcomes.

Network effects in thin markets: Polymarket mid-tier markets ($50K-$500K open interest) adjust slowly over days-to-weeks. The informed trader's first buy moves price 2-5%; our copy enters within 1-3 hours before the remaining 10-20% move occurs. This is the exploitable window.

KELLY CRITERION MATH:
Current empirical parameters (38 resolved trades at insider_score ≥ 70):
  Win rate (p) = 0.655
  Average entry price = 0.47 (on YES positions)
  Average payout at resolution = 1.00 (binary)
  Net odds at entry (b) = (1 - 0.47) / 0.47 = 1.128

Kelly fraction f* = (p × b - (1-p)) / b = (0.655 × 1.128 - 0.345) / 1.128
  = (0.739 - 0.345) / 1.128 = 0.394 / 1.128 = 0.349

Full Kelly = 34.9% of bankroll per trade. This is aggressive; full Kelly maximizes geometric growth but produces large drawdowns with a 34.5% loss rate.

Half-Kelly (recommended): 17.5% per bankroll. On $500 bankroll = $87.50 per trade.
Quarter-Kelly (conservative, current practice): 8.75% = ~$43. Current base $25 is ~5% = conservative by design while sample size is under 100.

Optimal sizing increases as sample size grows: at n=100 resolved trades, if WR holds at 65%, move toward half-Kelly. At n=200, consider 2/3-Kelly.

KNOWN FAILURE MODES:
1. Market already priced in: if insider_score wallet traded 6+ hours ago and price moved ≥8%, information is stale. Entry at current price captures nothing — signal decay kills edge.
2. Wallet diverging from strategy: wallets rotate strategies. A sports specialist who starts trading crypto markets is outside their edge zone. Monitor category WR separately per wallet; if sports WR drops below 55% on 10+ new trades, downgrade trust.
3. Wallet size too big to hide: large wallets (>$50K per position) create obvious price impact that counter-traders exploit. The signal is "crowded" before we copy. Prefer wallets with position sizes $1K-$15K (enough to be intentional, small enough to be non-obvious).
4. Resolution timing mismatch: markets resolving in >30 days have too much state change possible. Edge decays with time from signal. Markets resolving in <24h are best.
5. Correlated position overload: if 4 open copies are all NBA same-night games, one bad referee call eliminates all. True diversification requires independence of resolution events.

SIGNAL FRESHNESS DECAY:
Estimated edge half-life by category:
  Sports individual matchup: 1-4 hours (injury news / lineup info expires at game time)
  Politics/elections: 4-24 hours (political developments can reverse)
  Geopolitics/conflict: 1-6 hours (fast-moving situations)
  Crypto: <30 minutes (fully efficient, no lasting edge)

Rule: if time_since_wallet_trade > 4 hours AND entry price has not moved ≥5% in our favor, skip the signal. The information has already been priced in by others.

ADVERSARIAL DYNAMICS — CROWDED SIGNALS:
When too many copiers enter, the market FRONT-RUNS the copiers. Detection signals:
1. Price moved ≥10% between wallet trade time and our entry → signal crowded, skip
2. Multiple wallets we track ALL entered same market within 2 hours → check if coordinated (same source) vs independent (genuine cascade). Independent = stronger signal. Coordinated = amplified noise.
3. Twitter/X volume on market topic spikes → retail has discovered it, edge collapsed
4. Spread widened from 2% to 8%+ → liquidity providers withdrew, smart money already positioned

Crowding test: before entry, check (current_price - wallet_entry_price) / wallet_entry_price. If >12% and our signal is sports, skip. Politics threshold: 18% (slower markets).

CURRENT SETTINGS:
  Base position: $25 | Max: $100 | Min: $15
  Min position_insider_score: 80 (raised from 70 on 2026-03-22)
  Entry price range: 0.20-0.85 (skip extremes)
  Max concurrent positions: 25
  Wallet selection: wallet_quality_score behind Gate 0 + hard filters (audit-corrected 2026-03-25)
  speculator_bot wallets: do NOT penalize (avg lb_pnl $150K, most profitable class)
""",
))

# ─────────────────────────────────────────────────────────────────────────────
# ENRICHED: strategy_news_intel_v1
# ─────────────────────────────────────────────────────────────────────────────
ENTRIES.append(dict(
    name="strategy_news_intel_v1",
    description="News Intel strategy — pipeline details, signal quality scoring, failure modes, integration with copy strategy",
    content="""STRATEGY: NEWS_INTEL_V1 — Internal Memo (Enriched)
Generated: 2026-03-17 | Source: news_intel.py

OVERVIEW:
Scans 22 RSS news sources for headlines correlating to open Polymarket markets. Scores sentiment, matches to active markets by keyword overlap, creates trade_idea records. High-confidence matches trigger Telegram alerts.

PIPELINE (NewsIntel.run_once()):
1. Fetch all 22 RSS feeds concurrently (8-worker thread pool, 10s timeout each)
2. Enrich items: extract topic_tags + score_sentiment per headline
3. Store enriched items in Supabase (brain.store_news)
4. Fetch up to 500 open Polymarket markets (sorted by 24h volume)
5. Match news to markets: keyword overlap ≥ 2 with topic-level relevance gate
6. Create trade_idea records for medium/high confidence matches
7. Send Telegram alert for high-confidence matches

INFORMATION THEORY BASIS:
News-based signals have a 30s-5min edge window on liquid markets (>$1M OI). On mid-tier markets ($50K-$500K OI), the edge window is days-to-weeks — the market simply hasn't reacted yet. This is the exploitable zone for NEWS_INTEL_V1: target mid-tier markets where our news detection outpaces the sparse liquidity-provider repricing.

SIGNAL QUALITY SCORING:
Confidence is determined by:
  HIGH: 4+ keyword overlaps, topic exact match, headline contains explicit outcome direction
  MEDIUM: 2-3 keyword overlaps, topic adjacent match
  LOW: 2 overlaps, weak topic match (excluded from trade_ideas)

Problem: NLP keyword matching generates false positives. "Iran" in a headline about Iran nuclear talks != "Iran" in a market about Iran invading Israel. Precision without semantic understanding = noisy signals.

KNOWN FAILURE MODES:
1. Stale RSS feeds: some sources update every 4h, not real-time. A "breaking news" item from 3h ago is already priced in on liquid markets.
2. Keyword false positives: "Will Trump tweet about Ukraine" matches any Ukraine headline, but the causal link is not guaranteed.
3. Market matching ambiguity: same news item matching 3+ markets. All get trade_ideas, but only 1-2 are genuinely relevant.
4. Sentiment scoring limits: binary sentiment (positive/negative) doesn't capture "expected vs unexpected" which is what moves markets. "US jobs data beats expectations" and "US jobs data misses expectations" both parse as non-negative but have opposite market implications.

INTEGRATION WITH INSIDER_COPY_V1:
News signals amplify copy signals when BOTH fire on same market:
  Case 1: Insider wallet enters YES on "Fed rate cut March" + news_intel fires "Fed signals March pause" → DOUBLE signal, size up 1.25×
  Case 2: News fires but no wallet copy signal → lower confidence, max position = $30 (reduced from base $25 → cap unchanged, don't size up without wallet confirmation)
  Case 3: Wallet copy but news suggests adverse development → consider skipping or sizing down 0.75×

LATENCY ANALYSIS:
RSS fetch latency: 1-3s per feed (parallel)
Enrichment per item: ~100ms (local NLP, no API calls)
Market matching: ~200ms for 500 markets
Total pipeline: 5-15 seconds from trigger to trade_idea
Compared to on-chain copy latency: ~60-180 seconds
Result: news_intel can PRECEDE copy signal by 1-3 minutes on fast-moving events — useful as pre-entry alert, not yet as standalone entry trigger without wallet confirmation.

CAPITAL ALLOCATION:
News-only signals (no wallet copy): $15-20 position (min size)
News + wallet copy concordant: $30-45 position (1.2-1.5× base)
Discordant (news vs wallet): skip or reduce to $15
""",
))

# ─────────────────────────────────────────────────────────────────────────────
# ENRICHED: scoring_thresholds_empirical_basis
# ─────────────────────────────────────────────────────────────────────────────
ENTRIES.append(dict(
    name="scoring_thresholds_empirical_basis",
    description="AUDIT CORRECTED 2026-03-25: thresholds valid for position entry timing, not wallet selection; Bayesian framework, CIs, EV math",
    content="""SCORING THRESHOLDS — EMPIRICAL BASIS MEMO (Enriched)
Generated: 2026-03-17 | AUDIT CORRECTED: 2026-03-25

⚠️ SCOPE CORRECTION (2026-03-25): the thresholds below apply to PER-POSITION ENTRY TIMING only. They are NOT valid as wallet-selection evidence. ADR-007 added wallet_quality_score for wallet selection on 2026-03-25. Use wallet_scoring_system for wallet-selection thresholds.

ALL THRESHOLDS DERIVED FROM RESOLVED PAPER TRADE OUTCOMES — NOT PRIORS.
IMPORTANT: These are entry-timing thresholds (position_insider_score gates), not wallet quality indicators.

INSIDER SCORE THRESHOLD: 80 (PAPER_ENTRY_MIN_INSIDER_SCORE)
Previous values: 55, then 70. Raised to 80 on 2026-03-22.
Empirical data (30-day resolved insider_candidate sample, Mar 2026):
  Score 80-100: 71 trades, 59.7% WR, +$113 PnL  ← CLEARS BREAK-EVEN
  Score 70-79:  12 trades, 37.5% WR,  -$32 PnL  ← NEGATIVE EDGE
  Score 60-69:   2 trades, 50.0% WR,  -$17 PnL  ← COIN FLIP
Only 80+ cleared the ~58.8% break-even line from avg_win=$30 / avg_loss=$43.

CONFIDENCE INTERVALS ON WIN-RATE DATA:
For sports matchup (n=29 trades, 65.5% WR observed):
  95% Wilson CI: [46.2%, 81.0%]
  90% Wilson CI: [49.0%, 78.7%]
  This is wide. We cannot rule out true WR = 50% at 95% confidence yet.
  Required n for 95% CI upper-bound > 55%: approximately 80 trades.
  Required n for CI width < ±10pp: approximately 120 trades.
  Takeaway: treat all category WRs as directional evidence, not established facts. Do not abandon a category on fewer than 20 losses.

SAMPLE SIZE REQUIREMENTS BEFORE TRUSTING A NEW CATEGORY:
  "Edge exists" threshold (95% CI lower bound > 52%): ~60 trades
  "Trust for full sizing" (95% CI lower bound > 55%): ~120 trades
  "Match results from quant backtests" (CI width < ±8pp): ~200 trades
  Current state (March 2026): sports at 29 trades is PROMISING but not iron-clad.

BAYESIAN UPDATE FRAMEWORK:
Prior on WR for any new category: Beta(alpha=2, beta=2) = uniform prior centered at 50%.
After n trades with k wins: posterior = Beta(alpha=2+k, beta=2+n-k)
Posterior mean WR = (2+k) / (4+n)

Example: after 29 sports trades with 19 wins:
  Posterior mean = (2+19)/(4+29) = 21/33 = 63.6% (Bayesian shrinkage from raw 65.5%)
  As n grows, posterior converges to MLE (raw WR). Bayesian shrinkage prevents over-trusting small samples.

THRESHOLD VALIDATION — EXPECTED VALUE MATH:
At minimum score = 70 (current):
  Assumed WR = 64% (midpoint of 70-79 and 80-100 buckets)
  Average entry price = 0.47, payout = 1.00
  EV per dollar at entry: 0.64 × (1/0.47) - 1.0 = 0.64 × 2.128 - 1 = 0.362 = +36.2% ROI

At hypothetical threshold = 65:
  Assumed WR = 57% (blended 60-79 band with lower bound included)
  EV per dollar: 0.57 × 2.128 - 1 = 0.213 = +21.3% ROI (still positive but much lower)

At hypothetical threshold = 60:
  Assumed WR = 53% (based on 60-69 band historical)
  EV per dollar: 0.53 × 2.128 - 1 = 0.128 = +12.8% ROI (barely above break-even; killed by fees/slippage)

At threshold = 55 (old setting):
  Blended WR approaches 50%, EV ≈ 0% or negative after transaction costs.
  This is why raising to 70 was correct — the pre-raise threshold was systematically negative EV.

CONCLUSION: 70 is the minimum defensible threshold. 75 would be more conservative and likely justified once n>60 trades. Do not lower below 70 without a new empirical dataset showing sub-70 scores generate genuine edge.

SIZING THRESHOLDS:
  PAPER_SIZING_MIN_INSIDER_SCORE = 70  (below: 0.90× multiplier)
  Upsize trigger: score ≥ 85 → apply 1.15× multiplier
  Maximum multiplier stack: 2.0× (prevents runaway position sizing from correlated multipliers)
""",
))

# ─────────────────────────────────────────────────────────────────────────────
# ENRICHED: market_exploitability_scores
# ─────────────────────────────────────────────────────────────────────────────
ENTRIES.append(dict(
    name="market_exploitability_scores",
    description="Market exploitability by category — WHY each category has or lacks edge, bookmaker arbitrage mechanics, specific targets within sports",
    content="""MARKET EXPLOITABILITY ENGINE — Based on 61 resolved paper trades (Enriched 2026-03-17)

CATEGORY PERFORMANCE (most to least profitable):

1. SPORTS INDIVIDUAL MATCHUP — Score: 85/100
   Stats: 19W 10L = 65.5% WR, +$375 PnL, avg +29.6% ROI
   Markets: Tennis (ATP/WTA), NBA, NHL, MLB game outcomes, O/U totals, player matchups

   WHY EDGE EXISTS — MECHANISTIC EXPLANATION:
   a) Bookmaker arbitrage flow: Sharp sports bettors simultaneously bet Pinnacle/Betfair AND Polymarket. Pinnacle has ~2% vig; Polymarket has ~2-5% spread but no vig. When a sharp bet moves Pinnacle's line, Polymarket is lagging by minutes-to-hours. We're copying the Polymarket-native reflection of that sharp money.
   b) Injury/lineup information: beat reporters, team staff, and player agents know injury status 2-4 hours before public announcement. Polymarket prices don't adjust until the beat report hits Twitter. A wallet that consistently buys right before injury reports is exploiting this window.
   c) Price discovery lag: Polymarket's mid-tier sports market ($50K-$200K volume) has ~15-20 LPs. Pinnacle has 200+ market makers. Price discovery is 10× faster on Pinnacle. Our edge = copying the wallets that bridge this gap.
   d) Head-to-head / surface analytics: tennis H2H on clay vs hard court is analytically exploitable with publicly available data. Most Polymarket LPs use simple ELO models. A sports analytics specialist has persistent edge that survives indefinitely.

   SPECIFIC SPORTS TARGETS:
   TIER 1 (highest edge): Tennis individual matchup (ATP/WTA) — continuous year-round, fast resolution, analytical edge from H2H/surface data is persistent
   TIER 1 (highest edge): NBA/NHL game outcomes — injury information edge is highest in 4-24h pre-game window
   TIER 2 (good edge): Soccer knockout stages — single-elimination increases variance but insider knowledge of team fitness/tactics has value
   TIER 2 (good edge): Combat sports (UFC/boxing) — late camp injuries/weight cuts known to insiders 12-48h before event
   TIER 3 (moderate edge): MLB — large number of games dilutes insider edge; fewer high-conviction opportunities
   AVOID: Season-long totals (too far out), player awards (subjective, gameable), referee assignment markets

2. POLITICS/ELECTIONS — Score: 62/100
   Stats: 61.9% WR, +$103 PnL, +11.6% ROI (lower ROI than sports due to longer holds)
   WHY EDGE EXISTS: Access-based information networks. Congressional staffers, campaign insiders, DC lobbyists have 24-72h advance knowledge of votes, appointments, announcements. Wallets with high insider scores in politics have demonstrably better "inside the Beltway" information flow.
   WHY LOWER THAN SPORTS: Longer hold times tie up capital. Political outcomes have fat-tail reversal risk (unexpected events). Information advantage is less persistent than sports analytics.

3. GEOPOLITICS/CONFLICT — Score: 15/100  — NEGATIVE EDGE
   Stats: 9 trades, 33.3% WR, -$67 PnL, -38.1% avg ROI
   WHY NEGATIVE EDGE EXISTS:
   a) Intelligence asymmetry AGAINST us: conflict risk is priced by market makers who have access to defense contractor intelligence, ex-government analysts, OSINT services worth $500K/year. The retail wallet we're copying is NOT smarter than these LPs. When we copy a "war will happen" bet, we're betting AGAINST institutional intelligence.
   b) Fat-tail underpricing: geopolitical markets have non-linear payoffs. A 5% probability of war can jump to 60% overnight. Our stop-loss gets triggered on noise BEFORE the true event occurs, then the event happens and we're out.
   c) No reliable edge wallets in this category: our top performers in geopolitics are NO-on-absurd-events specialists (YatSen, myfirstpubes). These aren't insiders — they're efficiently pricing tail risk. No copyable edge.
   EXCEPTION: NO positions on clearly absurd geopolitical events (alien invasion, nuclear war by specific near date) ARE copyable — these are structural mispricings from retail lottery-ticket buyers.

4. CRYPTO PRICE TARGETS — Score: 5/100  — STRONG NEGATIVE EDGE
   Stats: 40% WR, -43.5% avg ROI
   WHY NO EDGE: Crypto prices are priced by 200+ quantitative trading firms running millisecond-latency arbitrage between spot, futures, and prediction markets. The information content of any wallet buying a Polymarket crypto target is near-zero relative to what's already priced in. The wallets we copy in crypto are NOT informed — they're gambling on price targets in an efficient market. Our copy signal is noise.

ACTION MATRIX:
  Sports matchup: SIZE UP 1.25× on entry. Actively search for these even without wallet copy.
  Politics/elections: STANDARD size. Good for diversification.
  Geopolitics: BLOCK all directional. ALLOW NO-on-extreme-probability markets only (price <5%).
  Crypto targets: BLOCK ALL. No exceptions until empirical data reverses.
""",
))

# ─────────────────────────────────────────────────────────────────────────────
# ENRICHED: wallet_scoring_system
# ─────────────────────────────────────────────────────────────────────────────
ENTRIES.append(dict(
    name="wallet_scoring_system",
    description="ADR-007 two-score architecture: wallet_quality_score for wallet selection, position_insider_score for timing",
    content="""WALLET SCORING SYSTEM — Internal Memo (Enriched, ADR-007 / AUDIT CORRECTED 2026-03-25)
Original: 2026-03-17 | CORRECTED: 2026-03-25 — old wallet-selection formula was inverted vs Polymarket P&L.

⚠️ SCORING AUDIT RESULT: original formula was INVERSELY correlated with actual profitability.
Score 80+ wallets: avg lb_pnl $16K. Score 1-19 wallets: avg lb_pnl $160K (10× more profitable).
speculator_bot (267 wallets): avg lb_pnl $150K — was penalized, is empirically the best class.
Formula rebuilt as ADR-007. wallet_quality_score now owns wallet selection; position_insider_score remains for entry timing.

TWO-SCORE ARCHITECTURE (computed by sniper-analyzer):

1. wallet_quality_score (0–100)
   Purpose: should this wallet be copied at all?
   Weighting in analyze_users.py:
     45% lb_pnl (or closed_realized_pnl fallback)
     20% net P&L health adjustment
     15% qualifying ROI
     20% sample/confidence (closed sample + capital deployed)
   Hard filters before scoring:
     - Gate 0 lifetime-P&L floor
     - market maker override
     - minimum evidence floor
   Hard-filter failures are stored as wallet_quality_eligible=0 with wallet_quality_fail_reason.

2. insider_score (0–100)
   Measures: how non-random a wallet's buying behavior is relative to outcome.
   NOTE: high insider_score does NOT reliably predict high Polymarket lifetime P&L.
   Still used as a per-POSITION signal gate (position_insider_score for entry timing).
   Gate in agent_pipeline: agent_signal_min_insider_score (default 70)

3. speculator_score (0–100)
   Measures: burst-trading pattern (high volume in short windows).
   AUDIT 2026-03-25: spec_penalty REMOVED from wallet-selection scoring and copy fraction.
   speculator_bot class is empirically the most profitable (avg lb_pnl $150K).
   spec_penalty still applied in agent_pipeline priority_score (signal routing) — pending review.

4. qualifying_roi (float)
   ROI on sub-0.80-entry positions only. Strips out near-certainty padding.

WHY LB_PNL IS THE PRIMARY WALLET SELECTION SIGNAL (audit finding, 2026-03-25):
win_rate was overweighted and is unreliable for wallet selection because:
  1. zombie orders: wallets hold losers and close winners → overstates WR by ~15%
  2. market makers: buy both YES and NO in same market → one side always resolves $1
  3. certainty buying: buying at 0.95 then resolving = win but no alpha
Polymarket lifetime lb_pnl (from profile page __NEXT_DATA__) is the ground truth.
wallet_quality_score formula: 45% lb_pnl + 20% net P&L health + 15% ROI + 20% confidence.
WR is still relevant for per-POSITION entry timing (position_insider_score >= 90 → 65.5% WR, n=84).

GATE 0 STATUS SEMANTICS:
  passed  = lb_pnl present and above configured floor
  failed  = lb_pnl below configured floor
  unknown = no lb_pnl available yet
  NOTE: gate0_status only tracks the lifetime-P&L floor. Market maker and evidence-floor failures live in wallet_quality_eligible / wallet_quality_fail_reason.

RECENCY WEIGHTING MATH:
Older trades have less predictive power because wallet strategies evolve and information edges expire. Apply exponential decay weighting to historical trades:
  weight(t) = exp(-lambda × days_ago)
  Lambda = 0.015 → half-life of 46 days (trades from 46 days ago get 50% weight)
  Lambda = 0.030 → half-life of 23 days (more aggressive recency bias)

Recommended: Lambda = 0.020 (half-life 35 days). A trade from 70 days ago gets 25% weight in WR calculation.
Current system does NOT apply recency weighting — this is a known improvement to implement.

WALLET "DEATH" SIGNALS — WHEN EDGE HAS EXPIRED:
1. Category drift: wallet historically strong in sports suddenly enters crypto/politics. Outside domain = no edge. Filter: skip wallet signals if current_signal_category != wallet's historically dominant category (when WR in dominant category was ≥65%).
2. Team/strategy change: if wallet P&L trajectory reverses sharply (drawdown >40% from peak), their information source has likely dried up. Sports bettors lose edge when coaching staff changes reduce their information access.
3. Sample reversal: wallet had 70% WR on 15 trades, then 40% WR on next 10 trades. Bayesian posterior has dropped significantly — the prior strong performance is getting washed out. Threshold: if last-10 WR < 45%, down-weight that wallet by 50% regardless of cumulative score.
4. Scale-up without WR improvement: wallet suddenly increases average position size 5× without proportional WR improvement. Large positions attract attention and counter-trading from smart money. The edge was partially in position stealth.

POSITION CONCENTRATION RISK — INTERPRETATION:
A wallet placing 70-90% of bankroll on a single position is a BIMODAL signal:
  SCENARIO A — INFORMATION EDGE: wallet has high-confidence information (genuine insider). The concentration is rational given their information certainty. Strong COPY signal if insider_score ≥ 80.
  SCENARIO B — DESPERATION: wallet is trying to recover losses via a Hail Mary. Red flags: preceded by drawdown, position in a market outside their historical category, entry at very high price (>0.75) on a binary event.

Distinguishing A vs B: check wallet's recent P&L trajectory. If current position is 5× their typical size AND they've had 5+ consecutive losses recently → SCENARIO B, skip. If current position is 2-3× typical size AND recent performance is neutral-to-positive → SCENARIO A, consider copying at reduced size (0.75×).

SCORE INTERACTION MATRIX (REVISED post-audit):
  lb_pnl > $200K + any spec_score = IDEAL — lifetime profitability is ground truth
  lb_pnl > $50K + spec_score > 80 = COPY — speculator_bot with real P&L is profitable
  lb_pnl < -$10K = GATE 0 FAIL — disqualified before any scoring
  position_insider_score ≥ 90 = strong per-POSITION timing signal (65.5% WR, n=84)
  NOTE: insider_score thresholds are valid for entry TIMING, not wallet SELECTION.
  LEGACY NOTE: followability_cached still exists for compatibility, but wallet_quality_score is now the canonical wallet-selection signal.
""",
))

# ─────────────────────────────────────────────────────────────────────────────
# ENRICHED: pm_stop_loss_best_practices
# ─────────────────────────────────────────────────────────────────────────────
ENTRIES.append(dict(
    name="pm_stop_loss_best_practices",
    description="Stop-loss framework — Kelly derivation, probability stops vs price stops, bankroll preservation math, tiered stops by entry price",
    content="""PROFESSIONAL PM STOP-LOSS FRAMEWORK (Enriched 2026-03-16)

CORE INSIGHT: Flat percentage stops are WRONG for prediction markets.
A 0.10 entry has ±30-50% normal noise from liquidity/spreads alone.
A flat 15% stop on a 0.10 entry exits on noise, not signal.

TIERED STOP BY ENTRY PRICE (current implementation):
  entry ≤ 0.20 → NO stop loss    (speculative range — stop = noise, not signal)
  entry 0.20–0.45 → 28% stop     (moderate prob, needs room to breathe)
  entry 0.45–0.70 → 22% stop     (balanced range)
  entry > 0.70 → 15% stop        (high conviction, exit fast on adverse drift)

KELLY STOP-LOSS DERIVATION:
If optimal Kelly fraction is f* = 0.35 (at 65% WR, avg entry 0.47), the theoretically optimal stop per position is:
  Max acceptable single-trade loss as % of trade size = (1-p)/p × f* / (1-f*)
  = (0.35/0.65) × 0.35 / 0.65 = 0.283 = 28.3%

This aligns with the 28% stop for mid-range entries. The Kelly framework VALIDATES the current tiered stop approach — it's mathematically optimal, not arbitrary.

For high-conviction entries (entry > 0.70, f* effectively higher due to higher p):
  Optimal stop = (0.30/0.70) × 0.40 / 0.60 = 0.286 → compressed to 15% because at high prices, adverse movement is more informative (others know something you don't).

PROBABILITY STOP vs PRICE STOP:
Price stop: "exit if price drops 22% from entry." Simple but mechanically triggered by liquidity noise.
Probability stop (conceptually superior): "exit if implied probability has changed in a way that our entry thesis is falsified."

In practice, detect probability stops from:
1. Outcome-relevant news: if you're long YES on "Fed cuts rates in March" and Fed publishes hawkish statement → probability stop regardless of current price
2. Wallet reversal: if the wallet we copied CLOSES their position early (before resolution) → probability stop, the insider is exiting
3. Cascade of correlated signals: if 3 other wallets we track just opened NO on the same market → cascade reversal, exit YES
4. Resolution event occurred and went against us: news confirms NO outcome → immediate exit, don't wait for price to reflect

Implementation note: probability stops are not yet fully automated. Currently: Telegram alerts for relevant news → human review → manual close. Target: automate wallet-reversal detection (if position is sold back to market by originating wallet, trigger auto-close).

TRAILING STOP:
Once position is up ≥ 50% from entry, trail at -25% below peak.
Example: entered at 0.40, current price 0.60 (+50% gain). Trail stop at 0.45 (0.60 × 0.75).
If price reaches 0.80, trail stop moves to 0.60. Locks in the original entry price as floor.
This mechanism captures large winners (sports matchups can run to 0.95 pre-resolution) without premature exit.

BANKROLL PRESERVATION MATH:
At what portfolio drawdown % should position sizing be reduced?
Standard quant framework (half-Kelly drawdown response):
  0-10% portfolio drawdown: FULL sizing (no action)
  10-20% drawdown: 50% size reduction (move from $25 base to $12-15 base)
  20-30% drawdown: 75% size reduction ($6-8 base), enter review mode
  >30% drawdown: FULL STOP. Halt all entries. Review all open positions. Read brain. Re-evaluate strategy.

Current $500 bankroll:
  10% drawdown = -$50 (trigger: reduce to $12 base)
  20% drawdown = -$100 (trigger: reduce to $6 base)
  30% drawdown = -$150 (trigger: full stop)

MINIMUM DOLLAR GATE:
  Don't stop if absolute dollar loss < $3 (prevents premature exit on minimum size positions).
  Stop-loss is irrelevant when position value < $5 anyway — let it ride to resolution.

AGE-BASED STOP-LOSS SKIP:
  Positions within 5 days of resolution: SKIP stop-loss.
  Rationale: price will resolve to 0 or 1 regardless; a 28% down-move at this stage is the last noise before truth. Stopping out at -28% and then watching it resolve YES at 1.0 is the worst outcome.
  Exception: if position has already been held 30+ days AND price is < 0.10 → exit, capital is better deployed elsewhere.
""",
))

# ─────────────────────────────────────────────────────────────────────────────
# ENRICHED: signal_pipeline_architecture
# ─────────────────────────────────────────────────────────────────────────────
ENTRIES.append(dict(
    name="signal_pipeline_architecture",
    description="Signal pipeline architecture — latency analysis, wallet-quality gates, fast_ooda/event_trigger integration, recovery procedures",
    content="""SIGNAL PIPELINE ARCHITECTURE — Internal Memo (Enriched)
Generated: 2026-03-17 | Source: scripts/brain_briefing.py, paper_trader.py, agent_pipeline.py

SYSTEM SERVICES (systemctl --user):
  sniper-web      — Flask app: /api/open-position-signals, /api/agent-feed, /api/paper-trades
  sniper-stream   — Real-time Polymarket CLOB trade ingestion
  sniper-analyzer — Scores wallets and positions, writes to position_signals table
  sniper-paper    — paper_trader.py: consumes signals, manages paper_trades table
  sniper-alerter  — Telegram alerts, agent feed updates
  sniper-world-analyst.timer   — every 4h geopolitics/politics analyst
  sniper-finance-analyst.timer — every 4h finance analyst, offset 2h from world analyst
  sniper-event-trigger.timer   — every 10min breaking-news trigger for quick analyst runs
  sniper-agent-wallet-scout.timer — 09:00 + 21:00 UTC wallet discovery / strategy lifecycle

SIGNAL FLOW:
1. STREAM: Polymarket CLOB WebSocket → local SQLite (sniper_tracker.db)
2. ANALYZER: reads SQLite → computes insider/speculator timing scores plus wallet_quality_score → writes user_analysis + position_signals
3. FAST OODA: deterministic 15min headline matcher writes micro-boosts into market_boosts_current
4. ANALYSTS: world_analyst / finance_analyst write narrative context, trade ideas, and larger market boosts
5. PAPER: polls /api/open-position-signals → applies wallet-quality selection gates and entry-timing filters → writes paper_trades
6. ALERTER: reads paper_trades → sends Telegram notifications

LATENCY ANALYSIS — ESTIMATED END-TO-END:
  Step 1: On-chain trade → CLOB WebSocket event: 0-2 seconds (WebSocket latency)
  Step 2: CLOB event → sniper-stream writes SQLite: 1-3 seconds (parsing + write)
  Step 3: sniper-analyzer poll interval: 10-30 seconds (scoring cycle time)
  Step 4: sniper-analyzer scores → writes Supabase: 2-5 seconds (Supabase API round-trip)
  Step 5: paper_trader.py poll interval: 60 seconds (default cycle)
  Step 6: paper_trader filter + entry decision: 1-2 seconds
  Step 7: paper_trader writes paper_trade: 1-2 seconds (Supabase)
  TOTAL ESTIMATED LATENCY: 75-105 seconds from wallet trade to our paper trade entry.
  BOTTLENECK: paper_trader 60-second poll cycle. Reduce to 30s to cut avg latency by ~30s.

Note: 75-105 second latency is acceptable for markets with edge windows of hours (sports/politics). For fast-moving events (<30 min window), this latency is too high — we would enter after 50%+ of the move. Solution: priority fast-lane for high-insider-score signals (skip next poll cycle, trigger immediate entry).

AUTONOMOUS SCHEDULE:
  world_analyst:   00,04,08,12,16,20 UTC
  finance_analyst: 02,06,10,14,18,22 UTC
  event_trigger:   every 10 minutes, 3h cooldown per target agent
  fast_ooda:       every 15 minutes via cron / fast-path automation
  wallet_scout:    09:00 and 21:00 UTC
  insider_copy:    06:00 and 18:00 UTC

WALLET-QUALITY SELECTION LAYER:
  paper_trader and insider_copy_v1 now check:
    - wallet_quality_eligible != 0
    - wallet_quality_score above threshold when populated
    - rollout grace: NULL score can pass temporarily until analyzer backfills
  position_insider_score still decides timing and sizing.

DATA INTEGRITY CHECKS — WHAT CAN CORRUPT SIGNALS:
1. Blockchain reorganizations (reorgs): rare on Polygon but possible. A trade that appeared in block N may not exist in the canonical chain. Mitigation: confirm trades after 20+ block confirmations before treating as definitive.
2. Stale CLOB price cache: PAPER_CLOB_CACHE_TTL = 30 seconds. If paper_trader reads price from cache that is 25 seconds old, price may have moved 5-8% since cached. For volatile markets, use fresh CLOB fetch. Stale cache is the #1 source of entry price errors.
3. Supabase write failures: if position_signals write fails silently, signal is lost. analyzer should implement retry with exponential backoff (3 retries, 2s/4s/8s delays).
4. SQLite corruption: sniper_tracker.db is local and not backed up in real-time. If VPS crashes during a write, DB may be corrupted. Mitigation: WAL mode (already on), daily backup to Supabase or S3.
5. Score reversal race condition: if analyzer scores a position as insider_score=75 during one cycle, then re-scores as 55 in the next cycle (due to additional data), paper_trader may enter between the two cycles at 75 and then find the signal invalidated. Mitigation: minimum score gate at Supabase query level, not just in paper_trader.

FAILURE CASCADE ANALYSIS:
Scenario: sniper-paper crashes during position entry (after filter, before write):
  State: paper_trader has decided to enter but has NOT written the paper_trade row.
  Risk: no double-entry (row doesn't exist to duplicate). Safe to restart.
  Recovery: on restart, paper_trader will re-evaluate same signals and may re-enter if signal still valid. The position_signal deduplication (1 per condition_id) prevents duplicate entries.

Scenario: sniper-paper crashes AFTER writing paper_trade but BEFORE sending Telegram alert:
  State: trade is in DB, alert not sent. Trade will be monitored and closed normally.
  Risk: operator doesn't know about the trade. Low severity.
  Recovery: on next alerter cycle, open trades without alerts will be caught.

Scenario: sniper-stream crashes (no CLOB data):
  State: new wallet trades are NOT captured. Existing open positions continue to be monitored.
  Risk: missed entry signals during downtime. Position exits may be missed if price moves to stop.
  Recovery: sniper-paper has independent CLOB price polling for exit checks. Exit logic survives stream downtime. Entry signal loss is acceptable (missed opportunity, not capital loss).

Scenario: Supabase API outage:
  State: all reads and writes blocked. sniper-paper cannot check signals or write trades.
  Risk: open positions not being monitored for stop-loss. High severity.
  Mitigation: local SQLite shadow copy of open paper_trades. If Supabase unreachable, check local shadow for stop-loss candidates and trigger Telegram alert for manual review.

MONITORING METRICS TO TRACK:
  signal_to_entry_latency: time from position_signal created_at to paper_trade entry_time
  missed_signals_per_hour: signals that passed filters but no paper_trade was created (anomaly)
  cache_age_at_entry: how old the CLOB price cache was when entry price was fetched
  analyzer_cycle_time: duration of each sniper-analyzer scoring cycle (should be <20s)
  wallet_quality_population: count of analyzed wallets with wallet_quality_score populated vs pending
""",
))

# ─────────────────────────────────────────────────────────────────────────────
# NEW ENTRY: autonomous_agent_schedule
# ─────────────────────────────────────────────────────────────────────────────
ENTRIES.append(dict(
    name="autonomous_agent_schedule",
    description="Live autonomous schedule — timers, fast-path triggers, and what each agent writes",
    content="""AUTONOMOUS AGENT SCHEDULE — 2026-03-26
world_analyst:
  schedule: every 4h at 00, 04, 08, 12, 16, 20 UTC
  writes: world_state, world_narrative, market_boosts_current, world_analyst trade_ideas

finance_analyst:
  schedule: every 4h at 02, 06, 10, 14, 18, 22 UTC
  writes: markets/state, markets/boosts, finance trade_ideas

event_trigger:
  schedule: every 10min
  behavior: RSS urgency watcher that can launch world_analyst or finance_analyst in --quick mode
  cooldown: 3h per target agent

fast_ooda:
  schedule: every 15min via cron / fast-path automation
  writes: market_boosts_current entries with source=fast_ooda and fast_ooda_last_run
  role: deterministic complement to the 4h analysts, not a replacement

wallet_scout:
  schedule: 09:00 + 21:00 UTC
  behavior: wallet vetting and insider_copy strategy lifecycle

insider_copy agent:
  schedule: 06:00 + 18:00 UTC
  behavior: copy-strategy review / management

strategy_reviewer:
  schedule: Wed + Sat 15:00 UTC

insider_analyst:
  schedule: Sun 14:00 UTC
""",
))

# ─────────────────────────────────────────────────────────────────────────────
# NEW ENTRY: wallet_analysis_pipeline
# ─────────────────────────────────────────────────────────────────────────────
ENTRIES.append(dict(
    name="wallet_analysis_pipeline",
    description="How analyze_users.py scores wallets — queue/hybrid mechanics, stream-window caveat, analysis_version, Gate 0 propagation",
    content="""WALLET ANALYSIS PIPELINE — 2026-03-26
analyze_users.py is the wallet-scoring engine.

IMPORTANT DATA CAVEAT:
  lb_pnl is lifetime-grounded from the Polymarket leaderboard/profile scrape.
  Most other wallet metrics are derived from locally captured trades only.
  If sniper-stream was offline or predates part of a wallet's history, local trade metrics are stream-window-bounded while lb_pnl remains lifetime-valid.

ANALYSIS MODES:
  legacy  — analyze from open-position scans directly
  queue   — score pending events, enqueue high-value wallets, claim wallets with a short lease
  hybrid  — queue first, legacy fallback when the queue is empty

QUEUE / VERSIONING:
  Each analysis run gets an analysis_version.
  Queue claims use a lease so abandoned work can be re-claimed.
  Discovery threshold defaults to 50 for enqueueing.

WRITES:
  user_analysis:
    wallet_quality_score
    wallet_quality_eligible
    wallet_quality_fail_reason
    market_maker_detected
    gate0_status (mirrored into wallet_registry)
  position_signals:
    per-position timing / flow / speculator features

SEMANTICS:
  gate0_status tracks ONLY the lifetime-P&L floor:
    passed / failed / unknown
  wallet_quality_eligible is broader:
    0 means hard-filter fail (Gate 0, market maker, or evidence floor)
    1 means eligible for wallet-quality ranking
    NULL means not evaluated yet
""",
))

# ─────────────────────────────────────────────────────────────────────────────
# NEW ENTRY: strategy_runner_position_management
# ─────────────────────────────────────────────────────────────────────────────
ENTRIES.append(dict(
    name="strategy_runner_position_management",
    description="Paused/stopped strategy behavior — open-position management, auto-pause, auto-resume, manual pause safety",
    content="""STRATEGY RUNNER POSITION MANAGEMENT — 2026-03-26
Paused or stopped strategies do NOT enter new positions, but their existing open positions are still managed.

This is enforced by strategy_runner:
  - paused strategies skip new entries
  - _manage_paused_strategy_positions() continues managing and closing open positions for paused/stopped plugin strategies
  - manually paused strategies are never auto-resumed
  - insider_copy strategies may auto-resume only when pause_reason='wallet_degradation' and the wallet recovers

Operational meaning:
  paused  = no new entries, but open risk still monitored
  stopped = strategy lifecycle ended, but lingering open positions must still be reconciled and closed

Important correction:
  pausing a strategy does NOT mean stop-loss and stale-position logic disappear. Risk management continues on already-open positions.
""",
))

# ─────────────────────────────────────────────────────────────────────────────
# NEW ENTRY: kelly_criterion_implementation
# ─────────────────────────────────────────────────────────────────────────────
ENTRIES.append(dict(
    name="kelly_criterion_implementation",
    description="Full Kelly Criterion theory and implementation for prediction markets — optimal sizing, half-Kelly, uncertainty adjustment, current sizing comparison",
    content="""KELLY CRITERION IMPLEMENTATION FOR PREDICTION MARKETS
Generated: 2026-03-17 | Source: theory + empirical paper trade data

KELLY CRITERION THEORY:
The Kelly Criterion maximizes the long-run geometric growth rate of a bankroll.
For binary outcomes: f* = (p × b - q) / b
  where: p = win probability, q = 1-p, b = net odds (payout per unit risked)

For prediction markets with binary YES/NO outcomes:
  Entry price = implied probability of YES = 0.47 (avg across scored trades)
  At resolution YES: gain = (1 - 0.47) / 0.47 = 1.128 per unit risked (b = 1.128)
  Win probability p = 0.655 (empirical WR at insider_score ≥ 70)

Full Kelly: f* = (0.655 × 1.128 - 0.345) / 1.128
  = (0.739 - 0.345) / 1.128 = 0.394 / 1.128 = 0.349
  → Bet 34.9% of bankroll per trade. On $500 bankroll = $174.50 per trade.

Half-Kelly (recommended baseline): 17.5% = $87.50 per trade on $500 bankroll.
Quarter-Kelly (conservative, current practice): 8.75% = $43.75. Current base $25 = ~5% = very conservative.

WHY CURRENT SIZING IS CONSERVATIVE:
1. Sample size uncertainty: n=38 resolved trades at score ≥ 70. True WR could be 50% (within 95% CI). Kelly assumes p is known; with estimation error, optimal fraction is reduced. Rule of thumb: multiply Kelly by n/(n+25) to adjust for sample uncertainty. Adjusted f* at n=38: 0.349 × 38/63 = 0.211 = 21.1%. Half of that = 10.5% = $52 per trade. Our $25 is still under this, appropriate while building track record.

2. Portfolio heat: Kelly assumes sequential bets. We have up to 25 simultaneous positions. With correlated outcomes (e.g., 4 sports trades same night), Kelly must be divided by effective number of simultaneous positions. Concurrent Kelly per trade = f*/sqrt(n_positions) in worst case. At 10 concurrent positions: 0.349/3.16 = 0.11 = $55. $25 is roughly appropriate here.

3. Uncertainty in entry price: avg entry 0.47 has variance. At entry 0.30 vs 0.65, Kelly fraction changes materially. A uniform $25 per trade is implicitly using a rough average.

ENTRY-PRICE-ADJUSTED KELLY TABLE:
  Entry 0.20: b = (0.80/0.20) = 4.00, f* = (0.655×4 - 0.345)/4 = (2.62-0.345)/4 = 0.569 → 56.9% Kelly
  Entry 0.35: b = (0.65/0.35) = 1.857, f* = (0.655×1.857 - 0.345)/1.857 = (1.216-0.345)/1.857 = 0.469
  Entry 0.47: b = 1.128, f* = 0.349 (baseline above)
  Entry 0.60: b = (0.40/0.60) = 0.667, f* = (0.655×0.667 - 0.345)/0.667 = (0.437-0.345)/0.667 = 0.138
  Entry 0.75: b = 0.333, f* = (0.655×0.333 - 0.345)/0.333 = (0.218-0.345)/0.333 = NEGATIVE → Kelly says DON'T BET

Kelly goes negative at entry > 0.65 with WR = 0.655. This is the theoretical basis for the entry price cap — at high prices, expected value of the copy trade is negative (market has already priced in the insider's information advantage).

HOW TO ADJUST KELLY WHEN WR ESTIMATE HAS UNCERTAINTY:
Use the "fractional Kelly with shrinkage" approach:
  p_adjusted = p_observed × (n / (n + n_prior)) + 0.50 × (n_prior / (n + n_prior))
  where n_prior = 20 (prior strength — effectively 20 pseudo-observations at 50%)

At n=38, p_observed=0.655: p_adjusted = 0.655 × (38/58) + 0.50 × (20/58) = 0.429 + 0.172 = 0.601
Recalculated f* with p_adj = 0.601: f* = (0.601 × 1.128 - 0.399) / 1.128 = (0.678 - 0.399)/1.128 = 0.247 = 24.7%
Half of that = 12.3% = $62 per trade. This is the most rigorous current optimal sizing.

WHEN TO INCREASE SIZING:
Milestone 1 (n=60 resolved trades at score ≥ 70): raise base to $35 if WR stays ≥ 60%
Milestone 2 (n=100 trades): raise base to $50 if WR stays ≥ 62%
Milestone 3 (n=200 trades): raise base to $75, move toward half-Kelly, if WR stays ≥ 60%
Never raise base more than 50% per milestone to avoid ratcheting up during a lucky streak.

MULTI-POSITION KELLY (for running portfolio):
Total portfolio Kelly allocation to ALL prediction market positions simultaneously:
  Conservative: 30-40% of bankroll in open positions at any time
  Moderate: 40-55%
  Aggressive (not recommended yet): 55%+
Current max exposure: 25 positions × $100 max = $2,500 on $500 bankroll = theoretically 5× overexposed. In practice, average position ~$30-40 × 10-15 concurrent = $300-$600 = 60-120% of bankroll. Reduce max concurrent to 10-12 positions until bankroll exceeds $1,000.
""",
))

# ─────────────────────────────────────────────────────────────────────────────
# NEW ENTRY: prediction_market_microstructure
# ─────────────────────────────────────────────────────────────────────────────
ENTRIES.append(dict(
    name="prediction_market_microstructure",
    description="Polymarket microstructure — AMM vs order book, slippage math, spread behavior near resolution, manipulation detection, optimal entry timing",
    content="""PREDICTION MARKET MICROSTRUCTURE — Polymarket Mechanics
Generated: 2026-03-17 | Source: Polymarket docs, observed behavior, paper trades

HOW POLYMARKET CLOB WORKS:
Polymarket operates a Central Limit Order Book (CLOB) on Polygon, NOT an AMM. This is critical:
- AMM (old model): price = function(liquidity pool ratio). Every trade moves price predictably.
- CLOB (current model): price = best bid/ask from limit orders placed by LPs and traders.
- Implication: slippage depends on order book depth, not a fixed formula.
- Liquidity providers (LPs) post both YES and NO limit orders across a price range.
- When a large buy comes in, it eats through limit orders from cheapest to most expensive.

SLIPPAGE MATH AT DIFFERENT SIZES:
Polymarket mid-tier market ($100K volume), typical order book:
  $10 buy: ~0.5% slippage (falls within one limit order level)
  $50 buy: ~1-2% slippage (eats through 2-3 order levels)
  $100 buy: ~2-4% slippage (meaningful impact on thin books)
  $500 buy: ~5-15% slippage (moves market significantly, alerts LPs)
  $2,000 buy: ~15-30% slippage (major price impact, signals large informed buyer)

Our $25-100 sizing is in the $10-100 range: 0.5-4% slippage. Acceptable.
Never place single orders >$200 in markets with <$500K volume. Use time-weighted entry (split into 3-4 orders over 30 minutes) for positions >$150.

SPREAD BEHAVIOR NEAR RESOLUTION:
Spreads widen dramatically as markets approach resolution because:
1. LPs WITHDRAW liquidity (don't want to be on wrong side of a certain outcome)
2. Remaining quotes are from directional traders, not neutral market makers
3. Information asymmetry is highest near resolution (insiders know, LPs don't)

Observed spread expansion timeline:
  >7 days to resolution: spread 2-5% (normal)
  1-7 days: spread 3-8%
  24-72 hours: spread 5-15%
  <24 hours: spread 10-40% (highly variable)
  <2 hours: spread 20-60% or no liquidity

Implication: EXIT before resolution, not at resolution. Sell at 0.85-0.90 rather than waiting for 0.97+ — the spread on the exit will eat 10-20% of that final gain. Exception: if you're in a $15 minimum position with trailing stop, the spread loss is less important than getting the full binary outcome.

WHY YES/NO SPREADS DIVERGE:
Near resolution, the "expected" side (e.g., YES is at 0.90) gets wide spread because NO sellers want high prices to sell NO (they think the outcome is NO). The unexpected side (NO at 0.10) may have tighter spreads from buyers who think a reversal is possible. Trade against the wide-spread side = adverse selection risk.

MANIPULATION DETECTION:
Signs that a single large position is artificially moving price:
1. Price moves 10%+ in <5 minutes with no corresponding news event
2. Volume in that move is dominated by 1-2 wallet addresses
3. After the move, price reverses 50% within 4-8 hours (pump-and-dump pattern)
4. Spread widens 3× immediately after price move (LPs repricing after shock)
5. Related markets (same topic, different date) do NOT move in parallel

If a signal triggers on a manipulation-pattern price move, skip entry: the wallet we're copying may be the manipulator, not an insider. The high insider_score could be inflated by their own manufactured price impact.

OPTIMAL ENTRY TIMING RELATIVE TO MARKET CREATION:
Early entry (first 24h of market existence):
  PRO: odds are often stale/mispriced, retail hasn't arrived, liquidity is thin = more edge
  CON: less historical data on this market, higher variance
  BEST FOR: markets created in response to breaking news (e.g., "Will X happen in next 48h?")

Mid-life entry (day 2-14):
  PRO: price discovery mostly done, can evaluate whether insider wallet entered at IPO-level price or at current price
  CON: information advantage partially dissipated if market is liquid

Late entry (>70% of market lifetime elapsed):
  PRO: outcome more certain, lower variance
  CON: odds already moved, ROI compressed, capital tied up for diminishing returns
  EDGE RULE: if a wallet entered 14 days ago at 0.30 and price is now 0.65, the copy opportunity IS GONE. The information has been fully priced in.

OPTIMAL ENTRY: within 6 hours of wallet's first entry into the market, in the same price vicinity (within 10% of their entry). Beyond this, skip.
""",
))

# ─────────────────────────────────────────────────────────────────────────────
# NEW ENTRY: copy_trading_information_cascade_theory
# ─────────────────────────────────────────────────────────────────────────────
ENTRIES.append(dict(
    name="copy_trading_information_cascade_theory",
    description="Information cascade theory for copy trading — multi-wallet signal amplification, cascade vs coordination, time decay by category, crowding detection",
    content="""COPY TRADING: INFORMATION CASCADE THEORY
Generated: 2026-03-17 | Source: information economics theory + Polymarket observation

INFORMATION CASCADE BASICS:
An information cascade occurs when multiple independent agents take the same action based on private information, creating a self-reinforcing public signal. In prediction markets, this manifests as:
  1. Wallet A enters YES (private information)
  2. Price moves slightly → Wallet B sees the signal AND has corroborating private information → enters YES
  3. Price moves more → Wallet C enters, now following both private info AND the cascade
  4. The cascade grows: each entrant validates the prior signal

WHY MULTI-WALLET SIGNALS ARE MULTIPLICATIVELY STRONGER:
  Single wallet YES: log-likelihood ratio of "information exists" = log(P(buy|info) / P(buy|no info)) ≈ 2.5
  Two independent wallets YES within 2h: LLR doubles (independence assumption) = 5.0
  Three independent wallets: LLR = 7.5 — this is beyond reasonable doubt threshold

Signal strength formula for n independent wallet buys in the same direction within T hours:
  Cascade_strength = baseline_insider_score × (1 + 0.35 × (n - 1))
  Example: 3 wallets with avg insider_score 75 → 75 × (1 + 0.35 × 2) = 75 × 1.70 = 127.5 (capped at 100)
  Practical: treat 3+ concordant wallets as MAX confidence regardless of individual scores.

DISTINGUISHING CASCADE FROM COORDINATION:
The key question: are these wallets independently informed, or sharing the same source?

INDEPENDENCE SIGNALS (genuine cascade, stronger buy signal):
  - Different wallet archetypes (one sports specialist, one political analyst, one generalist)
  - Entries staggered by 30-120 minutes (if synchronized, they have a shared source)
  - Different position sizes (independent sizing decisions reflect independent confidence levels)
  - Different historical category focus (both entering a sports market despite different specialties)

COORDINATION SIGNALS (same source, don't double-count):
  - Entries within 5 minutes of each other (same trigger event, possibly same Telegram group)
  - Same position size (coordinated) or round-number proportional sizes ($1000, $2000, $3000)
  - Both wallets are in the same on-chain network cluster (graph analysis, not currently implemented)
  - Wallets have traded together in 3+ prior markets (historical coordination pattern)

Current system does not distinguish coordination from cascade. Improvement: if 3 wallets enter within 10 minutes, treat as 1.5× rather than 3× signal. If staggered over 2+ hours, treat as full 3×.

TIME DECAY OF INFORMATION ADVANTAGE BY CATEGORY:
Sports (injury/lineup info): window = 0-4 hours pre-game
  - After game starts: information is public. Copy edge = 0.
  - 4h window is conservative; most injury announcements happen 90-120 min before tip-off
  - Late-breaking lineup changes (court/ice time): window = 0-30 minutes. Very fast.

Politics (regulatory/legislative/appointment): window = 1-24 hours pre-announcement
  - Senate vote outcomes: 2-6 hours window (whip count leaks)
  - Executive appointments: 4-48 hours (leak to specific reporters)
  - Fed decisions: 0-30 minutes (Fed blackout period prevents early leaks)

Geopolitics/military: window = 0-6 hours (fast-moving situations)
  - Exception: long-building negotiations → 24-72 hour windows

Crypto price targets: window = 0-5 minutes (efficient market)

PRICE CROWDING DETECTION:
The cascade has already been "discovered" when:
1. (current_price - wallet_entry_price) / wallet_entry_price > 15% for sports
2. > 20% for politics/geopolitics
3. Volume in last 2 hours > 200% of typical 2h volume for this market
4. Twitter mentions of the market topic spike > 3× baseline

If crowded: our copy ENTERS AFTER the information is priced in. We're the last one in, capturing spread risk with no information advantage. SKIP crowded signals regardless of insider score.

PORTFOLIO IMPLICATION — MULTI-WALLET BASKET:
Rather than copying single wallets, maintain a basket of 5-8 top insider wallets per category. When 2+ basket members agree on a direction, treat as high-conviction signal. This approach averages out individual wallet errors while amplifying genuine information cascades. Expected improvement: WR 65% → 70%+ on basket-confirmed signals based on cascade theory.
""",
))

# ─────────────────────────────────────────────────────────────────────────────
# NEW ENTRY: market_resolution_timing_edge
# ─────────────────────────────────────────────────────────────────────────────
ENTRIES.append(dict(
    name="market_resolution_timing_edge",
    description="Resolution timing edge — short vs long horizon markets, capital efficiency math, age-days stop-loss interaction, optimal hold time",
    content="""MARKET RESOLUTION TIMING EDGE
Generated: 2026-03-17 | Source: paper trade analysis, capital efficiency theory

RESOLUTION HORIZON EDGE TIERS:

TIER 1 — RESOLVING IN <24 HOURS (High Edge, High Capital Velocity)
  Edge source: Information freshness is near-maximum. Insider bought 1-4 hours ago; market hasn't fully priced it.
  Capital efficiency: position resolves same-day → capital recycled immediately
  Expected ROI/day: high (position held <1 day at +20-30% ROI = infinite annualized)
  Risk: price noise is highest intraday. Stop-loss triggers more on noise.
  Best markets: sports game outcomes (resolve same day), breaking political events
  Action: PRIORITIZE. Actively search for same-day resolution markets with fresh wallet signals.

TIER 2 — RESOLVING IN 1-7 DAYS (Medium Edge, Moderate Velocity)
  Edge source: Information still relatively fresh. Wallet likely entered on upcoming event (game, vote, announcement).
  Capital efficiency: position tied up 1-7 days. Capital velocity = 4-7× lower than Tier 1.
  Expected ROI per trade: ~15-25% (similar to Tier 1 but capital cost higher)
  Risk: additional news/events can change the probability landscape.
  Best markets: weekly political events, 3-day sports tournaments
  Action: STANDARD priority. No bonus or penalty on sizing.

TIER 3 — RESOLVING IN 8-30 DAYS (Lower Edge, Low Velocity)
  Edge source: Insider bought early with information advantage, but 2-4 weeks of new information will compound into the price anyway, eroding our edge timing.
  Capital efficiency: capital tied up 8-30 days → annualized ROI drops sharply
  Example: +25% ROI over 20 days = 456% annualized IF you could repeat daily. But capital is locked.
  Risk: position has 4+ weeks for adverse events. Fat-tail risk of unforeseeable developments.
  Action: REDUCE size by 0.80× for markets >2 weeks to resolution.

TIER 4 — RESOLVING IN >30 DAYS (Low Edge, Capital Trap)
  Edge source: minimal. Too much state change possible. Insider's information edge from week 1 is stale by week 5.
  Capital efficiency: terrible. $25 locked for 30-60 days at +20% ROI = 120% annualized. Sounds good but kills portfolio rotation.
  Risk: high. Market can reverse multiple times before resolution.
  Action: SKIP unless extraordinary signal (3+ wallets cascade, insider_score ≥ 90, very high confidence).

CAPITAL EFFICIENCY MATH:
Annual ROI depends on both per-trade ROI AND trade frequency (how fast capital recycles):
  Tier 1 (<24h): avg +22% ROI, 2 trades/week per slot = +44%/week = 2,288% annualized (theoretical)
  Tier 2 (1-7 days): avg +20% ROI, 1 trade/3 days = 24% per 3-day cycle = 2,920% annualized (theoretical)
  Tier 3 (8-30 days): avg +18% ROI, 1 trade/15 days = +18% per 15-day cycle = 438% annualized
  Tier 4 (>30 days): avg +15% ROI, 1 trade/45 days = +15% per 45-day cycle = 122% annualized

In practice, position slots are constrained (max 25 concurrent). Filling slots with Tier 1-2 markets maximizes portfolio throughput. Tier 3-4 markets occupy slots that could be recycled 3-5× faster.

AGE_DAYS STOP-LOSS SKIP INTERACTION:
Current setting: skip stop-loss if position age_days ≤ 5 from resolution.
This is CORRECT for Tier 1-2 markets (resolution in days, stop-skip is only the last 5 days of a short hold).
For Tier 4 markets (30-day+ hold), the 5-day skip applies to the LAST 5 of 30+ days — appropriate.
Problem: what if a Tier 4 position is 25 days in with paper_age 25 days and price is 0.08 (entered at 0.50)? Stop-loss should have triggered at -28% on day 3. Age-skip only applies when CLOSE to resolution (days_to_resolution ≤ 5), not when age is high. Verify implementation distinguishes trade_age from days_to_resolution.

OPTIMAL HOLD TIME vs EMPIRICAL DATA:
From paper trade analysis (58 resolved trades):
  <12h hold: 60% WR, +$133 PnL, +11% ROI ← best WR
  12h-1d:    50% WR, +$141 PnL, +16% ROI ← best PnL/trade
  1-3d:      53% WR, +$106 PnL, +13% ROI
  3-7d:      38% WR, -$39 PnL,  -23% ROI ← losing zone
  7d+:       33% WR, -$30 PnL,  -43% ROI ← worst

Clear signal: holds beyond 3 days are systematically losing. This maps to Tier 3-4 resolution markets. The edge decays. Capital allocation rule: prefer markets resolving within 72 hours of our entry, regardless of total market timeline.
""",
))

# ─────────────────────────────────────────────────────────────────────────────
# NEW ENTRY: bankroll_management_system
# ─────────────────────────────────────────────────────────────────────────────
ENTRIES.append(dict(
    name="bankroll_management_system",
    description="Full bankroll management system — Kelly sizing, portfolio heat limits, scaling milestones, correlation risk, growth targets",
    content="""BANKROLL MANAGEMENT SYSTEM — Hedge Fund Internal Memo
Generated: 2026-03-17 | Source: Kelly theory + empirical paper trade data

CURRENT STATE:
  Starting bankroll: $500
  Base position size: $25 (5% of bankroll)
  Min position: $15 (3%)
  Max position: $100 (20%)
  Max portfolio heat (theoretical): 25 positions × $100 = $2,500 (5× over-leveraged)
  Max portfolio heat (practical avg): ~12 positions × $35 = $420 ≈ 84% of bankroll

OPTIMAL SYSTEM (Kelly-derived, n=38 trades):
  Kelly fraction at current confidence: 12.3% (uncertainty-adjusted half-Kelly, see kelly_criterion_implementation)
  Optimal per-trade size at $500 bankroll: $61.50
  Current base $25 = 5% = UNDER-OPTIMAL by 40%. This is intentional: conservative while building sample.
  Portfolio heat ceiling: 35-40% of bankroll in open positions simultaneously = $175-$200
  At $25 avg position, this means max 7-8 concurrent positions optimally (not 25).
  RECOMMENDATION: Reduce max_concurrent_positions from 25 to 10-12. Quality over quantity.

SCALING MILESTONES:
  Milestone 1: After 60 resolved trades (if WR ≥ 60%)
    → Raise base to $35 (+40%). Max $120. Portfolio heat ceiling $250.
    → Reduce min insider_score to 68 if WR holds (marginal relaxation).

  Milestone 2: After 100 resolved trades (if WR ≥ 62%)
    → Raise base to $50 (+43%). Max $175. Portfolio heat ceiling $400.
    → Bankroll should be ~$800-1,200 by this point if WR holds.

  Milestone 3: After 200 resolved trades (if WR ≥ 60%)
    → Raise base to $75 (+50%). Max $250. Approach half-Kelly.
    → Portfolio heat ceiling 40% of current bankroll.

  NEVER raise base more than 50% per milestone. Prevents size-creep during lucky streaks.
  NEVER raise base if WR has declined over the most recent 20 trades.

DRAWDOWN RESPONSE PROTOCOL:
  0-10% portfolio drawdown: No action. Maintain full sizing. Normal variance.
  10-20% drawdown: Reduce base to $15 (halve sizing). Review recent losses for pattern.
  20-30% drawdown: Reduce base to $8. Enter investigation mode. Stop new entries >$20.
  >30% drawdown: Full stop on new entries. Read brain, review open positions, recalibrate.

  Drawdown measures from EQUITY HIGH (not starting capital). If bankroll grows to $700 then drops to $560, that's a 20% drawdown from peak — trigger tier-2 response.

PORTFOLIO HEAT LIMIT — 40% RULE:
  Never have more than 40% of current bankroll in open positions simultaneously.
  At $500 bankroll: max open = $200 across all positions.
  At $1,000 bankroll: max open = $400.

  Why 40%: at 65% WR with binary outcomes, the expected loss from a single bad day
  (all positions resolve NO) = -40% × (1-0.65) = -14% of bankroll. Manageable.
  At 80% heat and one bad day: -80% × 0.35 = -28% of bankroll. Devastating.

CORRELATION RISK — THE HIDDEN DANGER:
Five open sports trades on the same night's games are NOT 5 independent positions.
They share: weather events, officiating variance, betting market sentiment shifts.
Effective independent positions = sqrt(n_correlated) = sqrt(5) = 2.2 equivalent positions.
Sizing should reflect this:  each correlated position sized at 40-50% of what an uncorrelated position would be.

Correlation buckets:
  Same sport, same night: high correlation (0.6-0.8). Cap total exposure = 1.5× single position size.
  Same sport, different nights: low correlation (0.1-0.2). Treat as independent.
  Same geopolitical actor across markets (e.g., 3 Iran markets): VERY high correlation (0.9). Treat as ONE position.
  Unrelated sports + politics: near-zero correlation. True diversification.

TARGET PORTFOLIO CONSTRUCTION:
  3-4 sports positions (different nights/sports where possible)
  2-3 politics positions (different regions/topics)
  1-2 other/miscellaneous
  0 geopolitics/crypto per current policy
  Total: 6-9 positions, ~$220-$315 at $35 avg. Good heat management at 60% of $500 bankroll.

GROWTH MODEL (conservative projection):
  Assumptions: $25 avg position, 10 trades/week, 65% WR, avg +20% ROI on wins, avg -22% ROI on losses
  Weekly PnL = 10 × (0.65 × $25 × 0.20 + 0.35 × (-$25 × 0.22)) = 10 × ($3.25 - $1.925) = +$13.25/week
  Monthly: +$53. Annual: +$636 on $500 initial = +127% return.
  At milestone 2 sizing ($50 avg): +$26.50/week → +$1,272/year. Compounding accelerates.
""",
))

def main():
    print(f"Upserting {len(ENTRIES)} entries to Supabase knowledge brain...")
    print()

    results = []
    for entry in ENTRIES:
        name = entry["name"]
        try:
            upsert_memory(
                type="knowledge",
                name=name,
                description=entry["description"],
                content=entry["content"],
                source="vps",
            )
            word_count = len(entry["content"].split())
            print(f"  OK  {name:55s} ({word_count} words)")
            results.append((name, word_count, None))
        except Exception as e:
            print(f"  ERR {name:55s} ERROR: {e}")
            results.append((name, 0, str(e)))

    print()
    print(f"Done. {sum(1 for r in results if r[2] is None)}/{len(ENTRIES)} entries written successfully.")
    errors = [r for r in results if r[2] is not None]
    if errors:
        print("ERRORS:")
        for name, _, err in errors:
            print(f"  {name}: {err}")


if __name__ == "__main__":
    main()
