#!/usr/bin/env python3
"""Ops CLI for the HL real copy leg — resolve ambiguous intents (after checking the
exchange UI / API), clear the latched day-loss breaker, inspect or rotate the deployment.

  --list                              show pending/ambiguous intents + breaker + deployment
  --settle CLOID --as filled|rejected settle a stuck intent; filled needs --px and --sz
  --clear-breaker                     re-enable entries after a day-loss latch
  --new-deployment --yes [--now-ms N] rotate deployment id + epoch (fresh arm)

Settling as 'filled' records the fill you verified on-exchange; a close-leg settle also
recomputes drift accounting. Never guesses — refuses without explicit values.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import intel_store as IS       # noqa: E402
from sniper.hyperlive import real_store as RS        # noqa: E402

DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"


def run(conn, argv) -> int:
    ap = argparse.ArgumentParser(description="HL real leg ops")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--settle", metavar="CLOID")
    ap.add_argument("--as", dest="as_state", choices=["filled", "rejected"])
    ap.add_argument("--px", type=float)
    ap.add_argument("--sz", type=float)
    ap.add_argument("--clear-breaker", action="store_true")
    ap.add_argument("--new-deployment", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--now-ms", type=int, default=None)
    args = ap.parse_args(argv)
    now_ms = args.now_ms or int(time.time() * 1000)

    if args.settle:
        row = conn.execute("SELECT * FROM hl_real_intents WHERE cloid=?",
                           (args.settle,)).fetchone()
        if not row:
            print(f"no intent with cloid {args.settle}", file=sys.stderr)
            return 1
        if not args.as_state:
            print("--settle requires --as filled|rejected", file=sys.stderr)
            return 1
        if args.as_state == "filled":
            if args.px is None or args.sz is None:
                print("--as filled requires --px and --sz (the values you verified "
                      "on-exchange)", file=sys.stderr)
                return 1
            RS.set_intent_state(conn, args.settle, "filled",
                                reason="manual_settle", now_ms=now_ms)
            RS.record_fill(conn, cloid=args.settle, copy_id=row["copy_id"],
                           leg=row["leg"], fill_px=args.px, fill_sz=args.sz,
                           filled_ts_ms=now_ms)
            if row["leg"] == "close":
                RS.refinalize_close(conn, args.settle)
        else:
            RS.set_intent_state(conn, args.settle, "rejected",
                                reason="manual_settle", now_ms=now_ms)
        print(f"settled {args.settle} as {args.as_state}")
        return 0

    if args.clear_breaker:
        RS.clear_breaker(conn)
        print("breaker cleared — entries re-enabled next cycle")
        return 0

    if args.new_deployment:
        if not args.yes:
            print("refusing: --new-deployment rotates the epoch (a fresh arm). "
                  "Re-run with --yes.", file=sys.stderr)
            return 1
        conn.execute("DELETE FROM hl_meta WHERE key IN "
                     "('hl_real_deployment_id', 'hl_real_epoch_ms')")
        conn.commit()
        dep = RS.get_or_create_deployment(conn, now_ms=now_ms)
        print(f"new deployment {dep['deployment_id']} epoch={dep['epoch_ms']}")
        return 0

    # default / --list
    dep_row = conn.execute("SELECT value FROM hl_meta WHERE key='hl_real_deployment_id'"
                           ).fetchone()
    print(f"deployment: {dep_row[0] if dep_row else '(none yet)'}  "
          f"breaker: {RS.breaker_latched(conn) or 'clear'}")
    pending = RS.intents_in(conn, ["intended", "submitted", "ambiguous"])
    print(f"pending intents: {len(pending)}")
    for it in pending:
        print(f"  {it['cloid']}  {it['state']:10s} {it['leg']:5s} {it['coin']:8s} "
              f"copy_id={it['copy_id']} reason={it['reason']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ns, rest = ap.parse_known_args()
    conn = IS.connect(ns.db_path)
    IS.ensure_schema(conn)
    try:
        return run(conn, rest)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
