#!/usr/bin/env python3
"""Backfill quote-asset residue credits (operator order 2026-10-07 ~15:30Z; the operator runs --confirm).

What it books: every real Solana exit since MEMECOIN_QUOTE_RESIDUE_CREDITS_SINCE (2026-09-09, when the
native pump exit went live) is read once for the quote asset it left; every sold Other Holdings sell
that sold recorded residue is split pro rata by atomic amount into those positions' realized_pnl,
append-only (memecoin_quote_residue_credits). The foreign share of a sell (airdrops, older dust) is
recorded and never booked. Same code as the runner's passes (sniper/services/memecoin_quote_residue).

    python3 scripts/memecoin_quote_residue_backfill.py              # dry run: writes NOTHING to the ledger
    python3 scripts/memecoin_quote_residue_backfill.py --confirm    # books it (needs the deployed schema)

Dry run: the ledger rows the passes read (real Solana positions and their fills, Solana holding
sells, residue rows) are copied into an in-memory database and the passes run there.
Receipts: the research Helius key through its shared pacer (scripts/research/funded_exit_truth.rpc),
paused while a real Solana position is open (flat-book rule), cached in --cache so --confirm books
exactly the receipts the dry run showed. Rollback: --revert reverses every unreverted credit.
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))

from sniper.persistence import db_memecoin as m  # noqa: E402
from sniper.services import memecoin_quote_residue as Q  # noqa: E402

DEFAULT_CACHE = Path.home() / ".cache" / "sniper" / "quote_residue_receipts.json"
COPIED = (
    ("memecoin_positions", "execution_mode='real' AND network_id='solana'"),
    ("memecoin_fills", "position_id IN (SELECT id FROM memecoin_positions WHERE execution_mode='real' "
                       "AND network_id='solana')"),
    ("memecoin_holding_sell_intents", "network_id='solana'"),
    ("memecoin_quote_residue", "network_id='solana'"),
    ("memecoin_quote_residue_reads", "1"),
    ("memecoin_quote_residue_credits", "1"),
)


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def copy_ledger(src):
    """The rows the passes read, in a fresh in-memory ledger with the current schema."""
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(mem)
    mem.commit()
    have = _tables(src)
    for table, where in COPIED:
        if table not in have:
            continue
        dst_cols = {r[1] for r in mem.execute(f"PRAGMA table_info({table})")}
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})") if r[1] in dst_cols]
        rows = src.execute(f"SELECT {','.join(cols)} FROM {table} WHERE {where}").fetchall()
        mem.executemany(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                        [tuple(r) for r in rows])
    mem.commit()
    return mem


def research_receipts(ledger, cache_path):
    """sig -> parsed receipt, cached; reads wait while a real Solana position is open."""
    import funded_exit_truth as fet
    fet.SPACING_S = 0.5
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    fetched = [0]

    def flat():
        return ledger.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
                              "AND network_id='solana' AND is_open=1").fetchone()[0] == 0

    def fn(sig):
        if sig in cache:
            return cache[sig]
        while not flat():
            time.sleep(15)
        tx = fet.rpc("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                              "maxSupportedTransactionVersion": 1}])
        if tx is not None:                      # a miss is not cached: it is re-read next run
            cache[sig] = tx
        fetched[0] += 1
        if fetched[0] % 50 == 0:
            cache_path.write_text(json.dumps(cache))
            print("  %d receipts read" % fetched[0], file=sys.stderr, flush=True)
        return tx
    fn.save = lambda: (cache_path.parent.mkdir(parents=True, exist_ok=True),
                       cache_path.write_text(json.dumps(cache)))
    return fn


def owner_from(conn, receipt_fn):
    """The funded wallet = the fee payer of our own exit transactions (no keypair is read)."""
    row = conn.execute("SELECT f.tx_sig FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id "
                       "WHERE p.execution_mode='real' AND p.network_id='solana' AND f.kind<>'entry' "
                       "AND f.tx_sig IS NOT NULL ORDER BY f.created_at DESC LIMIT 1").fetchone()
    tx = receipt_fn(row[0]) if row else None
    keys = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    signers = [k.get("pubkey") for k in keys if isinstance(k, dict) and k.get("signer")]
    if not signers:
        raise SystemExit("could not read the funded wallet from our latest exit receipt")
    return signers[0]


def run(conn, receipt_fn, owner, now_iso):
    while True:
        done = Q.read_exit_residue_once(conn, receipt_fn=receipt_fn, owner=owner, now_iso=now_iso,
                                        limit=200, force=True)
        if not done:
            break
    return Q.credit_sells_once(conn, receipt_fn=receipt_fn, owner=owner, now_iso=now_iso, force=True)


def report(conn, sells, before):
    tick = {r[0]: r[1] for r in conn.execute("SELECT id, ticker FROM memecoin_positions")}
    after = {r[0]: r[1] for r in conn.execute("SELECT id, realized_pnl FROM memecoin_positions")}
    credits = {}
    for r in conn.execute("SELECT position_id, SUM(usd) FROM memecoin_quote_residue_credits "
                          "WHERE reverted_at IS NULL GROUP BY position_id"):
        credits[r[0]] = r[1]
    print("%-8s %-12s %12s %10s %12s" % ("position", "ticker", "booked", "credit", "new"))
    for pid in sorted(credits):
        print("%-8s %-12s %12.4f %+10.4f %12.4f" % (pid, (tick.get(pid) or "?")[:12],
                                                    before.get(pid) or 0.0, credits[pid], after.get(pid) or 0.0))
    print("TOTAL credit %.4f over %d positions" % (sum(credits.values()), len(credits)))
    foreign = {}
    for r in conn.execute("SELECT intent_id, MAX(foreign_usd) FROM memecoin_quote_residue_credits "
                          "WHERE reverted_at IS NULL GROUP BY intent_id"):
        foreign[r[0]] = r[1] or 0.0
    print("foreign (never booked): $%.4f over %d sells" % (sum(foreign.values()), len(foreign)))
    for s in sells:
        if s["state"] != "credited":
            print("sell %s %s: %s" % (s["intent_id"][:8], s["mint"][:8], s["state"]))
    pending = m.quote_residue_by_position(conn)
    for pid, v in sorted(pending.items()):
        if v["pending_usd"] > 0:
            print("pending (unsold, not booked): position %s ~$%.4f" % (pid, v["pending_usd"]))
    for sig, state, n in conn.execute("SELECT sig, state, tries FROM memecoin_quote_residue_reads "
                                      "WHERE state<>'read'"):
        print("exit read %s %s (tries %s): its residue is UNKNOWN" % (sig[:12], state, n))
    neg = conn.execute("SELECT sig, negative_mints FROM memecoin_quote_residue_reads "
                       "WHERE negative_mints IS NOT NULL").fetchall()
    for sig, mints in neg:
        print("exit %s spent a prior balance of %s (flag; not recorded)" % (sig[:12], mints))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(REPO / "sniper_tracker.db"))
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    ap.add_argument("--confirm", action="store_true", help="book into the ledger")
    ap.add_argument("--revert", action="store_true", help="reverse every unreverted credit")
    args = ap.parse_args()
    from sniper.util.envfiles import load_env_files
    load_env_files(str(Path(args.db).resolve().parent))      # the ledger's state root holds .env
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    if args.revert:
        if not args.confirm:
            raise SystemExit("--revert needs --confirm")
        conn = sqlite3.connect(args.db, timeout=30)
        n = 0
        for (cid,) in conn.execute("SELECT id FROM memecoin_quote_residue_credits WHERE reverted_at IS NULL").fetchall():
            n += bool(m.revert_quote_residue_credit(conn, cid, now_iso=now_iso, reason="backfill --revert"))
        print("reverted %d credits" % n)
        return
    ledger = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True, timeout=30)
    ledger.row_factory = sqlite3.Row
    receipts = research_receipts(ledger, Path(args.cache))
    if args.confirm:
        conn = sqlite3.connect(args.db, timeout=30)
        conn.row_factory = sqlite3.Row
        missing = {"memecoin_quote_residue_reads", "memecoin_quote_residue_credits"} - _tables(conn)
        if missing:
            raise SystemExit("the ledger has no %s yet: deploy the release first" % sorted(missing))
    else:
        conn = copy_ledger(ledger)
    before = {r[0]: r[1] for r in conn.execute("SELECT id, realized_pnl FROM memecoin_positions")}
    owner = owner_from(conn, receipts)
    try:
        sells = run(conn, receipts, owner, now_iso)
    finally:
        receipts.save()
    print("%s owner=%s since=%s" % ("CONFIRMED (ledger written)" if args.confirm else "DRY RUN (ledger untouched)",
                                    owner, Q._since()))
    report(conn, sells, before)


if __name__ == "__main__":
    main()
