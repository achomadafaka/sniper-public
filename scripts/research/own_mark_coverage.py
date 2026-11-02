#!/usr/bin/env python3
"""Before X1/X2 go off: how often did the funded exit sweep have a price of OUR OWN? (read-only)

DexScreener out, phase A (spec 2026-09-28-memecoin-dexscreener-out-phase-a-design.md; Alpha's
activation condition). The sweep records the mark it priced each held position with in
memecoin_position_marks, one row per (chain, mint, sweep), with its `source`. A row whose source is
ours (a Solana swap, state or curve mark, or an EVM exact preview) means our own price existed and was
fresh that sweep: the lanes admit a swap mark only within 5 s (`swap_max_age_s`), so N = 5 s. A
DexScreener row means ours did not.

`own_under_our_usd` is a LOWER BOUND for X2 off: it counts only the Solana rows whose position is bound
to a WSOL, USDC or curve pool (data/memecoin_sol_tape.db). The switched-off lanes price marks with
`memecoin_live_marks.fresh_quote_usd`, which also prices other quote assets (tokenized stocks, JTO, ...)
while its Jupiter quote is under 60 s old; how often that holds is not in the ledger, so measure it with
scripts/research/live_mark_coverage.py, which samples that same source while positions are open.

DENOMINATOR: sweep rows while a funded position was open (opened_at to closed_at, or --until), per
chain. Our own held book, which is the population the switch changes.

    python3 scripts/research/own_mark_coverage.py [--since ISO] [--until ISO]
    python3 scripts/research/own_mark_coverage.py --selftest
"""
import argparse
import contextlib
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

OWN = frozenset({"fastlane_sol_swap", "fastlane_sol_state", "fastlane_sol_curve", "fastlane_rpc"})
SOL_OWN = frozenset({"fastlane_sol_swap", "fastlane_sol_state", "fastlane_sol_curve"})
DEX = frozenset({"fastlane_mid", "dexscreener"})


def classify(source, *, chain, quote_ok):
    """(own_as_is, own_under_our_usd, kind) for one sweep row."""
    if source in OWN:
        ours = not (chain == "solana" and source in SOL_OWN and not quote_ok)
        return True, ours, "own"
    return False, False, "dex" if source in DEX else "other"


def quote_ok_positions(tape):
    """position_id -> True when its bound pool is priced in WSOL, USDC or is a pump curve."""
    from sniper.services.memecoin_solana_amounts import USDC, WSOL
    if tape is None:
        return {}
    out = {}
    for pid, kind, quote in tape.execute(
            "SELECT p.position_id, p.venue_kind, v.quote_mint FROM sol_position_pools p "
            "LEFT JOIN sol_pool_vaults v ON v.pool = p.pool"):
        out[int(pid)] = kind == "pump_curve" or quote in (WSOL, USDC)
    return out


def coverage(ledger, tape, since, until):
    """{chain: {...}} over the funded positions open in [since, until]."""
    ok = quote_ok_positions(tape)
    per_chain = defaultdict(lambda: {"positions": 0, "rows": 0, "own": 0, "own_our_usd": 0,
                                     "dex": 0, "other": 0, "position_shares": []})
    positions = ledger.execute(
        "SELECT id, network_id, mint, opened_at, closed_at FROM memecoin_positions "
        "WHERE execution_mode='real' AND opened_at <= ? AND (closed_at IS NULL OR closed_at >= ?)",
        (until, since)).fetchall()
    for pid, chain, mint, opened, closed in positions:
        lo, hi = max(opened, since), min(closed or until, until)
        rows = ledger.execute(
            "SELECT source FROM memecoin_position_marks WHERE chain_id=? AND mint=? "
            "AND sweep_ts BETWEEN ? AND ?", (chain, mint, lo, hi)).fetchall()
        if not rows:
            continue
        c = per_chain[chain]
        c["positions"] += 1
        mine = 0
        for (source,) in rows:
            own, ours, kind = classify(source, chain=chain, quote_ok=ok.get(int(pid), False))
            c["rows"] += 1
            c[kind] += 1
            c["own_our_usd"] += int(ours)
            mine += int(ours)
        c["position_shares"].append(mine / len(rows))
    return dict(per_chain)


def _iso(dt):
    return dt.isoformat()


def report(args):
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_path, state_root
    load_env_files(str(state_root()))
    from sniper.persistence.db_core import get_readonly_connection
    now = datetime.now(timezone.utc)
    until = args.until or _iso(now)
    since = args.since or _iso(now - timedelta(hours=24))
    tape_path = state_path("data", "memecoin_sol_tape.db")
    with contextlib.closing(get_readonly_connection(str(state_path("sniper_tracker.db")))) as ledger, \
            contextlib.closing(sqlite3.connect("file:%s?mode=ro" % tape_path, uri=True)
                               if tape_path.exists() else None) as tape:
        out = coverage(ledger, tape, since, until)
    print("own-mark coverage of the funded exit sweep, %s .. %s (N = 5 s, the lanes' own bound)"
          % (since, until))
    for chain, c in sorted(out.items()):
        shares = sorted(c["position_shares"])
        p10 = shares[int(0.1 * (len(shares) - 1))] if shares else None
        print("%-7s positions %3d  rows %6d  own %5.1f%%  own under our USD %5.1f%%  "
              "dexscreener %5.1f%%  other %4.1f%%  per-position own p10 %s median %s" % (
                  chain, c["positions"], c["rows"], 100.0 * c["own"] / c["rows"],
                  100.0 * c["own_our_usd"] / c["rows"], 100.0 * c["dex"] / c["rows"],
                  100.0 * c["other"] / c["rows"],
                  "%.0f%%" % (100 * p10) if p10 is not None else "-",
                  "%.0f%%" % (100 * statistics.median(shares)) if shares else "-"))
    return 0


def selftest():
    ledger = sqlite3.connect(":memory:")
    ledger.executescript(
        "CREATE TABLE memecoin_positions (id INTEGER, network_id TEXT, mint TEXT, opened_at TEXT,"
        " closed_at TEXT, execution_mode TEXT);"
        "CREATE TABLE memecoin_position_marks (chain_id TEXT, mint TEXT, sweep_ts TEXT, source TEXT);")
    tape = sqlite3.connect(":memory:")
    tape.executescript(
        "CREATE TABLE sol_position_pools (position_id INTEGER, pool TEXT, venue_kind TEXT);"
        "CREATE TABLE sol_pool_vaults (pool TEXT, quote_mint TEXT);")
    from sniper.services.memecoin_solana_amounts import WSOL
    ledger.executemany("INSERT INTO memecoin_positions VALUES (?,?,?,?,?, 'real')", [
        (1, "solana", "A", "2026-09-28T00:00", "2026-09-28T00:10"),
        (2, "solana", "B", "2026-09-28T00:00", "2026-09-28T00:10"),
        (3, "56", "C", "2026-09-28T00:00", None)])
    tape.executemany("INSERT INTO sol_position_pools VALUES (?,?,?)", [(1, "pA", "amm"), (2, "pB", "amm")])
    tape.executemany("INSERT INTO sol_pool_vaults VALUES (?,?)", [("pA", WSOL), ("pB", "XsStock")])
    ledger.executemany("INSERT INTO memecoin_position_marks VALUES (?,?,?,?)", [
        ("solana", "A", "2026-09-28T00:01", "fastlane_sol_swap"),
        ("solana", "A", "2026-09-28T00:02", "fastlane_mid"),
        ("solana", "B", "2026-09-28T00:01", "fastlane_sol_swap"),
        ("solana", "A", "2026-09-28T00:20", "fastlane_sol_swap"),     # after the close: out
        ("56", "C", "2026-09-28T00:01", "fastlane_rpc"),
        ("56", "C", "2026-09-28T00:02", "dexscreener")])
    out = coverage(ledger, tape, "2026-09-28T00:00", "2026-09-28T01:00")
    s, b = out["solana"], out["56"]
    assert (s["rows"], s["own"], s["own_our_usd"], s["dex"]) == (3, 2, 1, 1), s
    assert (b["rows"], b["own"], b["own_our_usd"], b["dex"]) == (2, 1, 1, 1), b
    assert classify("geckoterminal", chain="4663", quote_ok=False) == (False, False, "other")
    print("selftest ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    return selftest() if args.selftest else report(args)


if __name__ == "__main__":
    raise SystemExit(main())
