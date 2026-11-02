#!/usr/bin/env python3
"""Step 2 Solana COST PROBE for the setup study (Alpha 2026-09-30: 20 decisions only; the full
pull waits for the operator because the research key ran dry on 09-27).

For 20 funded Solana closes with a feed signal event (non-manual, spread evenly by position id), it
pulls the pool's full transactions over [t - 1 h, t) with Helius getTransactionsForAddress and counts
calls, transactions and credits. Helius bills that method at 10 credits per 100 full transactions
returned, rounded up, 10 minimum (helius.dev/docs/billing/credits, read 2026-09-30).

Research key only (slot_lane_send_monitor.pick_research_url: the first MEMECOIN_RESEARCH_HELIUS_RPC_URLS
entry, refused when it shares a key with a funded or watcher endpoint). FLAT BOOK: before every call
it refuses to run while any real Solana position is open other than a tracking-paused (Long Hold) one.
Stops on the first 429. Reads the ledger read-only; writes only --out. No outcome column is read.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE_LIMIT, MAX_PAGES, SPACING_S = 100, 60, 0.25


class Stop(Exception):
    pass


def flat(ledger):
    n = ledger.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' AND is_open=1 "
                       "AND network_id='solana' AND tracking_paused_at IS NULL").fetchone()[0]
    if n:
        raise Stop(f"book not flat: {n} open real Solana position(s)")


def call(url, ledger, method, params):
    flat(ledger)
    req = urllib.request.Request(url, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                                  "params": params}).encode(),
                                 {"Content-Type": "application/json", "User-Agent": "sniper-research/1"})
    try:
        out = json.loads(urllib.request.urlopen(req, timeout=45).read())
    except urllib.error.HTTPError as exc:
        raise Stop(f"HTTP {exc.code} on {method}")
    time.sleep(SPACING_S)
    if out.get("error"):
        raise Stop(f"RPC error on {method}: {json.dumps(out['error'])[:200]}")
    return out.get("result") or {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkout", default=str(ROOT))
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    checkout = Path(a.checkout)
    sys.path.insert(0, str(checkout))
    sys.path.insert(0, str(checkout / "scripts" / "research"))
    import os
    from sniper.util.envfiles import load_env_files
    load_env_files(checkout)
    from slot_lane_send_monitor import pick_research_url
    url = pick_research_url(os.environ)
    ledger = sqlite3.connect(f"file:{checkout / 'sniper_tracker.db'}?mode=ro", uri=True, timeout=30)
    tape = sqlite3.connect(f"file:{checkout / 'data' / 'memecoin_sol_tape.db'}?mode=ro", uri=True, timeout=30)
    pools = {pid: pool for pid, pool in tape.execute("SELECT position_id, pool FROM sol_position_pools")}
    rows = ledger.execute(
        "SELECT p.id, p.mint, e.created_at FROM memecoin_positions p "
        "JOIN memecoin_events e ON e.fomo_event_id = p.origin_event_id "
        "WHERE p.execution_mode='real' AND p.is_open=0 AND p.realized_pnl IS NOT NULL "
        "AND p.network_id='solana' AND COALESCE(p.signal_source,'') != 'manual' ORDER BY p.id").fetchall()
    step = max(1, len(rows) // a.n)
    sample = rows[::step][:a.n]
    results, stopped = [], None
    try:
        for pid, mint, created in sample:
            t = int(datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp())
            address = pools.get(pid) or mint
            calls = txs = credits = 0
            token, complete = None, False
            for _ in range(MAX_PAGES):
                opts = {"transactionDetails": "full", "sortOrder": "asc", "limit": PAGE_LIMIT, "encoding": "json",
                        "maxSupportedTransactionVersion": 1,
                        "filters": {"status": "succeeded", "blockTime": {"gte": t - 3600, "lte": t - 1}}}
                if token:
                    opts["paginationToken"] = token
                res = call(url, ledger, "getTransactionsForAddress", [address, opts])
                got = len(res.get("data") or [])
                calls += 1; txs += got; credits += max(10, 10 * math.ceil(got / 100))
                token = res.get("paginationToken")
                if not token or got < PAGE_LIMIT:
                    complete = True
                    break
            results.append({"position_id": pid, "address_kind": "pool" if pid in pools else "mint",
                            "calls": calls, "txs": txs, "credits": credits, "complete": complete})
            print(results[-1], flush=True)
    except Stop as exc:
        stopped = str(exc)
        print("STOPPED:", stopped, flush=True)
    done = [r for r in results if r["complete"]]
    per = sorted(r["credits"] for r in results)
    summary = {"generated_at": datetime.now(timezone.utc).isoformat(), "sampled": len(sample),
               "probed": len(results), "complete": len(done), "stopped": stopped,
               "calls": sum(r["calls"] for r in results), "txs": sum(r["txs"] for r in results),
               "credits": sum(per), "credits_per_decision_mean": (sum(per) / len(per)) if per else None,
               "credits_per_decision_median": per[len(per) // 2] if per else None,
               "credits_per_decision_max": per[-1] if per else None,
               "population": len(rows),
               "extrapolated_credits_mean": round(len(rows) * sum(per) / len(per)) if per else None,
               "cut_at_max_pages": sum(1 for r in results if not r["complete"]), "rows": results}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
