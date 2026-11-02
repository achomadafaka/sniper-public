#!/usr/bin/env python3
"""Full Tier W pass: every joinable Solana first-source-buy row -> one JSON line per coin (resumable). Research key only, one call at a time (~3 req/s).
    nice -n 19 python3 scripts/research/source_entry_window_run.py OUT.jsonl [DB=sniper_tracker.db]"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_entry_window as W  # noqa: E402


def main(out, db="/home/bigpapi/Desktop/Sniper/sniper_tracker.db"):
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=30)
    rows = conn.execute("""select mint, created_at, id from (select mint, created_at, id, their_price, their_market_cap,
        row_number() over (partition by network_id, mint order by created_at, id) rn
        from memecoin_events where side='buy' and network_id='solana') where rn=1 and their_price>0 and their_market_cap>0 order by created_at""").fetchall()
    done = set()
    if Path(out).exists():
        done = {json.loads(line)["mint"] for line in open(out) if line.strip()}
    rpc, n = W.Rpc(), 0
    with open(out, "a") as f:
        for mint, ca, eid in rows:
            if mint in done:
                continue
            tc = datetime.fromisoformat(ca.replace("Z", "+00:00")).timestamp()
            try:
                res = W.fetch_coin(rpc, mint, tc)
            except Exception as ex:                                  # keep going; the row is counted as an RPC failure, never dropped
                res = {"mint": mint, "tc": tc, "reason": "rpc_error", "error": str(ex)[:120], "swaps": []}
            res["event_id"], res["created_at"] = eid, ca
            f.write(json.dumps(res, separators=(",", ":")) + "\n")
            f.flush()
            n += 1
            if n % 100 == 0:
                print("coins", n, "calls", rpc.calls, "MB", round(rpc.bytes / 1e6), flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
