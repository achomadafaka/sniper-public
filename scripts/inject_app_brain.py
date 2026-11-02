#!/usr/bin/env python3
"""
inject_app_brain.py — Inject comprehensive app-wide knowledge into Supabase brain.

Pulls actual values from paper_settings.py, agent_pipeline.py, paper_trader.py,
news_intel.py, and filters.py. Each entry is written as a hedge fund internal
memo: concise, data-driven, actionable.

Run once:
    cd /home/sniper/sniper && .venv/bin/python3 scripts/inject_app_brain.py
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
        "name": "strategy_insider_copy_v1",
        "description": "2026-03-25 audit correction for insider_copy_v1; wallet selection uses wallet_quality_score, entry timing uses position_insider_score",
        "content": """STRATEGY: INSIDER_COPY_V1 - AUDIT CORRECTED 2026-03-25
CRITICAL FINDING: the wallet-level score used for selection was inverted versus real Polymarket profitability.

THE INVERSION:
- Score 80+ wallets, our former target, averaged about $16K lb_pnl
- Score 1-19 wallets, formerly excluded, averaged about $160K lb_pnl
- The old system selected wallets that looked insider-like on behavior but were mediocre in real lifetime performance

STATUS OF THE STRATEGY:
- ADR-007 introduced a TWO-SCORE architecture on 2026-03-25
- Gate 0 now runs before wallet ranking
- hard filters now block market makers and under-evidenced wallets
- speculator penalties were removed from the wallet-selection layer

WHAT STILL HOLDS:
- high_winrate_review remains the only locally validated wallet class
- position-level timing signals can still have edge even when wallet ranking was wrong
- Political NO value remains a surviving strategy independent of the wallet-score inversion

REVISED WALLET SELECTION:
1. Gate 0 filters obvious losers using lifetime lb_pnl before ranking
2. wallet_quality_score ranks eligible wallets for copying
3. wallet_quality_eligible=0 is a hard reject
4. position_insider_score remains the per-position timing gate
5. Do not down-rank profitable systematic traders just for being fast

COPY-TRADING BOTTOM LINE:
Whale-level copy trading is not automatically valid. The surviving edge comes from:
- wallets with strong lifetime P&L and recent relevant activity
- high_winrate_review wallets
- position-level timing/context, not raw wallet score bands
- wallet_quality_score for wallet selection and position_insider_score for entry timing are separate tools
""",
    },

    {
        "name": "strategy_news_intel_v1",
        "description": "What news_intel_v1 does: scans RSS feeds for prediction market signals",
        "content": """STRATEGY: NEWS_INTEL_V1 — Internal Memo
Generated: 2026-03-17 | Source: news_intel.py

OVERVIEW:
Scans 22 RSS news sources for headlines that correlate to open Polymarket markets.
Scores sentiment, matches to active markets by keyword overlap, creates trade_idea
records in Supabase. High-confidence matches trigger Telegram alerts.

PIPELINE (NewsIntel.run_once()):
1. Fetch all 22 RSS feeds concurrently (8-worker thread pool, 10s timeout each)
2. Enrich items: extract topic_tags + score_sentiment per headline
3. Store enriched items in Supabase (brain.store_news)
4. Fetch up to 500 open Polymarket markets (sorted by 24h volume)
5. Match news to markets: keyword overlap ≥ 2 with topic-level relevance gate
6. Create trade_idea records for medium/high confidence matches
7. Send Telegram alert for high-confidence matches
8. Expire trade ideas older than 24 hours

RSS SOURCES (22 total as of 2026-03-17):
GEOPOLITICS/NEWS CORE:
  bbc_world       — https://feeds.bbci.co.uk/news/world/rss.xml
  bbc_politics    — https://feeds.bbci.co.uk/news/politics/rss.xml
  bbc_tech        — https://feeds.bbci.co.uk/news/technology/rss.xml
  guardian_world  — https://www.theguardian.com/world/rss
  guardian_us     — https://www.theguardian.com/us-news/rss
  guardian_tech   — https://www.theguardian.com/technology/rss
  npr_world       — https://feeds.npr.org/1004/rss.xml
  foreignpolicy   — https://foreignpolicy.com/feed/
  the_hill        — https://thehill.com/news/feed/
  politico        — https://rss.politico.com/politics-news.xml
  coindesk        — https://www.coindesk.com/arc/outboundfeeds/rss/
  axios           — https://api.axios.com/feed/

CONFLICT / MIDDLE EAST / OSINT (added batch 2):
  aljazeera       — https://www.aljazeera.com/xml/rss/all.xml
  france24        — https://www.france24.com/en/rss
  dw_world        — https://rss.dw.com/rdf/rss-en-world
  middleeasteye   — https://www.middleeasteye.net/rss

CRYPTO / DEFI (added batch 2):
  cointelegraph   — https://cointelegraph.com/rss
  theblock        — https://www.theblock.co/rss.xml
  decrypt         — https://decrypt.co/feed
  blockworks      — https://blockworks.co/feed

ECONOMICS / FED / MARKETS (added batch 2):
  cnbc_economy    — https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258
  marketwatch     — https://feeds.marketwatch.com/marketwatch/topstories/

TOPIC DETECTION (16 topics):
iran, russia, china, northkorea, israel, military, trump, election, taiwan,
greenland, bitcoin, token, fed, oil, ai, spacex, climate

SENTIMENT SCORING:
  YES signals: confirmed, announces, launches, invades, attacks, wins, passes,
    approves, signs, declares, breaks, reaches, achieves, hits record, all-time high,
    surges, soars, escalates, escalation, breakthrough, deal reached, agreement signed,
    ceasefire agreed
  NO signals: denies, rejects, refuses, cancels, postpones, delays, fails, collapses,
    unlikely, no plans, rules out, drops, falls, plunges, crashes, not ready,
    not planning, no deal, no agreement, no ceasefire, dismissed, ruled out,
    backs away, walks back, reverses, retreats from

MATCH CONFIDENCE LEVELS:
  high:   keyword overlap ≥ 4 (Telegram alert sent)
  medium: keyword overlap ≥ 3 (trade idea created, no alert)
  low:    keyword overlap = 2 (discarded, no trade idea)

DIRECTION LOGIC:
  bullish_yes + YES price < 0.70 → buy_yes
  bullish_no  + YES price > 0.20 → buy_no (at 1 - yes_price)
  neutral or price out of range → skip

DEDUP: One trade idea per (condition_id, direction) per run cycle.
STALE: Ideas expire after 24 hours automatically.
""",
    },

    {
        "name": "scoring_thresholds_empirical_basis",
        "description": "2026-03-25 audit-corrected scoring thresholds; distinguish wallet selection from entry timing",
        "content": """SCORING THRESHOLDS - AUDIT CORRECTED 2026-03-25
The old thresholds were empirically derived from paper-trade results, but they measured the wrong thing for wallet selection.

WHAT WAS WRONG:
- High insider-score / old followability bands were useful for our paper entries, but not for choosing which wallets to trust
- The old formula excluded high-P&L traders and favored lower-quality classes
- Behavioral gates also filtered out strong wallets when lifetime P&L evidence already existed

CURRENT THRESHOLDS:
GATE 0:
- Hard first gate using Polymarket lifetime P&L
- Default floor is around -$10K

FOLLOWABILITY SCORE:
- 45% lb_pnl
- 15% ROI
- 15% win_rate after zombie correction
- 15% sample size
- 10% capital fit
- market_maker always 0.0

FOLLOWABILITY INPUT GATE:
- If lb_pnl or closed_realized_pnl is above about $5K, bypass the old behavioral gates
- Hard floor remains at least 5 closed trades

POSITION-ENTRY THRESHOLDS (still valid for timing, not wallet selection):
PAPER_ENTRY_MIN_INSIDER_SCORE = 80 (raised from 70 on 2026-03-22 based on resolved outcomes)
  Score 80-100: 59.7% WR, +$113 PnL (71 trades) ← clears break-even
  Score 70-79: 37.5% WR, -$32 PnL (12 trades) ← negative edge

SIZING THRESHOLDS:
  PAPER_SIZING_MIN_INSIDER_SCORE = 70   (below this: 0.90× sizing)
  PAPER_SIZING_ELITE_INSIDER_SCORE = 90 (above this: max 1.45× sizing)
  PAPER_SIZING_MIN_QROI = 10%           (below this: 0.85× sizing)
  PAPER_SIZING_ELITE_QROI = 50%         (above this: max 1.50× sizing)

POSITION SIZE DEFAULTS:
  PAPER_POSITION_SIZE = $25, min $15, max $100, max concurrent 25

STOP-LOSS TIERS (by entry price):
  entry ≤ 0.20: NO stop loss
  entry 0.20–0.45: 28% stop
  entry 0.45–0.70: 22% stop
  entry > 0.70: 15% stop
  Minimum dollar gate: $3; trailing stop at +50% entry, trail -25% below peak

WHAT STILL HOLDS:
- Paper-trade performance by insider-score band is still useful for position timing
- It must not be reused as wallet-selection proof
""",
    },

    {
        "name": "signal_pipeline_architecture",
        "description": "How signals flow: stream → analyzer → position_signals → paper_trader → trades",
        "content": """SIGNAL PIPELINE ARCHITECTURE — Internal Memo
Generated: 2026-03-17 | Source: scripts/brain_briefing.py, paper_trader.py, agent_pipeline.py

SYSTEM SERVICES (systemctl --user):
  sniper-web      — Flask app (routes/api_core.py, routes/api_misc.py)
                    Serves /api/open-position-signals, /api/agent-feed, /api/paper-trades
  sniper-stream   — Real-time Polymarket CLOB trade ingestion
  sniper-analyzer — Scores wallets and positions, writes to position_signals table
  sniper-paper    — paper_trader.py: consumes signals, manages paper_trades table
  sniper-alerter  — Sends Telegram alerts, agent feed updates

SIGNAL FLOW:
1. STREAM: sniper-stream ingests Polymarket CLOB events in real time.
   Writes raw trade data to the local SQLite DB (sniper_tracker.db).

2. ANALYZER: sniper-analyzer processes raw trades.
   - Computes wallet-level scores: insider_score, speculator_score, win_rate,
     qualifying_roi, wallet_classification, wallet_quality_score
   - Computes position-level scores: position_insider_score, position_speculator_score,
     position_classification (position_insider_candidate, position_watch,
     position_low_signal, position_speculator_ladder, position_emerging_insider)
   - Runs hard filters before wallet scoring: Gate 0, market maker override,
     minimum evidence floor
   - Writes wallet_quality_eligible, wallet_quality_fail_reason, market_maker_detected,
     and gate0_status alongside the legacy analysis row
   - Writes/updates rows in position_signals table
   - Quality gates enforced at upsert time: entry_price < 0.80, current_price < 0.80,
     is_bot=0, qualifying_roi ≥ 10% for low_signal class

3. API LAYER: /api/open-position-signals
   Served by sniper-web (routes/api_core.py). Returns quality-filtered position
   signals. paper_trader.py polls this endpoint every cycle (default: every 5 min).

4. PAPER TRADER: paper_trader.py (sniper-paper)
   - fetch_position_signals(): polls /api/open-position-signals?limit=500
   - enter_position(): applies wallet-quality selection gates first, then entry-timing gates
   - Writes to paper_trades table with: wallet, condition_id, outcome, entry_price,
     size_usdc, stop_loss_price, take_profit_price, signal_type, signal_insider_score
   - monitor_positions(): cycles through open trades, checks for exit conditions
   - Exit conditions: stop-loss, take-profit, trailing stop, resolution, source exit,
     stale (>14 days), adverse drift, manual

5. AGENT FEED: /api/agent-feed (agent_pipeline.py collect_agent_high_signals)
   Aggregates signals from 3 sources:
   a. flagged_users: wallets with insider_score ≥ agent_signal_min_insider_score
   b. emerging_insiders: new wallets showing insider-like behavior
   c. position_signals: open positions meeting focus criteria
   Applies world context boost from Supabase brain (market_boosts_current)
   Computes priority_score, severity, severity_reason, failed_checks per signal.
   Sorted by (priority_score DESC, signal_ts DESC).

6. WORLD ANALYST: scripts/world_analyst.py
   Analyzes current world events vs. open Polymarket markets.
   Writes per-market boosts to Supabase brain as market_boosts_current (JSON).
   Boost range: -30 to +30 applied to effective_insider_score.
   Cached with 15-minute TTL in both paper_trader.py and agent_pipeline.py.

7. FAST OODA: scripts/fast_ooda.py
   Deterministic 15-minute fast path for headline-to-position matching.
   Writes micro-boosts into market_boosts_current with source=fast_ooda,
   cap ±15, expiry 2h. Complements the 4-hour analyst cycle; does not replace it.

8. NEWS INTEL: news_intel.py (standalone cron or sniper service)
   Parallel RSS fetch → sentiment scoring → market matching → trade_ideas in Supabase.
   High-confidence matches trigger Telegram alerts.

9. EVENT TRIGGER: scripts/event_trigger.py
   Runs every 10 minutes, scores breaking RSS headlines, and can fire world_analyst
   or finance_analyst in --quick mode with a 3-hour cooldown per agent.

DATABASE:
  Primary: sniper_tracker.db (SQLite, local VPS)
  Key tables: position_signals, paper_trades, flagged_users, followed_wallets,
              paper_performance, alert_outbox, agent_mutes
  Secondary: Supabase (memories/brain, news_items, trade_ideas, market_boosts_current)

ALERT ROUTING:
  All Telegram alerts -> configured Telegram chat (TELEGRAM_CHAT_ID)
  Entry/exit alerts: paper_trader.py
  High-confidence news ideas: news_intel.py
  Agent feed severity=critical/high: sniper-alerter
""",
    },

    {
        "name": "wallet_analysis_pipeline",
        "description": "How analyze_users.py scores wallets: queue/hybrid modes, stream-window caveat, analysis_version, Gate 0 write-back",
        "content": """WALLET ANALYSIS PIPELINE — 2026-03-26
PRIMARY CODE PATH: analyze_users.py

WHAT IS AUTHORITATIVE:
- lb_pnl comes from the Polymarket leaderboard/profile scrape and is the best lifetime profitability signal
- local trade-derived metrics (win_rate, closed_realized_pnl, open_cash_pnl, concentration, burst trading) only see activity captured while sniper-stream was running
- conclusion: lb_pnl is lifetime-grounded; most other wallet metrics are stream-window-bounded

ANALYSIS MODES:
- legacy: analyze wallets directly from open-position scans
- queue: score pending events, enqueue high-value wallets, claim wallets with a lease
- hybrid: queue first, then fall back to legacy open-position scans when the queue is empty

QUEUE MECHANICS:
- DISCOVERY_SCORE_THRESHOLD defaults to 50 for enqueueing
- claimed wallets use a short lease (QUEUE_CLAIM_LEASE_SECONDS, default 300s)
- release_expired_leases() returns abandoned work to the queue
- every run gets an analysis_version so downstream rows can be tied back to a specific wallet analysis pass

WHAT EACH ANALYSIS RUN WRITES:
- user_analysis: legacy compatibility fields plus wallet_quality_score, wallet_quality_eligible, wallet_quality_fail_reason, market_maker_detected
- position_signals: per-position timing and speculator features
- wallet_registry.gate0_status: passed / failed / unknown for the lifetime-P&L floor only

GATE 0 / ELIGIBILITY SEMANTICS:
- gate0_status=failed: lb_pnl is below the configured lifetime P&L floor
- gate0_status=passed: lb_pnl is present and above the floor
- gate0_status=unknown: wallet has not been evaluated against lb_pnl yet
- wallet_quality_eligible=0 is broader than Gate 0: it also includes market-maker override and evidence-floor failures

IMPORTANT OPERATOR CAVEAT:
If a wallet has excellent lb_pnl but sparse local trade history, wallet_quality_score can still be pending or confidence-limited until more local evidence is captured. That is expected; it does not invalidate lb_pnl itself.
""",
    },

    {
        "name": "wallet_scoring_system",
        "description": "ADR-007 two-score wallet system: wallet_quality_score for selection, position_insider_score for timing",
        "content": """WALLET SCORING SYSTEM - ADR-007 / AUDIT CORRECTED 2026-03-25
CRITICAL: the original wallet-selection formula was inversely correlated with actual Polymarket profitability.

AUDIT FINDINGS (n=2528 wallets, 1325 with verified lb_pnl):
- Score 80+ wallets averaged only about $16K lb_pnl
- Score 1-19 wallets averaged about $160K lb_pnl
- speculator_bot wallets averaged about $150K lb_pnl and were being penalized
- arb_fund wallets averaged about -$17K lb_pnl and were being overrated
- Known failures: Theo4 (~$22M lb_pnl) and SwissMiss (~$952K lb_pnl) were badly mishandled by the old system

ROOT CAUSES:
1. win_rate was inflated by zombie-order behavior: hold losers, close winners
2. spec_penalty penalized the most profitable algorithmic wallets
3. lb_pnl had 0% weight in the original formula

ADR-007 TWO-SCORE ARCHITECTURE:
1. wallet_quality_score (wallet selection)
   - Purpose: should this wallet be copied at all?
   - Inputs from analyze_users.py:
     45% lb_pnl (or closed_realized_pnl fallback when lb_pnl unavailable)
     20% net P&L health adjustment (closed + open cash P&L)
     15% qualifying ROI
     20% sample/confidence (closed sample size + deployed capital)
   - Hard-filter failures do NOT get score 0. They get wallet_quality_eligible=0 and a fail reason.

2. position_insider_score (entry timing)
   - Purpose: is this specific position/timing setup attractive?
   - Used by paper_trader sizing and strategy/plugin entry gates
   - Valid as a per-position timing signal, not as wallet-selection evidence

GATE 0:
- Hard gate before scoring: Polymarket lifetime P&L below the configured floor is disqualified
- gate0_status lives in wallet_registry as passed / failed / unknown
- Audit result: 60 of 478 copy-pipeline wallets failed Gate 0, about -$9.3M combined

OTHER HARD FILTERS BEFORE wallet_quality_score:
- market_maker override: both sides of >50% of markets → ineligible
- evidence floor: minimum closed sample required before ranking
- these failures surface as wallet_quality_eligible=0 with wallet_quality_fail_reason

CLASSIFICATION TRUTH TABLE:
- speculator_bot: profitable class, do not penalize
- high_winrate_review: the only class with locally validated edge so far
- arb_fund: exclude from copy pipeline
- market_maker: synthetic win rate, no alpha

POSITION-LEVEL SCORES (separate from wallet selection):
  position_insider_score: primary entry-timing gate (≥80 required, per-position)
  position_speculator_score: per-position quality gate (≥70 blocks entry)
  These are entry-timing tools. They are NOT wallet-selection evidence.

LEGACY / COMPATIBILITY NOTE:
- followability_cached is still stored for compatibility and older analytics
- it is no longer the canonical wallet-selection signal
- canonical wallet-selection fields are wallet_quality_score, wallet_quality_eligible, and wallet_quality_fail_reason

PRIORITY SCORE FORMULA (agent_pipeline.py agent_signal_priority()):
  For position signals: priority = min(100, insider_score + value_bonus + burst_bonus - spec_penalty)
  For wallet signals: priority = min(100, insider_score + value_bonus - spec_penalty)
  spec_penalty here is for AGENT FEED PRIORITY ORDERING only, not wallet_quality_score.
""",
    },

    {
        "name": "autonomous_agent_schedule",
        "description": "Live autonomous schedule: timers, quick-trigger path, and what each agent writes",
        "content": """AUTONOMOUS AGENT SCHEDULE — 2026-03-26
WORLD ANALYST:
- systemd timer: every 4h at 00, 04, 08, 12, 16, 20 UTC
- service: scripts/world_analyst.py --quiet
- writes: world_state, world_narrative, market_boosts_current, trade_ideas (source_agent=world_analyst)

FINANCE ANALYST:
- systemd timer: every 4h at 02, 06, 10, 14, 18, 22 UTC
- service: finance_analyst.py --quiet
- writes: markets/state, markets/boosts, trade_ideas

EVENT TRIGGER:
- systemd timer: every 10 minutes
- service: scripts/event_trigger.py
- behavior: RSS urgency watcher that can fire world_analyst or finance_analyst in --quick mode
- cooldown: 3h per target agent

FAST OODA:
- schedule: every 15 minutes via cron / fast-path automation
- service path: scripts/fast_ooda.py
- writes: fast_ooda_last_run and source=fast_ooda micro-boosts into market_boosts_current
- cap: ±15 boost, 2h expiry

WALLET SCOUT:
- systemd timer: 09:00 and 21:00 UTC
- service: scripts/run_strategy_agent.py wallet_scout --quiet
- behavior: discovers wallets, vets them, and creates insider_copy_v1 strategies in paused state

INSIDER COPY AGENT:
- systemd timer: 06:00 and 18:00 UTC
- service: scripts/run_strategy_agent.py insider_copy --quiet

STRATEGY REVIEWER:
- systemd timer: Wed + Sat 15:00 UTC
- behavior: plugin performance review and win-prob calibration

INSIDER ANALYST:
- systemd timer: Sun 14:00 UTC
- behavior: score-band calibration and false-positive review

BRAIN HEALTH:
- systemd timer: every 6h
- writes: brain_health_latest
""",
    },

    {
        "name": "strategy_runner_position_management",
        "description": "How strategy_runner handles paused/stopped strategies and open positions",
        "content": """STRATEGY RUNNER POSITION MANAGEMENT — 2026-03-26
CRITICAL OPERATOR RULE:
Paused or stopped strategies do NOT open new positions, but their existing open positions are still actively managed.

WHAT PAUSED MEANS:
- strategy_runner skips new entries for paused strategies
- open positions remain live and can still be exited by plugin logic
- strategy_runner._manage_paused_strategy_positions() closes orphaned open positions for paused or stopped plugin strategies

AUTO-PAUSE / AUTO-RESUME:
- circuit breakers can auto-pause bleeding strategies
- insider_copy_v1 can auto-resume only when pause_reason='wallet_degradation' and the wallet recovers
- manually paused strategies are never auto-resumed by the bot

STOP-LOSS BEHAVIOR:
- pausing a strategy does NOT disable position management
- stop-loss / take-profit / stale / source-exit logic still runs against already-open positions
- the historical orphaned-position bug was fixed by explicitly managing open positions for paused/stopped strategies each cycle

OPERATOR INTERPRETATION:
- status='paused' means no new entries, not 'ignore all existing risk'
- status='stopped' means strategy lifecycle ended, but any lingering open positions must still be reconciled and closed
""",
    },

    {
        "name": "known_issues_and_watchlist",
        "description": "Tracked wallets with special handling, Gate 0 losers, entry gates — speculator_bot is NOT penalized (audit 2026-03-25)",
        "content": """KNOWN ISSUES AND WATCHLIST — Internal Memo
Generated: 2026-03-17 | Source: paper_settings.py, paper_trader.py, filters.py

HARD-BLOCKED WALLET CLASSES:
- arb_fund: Permanently skipped. Exploits price inefficiencies, not information.
  PAPER_SKIP_WALLET_CLASSES = frozenset({"arb_fund"})

SPECULATOR_BOT HANDLING (AUDIT CORRECTED 2026-03-25):
  ⚠️ Pre-audit claim that "spec_score=100 caused ALL losses" is WRONG.
  Wallet-level audit (n=2528): speculator_bot class avg lb_pnl $150K — most profitable class.
  spec_penalty REMOVED from wallet-selection scoring and copy fraction (commit 5dfc41b).
  DO NOT set PAPER_SKIP_EXECUTION_BOTS=true — that blocks the most profitable wallets.
  Current policy (PAPER_SKIP_EXECUTION_BOTS=false): keep as-is.
  Gate 0 (lifetime P&L floor) now handles disqualification before any class-based filtering.

SPECULATOR SCORE CAP: 70 (position_speculator_score, per-POSITION gate only)
  Any position where position_speculator_score ≥ 70 is skipped.
  This is a per-POSITION signal quality gate, not a wallet-class filter.
  Wallet-level speculator_bot classification is NO LONGER penalized in scoring.

LOW_SIGNAL WALLET GATE:
  Wallets classified as low_signal: require ≥10 closed trades AND ≥65% WR.
  Historical losers: ElonMarsianin (0 trades), Businio (0% WR in 2 trades) — both
  were generating entries before this gate was added. Entry gate blocked them.

MARKET CATEGORY LOSERS (hard-blocked from entry):
  Crypto/commodity price-target markets (PAPER_SKIP_CRYPTO_MARKETS=true):
    -43.5% avg ROI, 40% WR across 5 closed trades.
    Blocked keywords: bitcoin, btc, ethereum, eth, solana, sol, xrp, doge,
    hyperliquid, hype, crypto, crude oil, gold (gc), silver, oil hit, oil dip,
    hit $, dip to $, reach $
  Tech/AI product launches: 0% WR, -$12 PnL (2 trades) — 0.60× size multiplier

SIZE MULTIPLIER PENALTIES (filters.py market_exploitability):
  geopolitics/conflict:   0.70× — 33.3% WR, -$67 PnL (9 trades, NEGATIVE EDGE)
    Patterns: iran, israel, russia, ukraine, china taiwan, north korea, hamas,
    hezbollah, gaza, ceasefire, nuclear, netanyahu, putin, zelensky, coup, etc.
  crypto/commodity:       0.50× (also hard-filtered via PAPER_SKIP_CRYPTO_MARKETS)
  tech/ai_launch:         0.60× (Claude N, GPT-N, will X be released, IPO, etc.)

OPEN POSITIONS DEDUP RULE:
  Only one paper trade per (condition_id, outcome) at any time, regardless of wallet.
  Prevents inflating proof bar and concentration risk from multiple wallets
  signaling the same outcome.

RECENT ENTRY RE-OPEN GUARD:
  Closed position blocks re-entry for 24 hours on same (wallet, condition_id, outcome).
  Without this guard, paper trader immediately re-opens after resolution because
  the position signal remains in the API briefly after resolution event.

DUPLICATE TRADE DETECTION:
  Trades opened within 60 seconds of an identical (wallet, condition_id, outcome) trade
  are soft-marked as duplicate_of=canonical_trade_id.
  Excluded from performance stats but preserved for audit.

QUALIFYING_ROI RELIABILITY ISSUE (2026-03-15):
  wallet_qualifying_roi removed from entry gate due to data quality failures.
  Our PnL calculation (open_cashPnl + closed_realizedPnl) was off by 10-860%
  vs. Polymarket leaderboard, with sign flips. The field is still logged and
  used weakly in sizing, but never used as a hard block.
  TODO: re-enable when lb_pnl-based ROI calculation is implemented.

MARKET EXPOSURE CAP: $200 per market (PAPER_MAX_EXPOSURE_PER_MARKET)
  Prevents concentration in a single market across multiple wallet signals.
  Entry size is clamped to remaining headroom: size = min(size, $200 - current_exposure).

STALE DETECTION: 14 days (STALE_DAYS)
  Positions with no activity after 14 days are flagged as stale and closed.
  Prevents the portfolio from filling with unresolvable zombie positions.
""",
    },

    {
        "name": "paper_trader_position_sizing",
        "description": "Full position sizing formula: base_size, multipliers, caps, exploitability multiplier, world boost",
        "content": """PAPER TRADER POSITION SIZING — Internal Memo
Generated: 2026-03-17 | Source: paper_trader.py calculate_position_size(), paper_settings.py

FORMULA:
  size = base_size × upside_mult × roi_mult × insider_mult × win_mult × exp_mult
  size = clamp(size, PAPER_MIN_POSITION_SIZE, PAPER_MAX_POSITION_SIZE)
  size = min(size, PAPER_MAX_EXPOSURE_PER_MARKET - current_market_exposure)

BASE PARAMETERS:
  PAPER_POSITION_SIZE = $25 (base_size, raised from $15 on 2026-03-16)
  PAPER_MIN_POSITION_SIZE = $15
  PAPER_MAX_POSITION_SIZE = $100

MULTIPLIER 1: UPSIDE POTENTIAL (upside_mult)
  Formula: max(0.70, min(1.35, 1.35 - (entry_price × 0.9)))
  At entry_price=0.10: ~1.26×  (cheap entry, more room to run)
  At entry_price=0.30: ~1.08×
  At entry_price=0.50: ~0.90×
  At entry_price=0.70: ~0.72×  (expensive entry, capped upside)
  Range: 0.70× – 1.35×

MULTIPLIER 2: ROI QUALITY (roi_mult)
  Inputs: qualifying_roi, PAPER_SIZING_MIN_QROI=10, PAPER_SIZING_ELITE_QROI=50
  Unknown qualifying_roi: 0.85× (conservative default)
  At qROI=10% (min threshold): ~0.90×
  At qROI=50% (elite threshold): ~1.40×
  Above qROI=50%: +min(0.10, (qroi-50)/100) extra
  Range: 0.85× – 1.50× (capped)

MULTIPLIER 3: INSIDER CONVICTION (insider_mult)
  Inputs: effective_insider_score (base + world_analyst boost),
          PAPER_SIZING_MIN_INSIDER_SCORE=70, PAPER_SIZING_ELITE_INSIDER_SCORE=90
  Formula: insider_progress = (effective_score - 70) / 20.0
           insider_mult = 0.90 + insider_progress × 0.45
  At score=70 (minimum gate): ~0.90×
  At score=80: ~1.125×
  At score=90 (elite): ~1.35×
  Above 90: +min(0.10, (score-90)/100) extra
  Range: 0.85× – 1.45× (capped)

MULTIPLIER 4: WIN RATE (win_mult)
  Intentionally mild — secondary quality signal.
  ≥80% WR: 1.10×
  70-79%:  1.03×
  60-69%:  1.00× (neutral)
  50-59%:  0.95×
  <50%:    0.90×

MULTIPLIER 5: MARKET EXPLOITABILITY (exp_mult)
  From filters.py market_exploitability(title, slug):
  sports_individual_matchup: 1.25× (65.5% WR, +$375 PnL empirically, n=29)
  sports_other:              1.00× (62.5% WR, +$52 PnL, n=8)
  sports_major:              1.00× (57.1% WR, +$50 PnL, n=7)
  us_politics:               1.00× (insufficient data)
  other/unknown:             1.00× (neutral assumption)
  geopolitics/conflict:      0.70× (33.3% WR, -$67 PnL, n=9 — NEGATIVE EDGE)
  tech/ai_launch:            0.60× (0% WR, -$12 PnL, n=2)
  crypto/commodity:          0.50× (also hard-blocked by PAPER_SKIP_CRYPTO_MARKETS)

WORLD CONTEXT BOOST EFFECT ON SIZING:
  world_analyst.py injects per-market boosts to Supabase brain (market_boosts_current).
  Boost range: -30 to +30. Applied at entry time to effective_insider_score.
  Example: base insider_score=65, world_boost=+20 → effective=85 → insider_mult jumps
  from 0.875× to ~1.325× (67.5% sizing increase on a single boosted market).
  Cached 15 min (TTL). Boost is intentionally ephemeral — stale boosts don't persist.

EXAMPLE CALCULATION (typical good signal):
  base_size = $25
  entry_price=0.45 → upside_mult=0.945
  qualifying_roi=25% → roi_mult=1.075
  insider_score=78 (no world boost) → insider_mult=1.08
  win_rate=72% → win_mult=1.03
  Tennis matchup → exp_mult=1.25
  size = 25 × 0.945 × 1.075 × 1.08 × 1.03 × 1.25 = $34.61 → clamp → $34.61

EXAMPLE CALCULATION (elite signal):
  base_size = $25
  entry_price=0.35 → upside_mult=1.035
  qualifying_roi=60% → roi_mult=1.50 (capped)
  insider_score=92, world_boost=+10 → effective=102, insider_mult=1.45 (capped)
  win_rate=85% → win_mult=1.10
  Sports matchup → exp_mult=1.25
  size = 25 × 1.035 × 1.50 × 1.45 × 1.10 × 1.25 = $82.15 → within $100 cap

STOP LOSS ASSIGNMENT (paper_trader enter_position):
  stop_loss_price = RESOLUTION_LOSS_THRESHOLD = 0.03 (stored at entry)
  take_profit_price = RESOLUTION_WIN_THRESHOLD = 0.97 (stored at entry)
  NOTE: actual stop-loss monitoring in monitor_positions() uses tiered percentages
  (28%/22%/15% wide/mid/tight) against entry_price, not these stored threshold values.
  The stored 0.03/0.97 are resolution detection thresholds, not stop trigger prices.
""",
    },

    {
        "name": "agent_pipeline_priority_scoring",
        "description": "How priority_score is calculated for signals before routing to paper_trader",
        "content": """AGENT PIPELINE PRIORITY SCORING — Internal Memo
Generated: 2026-03-17 | Source: agent_pipeline.py

OVERVIEW:
Every signal in the agent feed (agent_pipeline.collect_agent_high_signals) receives
a priority_score (0–100) that determines its position in the sorted feed. Paper trader
and UI both use this ordering. World context boosts are applied after base calculation.

SIGNAL SOURCES (three lanes merged):
1. flagged_users (wallet_insider_candidate signals)
   Gate: insider_score ≥ agent_signal_min_insider_score
   Dedup: one signal per wallet
   Skip if: speculator-dominated (spec_score ≥ speculator_score_high AND spec > insider)

2. emerging_insiders (wallet_emerging_insider signals)
   Gate: insider_score ≥ agent_signal_min_insider_score
   Skip if: speculator-dominated (spec_score ≥ speculator_score_high)

3. position_signals (open positions, position_* classification signals)
   Gates:
   - open_signal_is_focused(): position must meet minimum size/score/flow thresholds
   - open_signal_is_active_non_holding(): must have recent activity (not just holding)
   - position_classification != position_low_signal (hard skip)
   - position_speculator_ladder: only if include_speculators=True
   - For non-speculator positions: insider_score ≥ agent_signal_min_position_insider_score
   - position_value ≥ agent_signal_min_position_value OR insider_score ≥ 75

PRIORITY SCORE FORMULA (agent_signal_priority()):

  For position_* signal types (position_insider_candidate, etc.):
    burst_bonus = min(12.0, max(trades_1h, buys_1h) × 1.5)
      [rewards active buying: 8 trades in 1h = +12 bonus]
    value_bonus = min(20.0, position_value / 5000.0)
      [rewards large positions: $100K position = +20 bonus]
    spec_penalty = min(16.0, speculator_score × 0.2)
      [penalizes speculator-like: spec=80 → -16 penalty]
      [NOT applied to position_speculator_ladder — spec IS the signal there]

    For position_speculator_ladder:
      priority = min(100, speculator_score + value_bonus + burst_bonus)
    For all other position types:
      priority = min(100, insider_score + value_bonus + burst_bonus - spec_penalty)

  For wallet signals (wallet_insider_candidate, wallet_emerging_insider):
    value_bonus = min(20.0, max_position_value / 5000.0)
    spec_penalty = min(16.0, speculator_score × 0.2)
    priority = min(100, insider_score + value_bonus - spec_penalty)

WORLD CONTEXT BOOST (applied after base priority):
  _boost_entry = market_boosts_current[condition_id]  (from Supabase brain, 15-min TTL)
  _boost_val = _boost_entry.get("boost", 0)  (range: -30 to +30)
  priority_score = clamp(base_priority + _boost_val, 0, 100)

  Signals with world_boost ≠ 0 get extra fields:
    item["world_boost"] = boost_val
    item["world_boost_reason"] = reason string
    item["world_boost_direction"] = "yes"/"no"

SEVERITY MAPPING (agent_signal_severity()):
  wallet_insider_candidate / position_insider_candidate:
    critical: insider_score ≥ 85 OR priority ≥ 85
    high:     insider_score ≥ 75 OR priority ≥ 75
    medium:   otherwise
  position_speculator_ladder:
    high:   speculator_score ≥ 85
    medium: speculator_score ≥ 70
    low:    otherwise
  all other signals:
    high:   priority ≥ 80
    medium: priority ≥ 65
    low:    priority < 65

FINAL SORT ORDER: (priority_score DESC, signal_ts DESC)
  Higher priority + more recent → appears first in agent feed and paper trade queue.

ELIGIBILITY BLOCKERS (reason field parsing):
  agent_failed_reason_checks() parses comma-delimited reason string from analyzer.
  Format: "key=yes,key=no,key=yes"
  Known blocker keys: insider_score_gte_threshold, emerging_score_gte_threshold,
    repeat_hits_ok, positions_ok, pnl_ok, top_open_value_ok, total_open_value_ok,
    max_position_ok, serious_trades_ok, serious_notional_ok, position_signal_strength_ok,
    position_speculator_guard_ok, emerging_top_open_value_ok, emerging_total_open_value_ok
  Any key=no in the reason string means that eligibility check failed.
  These appear as failed_checks[] in the serialized agent feed items.

MUTE SYSTEM:
  agent_mutes table: suppress specific wallets, dedupe_keys, or positions.
  Mutes have an expiry (dedupe_seconds configurable).
  Scopes: wallet (address), dedupe_key (string), position (condition_id:outcome).
  All muted signals are silently filtered before priority calculation and sorting.
""",
    },
    {
        "name": "patterns/insider-copy-scoring-edge-validated",
        "description": "Correct interpretation of score-band paper-trade edge after the 2026-03-25 audit",
        "content": """PATTERN: insider-copy-scoring-edge-validated
CORRECTED 2026-03-25 - read fully before using this memory.

ORIGINAL OBSERVATION THAT REMAINS TRUE:
- Score-gated paper trades at 90+ showed about 65.5% WR with meaningful sample
- Those numbers are valid for our own paper-trade entries

CRITICAL CORRECTION:
- Those score bands describe our entry-selection behavior, not wallet quality
- Wallet-level audit showed high-score wallets had worse lifetime P&L than low-score wallets
- So the old interpretation was over-broad

WHAT THIS MEANS:
1. Score bands remain valid as per-position timing signals
2. They are not valid as wallet-selection evidence
3. Wallet selection must use wallet_quality_score after Gate 0 and hard filters
4. Position-level insider_score and wallet_quality_score are different tools
""",
    },

    {
        "name": "signals/scoring_accuracy_by_band",
        "description": "Corrected interpretation of insider-score band performance for paper-trade entries",
        "content": """SCORING ACCURACY BY BAND - CORRECTED 2026-03-25
IMPORTANT: this memory measures our paper-trade win rates by entry score band. It does not measure wallet lifetime profitability.

PAPER-TRADE DATA:
- 90-100 band: real local edge, about 65.5% WR with meaningful sample
- 80-89 band: much weaker
- 70-79 band: marginal
- below that: weak or negative

CORRECT INTERPRETATION:
- Use these bands for per-position entry timing decisions
- Do not use them to justify wallet-selection thresholds
- High-score positions can be useful even if the wallets producing them are not the highest-P&L traders overall

RULE:
- Position signal quality and wallet quality are separate decisions
- Conflating them caused the scoring inversion
""",
    },
]


def main():
    print(f"Injecting {len(ENTRIES)} knowledge entries into Supabase brain...")
    print()
    success = 0
    failed = 0
    for entry in ENTRIES:
        name = entry["name"]
        try:
            result = upsert_memory(
                type="knowledge",
                name=name,
                description=entry["description"],
                content=entry["content"],
                source="vps",
            )
            action = "updated" if result.get("updated") else "created"
            print(f"  [OK] {action}: {name}")
            success += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")
            failed += 1

    print()
    print(f"Done: {success} injected, {failed} failed.")
    if failed:
        import sys
        sys.exit(1)


if __name__ == "__main__":
    main()
