#!/usr/bin/env python3
"""Was each funded Solana vol_death a dead coin or a lull? The chain around every fire.

Operator 2026-09-28 ("fake volume death, fix", PHISH 66467): the rail sold after 33.4 s of silence
and the mint traded again the second we sold. The silence itself was real: the mint had no
transaction at all from 14:42:54 to 14:43:26 (a complete getTransactionsForAddress read).

Per closed funded Solana vol_death position since --since, from the chain (research key via
funded_exit_truth.rpc, flat-gated per position): every succeeded transaction on the mint from the
exit - 120 s to the exit + 300 s, ordered by (slot, index). Our own exit transaction is excluded.
  lull_s   slots from the last other-party transaction at or before the fire to the first one
           after our sale, x 0.268 s (one clock: slots). This is the silence the coin actually had.
  after60  other-party transactions in the 60 s after our sale.
  price    prints decoded by dump_forensics.trade_of in the position's majority quote unit (a USDC
           leg and a SOL leg differ ~100x, so units never mix): the move from the last print before
           the fire to the max / min / last print within 300 s after our sale, and whether TP +25%
           or stop -35% from our ENTRY would have been reached, composing the realized exit return
           with that post-exit move.
  X-sweep  a fire at silence threshold X (30 / 45 / 60 / 90 s) happens only when lull_s >= X.

    python3 scripts/research/voldeath_lull_check.py run --out logs/acceptance/voldeath_lull.jsonl
    python3 scripts/research/voldeath_lull_check.py report --out logs/acceptance/voldeath_lull.jsonl
    python3 scripts/research/voldeath_lull_check.py --selftest
"""
import argparse
import contextlib
import json
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
LEDGER = str(REPO / "sniper_tracker.db")
SLOT_S = 0.268
PRE_S, POST_S = 120, 300
THRESHOLDS = (30, 45, 60, 90)
TP, STOP = 0.25, -0.35


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def ro():
    return sqlite3.connect("file:%s?mode=ro" % LEDGER, uri=True, timeout=30)


def positions(since):
    with contextlib.closing(ro()) as c:
        return [dict(zip(("id", "mint", "handle", "opened", "closed", "pnl", "notional", "close_sig", "fired_at",
                          "detail"), r)) for r in c.execute(
            "SELECT p.id, p.mint, p.source_handle, p.opened_recorded_at, p.closed_at, p.realized_pnl, "
            "p.entry_notional, p.close_sig, "
            "(SELECT a.checked_at FROM memecoin_exit_trigger_audit a WHERE a.position_id=p.id AND "
            " a.reason='vol_death' AND a.allowed=1 ORDER BY a.checked_at DESC LIMIT 1), "
            "(SELECT a.detail FROM memecoin_exit_trigger_audit a WHERE a.position_id=p.id AND "
            " a.reason='vol_death' AND a.allowed=1 ORDER BY a.checked_at DESC LIMIT 1) "
            "FROM memecoin_positions p WHERE p.execution_mode='real' AND p.network_id='solana' "
            "AND p.exit_reason='vol_death' AND p.closed_at>=? ORDER BY p.closed_at", (since,))]


def run(args):
    import dump_forensics as dfx
    import funded_exit_truth as fet
    import slot_lane_send_monitor as mon
    from slot_minout_grading import wait_flat
    from sniper.execution.solana_rpc_pool import key_id
    from wallet_crowd_dump_scoreboard import gtfa
    print("research key", key_id(mon.rpc_url()), flush=True)
    out = Path(args.out)
    done = {json.loads(l)["id"] for l in out.read_text().splitlines() if l.strip()} if out.exists() else set()
    with out.open("a") as fh:
        for p in positions(args.since):
            if p["id"] in done:
                continue
            wait_flat(ro)
            t_exit = int(ts(p["closed"]))
            try:
                txs, complete = gtfa(p["mint"], {"blockTime": {"gte": t_exit - PRE_S, "lte": t_exit + POST_S}},
                                     limit=100, pages=10)
            except fet.Stop as exc:
                print("STOPPED:", exc, flush=True)
                break
            rows = []
            for tx in txs:
                sig = tx["transaction"]["signatures"][0]
                t1 = dfx.trade_of(tx, p["mint"], 1.0)
                t2 = dfx.trade_of(tx, p["mint"], 1000.0) if t1 else None
                unit = None
                if t1 and t2 and t1.get("price") and t2.get("price"):
                    ratio = t2["price"] / t1["price"]
                    unit = "sol" if abs(ratio - 1000) < 1 else "usd" if abs(ratio - 1) < 1e-6 else None
                rows.append([tx.get("slot"), tx.get("transactionIndex") or 0, tx.get("blockTime"),
                             int(sig == p["close_sig"]), unit, t1["price"] if t1 and unit else None])
            rec = {**p, "complete": complete, "txs": rows}
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print("%s %-8s %-14s txs %d complete %s" % (p["closed"][:19], p["id"], (p["handle"] or "")[:14],
                                                     len(rows), complete), flush=True)
    print("research-key 429s %d" % fet.RATE_429[0], flush=True)


def lull(rec):
    """(lull_s, after60, ours_found, resumed_before_sale): the silence the rail fired on, in slot time.

    The rail fires after >= 30 s with no swap, so the silence began at or before fired - 30 s. The
    lull is the gap between the last other-party transaction at or before that instant and the first
    other-party transaction after it, whether that came before our sale landed or after it. (A
    first cut measured from the last transaction before our SALE, and read PHISH's 32 s silence as
    0.8 s because trading had resumed between the fire and our landing.)"""
    txs = sorted(rec["txs"])
    ours = next((t for t in txs if t[3]), None)
    if ours is None:
        return None, None, False, None
    fired = ts(rec["fired_at"]) if rec.get("fired_at") else ts(rec["closed"])
    others = [t for t in txs if not t[3] and t[2] is not None]
    before = [t for t in others if t[2] <= fired - 29]
    after60 = len([t for t in others if (t[0], t[1]) > (ours[0], ours[1]) and t[2] <= ours[2] + 60])
    if not before:
        return None, after60, True, None
    nxt = [t for t in others if (t[0], t[1]) > (before[-1][0], before[-1][1])]
    if not nxt:
        return None, after60, True, None
    return (nxt[0][0] - before[-1][0]) * SLOT_S, after60, True, (nxt[0][0], nxt[0][1]) < (ours[0], ours[1])


def price_after(rec):
    """(move_max, move_min, move_last, tp_hit, stop_hit) in the majority unit, or None."""
    txs = sorted(rec["txs"])
    ours = next((t for t in txs if t[3]), None)
    units = [t[4] for t in txs if t[4] and not t[3]]
    if not ours or not units:
        return None
    unit = max(set(units), key=units.count)
    prints = [t for t in txs if t[4] == unit and not t[3] and t[5]]
    pre = [t for t in prints if (t[0], t[1]) < (ours[0], ours[1])]
    post = [t for t in prints if (t[0], t[1]) > (ours[0], ours[1]) and t[2] is not None and t[2] <= ours[2] + 300]
    if not pre or not post:
        return None
    base = pre[-1][5]
    moves = [t[5] / base - 1 for t in post]
    exit_ret = (rec["pnl"] or 0) / rec["notional"] if rec.get("notional") else 0.0
    from_entry = [(1 + exit_ret) * (1 + m) - 1 for m in moves]
    return max(moves), min(moves), moves[-1], any(r >= TP for r in from_entry), any(r <= STOP for r in from_entry)


def report(args):
    recs = [json.loads(l) for l in Path(args.out).read_text().splitlines() if l.strip()]
    print("vol_death exits read: %d (complete %d)" % (len(recs), sum(r["complete"] for r in recs)))
    lulls = []
    for r in recs:
        g, a60, ok, resumed = lull(r)
        r["_resumed"] = resumed
        pa = price_after(r)
        r["_lull"] = g
        lulls.append(g)
        mv = ["%+.1f%%" % (100 * x) for x in pa[:3]] if pa else ["na"] * 3
        print("  %s %-6s %-14s pnl %+7.2f  %-34s lull %s  txs next 60s %s  after: max %s min %s last %s  TP %s stop %s" % (
            r["closed"][:16], r["id"], (r["handle"] or "")[:14], r["pnl"] or 0, (r["detail"] or "")[:34],
            ("%.1fs%s" % (g, " (resumed before our sale landed)" if resumed else "")) if g is not None else "na",
            a60 if a60 is not None else "na", mv[0], mv[1], mv[2],
            pa[3] if pa else "na", pa[4] if pa else "na"))
    known = [g for g in lulls if g is not None]
    print("\nlull known %d of %d; p25/p50/p75 %s" % (len(known), len(lulls), (
        "%.1f/%.1f/%.1f s" % tuple(sorted(known)[int(q * (len(known) - 1))] for q in (0.25, 0.5, 0.75))) if known else "na"))
    for x in THRESHOLDS:
        fire = [r for r in recs if r["_lull"] is not None and r["_lull"] >= x]
        print("  threshold %2ds: would still fire %d of %d known, pnl of those %+.2f; the rest resumed trading before %ds"
              % (x, len(fire), len(known), sum(r["pnl"] or 0 for r in fire), x))


def selftest():
    rec = {"fired_at": "2026-09-28T00:00:40+00:00", "closed": "2026-09-28T00:00:41+00:00", "complete": True,
           "pnl": -3.0, "notional": 100.0,
           "txs": [[100, 1, 1790553600, 0, "usd", 1.00], [110, 0, 1790553603, 0, "usd", 1.02],
                   [230, 5, 1790553641, 1, "usd", 0.98], [235, 2, 1790553642, 0, "usd", 1.10],
                   [240, 0, 1790553643, 0, "usd", 1.40]]}
    g, a60, ok, resumed = lull(rec)
    assert ok and abs(g - (235 - 110) * SLOT_S) < 1e-9 and a60 == 2 and resumed is False, (g, a60, resumed)
    early = {**rec, "txs": rec["txs"] + [[228, 0, 1790553640, 0, "usd", 1.01]]}   # trading resumed before our sale
    g2, _a, _o, resumed2 = lull(early)
    assert abs(g2 - (228 - 110) * SLOT_S) < 1e-9 and resumed2 is True, (g2, resumed2)
    mx, mn, last, tp, stop = price_after(rec)
    assert abs(mx - (1.40 / 1.02 - 1)) < 1e-9 and tp and not stop
    assert lull({**rec, "txs": [t for t in rec["txs"] if not t[3]]})[2] is False     # our sale not found
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run")
    p.add_argument("--out", required=True)
    p.add_argument("--since", default="2026-09-21")
    q = sub.add_parser("report")
    q.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    return run(a) if a.cmd == "run" else report(a)


if __name__ == "__main__":
    main()
