import os


PAPER_POSITION_SIZE_USDC = float(os.getenv("PAPER_POSITION_SIZE_USDC", "25"))
PAPER_POSITION_SIZE = PAPER_POSITION_SIZE_USDC
PAPER_MIN_POSITION_SIZE = float(os.getenv("PAPER_MIN_POSITION_SIZE_USDC", "15"))
PAPER_MAX_POSITION_SIZE = float(os.getenv("PAPER_MAX_POSITION_SIZE_USDC", "100"))
# Raised from 15→25 (2026-03-16): portfolio now mixes long-duration political
# bets (weeks-months) with same-day sports trades. 15 was too low — 17 open
# positions blocked all new entries. Political trades legitimately sit open
# for 20+ days while sports slots turn over in <1 day. 25 gives headroom for
# both without letting the portfolio grow uncontrollably.
PAPER_MAX_CONCURRENT = int(os.getenv("PAPER_MAX_CONCURRENT", "25"))
PAPER_STOP_LOSS_PCT = float(os.getenv("PAPER_STOP_LOSS_PCT", "0.15"))

# ── Tiered stop-loss thresholds (by entry price) ─────────────────────────────
# Prediction markets behave differently at different probability levels.
# A 0.10 entry has ±40% noise band just from liquidity; applying a flat 15%
# stop is guaranteed to get shaken out before any real signal materialises.
# Tiers mirror professional PM risk management:
#
#   entry ≤ LOW   → NO stop loss  (speculative range — stop = noise)
#   entry ≤ MID   → WIDE stop     (moderate probability, needs room to breathe)
#   entry ≤ HIGH  → MID stop      (balanced probability range)
#   entry > HIGH  → TIGHT stop    (high conviction, exit fast on adverse drift)
PAPER_STOP_LOSS_LOW_ENTRY  = float(os.getenv("PAPER_STOP_LOSS_LOW_ENTRY",  "0.20"))
PAPER_STOP_LOSS_MID_ENTRY  = float(os.getenv("PAPER_STOP_LOSS_MID_ENTRY",  "0.45"))
PAPER_STOP_LOSS_HIGH_ENTRY = float(os.getenv("PAPER_STOP_LOSS_HIGH_ENTRY", "0.70"))
PAPER_STOP_LOSS_PCT_WIDE   = float(os.getenv("PAPER_STOP_LOSS_PCT_WIDE",   "0.28"))
PAPER_STOP_LOSS_PCT_MID    = float(os.getenv("PAPER_STOP_LOSS_PCT_MID",    "0.22"))
PAPER_STOP_LOSS_PCT_TIGHT  = float(os.getenv("PAPER_STOP_LOSS_PCT_TIGHT",  "0.15"))

# ── Trailing stop ─────────────────────────────────────────────────────────────
# Once a position appreciates TRIGGER_PCT above entry, activate trailing stop.
# If price then falls DRAWDOWN_PCT below the recorded peak, exit.
# Locks in profits on big winners without cutting them short too early.
# Trailing trigger lowered from 50% to 20% (2026-03-24 research: positions that
# gain 50% have often already resolved. 20% captures more partial wins.)
PAPER_TRAILING_STOP_TRIGGER_PCT  = float(os.getenv("PAPER_TRAILING_STOP_TRIGGER_PCT",  "0.20"))
# Trail at 50% of gain from peak (was 25%). If we're up 30%, trail at +15%.
# If it drops to +15%, exit with profit instead of riding back to zero.
PAPER_TRAILING_STOP_DRAWDOWN_PCT = float(os.getenv("PAPER_TRAILING_STOP_DRAWDOWN_PCT", "0.50"))

# ── Expiry urgency exit ──────────────────────────────────────────────────────
# If market expires within this many hours AND position PnL is within the
# dead-zone threshold of entry (going nowhere), exit to free capital.
# Dead capital approaching expiry is worse than a small realized loss.
PAPER_EXPIRY_URGENCY_HOURS = float(os.getenv("PAPER_EXPIRY_URGENCY_HOURS", "48"))
PAPER_EXPIRY_DEAD_ZONE_PCT = float(os.getenv("PAPER_EXPIRY_DEAD_ZONE_PCT", "0.05"))  # within 5% of entry

# ── Adverse trend exit ───────────────────────────────────────────────────────
# If position is down AND price has moved adversely for N consecutive checks,
# exit early. Unlike flat stop-loss, this detects sustained directional movement
# (thesis invalidation signal) vs temporary noise dips.
# Requires: down >= ADVERSE_TREND_MIN_DROP AND N consecutive adverse moves.
PAPER_ADVERSE_TREND_CHECKS = int(os.getenv("PAPER_ADVERSE_TREND_CHECKS", "4"))  # 4 consecutive adverse checks (~8min at 2min cycle)
PAPER_ADVERSE_TREND_MIN_DROP = float(os.getenv("PAPER_ADVERSE_TREND_MIN_DROP", "0.10"))  # must be down 10%+ from entry

# ── Min absolute dollar loss gate ─────────────────────────────────────────────
# Don't fire stop-loss if the absolute dollar loss is below $N.
# Prevents closing tiny positions on normal bid/ask spread noise.
PAPER_STOP_LOSS_MIN_DOLLARS = float(os.getenv("PAPER_STOP_LOSS_MIN_DOLLARS", "3.0"))

PAPER_TAKE_PROFIT_PCT = float(os.getenv("PAPER_TAKE_PROFIT_PCT", "0.50"))
PAPER_SIZING_MIN_QROI = float(os.getenv("PAPER_SIZING_MIN_QROI", "10"))
PAPER_SIZING_ELITE_QROI = float(os.getenv("PAPER_SIZING_ELITE_QROI", "50"))
PAPER_SIZING_MIN_INSIDER_SCORE = float(os.getenv("PAPER_SIZING_MIN_INSIDER_SCORE", "70"))
PAPER_SIZING_ELITE_INSIDER_SCORE = float(os.getenv("PAPER_SIZING_ELITE_INSIDER_SCORE", "90"))
PAPER_MAX_DAYS_TO_RESOLUTION = int(os.getenv("PAPER_MAX_DAYS_TO_RESOLUTION", "60"))
# Raised from 55→70 (2026-03-16), then 70→80 (2026-03-22):
#   30-day resolved outcomes (insider_candidate trades, Mar 2026):
#   80+:   59.7% WR, +$113 PnL (71 resolved)  ← above break-even (~59%)
#   90-100: 61.4% WR, +$384 PnL (153 resolved) ← only profitable band at scale
#   80-89:  51.5% WR,  -$36 PnL  (33 resolved) ← below break-even
#   70-79:  47.1% WR,  +$92 PnL  (17 resolved) ← coin flip, small sample
# Break-even at avg_win=$32 / avg_loss=$40 is 55.9%. Only 90+ clears it reliably.
# Updated 2026-03-30 from 234 resolved trades (was 80, raised to 90).
PAPER_ENTRY_MIN_INSIDER_SCORE = float(os.getenv("PAPER_ENTRY_MIN_INSIDER_SCORE", "90"))
# Wallet quality score minimum for entry (wallet-level selection, not position timing)
PAPER_MIN_WALLET_QUALITY_SCORE = float(os.getenv("PAPER_MIN_WALLET_QUALITY_SCORE", "30"))
# Global entry price floor — sub-0.45 entries at insider 90+ show 35% WR and
# destroyed $167 in P&L (27 resolved). Edge starts at 0.45 (61% WR, +$529).
# Updated 2026-03-30 from 234 resolved trades (was 0.40, raised to 0.45).
PAPER_MIN_ENTRY_PRICE = float(os.getenv("PAPER_MIN_ENTRY_PRICE", "0.45"))
PAPER_MAX_EXPOSURE_PER_MARKET = float(os.getenv("PAPER_MAX_EXPOSURE_PER_MARKET_USDC", "200"))
PAPER_MAX_ADVERSE_DRIFT = float(os.getenv("PAPER_MAX_ADVERSE_DRIFT_PCT", "15"))
PAPER_MAX_FAVORABLE_DRIFT = float(os.getenv("PAPER_MAX_FAVORABLE_DRIFT_PCT", "40"))
PAPER_SKIP_WALLET_CLASSES = frozenset({"arb_fund"})

# Market category filter — block entry on markets where we have demonstrated
# negative edge. Crypto/commodities (binary price-target markets) showed
# -43.5% avg ROI, 40% WR across 5 closed trades. The issue: these markets
# have long duration and high variance; price-target speculation from wallets
# we're copying is not reliably informed vs. random on these market types.
# Sports (58% WR, +18% ROI) and politics (62% WR, +12% ROI) remain enabled.
# Set True to block crypto/commodity market entries from the general pool.
# Per-strategy override still possible via strategy parameters.
PAPER_SKIP_CRYPTO_MARKETS = os.getenv("PAPER_SKIP_CRYPTO_MARKETS", "true").lower() == "true"

# Keywords used to detect crypto/commodity binary price-target markets.
# Matched case-insensitively against market_title at entry time.
PAPER_CRYPTO_MARKET_KEYWORDS = frozenset({
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "doge",
    "hyperliquid", "hype", "crypto", "crude oil", "gold (gc)", "silver",
    "oil hit", "oil dip", " hit $", "hit (high)", "hit (low)",
    " dip to $", "reach $",
})

# Geopolitical crisis market filter (2026-03-24 backtest):
# Iran/war/ceasefire/regime markets: 40% WR, -$132 PnL across 14 resolved trades.
# Insider signals on these markets are NOT reliably informed — everyone is speculating.
# Set True to block geopolitical crisis markets from the insider copy pool.
PAPER_SKIP_GEO_CRISIS_MARKETS = os.getenv("PAPER_SKIP_GEO_CRISIS_MARKETS", "true").lower() == "true"

PAPER_GEO_CRISIS_KEYWORDS = frozenset({
    "ceasefire", "military action", "military operation",
    "regime fall", "regime change", "forces enter",
    "strait of hormuz", "hormuz", "nuclear facility",
    "invade iran", "strike iran", "iran strike",
    "war end", "conflict end", "war day",
    "khamenei", "pahlavi", "kharg island",
    "iranian regime",
})
# These keywords should NOT trigger geo crisis filter (false positives)
PAPER_GEO_CRISIS_EXCLUDE = frozenset({
    "ufc", "fight night", "bellator", "boxing", "mma",
})

# Soccer market filter (2026-03-24 backtest):
# Soccer: 42.9% WR, -$110 PnL. Outcomes too uncertain for insider copy.
PAPER_SKIP_SOCCER_MARKETS = os.getenv("PAPER_SKIP_SOCCER_MARKETS", "true").lower() == "true"

PAPER_SOCCER_KEYWORDS = frozenset({
    "win on 2026", "fc ", "atletico", "real madrid", "barcelona",
    "juventus", "bayern", "chelsea", "arsenal", "liverpool",
    "manchester", "psg", "dortmund", "inter milan", "napoli",
    "ligue 1", "serie a", "bundesliga", "la liga", "premier league",
    "relegat",
})

# Execution bot filter: wallets reclassified to speculator_bot with
# very high spec scores are likely execution bots (burst trading pattern).
# Their position insider scores are inflated by the win_rate component
# but their edge comes from speed, not information. Empirically, ALL
# paper trade losses at ins>=80 came from wallets that degraded to
# speculator_bot with spec_score 100.
# Set this True to skip speculator_bot wallets entirely (more conservative).
# Set False to use the existing WR/ROI gate.
PAPER_SKIP_EXECUTION_BOTS = os.getenv("PAPER_SKIP_EXECUTION_BOTS", "false").lower() == "true"
RESOLUTION_WIN_THRESHOLD = float(os.getenv("RESOLUTION_WIN_THRESHOLD", "0.97"))
RESOLUTION_LOSS_THRESHOLD = float(os.getenv("RESOLUTION_LOSS_THRESHOLD", "0.03"))
SPECULATOR_BOT_MIN_WIN_RATE = 0.85
SPECULATOR_BOT_MIN_QUALIFYING_ROI = 15.0
STALE_DAYS = 14
# Grace period after signal_disqualified_at before force-closing the position.
# 3 days gives enough time for transient API blips to self-correct before we exit.
# Bypasses market_pending_resolution — disqualification means the thesis is invalid
# regardless of when the underlying market resolves.
DISQUALIFIED_EXIT_DAYS = 3

# ---------------------------------------------------------------------------
# Shared sizing / gate constants (referenced by paper_trader + strategy plugins)
# ---------------------------------------------------------------------------

# Kelly criterion bankroll for NO-value strategies
PAPER_BANKROLL_USDC = float(os.getenv("PAPER_BANKROLL_USDC", "500"))

# Minimum confidence score for paper entry (0–100 scale from analyze_users)
PAPER_MIN_CONFIDENCE_SCORE = float(os.getenv("PAPER_MIN_CONFIDENCE_SCORE", "35.0"))

# Minimum sample / win-rate for low-signal wallets before entry is allowed
PAPER_LOW_SIGNAL_MIN_SAMPLE = int(os.getenv("PAPER_LOW_SIGNAL_MIN_SAMPLE", "10"))
PAPER_LOW_SIGNAL_MIN_WIN_RATE = float(os.getenv("PAPER_LOW_SIGNAL_MIN_WIN_RATE", "0.65"))

# Speculator score ceiling — wallets above this are execution bots, not signal traders
PAPER_MAX_SPECULATOR_SCORE = float(os.getenv("PAPER_MAX_SPECULATOR_SCORE", "70"))

# Price below which we treat a position as near-zero and suppress source-based exits
NEAR_ZERO_PRICE_THRESHOLD = float(os.getenv("NEAR_ZERO_PRICE_THRESHOLD", "0.10"))

# Per-category entry price caps — prevent entering overpriced geopolitics/crypto markets
PAPER_CATEGORY_MAX_ENTRY: dict = {
    "geopolitics":       float(os.getenv("PAPER_CAT_MAX_GEOPOLITICS", "0.45")),
    "crypto/commodity":  float(os.getenv("PAPER_CAT_MAX_CRYPTO", "0.40")),
    "tech/ai_launch":    float(os.getenv("PAPER_CAT_MAX_TECH", "0.50")),
}
