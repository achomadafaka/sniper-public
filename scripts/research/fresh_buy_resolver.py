#!/usr/bin/env python3
"""Resolve fomo traders' real on-chain wallets by extracting the buyer of their FRESH buys.

Every other resolution method failed and is documented as refuted:
  - holder-intersection finds CO-HOLDERS of the same piled coins, not the trader (the resolved
    address does not even hold the trader's recent buys);
  - historical mint-signature paging is infeasible (hot pump.fun coins do >10 tx/s, so a
    79-min-old buy is >60k signatures deep);
  - timestamp correlation gives false positives from hyper-active accounts.

The one method that can work: catch the buy while it is SECONDS old. Our own fomo feed records
armed-wallet buys at ~2.5s median. At that instant the on-chain buy sits on PAGE 1 of the mint's
signatures, so we can pull it directly and read the beneficiary (the owner whose token balance
increased). Accumulate across a trader's fresh buys: the address that is the actual buyer of
>=2 of the trader's coins is the trader's wallet, confirmed by construction. This ALSO answers
the open feasibility question: if the beneficiary is a stable per-trader address, the on-chain
watch is viable; if it is ephemeral or shared across traders, it is not.

Writes only its own rows. Reads memecoin_events (our feed) and the chain.
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from sniper.persistence.db_core import get_readonly_connection
DB = REPO / "sniper_tracker.db"
HEARTBEAT = REPO / "data" / "fresh_buy_resolver.heartbeat"

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
NATIVE = "11111111111111111111111111111111"
QUOTE = {USDC, WSOL, NATIVE}
ROSTER_UIDS = {
    "aefe2ddd-c580-5245-a2f5-e4ed62f7ef10": "frankdegods",
    "5e911042-e839-569e-ab0e-2ab7c4389b95": "casino847",
    "d676a05f-0b3e-57d1-86dc-d6986927c6c8": "Aurelius0121",
    "eb960be1-cca6-5bef-9977-d7c63ed46d3b": "MasterChief117",
    "f62ddfdd-eeb1-5749-ad3b-a4cd65c319a9": "jester",
    "c05654ac-2ce2-513d-be13-76b81b34a6e1": "Thepennyflippe",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS fresh_buy_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fomo_event_id TEXT UNIQUE,
    source_user_id TEXT NOT NULL, handle TEXT, mint TEXT NOT NULL,
    feed_created_at TEXT, feed_seen_at TEXT, buy_age_at_lookup_s REAL,
    buyer_owner TEXT, token_delta REAL, block_time INTEGER,
    signer TEXT, reached INTEGER, note TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fresh_buy_resolved (
    source_user_id TEXT PRIMARY KEY, handle TEXT, address TEXT,
    coins_confirmed INTEGER, first_at TEXT, last_at TEXT, status TEXT);
"""


def rpc_url():
    for line in (REPO / ".env").read_text().splitlines():
        if line.startswith("MEMECOIN_SOLANA_RPC_URL=") and "helius" in line:
            return line.split("=", 1)[1].strip()
    raise SystemExit("no Helius RPC url")


URL = rpc_url()


def _rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(URL, data=body, headers={"content-type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode()).get("result")
        except Exception:
            if attempt == 2:
                return None
            time.sleep(0.3)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


TOKEN_PROG = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _holds(owner, mint):
    """True if `owner` currently holds a positive balance of `mint`. Filters out flippers who
    were the largest buyer in the window but already sold — a real copy-trader still holds."""
    ta = _rpc("getTokenAccountsByOwner",
              [owner, {"mint": mint}, {"encoding": "jsonParsed"}]) or {}
    for x in ta.get("value", []):
        amt = x["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
        if float(amt or 0) > 0:
            return True
    return False


def extract_buyer(mint, target_epoch, tol_s=45, pages=2, target_usd=None):
    """The buyer of `mint` nearest target_epoch that BEST matches the trade.

    Among swaps in the window, rank candidates by token delta and return the largest that still
    HOLDS the coin (a real position, not a flipped one). Returns (owner, delta, bt, signer,
    reached). A busy window has many buyers; cross-coin consistency in the caller is the real
    disambiguator — this just filters obvious non-holders."""
    before, cands = None, []
    reached = False
    for _ in range(pages):
        pg = _rpc("getSignaturesForAddress",
                  [mint, {"limit": 1000, **({"before": before} if before else {})}]) or []
        if not pg:
            break
        in_win = [s for s in pg if s.get("blockTime") and abs(s["blockTime"] - target_epoch) <= tol_s]
        for s in in_win[:60]:            # bounded: busy coins have hundreds; 60 covers the window
            reached = True
            tx = _rpc("getTransaction", [s["signature"],
                      {"maxSupportedTransactionVersion": 1, "encoding": "jsonParsed"}])
            if not tx or (tx.get("meta") or {}).get("err"):
                continue
            meta = tx["meta"]
            pre = {(b.get("owner"), b.get("mint")): float(b["uiTokenAmount"]["uiAmount"] or 0)
                   for b in meta.get("preTokenBalances", [])}
            for b in meta.get("postTokenBalances", []):
                o, m = b.get("owner"), b.get("mint")
                if m != mint or not o or o in QUOTE:
                    continue
                d = float(b["uiTokenAmount"]["uiAmount"] or 0) - pre.get((o, m), 0.0)
                if d <= 0:
                    continue
                signer = next((k.get("pubkey") for k in tx["transaction"]["message"]["accountKeys"]
                               if isinstance(k, dict) and k.get("signer")), None)
                cands.append((o, d, b_bt := s["blockTime"], signer))
        oldest = pg[-1].get("blockTime", 0)
        if oldest < target_epoch - tol_s:
            break
        before = pg[-1]["signature"]
    # largest delta that still holds the coin
    for cand in sorted(cands, key=lambda x: -x[1])[:6]:
        if _holds(cand[0], mint):
            return (*cand, reached)
    if cands:
        best = max(cands, key=lambda x: x[1])
        return (*best, reached)
    return (None, None, None, None, reached)


def run(poll_s=3.0, duration_s=None):
    wdb = sqlite3.connect(DB, timeout=30)
    wdb.executescript(SCHEMA)
    row = wdb.execute("SELECT COALESCE(MAX(id),0) FROM fresh_buy_observations").fetchone()
    ro = get_readonly_connection(str(DB))
    ro.row_factory = sqlite3.Row
    cursor = ro.execute("SELECT COALESCE(MAX(id),0) FROM memecoin_events").fetchone()[0]
    print(f"fresh-buy resolver start; watching {len(ROSTER_UIDS)} wallets; cursor={cursor}",
          flush=True)
    votes = defaultdict(lambda: defaultdict(set))   # uid -> owner -> {mints}
    t0 = time.time()
    while duration_s is None or time.time() - t0 < duration_s:
        uids = ",".join("?" * len(ROSTER_UIDS))
        rows = ro.execute(
            f"SELECT id, fomo_event_id, source_user_id, source_handle, mint, created_at, "
            f"received_at_normalized FROM memecoin_events WHERE id>? AND side='buy' "
            f"AND network_id='solana' AND source_user_id IN ({uids}) ORDER BY id",
            (cursor, *ROSTER_UIDS)).fetchall()
        for e in rows:
            cursor = e["id"]
            handle = ROSTER_UIDS.get(e["source_user_id"], e["source_handle"])
            try:
                tgt = datetime.fromisoformat(
                    (e["created_at"] or e["received_at_normalized"]).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError, AttributeError):
                continue
            age = time.time() - tgt
            owner, delta, bt, signer, reached = extract_buyer(e["mint"], tgt)
            note = "ok" if owner else ("mint_too_deep" if not reached else "no_buyer_in_window")
            try:
                wdb.execute(
                    "INSERT OR IGNORE INTO fresh_buy_observations (fomo_event_id, source_user_id, "
                    "handle, mint, feed_created_at, feed_seen_at, buy_age_at_lookup_s, buyer_owner, "
                    "token_delta, block_time, signer, reached, note, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (e["fomo_event_id"], e["source_user_id"], handle, e["mint"], e["created_at"],
                     now_iso(), age, owner, delta, bt, signer, 1 if reached else 0, note, now_iso()))
                wdb.commit()
            except Exception as exc:
                print(f"  write failed: {exc}", flush=True)
                continue
            print(f"BUY {handle:<14} {e['mint'][:12]} age={age:.0f}s -> "
                  f"buyer={owner or note} delta={delta}", flush=True)
            if owner:
                votes[e["source_user_id"]][owner].add(e["mint"])
                # promote when an owner is the buyer for >=2 distinct coins of this trader
                for own, mints in votes[e["source_user_id"]].items():
                    if len(mints) >= 2:
                        wdb.execute(
                            "INSERT OR REPLACE INTO fresh_buy_resolved VALUES (?,?,?,?,?,?,?)",
                            (e["source_user_id"], handle, own, len(mints), now_iso(), now_iso(),
                             "live_confirmed"))
                        wdb.commit()
                        print(f"  ** CONFIRMED {handle} = {own} ({len(mints)} coins) **", flush=True)
        HEARTBEAT.write_text(json.dumps({"ts": now_iso(), "cursor": cursor,
                                         "observations": row[0] + len(rows)}))
        time.sleep(poll_s)


def report():
    conn = get_readonly_connection(str(DB))
    conn.row_factory = None  # tuple rows, as before: the helper defaults to sqlite3.Row and Row does not slice
    try:
        obs = conn.execute("SELECT handle, note, COUNT(*), AVG(buy_age_at_lookup_s) "
                           "FROM fresh_buy_observations GROUP BY handle, note").fetchall()
    except sqlite3.OperationalError:
        print("no observations yet")
        return
    print("observations by wallet/outcome:")
    for h, note, n, age in obs:
        print(f"  {h:<14} {note:<20} n={n} avg_age={age:.0f}s")
    print("\nconfirmed addresses:")
    for r in conn.execute("SELECT handle, address, coins_confirmed FROM fresh_buy_resolved"):
        print(f"  {r[0]:<14} {r[1]} ({r[2]} coins)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if args.report:
        report()
    else:
        run(duration_s=args.duration)


if __name__ == "__main__":
    main()
