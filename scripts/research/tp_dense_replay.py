"""Lower-TP replay on EVERY trade during the hold, not only our exact quotes (operator, 2026-09-28).

source_sell_tp_replay.py (9356df8d) used the exit_decision exact quotes: p50 0.73/s in the first minute,
gap p50 1.1 s / p90 2.1 s, so a spike between two quotes was invisible. This reads every trade on the
coin during each hold from the chain and rebuilds a dense path. EXPLORATORY, same caveat as 9356df8d.

  Population  source_sell_tp_replay's cohort B restricted to SOLANA: every funded Solana position (since
              09-13, shape_overlay_replay.load rules) of the wallets that sold on us; A = those closed by
              the source selling. 4663 has no dense read here.
  Trades      Helius getTransactionsForAddress on the MINT, blockTime within the hold, succeeded, asc,
              ordered by (slot, transactionIndex); research key only, flat-gated per call; decoded by
              dump_forensics.trade_of. ONLY SOL-QUOTED trades are kept: trade_of prices a USDC leg at
              1.0 and a SOL leg at sol_usd, so with any fixed sol_usd the two sit ~100x apart and a mixed
              path invents spikes (the first run read +$316k "optimistic"). A trade is SOL-quoted when its
              price scales with sol_usd (decoded at 1 and 1000). A hold with more trades than the page cap
              is TRUNCATED and reported.
  Dense r     exit quotes carry context_slot, so trades and quotes align by SLOT (one clock). For a trade at
              slot S: the latest quote q with context_slot <= S, spot_q = the last trade spot at or before
              that slot, r = (1 + r_q) * spot / spot_q - 1: the quote's executable level moved by the market.
  TP(x)       triggers at the first trade with r >= x; FILLS at the first exact quote with context_slot
              after the trigger slot (what a live exit could get), else the actual exit stands. The trigger
              trade's own r is printed as the optimistic bound.
    python3 scripts/research/tp_dense_replay.py run --out logs/acceptance/tp_dense.jsonl
    python3 scripts/research/tp_dense_replay.py report --out logs/acceptance/tp_dense.jsonl
    python3 scripts/research/tp_dense_replay.py --selftest
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import bisect
import contextlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
LEDGER = str(REPO / "sniper_tracker.db")
LEVELS = (5, 10, 15, 20)
MIRROR = ("mirror", "mirror_source_sell")
PAGES = 30


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def dense_path(quotes, trades):
    """quotes: [(context_slot, r_pct)] in order; trades: [(slot, spot)] in (slot, index) order.
    Returns [(slot, r_pct)] for trades at or after the first quote's slot."""
    out, qslots = [], [q[0] for q in quotes]
    tslots = [t[0] for t in trades]
    for slot, spot in trades:
        k = bisect.bisect_right(qslots, slot) - 1
        if k < 0 or spot <= 0:
            continue
        qslot, r_q = quotes[k]
        j = bisect.bisect_right(tslots, qslot) - 1
        if j < 0 or trades[j][1] <= 0:
            continue
        out.append((slot, ((1.0 + r_q / 100.0) * spot / trades[j][1] - 1.0) * 100.0))
    return out


def tp_dense(dense, quotes, x, notional):
    """(rule P&L filled at the next quote after the trigger or None, optimistic P&L at the trigger or None)."""
    for slot, r in dense:
        if r >= x:
            nxt = [rq for qs, rq in quotes if qs > slot]
            return (float(notional) * nxt[0] / 100.0 if nxt else None), float(notional) * r / 100.0
    return None, None


def run(args):
    import dump_forensics as dfx
    import funded_exit_truth as fet
    from shape_overlay_replay import load
    from venue_census import book_open
    from wallet_crowd_dump_scoreboard import gtfa
    rows = load()
    wallets = {r["handle"] for r in rows if r["reason"] in MIRROR}
    todo = [r for r in rows if r["chain"] == "solana" and (args.all or r["handle"] in wallets)]
    out = Path(args.out)
    done = {json.loads(l)["id"] for l in out.read_text().splitlines()} if out.exists() else set()
    print("cohort B solana: %d positions of %d wallets; %d already read" % (len(todo), len(wallets), len(done)), flush=True)
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % LEDGER, uri=True, timeout=20)) as c, out.open("a") as fh:
        for r in todo:
            if r["id"] in done:
                continue
            if book_open(LEDGER):
                print("stopped: funded Solana book not flat")
                break
            mint, entry, o, cl = c.execute("SELECT mint, entry_price, opened_recorded_at, closed_at FROM memecoin_positions "
                                           "WHERE id=?", (r["id"],)).fetchone()
            quotes = []
            for ev, u in c.execute("SELECT quote_evidence_json, unit_price FROM memecoin_route_series WHERE position_id=? "
                                   "AND seam='exit_decision' ORDER BY id", (r["id"],)):
                try:
                    slot = (json.loads(ev or "{}") or {}).get("context_slot")
                except ValueError:
                    slot = None
                if slot and u and u > 0:
                    quotes.append((int(slot), (u / entry - 1.0) * 100.0))
            quotes.sort()
            txs, complete = gtfa(mint, {"blockTime": {"gte": int(ts(o)) - 1, "lte": int(ts(cl)) + 1}},
                                 limit=100, pages=PAGES)
            trades = []
            other = 0
            for tx in txs:
                t = dfx.trade_of(tx, mint, 1.0)
                if t and t.get("price"):
                    t2 = dfx.trade_of(tx, mint, 1000.0)
                    if t2 and t2.get("price") and t2["price"] / t["price"] > 500:     # SOL-quoted
                        trades.append((tx.get("slot"), tx.get("transactionIndex") or 0, t["price"]))
                    else:
                        other += 1
            trades.sort()
            rec = {"id": r["id"], "handle": r["handle"], "reason": r["reason"], "pnl": r["pnl"], "notional": r["notional"],
                   "quotes": quotes, "trades": [(s, p) for s, _i, p in trades], "tx_read": len(txs),
                   "non_sol_trades": other, "complete": complete}
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print("  %s %-16s txs %d trades %d quotes %d complete %s" % (r["id"], r["handle"], len(txs), len(trades),
                                                                         len(quotes), complete), flush=True)
    print("research-key 429s %d" % fet.RATE_429[0])


def report(args):
    from shape_overlay_replay import boot_ci
    recs = [json.loads(l) for l in Path(args.out).read_text().splitlines()]
    recs = [r for r in recs if r["quotes"]]
    peaks_q, peaks_d = [], []
    for r in recs:
        d = dense_path(r["quotes"], r["trades"])
        r["dense"] = d
        peaks_q.append(max(q[1] for q in r["quotes"]))
        peaks_d.append(max([x[1] for x in d] + [max(q[1] for q in r["quotes"])]))
    print("dense replay: %d positions (%d truncated at %d pages); trades per position p50 %d"
          % (len(recs), sum(1 for r in recs if not r["complete"]), PAGES,
             sorted(len(r["trades"]) for r in recs)[len(recs) // 2]))
    higher = sum(1 for a, b in zip(peaks_q, peaks_d) if b > a + 0.5)
    print("peak run-up seen by the dense path above the quote path by > 0.5 pp: %d of %d positions" % (higher, len(recs)))
    # The same 5x-median outlier filter the operator-facing read used (a second pool or a bad print).
    for r in recs:
        ps = sorted(p for _s, p in r["trades"])
        med = ps[len(ps) // 2] if ps else None
        r["dense"] = dense_path(r["quotes"], [(s, p) for s, p in r["trades"] if med and med / 5 <= p <= med * 5])
    if args.wallets:
        chosen = {w.strip() for w in Path(args.wallets).read_text().split() if w.strip()}
        cohorts = (("SELECTED wallets (%s): full books" % ", ".join(sorted(chosen)),
                    [r for r in recs if r["handle"] in chosen]),
                   ("EVERYONE ELSE", [r for r in recs if r["handle"] not in chosen]))
    else:
        cohorts = (("A closed by the source selling", [r for r in recs if r["reason"] in MIRROR]),
                   ("B every solana position of those wallets", recs))
    for label, rs in cohorts:
        print("\n%s: n=%d, actual P&L $%+.2f" % (label, len(rs), sum(r["pnl"] for r in rs)))
        for x in LEVELS:
            d_next, d_opt, fired = [], [], 0
            for r in rs:
                v, opt = tp_dense(r["dense"], r["quotes"], x, r["notional"])
                d_next.append((v - r["pnl"]) if v is not None else 0.0)
                d_opt.append((opt - r["pnl"]) if opt is not None else 0.0)
                fired += v is not None
            lo, hi = boot_ci(d_next)
            print("  TP %2d%% dense trigger, fill next quote: fired %3d  delta %+9.2f  CI[%s, %s] | at the trigger trade (optimistic) %+9.2f"
                  % (x, fired, sum(d_next), "%+.3f" % lo if lo is not None else "na",
                     "%+.3f" % hi if hi is not None else "na", sum(d_opt)))


def selftest():
    quotes = [(100, 0.0), (104, 5.0)]
    trades = [(99, 1.0), (100, 1.0), (102, 1.2), (104, 1.05), (105, 1.10)]
    d = dense_path(quotes, trades)
    assert [s for s, _ in d] == [100, 102, 104, 105], d
    assert abs(d[1][1] - 20.0) < 1e-9          # spot +20% since the 0% quote at slot 100
    assert abs(d[3][1] - ((1.05 * 1.10 / 1.05) - 1) * 100) < 1e-9   # re-anchored on the slot-104 quote
    v, opt = tp_dense(d, quotes, 15, 100.0)
    assert v == 5.0 and abs(opt - 20.0) < 1e-9  # triggered by the slot-102 spike, filled at the slot-104 quote
    assert tp_dense(d, quotes, 50, 100.0) == (None, None)
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("run", "report"):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
        p.add_argument("--all", action="store_true", help="run: every Solana position, not only cohort B")
        p.add_argument("--wallets", help="report: a file of handles; split selected vs everyone else")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    return run(a) if a.cmd == "run" else report(a)



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('report', 'main'))

if __name__ == "__main__":
    main()
