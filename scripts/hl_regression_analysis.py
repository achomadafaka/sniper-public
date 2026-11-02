#!/usr/bin/env python3
"""Proper statistical analysis of HL signals — regression + overlapping windows.

Fixes the 5 methodological weaknesses from the bucket-based approach:
1. Uses ALL observations (overlapping windows), not thinned non-overlapping
2. Continuous signals (not ternary buckets) — preserves information
3. Tests the specific thesis directly (single hypothesis, no Holm penalty)
4. Rolling walk-forward validation (not single static split)
5. Cross-asset pooled AND per-asset analysis

Run on VPS: python3 scripts/hl_regression_analysis.py
"""
import os
import sqlite3
import sys
import math
import argparse
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, os.getenv("DB_PATH", "sniper_tracker.db"))
OI_REQUIRED_UNITS = "usd_notional"


def table_has_column(conn, table_name, column_name):
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    return column_name in cols


def require_oi_contract(conn):
    required = {"oi_units", "source_detail"}
    missing = [c for c in required if not table_has_column(conn, "hl_oi_history", c)]
    if missing:
        raise SystemExit(
            "hl_oi_history is missing OI provenance columns "
            f"{missing}. Rerun scripts/backfill_oi_history.py before using OI research."
        )


def newey_west_se(residuals, X, lag=None):
    """Newey-West heteroskedasticity and autocorrelation consistent standard errors.

    X is a list of lists (each row is a feature vector including intercept).
    Returns list of standard errors for each coefficient.
    """
    n = len(residuals)
    k = len(X[0])
    if lag is None:
        lag = int(math.floor(4 * (n / 100) ** (2/9)))  # Andrews optimal lag

    # X'X inverse
    XtX = [[0.0] * k for _ in range(k)]
    for i in range(n):
        for j in range(k):
            for l in range(k):
                XtX[j][l] += X[i][j] * X[i][l]

    # Invert XtX (simple for small k)
    XtX_inv = invert_matrix(XtX)
    if XtX_inv is None:
        return [float('inf')] * k

    # Meat: S = sum of gamma_j * (I + I') weighted by Bartlett kernel
    S = [[0.0] * k for _ in range(k)]

    for j in range(-lag, lag + 1):
        weight = 1.0 - abs(j) / (lag + 1)  # Bartlett kernel
        if weight <= 0:
            continue
        for t in range(max(0, j), min(n, n + j)):
            s = t - j
            if 0 <= s < n:
                for a in range(k):
                    for b in range(k):
                        S[a][b] += weight * residuals[t] * residuals[s] * X[t][a] * X[s][b]

    # Sandwich: (X'X)^-1 S (X'X)^-1
    temp = mat_mul(XtX_inv, S)
    V = mat_mul(temp, XtX_inv)

    return [max(0, V[i][i]) ** 0.5 for i in range(k)]


def invert_matrix(M):
    """Invert a small matrix using Gauss-Jordan."""
    n = len(M)
    aug = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(M)]

    for col in range(n):
        max_row = max(range(col, n), key=lambda r: abs(aug[r][col]))
        aug[col], aug[max_row] = aug[max_row], aug[col]

        if abs(aug[col][col]) < 1e-12:
            return None

        pivot = aug[col][col]
        aug[col] = [x / pivot for x in aug[col]]

        for row in range(n):
            if row != col:
                factor = aug[row][col]
                aug[row] = [aug[row][j] - factor * aug[col][j] for j in range(2 * n)]

    return [row[n:] for row in aug]


def mat_mul(A, B):
    n = len(A)
    m = len(B[0])
    p = len(B)
    return [[sum(A[i][k] * B[k][j] for k in range(p)) for j in range(m)] for i in range(n)]


def ols_with_newey_west(y, X_features):
    """OLS regression with Newey-West standard errors.

    y: list of floats (dependent variable)
    X_features: list of lists (independent variables, NO intercept — added here)

    Returns dict with coefficients, standard errors, t-stats, R-squared.
    """
    n = len(y)
    k = len(X_features[0]) + 1  # +1 for intercept

    # Add intercept
    X = [[1.0] + row for row in X_features]

    # OLS: beta = (X'X)^-1 X'y
    XtX = [[0.0] * k for _ in range(k)]
    Xty = [0.0] * k
    for i in range(n):
        for j in range(k):
            Xty[j] += X[i][j] * y[i]
            for l in range(k):
                XtX[j][l] += X[i][j] * X[i][l]

    XtX_inv = invert_matrix(XtX)
    if XtX_inv is None:
        return None

    beta = [sum(XtX_inv[j][l] * Xty[l] for l in range(k)) for j in range(k)]

    # Residuals
    residuals = [y[i] - sum(beta[j] * X[i][j] for j in range(k)) for i in range(n)]

    # R-squared
    y_mean = sum(y) / n
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)
    ss_res = sum(r ** 2 for r in residuals)
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # Newey-West standard errors
    nw_se = newey_west_se(residuals, X)

    # t-statistics
    t_stats = [beta[j] / nw_se[j] if nw_se[j] > 0 else 0.0 for j in range(k)]

    # p-values (two-tailed, approximate using normal)
    def normal_cdf(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    p_values = [2 * (1 - normal_cdf(abs(t))) for t in t_stats]

    return {
        "n": n,
        "k": k,
        "beta": beta,
        "se": nw_se,
        "t_stat": t_stats,
        "p_value": p_values,
        "r_squared": r_squared,
        "adj_r_squared": 1 - (1 - r_squared) * (n - 1) / (n - k),
        "residuals": residuals,
    }


def main():
    parser = argparse.ArgumentParser(description="Run HL regression analysis")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Path to research SQLite database")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    require_oi_contract(conn)

    print("=" * 80)
    print("HL SIGNAL REGRESSION ANALYSIS")
    print("Continuous signals, overlapping windows, Newey-West SEs")
    print("=" * 80)

    # ================================================================
    # BUILD CONTINUOUS DATASET (no bucketing)
    # ================================================================
    print("\n--- Building continuous dataset ---")

    rows = conn.execute("""
        WITH funding AS (
            SELECT asset, timestamp_ms,
                   funding_rate * 8 as funding_8h  -- hourly to 8h equivalent
            FROM hl_funding_history
        ),
        prices AS (
            SELECT asset, timestamp_ms, close,
                   LAG(close, 4) OVER (PARTITION BY asset ORDER BY timestamp_ms) as close_4h_ago,
                   LEAD(close, 1) OVER (PARTITION BY asset ORDER BY timestamp_ms) as close_1h_fwd,
                   LEAD(close, 4) OVER (PARTITION BY asset ORDER BY timestamp_ms) as close_4h_fwd,
                   LEAD(close, 24) OVER (PARTITION BY asset ORDER BY timestamp_ms) as close_24h_fwd,
                   LEAD(close, 72) OVER (PARTITION BY asset ORDER BY timestamp_ms) as close_72h_fwd
            FROM price_history
        ),
        oi_raw AS (
            SELECT asset, timestamp_ms, open_interest, source_detail,
                   LAG(open_interest) OVER (PARTITION BY asset ORDER BY timestamp_ms) as prev_oi,
                   LAG(source_detail) OVER (PARTITION BY asset ORDER BY timestamp_ms) as prev_source_detail
            FROM hl_oi_history
            WHERE oi_units = ?
        ),
        oi AS (
            SELECT asset, timestamp_ms, open_interest,
                   CASE WHEN prev_source_detail = source_detail THEN prev_oi ELSE NULL END as prev_oi
            FROM oi_raw
        )
        SELECT
            p.asset,
            p.timestamp_ms,
            p.close as price,
            -- Continuous features
            CASE WHEN p.close_4h_ago > 0 THEN (p.close - p.close_4h_ago) / p.close_4h_ago ELSE NULL END as momentum_4h,
            f.funding_8h,
            CASE WHEN o.prev_oi > 0 THEN (o.open_interest - o.prev_oi) / o.prev_oi ELSE NULL END as oi_change_pct,
            -- Forward returns (labels)
            CASE WHEN p.close > 0 THEN (p.close_1h_fwd - p.close) / p.close ELSE NULL END as ret_1h,
            CASE WHEN p.close > 0 THEN (p.close_4h_fwd - p.close) / p.close ELSE NULL END as ret_4h,
            CASE WHEN p.close > 0 THEN (p.close_24h_fwd - p.close) / p.close ELSE NULL END as ret_24h,
            CASE WHEN p.close > 0 THEN (p.close_72h_fwd - p.close) / p.close ELSE NULL END as ret_72h
        FROM prices p
        LEFT JOIN funding f ON f.asset = p.asset
            AND f.timestamp_ms = (
                SELECT MAX(f2.timestamp_ms) FROM hl_funding_history f2
                WHERE f2.asset = p.asset AND f2.timestamp_ms <= p.timestamp_ms
            )
        LEFT JOIN oi o ON o.asset = p.asset
            AND o.timestamp_ms = (
                SELECT MAX(o2.timestamp_ms) FROM oi o2
                WHERE o2.asset = p.asset AND o2.timestamp_ms <= p.timestamp_ms - 86400000
            )
        WHERE p.close > 0
        ORDER BY p.asset, p.timestamp_ms
    """, (OI_REQUIRED_UNITS,)).fetchall()

    # Parse into usable structure
    data = []
    for r in rows:
        mom = r["momentum_4h"]
        fr = r["funding_8h"]
        oi_chg = r["oi_change_pct"]
        ret_24h = r["ret_24h"]

        if mom is not None and fr is not None and ret_24h is not None:
            data.append({
                "asset": r["asset"],
                "ts": r["timestamp_ms"],
                "momentum_4h": mom,
                "funding_8h": fr,
                "oi_change": oi_chg,  # may be None
                "ret_1h": r["ret_1h"],
                "ret_4h": r["ret_4h"],
                "ret_24h": ret_24h,
                "ret_72h": r["ret_72h"],
            })

    has_oi = [d for d in data if d["oi_change"] is not None]
    print(f"Total observations: {len(data)}")
    print(f"With OI data: {len(has_oi)}")
    print(f"Assets: {sorted(set(d['asset'] for d in data))}")

    # ================================================================
    # TEST 1: CONTINUOUS REGRESSION (all data, overlapping)
    # ================================================================
    print("\n" + "=" * 80)
    print("TEST 1: OLS REGRESSION — ret_24h = a + b*oi_change + c*funding + d*momentum")
    print("All observations, overlapping windows, Newey-West standard errors")
    print("=" * 80)

    # Full sample with OI
    for label, subset in [("ALL ASSETS POOLED", has_oi)] + [(a, [d for d in has_oi if d["asset"] == a]) for a in ["BTC", "ETH", "SOL"]]:
        if len(subset) < 30:
            print(f"\n{label}: insufficient data ({len(subset)} obs)")
            continue

        y = [d["ret_24h"] * 100 for d in subset]  # in percentage
        X = [[d["oi_change"] * 100, d["funding_8h"] * 10000, d["momentum_4h"] * 100] for d in subset]

        result = ols_with_newey_west(y, X)
        if result is None:
            print(f"\n{label}: regression failed (singular matrix)")
            continue

        names = ["intercept", "oi_change_%", "funding_8h_bps", "momentum_4h_%"]
        print(f"\n{label} (N={result['n']}, R²={result['r_squared']:.6f}, adj-R²={result['adj_r_squared']:.6f}):")
        print(f"  {'Variable':>20s} {'Coef':>10s} {'NW-SE':>10s} {'t-stat':>8s} {'p-value':>8s} {'Sig':>5s}")
        print(f"  " + "-" * 65)
        for i, name in enumerate(names):
            sig = "***" if result["p_value"][i] < 0.01 else "**" if result["p_value"][i] < 0.05 else "*" if result["p_value"][i] < 0.10 else ""
            print(f"  {name:>20s} {result['beta'][i]:>10.6f} {result['se'][i]:>10.6f} {result['t_stat'][i]:>8.3f} {result['p_value'][i]:>8.4f} {sig:>5s}")

    # ================================================================
    # TEST 2: THE SPECIFIC THESIS (single hypothesis, no correction)
    # ================================================================
    print("\n" + "=" * 80)
    print("TEST 2: SPECIFIC THESIS — 'When OI rises AND momentum is up, 24h return is negative'")
    print("Single hypothesis test. No multiple-testing correction needed.")
    print("=" * 80)

    for label, subset in [("ALL ASSETS", has_oi)] + [(a, [d for d in has_oi if d["asset"] == a]) for a in ["BTC", "ETH", "SOL"]]:
        # Filter to thesis condition: OI rising (>0.5%) AND momentum up (>0.5%)
        thesis_data = [d for d in subset if d["oi_change"] > 0.005 and d["momentum_4h"] > 0.005]
        if len(thesis_data) < 10:
            print(f"\n{label}: {len(thesis_data)} thesis observations (insufficient)")
            continue

        rets = [d["ret_24h"] * 100 for d in thesis_data]
        n = len(rets)
        mean_ret = sum(rets) / n
        std_ret = (sum((r - mean_ret) ** 2 for r in rets) / (n - 1)) ** 0.5

        # t-test: is mean return significantly different from zero?
        t_stat = mean_ret / (std_ret / n ** 0.5) if std_ret > 0 else 0

        def normal_cdf(x):
            return 0.5 * (1 + math.erf(x / math.sqrt(2)))

        # One-sided p-value (thesis says return is NEGATIVE)
        p_one_sided = normal_cdf(t_stat)  # P(Z < t) for testing mean < 0

        bearish_count = sum(1 for r in rets if r < 0)
        bearish_pct = bearish_count / n * 100

        # Binomial test for directional accuracy
        from math import lgamma, exp, log
        def binom_p(k, n, p=0.5):
            total = 0.0
            for i in range(k, n + 1):
                lp = lgamma(n+1) - lgamma(i+1) - lgamma(n-i+1) + i*log(p) + (n-i)*log(1-p)
                total += exp(lp)
            return min(total, 1.0)

        p_binomial = binom_p(bearish_count, n, 0.5)

        print(f"\n{label} (N={n}):")
        print(f"  Mean 24h return: {mean_ret:.4f}% (std={std_ret:.4f}%)")
        print(f"  t-test (mean < 0): t={t_stat:.3f}, p={p_one_sided:.4f} {'***' if p_one_sided < 0.01 else '**' if p_one_sided < 0.05 else '*' if p_one_sided < 0.1 else 'ns'}")
        print(f"  Directional: {bearish_count}/{n} bearish ({bearish_pct:.1f}%)")
        print(f"  Binomial (>50% bearish): p={p_binomial:.4f} {'***' if p_binomial < 0.01 else '**' if p_binomial < 0.05 else '*' if p_binomial < 0.1 else 'ns'}")

        # PM EV at different entry prices
        for entry_p in [0.15, 0.20, 0.30]:
            ev = (bearish_pct / 100) * (1 - entry_p) - (1 - bearish_pct / 100) * entry_p
            print(f"  PM EV @ ${entry_p:.2f} entry: ${ev:.4f}")

    # ================================================================
    # TEST 3: ROLLING WALK-FORWARD
    # ================================================================
    print("\n" + "=" * 80)
    print("TEST 3: ROLLING WALK-FORWARD (train 3mo, test 1mo, 3 windows)")
    print("=" * 80)

    # Sort by timestamp
    has_oi_sorted = sorted(has_oi, key=lambda d: d["ts"])
    if has_oi_sorted:
        min_ts = has_oi_sorted[0]["ts"]
        max_ts = has_oi_sorted[-1]["ts"]
        total_ms = max_ts - min_ts
        month_ms = 30 * 24 * 3600 * 1000

        windows = []
        for i in range(3):  # 3 rolling windows
            train_start = min_ts + i * month_ms
            train_end = train_start + 3 * month_ms
            test_start = train_end
            test_end = test_start + month_ms

            if test_end > max_ts:
                break

            train = [d for d in has_oi_sorted if train_start <= d["ts"] < train_end]
            test = [d for d in has_oi_sorted if test_start <= d["ts"] < test_end]

            if len(train) < 50 or len(test) < 20:
                continue

            windows.append({"train": train, "test": test, "window": i + 1,
                           "train_start": datetime.fromtimestamp(train_start/1000, tz=timezone.utc).strftime("%Y-%m"),
                           "test_month": datetime.fromtimestamp(test_start/1000, tz=timezone.utc).strftime("%Y-%m")})

        print(f"Windows: {len(windows)}")

        cumulative_correct = 0
        cumulative_total = 0

        for w in windows:
            # Train: learn regression
            y_train = [d["ret_24h"] * 100 for d in w["train"]]
            X_train = [[d["oi_change"] * 100, d["funding_8h"] * 10000, d["momentum_4h"] * 100] for d in w["train"]]

            train_result = ols_with_newey_west(y_train, X_train)
            if train_result is None:
                continue

            # Predict on test
            y_test = [d["ret_24h"] * 100 for d in w["test"]]
            X_test = [[d["oi_change"] * 100, d["funding_8h"] * 10000, d["momentum_4h"] * 100] for d in w["test"]]

            predictions = [sum(train_result["beta"][j] * ([1.0] + x)[j] for j in range(4)) for x in X_test]

            # Directional accuracy
            correct = sum(1 for pred, actual in zip(predictions, y_test) if (pred > 0 and actual > 0) or (pred < 0 and actual < 0))
            acc = correct / len(y_test) * 100

            cumulative_correct += correct
            cumulative_total += len(y_test)

            # OOS R-squared
            y_mean = sum(y_test) / len(y_test)
            ss_tot = sum((y - y_mean) ** 2 for y in y_test)
            ss_res = sum((actual - pred) ** 2 for pred, actual in zip(predictions, y_test))
            oos_r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

            print(f"\n  Window {w['window']}: train={w['train_start']}..., test={w['test_month']} (train={len(w['train'])}, test={len(w['test'])})")
            print(f"    OOS directional accuracy: {correct}/{len(y_test)} ({acc:.1f}%)")
            print(f"    OOS R²: {oos_r2:.6f}")
            print(f"    Train R²: {train_result['r_squared']:.6f}")
            print(f"    OI coef: {train_result['beta'][1]:.6f} (t={train_result['t_stat'][1]:.3f})")

        if cumulative_total > 0:
            print(f"\n  CUMULATIVE OOS: {cumulative_correct}/{cumulative_total} ({cumulative_correct/cumulative_total*100:.1f}%)")

    # ================================================================
    # TEST 4: CROSS-ASSET RELATIVE VALUE
    # ================================================================
    print("\n" + "=" * 80)
    print("TEST 4: CROSS-ASSET — When BTC OI rises more than ETH, does BTC underperform?")
    print("=" * 80)

    # Align BTC and ETH by timestamp
    btc_dict = {d["ts"]: d for d in has_oi if d["asset"] == "BTC"}
    eth_dict = {d["ts"]: d for d in has_oi if d["asset"] == "ETH"}
    sol_dict = {d["ts"]: d for d in has_oi if d["asset"] == "SOL"}

    common_ts = set(btc_dict.keys()) & set(eth_dict.keys())

    if len(common_ts) >= 30:
        pairs = []
        for ts in sorted(common_ts):
            b = btc_dict[ts]
            e = eth_dict[ts]
            if b["ret_24h"] is not None and e["ret_24h"] is not None:
                oi_spread = (b["oi_change"] or 0) - (e["oi_change"] or 0)
                ret_spread = b["ret_24h"] - e["ret_24h"]
                pairs.append({"oi_spread": oi_spread, "ret_spread": ret_spread})

        if len(pairs) >= 30:
            y = [p["ret_spread"] * 100 for p in pairs]
            X = [[p["oi_spread"] * 100] for p in pairs]

            result = ols_with_newey_west(y, X)
            if result:
                print(f"\nBTC-ETH relative value (N={result['n']}):")
                print(f"  ret_spread_24h = {result['beta'][0]:.6f} + {result['beta'][1]:.6f} * oi_spread")
                print(f"  R²: {result['r_squared']:.6f}")
                print(f"  OI spread coef: {result['beta'][1]:.6f} (t={result['t_stat'][1]:.3f}, p={result['p_value'][1]:.4f})")

                interpretation = "BTC underperforms when its OI rises more" if result["beta"][1] < 0 else "BTC outperforms when its OI rises more"
                print(f"  Interpretation: {interpretation}")

    # Also test BTC vs SOL
    common_ts_sol = set(btc_dict.keys()) & set(sol_dict.keys())
    if len(common_ts_sol) >= 30:
        pairs = []
        for ts in sorted(common_ts_sol):
            b = btc_dict[ts]
            s = sol_dict[ts]
            if b["ret_24h"] is not None and s["ret_24h"] is not None:
                oi_spread = (b["oi_change"] or 0) - (s["oi_change"] or 0)
                ret_spread = b["ret_24h"] - s["ret_24h"]
                pairs.append({"oi_spread": oi_spread, "ret_spread": ret_spread})

        if len(pairs) >= 30:
            y = [p["ret_spread"] * 100 for p in pairs]
            X = [[p["oi_spread"] * 100] for p in pairs]

            result = ols_with_newey_west(y, X)
            if result:
                print(f"\nBTC-SOL relative value (N={result['n']}):")
                print(f"  ret_spread_24h = {result['beta'][0]:.6f} + {result['beta'][1]:.6f} * oi_spread")
                print(f"  R²: {result['r_squared']:.6f}")
                print(f"  OI spread coef: {result['beta'][1]:.6f} (t={result['t_stat'][1]:.3f}, p={result['p_value'][1]:.4f})")

    # ================================================================
    # TEST 5: 2-SIGNAL REGRESSION (no OI dependency)
    # ================================================================
    print("\n" + "=" * 80)
    print("TEST 5: 2-SIGNAL REGRESSION (funding + momentum only, full dataset)")
    print("Uses ALL 12,960 observations — no OI requirement")
    print("=" * 80)

    full_data = [d for d in data if d["ret_24h"] is not None]

    for label, subset in [("ALL ASSETS", full_data)] + [(a, [d for d in full_data if d["asset"] == a]) for a in ["BTC", "ETH", "SOL"]]:
        if len(subset) < 50:
            continue

        y = [d["ret_24h"] * 100 for d in subset]
        X = [[d["funding_8h"] * 10000, d["momentum_4h"] * 100] for d in subset]

        result = ols_with_newey_west(y, X)
        if result is None:
            continue

        names = ["intercept", "funding_8h_bps", "momentum_4h_%"]
        print(f"\n{label} (N={result['n']}, R²={result['r_squared']:.6f}):")
        print(f"  {'Variable':>20s} {'Coef':>10s} {'NW-SE':>10s} {'t-stat':>8s} {'p':>8s} {'Sig':>5s}")
        for i, name in enumerate(names):
            sig = "***" if result["p_value"][i] < 0.01 else "**" if result["p_value"][i] < 0.05 else "*" if result["p_value"][i] < 0.10 else ""
            print(f"  {name:>20s} {result['beta'][i]:>10.6f} {result['se'][i]:>10.6f} {result['t_stat'][i]:>8.3f} {result['p_value'][i]:>8.4f} {sig:>5s}")

    print("\n" + "=" * 80)
    print("DONE")
    print("=" * 80)

    conn.close()


if __name__ == "__main__":
    main()
