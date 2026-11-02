#!/usr/bin/env python3
"""
Layer 2: Bayesian Beta-Binomial Threshold Optimizer
====================================================
Single-parameter Bayesian analysis for PAPER_ENTRY_MIN_INSIDER_SCORE.

Uses Beta-Binomial conjugate model:
  Prior: Beta(2, 2) — weakly informative, assumes ~50% WR before any data
  Posterior: Beta(alpha + wins, beta + losses) per score bucket

Decision rule:
  Threshold = lowest score bucket where the 5th percentile of the posterior
  credible interval for WR exceeds 0.50 (95% confidence of positive edge)

Data quality tiers:
  INSUFFICIENT (<30 trades): report only, never auto-apply
  MARGINAL     (30-80 trades): report only, directional guidance
  ADEQUATE     (80+ trades): may auto-apply IF direction is consistent and
               diff > 2 points AND confirmed for 2+ consecutive runs
  STRONG       (200+ trades): high confidence auto-apply

Auto-apply conditions (ALL must be true):
  1. data_quality == 'adequate' (80+ trades)
  2. suggested_threshold differs from current by > 2 points
  3. Direction consistent for 2+ consecutive weekly runs (checked via Supabase history)

Run: python3 scripts/bayesian_threshold.py [--apply]
     --apply flag: ONLY acts if above conditions are met. Reports otherwise.
"""

import os
import sys
import json
import sqlite3
import argparse
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from scipy import stats

PRIOR_ALPHA = 2  # pseudocount wins — weakly informative
PRIOR_BETA = 2   # pseudocount losses — weakly informative

SCORE_RANGES = [(60, 65), (65, 70), (70, 75), (75, 80), (80, 85), (85, 90), (90, 101)]
CURRENT_THRESHOLD = float(os.getenv("PAPER_ENTRY_MIN_INSIDER_SCORE", "80"))
TERMINAL = "('resolution_win','resolution_loss','early_exit','source_exited','stale','manual','closed')"


def get_trades() -> list[dict]:
    db_path = os.getenv("DB_PATH", "sniper_tracker.db")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""SELECT signal_insider_score, pnl_usdc
            FROM paper_trades
            WHERE status IN {TERMINAL}
              AND duplicate_of IS NULL
              AND signal_insider_score IS NOT NULL
              AND signal_insider_score > 0"""
    ).fetchall()
    conn.close()
    return [{"score": float(r["signal_insider_score"]), "won": (r["pnl_usdc"] or 0) > 0} for r in rows]


def data_quality_label(n: int) -> str:
    if n < 30:
        return "insufficient"
    elif n < 80:
        return "marginal"
    elif n < 200:
        return "adequate"
    return "strong"


def update_score_model(trades: list[dict]) -> dict:
    """
    Fit Beta-Binomial model per score bucket.
    Returns per-bucket posteriors and suggested threshold.
    """
    buckets = {}

    for lo, hi in SCORE_RANGES:
        bucket_trades = [t for t in trades if lo <= t["score"] < hi]
        wins = sum(1 for t in bucket_trades if t["won"])
        losses = len(bucket_trades) - wins

        alpha = PRIOR_ALPHA + wins
        beta_param = PRIOR_BETA + losses

        mean_wr = alpha / (alpha + beta_param)
        # 5th percentile: we're 95% confident WR exceeds this value
        p5 = stats.beta.ppf(0.05, alpha, beta_param)
        # 95th percentile: upper bound
        p95 = stats.beta.ppf(0.95, alpha, beta_param)

        edge_confirmed = bool(p5 > 0.50)
        edge_possible = bool(mean_wr > 0.50 and len(bucket_trades) >= 3)

        buckets[f"{lo}-{hi}"] = {
            "n": len(bucket_trades),
            "wins": wins,
            "losses": losses,
            "posterior_mean_wr": round(mean_wr, 4),
            "credible_interval_95": [round(p5, 4), round(p95, 4)],
            "edge_confirmed": edge_confirmed,
            "edge_possible": edge_possible,
            "note": _bucket_note(len(bucket_trades), mean_wr, p5, p95),
        }

    # Optimal threshold: lowest score bucket where edge_confirmed = True
    confirmed_ranges = [
        int(k.split("-")[0])
        for k, v in buckets.items()
        if v["edge_confirmed"]
    ]
    suggested_threshold = min(confirmed_ranges) if confirmed_ranges else CURRENT_THRESHOLD

    # Possible threshold: lowest score bucket where edge_possible = True
    possible_ranges = [
        int(k.split("-")[0])
        for k, v in buckets.items()
        if v["edge_possible"]
    ]
    possible_threshold = min(possible_ranges) if possible_ranges else CURRENT_THRESHOLD

    n_total = len(trades)
    dq = data_quality_label(n_total)

    return {
        "buckets": buckets,
        "current_threshold": CURRENT_THRESHOLD,
        "suggested_threshold": suggested_threshold,
        "possible_threshold": possible_threshold,
        "data_quality": dq,
        "n_trades": n_total,
        "note": f"Based on {n_total} closed trades with score>0. Need 200+ for high confidence.",
        "prior": {"alpha": PRIOR_ALPHA, "beta": PRIOR_BETA, "assumption": "50% WR prior to data"},
    }


def _bucket_note(n: int, mean_wr: float, p5: float, p95: float) -> str:
    if n == 0:
        return "no data — prior only"
    if n < 5:
        return f"n={n}: prior dominates, posterior = prior + tiny data"
    if n < 15:
        return f"n={n}: MARGINAL — posterior is data+prior blended, wide CI"
    if n < 40:
        return f"n={n}: moderate — direction visible but interval wide"
    return f"n={n}: adequate — posterior meaningful"


def print_model_results(model: dict):
    print()
    print("=" * 70)
    print("LAYER 2: BAYESIAN THRESHOLD ANALYSIS")
    print(f"Data quality: {model['data_quality'].upper()} ({model['n_trades']} trades with score>0)")
    print(f"Prior: Beta({PRIOR_ALPHA}, {PRIOR_BETA}) — assumes {PRIOR_ALPHA/(PRIOR_ALPHA+PRIOR_BETA)*100:.0f}% WR before any data")
    print("=" * 70)
    print()
    print(f"  {'Bucket':<12} {'n':>4}  {'Wins':>5}  {'WR Mean':>8}  {'95% CI Lower':>13}  {'95% CI Upper':>13}  {'Edge?':<12}  Note")
    print("  " + "-" * 95)

    for key, b in model["buckets"].items():
        ci_lo, ci_hi = b["credible_interval_95"]
        edge = "CONFIRMED" if b["edge_confirmed"] else ("possible" if b["edge_possible"] else "no")
        print(
            f"  {key:<12} {b['n']:>4}  {b['wins']:>5}  "
            f"{b['posterior_mean_wr']*100:>7.1f}%  "
            f"{ci_lo*100:>12.1f}%  "
            f"{ci_hi*100:>12.1f}%  "
            f"{edge:<12}  {b['note']}"
        )

    print()
    print(f"  Current threshold:           {model['current_threshold']:.0f}")
    print(f"  Bayesian suggested:          {model['suggested_threshold']:.0f}  (95% confident edge confirmed)")
    print(f"  Bayesian possible threshold: {model['possible_threshold']:.0f}  (mean WR > 50%, n >= 3)")

    diff = model["suggested_threshold"] - model["current_threshold"]
    print()
    print("  INTERPRETATION:")
    if model["data_quality"] == "insufficient":
        print(f"  [INSUFFICIENT DATA — {model['n_trades']} trades]")
        print("  Prior dominates most buckets. Do not interpret as signal.")
        print("  Need 30+ trades per bucket for meaningful posteriors.")
    elif model["data_quality"] == "marginal":
        print(f"  [MARGINAL DATA — {model['n_trades']} trades with scores]")
        print("  Posterior is a blend of prior and limited data.")
        print("  Direction is visible but intervals are too wide for confident decisions.")
        if abs(diff) > 0:
            direction = "lower" if diff < 0 else "higher"
            print(f"  Directional signal: consider setting threshold {direction} ({model['suggested_threshold']:.0f})")
            print(f"  But CI overlaps are large — do NOT auto-apply on this data alone.")
    else:
        print(f"  [ADEQUATE DATA — {model['n_trades']} trades]")
        if abs(diff) <= 2:
            print(f"  Suggested threshold ({model['suggested_threshold']:.0f}) is within 2pts of current ({CURRENT_THRESHOLD:.0f})")
            print("  Difference is within noise band. No adjustment warranted.")
        else:
            direction = "LOWER" if diff < 0 else "HIGHER"
            print(f"  Suggested threshold is {direction} by {abs(diff):.0f} points: {CURRENT_THRESHOLD:.0f} -> {model['suggested_threshold']:.0f}")
            print("  This is a meaningful signal. Check --apply for auto-apply conditions.")

    print()
    print("  HONEST WARNINGS:")
    print("  - Beta-Binomial assumes trades are iid — they are NOT (market correlations exist)")
    print("  - Score buckets have very different sample sizes — comparison is uneven")
    print("  - Some trades entered BEFORE the 70 threshold was set — selection bias possible")
    print("  - The 'confirmed edge' standard (5th pct > 0.50) is strict but still uncertain")
    print("  - With n<20 per bucket, the prior (50% WR) dominates substantially")


def check_apply_conditions(model: dict) -> tuple[bool, str]:
    """
    Returns (should_apply: bool, reason: str).
    Auto-apply is ONLY safe when all conditions are met.
    """
    dq = model["data_quality"]
    diff = abs(model["suggested_threshold"] - model["current_threshold"])

    if dq not in ("adequate", "strong"):
        return False, f"data_quality={dq} (need 'adequate' — 80+ trades)"

    if diff <= 2:
        return False, f"suggested_threshold only differs by {diff:.0f} pts (need >2)"

    # Check Supabase for consecutive run history
    consistent, history_reason = _check_consecutive_runs(model["suggested_threshold"])
    if not consistent:
        return False, f"Not consistent for 2+ consecutive runs: {history_reason}"

    return True, "All conditions met"


def _check_consecutive_runs(suggested: float) -> tuple[bool, str]:
    """Check if the suggestion has been consistent for 2+ consecutive runs."""
    try:
        from bot.supabase_helper import get_memory
        prev = get_memory("bayesian_threshold_model", type_filter="knowledge")
        if not prev:
            return False, "No previous run found — this is the first run"

        prev_data = json.loads(prev.get("content", "{}"))
        prev_suggested = prev_data.get("suggested_threshold")

        if prev_suggested is None:
            return False, "Previous run has no suggested_threshold"

        if abs(float(prev_suggested) - suggested) > 2:
            return False, f"Previous suggestion ({prev_suggested}) differs from current ({suggested})"

        return True, f"Consistent with previous run (both suggest {suggested:.0f})"
    except Exception as e:
        return False, f"Could not check history: {e}"


def apply_threshold(new_threshold: float):
    """Update PAPER_ENTRY_MIN_INSIDER_SCORE in .env file."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_path):
        print(f"  ERROR: .env not found at {env_path}")
        return False

    with open(env_path, "r") as f:
        content = f.read()

    key = "PAPER_ENTRY_MIN_INSIDER_SCORE"
    new_line = f"{key}={int(new_threshold)}"

    if key in content:
        import re
        new_content = re.sub(rf"^{key}=.*$", new_line, content, flags=re.MULTILINE)
    else:
        new_content = content + f"\n{new_line}\n"

    with open(env_path, "w") as f:
        f.write(new_content)

    print(f"  APPLIED: {key} updated to {int(new_threshold)} in {env_path}")
    return True


def store_to_supabase(model: dict):
    try:
        from bot.supabase_helper import upsert_memory
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        content = json.dumps({
            "generated_at": ts,
            **model,
        }, indent=2)
        upsert_memory(
            type="knowledge",
            name="bayesian_threshold_model",
            description=f"Bayesian insider score threshold model ({ts}) — {model['n_trades']} trades",
            content=content,
            source="vps",
        )
        print(f"  Stored to Supabase brain as 'bayesian_threshold_model'")
    except Exception as e:
        print(f"  WARNING: Could not store to Supabase: {e}")


def main():
    parser = argparse.ArgumentParser(description="Bayesian threshold optimizer")
    parser.add_argument("--apply", action="store_true",
                        help="Apply suggested threshold to .env IF conditions are met")
    args = parser.parse_args()

    trades = get_trades()
    model = update_score_model(trades)
    print_model_results(model)

    if args.apply:
        print()
        print("  --apply flag: checking auto-apply conditions...")
        can_apply, reason = check_apply_conditions(model)
        if can_apply:
            print(f"  Conditions met: {reason}")
            old = model["current_threshold"]
            new = model["suggested_threshold"]
            applied = apply_threshold(new)
            if applied:
                print(f"  Threshold changed: {old:.0f} -> {new:.0f}")
                model["applied"] = True
                model["applied_from"] = old
                model["applied_to"] = new
        else:
            print(f"  SKIP: {reason}")
            print("  No changes made.")
            model["applied"] = False
            model["skip_reason"] = reason
    else:
        model["applied"] = False
        model["skip_reason"] = "no --apply flag"

    store_to_supabase(model)


if __name__ == "__main__":
    main()
