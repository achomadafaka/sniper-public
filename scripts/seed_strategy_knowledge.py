#!/usr/bin/env python3
"""Seed Supabase brain with per-strategy knowledge bases + audited HL research notes.

Run on VPS:
    python3 scripts/seed_strategy_knowledge.py

Writes LLM-optimized knowledge entries for each active strategy, plus the
audited HL signal research methodology/results/conclusions captured after the
March 29 hard review.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_env_path = os.path.join(ROOT, ".env")
if os.path.isfile(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()

from bot.supabase_helper import upsert_memory

now = datetime.now(timezone.utc).isoformat()

ENTRIES = [
    # ── Strategy Knowledge Bases ─────────────────────────────────────
    {
        "name": "strategy_knowledge/hl_liquidation_v1",
        "description": "HL Liquidation v1: counter-trade whales approaching forced liquidation on Hyperliquid isolated-margin positions",
        "content": {
            "timestamp": now,
            "version": "1.0",
            "strategy_id": 33,
            "status": "canary_mode",
            "thesis": (
                "When a whale's isolated-margin position on Hyperliquid approaches its liquidation price "
                "(distance worsening over consecutive polls), the forced closure will create temporary "
                "price pressure in the direction of liquidation. A counter-position (opposite side to the whale) "
                "captures the bounce-back or continuation of the cascade."
            ),
            "mechanism": {
                "data_source": "hl_whale_positions table, populated by hl_whale_poller.py every 60s",
                "signal": "isolated margin position with distance_pct < 25% AND worsening (prev_distance > current_distance)",
                "entry": "counter-trade the whale: if whale is long, we go short and vice versa",
                "sizing": "margin = clamp(bankroll * 0.01 * (0.75 + confidence), 10, 25) with confidence = 1 - distance/entry_threshold",
                "leverage": "1.5x default, 2.0x in panic zone (<15% distance)",
                "exit_conditions": [
                    "take_profit: 2% move in our direction",
                    "stop_loss: 1% move against us",
                    "forced_liquidation: our mark crossed our liq price",
                    "time_stop: 30 minutes",
                    "whale_flipped_side: whale changed direction",
                    "whale_exited: whale position gone from table",
                    "thesis_invalidated: whale distance widened past entry_threshold + 5",
                ],
            },
            "evidence": {
                "live_trades": 7,
                "closed_pnl_usdc": 0.36,
                "win_rate": "28.6% (2/7)",
                "dominant_exit": "time_stop (all exits are 30-min time stops, zero thesis-driven exits)",
                "asset_concentration": "MON carries all profit (+$0.92), BTC is net negative (-$0.56)",
                "verdict": "UNPROVEN. No thesis-driven exits have fired. All trades run to time stop.",
            },
            "known_limitations": [
                "Public signal: clearinghouseState is visible to everyone",
                "Sparse supply: only 5 isolated positions across 20 tracked wallets",
                "Polling cadence: 60s polls cannot capture sub-second liquidation cascades",
                "BTC concentration: most signals are BTC, one position at a time due to dedup",
                "Fee drag: 0.045% round-trip on tiny notional ($50) eats most of the PnL",
            ],
            "gpt_critique_date": "2026-03-28",
            "gpt_critique_summary": (
                "The naive thesis is mechanically plausible but not a real edge because: "
                "(1) the signal is public, (2) polling is too slow for the event, "
                "(3) all exits are time stops not thesis exits, (4) BTC is fee-dragged noise, "
                "(5) MON profits are 2-trade luck not evidence of durable edge."
            ),
            "recommendation": "Keep in canary mode. Do not promote. Collect data for 30+ days before re-evaluating.",
        },
    },
    {
        "name": "strategy_knowledge/crypto_momentum_v1",
        "description": "BTC Binary Momentum: trade Polymarket crypto price-target markets using Binance momentum + macro bias",
        "content": {
            "timestamp": now,
            "version": "1.1",
            "strategy_id": 16,
            "status": "paper_paused_pending_research_followup",
            "thesis": (
                "Polymarket binary crypto markets (Will BTC be above $X on date Y?) are priced as probabilities. "
                "When short-term BTC momentum is positive, cheap YES tokens on near-strike price targets "
                "offer asymmetric payoff: risk $0.20 to win $0.80. You only need >20% accuracy to profit. "
                "Combined with macro news bias filtering and proximity scoring, the strategy selects markets "
                "where momentum aligns with direction and payoff structure is favorable."
            ),
            "mechanism": {
                "data_source": "Binance BTCUSDT spot price + klines + funding rate + order book",
                "signal": "Positive 24h momentum + proximity to strike price + macro bias alignment",
                "entry_gates": [
                    "price in [0.10, 0.65] range (asymmetric payoff zone)",
                    "proximity score >= 0.30",
                    "max 5 days to resolution",
                    "max concurrent: 8 positions",
                    "momentum direction confirmed at entry time",
                    "macro bias must not contradict (neutral or aligned)",
                    "news volume gate: block if >100 BTC items/24h (crowded trade)",
                ],
                "exit_conditions": [
                    "market_closed: PM market resolved",
                    "early_close_win: price >= 0.97 (resolution likely)",
                    "early_close_loss: price <= 0.03 (resolution likely)",
                    "take_profit: ROI >= 50%",
                    "stale: held > stale_days",
                ],
                "min_hold_guard": "60 seconds (prevents same-cycle phantom closes)",
            },
            "evidence": {
                "total_positions": 145,
                "phantom_closes_pre_fix": 108,
                "clean_trades": 37,
                "clean_wr": "51.4% (19/37)",
                "clean_pnl": "+$6.08",
                "pre_bug_cohort_mar16_17": {"trades": 20, "wr": "55%", "pnl": "+$26.36"},
                "post_reactivation_mar28": {"positions": 6, "first_close": "+$7.16 (47% ROI on $68K target)"},
                "biggest_winner": "+$42 (BTC reach $78K, entry $0.05, exit $0.19, 280% ROI)",
                "pattern": "Wins come from cheap entries ($0.05-$0.10) on unlikely-but-possible targets during rallies",
                "hl_research_audit_2026_03_29": (
                    "No robust crowded-long reversal edge. The only salvageable HL derivative path is a "
                    "bearish deleveraging continuation filter that still needs PM-native paper testing."
                ),
            },
            "known_limitations": [
                "Currently paused operationally; paper-only restart should happen only with an explicit follow-up experiment plan",
                "Currently long-only (no bearish/NO-side entries)",
                "Uses Binance data, not PM-native resolved-market backtests",
                "The old HL crowded-long gate thesis did not survive audit and should not be added",
                "Phantom close bug existed March 23-24, fixed by 60s min-hold guard",
                "BTC funding may carry weak information, but not enough to use as a standalone gate",
                "Order book imbalance is logged but not used as a gate",
            ],
            "planned_upgrades": [
                "Bidirectional: add NO-side entries when momentum is negative",
                "PM-native bearish veto experiment: test OI down + price down as a risk-off filter on BTC/SOL markets",
                "Keep HL microstructure research separate from the daily-horizon strategy path",
            ],
            "recommendation": (
                "Do not add the crowded-long HL gate. If crypto_momentum_v1 is reactivated, treat HL research as a "
                "separate PM-native veto experiment first, not a production sizing input."
            ),
        },
    },
    {
        "name": "strategy_knowledge/insider_copy_v1",
        "description": "Insider Copy v1: copy high-quality Polymarket wallets with insider-like behavior patterns",
        "content": {
            "timestamp": now,
            "version": "1.0",
            "status": "active_paper",
            "thesis": (
                "Certain Polymarket wallets exhibit insider-like trading patterns: they enter positions at low prices "
                "before markets move in their favor, consistently. Copying their entries (with timing and drift filters) "
                "captures the information advantage. ADR-007 (March 2026) established a two-score system: "
                "wallet_quality_score (P&L-first) for wallet SELECTION, position_insider_score (behavior/timing) "
                "for entry TIMING within selected wallets."
            ),
            "mechanism": {
                "data_source": "position_signals table (analyzer output) + followed_wallets",
                "signal": "position_insider_score >= 70 on open position from a followed wallet",
                "entry": "copy at current CLOB price, with adverse drift check (max 15% drift from source entry)",
                "sizing": "$15-50 per position depending on strategy params",
                "exit_conditions": [
                    "resolution_win: price >= 0.97",
                    "resolution_loss: price <= 0.03",
                    "source_exited: original wallet closed their position",
                    "score_decay: insider score dropped >= 25 points from entry",
                    "stale: held > stale_days",
                    "cascade_exit: correlated positions all moving against us",
                ],
            },
            "evidence": {
                "paper_trades_total": 342,
                "resolved": 225,
                "wins": 133,
                "resolved_wr": "59.1%",
                "resolved_pnl": "+$557.88",
                "edge_by_score_band": {
                    "90-100": "67.6% WR, +$343 (strongest)",
                    "80-89": "50.0% WR, -$51",
                    "70-79": "50.0% WR, +$152",
                    "60-69": "55.6% WR, +$11",
                    "below_60": "~50% WR, small losses",
                },
                "scoring_audit_2026_03_25": (
                    "Original insider_score was INVERSELY correlated with wallet P&L. "
                    "Score 80+ wallets avg $16K P&L, score 1-19 avg $160K. "
                    "Rebuilt as followability score with 45% lb_pnl weight. "
                    "Gate 0 (lifetime P&L floor) added, disqualifying 60/478 wallets."
                ),
            },
            "known_limitations": [
                "Follows public wallet behavior (not actual insider info)",
                "Copy latency: signal arrives via analyzer, not real-time",
                "Adverse drift: market may move before we enter",
                "Resolution timing: most PnL comes from waiting for binary outcome, not trading skill",
            ],
            "recommendation": "Most proven strategy by trade count and PnL. Primary candidate for real money when ready.",
        },
    },
    {
        "name": "strategy_knowledge/political_no_value_v1",
        "description": "Political NO Value: exploit behavioral overpricing of dramatic political events on Polymarket",
        "content": {
            "timestamp": now,
            "version": "1.0",
            "strategy_id": 21,
            "status": "active_paper",
            "thesis": (
                "Prediction markets systematically overprice dramatic political events because retail traders "
                "anchor on availability bias (dramatic events FEEL more likely than they are). "
                "Buying NO tokens on overpriced YES positions captures the calibration gap. "
                "Le 2026 (arxiv) found a ~13pp underconfidence in political NO outcomes. "
                "This is a structural mispricing with a behavioral cause — one of the proven edge types."
            ),
            "mechanism": {
                "data_source": "market_metadata + position_signals from analyzer",
                "signal": "Political/geopolitical market where NO token is underpriced (YES > fair probability)",
                "entry": "Buy NO at market price when YES appears overpriced vs calibration curve",
                "sizing": "$15 per position",
                "exit": "Resolution (binary outcome) or early close on price move",
            },
            "evidence": {
                "research_wr": "80% in backtested political markets",
                "academic_source": "Le 2026, Domain-Specific Calibration, arxiv",
                "calibration_gap": "~13pp systematic overpricing of dramatic events",
                "live_positions": 16,
                "strategy_pnl": "Small positive (early stage)",
                "market_types_tested": "Political elections, geopolitical events, policy decisions",
            },
            "known_limitations": [
                "Long resolution times (weeks to months)",
                "Capital locked during hold period",
                "Low turnover = slow compounding",
                "Research WR may not transfer to live trading (selection bias)",
                "PM spread (~1.2%) reduces edge on small positions",
            ],
            "recommendation": "Strong theoretical backing. Continue paper trading. Capital-intensive but high-confidence edge.",
        },
    },
    # ── Research Methodology (audited) ───────────────────────────────
    {
        "name": "research/hl_signal_research_methodology",
        "description": "AUDITED: HL derivatives signal research methodology after hard review of OI lag, units, and source-splice risk.",
        "content": {
            "timestamp": now,
            "version": "2.2_audited",
            "status": "audited_complete",
            "purpose": (
                "Determine which combinations of Hyperliquid derivatives signals predict BTC/ETH/SOL "
                "price direction at horizons relevant to Polymarket binary crypto markets (1h to 3d)."
            ),
            "data_contracts": {
                "oi_primary_source": "Coinalyze /open-interest-history with convert_to_usd=true",
                "oi_fallback_source": "hyperliquid_snapshots daily last-reading fallback, explicitly tagged",
                "oi_required_metadata": ["source", "oi_units", "source_detail"],
                "oi_safe_usage_rule": (
                    "Only compute OI deltas on usd_notional rows, and never compute across a source_detail change."
                ),
                "oi_timestamp_policy": (
                    "Defensive 24h lag for daily Coinalyze OI before hourly anchoring because the daily series behaves "
                    "like period-start timestamps for end-of-day values."
                ),
            },
            "signal_families": {
                "crowded_long_reversal": "OI up + momentum up -> bearish next 24h",
                "deleveraging_continuation": "OI down + momentum down -> bearish next 24h",
                "btc_funding_watchlist": "BTC funding as a weak standalone directional covariate, not a gate",
                "short_horizon_flow": "5-10 minute HL trade-flow imbalance for ultra-short PM markets only",
            },
            "test_framework": {
                "headline_sample": "Test-only, non-overlapping 24h windows",
                "supporting_sample": "Overlapping windows with block bootstrap (24h blocks)",
                "train_test_split": "First 4 months train, last 2 months test",
                "robustness_checks": [
                    "OI lag sweep across 0h to 48h",
                    "Coinalyze-only rerun after stripping snapshot fallback tail",
                    "Per-asset regressions instead of pooled HAC panel regression",
                    "Threshold sensitivity for both crowded-long and deleveraging variants",
                ],
                "minimum_N": 20,
            },
            "limitations": [
                "No true PM-native resolved-market backtest yet",
                "Coinalyze timestamp semantics are inferred defensively rather than explicitly documented",
                "Short-horizon flow findings are exploratory and use live-only coverage",
            ],
        },
    },
    # ── Research Results (stub) ──────────────────────────────────────
    {
        "name": "research/hl_signal_research_results",
        "description": "AUDITED: HL derivatives research results after correcting OI lag assumptions and identifying the OI unit/source splice bug.",
        "content": {
            "timestamp": now,
            "version": "2.2_audited",
            "status": "audited_complete",
            "headline_result": "The original crowded-long reversal thesis failed hard audit.",
            "crowded_long_reversal": {
                "old_result_reference": {
                    "script_commit": "c8a27cf",
                    "n": 1552,
                    "mean_pct": -0.5627,
                    "bearish_pct": 55.5,
                    "p_value": "<0.0001",
                },
                "corrected_test_only_non_overlap": {
                    "all": {"n": 41, "mean_pct": -0.39, "bearish_pct": 58.5, "p_value": 0.2892},
                    "sol": {"n": 23, "mean_pct": 0.7622, "bearish_pct": 39.1},
                },
                "robustness": (
                    "The bearish effect is strongest at 0h OI lag and decays quickly once sane lag is introduced. "
                    "Coinalyze-only reruns do not revive it."
                ),
            },
            "data_quality_findings": {
                "issue": "hl_oi_history mixed Coinalyze asset-unit rows with Hyperliquid snapshot USD-notional rows",
                "impact": "schema mismatch and source splice risk; not the root cause of the crowded-long thesis failure, but a real hygiene bug",
                "repair_rule": "normalize Coinalyze to usd_notional and persist oi_units + source_detail",
            },
            "deleveraging_continuation": {
                "definition": "OI down < -0.5% and 4h momentum down < -0.5%",
                "test_only_non_overlap": {
                    "all": {"n": 41, "mean_pct": -0.4832, "bearish_pct": 56.1},
                    "btc": {"n": 30, "mean_pct": -0.9634, "bearish_pct": 70.0, "one_sided_p": 0.0368},
                    "sol": {"n": 30, "mean_pct": -1.3727, "bearish_pct": 76.7, "one_sided_p": 0.0505},
                },
                "block_bootstrap_overlapping": {
                    "all": {"n": 714, "mean_pct": -0.8667, "p_value": 0.0003},
                    "coinalyze_only": {"n": 658, "mean_pct": -0.7173, "p_value": 0.0073},
                    "sol_coinalyze_only": {"n": 253, "mean_pct": -1.343, "p_value": 0.002},
                },
                "robustness": "Negative across 12h to 36h OI lags, especially in SOL and secondarily BTC.",
            },
            "btc_funding_only": {
                "regression_note": "BTC funding remained significant in per-asset regression",
                "direct_threshold_test": {"n": 53, "mean_pct": -0.4438, "bearish_pct": 56.6, "p_value": 0.1309},
                "verdict": "watchlist material only, not a standalone gate",
            },
            "short_horizon_trade_flow": {
                "status": "exploratory_only",
                "notable_signals": [
                    "SOL 10-minute flow imbalance -> next return: r=0.174, t=2.259",
                    "HYPE 5-minute flow imbalance -> next return: r=0.159, t=2.918",
                ],
                "warning": "This is a separate microstructure project, not evidence for the daily BTC gate thesis.",
            },
        },
    },
    # ── Research Conclusions (audited) ───────────────────────────────
    {
        "name": "research/hl_signal_research_conclusions",
        "description": "AUDITED: Conclusions and next actions from the HL derivatives signal review.",
        "content": {
            "timestamp": now,
            "version": "2.2_audited",
            "status": "audited_complete",
            "bottom_line": (
                "Kill the original crowded-long reversal thesis. The only credible salvage path is a narrower "
                "deleveraging continuation filter, strongest in SOL and secondarily BTC, and it still needs "
                "PM-native paper testing before strategy use."
            ),
            "keep": [
                "The research pipeline and train/test discipline",
                "Deleveraging continuation as a bearish veto or bias filter candidate",
                "Short-horizon HL flow research as a separate ultra-short-duration project",
            ],
            "discard": [
                "Crowded longs get punished as a daily HL -> PM gate",
                "Funding-alone as a single-asset directional trigger",
                "Old PM EV tables as evidence of tradable edge",
            ],
            "required_repairs": [
                "Backfill Coinalyze OI with convert_to_usd=true",
                "Store oi_units and source_detail in hl_oi_history",
                "Block OI deltas across source-detail changes",
            ],
            "next_sprint": [
                "Patch the OI contract and rerun the full research pipeline",
                "Build one PM-native backtest for BTC/SOL bearish or NO-side markets using the deleveraging filter",
                "Keep the short-horizon trade-flow branch separate from the daily momentum strategy",
            ],
            "strategy_implication": (
                "For crypto_momentum_v1, the correct question is not when HL says buy more crypto YES. "
                "It is when derivatives say avoid bullish entries because downside continuation is more likely."
            ),
        },
    },
]


def main():
    success = 0
    failed = 0

    for entry in ENTRIES:
        try:
            content_str = json.dumps(entry["content"], indent=2) if isinstance(entry["content"], dict) else str(entry["content"])
            upsert_memory(
                type="knowledge",
                name=entry["name"],
                description=entry["description"],
                content=content_str,
            )
            print(f"  OK: {entry['name']}")
            success += 1
        except Exception as e:
            print(f"  FAIL: {entry['name']} — {e}")
            failed += 1

    print(f"\nDone: {success} written, {failed} failed")


if __name__ == "__main__":
    main()
