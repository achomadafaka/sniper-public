#!/usr/bin/env python3
"""The Axiom kol-list seed for the Wallet Map's one-off read (pre-registration
docs/audits/2026-09-30-wallet-map-reads-preregistration.md F): every Solana address on Axiom's
smart-money "kol" list in the 2026-09-16 capture, in 7-day rank order, with the other lists it is
on. Read-only on data/axiom_raw.db; prints JSON (redirect it to a file for `wallet_link_graph.py
run --axiom FILE`).

    python3 scripts/research/axiom_kol_seed.py [--raw data/axiom_raw.db] > data/wallet_map_axiom_kol.json

A list's rank comes from its NEWEST captured payload; a wallet on several lists keeps its kol rank.
"""
import argparse
import json
import sqlite3
import sys


def seed(rows):
    """[(payload_id, payload_obj)] -> the kol-list wallets in 7-day rank order, each with the lists it is
    on. Membership and rank come from each list's NEWEST captured payload (the lists as stored at the
    end of the capture); a wallet on several lists keeps its kol rank. A list the wallet was on only in
    an EARLIER payload is kept as a note (`earlier_only`), never as a label (Gamma, F amendment). Pure."""
    newest, ever = {}, {}                                 # list name -> (payload id, rows); address -> lists
    for pid, obj in rows:
        if not isinstance(obj, dict) or not isinstance(obj.get("rows"), list):
            continue
        name = obj.get("source")
        if name and (name not in newest or pid > newest[name][0]):
            newest[name] = (pid, obj["rows"])
        for r in obj["rows"]:
            addr = ((r.get("wallets") or {}).get("solana") or {}).get("address")
            if addr and name:
                ever.setdefault(addr, set()).add(name)
    lists, handle, rank = {}, {}, {}
    for name, (_pid, list_rows) in newest.items():
        for r in list_rows:
            addr = ((r.get("wallets") or {}).get("solana") or {}).get("address")
            if not addr:
                continue
            lists.setdefault(addr, set()).add(name)
            handle.setdefault(addr, r.get("handle"))
            rk = ((r.get("windows") or {}).get("7d") or {}).get("rank")
            if name == "kol" and rk is not None:
                rank[addr] = rk
    kol = [a for a, ls in lists.items() if "kol" in ls]
    ordered = sorted(kol, key=lambda a: (rank.get(a) is None, rank.get(a) or 0, a))
    return [{"address": a, "handle": handle.get(a), "rank_7d": rank.get(a), "lists": sorted(lists[a]),
             "earlier_only": sorted(ever.get(a, set()) - lists[a])} for a in ordered]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--raw", default="data/axiom_raw.db")
    args = ap.parse_args()
    conn = sqlite3.connect("file:%s?mode=ro" % args.raw, uri=True, timeout=5)
    rows = []
    for pid, payload in conn.execute("SELECT id, payload FROM axiom_raw_messages WHERE transport='rest' "
                                     "AND source LIKE '%smart-money-traders%' ORDER BY id"):
        try:
            rows.append((pid, json.loads(payload if isinstance(payload, str) else payload.decode())))
        except ValueError:
            continue
    wallets = seed(rows)
    json.dump({"source": "axiom_raw.db smart-money-traders, 2026-09-16 capture", "wallets": wallets}, sys.stdout,
              indent=1)
    print(file=sys.stderr)
    print("kol-list wallets: %d (kol only %d, with pump %d, unranked %d)" % (
        len(wallets), sum(1 for w in wallets if w["lists"] == ["kol"]),
        sum(1 for w in wallets if "pump" in w["lists"]), sum(1 for w in wallets if w["rank_7d"] is None)),
        file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
