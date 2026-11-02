#!/usr/bin/env python3
"""Resolve each fomo trader's on-chain Solana address by current-holder intersection.

We store only fomo UUIDs for the armed roster, never the on-chain wallet. To watch these
wallets on-chain (the fomo-bypass), we need the address. Method, using data we already own
(the mints each trader bought, from memecoin_events) plus Helius DAS getTokenAccounts:

1. For each trader, take several mints they bought and pull the CURRENT holder set of each
   (one getTokenAccounts call per mint, paged). No time search — the killer for hot pump.fun
   coins whose mint history is thousands of pages deep.
2. Intersect the holder sets. A wallet that still holds MANY of a trader's coins is a strong
   candidate; the probability a random holder holds N specific coins is vanishingly small.
3. Cross-eliminate: any address that is a top candidate for MORE THAN ONE trader is shared
   infrastructure (router / market maker / fee account), not a trader. Drop it.
4. What remains, unique per trader and holding the most of that trader's coins, is the wallet.

The intersection is the proof: one coincidental shared holder means nothing, a wallet holding
6 of a trader's 8 specific coins that no other trader's candidate holds is that trader.

Read-only against Helius + our tracker. Prints the mapping; writes only with --write.
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

ROSTER = {
    "frankdegods": "aefe2ddd-c580-5245-a2f5-e4ed62f7ef10",
    "casino847": "5e911042-e839-569e-ab0e-2ab7c4389b95",
    "Aurelius0121": "d676a05f-0b3e-57d1-86dc-d6986927c6c8",
    "MasterChief117": "eb960be1-cca6-5bef-9977-d7c63ed46d3b",
    "jester": "f62ddfdd-eeb1-5749-ad3b-a4cd65c319a9",
    "Thepennyflippe": "c05654ac-2ce2-513d-be13-76b81b34a6e1",
}
# Known shared infra (pump.fun fee, common routers) accumulates over runs; seed empties.
KNOWN_INFRA = set()


def rpc_url():
    for line in (REPO / ".env").read_text().splitlines():
        if line.startswith("MEMECOIN_SOLANA_RPC_URL=") and "helius" in line:
            return line.split("=", 1)[1].strip()
    raise SystemExit("no Helius RPC url in env")


URL = rpc_url()


def _rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(URL, data=body, headers={"content-type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode()).get("result")
        except Exception:
            if attempt == 2:
                raise
            time.sleep(0.5)


def holders(mint, max_pages=8):
    """Current owners with a positive balance of `mint` (Helius DAS getTokenAccounts)."""
    owners, cursor = set(), None
    for _ in range(max_pages):
        params = {"mint": mint, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        res = _rpc("getTokenAccounts", params) or {}
        accs = res.get("token_accounts", [])
        for a in accs:
            if a.get("owner") and float(a.get("amount", 0) or 0) > 0:
                owners.add(a["owner"])
        cursor = res.get("cursor")
        if not cursor or not accs:
            break
        time.sleep(0.1)
    return owners


def trader_mints(conn, uid, n=8):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT mint FROM memecoin_events WHERE source_user_id=? AND side='buy' "
        "AND network_id='solana' ORDER BY id DESC LIMIT ?", (uid, n))]


def candidates(conn, uid, n_mints=8):
    """Rank holders by how many of the trader's mints they still hold. Returns Counter."""
    mints = trader_mints(conn, uid, n_mints)
    tally = Counter()
    used = 0
    for m in mints:
        h = holders(m)
        if not h:
            continue
        used += 1
        for o in h:
            tally[o] += 1
    return tally, used


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated handles")
    ap.add_argument("--n-mints", type=int, default=8)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{REPO/'sniper_tracker.db'}?mode=ro", uri=True)
    only = set(args.only.split(",")) if args.only else None

    # Pass 1: candidate tallies per trader.
    tallies = {}
    for handle, uid in ROSTER.items():
        if only and handle not in only:
            continue
        tally, used = candidates(conn, uid, args.n_mints)
        tallies[handle] = (tally, used)
        top = tally.most_common(4)
        print(f"{handle:<16} mints_used={used}  top holders: "
              + ", ".join(f"{a[:8]}={c}" for a, c in top))

    # Pass 2: cross-eliminate addresses that are strong candidates for >1 trader (shared infra).
    strong_for = Counter()
    for handle, (tally, used) in tallies.items():
        thresh = max(2, used - 1)          # holds nearly all of this trader's coins
        for a, c in tally.items():
            if c >= thresh:
                strong_for[a] += 1
    infra = {a for a, n in strong_for.items() if n >= 2} | KNOWN_INFRA
    print(f"\nshared-infra addresses dropped (candidate for >=2 traders): {len(infra)}")
    for a in list(infra)[:10]:
        print(f"  infra {a}")

    # Pass 3: pick each trader's unique best holder after removing infra.
    resolved = {}
    print("\n=== resolution ===")
    for handle, (tally, used) in tallies.items():
        uid = ROSTER[handle]
        ranked = [(a, c) for a, c in tally.most_common(20) if a not in infra]
        if not ranked:
            print(f"  {handle:<16} UNRESOLVED (all top candidates were shared infra)")
            continue
        addr, hits = ranked[0]
        conf = "STRONG" if hits >= max(3, used - 1) else ("medium" if hits >= 2 else "WEAK")
        second = ranked[1] if len(ranked) > 1 else ("-", 0)
        print(f"  {handle:<16} {addr}  holds {hits}/{used} coins  [{conf}]  "
              f"(next: {second[0][:8]}={second[1]})")
        if hits >= 2:
            resolved[handle] = (uid, addr, hits, used)

    if args.write and resolved:
        w = sqlite3.connect(REPO / "sniper_tracker.db", timeout=30)
        w.execute("""CREATE TABLE IF NOT EXISTS memecoin_wallet_addresses (
            source_user_id TEXT PRIMARY KEY, handle TEXT, network_id TEXT NOT NULL,
            address TEXT NOT NULL, resolved_at TEXT NOT NULL, method TEXT,
            coins_held INTEGER, coins_checked INTEGER)""")
        now = datetime.now(timezone.utc).isoformat()
        for handle, (uid, addr, hits, used) in resolved.items():
            w.execute("INSERT OR REPLACE INTO memecoin_wallet_addresses VALUES (?,?,?,?,?,?,?,?)",
                      (uid, handle, "solana", addr, now, "holder_intersection", hits, used))
        w.commit()
        print(f"\nwrote {len(resolved)} addresses to memecoin_wallet_addresses")


if __name__ == "__main__":
    main()
