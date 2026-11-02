"""Report the venue programs for unbound funded Solana positions.

The script opens both local databases in read-only mode.
It reads each position entry transaction from the configured Solana RPC.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Infrastructure that appears in every route and never identifies the venue.
INFRA = {
    "11111111111111111111111111111111": "system",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "spl-token",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "token-2022",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "associated-token",
    "ComputeBudget111111111111111111111111111111": "compute-budget",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter-aggregator",
}
KNOWN = {
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pump.fun AMM (pumpswap)",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun bonding curve",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium v4",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium clmm",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora dlmm",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "meteora pools",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "orca whirlpool",
}


def rpc(url, method, params, retries=3):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last_error = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=30))
        except Exception as exc:
            last_error = exc
            if attempt == retries - 1:
                break
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"Solana RPC read failed for {method}") from last_error


def venue_of(url, sig, mint, cache):
    """Return the venue program and pool authority for one entry transaction."""
    tx = rpc(url, "getTransaction",
             [sig, {"maxSupportedTransactionVersion": 1, "encoding": "jsonParsed"}]).get("result")
    if not tx:
        return None, None, "tx_not_found"
    payer = tx["transaction"]["message"]["accountKeys"][0]["pubkey"]
    owners = set()
    for side in ("preTokenBalances", "postTokenBalances"):
        for b in tx["meta"].get(side) or []:
            if b.get("mint") == mint and b.get("owner") and b["owner"] != payer:
                owners.add(b["owner"])
    if not owners:
        return None, None, "no_pool_counterparty"
    candidates = []
    for authority in sorted(owners):
        if authority not in cache:
            info = rpc(url, "getAccountInfo", [authority, {"encoding": "base64"}]).get("result") or {}
            cache[authority] = ((info.get("value") or {}) or {}).get("owner")
        if cache[authority]:
            candidates.append((cache[authority], authority))
    if not candidates:
        return None, None, "authority_not_found"
    known = [item for item in candidates if item[0] in KNOWN]
    selected = known or candidates
    programs = {item[0] for item in selected}
    if len(programs) != 1:
        return None, None, "ambiguous_programs"
    return selected[0][0], selected[0][1], "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-01")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--tape-db", default="data/memecoin_sol_tape.db")
    ap.add_argument("--rpc", default=os.getenv("MEMECOIN_SOLANA_READ_RPC_URL")
                    or os.getenv("MEMECOIN_SOLANA_RPC_URL")
                    or "https://api.mainnet-beta.solana.com")
    ap.add_argument("--sleep", type=float, default=0.12)
    a = ap.parse_args()

    m = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True); m.row_factory = sqlite3.Row
    t = sqlite3.connect(f"file:{a.tape_db}?mode=ro", uri=True); t.row_factory = sqlite3.Row

    rows = m.execute(
        "SELECT p.id,p.ticker,p.mint,p.entry_notional,p.opened_at,f.tx_sig "
        "FROM memecoin_positions p JOIN memecoin_fills f "
        "  ON f.position_id=p.id AND f.kind='entry' AND f.tx_sig IS NOT NULL "
        "WHERE p.execution_mode='real' AND p.network_id='solana' AND p.opened_at>=? "
        "ORDER BY p.id DESC LIMIT ?", (a.since, a.limit)).fetchall()

    bound, unbound = [], []
    for r in rows:
        hit = t.execute("SELECT 1 FROM sol_position_pools p JOIN sol_pool_vaults v ON v.pool=p.pool "
                        "WHERE p.position_id=? AND p.mint=?", (r["id"], r["mint"])).fetchone()
        (bound if hit else unbound).append(r)
    print(f"funded solana positions since {a.since}: {len(rows)}  "
          f"bound {len(bound)}  UNBOUND {len(unbound)} ({100*len(unbound)/max(1,len(rows)):.0f}%)\n")

    by_venue = defaultdict(lambda: {"n": 0, "usd": 0.0, "tickers": [], "authority": None})
    cache = {}
    for i, r in enumerate(unbound, 1):
        try:
            prog, authority, status = venue_of(a.rpc, r["tx_sig"], r["mint"], cache)
        except RuntimeError:
            prog, authority, status = None, None, "rpc_error"
        key = prog or f"<{status}>"
        v = by_venue[key]
        v["n"] += 1
        v["usd"] += float(r["entry_notional"] or 0)
        if len(v["tickers"]) < 4:
            v["tickers"].append(r["ticker"] or r["mint"][:8])
        v["authority"] = v["authority"] or authority
        print(f"  [{i:>3}/{len(unbound)}] pos {r['id']:<6} {str(r['ticker'])[:12]:<12} "
              f"-> {KNOWN.get(key, key)}")
        time.sleep(a.sleep)

    print(f"\n{'venue program':<46}{'pos':>5}{'funded $':>11}  examples")
    for prog, v in sorted(by_venue.items(), key=lambda kv: -kv[1]["n"]):
        label = KNOWN.get(prog, INFRA.get(prog, ""))
        print(f"{prog:<46}{v['n']:>5}{v['usd']:>11.2f}  {', '.join(v['tickers'])}"
              + (f"   [{label}]" if label else "   [UNKNOWN — needs a decoder]"))


if __name__ == "__main__":
    main()
