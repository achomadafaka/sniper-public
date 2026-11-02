#!/usr/bin/env python3
"""Median round-trip FIXED fee per funded close, for the trial-arm clip (rule 3a0b141ee: the chain's
median round-trip fixed fee must be <= 2% of the trial clip).

Fixed fee = what a round trip costs whatever its size: on 4663 the gas of every fill transaction
(gasUsed x effectiveGasPrice, which on this Orbit chain already carries the L1 component); on Solana
each fill transaction's `meta.fee` (base + priority). Not counted, and stated: LP fees, price impact
and token taxes scale with the clip (they are not fixed); a Solana token account's rent is refunded
when the exit closes it; 4663 approval transactions are not linked to fills (the pre-approval table's
median is reported beside the swaps).

Reads our own fills read-only and their receipts on the RESEARCH endpoints only (4663: the research
QuickNode key, 0.5 s spacing, stop on a 429; Solana: pick_research_url, 0.5 s spacing). Writes nothing.

    python3 scripts/research/trial_clip_fees.py --chain 4663 --n 20
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("SNIPER_CHECKOUT") or Path(__file__).resolve().parents[2])
SPACING_S = 0.5


def rpc(url, method, params):
    time.sleep(SPACING_S)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json",
                                             "User-Agent": "sniper-research/1"})
    out = json.loads(urllib.request.urlopen(req, timeout=30).read())   # a 429 raises: stop
    if out.get("error"):
        raise SystemExit(f"RPC error on {method}: {out['error']}")
    return out.get("result")


def closes(ledger, chain, n):
    rows = ledger.execute(
        "SELECT p.id, f.kind, f.tx_sig, f.notional FROM memecoin_positions p "
        "JOIN memecoin_fills f ON f.position_id=p.id WHERE p.execution_mode='real' AND p.is_open=0 "
        "AND p.network_id=? AND COALESCE(p.signal_source,'')!='manual' AND p.realized_pnl IS NOT NULL "
        "AND f.tx_sig IS NOT NULL ORDER BY p.id DESC, f.id", (chain,)).fetchall()
    by = {}
    for pid, kind, sig, notional in rows:
        by.setdefault(pid, []).append((kind, sig, notional))
    full = [(pid, fills) for pid, fills in by.items()
            if any(k == "entry" for k, *_ in fills) and any(k != "entry" for k, *_ in fills)]
    return full[:n]


def evm_fees(url, sample):
    out = []
    for pid, fills in sample:
        wei = 0
        eth_usd = None
        for kind, sig, notional in fills:
            rec = rpc(url, "eth_getTransactionReceipt", [sig])
            wei += int(rec["gasUsed"], 16) * int(rec["effectiveGasPrice"], 16)
            if kind == "entry" and notional:
                value = int(rpc(url, "eth_getTransactionByHash", [sig])["value"], 16)
                if value > 0:
                    eth_usd = float(notional) / (value / 1e18)
        out.append({"position_id": pid, "fills": len(fills), "fee_eth": wei / 1e18, "eth_usd": eth_usd})
    prices = [r["eth_usd"] for r in out if r["eth_usd"]]
    eth_usd = statistics.median(prices) if prices else None
    for r in out:
        r["fee_usd"] = r["fee_eth"] * eth_usd if eth_usd else None
    return out, {"eth_usd_median_from_entries": eth_usd}


def solana_fees(url, sample, ledger):
    sol = [r[0] for r in ledger.execute(
        "SELECT funding_sol_usd FROM memecoin_fills WHERE funding_sol_usd IS NOT NULL "
        "ORDER BY id DESC LIMIT 50")]
    sol_usd = statistics.median(sol) if sol else None
    out = []
    for pid, fills in sample:
        lamports = 0
        for _kind, sig, _n in fills:
            tx = rpc(url, "getTransaction", [sig, {"encoding": "json", "commitment": "confirmed",
                                                   "maxSupportedTransactionVersion": 1}])
            lamports += int(((tx or {}).get("meta") or {}).get("fee") or 0)
        out.append({"position_id": pid, "fills": len(fills), "fee_sol": lamports / 1e9,
                    "fee_usd": lamports / 1e9 * sol_usd if sol_usd else None})
    return out, {"sol_usd_median_recent_fills": sol_usd}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", required=True, choices=("4663", "solana"))
    ap.add_argument("--n", type=int, default=20)
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts" / "research"))
    from sniper.util.envfiles import load_env_files
    load_env_files(ROOT)
    from sniper.persistence.db_core import get_readonly_connection
    ledger = get_readonly_connection(str(ROOT / "sniper_tracker.db"))     # autocommit: no WAL pin
    sample = closes(ledger, a.chain, a.n)
    if a.chain == "4663":
        url = os.environ["MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663"]
        rows, basis = evm_fees(url, sample)
        approvals = sorted(g * p / 1e18 for g, p in ledger.execute(
            "SELECT gas_used, effective_gas_price_wei FROM memecoin_evm_preapprovals "
            "WHERE gas_used IS NOT NULL AND effective_gas_price_wei IS NOT NULL"))
        basis["approval_fee_eth_median"] = statistics.median(approvals) if approvals else None
    else:
        from slot_lane_send_monitor import pick_research_url
        rows, basis = solana_fees(pick_research_url(os.environ), sample, ledger)
    usd = sorted(r["fee_usd"] for r in rows if r["fee_usd"] is not None)
    print(json.dumps({"chain": a.chain, "closes": len(rows), **basis,
                      "fee_usd_median": statistics.median(usd) if usd else None,
                      "fee_usd_p90": usd[int(0.9 * len(usd))] if usd else None,
                      "clip_floor_usd_at_2pct": 50 * statistics.median(usd) if usd else None,
                      "rows": rows}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
