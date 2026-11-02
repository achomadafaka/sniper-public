#!/usr/bin/env python3
"""Verify a Solana address for a Fomo profile by its own buy receipts, and optionally record it.

For the profile's N most recent distinct-mint Solana buys (memecoin_events), read the candidate
address's transactions within +-FOMO_BUY_WINDOW_S of each buy (Helius getTransactionsForAddress on
the RESEARCH key) and look for one in which the address's own balance of that mint rose. The same
read runs over a CONTROL profile's buys, which must match none. The verdict and the evidence row
are memecoin_address_registry.fomo_buy_receipt_status / record_fomo_buy_receipt_match.

Refuses while a real Solana position is open and stops if one opens (the research key shares this
host's Solana budget). Default is a dry run; --write records a verified row (registry only: it
never arms or disarms a wallet, but the Solana watcher subscribes verified addresses of armed
profiles at its next refresh).

    python3 scripts/research/verify_solana_fomo_buys.py --handle Ew4rdo \
        --address V8c7i9prMkHT7fdMScUdZcZUjPmVJKFpTiVbzbaE2Bt --control-handle smol_intern
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE = Path(os.getenv("SNIPER_STATE_ROOT") or REPO)
sys.path.insert(0, str(REPO))

from sniper.services import memecoin_address_registry as reg  # noqa: E402


def recent_buys(conn, uid, n):
    out, seen = [], set()
    for mint, created in conn.execute(
            "SELECT mint, created_at FROM memecoin_events WHERE source_user_id=? AND "
            "network_id='solana' AND side='buy' ORDER BY created_at DESC", (uid,)):
        if mint and mint not in seen:
            seen.add(mint)
            out.append((mint, created))
            if len(out) >= n:
                break
    return out


def check(rpc, address, buys, guard):
    """[(mint, signature or None)] for each buy."""
    out = []
    for mint, created in buys:
        guard()
        t = int(datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp())
        res = rpc("getTransactionsForAddress", [address, {
            "transactionDetails": "full", "sortOrder": "asc", "limit": 50, "encoding": "json",
            "maxSupportedTransactionVersion": 1, "commitment": "confirmed",
            "filters": {"blockTime": {"gte": t - reg.FOMO_BUY_WINDOW_S,
                                      "lte": t + reg.FOMO_BUY_WINDOW_S},
                        "tokenAccounts": "balanceChanged"}}]) or {}
        sig = next((tx["transaction"]["signatures"][0] for tx in res.get("data") or ()
                    if reg.owner_token_rise(tx, address, mint) > 0), None)
        out.append((mint, sig))
        time.sleep(0.2)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--handle", required=True)
    ap.add_argument("--address", required=True)
    ap.add_argument("--control-handle", required=True)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--write", action="store_true", help="record a verified row")
    args = ap.parse_args()
    from sniper.util.envfiles import load_env_files
    load_env_files(str(STATE))
    url = os.environ.get("MEMECOIN_RESEARCH_HELIUS_RPC_URL", "")
    if "helius" not in url:
        sys.exit("no research Helius key")
    db = STATE / "sniper_tracker.db"
    ro = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)

    def guard():
        if ro.execute("SELECT COUNT(*) FROM memecoin_positions WHERE is_open=1 AND "
                      "execution_mode='real' AND network_id='solana'").fetchone()[0]:
            sys.exit("held: a real Solana position is open")

    def rpc(method, params):
        req = urllib.request.Request(url, data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "curl/8.5.0"})
        answer = json.loads(urllib.request.urlopen(req, timeout=30).read())
        if answer.get("error"):
            raise RuntimeError(str(answer["error"])[:160])
        return answer.get("result")

    def uid_of(handle):
        row = ro.execute("SELECT source_user_id FROM memecoin_wallets WHERE handle=?",
                         (handle,)).fetchone()
        if row is None:
            sys.exit("unknown handle %s" % handle)
        return row[0]
    uid, control_uid = uid_of(args.handle), uid_of(args.control_handle)
    guard()
    checks = check(rpc, args.address, recent_buys(ro, uid, args.n), guard)
    control = check(rpc, args.address, recent_buys(ro, control_uid, args.n), guard)
    status = reg.fomo_buy_receipt_status(checks, control)
    print("%s %s: %d/%d own buys matched, control %s %d/%d -> %s" % (
        args.handle, args.address, sum(1 for _m, s in checks if s), len(checks),
        args.control_handle, sum(1 for _m, s in control if s), len(control), status or "NOT verified"))
    for mint, sig in checks:
        print("   own     %s %s" % (mint[:8], sig or "-"))
    if not args.write or status is None:
        return
    from sniper.util.clock import real_utc_now_iso
    rw = sqlite3.connect(str(db), timeout=30)
    try:
        changed = reg.record_fomo_buy_receipt_match(
            rw, source_user_id=uid, handle=args.handle, address=args.address, checks=checks,
            control_checks=control, control_user_id=control_uid, now_iso=real_utc_now_iso(),
            actor="verify_solana_fomo_buys")
        rw.commit()
    finally:
        rw.close()
    print("registry:", changed or "unchanged")


if __name__ == "__main__":
    main()
