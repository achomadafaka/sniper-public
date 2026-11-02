#!/usr/bin/env python3
"""HL Signal Research v2 — corrected methodology.

Fixes from GPT review:
1. Per-asset regressions only (no pooled HAC on panel data)
2. Test 2 headline is test-only non-overlapping (not full sample)
3. Block bootstrap for conditional thesis test
4. Threshold sensitivity grid
5. PM EV quarantined as illustrative

Run on VPS: python3 scripts/hl_research_v2.py
"""
import os
import sqlite3
import sys
import math
import random
import argparse
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, os.getenv("DB_PATH", "sniper_tracker.db"))

# Train/test split timestamp (Jan 30 2026 00:00 UTC)
TRAIN_END_MS = int(datetime(2026, 1, 30, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
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


def invert_matrix(M):
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


def newey_west_se(residuals, X, lag=None):
    n = len(residuals)
    k = len(X[0])
    if lag is None:
        lag = int(math.floor(4 * (n / 100) ** (2 / 9)))

    XtX = [[sum(X[i][j] * X[i][l] for i in range(n)) for l in range(k)] for j in range(k)]
    XtX_inv = invert_matrix(XtX)
    if XtX_inv is None:
        return [float('inf')] * k

    S = [[0.0] * k for _ in range(k)]
    for j_lag in range(-lag, lag + 1):
        w = 1.0 - abs(j_lag) / (lag + 1)
        if w <= 0:
            continue
        for t in range(max(0, j_lag), min(n, n + j_lag)):
            s = t - j_lag
            if 0 <= s < n:
                for a in range(k):
                    for b in range(k):
                        S[a][b] += w * residuals[t] * residuals[s] * X[t][a] * X[s][b]

    temp = [[sum(XtX_inv[i][j] * S[j][l] for j in range(k)) for l in range(k)] for i in range(k)]
    V = [[sum(temp[i][j] * XtX_inv[j][l] for j in range(k)) for l in range(k)] for i in range(k)]
    return [max(0, V[i][i]) ** 0.5 for i in range(k)]


def ols_nw(y, X_features):
    n = len(y)
    k = len(X_features[0]) + 1
    X = [[1.0] + row for row in X_features]
    XtX = [[sum(X[i][j] * X[i][l] for i in range(n)) for l in range(k)] for j in range(k)]
    Xty = [sum(X[i][j] * y[i] for i in range(n)) for j in range(k)]
    XtX_inv = invert_matrix(XtX)
    if XtX_inv is None:
        return None
    beta = [sum(XtX_inv[j][l] * Xty[l] for l in range(k)) for j in range(k)]
    residuals = [y[i] - sum(beta[j] * X[i][j] for j in range(k)) for i in range(n)]
    y_mean = sum(y) / n
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)
    ss_res = sum(r ** 2 for r in residuals)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    nw_se = newey_west_se(residuals, X)
    t_stats = [beta[j] / nw_se[j] if nw_se[j] > 1e-15 else 0.0 for j in range(k)]

    def ncdf(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    p_values = [2 * (1 - ncdf(abs(t))) for t in t_stats]
    return {"n": n, "beta": beta, "se": nw_se, "t_stat": t_stats, "p_value": p_values, "r_squared": r2}


def thin_non_overlapping(rows, horizon_ms):
    result = []
    last_ts = -horizon_ms
    for r in rows:
        if r["ts"] >= last_ts + horizon_ms:
            result.append(r)
            last_ts = r["ts"]
    return result


def block_bootstrap(values, block_length, n_bootstrap=5000, stat_fn=None):
    """Block bootstrap for serially correlated data.

    Returns list of bootstrap statistics.
    """
    if stat_fn is None:
        stat_fn = lambda v: sum(v) / len(v)

    n = len(values)
    n_blocks = max(1, n // block_length)
    stats = []

    for _ in range(n_bootstrap):
        sample = []
        for _ in range(n_blocks):
            start = random.randint(0, n - block_length)
            sample.extend(values[start:start + block_length])
        sample = sample[:n]
        if sample:
            stats.append(stat_fn(sample))

    return stats


def load_data(conn):
    require_oi_contract(conn)
    rows = conn.execute("""
        WITH funding AS (
            SELECT asset, timestamp_ms, funding_rate * 8 as funding_8h
            FROM hl_funding_history
        ),
        prices AS (
            SELECT asset, timestamp_ms, close,
                   LAG(close, 4) OVER (PARTITION BY asset ORDER BY timestamp_ms) as close_4h_ago,
                   LEAD(close, 24) OVER (PARTITION BY asset ORDER BY timestamp_ms) as close_24h_fwd
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
            p.asset, p.timestamp_ms as ts, p.close as price,
            CASE WHEN p.close_4h_ago > 0 THEN (p.close - p.close_4h_ago) / p.close_4h_ago ELSE NULL END as momentum_4h,
            f.funding_8h,
            CASE WHEN o.prev_oi > 0 THEN (o.open_interest - o.prev_oi) / o.prev_oi ELSE NULL END as oi_change,
            CASE WHEN p.close > 0 AND p.close_24h_fwd IS NOT NULL THEN (p.close_24h_fwd - p.close) / p.close ELSE NULL END as ret_24h
        FROM prices p
        LEFT JOIN funding f ON f.asset = p.asset
            AND f.timestamp_ms = (SELECT MAX(f2.timestamp_ms) FROM hl_funding_history f2 WHERE f2.asset = p.asset AND f2.timestamp_ms <= p.timestamp_ms)
        LEFT JOIN oi o ON o.asset = p.asset
            AND o.timestamp_ms = (SELECT MAX(o2.timestamp_ms) FROM oi o2 WHERE o2.asset = p.asset AND o2.timestamp_ms <= p.timestamp_ms - 86400000)
        WHERE p.close > 0
        ORDER BY p.asset, p.timestamp_ms
    """, (OI_REQUIRED_UNITS,)).fetchall()

    data = []
    for r in rows:
        if r["momentum_4h"] is not None and r["funding_8h"] is not None and r["ret_24h"] is not None:
            data.append(dict(r))
    return data


def main():
    parser = argparse.ArgumentParser(description="Run corrected HL research v2")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Path to research SQLite database")
    args = parser.parse_args()

    random.seed(42)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    data = load_data(conn)
    has_oi = [d for d in data if d["oi_change"] is not None]

    print("=" * 80)
    print("HL SIGNAL RESEARCH v2 — CORRECTED METHODOLOGY")
    print("=" * 80)
    print(f"Total obs: {len(data)}, with OI: {len(has_oi)}")

    # Split
    train_oi = [d for d in has_oi if d["ts"] < TRAIN_END_MS]
    test_oi = [d for d in has_oi if d["ts"] >= TRAIN_END_MS]
    print(f"Train: {len(train_oi)}, Test: {len(test_oi)}")

    # ================================================================
    # SECTION 1: PER-ASSET REGRESSIONS (no pooled HAC)
    # ================================================================
    print("\n" + "=" * 80)
    print("SECTION 1: PER-ASSET REGRESSIONS (Newey-West, time-ordered)")
    print("ret_24h% = a + b*oi_change% + c*funding_bps + d*momentum%")
    print("=" * 80)

    for asset in ["BTC", "ETH", "SOL"]:
        subset = sorted([d for d in has_oi if d["asset"] == asset], key=lambda d: d["ts"])
        if len(subset) < 50:
            continue
        y = [d["ret_24h"] * 100 for d in subset]
        X = [[d["oi_change"] * 100, d["funding_8h"] * 10000, d["momentum_4h"] * 100] for d in subset]
        result = ols_nw(y, X)
        if not result:
            continue
        names = ["intercept", "oi_change_%", "funding_bps", "momentum_%"]
        print(f"\n{asset} (N={result['n']}, R2={result['r_squared']:.6f}):")
        print(f"  {'Var':>16s} {'Coef':>10s} {'NW-SE':>10s} {'t':>7s} {'p':>7s}")
        for i, nm in enumerate(names):
            sig = "***" if result["p_value"][i] < 0.01 else "**" if result["p_value"][i] < 0.05 else "*" if result["p_value"][i] < 0.1 else ""
            print(f"  {nm:>16s} {result['beta'][i]:>10.6f} {result['se'][i]:>10.6f} {result['t_stat'][i]:>7.3f} {result['p_value'][i]:>7.4f} {sig}")

    # ================================================================
    # SECTION 2: CONDITIONAL THESIS — PRIMARY: TEST-ONLY NON-OVERLAP
    # ================================================================
    print("\n" + "=" * 80)
    print("SECTION 2: CONDITIONAL THESIS TEST")
    print("H0: When OI rising AND momentum up, mean 24h return = 0")
    print("PRIMARY RESULT: test-only, non-overlapping 24h windows")
    print("=" * 80)

    for oi_thresh, mom_thresh in [(0.005, 0.005)]:  # main thresholds
        print(f"\nThresholds: OI > {oi_thresh*100:.1f}%, Momentum > {mom_thresh*100:.1f}%")

        for label, subset in [("ALL (test-only)", test_oi),
                               ("BTC (test-only)", [d for d in test_oi if d["asset"] == "BTC"]),
                               ("ETH (test-only)", [d for d in test_oi if d["asset"] == "ETH"]),
                               ("SOL (test-only)", [d for d in test_oi if d["asset"] == "SOL"])]:
            thesis = sorted(
                [d for d in subset if d["oi_change"] > oi_thresh and d["momentum_4h"] > mom_thresh],
                key=lambda d: d["ts"],
            )
            # Non-overlapping 24h windows
            thesis_no = thin_non_overlapping(thesis, 24 * 3600 * 1000)

            if len(thesis_no) < 5:
                print(f"  {label}: {len(thesis_no)} non-overlap obs (insufficient)")
                continue

            rets = [d["ret_24h"] * 100 for d in thesis_no]
            n = len(rets)
            mean_r = sum(rets) / n
            std_r = (sum((r - mean_r) ** 2 for r in rets) / max(n - 1, 1)) ** 0.5
            t_stat = mean_r / (std_r / n ** 0.5) if std_r > 0 else 0

            def ncdf(x):
                return 0.5 * (1 + math.erf(x / math.sqrt(2)))

            p_one = ncdf(t_stat)  # one-sided P(mean < 0)
            bearish_n = sum(1 for r in rets if r < 0)
            bearish_pct = bearish_n / n * 100

            # Binomial
            def binom_p(k, n, p=0.5):
                total = 0.0
                for i in range(k, n + 1):
                    lp = math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * math.log(p) + (n - i) * math.log(1 - p)
                    total += math.exp(lp)
                return min(total, 1.0)

            p_binom = binom_p(bearish_n, n, 0.5)

            print(f"\n  {label} (N={n} non-overlapping):")
            print(f"    Mean 24h ret: {mean_r:.4f}% (std={std_r:.4f}%)")
            print(f"    t-test (mean<0): t={t_stat:.3f}, p={p_one:.4f} {'***' if p_one < 0.01 else '**' if p_one < 0.05 else '*' if p_one < 0.1 else 'ns'}")
            print(f"    Directional: {bearish_n}/{n} bearish ({bearish_pct:.1f}%)")
            print(f"    Binomial p: {p_binom:.4f} {'***' if p_binom < 0.01 else '**' if p_binom < 0.05 else '*' if p_binom < 0.1 else 'ns'}")

    # ================================================================
    # SECTION 3: BLOCK BOOTSTRAP for conditional thesis
    # ================================================================
    print("\n" + "=" * 80)
    print("SECTION 3: BLOCK BOOTSTRAP (block=24, 5000 resamples)")
    print("Tests whether the conditional mean return is significantly < 0")
    print("=" * 80)

    for label, subset in [("ALL test", test_oi),
                           ("BTC test", [d for d in test_oi if d["asset"] == "BTC"]),
                           ("SOL test", [d for d in test_oi if d["asset"] == "SOL"])]:
        thesis = sorted(
            [d for d in subset if d["oi_change"] > 0.005 and d["momentum_4h"] > 0.005],
            key=lambda d: d["ts"],
        )
        if len(thesis) < 24:
            print(f"\n  {label}: {len(thesis)} obs (need >= 24 for block bootstrap)")
            continue

        rets = [d["ret_24h"] * 100 for d in thesis]
        observed_mean = sum(rets) / len(rets)

        # Block bootstrap: resample under H0 (centered at 0)
        centered = [r - observed_mean for r in rets]
        boot_means = block_bootstrap(centered, block_length=24, n_bootstrap=5000)

        # p-value: fraction of bootstrap means <= observed_mean
        p_boot = sum(1 for bm in boot_means if bm <= observed_mean) / len(boot_means)

        # Confidence interval
        boot_means_orig = block_bootstrap(rets, block_length=24, n_bootstrap=5000)
        boot_sorted = sorted(boot_means_orig)
        ci_lo = boot_sorted[int(0.025 * len(boot_sorted))]
        ci_hi = boot_sorted[int(0.975 * len(boot_sorted))]

        print(f"\n  {label} (N={len(thesis)} overlapping):")
        print(f"    Observed mean: {observed_mean:.4f}%")
        print(f"    Bootstrap p (mean<0): {p_boot:.4f} {'***' if p_boot < 0.01 else '**' if p_boot < 0.05 else '*' if p_boot < 0.1 else 'ns'}")
        print(f"    95% CI: [{ci_lo:.4f}%, {ci_hi:.4f}%]")
        print(f"    CI excludes zero: {'YES' if ci_hi < 0 else 'NO'}")

    # ================================================================
    # SECTION 4: THRESHOLD SENSITIVITY GRID
    # ================================================================
    print("\n" + "=" * 80)
    print("SECTION 4: THRESHOLD SENSITIVITY (test-only non-overlap)")
    print("=" * 80)

    print(f"\n  {'OI_thr':>8s} {'Mom_thr':>8s} {'N':>5s} {'Mean%':>8s} {'Bear%':>6s} {'t':>7s} {'p_one':>7s}")
    print(f"  " + "-" * 55)

    for oi_t in [0.0025, 0.005, 0.0075, 0.01]:
        for mom_t in [0.0025, 0.005, 0.0075, 0.01]:
            thesis = sorted(
                [d for d in test_oi if d["oi_change"] > oi_t and d["momentum_4h"] > mom_t],
                key=lambda d: d["ts"],
            )
            thesis_no = thin_non_overlapping(thesis, 24 * 3600 * 1000)
            if len(thesis_no) < 10:
                continue
            rets = [d["ret_24h"] * 100 for d in thesis_no]
            n = len(rets)
            mean_r = sum(rets) / n
            std_r = (sum((r - mean_r) ** 2 for r in rets) / max(n - 1, 1)) ** 0.5
            t = mean_r / (std_r / n ** 0.5) if std_r > 0 else 0
            p = 0.5 * (1 + math.erf(t / math.sqrt(2)))
            bear = sum(1 for r in rets if r < 0) / n * 100
            sig = "***" if p < 0.01 else "**" if p < 0.05 else "*" if p < 0.1 else ""
            print(f"  {oi_t*100:>7.2f}% {mom_t*100:>7.2f}% {n:>5d} {mean_r:>8.4f} {bear:>6.1f} {t:>7.3f} {p:>7.4f} {sig}")

    # ================================================================
    # SECTION 5: SUPPORTING — full-sample (DEMOTED, for reference only)
    # ================================================================
    print("\n" + "=" * 80)
    print("SECTION 5: SUPPORTING — Full-sample thesis (DEMOTED, not primary)")
    print("Included for completeness. Not decision-grade (thesis selected post-hoc).")
    print("=" * 80)

    for label, subset in [("ALL full", has_oi),
                           ("BTC full", [d for d in has_oi if d["asset"] == "BTC"]),
                           ("SOL full", [d for d in has_oi if d["asset"] == "SOL"])]:
        thesis = [d for d in subset if d["oi_change"] > 0.005 and d["momentum_4h"] > 0.005]
        if len(thesis) < 10:
            continue
        rets = [d["ret_24h"] * 100 for d in thesis]
        n = len(rets)
        mean_r = sum(rets) / n
        std_r = (sum((r - mean_r) ** 2 for r in rets) / max(n - 1, 1)) ** 0.5
        t = mean_r / (std_r / n ** 0.5) if std_r > 0 else 0
        bear = sum(1 for r in rets if r < 0) / n * 100
        print(f"  {label}: N={n}, mean={mean_r:.4f}%, bearish={bear:.1f}%, t={t:.3f} [SUPPORTING ONLY]")

    # ================================================================
    # SECTION 6: PM EV (QUARANTINED — illustrative only)
    # ================================================================
    print("\n" + "=" * 80)
    print("SECTION 6: PM EV ILLUSTRATION (QUARANTINED)")
    print("These numbers map directional WR to binary payoff.")
    print("This is NOT operational EV. Real EV requires strike selection,")
    print("expiry timing, and entry price discovery from actual PM markets.")
    print("=" * 80)

    # Use test-only non-overlap directional accuracy
    thesis_test_no = thin_non_overlapping(
        sorted([d for d in test_oi if d["oi_change"] > 0.005 and d["momentum_4h"] > 0.005], key=lambda d: d["ts"]),
        24 * 3600 * 1000,
    )
    if thesis_test_no:
        bear_n = sum(1 for d in thesis_test_no if d["ret_24h"] < 0)
        wr = bear_n / len(thesis_test_no)
        print(f"\n  Based on test-only non-overlap: {bear_n}/{len(thesis_test_no)} = {wr:.1%} directional accuracy")
        print(f"  {'Entry':>8s} {'Risk':>8s} {'Reward':>8s} {'Req WR':>8s} {'Illustrative EV':>16s}")
        for ep in [0.15, 0.20, 0.30, 0.40]:
            req = ep
            ev = wr * (1 - ep) - (1 - wr) * ep
            print(f"  ${ep:.2f}    ${ep:.2f}    ${1-ep:.2f}    {req:.0%}       ${ev:.4f} per trade")
        print(f"\n  WARNING: These assume perfect strike selection and ignore spread costs.")
        print(f"  Real PM EV requires backtest against actual resolved crypto markets.")

    print("\n" + "=" * 80)
    print("DONE — v2 corrected methodology")
    print("=" * 80)

    conn.close()


if __name__ == "__main__":
    main()
