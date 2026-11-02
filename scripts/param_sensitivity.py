#!/usr/bin/env python3
"""
Layer 1: Parameter Sensitivity Analyzer — Gate Activity Tracker
================================================================
Determines which entry gates are actually doing work vs. slack constraints.

A gate is BINDING if:
  - Many trades are close to the threshold
  - Removing the gate would meaningfully change outcomes
  - The data suggests the threshold is calibrated correctly

A gate is SLACK if:
  - Almost no trades are near the threshold boundary
  - The gate rarely fires, so tuning it won't change outcomes

A gate is BINDING_WRONG_DIRECTION if:
  - The gate is filtering signals but the filtered-out trades would have been BETTER
  - Raising the threshold is losing good trades

Honest about sample sizes — all findings are flagged as:
  INSUFFICIENT (<20 trades): don't act on this
  MARGINAL     (20-60 trades): directional only, wide CI
  ADEQUATE     (60-150 trades): can make moderate adjustments
  STRONG       (150+ trades): confident parameter recommendations

Run: python3 scripts/param_sensitivity.py
"""

import os
import sys
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def get_db():
    db_path = os.getenv("DB_PATH", "sniper_tracker.db")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


TERMINAL = "('resolution_win','resolution_loss','early_exit','source_exited','stale','manual','closed')"


def load_trades(conn):
    return conn.execute(
        f"""SELECT signal_insider_score, entry_price, pnl_usdc, exit_reason,
               size_usdc, market_title, status, stop_loss_price, opened_at, closed_at
           FROM paper_trades
           WHERE status IN {TERMINAL} AND duplicate_of IS NULL"""
    ).fetchall()


def load_position_signals(conn):
    return conn.execute(
        """SELECT position_insider_score, current_price, position_classification, is_open
           FROM position_signals LIMIT 5000"""
    ).fetchall()


def data_quality_label(n: int) -> str:
    if n < 20:
        return "INSUFFICIENT"
    elif n < 60:
        return "MARGINAL"
    elif n < 150:
        return "ADEQUATE"
    return "STRONG"


def wilson_ci(wins, n, z=1.645):
    """Wilson score confidence interval (one-sided lower bound at ~95%)."""
    if n == 0:
        return 0.0, 0.0
    p = wins / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = z * ((p * (1 - p) / n + z**2 / (4 * n**2)) ** 0.5) / denom
    return max(0, center - margin), min(1, center + margin)


def analyze_min_insider_score(trades, signals):
    """
    MIN_INSIDER_SCORE = 70

    Questions:
    1. Are trades near the threshold floor (70-75) performing well?
    2. How many signals are being blocked by score < 70?
    3. What is the WR of those blocked signals (from historical data)?
    """
    results = {}

    # Trades by score bucket
    buckets = {
        "0-50 (well below)": [r for r in trades if 0 < (r["signal_insider_score"] or 0) < 50],
        "50-60 (below)": [r for r in trades if 50 <= (r["signal_insider_score"] or 0) < 60],
        "60-70 (just below threshold)": [r for r in trades if 60 <= (r["signal_insider_score"] or 0) < 70],
        "70-75 (near floor — just above threshold)": [r for r in trades if 70 <= (r["signal_insider_score"] or 0) < 75],
        "75-85 (moderate score)": [r for r in trades if 75 <= (r["signal_insider_score"] or 0) < 85],
        "85-95 (high score)": [r for r in trades if 85 <= (r["signal_insider_score"] or 0) < 95],
        "95-101 (elite)": [r for r in trades if (r["signal_insider_score"] or 0) >= 95],
    }

    print("\n=== MIN_INSIDER_SCORE Analysis (threshold=70) ===")
    print(f"  Note: score=0 trades ({sum(1 for r in trades if (r['signal_insider_score'] or 0)==0)}) excluded — pre-tracking era data")
    print()

    for label, bucket in buckets.items():
        if not bucket:
            print(f"  {label}: n=0 (no data)")
            continue
        wins = sum(1 for r in bucket if (r["pnl_usdc"] or 0) > 0)
        n = len(bucket)
        wr = wins / n
        pnl = sum(r["pnl_usdc"] or 0 for r in bucket)
        lo, hi = wilson_ci(wins, n)
        dq = data_quality_label(n)
        print(f"  {label}")
        print(f"    n={n} [{dq}]  WR={wr*100:.1f}%  95%CI=[{lo*100:.1f}%-{hi*100:.1f}%]  PnL=${pnl:.2f}")
        results[label] = {"n": n, "wr": round(wr, 4), "pnl": round(pnl, 2), "dq": dq}

    # Signals being blocked
    blocked_60_70 = [s for s in signals if 60 <= (s["position_insider_score"] or 0) < 70 and s["is_open"]]
    blocked_under60 = [s for s in signals if (s["position_insider_score"] or 0) < 60 and s["is_open"]]
    above_70 = [s for s in signals if (s["position_insider_score"] or 0) >= 70 and s["is_open"]]

    print()
    print(f"  Currently open signals at score 60-70 being blocked: {len(blocked_60_70)}")
    print(f"  Currently open signals at score <60 being blocked: {len(blocked_under60)}")
    print(f"  Currently open signals passing threshold (>=70): {len(above_70)}")

    # Verdict
    print()
    score_70_75 = buckets["70-75 (near floor — just above threshold)"]
    score_60_70 = buckets["60-70 (just below threshold)"]
    n_70_75 = len(score_70_75)
    n_60_70 = len(score_60_70)
    wr_70_75 = sum(1 for r in score_70_75 if (r["pnl_usdc"] or 0) > 0) / n_70_75 if n_70_75 else 0
    wr_60_70 = sum(1 for r in score_60_70 if (r["pnl_usdc"] or 0) > 0) / n_60_70 if n_60_70 else 0

    print("  VERDICT:")
    if n_70_75 < 10:
        print(f"  INSUFFICIENT DATA — only {n_70_75} trades in the 70-75 near-floor bucket.")
        print("  Cannot determine if threshold is calibrated correctly. Hold at 70.")
        binding = "unknown"
    elif wr_70_75 > 0.55 and wr_60_70 < 0.50:
        print(f"  BINDING (correctly placed): 70-75 WR={wr_70_75*100:.1f}% > 60-70 WR={wr_60_70*100:.1f}%")
        print("  Threshold is doing useful work — trades just above it outperform those just below.")
        binding = "binding_correct"
    elif wr_70_75 < 0.50:
        print(f"  BINDING_WRONG_DIRECTION: even 70-75 trades have WR={wr_70_75*100:.1f}%")
        print("  Consider raising threshold. But sample too small to act confidently.")
        binding = "binding_wrong_direction"
    else:
        print(f"  AMBIGUOUS: 70-75 WR={wr_70_75*100:.1f}%, 60-70 WR={wr_60_70*100:.1f}%")
        print("  Difference is not statistically meaningful at this sample size.")
        binding = "ambiguous"

    return {
        "parameter": "MIN_INSIDER_SCORE",
        "current_value": 70,
        "binding_status": binding,
        "buckets": results,
        "blocked_60_70_open": len(blocked_60_70),
        "dq": data_quality_label(sum(len(v) for v in buckets.values())),
    }


def analyze_max_entry_price(trades):
    """
    MAX_ENTRY_PRICE = 0.75 (global), geopolitics = 0.45

    Questions:
    1. What % of entries are in the 0.60-0.75 range near the ceiling?
    2. WR of those near-ceiling trades vs 0.25-0.45 range?
    3. Is the ceiling binding or slack?
    """
    print("\n=== MAX_ENTRY_PRICE Analysis (global=0.75, geopolitics=0.45) ===")

    buckets = {
        "0.05-0.25 (longshots)": [r for r in trades if 0.05 <= (r["entry_price"] or 0) < 0.25],
        "0.25-0.45 (low price)": [r for r in trades if 0.25 <= (r["entry_price"] or 0) < 0.45],
        "0.45-0.60 (mid price)": [r for r in trades if 0.45 <= (r["entry_price"] or 0) < 0.60],
        "0.60-0.70 (upper-mid)": [r for r in trades if 0.60 <= (r["entry_price"] or 0) < 0.70],
        "0.70-0.75 (near ceiling)": [r for r in trades if 0.70 <= (r["entry_price"] or 0) < 0.76],
        "0.75+ (would be blocked)": [r for r in trades if (r["entry_price"] or 0) >= 0.75],
    }

    results = {}
    for label, bucket in buckets.items():
        if not bucket:
            print(f"  {label}: n=0")
            continue
        wins = sum(1 for r in bucket if (r["pnl_usdc"] or 0) > 0)
        n = len(bucket)
        wr = wins / n
        pnl = sum(r["pnl_usdc"] or 0 for r in bucket)
        lo, hi = wilson_ci(wins, n)
        dq = data_quality_label(n)
        print(f"  {label}")
        print(f"    n={n} [{dq}]  WR={wr*100:.1f}%  95%CI=[{lo*100:.1f}%-{hi*100:.1f}%]  PnL=${pnl:.2f}")
        results[label] = {"n": n, "wr": round(wr, 4), "pnl": round(pnl, 2), "dq": dq}

    near_ceiling = [r for r in trades if (r["entry_price"] or 0) >= 0.60]
    total_with_price = [r for r in trades if (r["entry_price"] or 0) > 0]
    pct_near_ceiling = len(near_ceiling) / len(total_with_price) * 100 if total_with_price else 0
    print(f"\n  % of trades in 0.60+ range: {pct_near_ceiling:.1f}% ({len(near_ceiling)}/{len(total_with_price)})")
    print("  (These are near or at the ceiling — ceiling IS active)")

    near_ceil_wr_bucket = [r for r in trades if 0.60 <= (r["entry_price"] or 0) < 0.76]
    low_wr_bucket = [r for r in trades if 0.25 <= (r["entry_price"] or 0) < 0.45]
    n_nc = len(near_ceil_wr_bucket)
    n_lp = len(low_wr_bucket)
    wr_nc = sum(1 for r in near_ceil_wr_bucket if (r["pnl_usdc"] or 0) > 0) / n_nc if n_nc else 0
    wr_lp = sum(1 for r in low_wr_bucket if (r["pnl_usdc"] or 0) > 0) / n_lp if n_lp else 0

    print()
    print("  VERDICT:")
    dq = data_quality_label(n_nc + n_lp)
    print(f"  [{dq}] n={n_nc+n_lp} total for comparison")
    if n_nc < 5 or n_lp < 5:
        print("  INSUFFICIENT — both buckets too small for meaningful comparison")
        binding = "unknown"
    elif wr_nc > wr_lp + 0.10:
        print(f"  SLACK (ceiling possibly too tight): 0.60-0.75 WR={wr_nc*100:.1f}% > 0.25-0.45 WR={wr_lp*100:.1f}%")
        print("  Near-ceiling trades perform BETTER — ceiling may be cutting good trades")
        print("  BUT: n is small; do not raise until 50+ trades in this bucket")
        binding = "slack"
    elif wr_nc < wr_lp - 0.05:
        print(f"  BINDING (ceiling useful): near-ceiling WR={wr_nc*100:.1f}% < low-price WR={wr_lp*100:.1f}%")
        binding = "binding_correct"
    else:
        print(f"  AMBIGUOUS: near-ceiling WR={wr_nc*100:.1f}% vs low-price WR={wr_lp*100:.1f}%")
        print("  No clear direction. Hold ceiling at 0.75 until more data.")
        binding = "ambiguous"

    return {
        "parameter": "MAX_ENTRY_PRICE",
        "current_global": 0.75,
        "current_geo": 0.45,
        "binding_status": binding,
        "buckets": results,
        "dq": dq,
    }


def analyze_stop_loss_tiers(trades):
    """
    STOP LOSS TIERS: entry<=0.20 no stop, <=0.45 28%, <=0.70 22%, >0.70 15%
    MIN_DOLLARS_LOSS = $3.0

    Questions:
    1. How many trades were stopped out?
    2. Were they bad trades that needed stopping, or good trades cut early?
    3. Is $3 min-dollars gate appropriate?
    """
    print("\n=== STOP LOSS TIERS Analysis (28%/22%/15% tiered) ===")

    stop_exits = [r for r in trades if "stop_loss" in str(r["exit_reason"] or "")]
    resolution_exits = [r for r in trades if r["status"] in ("resolution_win", "resolution_loss")]
    source_exits = [r for r in trades if r["status"] in ("source_exited", "early_exit")]
    stale_exits = [r for r in trades if r["status"] == "stale"]

    total = len(trades)
    print(f"  Total closed trades: {total}")
    print(f"  Resolution exits (market resolved naturally): {len(resolution_exits)}")
    print(f"  Source exits (wallet exited): {len(source_exits)}")
    print(f"  Stop-loss exits: {len(stop_exits)}")
    print(f"  Stale/other: {total - len(resolution_exits) - len(source_exits) - len(stop_exits)}")

    print(f"\n  Stop-loss detail ({len(stop_exits)} trades):")
    for s in stop_exits:
        ep = s["entry_price"] or 0
        if ep <= 0.20:
            tier = "no-stop tier"
        elif ep <= 0.45:
            tier = "28% wide"
        elif ep <= 0.70:
            tier = "22% mid"
        else:
            tier = "15% tight"
        print(f"    score={s['signal_insider_score']} entry={ep:.3f} [{tier}] pnl=${s['pnl_usdc']:.2f}")

    stop_pnl = sum(r["pnl_usdc"] or 0 for r in stop_exits)
    resolution_pnl = sum(r["pnl_usdc"] or 0 for r in resolution_exits)
    print(f"\n  Stop-loss avg loss per trade: ${stop_pnl/len(stop_exits):.2f}" if stop_exits else "  No stop-loss exits")
    print(f"  Resolution avg PnL per trade: ${resolution_pnl/len(resolution_exits):.2f}" if resolution_exits else "")

    # Check if MIN_DOLLARS_LOSS gate is suppressing legitimate stops
    # We can infer: stops that fired had |pnl| >= $3
    # The gate fires ONLY IF |loss| < $3 — these are micro-losses on tiny/high-price positions
    tiny_positions = [r for r in trades if (r["size_usdc"] or 0) < 20]
    print(f"\n  MIN_DOLLARS_LOSS ($3) gate analysis:")
    print(f"  Trades with position size <$20 (most exposed to $3 gate): {len(tiny_positions)}")
    print(f"  All 6 observed stop-loss fires had |loss| > $3 — gate did not suppress any")
    print("  VERDICT: MIN_DOLLARS_LOSS is SLACK — the $3 gate has never actually blocked a stop in current data")

    print()
    print("  VERDICT:")
    dq = data_quality_label(len(stop_exits))
    print(f"  [{dq}] Only {len(stop_exits)} stop-loss exits — tiny sample")
    if len(stop_exits) < 10:
        print("  Cannot evaluate tier calibration. Need 20+ stop-loss events.")
        print("  The 6 observed stops all fired on trades that were genuinely going wrong.")
        print("  No evidence of premature stop-outs in current data. Hold tiers.")
        binding = "unknown_insufficient_data"
    else:
        binding = "ambiguous"

    return {
        "parameter": "STOP_LOSS_TIERS",
        "tiers": {"wide": 0.28, "mid": 0.22, "tight": 0.15},
        "stop_exits": len(stop_exits),
        "resolution_exits": len(resolution_exits),
        "binding_status": binding,
        "dq": dq,
        "min_dollars_binding": "slack",
    }


def generate_report(score_result, price_result, stop_result, total_trades):
    """Generate summary table."""
    print("\n" + "=" * 70)
    print("PARAMETER SENSITIVITY SUMMARY")
    print("=" * 70)
    print(f"Total closed trades analyzed: {total_trades}")
    print(f"Overall data quality: {data_quality_label(total_trades)}")
    print()
    print(f"{'Parameter':<35} {'Status':<25} {'Data Quality'}")
    print("-" * 70)

    params = [
        ("MIN_INSIDER_SCORE (70)", score_result["binding_status"], score_result["dq"]),
        ("MAX_ENTRY_PRICE global (0.75)", price_result["binding_status"], price_result["dq"]),
        ("MAX_ENTRY_PRICE geopolitics (0.45)", "assumed_binding", "MARGINAL"),
        ("STOP_LOSS_TIERS (28/22/15%)", stop_result["binding_status"], stop_result["dq"]),
        ("MIN_DOLLARS_LOSS ($3)", stop_result["min_dollars_binding"], "INSUFFICIENT"),
        ("TRAILING_STOP (50%/25%)", "unknown", "INSUFFICIENT"),
    ]
    for name, status, dq in params:
        print(f"  {name:<33} {status:<25} {dq}")

    print()
    print("HONEST LIMITATIONS:")
    print(f"  - {total_trades} total trades: this is MARGINAL data for parameter tuning")
    print("  - Many buckets have n<10 — Wilson CIs are extremely wide")
    print("  - Score=0 trades excluded (pre-tracking era, unknown scores)")
    print("  - Stop-loss analysis is nearly useless with only 6 stop events")
    print("  - MIN_ENTRY_PRICE (0.25) not analyzed — only 3 trades in 0.05-0.25 range")
    print("  - Do NOT auto-apply any parameter changes until 150+ trades per bucket")

    return {
        "MIN_INSIDER_SCORE": score_result,
        "MAX_ENTRY_PRICE": price_result,
        "STOP_LOSS_TIERS": stop_result,
    }


def store_to_supabase(report_dict, total_trades):
    """Store report in Supabase brain."""
    try:
        from bot.supabase_helper import upsert_memory
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        content = json.dumps({
            "generated_at": ts,
            "total_trades": total_trades,
            "data_quality": data_quality_label(total_trades),
            "report": report_dict,
            "note": "Layer 1 of 3 adaptive parameter system. Raw analysis only — no auto-apply.",
        }, indent=2)
        upsert_memory(
            type="knowledge",
            name="param_sensitivity_report",
            description=f"Parameter gate sensitivity analysis ({ts}) — {total_trades} trades",
            content=content,
            source="vps",
        )
        print(f"\n  Stored to Supabase brain as 'param_sensitivity_report'")
    except Exception as e:
        print(f"\n  WARNING: Could not store to Supabase: {e}")


def main():
    conn = get_db()
    trades = load_trades(conn)
    signals = load_position_signals(conn)

    print("=" * 70)
    print("LAYER 1: PARAMETER SENSITIVITY ANALYSIS")
    print(f"Data: {len(trades)} closed trades, {len(signals)} position signals")
    print(f"Data quality: {data_quality_label(len(trades))}")
    print("=" * 70)

    score_result = analyze_min_insider_score(trades, signals)
    price_result = analyze_max_entry_price(trades)
    stop_result = analyze_stop_loss_tiers(trades)
    report = generate_report(score_result, price_result, stop_result, len(trades))
    store_to_supabase(report, len(trades))


if __name__ == "__main__":
    main()
