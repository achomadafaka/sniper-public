#!/usr/bin/env python3
"""How fast does each armed Solana wallet's SOURCE exit after its own buy? (chain only, read-only)

Operator order 2026-09-27 (via Alpha, 14:24Z): "change the strategy for account that sell like
limfork and not decu to tp at +15%". For every armed Solana wallet, over its newest buys: the time
from the source's buy to the moment its cumulative outflow of that coin first reaches 30% of its
post-buy bag (sells and transfers out, from its own token account for the mint, exact), and whether
that happened within 120 s. Reference: limfork 66032, source sold 100% 75 s after our fill.

Buys and sells come from the chain through the wallet scoreboard's helpers (watcher detections
first, then the owner's own succeeded transactions). A buy with no 30% exit inside WINDOW_S is
CENSORED (held longer), never a zero. Research key only (funded_exit_truth.rpc), paced at 1 call/s,
and the run pauses while any real Solana position is open (research-read rule).

    python3 scripts/research/source_exit_speed.py run --out logs/source_exit_speed_20260927.jsonl
    python3 scripts/research/source_exit_speed.py table --out logs/source_exit_speed_20260927.jsonl
    python3 scripts/research/source_exit_speed.py --selftest
"""
import argparse
import contextlib
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
import wallet_crowd_dump_scoreboard as wsb  # noqa: E402

wsb.fet.SPACING_S = 1.0         # Alpha 14:24Z: <= 1 rps on e1f95763
BUYS, WINDOW_S, SHARE, FAST_S = 10, 1800, 0.30, 120


def first_exit(txs, owner, mint, buy_sig, bag, t_buy):
    """(seconds to first >= SHARE cumulative outflow, share out within FAST_S). None when unreached."""
    if not bag:
        return None, None
    out, hold, fast = 0, None, 0
    for tx in sorted(txs, key=lambda x: (x.get("slot") or 0, x.get("transactionIndex") or 0)):
        if tx["transaction"]["signatures"][0] == buy_sig:
            continue
        pre, post = wsb.owner_balance(tx, owner, mint)
        out += max(0, pre - post)
        t = tx.get("blockTime")
        if t is not None and t - t_buy <= FAST_S:
            fast = out
        if hold is None and out / bag >= SHARE and t is not None:
            hold = t - t_buy
    return hold, round(fast / bag, 3)


def real_solana_open():
    with contextlib.closing(sqlite3.connect(wsb.LEDGER, uri=True, timeout=30)) as c:
        return c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE is_open=1 "
                         "AND execution_mode='real' AND network_id='solana' AND COALESCE(tracking_paused_at,'')=''").fetchone()[0]


def wait_flat():
    while real_solana_open():
        print("real Solana position open: research reads paused", file=sys.stderr, flush=True)
        time.sleep(20)


def run(out_path, sol_usd):
    done = set()
    if out_path.exists():
        done = {json.loads(l)["uid"] for l in out_path.read_text().splitlines() if l.strip()}
    for w in wsb.wallets():
        if w["uid"] in done or w["role"] != "armed":
            continue
        rec = {**w, "rows": []}
        if w["owner"]:
            try:
                wait_flat()
                buys, _src = wsb.wallet_buys(w["uid"], w["owner"], sol_usd, want=BUYS)
                for b in buys:
                    wait_flat()
                    ata = wsb.dfx.owner_token_account(b["tx"], w["owner"], b["mint"])
                    hold = fast = None
                    complete = False
                    if ata and b["t"]:
                        txs, complete = wsb.gtfa(ata, {"blockTime": {"gte": b["t"], "lte": b["t"] + WINDOW_S}},
                                                 limit=50, pages=2)
                        hold, fast = first_exit(txs, w["owner"], b["mint"], b["sig"], b["bag"], b["t"])
                    rec["rows"].append({"sig": b["sig"], "mint": b["mint"], "t": b["t"], "sol": b["sol"],
                                        "hold_s": hold, "share_out_120s": fast, "scan_complete": complete})
            except wsb.fet.Stop as exc:
                print(json.dumps({"uid": w["uid"], "STOPPED": str(exc)}), file=sys.stderr, flush=True)
                return
            except OSError as exc:
                print(json.dumps({"uid": w["uid"], "handle": w["handle"], "SKIPPED": repr(exc)}),
                      file=sys.stderr, flush=True)
                continue
        with open(out_path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps({"handle": w["handle"], **summary(rec["rows"]), "calls": wsb.CALLS[0]}), flush=True)


def summary(rows):
    n = len(rows)
    holds = sorted(r["hold_s"] if r["hold_s"] is not None else float("inf") for r in rows)
    fast = sum(1 for r in rows if r["hold_s"] is not None and r["hold_s"] <= FAST_S)
    med = holds[n // 2] if n else None
    return {"n": n, "median_hold_s": (None if med is None else ("censored>%d" % WINDOW_S
                                                              if med == float("inf") else med)),
            "sold_lt2m": fast, "share_lt2m": round(fast / n, 2) if n else None}


def table(records, funded):
    lines = ["| handle | n buys | median source hold (s) | sold >=30% <2 min | funded Solana closes, $ |",
             "|---|---|---|---|---|"]
    rows = []
    for r in records:
        s = summary(r["rows"])
        rows.append((-(s["share_lt2m"] or 0), r["handle"], s, funded.get(r["uid"])))
    for _k, h, s, f in sorted(rows, key=lambda x: (x[0], x[1] or "")):
        lines.append("| %s | %d | %s | %s/%d (%s) | %s |" % (
            h, s["n"], s["median_hold_s"], s["sold_lt2m"], s["n"],
            "-" if s["share_lt2m"] is None else "%.0f%%" % (100 * s["share_lt2m"]),
            "-" if not f else "%d, %+.2f" % (f[0], f[2] or 0)))
    return "\n".join(lines)


def selftest():
    owner, mint = "O", "M"

    def tx(sig, slot, t, pre, post):
        return {"transaction": {"signatures": [sig]}, "slot": slot, "blockTime": t,
                "meta": {"preTokenBalances": [{"owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(pre)}}],
                         "postTokenBalances": [{"owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(post)}}]}}
    orig = wsb.owner_balance
    wsb.owner_balance = lambda t, o, m: (int(t["meta"]["preTokenBalances"][0]["uiTokenAmount"]["amount"]),
                                         int(t["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"]))
    try:
        txs = [tx("buy", 10, 1000, 0, 100), tx("s1", 11, 1030, 100, 80), tx("s2", 12, 1075, 80, 0)]
        assert first_exit(txs, owner, mint, "buy", 100, 1000) == (75, 1.0), first_exit(txs, owner, mint, "buy", 100, 1000)
        txs = [tx("s1", 11, 1030, 100, 90), tx("s2", 12, 1500, 90, 60)]
        assert first_exit(txs, owner, mint, "buy", 100, 1000) == (500, 0.1)
        assert first_exit(txs[:1], owner, mint, "buy", 100, 1000) == (None, 0.1)
    finally:
        wsb.owner_balance = orig
    s = summary([{"hold_s": 75}, {"hold_s": None}, {"hold_s": 400}])
    assert s["median_hold_s"] == 400 and s["sold_lt2m"] == 1 and s["share_lt2m"] == 0.33, s
    assert summary([{"hold_s": None}])["median_hold_s"] == "censored>1800"
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=("run", "table"))
    ap.add_argument("--out", default="logs/source_exit_speed_20260927.jsonl")
    ap.add_argument("--sol-usd", type=float, default=121.0)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.cmd == "run":
        run(REPO / a.out, a.sol_usd)
    if a.cmd == "table":
        print(table([json.loads(l) for l in (REPO / a.out).read_text().splitlines() if l.strip()],
                    wsb.funded_solana()))


if __name__ == "__main__":
    main()
