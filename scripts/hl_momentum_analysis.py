#!/usr/bin/env python3
"""Statistical analysis of HL trade flow momentum.

Run on VPS: python3 scripts/hl_momentum_analysis.py
"""
import sqlite3
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "sniper_tracker.db")

c = sqlite3.connect(DB_PATH)
c.row_factory = sqlite3.Row


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return 0, 0
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    vx = sum((x - mx) ** 2 for x in xs) / n
    vy = sum((y - my) ** 2 for y in ys) / n
    if vx <= 0 or vy <= 0:
        return 0, 0
    r = cov / (vx ** 0.5 * vy ** 0.5)
    t = r * (n - 2) ** 0.5 / max((1 - r ** 2) ** 0.5, 1e-10)
    return r, t


# ============================================================
# 1. DATA INVENTORY
# ============================================================
print("=" * 70)
print("1. DATA INVENTORY")
print("=" * 70)

total = c.execute("SELECT COUNT(*) FROM hl_trades").fetchone()[0]
r = c.execute("SELECT MIN(captured_at), MAX(captured_at), COUNT(DISTINCT asset) FROM hl_trades").fetchone()
hours = c.execute("SELECT ROUND((MAX(time_ms) - MIN(time_ms)) / 3600000.0, 1) FROM hl_trades").fetchone()[0] or 1
print(f"Trades: {total:,}  |  Span: {hours}h  |  {r[0]} to {r[1]}")
print(f"Avg trades/hour: {total / hours:.0f}")
print()

rows = c.execute("""
    SELECT asset, COUNT(*) as n, ROUND(SUM(size * price)) as vol
    FROM hl_trades GROUP BY asset ORDER BY n DESC
""").fetchall()
print(f"{'Asset':8s} {'Trades':>8s} {'Volume USD':>14s} {'Trades/hr':>10s}")
print("-" * 44)
for r in rows:
    print(f"{r[0]:8s} {r[1]:>8,d} ${r[2]:>12,.0f} {r[1]/hours:>10.0f}")


# ============================================================
# 2. RETURN DISTRIBUTION per asset (5-min windows)
# ============================================================
print()
print("=" * 70)
print("2. RETURN DISTRIBUTIONS (5-min VWAP windows)")
print("=" * 70)

for asset in ["BTC", "ETH", "SOL", "HYPE"]:
    rets = [r[0] for r in c.execute("""
        WITH windows AS (
            SELECT (time_ms / 300000) as wid,
                   SUM(price * size) / SUM(size) as vwap,
                   COUNT(*) as n
            FROM hl_trades WHERE asset = ?
            GROUP BY wid HAVING n >= 5
        )
        SELECT (w2.vwap - w1.vwap) / w1.vwap * 100 as ret
        FROM windows w1
        JOIN windows w2 ON w2.wid = w1.wid + 1
    """, (asset,)).fetchall()]

    n = len(rets)
    if n < 5:
        print(f"\n{asset}: insufficient data ({n} windows)")
        continue

    mean_r = sum(rets) / n
    std_r = (sum((x - mean_r) ** 2 for x in rets) / (n - 1)) ** 0.5
    sorted_r = sorted(rets)
    skew = sum((x - mean_r) ** 3 for x in rets) / (n * std_r ** 3) if std_r > 0 else 0
    kurt = sum((x - mean_r) ** 4 for x in rets) / (n * std_r ** 4) - 3 if std_r > 0 else 0
    pos = sum(1 for x in rets if x > 0)

    print(f"\n{asset}: N={n}  mean={mean_r:.6f}%  std={std_r:.6f}%  skew={skew:.3f}  kurt={kurt:.3f}")
    print(f"  Up: {pos}/{n} ({100 * pos / n:.1f}%)  P5={sorted_r[int(n * 0.05)]:.5f}%  P95={sorted_r[int(n * 0.95)]:.5f}%")


# ============================================================
# 3. AUTOCORRELATION — does past return predict future return?
# ============================================================
print()
print("=" * 70)
print("3. RETURN AUTOCORRELATION (momentum vs mean reversion)")
print("=" * 70)

for asset in ["BTC", "ETH", "SOL", "HYPE"]:
    for window_ms, label in [(60000, "1min"), (300000, "5min")]:
        rets = [r[0] for r in c.execute(f"""
            WITH windows AS (
                SELECT (time_ms / {window_ms}) as wid,
                       SUM(price * size) / SUM(size) as vwap,
                       COUNT(*) as n
                FROM hl_trades WHERE asset = ?
                GROUP BY wid HAVING n >= 3
            )
            SELECT (w2.vwap - w1.vwap) / w1.vwap * 100
            FROM windows w1
            JOIN windows w2 ON w2.wid = w1.wid + 1
        """, (asset,)).fetchall()]

        if len(rets) < 10:
            continue

        print(f"\n{asset} {label} (N={len(rets)}):")
        print(f"  {'Lag':>4s} {'Corr':>10s} {'t-stat':>8s} {'Sig':>5s} {'Meaning'}")
        print(f"  " + "-" * 50)
        for lag in [1, 2, 3, 6, 12]:
            if lag >= len(rets):
                break
            r, t = pearson(rets[:-lag], rets[lag:])
            sig = "***" if abs(t) > 3 else "**" if abs(t) > 2 else "*" if abs(t) > 1.65 else ""
            meaning = "momentum" if r > 0 else "reversion"
            print(f"  {lag:>4d} {r:>10.6f} {t:>8.3f} {sig:>5s} {meaning}")


# ============================================================
# 4. FLOW IMBALANCE → NEXT RETURN (the actual thesis test)
# ============================================================
print()
print("=" * 70)
print("4. FLOW IMBALANCE → NEXT RETURN (predictive signal test)")
print("=" * 70)

for asset in ["BTC", "ETH", "SOL", "HYPE"]:
    for window_ms, label in [(300000, "5min"), (600000, "10min")]:
        rows = c.execute(f"""
            WITH windows AS (
                SELECT (time_ms / {window_ms}) as wid,
                       SUM(CASE WHEN side = 'buy' THEN size * price ELSE 0 END) as buy_vol,
                       SUM(CASE WHEN side = 'sell' THEN size * price ELSE 0 END) as sell_vol,
                       SUM(price * size) / SUM(size) as vwap,
                       COUNT(*) as n
                FROM hl_trades WHERE asset = ?
                GROUP BY wid HAVING n >= 5
            )
            SELECT
                CASE WHEN (w1.buy_vol + w1.sell_vol) > 0
                    THEN (w1.buy_vol - w1.sell_vol) / (w1.buy_vol + w1.sell_vol) * 100
                    ELSE 0 END as imbalance,
                CASE WHEN w1.vwap > 0
                    THEN (w2.vwap - w1.vwap) / w1.vwap * 100
                    ELSE 0 END as next_ret,
                w1.buy_vol + w1.sell_vol as total_vol
            FROM windows w1
            JOIN windows w2 ON w2.wid = w1.wid + 1
        """, (asset,)).fetchall()

        if len(rows) < 10:
            continue

        imbs = [r[0] for r in rows]
        rets = [r[1] for r in rows]

        # Overall correlation: imbalance vs next return
        r_val, t_val = pearson(imbs, rets)

        # Quintile analysis
        sorted_pairs = sorted(zip(imbs, rets), key=lambda x: x[0])
        q_size = len(sorted_pairs) // 5
        if q_size < 2:
            continue

        print(f"\n{asset} {label} (N={len(rows)}):")
        print(f"  Overall imbalance→return correlation: r={r_val:.6f} t={t_val:.3f} {'SIG' if abs(t_val) > 2 else 'ns'}")
        print(f"  {'Quintile':>10s} {'Imb range':>20s} {'N':>4s} {'Avg ret%':>10s} {'WR%':>6s} {'Avg |ret|%':>10s}")
        print(f"  " + "-" * 65)

        for qi in range(5):
            start = qi * q_size
            end = start + q_size if qi < 4 else len(sorted_pairs)
            chunk = sorted_pairs[start:end]
            chunk_imbs = [x[0] for x in chunk]
            chunk_rets = [x[1] for x in chunk]
            cn = len(chunk)
            avg_ret = sum(chunk_rets) / cn
            wr = sum(1 for x in chunk_rets if x > 0) / cn * 100
            avg_abs = sum(abs(x) for x in chunk_rets) / cn
            imb_lo = min(chunk_imbs)
            imb_hi = max(chunk_imbs)
            labels = ["Q1(sell)", "Q2", "Q3(neutral)", "Q4", "Q5(buy)"]
            print(f"  {labels[qi]:>10s} [{imb_lo:>7.1f}, {imb_hi:>6.1f}] {cn:>4d} {avg_ret:>10.6f} {wr:>6.1f} {avg_abs:>10.6f}")

        # Monotonicity check: is Q5 > Q1?
        q1_ret = sum(x[1] for x in sorted_pairs[:q_size]) / q_size
        q5_ret = sum(x[1] for x in sorted_pairs[-q_size:]) / q_size
        spread = q5_ret - q1_ret
        print(f"  Long-short spread (Q5-Q1): {spread:.6f}%")


# ============================================================
# 5. FUNDING RATE AS CONFIRMATORY SIGNAL
# ============================================================
print()
print("=" * 70)
print("5. FUNDING RATE AS CONFIRMING/CONFLICTING SIGNAL")
print("=" * 70)

for asset in ["BTC", "SOL"]:
    rows = c.execute("""
        WITH windows AS (
            SELECT (time_ms / 300000) as wid,
                   (time_ms / 300000) * 300 as window_ts,
                   SUM(CASE WHEN side = 'buy' THEN size * price ELSE 0 END) as buy_vol,
                   SUM(CASE WHEN side = 'sell' THEN size * price ELSE 0 END) as sell_vol,
                   SUM(price * size) / SUM(size) as vwap,
                   COUNT(*) as n
            FROM hl_trades WHERE asset = ?
            GROUP BY wid HAVING n >= 5
        ),
        with_funding AS (
            SELECT w.*,
                CASE WHEN (w.buy_vol + w.sell_vol) > 0
                    THEN (w.buy_vol - w.sell_vol) / (w.buy_vol + w.sell_vol) * 100
                    ELSE 0 END as imbalance,
                (SELECT s.funding_rate FROM hyperliquid_snapshots s
                 WHERE s.asset = ? AND s.funding_rate != 0
                 AND s.captured_at <= datetime(w.window_ts, 'unixepoch')
                 ORDER BY s.captured_at DESC LIMIT 1) as fr
            FROM windows w
        )
        SELECT
            wf1.imbalance,
            wf1.fr,
            CASE WHEN wf1.vwap > 0
                THEN (wf2.vwap - wf1.vwap) / wf1.vwap * 100 ELSE 0 END as next_ret
        FROM with_funding wf1
        JOIN windows wf2 ON wf2.wid = wf1.wid + 1
        WHERE wf1.fr IS NOT NULL
    """, (asset, asset)).fetchall()

    if len(rows) < 10:
        continue

    # Split into 4 regimes
    regimes = {}
    for r in rows:
        imb, fr, ret = r[0], r[1], r[2]
        if abs(imb) < 5:
            continue  # skip neutral flow
        flow_dir = "buy" if imb > 0 else "sell"
        fund_dir = "pay_long" if fr > 0 else "pay_short"
        key = f"{flow_dir}+{fund_dir}"
        if key not in regimes:
            regimes[key] = []
        regimes[key].append(ret)

    print(f"\n{asset} (flow > |5%| only, 5-min windows):")
    print(f"  {'Regime':>22s} {'N':>4s} {'Avg ret%':>10s} {'WR%':>6s} {'Sharpe-ish':>10s}")
    print(f"  " + "-" * 55)
    for key in sorted(regimes.keys()):
        vals = regimes[key]
        n = len(vals)
        if n < 3:
            continue
        avg = sum(vals) / n
        std = (sum((x - avg) ** 2 for x in vals) / (n - 1)) ** 0.5 if n > 1 else 1
        wr = sum(1 for x in vals if x > 0) / n * 100
        sharpe = avg / std if std > 0 else 0
        print(f"  {key:>22s} {n:>4d} {avg:>10.6f} {wr:>6.1f} {sharpe:>10.4f}")


# ============================================================
# 6. OI CHANGE AS SIGNAL
# ============================================================
print()
print("=" * 70)
print("6. OI CHANGE → NEXT RETURN")
print("=" * 70)

for asset in ["BTC", "ETH", "SOL"]:
    rows = c.execute("""
        WITH oi_pairs AS (
            SELECT
                s1.open_interest as oi_prev,
                s2.open_interest as oi_curr,
                s2.mark_price as mark,
                s1.captured_at as t1,
                s2.captured_at as t2
            FROM hyperliquid_snapshots s1
            JOIN hyperliquid_snapshots s2
                ON s2.asset = s1.asset
                AND s2.id > s1.id
                AND s2.captured_at > datetime(s1.captured_at, '+4 minutes')
                AND s2.captured_at < datetime(s1.captured_at, '+6 minutes')
            WHERE s1.asset = ? AND s1.open_interest > 0 AND s2.open_interest > 0
        ),
        with_next AS (
            SELECT
                op.*,
                (op.oi_curr - op.oi_prev) / op.oi_prev * 100 as oi_change_pct,
                (SELECT s3.mark_price FROM hyperliquid_snapshots s3
                 WHERE s3.asset = ? AND s3.captured_at > op.t2
                 ORDER BY s3.captured_at ASC LIMIT 1) as next_mark
            FROM oi_pairs op
        )
        SELECT oi_change_pct,
               CASE WHEN mark > 0 THEN (next_mark - mark) / mark * 100 ELSE 0 END as next_ret
        FROM with_next
        WHERE next_mark IS NOT NULL
    """, (asset, asset)).fetchall()

    if len(rows) < 10:
        print(f"\n{asset}: insufficient OI pairs ({len(rows)})")
        continue

    oi_changes = [r[0] for r in rows]
    next_rets = [r[1] for r in rows]
    r_val, t_val = pearson(oi_changes, next_rets)
    print(f"\n{asset} (N={len(rows)}): OI change→return corr: r={r_val:.6f} t={t_val:.3f} {'SIG' if abs(t_val) > 2 else 'ns'}")


# ============================================================
# 7. VOLUME BURST DETECTION — do large volume spikes predict?
# ============================================================
print()
print("=" * 70)
print("7. VOLUME BURST → NEXT RETURN")
print("=" * 70)

for asset in ["BTC", "SOL"]:
    rows = c.execute("""
        WITH windows AS (
            SELECT (time_ms / 300000) as wid,
                   SUM(size * price) as vol,
                   SUM(price * size) / SUM(size) as vwap,
                   SUM(CASE WHEN side = 'buy' THEN size * price ELSE 0 END) as buy_vol,
                   SUM(CASE WHEN side = 'sell' THEN size * price ELSE 0 END) as sell_vol,
                   COUNT(*) as n
            FROM hl_trades WHERE asset = ?
            GROUP BY wid HAVING n >= 5
        ),
        with_avg AS (
            SELECT w1.*,
                   (SELECT AVG(w0.vol) FROM windows w0
                    WHERE w0.wid BETWEEN w1.wid - 12 AND w1.wid - 1) as avg_vol_12
            FROM windows w1
        )
        SELECT
            CASE WHEN avg_vol_12 > 0 THEN wa.vol / avg_vol_12 ELSE 1 END as vol_ratio,
            CASE WHEN (wa.buy_vol + wa.sell_vol) > 0
                THEN (wa.buy_vol - wa.sell_vol) / (wa.buy_vol + wa.sell_vol) * 100
                ELSE 0 END as imbalance,
            CASE WHEN wa.vwap > 0
                THEN (w2.vwap - wa.vwap) / wa.vwap * 100
                ELSE 0 END as next_ret
        FROM with_avg wa
        JOIN windows w2 ON w2.wid = wa.wid + 1
        WHERE wa.avg_vol_12 IS NOT NULL AND wa.avg_vol_12 > 0
    """, (asset,)).fetchall()

    if len(rows) < 10:
        continue

    # Split by volume regime
    print(f"\n{asset} (N={len(rows)}):")
    print(f"  {'Vol regime':>20s} {'N':>4s} {'Avg ret%':>10s} {'WR%':>6s}")
    print(f"  " + "-" * 45)

    for label, lo, hi in [("< 0.5x (quiet)", 0, 0.5), ("0.5-1x (normal)", 0.5, 1.0),
                           ("1-2x (elevated)", 1.0, 2.0), ("2-3x (spike)", 2.0, 3.0),
                           ("> 3x (extreme)", 3.0, 100)]:
        chunk = [r for r in rows if lo <= r[0] < hi]
        if len(chunk) < 3:
            continue
        avg_ret = sum(r[2] for r in chunk) / len(chunk)
        wr = sum(1 for r in chunk if r[2] > 0) / len(chunk) * 100
        print(f"  {label:>20s} {len(chunk):>4d} {avg_ret:>10.6f} {wr:>6.1f}")

    # Does volume burst + directional flow predict better?
    print(f"  Volume burst (>2x) + directional flow (>|10%|):")
    for flow_label, flow_lo in [("buy_burst", 10), ("sell_burst", -100)]:
        flow_hi = 100 if flow_lo > 0 else -10
        chunk = [r for r in rows if r[0] >= 2.0 and flow_lo <= r[1] <= flow_hi]
        if len(chunk) < 2:
            continue
        avg_ret = sum(r[2] for r in chunk) / len(chunk)
        wr = sum(1 for r in chunk if (r[2] > 0) == (flow_lo > 0)) / len(chunk) * 100
        print(f"    {flow_label}: N={len(chunk)} avg_ret={avg_ret:.6f}% directional_wr={wr:.1f}%")


# ============================================================
# 8. MINIMUM DETECTABLE EDGE GIVEN FEE STRUCTURE
# ============================================================
print()
print("=" * 70)
print("8. EDGE vs FEES — BREAK-EVEN ANALYSIS")
print("=" * 70)

fee_rate = 0.00045  # 0.045% per fill
round_trip_bps = fee_rate * 2 * 10000  # entry + exit in bps
print(f"Round-trip fee: {round_trip_bps:.1f} bps ({fee_rate * 2 * 100:.3f}%)")

for asset in ["BTC", "SOL"]:
    rets = [r[0] for r in c.execute("""
        WITH windows AS (
            SELECT (time_ms / 300000) as wid,
                   SUM(price * size) / SUM(size) as vwap,
                   COUNT(*) as n
            FROM hl_trades WHERE asset = ?
            GROUP BY wid HAVING n >= 5
        )
        SELECT (w2.vwap - w1.vwap) / w1.vwap * 100
        FROM windows w1
        JOIN windows w2 ON w2.wid = w1.wid + 1
    """, (asset,)).fetchall()]

    if len(rets) < 5:
        continue

    avg_abs_ret = sum(abs(x) for x in rets) / len(rets)
    fee_pct = fee_rate * 2 * 100

    print(f"\n{asset}:")
    print(f"  Avg absolute 5-min return: {avg_abs_ret:.5f}%")
    print(f"  Round-trip fee cost:       {fee_pct:.4f}%")
    print(f"  Fee as % of avg move:      {fee_pct / avg_abs_ret * 100:.1f}%")
    print(f"  Required directional WR to break even: {50 + fee_pct / avg_abs_ret / 2 * 100:.1f}%")
    print(f"  (assuming symmetric returns and perfect entry/exit)")

print()
print("=" * 70)
print("DONE")
print("=" * 70)
