#!/usr/bin/env python3
"""Which PumpSwap pools did the slot lane miss, and could a prewarm have known them?

Question 2026-09-27 (Codex lemon/Dougherty S+7 diagnosis, Alpha task 1): the lane refuses a
PumpSwap buy `pool_unknown` when its pool cache has not learned the pool yet. A pump-migrated pool
sits at a DERIVABLE address, ["pool", u16 0, pool-authority(mint), mint, wSOL] under pAMM, so a
prewarm needs only a list of candidate MINTS. For every `pool_unknown` line in the runner log:

  mint       resolved from our own ledger rows for the same wallet near the refusal (a candidate
             whose canonical pool matches the logged 8-char pool prefix), else from the owner's
             transaction at slot S on the research key
  canonical  the pool is the derived pump-migration pool (a non-canonical pool cannot be prewarmed
             from a mint)
  pool_age_s refusal time minus the pool's first transaction (its creation)
  seen_*     seconds between the mint's FIRST appearance in a ledger table (any wallet, any lane)
             and the refusal; None when it never appeared before the refusal

Research key only, read-only, no ledger write.

    python3 scripts/research/slot_lane_pool_miss_census.py
    python3 scripts/research/slot_lane_pool_miss_census.py --selftest
"""
import argparse
import contextlib
import json
import re
import sqlite3
import struct
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
import funded_exit_truth as fet  # noqa: E402

fet.SPACING_S = 0.25
LEDGER = f"file:{REPO / 'sniper_tracker.db'}?mode=ro"
LOG = REPO / "logs" / "memecoin_runner.log"
LINE = re.compile(r"^(\S+) \[memecoin\] slot lane (\S+) (\S{1,8}) S=(\d+) -> rejected pool_unknown")
WINDOW_S = 180
SOURCES = {
    "events": "SELECT MIN(created_at) FROM memecoin_events WHERE mint=?",
    "detections": "SELECT MIN(detected_at) FROM onchain_wallet_detections WHERE mint=?",
    "feed_v2": "SELECT MIN(observed_at) FROM memecoin_feed_observations_v2 "
               "WHERE network_id='solana' AND mint=?",
    "attempts": "SELECT MIN(decision_started_at) FROM memecoin_entry_attempts "
                "WHERE network_id='solana' AND mint=?",
}


def canonical_pool(mint):
    from sniper.execution.pump_curve_buy import WSOL_MINT
    from sniper.execution.pump_native_exit import PUMP_PROGRAM, _pda, _pk
    from sniper.execution.pumpswap_buy import AMM_PROGRAM
    auth = _pda([b"pool-authority", bytes(_pk(mint))], PUMP_PROGRAM)
    return str(_pda([b"pool", struct.pack("<H", 0), bytes(auth), bytes(_pk(mint)),
                     bytes(_pk(WSOL_MINT))], AMM_PROGRAM))


def ts(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00").replace(" ", "T"))


def misses(path=LOG):
    out = []
    with open(path, errors="replace") as fh:
        for line in fh:
            hit = LINE.match(line)
            if hit:
                out.append({"at": hit[1], "handle": hit[2], "pool8": hit[3], "slot": int(hit[4])})
    return out


def ledger_candidates(c, uids, at):
    """Mints these wallet ids touched in the ledger within WINDOW_S of `at`. Indexed reads only
    (events by (source_user_id, created_at)); bounds built in Python."""
    lo, hi = ((ts(at) + timedelta(seconds=d)).isoformat() for d in (-WINDOW_S, WINDOW_S))
    out = []
    for uid in uids:
        out += [r[0] for r in c.execute(
            "SELECT mint FROM onchain_wallet_detections WHERE source_user_id=? "
            "AND detected_at BETWEEN ? AND ? UNION SELECT mint FROM memecoin_events "
            "WHERE source_user_id=? AND created_at BETWEEN ? AND ?",
            (uid, lo, hi, uid, lo, hi)) if r[0]]
    return out


def chain_mint(owner, slot, pool8):
    """The mint of the owner's PumpSwap buy at `slot`, from its own transaction, or None."""
    res = fet.rpc("getTransactionsForAddress", [owner, {
        "transactionDetails": "full", "sortOrder": "asc", "limit": 20,
        "filters": {"slot": {"gte": slot, "lte": slot}, "tokenAccounts": "balanceChanged"},
        "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}]) or {}
    for tx in res.get("data") or ():
        for bal in (tx.get("meta") or {}).get("postTokenBalances") or ():
            mint = bal.get("mint")
            if mint and bal.get("owner") == owner and canonical_pool(mint).startswith(pool8):
                return mint
    return None


def pool_created_at(pool):
    res = fet.rpc("getTransactionsForAddress", [pool, {
        "transactionDetails": "signatures", "sortOrder": "asc", "limit": 1}]) or {}
    rows = res.get("data") or []
    return rows[0].get("blockTime") if rows else None


def main(no_chain=False):
    from sniper.services.memecoin_address_registry import armed_verified_owners
    rows = misses()
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=30)) as c:
        uid_owner = {}
        everyone = [r[0] for r in c.execute("SELECT source_user_id FROM memecoin_wallets")]
        for owner, (uid, handle) in (armed_verified_owners(c, armed=everyone) or {}).items():
            uid_owner[str(handle or "").lower()] = owner
        uids = {}
        for uid, handle in c.execute("SELECT source_user_id, handle FROM memecoin_wallets"):
            uids.setdefault(str(handle or "").lower(), []).append(uid)
        for r in rows:
            mint = next((m for m in ledger_candidates(c, uids.get(r["handle"].lower(), []), r["at"])
                         if canonical_pool(m).startswith(r["pool8"])), None)
            r["mint_from"] = "ledger" if mint else None
            if mint is None and not no_chain and uid_owner.get(r["handle"].lower()):
                mint = chain_mint(uid_owner[r["handle"].lower()], r["slot"], r["pool8"])
                r["mint_from"] = "chain" if mint else "unresolved"
            r["mint"] = mint
            if not mint:
                continue
            r["pool"] = canonical_pool(mint)
            refused = ts(r["at"])
            for name, sql in SOURCES.items():
                first = c.execute(sql, (mint,)).fetchone()[0]
                r["seen_" + name] = ((refused - ts(first)).total_seconds()
                                     if first and ts(first) < refused else None)
    for pool in {r["pool"] for r in rows if r.get("pool")} if not no_chain else ():
        created = pool_created_at(pool)
        for r in rows:
            if r.get("pool") == pool and created:
                r["pool_age_s"] = ts(r["at"]).timestamp() - created
    for r in rows:
        print(json.dumps(r))
    resolved = [r for r in rows if r.get("mint")]
    prior = [r for r in resolved if any(r.get("seen_" + s) for s in SOURCES)]
    print("misses %d, distinct pools %d, resolved %d (all canonical by construction), "
          "mint in ledger before the miss %d" % (
              len(rows), len({r["pool8"] for r in rows}), len(resolved), len(prior)),
          file=sys.stderr)
    print("429s %d" % fet.RATE_429[0], file=sys.stderr)


def selftest():
    assert canonical_pool("6UTgBWACzB1zX2GA2uEvVn6325XE99wNvkVTXv5qpump") == \
        "qNadZjDebjUwHS5ZRcLTLtoGk4wAU15kydQLFSJGUUe"
    assert canonical_pool("GW5xNXBtW9usH62Uimis4L5T1Fi5ceeMa9dupjTVpxaD") == \
        "HcvoKF2XajWwCzcLAvkeKNCUfSMTpu4jf65Um2bvgJPF"
    hit = LINE.match("2026-09-27T11:03:21.151403+00:00 [memecoin] slot lane fmpumpguy HcvoKF2X "
                     "S=450977816 -> rejected pool_unknown ms=0.0 head-S=None recv-S=0 land-S=None")
    assert hit and hit[3] == "HcvoKF2X" and hit[4] == "450977816"
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--no-chain", action="store_true")
    a = ap.parse_args()
    selftest() if a.selftest else main(a.no_chain)
