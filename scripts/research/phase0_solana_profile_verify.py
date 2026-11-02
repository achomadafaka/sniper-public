#!/usr/bin/env python3
"""Verify the 14 freshly-harvested fomo-profile Solana addresses (memecoin_wallet_addresses,
method='fomo_profile_solana', verify_status='unverified') against real on-chain activity, before
harvesting the remaining 41. Alpha's instruction, 2026-09-08: the Solana `address` field sits in
the SAME profile object as `evmAddress`, and the EVM version of that field was proven NOT the
executing address (0/47 receiver, 0/41 signer) -- a 100% harvest hit rate means fomo exposes a
Solana address, not that it is the right one. This answers that, per trader, with a sample size.

No fomo calls. Local DB (memecoin_events, memecoin_wallet_addresses) plus Solana RPC reads only.
The dedicated research Helius key (MEMECOIN_RESEARCH_HELIUS_RPC_URL) came back exhausted ("max
usage reached") when this was first tested 2026-09-08 -- switched to
sniper.settings.memecoin.solana_read_rpc() (MEMECOIN_SOLANA_READ_RPC_URL), the codebase's
designated free-public read path (api.mainnet-beta.solana.com, per Alpha's own URL fingerprint
check), never the three funded/custody Helius keys (MEMECOIN_SOLANA_RPC_URL,
MEMECOIN_SOLANA_RPC_POOL_URLS). That same public URL is also the LAST member of the funded pool's
rotation (a fallback if all three Helius keys degrade) -- not a reason to avoid it, but a reason to
pace it and stop outright on the first throttle rather than back off and retry.

METHOD, cheaper than fresh_buy_resolver.py's mint-wide search because the candidate address is
already known (searching for evidence FOR one address, not discovering one from scratch among all
buyers): for each (trader, mint) sample, getTokenAccountsByOwner(candidate, {mint}) finds that
trader's SPL token account for exactly this mint (0 or 1 account, no PDA math needed -- the RPC
does the lookup). If none exists, the candidate never held this mint under this owner: a clean
negative. If one exists, getSignaturesForAddress on THAT account (not the wallet, not the mint --
a single owner's balance history of a single mint, a far smaller history than either) gives real
transaction timestamps; the signature nearest the fomo-reported buy time, within WINDOW_S, is a
match.

REQUEST BUDGET, stated before running: up to 6 most-recent distinct-mint Solana buys per trader (14
traders) = <=76 sampled events. 2 RPC calls each (getTokenAccountsByOwner + getSignaturesForAddress,
skipped if no token account) => <=152 calls. Paced 0.4s apart.

Writes only its own table, phase0_solana_profile_verify.
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

DB = REPO / "sniper_tracker.db"
WINDOW_S = 180
CALL_PAUSE_S = 0.4
MAX_MINTS_PER_TRADER = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS phase0_solana_profile_verify (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_user_id TEXT, handle TEXT, candidate_address TEXT, mint TEXT,
    event_created_at TEXT, event_epoch INTEGER, token_account TEXT,
    nearest_sig_block_time INTEGER, nearest_sig_delta_s REAL,
    match INTEGER, note TEXT, created_at TEXT NOT NULL
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _epoch(iso_ts):
    try:
        return int(datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, AttributeError):
        return None


class Throttled(RuntimeError):
    """The endpoint refused with a 429. Never retried -- the caller stops the whole pass."""


def research_rpc_url():
    from sniper.util.envfiles import load_env_files
    load_env_files(str(REPO))
    from sniper.settings.memecoin import solana_read_rpc
    url = solana_read_rpc()
    if not url:
        sys.exit("REFUSE: solana_read_rpc() returned empty -- never falling back to the "
                 "funded/custody Solana RPC for a research loop (2026-08-21 precedent).")
    return url


def _rpc(url, method, params, tries=2):
    import urllib.error
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())
            if "error" in d:
                return None, d["error"]
            return d.get("result"), None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise Throttled(f"{method}: HTTP 429 -- stopping, not retrying")
            if attempt == tries - 1:
                return None, str(e)
            time.sleep(1.0)
        except Exception as e:
            if attempt == tries - 1:
                return None, str(e)
            time.sleep(1.0)
    return None, "failed"


def token_account_for(url, owner, mint):
    time.sleep(CALL_PAUSE_S)
    res, err = _rpc(url, "getTokenAccountsByOwner",
                     [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
    if err is not None:
        return None, err
    accts = (res or {}).get("value") or []
    return (accts[0]["pubkey"] if accts else None), None


def nearest_signature_time(url, token_account, target_epoch, limit=30):
    time.sleep(CALL_PAUSE_S)
    res, err = _rpc(url, "getSignaturesForAddress", [token_account, {"limit": limit}])
    if err is not None:
        return None, err
    sigs = [s for s in (res or []) if s.get("blockTime")]
    if not sigs:
        return None, None
    nearest = min(sigs, key=lambda s: abs(s["blockTime"] - target_epoch))
    return nearest["blockTime"], None


def distinct_mints_for_trader(conn, uid, limit):
    return conn.execute(
        "SELECT mint, MAX(created_at) AS created_at FROM memecoin_events "
        "WHERE source_user_id=? AND side='buy' AND network_id='solana' GROUP BY mint "
        "ORDER BY created_at DESC LIMIT ?", (uid, limit)).fetchall()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-mints", type=int, default=MAX_MINTS_PER_TRADER)
    args = ap.parse_args()

    url = research_rpc_url()
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)

    traders = c.execute(
        "SELECT source_user_id, handle, address FROM memecoin_wallet_addresses "
        "WHERE method='fomo_profile_solana' ORDER BY handle").fetchall()
    print(f"traders to verify: {len(traders)}  max mints/trader: {args.max_mints}", flush=True)

    per_trader = {}
    throttled = False
    for t in traders:
        if throttled:
            break
        uid, handle, addr = t["source_user_id"], t["handle"], t["address"]
        mints = distinct_mints_for_trader(c, uid, args.max_mints)
        matches, checked = 0, 0
        for m in mints:
            mint, created_at = m["mint"], m["created_at"]
            epoch = _epoch(created_at)
            if epoch is None:
                continue
            checked += 1
            try:
                ta, err = token_account_for(url, addr, mint)
            except Throttled as exc:
                print(f"THROTTLED at {handle} {mint[:10]}: {exc} -- stopping the whole pass, "
                      f"not retrying (shared public endpoint).", flush=True)
                throttled = True
                break
            if err is not None:
                note, match, ta_out, nearest_bt, delta = "rpc_failed", 0, None, None, None
            elif ta is None:
                note, match, ta_out, nearest_bt, delta = "no_token_account", 0, None, None, None
            else:
                try:
                    bt, err2 = nearest_signature_time(url, ta, epoch)
                except Throttled as exc:
                    print(f"THROTTLED at {handle} {mint[:10]}: {exc} -- stopping the whole pass, "
                          f"not retrying (shared public endpoint).", flush=True)
                    throttled = True
                    break
                if err2 is not None:
                    note, match, ta_out, nearest_bt, delta = "rpc_failed", 0, ta, None, None
                elif bt is None:
                    note, match, ta_out, nearest_bt, delta = "token_account_no_signatures", 0, ta, None, None
                else:
                    delta = bt - epoch
                    match = 1 if abs(delta) <= WINDOW_S else 0
                    note = "ok"
                    ta_out, nearest_bt = ta, bt
            matches += match
            c.execute(
                "INSERT INTO phase0_solana_profile_verify (source_user_id, handle, "
                "candidate_address, mint, event_created_at, event_epoch, token_account, "
                "nearest_sig_block_time, nearest_sig_delta_s, match, note, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, handle, addr, mint, created_at, epoch, ta_out, nearest_bt, delta, match,
                 note, now_iso()))
            c.commit()
            print(f"{handle:<15} mint={mint[:10]} match={match} delta_s={delta} note={note}",
                  flush=True)
        if checked:
            per_trader[handle] = (matches, checked)

    print("\n=== SOLANA PROFILE-ADDRESS VERIFICATION REPORT ===")
    for handle, (m, n) in per_trader.items():
        flag = ""
        if n <= 1:
            flag = "  [THIN -- 1-of-1 or 0-of-0, do not treat as confirmed]"
        elif m == n and n >= 2:
            flag = "  [strong -- full agreement across multiple coins]"
        elif m == 0 and n >= 2:
            flag = "  [strong negative -- zero agreement across multiple coins]"
        print(f"  {handle:<15} {m}/{n}{flag}")
    total_m = sum(m for m, n in per_trader.values())
    total_n = sum(n for m, n in per_trader.values())
    print(f"\noverall: {total_m}/{total_n} sampled events matched")


if __name__ == "__main__":
    main()
