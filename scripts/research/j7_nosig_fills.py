"""The J7 Solana fills whose event carried no source signature: is the lane blind to the wallet, or
does J7 see buys the chain shows differently?

Alpha, 2026-09-28 06:3xZ: 40 of the 138 funded J7 Solana fills in 7 days (+$531, the largest cell of
j7_fill_lane_gap.py) came from J7 uuid events, which carry no transaction signature, so neither the
venue nor a lane path is known. Per fill:
  * handle and chain;
  * whether the wallet had a VERIFIED Solana address at the fill (memecoin_address_registry,
    status_at <= fill). Armed-on-solana is given: the funded path filled it. Armed AND verified is
    the one definition of "watched" that both the slot lane and the confirmed watcher read
    (armed_verified_owners);
  * whether the wallet's own chain history shows the buy: Helius getTransactionsForAddress on the
    research key, blockTime within +/-10 s of J7's receipt (then +/-60 s if nothing), succeeded only,
    a tx that raised the address's balance of that mint. Flat-gated before every read.

J7's receipt (memecoin_events.received_at) is the HOST clock; blockTime is whole seconds, which the
+/-10 s window absorbs. Writes one JSON line per fill to --out (resumes by position id).

    python3 scripts/research/j7_nosig_fills.py run --out logs/acceptance/j7_nosig.jsonl
    python3 scripts/research/j7_nosig_fills.py report --out logs/acceptance/j7_nosig.jsonl
    python3 scripts/research/j7_nosig_fills.py --selftest
"""
import argparse
import collections
import contextlib
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
LEDGER = str(REPO / "sniper_tracker.db")


def bought(tx, owner, mint):
    """Token units the owner gained of `mint` in this tx (0 when none)."""
    meta = (tx or {}).get("meta") or {}
    amt = lambda bals: sum(int((b.get("uiTokenAmount") or {}).get("amount") or 0) for b in bals or ()
                           if b.get("owner") == owner and b.get("mint") == mint)
    return amt(meta.get("postTokenBalances")) - amt(meta.get("preTokenBalances"))


def sig_of_tx(tx):
    return ((tx or {}).get("transaction") or {}).get("signatures", [None])[0]


def verdict(verified_at_fill, any_address, found):
    if not any_address:
        return "no_solana_address"
    if found is None:
        return "chain_not_read"
    if not verified_at_fill:
        return ("address_verified_after_fill: chain shows buy" if found
                else "address_verified_after_fill: chain shows no buy")
    return "watched: chain shows buy" if found else "watched: chain shows NO buy in window"


def run(args):
    import funded_exit_truth as fet
    from j7_fill_lane_gap import fills, ts
    from venue_census import book_open
    from wallet_crowd_dump_scoreboard import gtfa
    out = Path(args.out)
    done = {json.loads(l)["id"] for l in out.read_text().splitlines()} if out.exists() else set()
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % LEDGER, uri=True, timeout=20)) as c:
        c.row_factory = sqlite3.Row
        rows = [r for r in fills(c, args.since) if r["sig_via"] == "none"]
        meta = {}
        for r in rows:
            p = c.execute("SELECT source_user_id, origin_event_id FROM memecoin_positions WHERE id=?",
                          (r["id"],)).fetchone()
            ev = c.execute("SELECT received_at, created_at, their_size_usd FROM memecoin_events "
                           "WHERE fomo_event_id=?", (p["origin_event_id"],)).fetchone()
            reg = c.execute("SELECT address, status, status_at FROM memecoin_address_registry "
                            "WHERE source_user_id=? AND network_id='solana'", (p["source_user_id"],)).fetchall()
            meta[r["id"]] = (p["source_user_id"], dict(ev) if ev else {}, [dict(x) for x in reg])
    print("no-signature J7 fills since %s: %d (%d already read)" % (args.since[:19], len(rows), len(done)))
    with out.open("a") as fh:
        for r in rows:
            if r["id"] in done:
                continue
            uid, ev, reg = meta[r["id"]]
            recv = ev.get("received_at") or r["t0"]
            addrs = [x["address"] for x in reg if x["status"] == "verified"]
            if str(uid).startswith("pump:"):
                addrs = addrs or [str(uid)[5:]]
            verified_at_fill = str(uid).startswith("pump:") or any(
                x["status"] == "verified" and x["status_at"] <= r["t0"] for x in reg)
            found, hits, window = None, [], None
            for half in (10, 60):
                if not addrs:
                    break
                if book_open(LEDGER):
                    print("stopped: funded Solana book not flat")
                    return
                t = int(ts(recv).timestamp())
                found = False
                for a in addrs:
                    txs, _complete = gtfa(a, {"blockTime": {"gte": t - half, "lte": t + half}}, limit=100)
                    for tx in txs:
                        if bought(tx, a, r["mint"]) > 0:
                            hits.append({"address": a, "sig": sig_of_tx(tx), "block_time": tx.get("blockTime"),
                                         "slot": tx.get("slot"), "dt_s": (tx.get("blockTime") or t) - t})
                found, window = bool(hits), half
                if found:
                    break
            rec = {"id": r["id"], "handle": r["handle"], "chain": "solana", "mint": r["mint"], "pnl": r["pnl"],
                   "fill_t0": r["t0"], "j7_received_at": recv, "j7_created_at": ev.get("created_at"),
                   "their_size_usd": ev.get("their_size_usd"), "addresses": addrs,
                   "registry": reg, "verified_at_fill": verified_at_fill, "window_s": window,
                   "chain_hits": hits, "verdict": verdict(verified_at_fill, bool(addrs), found)}
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print("  %s %-16s %s %s" % (r["id"], r["handle"], rec["verdict"], [h["sig"][:10] for h in hits]))
    print("research-key 429s %d" % fet.RATE_429[0])


def report(args):
    recs = [json.loads(l) for l in Path(args.out).read_text().splitlines()]
    by = collections.defaultdict(list)
    for r in recs:
        by[r["verdict"]].append(r)
    print("no-signature J7 fills read: %d" % len(recs))
    for v, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        print("  %-52s n=%-3d PnL $%+.2f  handles %s" % (
            v, len(rs), sum(r["pnl"] or 0 for r in rs),
            dict(collections.Counter(r["handle"] for r in rs).most_common(6))))
    dts = sorted(h["dt_s"] for r in recs for h in r["chain_hits"][:1])
    if dts:
        print("  source blockTime minus J7 receipt (s, first hit per fill, whole seconds): p10 %s p50 %s p90 %s (n=%d)"
              % (dts[len(dts) // 10], dts[len(dts) // 2], dts[9 * len(dts) // 10], len(dts)))


def selftest():
    tx = {"meta": {"preTokenBalances": [{"owner": "A", "mint": "M", "uiTokenAmount": {"amount": "5"}}],
                   "postTokenBalances": [{"owner": "A", "mint": "M", "uiTokenAmount": {"amount": "12"}},
                                         {"owner": "B", "mint": "M", "uiTokenAmount": {"amount": "99"}}]},
          "transaction": {"signatures": ["SIG1"]}}
    assert bought(tx, "A", "M") == 7 and bought(tx, "B", "M") == 99 and bought(tx, "A", "X") == 0
    assert sig_of_tx(tx) == "SIG1"
    assert verdict(True, True, True) == "watched: chain shows buy"
    assert verdict(True, True, False) == "watched: chain shows NO buy in window"
    assert verdict(False, True, True).startswith("address_verified_after_fill")
    assert verdict(True, False, None) == "no_solana_address"
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--since", default="2026-09-21T06:00:00+00:00")
    p = sub.add_parser("report")
    p.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    return run(a) if a.cmd == "run" else report(a)


if __name__ == "__main__":
    main()
