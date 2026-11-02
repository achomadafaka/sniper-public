"""The 5 minutes of trades BEFORE each funded Solana entry, read from the chain (m5 volume, buys, sells).

Part 2 of the operator's sold-into question (2026-09-28): the ledger's pre_entry_flow substage is on 1 of
123 rows, so the m5 flow traits could not be read from it. Research key only, flat-gated per call, one job.
  Window   blockTime in [opened_at - 300 s, opened_at) on the MINT (Helius getTransactionsForAddress,
           succeeded, asc, up to 10 pages). Our own entry is after the window, so it is not counted.
  Decode   dump_forensics.trade_of; SOL-quoted trades only (price scales with sol_usd), others counted.
  Output   one JSON line per position: buys_n, sells_n, buy_sol, sell_sol, trades_n, non_sol_n, complete.
    python3 scripts/research/pre_entry_flow_chain.py run --ids-from OUT_OF_sold_into_traits --out F
    python3 scripts/research/pre_entry_flow_chain.py --selftest
"""
import argparse
import contextlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
LEDGER = str(REPO / "sniper_tracker.db")


def flow(trades):
    """trades: [(side, sol)] -> summary dict."""
    b = [s for side, s in trades if side == "buy"]
    x = [s for side, s in trades if side == "sell"]
    return {"trades_n": len(trades), "buys_n": len(b), "sells_n": len(x), "buy_sol": round(sum(b), 6),
            "sell_sol": round(sum(x), 6)}


def run(args):
    import dump_forensics as dfx
    import funded_exit_truth as fet
    from venue_census import book_open
    from wallet_crowd_dump_scoreboard import gtfa
    ids = [int(x) for x in Path(args.ids).read_text().split()]
    out = Path(args.out)
    done = {json.loads(l)["id"] for l in out.read_text().splitlines()} if out.exists() else set()
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % LEDGER, uri=True, timeout=20)) as c, out.open("a") as fh:
        for pid in ids:
            if pid in done:
                continue
            if book_open(LEDGER):
                print("stopped: funded Solana book not flat")
                break
            mint, opened = c.execute("SELECT mint, opened_at FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
            t0 = int(datetime.fromisoformat(opened.replace("Z", "+00:00")).timestamp())
            txs, complete = gtfa(mint, {"blockTime": {"gte": t0 - 300, "lte": t0 - 1}}, limit=100, pages=10)
            trades, other = [], 0
            for tx in txs:
                t = dfx.trade_of(tx, mint, 1.0)
                if not t or not t.get("price"):
                    continue
                t2 = dfx.trade_of(tx, mint, 1000.0)
                if t2 and t2.get("price") and t2["price"] / t["price"] > 500:
                    trades.append((t["side"], float(t.get("sol") or 0.0)))
                else:
                    other += 1
            rec = {"id": pid, **flow(trades), "non_sol_n": other, "tx_read": len(txs), "complete": complete}
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
    print("research-key 429s %d" % fet.RATE_429[0])


def selftest():
    f = flow([("buy", 1.0), ("buy", 0.5), ("sell", 0.25)])
    assert f == {"trades_n": 3, "buys_n": 2, "sells_n": 1, "buy_sol": 1.5, "sell_sol": 0.25}
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--ids", required=True, help="file of position ids, whitespace separated")
    r.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    return run(a)


if __name__ == "__main__":
    main()
