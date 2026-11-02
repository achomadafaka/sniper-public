#!/usr/bin/env python3
"""Per-pool decode coverage for the Solana tape: how many decoded transactions got a PRICE.

    python3 scripts/research/solana_decode_coverage.py [--hours 24] [--min-sigs 1]

ZERO CHAIN READS BY CONSTRUCTION. It opens `data/memecoin_sol_tape.db` read-only and nothing else,
so it is safe to run with a funded Solana position open. No RPC client is imported.

READ THE TWO CENSORING LIMITS BEFORE THE NUMBERS. They are not caveats, they bound what the word
"drop rate" can mean here.

  1. **"SIGNATURES SEEN" IS NOT RECORDED ANYWHERE.** `sol_sig_cursor` carries
     vault/last_sig/updated_at/frontier_before/floor_ts and NO count; `tick_gaps` and
     `pool_features` hold 0 rows. `vault_rows` returns [] for a transaction that does not touch the
     named vault, and an empty return leaves no trace. So the only honest denominator is
     **transactions that produced at least one row**, and transactions fetched-but-empty are
     invisible. A rate computed here is therefore a CEILING on coverage: the true denominator is
     larger by an unknown amount.
  2. **THERE IS NO PER-LANE SPLIT, because the fast lane does not persist.** Only the pull lane
     calls `insert_ticks` (`memecoin_solana_tape.py:577`); `memecoin_hot_supervisor` decodes rows
     for corroboration and holds them in memory. Every row counted here is PULL-lane. A fast-lane
     coverage number cannot be produced from this store at all, and asking the store for one would
     silently answer about the pull lane.

WHAT A ROW MEANS. `vault_rows` emits `sol_sync` whenever the vault is touched (the reserve level),
and additionally `sol_swap` for opposite-sign legs, or `sol_mint`/`sol_burn` for same-sign legs. So:
  * `sol_swap` present  -> the transaction was PRICED (directional flow in both legs).
  * `sol_sync` with `amount1 IS NULL` -> **no quote vault matched at all**. This is the exact
    fingerprint of a quote-mint mismatch, the defect fixed in the pull lane on 2026-09-22 and in the
    fast lane on 2026-09-23.
  * `sol_sync` with a numeric `amount1` and no swap -> the quote vault was found and one leg was
    zero: a one-sided operation, not a failure.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TAPE = os.path.join(ROOT, "data", "memecoin_sol_tape.db")

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# The pull lane began passing the BOUND quote mint on this date (QQQ position 63509).
FIX_DAY = "2026-09-22"


def qclass(quote_mint):
    if quote_mint in (WSOL, USDC):
        return "standard"
    if quote_mint == "native-sol":
        return "native-sol"
    return "exotic" if quote_mint else "unbound"


def connect():
    if not os.path.exists(TAPE):
        return None
    return sqlite3.connect(f"file:{TAPE}?mode=ro", uri=True)


def _pct(a, b):
    return f"{100.0 * a / b:5.1f}%" if b else "    -"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--min-sigs", type=int, default=1, help="hide pools below this many decoded txs")
    args = ap.parse_args()

    c = connect()
    if c is None:
        print(f"no tape at {TAPE} — ABSENT READ, not a zero result.")
        return 1
    c.create_function("qclass", 1, qclass)
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=args.hours)).isoformat()
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print(f"# Solana decode coverage — {now}")
    print(f"window: rows observed since {since}  (--hours {args.hours:g})")
    print("source: data/memecoin_sol_tape.db, read-only, zero chain reads. PULL LANE ONLY —")
    print("        the fast lane does not persist ticks, so it cannot be measured here.")

    # Per quote class. One indexed pass; the join is pool(=coin vault) -> sol_pool_vaults.
    rows = c.execute(
        "SELECT qclass(v.quote_mint) qc, t.kind, COUNT(*) n, "
        "       SUM(CASE WHEN t.amount1 IS NULL THEN 1 ELSE 0 END) nullq, "
        "       COUNT(DISTINCT t.pool) pools "
        "  FROM pool_ticks t LEFT JOIN sol_pool_vaults v ON v.coin_vault = t.pool "
        " WHERE t.chain_id='solana' AND t.observed_at >= ? GROUP BY 1,2", (since,)).fetchall()
    by = {}
    for qc, kind, n, nullq, pools in rows:
        d = by.setdefault(qc, {"kinds": {}, "nullq": 0, "pools": 0})
        d["kinds"][kind] = n
        if kind == "sol_sync":
            d["nullq"] = nullq or 0
            d["pools"] = pools
    print(f"\n## Coverage by quote class (window {args.hours:g} h)")
    print(f"  {'quote class':<12} {'pools':>6} {'decoded tx':>11} {'priced':>8} {'priced%':>8} "
          f"{'no quote leg':>13} {'one-sided':>10}")
    for qc in sorted(by):
        d = by[qc]
        k = d["kinds"]
        sync = k.get("sol_sync", 0) + k.get("sol_curve_sync", 0)
        priced = k.get("sol_swap", 0) + k.get("sol_curve_swap", 0)
        lp = k.get("sol_mint", 0) + k.get("sol_burn", 0)
        one_sided = max(0, sync - priced - lp - d["nullq"])
        print(f"  {qc:<12} {d['pools']:>6} {sync:>11} {priced:>8} {_pct(priced, sync):>8} "
              f"{d['nullq']:>13} {one_sided:>10}")

    # The natural experiment. The pull-lane fix is a hard date boundary, so the same pools before
    # and after it measure the fix rather than the market.
    print(f"\n## The {FIX_DAY} pull-lane fix, measured on its own boundary (ALL history, not the window)")
    print("  The pull lane began passing each pool's BOUND quote mint instead of the static")
    print("  (WSOL, USDC) pair. Same pools, same decoder, one changed argument.")
    print(f"  {'quote class':<12} {'era':<11} {'sync':>8} {'no quote leg':>13} {'swaps':>7} {'pools':>6}")
    q = ("SELECT qclass(v.quote_mint) qc, "
         f"       CASE WHEN t.observed_at >= '{FIX_DAY}' THEN 'after_fix' ELSE 'before_fix' END era, "
         "       SUM(CASE WHEN t.kind='sol_sync' THEN 1 ELSE 0 END) sync, "
         "       SUM(CASE WHEN t.kind='sol_sync' AND t.amount1 IS NULL THEN 1 ELSE 0 END) nullq, "
         "       SUM(CASE WHEN t.kind='sol_swap' THEN 1 ELSE 0 END) swaps, "
         "       COUNT(DISTINCT t.pool) pools "
         "  FROM pool_ticks t JOIN sol_pool_vaults v ON v.coin_vault = t.pool "
         " WHERE t.chain_id='solana' AND t.kind IN ('sol_sync','sol_swap') GROUP BY 1,2 ORDER BY 1,2")
    for qc, era, sync, nullq, swaps, pools in c.execute(q):
        print(f"  {qc:<12} {era:<11} {sync:>8} {nullq:>6} {_pct(nullq, sync):>6} {swaps:>7} {pools:>6}")

    # Worst pools in the window, by unpriced share.
    print(f"\n## Pools with the worst priced share in the window (>= {args.min_sigs} decoded tx)")
    q2 = ("SELECT t.pool, qclass(v.quote_mint) qc, "
          "       SUM(CASE WHEN t.kind IN ('sol_sync','sol_curve_sync') THEN 1 ELSE 0 END) sync, "
          "       SUM(CASE WHEN t.kind IN ('sol_swap','sol_curve_swap') THEN 1 ELSE 0 END) swaps, "
          "       SUM(CASE WHEN t.kind='sol_sync' AND t.amount1 IS NULL THEN 1 ELSE 0 END) nullq "
          "  FROM pool_ticks t LEFT JOIN sol_pool_vaults v ON v.coin_vault = t.pool "
          " WHERE t.chain_id='solana' AND t.observed_at >= ? GROUP BY 1,2 HAVING sync >= ? "
          " ORDER BY (CAST(swaps AS REAL) / sync) ASC, sync DESC LIMIT 20")
    print(f"  {'pool (coin vault)':<46} {'quote':<11} {'sync':>6} {'swaps':>6} {'priced%':>8} {'nullq':>6}")
    for pool, qc, sync, swaps, nullq in c.execute(q2, (since, args.min_sigs)):
        print(f"  {str(pool)[:44]:<46} {qc:<11} {sync:>6} {swaps:>6} {_pct(swaps, sync):>8} {nullq:>6}")

    print("\n## What this cannot tell you")
    print("  1. The TRUE drop rate. A transaction fetched that touched no vault produced no row and")
    print("     left no trace, and nothing records how many signatures were enumerated. Every")
    print("     `priced%` here is a CEILING.")
    print("  2. Anything about the FAST lane. It does not persist ticks; these are pull-lane rows.")
    print("  3. The cost of a miss. An unpriced transaction on a held pool delayed a fast-lane exit")
    print("     into the pull lane's slower sweep; it never produced a wrong price. Quantifying that")
    print("     delay needs the exit-decision ledger, not this tape.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
