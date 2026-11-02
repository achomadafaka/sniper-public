#!/usr/bin/env python3
"""Run the HL signal combination research.

Usage: python3 scripts/run_signal_research.py [--db path]

Reads from `research_dataset` table. Enumerates all signal subsets,
learns direction on train, scores on test with Holm-Bonferroni correction.
Outputs results to stdout and optionally to Supabase brain.
"""
import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone
from itertools import combinations

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, os.getenv("DB_PATH", "sniper_tracker.db"))

MIN_N = 20  # minimum observations per cell per split
DIRECTION_THRESHOLD = 0.55  # >55% up = bullish, <45% up = bearish


def binomial_test_pvalue(k, n, p=0.5):
    """One-sided binomial test p-value (exact, using log-space for stability)."""
    if n == 0:
        return 1.0
    # P(X >= k) for X ~ Binomial(n, p)
    # Use cumulative sum of binomial PMF from k to n
    total = 0.0
    for i in range(k, n + 1):
        # log(C(n,i)) + i*log(p) + (n-i)*log(1-p)
        log_pmf = _log_comb(n, i) + i * math.log(p) + (n - i) * math.log(1 - p)
        total += math.exp(log_pmf)
    return min(total, 1.0)


def _log_comb(n, k):
    """Log of binomial coefficient C(n,k)."""
    if k < 0 or k > n:
        return float('-inf')
    if k == 0 or k == n:
        return 0.0
    # Use lgamma for numerical stability
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def holm_bonferroni(pvalues):
    """Apply Holm-Bonferroni correction. Returns list of (original_idx, corrected_p, significant)."""
    m = len(pvalues)
    if m == 0:
        return []

    indexed = sorted(enumerate(pvalues), key=lambda x: x[1])
    results = [None] * m
    max_corrected = 0.0

    for rank, (orig_idx, p) in enumerate(indexed):
        corrected = p * (m - rank)
        corrected = max(corrected, max_corrected)  # enforce monotonicity
        corrected = min(corrected, 1.0)
        max_corrected = corrected
        results[orig_idx] = (orig_idx, corrected, corrected < 0.05)

    return results


def get_active_signals(conn):
    """Determine which signals have data (S1 may be NULL if no OI)."""
    s1_count = conn.execute(
        "SELECT COUNT(*) FROM research_dataset WHERE s1_state IS NOT NULL AND split = 'train'"
    ).fetchone()[0]

    signals = []
    if s1_count >= 100:
        signals.append(("s1_state", "S1_OI"))
    signals.append(("s2_state", "S2_Funding"))
    signals.append(("s3_state", "S3_Momentum"))

    # S4 would go here if promoted
    s4_count = conn.execute(
        "SELECT COUNT(*) FROM research_dataset WHERE s4_state IS NOT NULL AND split = 'train'"
    ).fetchone()[0]
    if s4_count >= 100:
        signals.append(("s4_state", "S4_Flow"))

    return signals


def enumerate_subsets(signals):
    """Generate all non-empty subsets of signals."""
    subsets = []
    for size in range(1, len(signals) + 1):
        for combo in combinations(signals, size):
            subsets.append(list(combo))
    return subsets


def thin_to_non_overlapping(rows, horizon_hours):
    """Thin observations to non-overlapping windows for a given horizon."""
    if horizon_hours <= 1:
        return rows  # already hourly
    interval_ms = horizon_hours * 3600 * 1000
    result = []
    last_ts = 0
    for r in rows:
        if r["anchor_ts"] >= last_ts + interval_ms or last_ts == 0:
            result.append(r)
            last_ts = r["anchor_ts"]
    return result


def classify_regime(ret_1d):
    """Classify market regime from 1-day return."""
    if ret_1d is None:
        return "unknown"
    if ret_1d > 0.01:
        return "rally"
    elif ret_1d < -0.01:
        return "decline"
    else:
        return "consolidation"


def run_research(conn):
    """Main research loop."""
    signals = get_active_signals(conn)
    signal_names = [s[1] for s in signals]
    print(f"Active signals: {signal_names}")
    print(f"Signal count: {len(signals)}")

    subsets = enumerate_subsets(signals)
    print(f"Non-empty subsets: {len(subsets)}")

    horizons = [
        ("ret_1h", 1),
        ("ret_4h", 4),
        ("ret_1d", 24),
        ("ret_3d", 72),
    ]

    all_results = []
    all_pvalues = []

    for subset in subsets:
        cols = [s[0] for s in subset]
        names = [s[1] for s in subset]
        subset_label = "+".join(names)

        # Build GROUP BY query for this subset
        col_list = ", ".join(cols)
        where_not_null = " AND ".join(f"{c} IS NOT NULL" for c in cols)

        # Get all joint states for this subset
        states = conn.execute(f"""
            SELECT {col_list}, COUNT(*) as n
            FROM research_dataset
            WHERE split = 'train' AND {where_not_null}
            GROUP BY {col_list}
            HAVING n >= {MIN_N}
        """).fetchall()

        for state_row in states:
            state_values = [state_row[i] for i in range(len(cols))]
            state_label = "|".join(f"{names[i]}={state_values[i]}" for i in range(len(cols)))
            train_n = state_row[-1]  # the COUNT

            # Build WHERE clause for this exact state
            state_where = " AND ".join(f"{cols[i]} = ?" for i in range(len(cols)))

            for ret_col, horizon_h in horizons:
                # Get train data for direction learning
                train_rows = conn.execute(f"""
                    SELECT anchor_ts, {ret_col}, ret_1d
                    FROM research_dataset
                    WHERE split = 'train' AND {where_not_null}
                    AND {state_where} AND {ret_col} IS NOT NULL
                    ORDER BY anchor_ts
                """, state_values).fetchall()

                train_thinned = thin_to_non_overlapping(
                    [{"anchor_ts": r[0], "ret": r[1], "ret_1d": r[2]} for r in train_rows],
                    horizon_h,
                )

                if len(train_thinned) < MIN_N:
                    continue

                # Learn direction from train
                up_count = sum(1 for r in train_thinned if r["ret"] > 0)
                up_rate = up_count / len(train_thinned)

                if up_rate > DIRECTION_THRESHOLD:
                    direction = "bullish"
                elif up_rate < (1 - DIRECTION_THRESHOLD):
                    direction = "bearish"
                else:
                    direction = "no_signal"

                # Get test data
                test_rows = conn.execute(f"""
                    SELECT anchor_ts, {ret_col}, ret_1d
                    FROM research_dataset
                    WHERE split = 'test' AND {where_not_null}
                    AND {state_where} AND {ret_col} IS NOT NULL
                    ORDER BY anchor_ts
                """, state_values).fetchall()

                test_thinned = thin_to_non_overlapping(
                    [{"anchor_ts": r[0], "ret": r[1], "ret_1d": r[2]} for r in test_rows],
                    horizon_h,
                )

                if len(test_thinned) < MIN_N:
                    continue

                # Score on test with frozen label
                if direction == "no_signal":
                    continue

                test_rets = [r["ret"] for r in test_thinned]
                if direction == "bullish":
                    correct = sum(1 for r in test_rets if r > 0)
                else:
                    correct = sum(1 for r in test_rets if r < 0)

                test_n = len(test_thinned)
                accuracy = correct / test_n
                mean_ret = sum(test_rets) / test_n
                std_ret = (sum((r - mean_ret) ** 2 for r in test_rets) / max(test_n - 1, 1)) ** 0.5
                sharpe = mean_ret / std_ret if std_ret > 0 else 0

                # Directional return (positive = correct direction)
                if direction == "bearish":
                    dir_rets = [-r for r in test_rets]
                else:
                    dir_rets = test_rets
                dir_mean = sum(dir_rets) / test_n

                pvalue = binomial_test_pvalue(correct, test_n, 0.5)

                # PM-adjusted EV at different entry prices
                pm_ev = {}
                for entry_price in [0.20, 0.30, 0.40]:
                    win_payout = 1.0 - entry_price
                    loss_cost = entry_price
                    ev = accuracy * win_payout - (1 - accuracy) * loss_cost
                    pm_ev[f"ev_{int(entry_price*100)}"] = round(ev, 4)

                # Regime diversity
                regime_counts = {"rally": 0, "decline": 0, "consolidation": 0, "unknown": 0}
                for r in test_thinned:
                    regime = classify_regime(r.get("ret_1d"))
                    regime_counts[regime] = regime_counts.get(regime, 0) + 1
                dominant_regime_pct = max(regime_counts.values()) / test_n * 100

                result = {
                    "subset": subset_label,
                    "state": state_label,
                    "horizon": ret_col,
                    "horizon_h": horizon_h,
                    "direction": direction,
                    "train_n": len(train_thinned),
                    "train_up_rate": round(up_rate, 4),
                    "test_n": test_n,
                    "test_accuracy": round(accuracy, 4),
                    "test_correct": correct,
                    "mean_ret": round(mean_ret, 6),
                    "dir_mean_ret": round(dir_mean, 6),
                    "std_ret": round(std_ret, 6),
                    "sharpe": round(sharpe, 4),
                    "pvalue": round(pvalue, 6),
                    "dominant_regime_pct": round(dominant_regime_pct, 1),
                    **pm_ev,
                }
                all_results.append(result)
                all_pvalues.append(pvalue)

    # Apply Holm-Bonferroni correction
    corrections = holm_bonferroni(all_pvalues)
    for i, (orig_idx, corrected_p, significant) in enumerate(corrections):
        all_results[i]["corrected_p"] = round(corrected_p, 6)
        all_results[i]["significant"] = significant

    return all_results


def print_results(results):
    """Print research results."""
    print(f"\n{'='*80}")
    print(f"RESEARCH RESULTS: {len(results)} cells tested")
    print(f"{'='*80}")

    if not results:
        print("No cells survived minimum-N filter.")
        return

    # Summary
    sig_count = sum(1 for r in results if r.get("significant"))
    print(f"\nCells surviving Holm-Bonferroni (alpha=0.05): {sig_count}/{len(results)}")

    # Top 10 by OOS accuracy
    print(f"\n--- TOP 10 BY OOS ACCURACY ---")
    by_acc = sorted(results, key=lambda x: x["test_accuracy"], reverse=True)[:10]
    print(f"{'Subset':>30s} {'State':>40s} {'H':>5s} {'Dir':>8s} {'TrN':>4s} {'TeN':>4s} {'Acc':>6s} {'Ret%':>8s} {'p':>8s} {'Sig':>4s}")
    for r in by_acc:
        print(f"{r['subset']:>30s} {r['state'][:40]:>40s} {r['horizon_h']:>5d}h {r['direction']:>8s} {r['train_n']:>4d} {r['test_n']:>4d} {r['test_accuracy']:>6.1%} {r['mean_ret']*100:>8.4f} {r['corrected_p']:>8.4f} {'***' if r.get('significant') else '':>4s}")

    # Top 10 by OOS Sharpe
    print(f"\n--- TOP 10 BY OOS SHARPE ---")
    by_sharpe = sorted(results, key=lambda x: abs(x["sharpe"]), reverse=True)[:10]
    print(f"{'Subset':>30s} {'State':>40s} {'H':>5s} {'Sharpe':>8s} {'Acc':>6s} {'Ret%':>8s} {'Sig':>4s}")
    for r in by_sharpe:
        print(f"{r['subset']:>30s} {r['state'][:40]:>40s} {r['horizon_h']:>5d}h {r['sharpe']:>8.4f} {r['test_accuracy']:>6.1%} {r['mean_ret']*100:>8.4f} {'***' if r.get('significant') else '':>4s}")

    # Top 10 by PM EV at 0.20
    print(f"\n--- TOP 10 BY PM EV (entry $0.20) ---")
    by_ev = sorted(results, key=lambda x: x.get("ev_20", 0), reverse=True)[:10]
    print(f"{'Subset':>30s} {'State':>40s} {'H':>5s} {'EV@20':>8s} {'EV@30':>8s} {'Acc':>6s} {'Sig':>4s}")
    for r in by_ev:
        print(f"{r['subset']:>30s} {r['state'][:40]:>40s} {r['horizon_h']:>5d}h {r.get('ev_20',0):>8.4f} {r.get('ev_30',0):>8.4f} {r['test_accuracy']:>6.1%} {'***' if r.get('significant') else '':>4s}")

    # Significant cells detail
    sig_cells = [r for r in results if r.get("significant")]
    if sig_cells:
        print(f"\n--- SIGNIFICANT CELLS (HOLM-BONFERRONI p<0.05) ---")
        for r in sorted(sig_cells, key=lambda x: x["corrected_p"]):
            print(f"  {r['subset']} | {r['state']} | {r['horizon_h']}h")
            print(f"    direction={r['direction']} train_n={r['train_n']} test_n={r['test_n']}")
            print(f"    accuracy={r['test_accuracy']:.1%} mean_ret={r['mean_ret']*100:.4f}% sharpe={r['sharpe']:.4f}")
            print(f"    raw_p={r['pvalue']:.6f} corrected_p={r['corrected_p']:.6f}")
            print(f"    dominant_regime={r['dominant_regime_pct']:.0f}% EV@20={r.get('ev_20',0):.4f} EV@30={r.get('ev_30',0):.4f}")
            print()

    # Full results as JSON for brain output
    return results


def main():
    parser = argparse.ArgumentParser(description="Run HL signal research")
    parser.add_argument("--db", type=str, default=DB_PATH)
    parser.add_argument("--json-out", type=str, default="", help="Path to write full results JSON")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Verify dataset exists
    count = conn.execute("SELECT COUNT(*) FROM research_dataset").fetchone()[0]
    if count == 0:
        print("ERROR: research_dataset is empty. Run build_research_dataset.py first.")
        sys.exit(1)

    print(f"Research dataset: {count} rows")
    train_n = conn.execute("SELECT COUNT(*) FROM research_dataset WHERE split = 'train'").fetchone()[0]
    test_n = conn.execute("SELECT COUNT(*) FROM research_dataset WHERE split = 'test'").fetchone()[0]
    print(f"Train: {train_n}  Test: {test_n}")

    results = run_research(conn)
    print_results(results)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nFull results written to {args.json_out}")

    conn.close()


if __name__ == "__main__":
    main()
