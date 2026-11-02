#!/usr/bin/env python3
"""Forward-verify a roster handle's claimed Solana address against what that handle SIGNALLED.

THE QUESTION (operator, 2026-09-22): "we found addresses of wallets — can we get their signals
before J7?" A chain-direct lane is only worth building on an address we have PROVEN belongs to the
trader. The Axiom directory supplies (x_handle -> address); the join key is a NAME, and a name is
not proof. [[finding-the-fomo-profile-address-is-not-the-trader-2026-09-16]] is the precedent:
profile addresses contradicted 50 of 55.

THE TEST IS FORWARD, NOT BACKWARD. For each handle we take the coins that handle actually signalled
on J7 in the recent window, then ask the chain whether the claimed address holds a token account for
those exact mints. A stranger's address cannot hold three of a specific trader's recent calls by
chance, so a few hits prove the link; the coins come from OUR event log, so the directory cannot
influence what we test it against.

A CLOSED ACCOUNT IS NOT A DENIAL. Memecoin traders close an ATA to reclaim rent, and a zero-hit
result is therefore ambiguous between "never held" and "held and closed". This reports NOT_CONFIRMED
for zero hits and never REFUTED. A balance of 0 in a still-open account is a HIT: the account's
existence is the evidence, not its balance.

RESEARCH KEY ONLY. It reads MEMECOIN_RESEARCH_HELIUS_RPC_URL and refuses to run on the funded
custody endpoint ([[finding-research-process-burns-shared-funded-rpc-quota-2026-08-21]]: a research
loop on the shared key burned ~7k funded quota). publicnode is not usable here — it answers
getTokenAccountsByOwner with 403.

    python3 scripts/research/memecoin_forward_address_verify.py --days 7 --mints 10
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

VERIFIED, WEAK, NOT_CONFIRMED, NO_EVIDENCE = "VERIFIED", "WEAK", "NOT_CONFIRMED", "NO_EVIDENCE"


def _rpc_factory(url, *, sleep_s=0.25):
    def call(method, params, _retry=True):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params}).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                out = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and _retry:
                time.sleep(2.0)
                return call(method, params, _retry=False)
            raise
        time.sleep(sleep_s)
        return out
    return call


def verdict_for(hits, tested, errors):
    if tested == 0:
        return NO_EVIDENCE
    if hits >= 3:
        return VERIFIED
    if hits >= 1:
        return WEAK
    # Zero hits. Only meaningful when we actually got answers for a decent sample.
    return NOT_CONFIRMED if (tested - errors) >= 5 else NO_EVIDENCE


# --- persistence (2026-09-23) -----------------------------------------------------------------
#
# WHY THIS EXISTS. The 2026-09-22 run proved 18 Solana owner addresses on chain and wrote them
# NOWHERE: this script only ever emitted an optional --json-out, and `onchain_wallet_watch.py`
# subscribes from `memecoin_wallet_addresses`. Measured 2026-09-23: that table held TWO confirmed
# Solana addresses, so the watch covered 1 armed wallet of 37 and saw 2.7% of the roster's J7
# Solana buys. The gap was persistence, not measurement.
#
# WHAT COUNTS AS PROVEN, and it is NOT the verdict. `verdict_for` promotes a WEAK or NOT_CONFIRMED
# row to VERIFIED when the cross-source `pump:` id agrees, which is good corroboration but is NOT a
# chain proof: such a row can carry ZERO hits. Only `hits >= MIN_PROVEN_HITS` is written here.
MIN_PROVEN_HITS = 3
WRITE_METHOD = "forward_signal_verify"
WRITE_STATUS = "live_confirmed"      # one of the two states onchain_wallet_watch.py subscribes to


def persist_candidates(conn, results, *, min_hits=MIN_PROVEN_HITS):
    """Chain-proven results turned into writable rows, with the armed identity as the key.

    A handle can carry several `source_user_id`s (a uuid and a `pump:` one). The table's primary key
    is `source_user_id`, so the row is keyed on the ARMED identity when there is one — that is the
    id the roster and the runner use.
    """
    out = []
    for row in results or ():
        if int(row.get("hits") or 0) < min_hits:
            continue
        handle, address = row.get("handle"), row.get("address")
        if not handle or not address:
            continue
        ids = [r[0] for r in conn.execute(
            "SELECT source_user_id FROM memecoin_wallets WHERE LOWER(handle)=LOWER(?) "
            "ORDER BY real_approved DESC, (source_user_id LIKE 'pump:%') ASC", (handle,))]
        if not ids:
            continue
        out.append({"source_user_id": ids[0], "handle": handle, "address": address,
                    "hits": int(row.get("hits") or 0), "tested": int(row.get("tested") or 0),
                    "cross_source": row.get("cross_source")})
    return out


def persist_rows(conn, candidates, *, now_iso, confirm=False):
    """(planned, written). Idempotent: a row already carrying this address AND a subscribed status
    is skipped, so a second run writes nothing."""
    planned, written = [], 0
    for cand in candidates:
        existing = conn.execute(
            "SELECT address, verify_status FROM memecoin_wallet_addresses WHERE source_user_id=?",
            (cand["source_user_id"],)).fetchone()
        note = "new"
        if existing:
            old_address, old_status = existing[0], existing[1]
            if old_address == cand["address"] and old_status in ("fomoscan_confirmed",
                                                                 WRITE_STATUS):
                continue                       # already subscribed to this exact address
            note = ("supersedes %s %s" % (old_status, str(old_address)[:12])
                    if old_address != cand["address"] else "raises %s" % old_status)
        detail = json.dumps({"proof": "forward signal verification",
                             "hits": cand["hits"], "tested": cand["tested"],
                             "cross_source": cand["cross_source"],
                             "supersedes": (list(existing) if existing else None)},
                            sort_keys=True)
        planned.append(dict(cand, note=note))
        if not confirm:
            continue
        conn.execute(
            "INSERT INTO memecoin_wallet_addresses(source_user_id, handle, network_id, address,"
            " resolved_at, method, coins_held, coins_checked, verify_status, verify_detail) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_user_id) DO UPDATE SET "
            "handle=excluded.handle, network_id=excluded.network_id, address=excluded.address, "
            "resolved_at=excluded.resolved_at, method=excluded.method, "
            "coins_held=excluded.coins_held, coins_checked=excluded.coins_checked, "
            "verify_status=excluded.verify_status, verify_detail=excluded.verify_detail",
            (cand["source_user_id"], cand["handle"], "solana", cand["address"], str(now_iso),
             WRITE_METHOD, cand["hits"], cand["tested"], WRITE_STATUS, detail))
        written += 1
    if confirm:
        conn.commit()
    return planned, written


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--mints", type=int, default=10, help="mints probed per handle")
    ap.add_argument("--handle", help="verify one handle only")
    ap.add_argument("--json-out", help="write the full result table here")
    ap.add_argument("--persist", action="store_true",
                    help="write chain-proven rows into memecoin_wallet_addresses "
                         "so onchain_wallet_watch.py subscribes to them")
    ap.add_argument("--confirm", action="store_true",
                    help="actually write; without it --persist is a dry run")
    args = ap.parse_args(argv)

    from sniper.util.envfiles import load_env_files
    load_env_files(str(ROOT))
    url = os.getenv("MEMECOIN_RESEARCH_HELIUS_RPC_URL")
    if not url:
        print("MEMECOIN_RESEARCH_HELIUS_RPC_URL is unset; refusing to use a funded endpoint")
        return 2
    custody = os.getenv("MEMECOIN_SOLANA_RPC_URL") or ""
    if custody and url.strip() == custody.strip():
        print("research URL equals the funded custody URL; refusing")
        return 2
    rpc = _rpc_factory(url)

    ledger = ROOT / "sniper_tracker.db"                 # anchored at the repo root, never the CWD
    conn = sqlite3.connect("file:%s?mode=ro" % ledger, uri=True)
    conn.row_factory = sqlite3.Row
    where = "AND LOWER(d.x_handle)=LOWER(?)" if args.handle else ""
    params = (args.handle,) if args.handle else ()
    pairs = conn.execute(
        "SELECT DISTINCT d.x_handle, d.address FROM axiom_trader_directory d "
        "JOIN memecoin_wallets w ON LOWER(w.handle)=LOWER(d.x_handle) "
        "WHERE d.chain='solana' AND w.real_approved=1 %s ORDER BY d.x_handle" % where,
        params).fetchall()
    print("armed handles with a Solana directory address: %d\n" % len(pairs))

    results = []
    for pair in pairs:
        handle, addr = pair["x_handle"], pair["address"]
        # EVERY identity for this handle. The same person carries a uuid (J7/fomo) row AND a
        # `pump:` row, and their events split across the two -- reading one alone finds nothing.
        sids = [r[0] for r in conn.execute(
            "SELECT source_user_id FROM memecoin_wallets WHERE LOWER(handle)=LOWER(?)", (handle,))]
        mints = []
        if sids:
            q = ("SELECT DISTINCT mint FROM memecoin_events WHERE network_id='solana' "
                 "AND mint IS NOT NULL AND created_at>=datetime('now',?) "
                 "AND source_user_id IN (%s) ORDER BY created_at DESC LIMIT ?"
                 % ",".join("?" * len(sids)))
            mints = [r[0] for r in conn.execute(q, ("-%d days" % args.days, *sids, args.mints))]

        # A `pump:` source_user_id IS the trader's Solana address, so J7 and the Axiom directory
        # are two independent sources for the same fact. Free corroboration, no RPC needed.
        pump = next((s.split(":", 1)[1] for s in sids if s.startswith("pump:")), None)
        cross = None if pump is None else (pump == addr)

        hits = errors = 0
        held = []
        for mint in mints:
            try:
                out = rpc("getTokenAccountsByOwner", [addr, {"mint": mint},
                                                      {"encoding": "jsonParsed"}])
                if "error" in out:
                    errors += 1
                    continue
                accounts = (out.get("result") or {}).get("value") or []
                if accounts:
                    hits += 1
                    held.append(mint)
            except Exception:
                errors += 1
        v = verdict_for(hits, len(mints), errors)
        if cross is True and v in (WEAK, NOT_CONFIRMED, NO_EVIDENCE):
            v = VERIFIED            # two independent sources naming one address outrank a thin probe
        results.append({"handle": handle, "address": addr, "verdict": v, "hits": hits,
                        "tested": len(mints), "errors": errors,
                        "cross_source": cross, "held_mints": held})
        print("  %-20s %-14s hits %2d/%-2d err %d  cross=%-5s  %s"
              % (handle, addr[:12], hits, len(mints), errors,
                 "n/a" if cross is None else ("AGREE" if cross else "DISAGREE"), v))

    print("\n=== SUMMARY ===")
    for v in (VERIFIED, WEAK, NOT_CONFIRMED, NO_EVIDENCE):
        names = [r["handle"] for r in results if r["verdict"] == v]
        print("  %-14s %2d  %s" % (v, len(names), ", ".join(names) if names else "-"))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2, sort_keys=True))
        print("\nwrote %s" % args.json_out)

    if args.persist:
        from sniper.util.clock import real_utc_now_iso
        # The same ledger the rest of the script reads. Read-only unless --confirm, so a dry run
        # cannot write even if the code below is wrong.
        ledger = ROOT / "sniper_tracker.db"
        write_conn = (sqlite3.connect(str(ledger), timeout=30) if args.confirm
                      else sqlite3.connect("file:%s?mode=ro" % ledger, uri=True))
        try:
            cands = persist_candidates(write_conn, results)
            planned, written = persist_rows(write_conn, cands, now_iso=real_utc_now_iso(),
                                            confirm=args.confirm)
            print("\n=== PERSIST (%s) ===" % ("CONFIRMED" if args.confirm else "dry run"))
            print("chain-proven (hits >= %d): %d of %d results"
                  % (MIN_PROVEN_HITS, len(cands), len(results)))
            for row in planned:
                print("  %-20s %-44s hits %2d/%-2d  %s"
                      % (row["handle"], row["address"], row["hits"], row["tested"], row["note"]))
            if not planned:
                print("  nothing to write; every proven address is already subscribed")
            print("rows written: %d%s" % (written, "" if args.confirm else " (dry run)"))
        finally:
            write_conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
