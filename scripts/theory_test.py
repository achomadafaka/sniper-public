#!/usr/bin/env python3
"""Test Theory A (whale distress -> direction) and Theory B (OI regime -> direction)."""
import sqlite3
import os

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sniper_tracker.db")
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

# ============================================================
# THEORY A: Whale liquidation proximity -> BTC direction
# ============================================================
print("=" * 70)
print("THEORY A: Whale distress signals and BTC direction")
print("=" * 70)

rows = c.execute("""
    SELECT wallet, asset, side, distance_pct, prev_distance_pct,
           mark_price, margin_type, leverage, captured_at
    FROM hl_whale_positions
    WHERE margin_type = 'isolated' AND asset = 'BTC'
    AND prev_distance_pct IS NOT NULL
    ORDER BY captured_at
""").fetchall()
print(f"Isolated BTC whale snapshots with prev_distance: {len(rows)}")

correct_5 = 0
correct_30 = 0
total_5 = 0
total_30 = 0
details = []

for r in rows:
    dist = r['distance_pct']
    prev = r['prev_distance_pct']
    side = r['side']
    mark = r['mark_price']
    at = r['captured_at']

    if dist >= prev:
        continue  # only care about worsening

    next_5 = c.execute("""
        SELECT mark_price FROM hyperliquid_snapshots
        WHERE asset = 'BTC' AND funding_rate != 0
        AND captured_at > datetime(?, '+4 minutes')
        AND captured_at < datetime(?, '+10 minutes')
        ORDER BY captured_at LIMIT 1
    """, (at, at)).fetchone()

    next_30 = c.execute("""
        SELECT mark_price FROM hyperliquid_snapshots
        WHERE asset = 'BTC' AND funding_rate != 0
        AND captured_at > datetime(?, '+25 minutes')
        AND captured_at < datetime(?, '+35 minutes')
        ORDER BY captured_at LIMIT 1
    """, (at, at)).fetchone()

    if next_5:
        ret_5 = (next_5[0] - mark) / mark * 100
        # whale long worsening = BTC dropping, counter = short = expect continued drop
        # whale short worsening = BTC rising, counter = long = expect continued rise
        if side == 'long':
            correct_5 += 1 if ret_5 < 0 else 0  # expect further drop
        else:
            correct_5 += 1 if ret_5 > 0 else 0  # expect further rise
        total_5 += 1

    if next_30:
        ret_30 = (next_30[0] - mark) / mark * 100
        if side == 'long':
            correct_30 += 1 if ret_30 < 0 else 0
        else:
            correct_30 += 1 if ret_30 > 0 else 0
        total_30 += 1

        details.append({
            'side': side, 'dist': dist, 'prev': prev,
            'ret_5': ret_5 if next_5 else None,
            'ret_30': ret_30,
            'correct': (ret_30 < 0) if side == 'long' else (ret_30 > 0),
        })

print(f"\nWorsening events: {len(details)}")
if total_5 > 0:
    print(f"  5-min directional accuracy: {correct_5}/{total_5} ({correct_5/total_5*100:.1f}%)")
if total_30 > 0:
    print(f"  30-min directional accuracy: {correct_30}/{total_30} ({correct_30/total_30*100:.1f}%)")

# Show sample
print("\nSample worsening events:")
for d in details[:10]:
    tag = "HIT" if d['correct'] else "MISS"
    print(f"  whale={d['side']} dist={d['dist']:.1f}%<-{d['prev']:.1f}% "
          f"5m={d['ret_5']:.4f}% 30m={d['ret_30']:.4f}% [{tag}]")

# By distance bucket
print("\nBy distance bucket:")
for lo, hi, label in [(0, 5, "<5%"), (5, 15, "5-15%"), (15, 25, "15-25%")]:
    bucket = [d for d in details if lo <= d['dist'] < hi]
    if bucket:
        correct = sum(1 for d in bucket if d['correct'])
        print(f"  {label}: {correct}/{len(bucket)} ({correct/len(bucket)*100:.0f}%) directional accuracy")

# ============================================================
# THEORY B: OI regime at multiple horizons
# ============================================================
print()
print("=" * 70)
print("THEORY B: OI regime -> BTC direction at 1h/4h/12h/24h")
print("=" * 70)

for hours in [1, 4, 12, 24]:
    rows = c.execute(f"""
        WITH snapshots AS (
            SELECT id, mark_price, funding_rate, open_interest, captured_at,
                LAG(open_interest) OVER (ORDER BY id) as prev_oi,
                LAG(mark_price) OVER (ORDER BY id) as prev_mark
            FROM hyperliquid_snapshots
            WHERE asset = 'BTC' AND funding_rate != 0
            ORDER BY id
        ),
        with_forward AS (
            SELECT s.*,
                (SELECT s2.mark_price FROM hyperliquid_snapshots s2
                 WHERE s2.asset = 'BTC' AND s2.funding_rate != 0
                 AND s2.captured_at > datetime(s.captured_at, '+{hours} hours')
                 ORDER BY s2.captured_at LIMIT 1) as future_mark
            FROM snapshots s
            WHERE s.prev_oi IS NOT NULL AND s.prev_oi > 0
        )
        SELECT
            CASE
                WHEN (open_interest - prev_oi) / prev_oi > 0.001 AND funding_rate > 0
                    THEN 'oi_up+longs_pay'
                WHEN (open_interest - prev_oi) / prev_oi > 0.001 AND funding_rate < 0
                    THEN 'oi_up+shorts_pay'
                WHEN (open_interest - prev_oi) / prev_oi < -0.001
                    AND (mark_price - prev_mark) / prev_mark < -0.0001
                    THEN 'capitulation'
                ELSE 'other'
            END as regime,
            COUNT(*) as n,
            ROUND(AVG(CASE WHEN future_mark > mark_price THEN 1.0 ELSE 0.0 END) * 100, 1) as pct_up,
            ROUND(AVG((future_mark - mark_price) / mark_price * 100), 4) as avg_ret
        FROM with_forward
        WHERE future_mark IS NOT NULL
        GROUP BY regime
        HAVING n >= 3
        ORDER BY regime
    """).fetchall()

    print(f"\n{hours}h forward:")
    print(f"  {'Regime':>22s} {'N':>5s} {'%Up':>6s} {'AvgRet':>10s}")
    print(f"  " + "-" * 46)
    for r in rows:
        print(f"  {r[0]:>22s} {r[1]:>5d} {r[2]:>6.1f} {r[3]:>10.4f}%")

# ============================================================
# Cross-reference: HL OI at time of PM trade entry -> outcome
# ============================================================
print()
print("=" * 70)
print("CROSS-VENUE: OI regime at PM entry -> trade outcome")
print("=" * 70)

rows = c.execute("""
    SELECT sp.position_id, sp.entry_price, sp.avg_exit_price, sp.realized_pnl,
        sp.opened_ts, mm.title,
        (SELECT s.funding_rate FROM hyperliquid_snapshots s
         WHERE s.asset = 'BTC' AND s.funding_rate != 0
         AND s.captured_at <= datetime(sp.opened_ts, 'unixepoch')
         ORDER BY s.captured_at DESC LIMIT 1) as entry_funding,
        (SELECT s.open_interest FROM hyperliquid_snapshots s
         WHERE s.asset = 'BTC' AND s.open_interest > 0
         AND s.captured_at <= datetime(sp.opened_ts, 'unixepoch')
         ORDER BY s.captured_at DESC LIMIT 1) as entry_oi,
        (SELECT s.open_interest FROM hyperliquid_snapshots s
         WHERE s.asset = 'BTC' AND s.open_interest > 0
         AND s.captured_at <= datetime(sp.opened_ts - 3600, 'unixepoch')
         ORDER BY s.captured_at DESC LIMIT 1) as oi_1h_ago
    FROM strategy_positions sp
    LEFT JOIN market_metadata mm ON mm.condition_id = sp.condition_id
    WHERE sp.strategy_id = 16 AND sp.state = 'closed'
    AND (sp.closed_ts - sp.opened_ts) >= 60
""").fetchall()

w_oi_up = 0
w_oi_dn = 0
l_oi_up = 0
l_oi_dn = 0
matched = 0

for r in rows:
    pnl = r['realized_pnl'] or 0
    oi = r['entry_oi']
    oi_prev = r['oi_1h_ago']
    if not oi or not oi_prev or oi_prev == 0:
        continue
    matched += 1
    oi_rising = (oi - oi_prev) / oi_prev > 0.001
    if pnl > 0:
        if oi_rising:
            w_oi_up += 1
        else:
            w_oi_dn += 1
    else:
        if oi_rising:
            l_oi_up += 1
        else:
            l_oi_dn += 1

print(f"Matched trades: {matched}/{len(rows)}")
print(f"\n  OI Rising:       W={w_oi_up} L={l_oi_up}", end="")
if (w_oi_up + l_oi_up) > 0:
    print(f"  WR={w_oi_up/(w_oi_up+l_oi_up)*100:.0f}%")
else:
    print()
print(f"  OI Flat/Falling: W={w_oi_dn} L={l_oi_dn}", end="")
if (w_oi_dn + l_oi_dn) > 0:
    print(f"  WR={w_oi_dn/(w_oi_dn+l_oi_dn)*100:.0f}%")
else:
    print()

# ============================================================
# Bidirectional: what does NEGATIVE momentum look like?
# ============================================================
print()
print("=" * 70)
print("BIDIRECTIONAL: BTC down momentum -> buy NO tokens")
print("=" * 70)

# Check: do PM markets have NO-side liquidity?
rows = c.execute("""
    SELECT condition_id, title, volume_24h
    FROM market_metadata
    WHERE LOWER(title) LIKE '%bitcoin%' AND is_open = 1
    ORDER BY volume_24h DESC LIMIT 10
""").fetchall()
print("Top active BTC markets (NO side would be the inverse bet):")
for r in rows:
    print(f"  vol=${r[2] or 0:>12,.0f}  {r[1][:65]}")

print("\nNote: On PM binary markets, buying YES at $0.30 = buying NO at $0.70")
print("For bearish plays: buy YES on 'dip to' markets, or buy NO on 'above' markets")
print("NO at $0.40 = risk $0.40 to win $0.60 = 1.5:1 payoff if BTC drops below strike")
