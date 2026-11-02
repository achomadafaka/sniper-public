#!/usr/bin/env python3
"""Record the pool state our own funded 4663 entry actually filled against.

WHY. The impact gate was replayed on 2026-09-23 with a PROXY — `round_trip_pct` from the rung
ledger — because nothing stored the pool state at the moment of a fill. The pool-depth cache holds
CURRENT state only, so it cannot price a past entry, and the on-chain tape
(`data/memecoin_onchain_volume.db pool_ticks`) retains about one hour. So the true
"winners refused against losers saved" question cannot be asked backwards; it can only accrue
forward. This harness makes it accrue.

WHAT IT CAPTURES, AND WHY THE TICK BEFORE OURS. A V4 `Swap` event carries the sqrt price AFTER the
swap, so our own tick describes the pool we left behind, not the one we hit. The gate models the
impact of our clip on the pool as it stood BEFORE us, so the row records the immediately preceding
tick on the same pool as the pre-fill state, and keeps our own tick beside it for reference.

UNKNOWN IS NOT ZERO. A fill whose transaction is not in the tape — the observer was down, the pool
was never indexed, the hour of retention had passed — writes NO row rather than a row of nulls.

Read-only on the funded ledger. Writes one table in the ticks store, and only with --confirm.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

LEDGER = "file:%s/sniper_tracker.db?mode=ro" % ROOT
TICKS = "%s/data/memecoin_onchain_volume.db" % ROOT
CHAIN = "4663"

DDL = """
CREATE TABLE IF NOT EXISTS entry_fill_pool_state (
  position_id INTEGER PRIMARY KEY,
  chain_id TEXT NOT NULL,
  mint TEXT,
  pool TEXT NOT NULL,
  fill_tx TEXT NOT NULL,
  fill_block INTEGER,
  pre_liquidity TEXT,
  pre_sqrt_price_x96 TEXT,
  pre_block INTEGER,
  our_liquidity TEXT,
  our_sqrt_price_x96 TEXT,
  captured_at TEXT NOT NULL,
  source TEXT NOT NULL)
"""


def ensure(conn):
    conn.execute(DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entry_fill_pool_state_chain "
                 "ON entry_fill_pool_state(chain_id, captured_at)")
    conn.commit()


def state_for_fill(vol, fill_tx):
    """The pool state our fill hit, from the tape, or None when the tape cannot answer.

    Returns the tick BEFORE ours on the same pool as the pre-fill state. None means unknown — the
    transaction is not in the retained window, or the pool is not indexed — never a zeroed row.
    """
    if not fill_tx:
        return None
    ours = vol.execute(
        "SELECT pool, block_no, log_index, liquidity, sqrt_price_x96 FROM pool_ticks "
        "WHERE chain_id=? AND lower(tx_sig)=lower(?) AND liquidity IS NOT NULL "
        "ORDER BY log_index LIMIT 1", (CHAIN, str(fill_tx))).fetchone()
    if not ours:
        return None
    pool, block_no, log_index, liq, sqrt_p = ours
    prev = vol.execute(
        "SELECT block_no, liquidity, sqrt_price_x96 FROM pool_ticks "
        "WHERE chain_id=? AND pool=? AND liquidity IS NOT NULL "
        "AND (block_no < ? OR (block_no = ? AND log_index < ?)) "
        "ORDER BY block_no DESC, log_index DESC LIMIT 1",
        (CHAIN, pool, block_no, block_no, log_index)).fetchone()
    if not prev:
        return None                      # our swap is the first tick we hold: no pre-state to use
    return {"pool": pool, "fill_block": block_no, "our_liquidity": str(liq),
            "our_sqrt_price_x96": str(sqrt_p), "pre_block": prev[0],
            "pre_liquidity": str(prev[1]), "pre_sqrt_price_x96": str(prev[2])}


def pending_fills(led, vol, *, since):
    """Funded 4663 fills that have no captured state yet."""
    done = {r[0] for r in vol.execute("SELECT position_id FROM entry_fill_pool_state")}
    rows = led.execute(
        "SELECT p.id, p.mint, f.tx_sig FROM memecoin_positions p "
        "JOIN memecoin_fills f ON f.position_id=p.id AND f.kind='entry' "
        "WHERE p.execution_mode='real' AND p.network_id=? AND p.opened_at>=? "
        "AND f.tx_sig IS NOT NULL ORDER BY p.id", (CHAIN, since)).fetchall()
    return [r for r in rows if r[0] not in done]


def capture(led, vol, *, since, now_iso, confirm=False):
    """(planned, written, unknown). `unknown` counts fills the tape could not answer."""
    ensure(vol)
    planned, written, unknown = [], 0, 0
    for pid, mint, tx in pending_fills(led, vol, since=since):
        state = state_for_fill(vol, tx)
        if not state:
            unknown += 1
            continue
        planned.append(dict(state, position_id=pid, mint=mint, fill_tx=tx))
        if not confirm:
            continue
        vol.execute(
            "INSERT OR REPLACE INTO entry_fill_pool_state(position_id, chain_id, mint, pool, "
            "fill_tx, fill_block, pre_liquidity, pre_sqrt_price_x96, pre_block, our_liquidity, "
            "our_sqrt_price_x96, captured_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, CHAIN, mint, state["pool"], tx, state["fill_block"], state["pre_liquidity"],
             state["pre_sqrt_price_x96"], state["pre_block"], state["our_liquidity"],
             state["our_sqrt_price_x96"], str(now_iso), "pool_ticks"))
        written += 1
    if confirm:
        vol.commit()
    return planned, written, unknown


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2026-09-23", help="only fills opened at or after this")
    ap.add_argument("--confirm", action="store_true", help="write; default is a dry run")
    args = ap.parse_args(argv)

    from sniper.util.clock import real_utc_now_iso
    led = sqlite3.connect(LEDGER, uri=True)
    vol = sqlite3.connect(TICKS, timeout=30)
    try:
        planned, written, unknown = capture(led, vol, since=args.since,
                                            now_iso=real_utc_now_iso(), confirm=args.confirm)
        print("=== fill pool state (%s) ===" % ("CONFIRMED" if args.confirm else "dry run"))
        for row in planned:
            print("  pos=%-7s pool=%s block %s (pre %s) liquidity %s -> %s"
                  % (row["position_id"], str(row["pool"])[:20], row["fill_block"],
                     row["pre_block"], row["pre_liquidity"][:14], row["our_liquidity"][:14]))
        print("capturable: %d | written: %d%s | tape could not answer: %d"
              % (len(planned), written, "" if args.confirm else " (dry run)", unknown))
        if unknown:
            print("  NOTE: an unanswerable fill writes NO row. The tape retains about one hour, so "
                  "a fill older than that is unknown, not zero.")
    finally:
        led.close()
        vol.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
