#!/usr/bin/env python3
"""Cohort D Amendment 23 daily cross-check (read-only): curve-derived pool bindings (ps_grads) against captured
create_pool rows (pools). Prints agree / disagree / unchecked; any disagreement trips ps_evidence's tripwire.
usage: nice -n 19 python3 scripts/desk_curve_binding_crosscheck.py [--tape data/desk/pumpswap/tape.db]"""
import argparse
import json
import sqlite3


def crosscheck(conn):
    agree, disagree = conn.execute(
        "SELECT COALESCE(SUM(p.mint IS g.mint AND p.quote_mint IS g.quote_mint),0), "
        "COALESCE(SUM(p.mint IS NOT g.mint OR p.quote_mint IS NOT g.quote_mint),0) "
        "FROM ps_grads g JOIN pools p ON p.pool=g.pool").fetchone()
    unchecked = conn.execute("SELECT COUNT(*) FROM ps_grads g WHERE NOT EXISTS (SELECT 1 FROM pools p WHERE p.pool=g.pool)"
                             ).fetchone()[0]
    pools = [r[0] for r in conn.execute("SELECT DISTINCT g.pool FROM ps_grads g JOIN pools p ON p.pool=g.pool "
                                        "WHERE p.mint IS NOT g.mint OR p.quote_mint IS NOT g.quote_mint LIMIT 50")]
    return {"agree": agree, "disagree": disagree, "unchecked": unchecked, "tripwire": disagree > 0,
            "disagreeing_pools": pools}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tape", default="data/desk/pumpswap/tape.db")
    a = p.parse_args(argv)
    conn = sqlite3.connect("file:%s?mode=ro" % a.tape, uri=True, timeout=1.)
    try:
        print(json.dumps(crosscheck(conn)))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
