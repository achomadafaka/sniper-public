#!/usr/bin/env python3
"""Graduated PumpSwap census for the F0 stratum (Gamma pre-reg skeleton, Alpha 2026-10-22). COUNT-ONLY:
no prices, no outcomes. Read-only on the desk universe worker's PumpSwap tape (data/desk/pumpswap/tape.db,
written only with MEMECOIN_DESK_PUMPSWAP_CAPTURE=1).

Per UTC hour of block_time (rows with no block_time are counted apart, never placed in an hour):
  pools          distinct pools with at least one swap
  fee_proven     pools whose newest 3 NON-agent swaps in the hour hold >= 2 identical (lp, protocol, creator)
                 fee triples (the desk's regular-fee rule, memecoin_desk_safety FEE_AGREE_MIN / FEE_OF_NEWEST;
                 the Mayhem agent trades fee-free and never counts); fewer or disagreeing = not proven
  mapped         pools whose swaps all carry map_status 'mapped' (a canonical pool bound to its mint);
  conflict       pools with any 'conflict' row; unmapped = the rest (any 'unmapped' or NULL row)
  mapped_mints   distinct mints among mapped pools (F0 floors count mints, not pools)
  new_pools      pools whose first swap in the WHOLE tape falls in this hour (fresh graduations), the
                 rest are already active; per hour and per UTC day
  swaps_per_pool median and p90 swaps per pool in the hour
Hourly fee agreement only sizes floors: F0 defines fee_proven at a decision cut from rows strictly before it.
Gap rows (gaps_ps) inside the window are reported: an hour with a gap undercounts.

    python3 scripts/research/pumpswap_census.py --hours 24
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sniper.services.memecoin_desk_safety import FEE_AGREE_MIN, FEE_OF_NEWEST   # noqa: E402
from sniper.services.memecoin_desk_structure import AGENTS                       # noqa: E402


def fee_proven(triples):
    """triples: (lp, protocol, creator) of NON-agent swaps, newest first. True when >= FEE_AGREE_MIN of the
    newest FEE_OF_NEWEST are identical and every value is a non-negative int below 10,000."""
    newest = list(triples[:FEE_OF_NEWEST])
    if len(newest) < FEE_AGREE_MIN or any(
            type(f) is not int or not 0 <= f < 10_000 for t in newest for f in t):
        return False
    return any(newest.count(t) >= FEE_AGREE_MIN for t in newest)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


WSOL = "So11111111111111111111111111111111111111112"


def load_pool_mints(path):
    """{pool: token mint} from pumpswap_pool_mints.py's side table, status 'read' only. The token is the
    non-WSOL side; a pool with WSOL on neither side keeps its base mint. Missing file = {}."""
    if not Path(path).exists():
        return {}
    side = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    try:
        return {pool: (quote if base == WSOL else base) for pool, base, quote in
                side.execute("SELECT pool, base_mint, quote_mint FROM pool_mints WHERE status='read'")}
    finally:
        side.close()


CHUNK_ROWS = 50_000
RECEIVED_PRE_S = 300.0        # host clock may stamp a row slightly before the chain's block_time
RECEIVED_MARGIN_S = 1800.0    # a row whose block_time is in the hour is received soon after; rows received later than this are NOT read (stated lower bound)


def _received_chunks(conn, lo, hi, step=None):
    """Yield swaps_ps rows (pool, block_time, slot, tx_index, ordinal, actor, map_status, lp, protocol, creator, mint) received in [lo, hi), in keyset pages over
    (received_at, signature, ordinal). The WHERE is an indexed received_at range (swaps_ps_received); block_time is only a filter applied by the caller INSIDE it.
    Each page is fetched whole, so no read transaction outlives it and the WAL can checkpoint between pages (2026-10-23, 40 GB cap). Operator order 2026-10-28:
    desk data accumulates, so a reader reads only a bounded window, never the whole tape."""
    step = step or CHUNK_ROWS
    key = (lo, "", -1)
    while True:
        page = conn.execute(
            "SELECT pool, block_time, slot, tx_index, ordinal, actor, map_status, lp_fee_bps, protocol_fee_bps, "
            "creator_fee_bps, mint, signature, received_at FROM swaps_ps WHERE received_at >= ? AND received_at < ? "
            "AND (received_at, signature, ordinal) > (?, ?, ?) ORDER BY received_at, signature, ordinal LIMIT ?",
            (key[0], hi, key[0], key[1], key[2], step)).fetchall()                  # the range STARTS at the cursor's received_at: starting at lo re-walked every earlier page (3.8 s vs 1.0 s per page, 2026-10-28)
        if not page:
            return
        key = (page[-1][12], page[-1][11], page[-1][4])
        yield [r[:11] for r in page]


FIRST_PROBE_ROWS = 64


def _first_block_time(conn, pool):
    """The pool's creation/earliest block_time from BOUNDED sources only, else None = UNKNOWN (never the earliest row of a clipped window, never a scan):
    (1) the captured create_pool (`pools`, index pools_pool); (2) the pool's first FIRST_PROBE_ROWS rows by slot (pool_slot index) -- an old run of NULL block_time rows
    can no longer be walked past. A pool whose earliest time is unknown is never classified new."""
    try:
        r = conn.execute("SELECT MIN(block_time) FROM pools WHERE pool = ? AND block_time IS NOT NULL", (pool,)).fetchone()
        if r and r[0] is not None:
            return r[0]
    except sqlite3.OperationalError:
        pass                                              # no pools table in this tape: fall through to the bounded probe
    r = conn.execute("SELECT MIN(block_time) FROM (SELECT block_time FROM swaps_ps WHERE pool = ? ORDER BY slot LIMIT ?)", (pool, FIRST_PROBE_ROWS)).fetchone()
    return None if r is None else r[0]


def census(conn, *, start, end, agents=AGENTS, pool_mints=None, received_pre_s=RECEIVED_PRE_S, received_margin_s=RECEIVED_MARGIN_S):
    """{'hours': [...], 'no_block_time_rows': n, 'gaps': n, 'received_window': [lo, hi]} for block_time in [start, end), read from rows RECEIVED in
    [start - received_pre_s, end + received_margin_s): a row whose block_time is in range but was received outside that window is not counted (lower bound; stated in the output).
    pool_mints ({pool: mint}, post-hoc account reads) fills posthoc_mapped/posthoc_mints for pools the
    tape left unmapped; it never changes mapped/unmapped/conflict, which stay the tape's own facts."""
    pool_mints = pool_mints or {}
    rows, seen = [], set()
    lo, hi = float(start) - received_pre_s, float(end) + received_margin_s
    for chunk in _received_chunks(conn, lo, hi):      # short read transactions: one long scan pins the WAL (2026-10-23, 40 GB cap)
        for pool, bt, *rest in chunk:
            seen.add(pool)
            if bt is None or int(start) <= bt < int(end):
                rows.append((pool, bt, *rest))
    first = {p: _first_block_time(conn, p) for p in seen}
    by_hour = defaultdict(lambda: defaultdict(list))
    no_time = 0
    for pool, bt, slot, txi, ordn, actor, status, lp, proto, creator, mint in rows:
        if bt is None:
            no_time += 1
            continue
        by_hour[int(bt) // 3600 * 3600][pool].append(
            ((slot or 0, txi if txi is not None else -1, ordn or 0), actor, status, (_int(lp), _int(proto), _int(creator)),
             mint))
    gaps = conn.execute("SELECT COUNT(*) FROM gaps_ps WHERE at >= ? AND at < ?", (start, end)).fetchone()[0]
    hours, days = [], defaultdict(lambda: {"pools": set(), "new_pools": 0})
    for hour in sorted(by_hour):
        pools = by_hour[hour]
        proven = mapped = conflict = new = posthoc = unknown_first = 0
        mints, posthoc_mints = set(), set()
        for pool, swaps in pools.items():
            swaps.sort(key=lambda s: s[0], reverse=True)
            proven += fee_proven([s[3] for s in swaps if s[1] not in agents])
            if any(s[2] == "conflict" for s in swaps):
                conflict += 1
            elif all(s[2] == "mapped" for s in swaps):
                mapped += 1
                mints.update(s[4] for s in swaps if s[4])
            elif pool in pool_mints:
                posthoc += 1
                posthoc_mints.add(pool_mints[pool])
            unknown_first += first.get(pool) is None
            is_new = first.get(pool) is not None and int(first[pool]) // 3600 * 3600 == hour
            new += is_new
            day = days[hour // 86400 * 86400]
            day["pools"].add(pool)
            day["new_pools"] += is_new
        counts = sorted(len(s) for s in pools.values())
        hours.append({"hour": hour, "pools": len(pools), "fee_proven": proven, "mapped": mapped,
                      "conflict": conflict, "unmapped": len(pools) - mapped - conflict,
                      "mapped_mints": len(mints), "posthoc_mapped": posthoc,
                      "posthoc_mints": len(posthoc_mints), "all_mints": len(mints | posthoc_mints), "new_pools": new, "new_pools_unknown": unknown_first, "swaps": sum(counts),
                      "swaps_per_pool_median": _q(counts, 0.5), "swaps_per_pool_p90": _q(counts, 0.9)})
    return {"hours": hours, "days": [{"day": d, "pools": len(v["pools"]), "new_pools": v["new_pools"]}
                                     for d, v in sorted(days.items())],
            "no_block_time_rows": no_time, "gaps": gaps, "received_window": [lo, hi]}


def _q(sorted_counts, q):
    """Nearest-rank quantile of a non-empty sorted list."""
    return sorted_counts[min(len(sorted_counts) - 1, max(0, -(-int(q * 100) * len(sorted_counts) // 100) - 1))]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--tape", default=str(REPO / "data" / "desk" / "pumpswap" / "tape.db"))
    p.add_argument("--pool-mints", default=str(REPO / "data" / "desk" / "pumpswap" / "pool_mints.db"))
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--end", type=float, default=None, help="epoch end (default now)")
    a = p.parse_args(argv)
    if not Path(a.tape).exists():
        raise SystemExit("no PumpSwap tape at %s (MEMECOIN_DESK_PUMPSWAP_CAPTURE is off?)" % a.tape)
    end = time.time() if a.end is None else a.end
    conn = sqlite3.connect("file:%s?mode=ro" % a.tape, uri=True, timeout=5)
    try:
        out = census(conn, start=end - a.hours * 3600, end=end, pool_mints=load_pool_mints(a.pool_mints))
    finally:
        conn.close()
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
