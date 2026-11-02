#!/usr/bin/env python3
"""
Memecoin feature -> outcome scan (read-only) over `memecoin_signal_features`.

Two modes:

  --bands   Per-band hit-rate (hit20 / hit50) + avg forward gain + N, for each feature
            (mcap_band, size_band, is_first_buy, consensus_60m, network_id). This is the
            "which computable signal-time feature separates outcomes" view. Every cell
            prints its N; cells below --min-n are flagged as low-confidence.

  --rank    Score recent still-open signals by their band-implied hit20 probability, using
            a TRANSPARENT empirical lookup (each feature's matured base rate, combined as a
            likelihood-ratio adjustment on the global base rate — no black-box model). Lists
            the signals the collected data currently favors. This is a lens, not a trigger.

Build the store first:  python3 scripts/research/memecoin_feature_store.py --db <db>

Usage:
    python3 scripts/research/memecoin_feature_scan.py --db <db> --bands
    python3 scripts/research/memecoin_feature_scan.py --db <db> --rank --limit 20
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import sqlite3

FEATURES = [
    ("mcap_band", ["<300k", "300k-1M", "1M-5M", "5M-30M", "30M+", "unknown"]),
    ("size_band", ["<1k", "1k-5k", "5k-20k", "20k+", "unknown"]),
    ("is_first_buy", [1, 0]),
    ("network_id", None),   # enumerate from data
]


def _base_rate(conn: sqlite3.Connection) -> tuple[float, int]:
    r = conn.execute(
        "SELECT AVG(hit20)*1.0 br, COUNT(*) n FROM memecoin_signal_features WHERE hit20 IS NOT NULL"
    ).fetchone()
    return (r[0] or 0.0, r[1] or 0)


def _consensus_bucket_sql() -> str:
    return ("CASE WHEN consensus_60m=0 THEN '0 solo' WHEN consensus_60m=1 THEN '1' "
            "WHEN consensus_60m<=3 THEN '2-3' ELSE '4+' END")


def run_bands(conn: sqlite3.Connection, min_n: int) -> None:
    br, n = _base_rate(conn)
    span = conn.execute("SELECT MIN(buy_ts), MAX(buy_ts) FROM memecoin_signal_features").fetchone()
    print(f"feature-store span {str(span[0])[:10]} -> {str(span[1])[:10]} | "
          f"labeled base rate hit20 = {br*100:.1f}% (n={n})\n")

    def emit(title: str, group_sql: str, order=None):
        print(f"--- {title} ---")
        rows = conn.execute(
            f"""SELECT {group_sql} band, COUNT(*) n, ROUND(AVG(hit20)*100,1) hit20,
                       ROUND(AVG(hit50)*100,1) hit50, ROUND(AVG(max_gain_pct),1) avg_gain
                  FROM memecoin_signal_features WHERE hit20 IS NOT NULL
                 GROUP BY band"""
        ).fetchall()
        rows = [r for r in rows if r["band"] is not None]
        if order:
            rows.sort(key=lambda r: order.index(r["band"]) if r["band"] in order else 99)
        else:
            rows.sort(key=lambda r: -(r["hit20"] or 0))
        for r in rows:
            flag = "  ⚠low-n" if r["n"] < min_n else ""
            lift = (r["hit20"] / (br * 100) if br else 0)
            print(f"   {str(r['band']):10} n={r['n']:4} hit20={str(r['hit20'])+'%':>7} "
                  f"hit50={str(r['hit50'])+'%':>7} avg_gain={str(r['avg_gain'])+'%':>8} "
                  f"lift={lift:.2f}x{flag}")
        print()

    for feat, order in FEATURES:
        emit(feat, feat, order if isinstance(order, list) else None)
    emit("consensus_60m", _consensus_bucket_sql(), ["0 solo", "1", "2-3", "4+"])


def run_mint_grain(conn: sqlite3.Connection, min_n: int) -> None:
    """PRIMARY honest view (2026-07-13 audit): dedup to one outcome per MINT (did ANY signal on the
    token hit20) so a pumping coin re-signalled by many wallets counts once. Pooled per-signal rates
    over-count correlated outcomes of the same price path (pseudo-replication)."""
    base = conn.execute(
        """SELECT ROUND(AVG(hit)*100,1) br, COUNT(*) mints FROM
             (SELECT mint, MAX(hit20) hit FROM memecoin_signal_features
               WHERE hit20 IS NOT NULL AND label_status='matured' GROUP BY mint)"""
    ).fetchone()
    print(f"MINT-GRAIN (matured, 1 outcome/mint) base hit20 = {base['br']}% (n={base['mints']} mints)\n")
    for col, order in [("mcap_band", ["<300k", "300k-1M", "1M-5M", "5M-30M", "30M+"]),
                       ("size_band", ["<1k", "1k-5k", "5k-20k", "20k+"]),
                       ("is_first_buy", [1, 0])]:
        print(f"--- {col} (mint grain) ---")
        rows = conn.execute(
            f"""SELECT band, COUNT(*) mints, ROUND(AVG(hit)*100,1) hit20 FROM
                  (SELECT {col} band, mint, MAX(hit20) hit FROM memecoin_signal_features
                    WHERE hit20 IS NOT NULL AND label_status='matured' GROUP BY {col}, mint)
                 GROUP BY band"""
        ).fetchall()
        rows = [r for r in rows if r["band"] is not None]
        rows.sort(key=lambda r: order.index(r["band"]) if r["band"] in order else 99)
        for r in rows:
            flag = "  ⚠low-n" if r["mints"] < min_n else ""
            print(f"   {str(r['band']):10} mints={r['mints']:4} hit20={str(r['hit20'])+'%':>7}{flag}")
        print()
    print("  Caveat: 6-day window, exploratory multi-feature scan (no Holm correction, no no-signal\n"
          "  control). Only mcap 300k-1M (elevated) + 30M+ (suppressed) survive as trustworthy legs.\n")


def run_rank(conn: sqlite3.Connection, limit: int, min_n: int) -> None:
    """Score still-open signals by band-implied hit20 probability using empirical
    likelihood ratios (transparent, no model). p = base * LR(mcap) * LR(size) * LR(firstbuy),
    each LR = cell_rate / base_rate computed only on cells with n >= min_n (else LR=1)."""
    br, _ = _base_rate(conn)
    if br <= 0:
        print("no labeled data; cannot rank.")
        return

    def lr_table(col: str) -> dict:
        out = {}
        for r in conn.execute(
            f"SELECT {col} k, AVG(hit20) rate, COUNT(*) n FROM memecoin_signal_features "
            f"WHERE hit20 IS NOT NULL GROUP BY {col}"
        ):
            if r["k"] is not None and r["n"] >= min_n and r["rate"] is not None:
                out[r["k"]] = r["rate"] / br
        return out

    lr_mcap, lr_size, lr_fb = lr_table("mcap_band"), lr_table("size_band"), lr_table("is_first_buy")

    open_rows = conn.execute(
        """SELECT handle, ticker, network_id, mcap_band, size_band, is_first_buy,
                  consensus_60m, buy_ts, their_market_cap
             FROM memecoin_signal_features
            WHERE label_status != 'matured' AND their_market_cap IS NOT NULL
            ORDER BY buy_ts DESC LIMIT 400"""
    ).fetchall()

    scored = []
    for r in open_rows:
        p = br * lr_mcap.get(r["mcap_band"], 1.0) * lr_size.get(r["size_band"], 1.0) \
            * lr_fb.get(r["is_first_buy"], 1.0)
        scored.append((min(p, 0.99), r))
    scored.sort(key=lambda x: -x[0])

    print(f"base hit20 rate {br*100:.1f}%.  Top {limit} still-open signals by band-implied probability")
    print(f"{'p(hit20)':>9}  {'wallet':16}{'ticker':10}{'net':10}{'mcap_band':10}{'size':8}{'fb':3}{'cons':5} buy_ts")
    for p, r in scored[:limit]:
        print(f"{p*100:8.1f}%  @{str(r['handle'])[:15]:15}{str(r['ticker'])[:9]:10}"
              f"{str(r['network_id']):10}{r['mcap_band']:10}{str(r['size_band']):8}"
              f"{str(r['is_first_buy']):3}{str(r['consensus_60m']):5} {str(r['buy_ts'])[:19]}")
    print("\n  Transparent lens, NOT a trigger. Low-n bands fall back to LR=1 (no adjustment).")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--bands", action="store_true", help="per-signal band rates (row-level; secondary)")
    ap.add_argument("--mint-grain", action="store_true", help="PRIMARY dedup-to-mint band rates")
    ap.add_argument("--rank", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--min-n", type=int, default=30, help="cells below this N are low-confidence")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    if not (args.bands or args.rank or args.mint_grain):
        args.mint_grain = True  # default to the honest primary view
    if args.mint_grain:
        run_mint_grain(conn, args.min_n)
    if args.bands:
        run_bands(conn, args.min_n)
    if args.rank:
        run_rank(conn, args.limit, args.min_n)



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    main()
