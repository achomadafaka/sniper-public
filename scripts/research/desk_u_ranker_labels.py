#!/usr/bin/env python3
"""Desk cohort U ranker tape labels (pre-registration 2026-10-21-desk-u-ranker §4;
sniper/services/memecoin_desk_ranker_label.py). Tape only, no provider calls. Reads the ranker's
candidates.db and producer U's swaps/gaps day files read-only; its one writer is
data/desk/ranker_labels.db (flock). Labels every ranker candidate, kept or cut, whose window has
closed; a labelled one is never rewritten.

    python3 scripts/research/desk_u_ranker_labels.py --once [--exclude-actor <another actor> ...]

Our funded Solana wallet (the desk's signer) is always excluded: the actors of tape swaps that are our
own fills (memecoin_fills.tx_sig, no key access), or, before our first fill, the public key of
MEMECOIN_SOLANA_KEYPAIR. The labeller refuses to run when neither resolves.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.services import memecoin_desk_ranker as R            # noqa: E402
from sniper.services import memecoin_desk_ranker_label as L      # noqa: E402

SETTLE_S = 30.0          # late swaps for the window's last seconds land before it is read
SPAN_S = L.ENTRY_WAIT_S + L.WINDOW_S

DDL = ("CREATE TABLE IF NOT EXISTS labels(opportunity_id TEXT PRIMARY KEY, label_version TEXT NOT NULL, "
       "ranker_version TEXT, t0 REAL NOT NULL, status TEXT NOT NULL, y2 INTEGER, d20 INTEGER, t_e REAL, "
       "e_price REAL, max_ln REAL, graduated_at REAL, exclude_hash TEXT NOT NULL, labelled_at REAL NOT NULL, "
       "gap_cause TEXT)")


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5) if Path(path).exists() else None


def _days(start, end):
    out, t = [], start
    while t <= end + 86400:
        d = utc_day(t)
        if d not in out and d <= utc_day(end):
            out.append(d)
        t += 86400
    return out


def run(desk_dir, *, now, exclude_actors=()):
    desk_dir = Path(desk_dir)
    cand = ro(desk_dir / "ranker" / "candidates.db")
    if cand is None:
        return {"labelled": 0, "pending": 0, "reason": "no_ranker"}
    out = sqlite3.connect(str(desk_dir / "ranker_labels.db"), timeout=30)
    out.execute("PRAGMA journal_mode=WAL")
    out.execute(DDL)
    if "gap_cause" not in {r[1] for r in out.execute("PRAGMA table_info(labels)")}:
        out.execute("ALTER TABLE labels ADD COLUMN gap_cause TEXT")      # a u-label-v1 file
    done = {r[0] for r in out.execute("SELECT opportunity_id FROM labels")}
    try:
        todo = [r for r in cand.execute("SELECT opportunity_id, mint, emission_at, ranker_version FROM candidates "
                                        "WHERE emission_at <= ?", (now - SPAN_S - SETTLE_S,))
                if r[0] not in done]
    finally:
        cand.close()
    if not todo:
        out.close()
        return {"labelled": 0, "pending": 0}
    start, end = min(r[2] for r in todo), max(r[2] for r in todo) + SPAN_S
    days = _days(start, end)
    swaps_db = [c for c in (ro(desk_dir / "swaps" / ("%s.db" % d)) for d in days) if c]
    gaps_db = [c for c in (ro(desk_dir / "gaps" / ("%s.db" % d)) for d in days) if c]
    exclude_hash = hashlib.sha256(json.dumps(sorted(exclude_actors)).encode()).hexdigest()[:16]
    counts = {}
    try:
        received = [r[0] for c in swaps_db for r in c.execute(
            "SELECT received_at FROM swaps WHERE received_at BETWEEN ? AND ?", (start, end))]
        silences = L.tape_silences(received, start=start, end=end)
        gaps = [(r[0], r[1]) for c in gaps_db for r in c.execute(
            "SELECT at, cause FROM gaps WHERE at BETWEEN ? AND ?", (start, end))]
        rows = []
        for oid, mint, t0, version in todo:
            swaps = [(r[0], R.price(r[1], r[2]), r[3], r[4]) for c in swaps_db for r in c.execute(
                "SELECT received_at, virtual_quote, virtual_token, actor, real_token FROM swaps "
                "WHERE mint=? AND received_at BETWEEN ? AND ?", (mint, t0, t0 + SPAN_S))]
            got = L.label(swaps, t0=t0, silences=silences, gaps=gaps, exclude_actors=exclude_actors)
            counts[got["status"]] = counts.get(got["status"], 0) + 1
            rows.append((oid, got["label_version"], version, t0, got["status"], got["y2"], got["d20"], got["t_e"],
                         got["e_price"], got["max_ln"], got["graduated_at"], exclude_hash, now, got["gap_cause"]))
    finally:
        for c in swaps_db + gaps_db:
            c.close()
    with out:
        out.executemany("INSERT OR IGNORE INTO labels(opportunity_id, label_version, ranker_version, t0, status, "
                        "y2, d20, t_e, e_price, max_ln, graduated_at, exclude_hash, labelled_at, gap_cause) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    out.close()
    return {"labelled": len(rows), "by_status": counts}


def fill_actors(ledger_path, desk_dir):
    """Our Solana wallet with zero key access (Gamma): the actors of the U tape swaps whose signature is
    one of our own funded fills (memecoin_fills.tx_sig, network solana, on a real position). Exact for every recorded fill;
    empty before our first fill on the tape. The ledger is read read-only, one short query."""
    from sniper.persistence.db_core import get_readonly_connection
    days = sorted(p.stem for p in (Path(desk_dir) / "swaps").glob("*.db"))
    if not days or not Path(ledger_path).exists():
        return set()
    conn = get_readonly_connection(str(ledger_path))
    try:
        # real positions only (Gamma): a paper path that stored a SOURCE's signature in tx_sig would
        # otherwise exclude a tracked wallet from every label
        sigs = [r[0] for r in conn.execute(
            "SELECT DISTINCT f.tx_sig FROM memecoin_fills f JOIN memecoin_positions p ON p.id = f.position_id "
            "WHERE f.network_id='solana' AND f.tx_sig IS NOT NULL AND f.created_at >= ? "
            "AND p.execution_mode='real'", (days[0],))]
    finally:
        conn.close()
    actors = set()
    for d in days:
        swaps = ro(Path(desk_dir) / "swaps" / ("%s.db" % d))
        if swaps is None:
            continue
        try:
            actors |= {r[0] for r in swaps.execute(
                "SELECT DISTINCT actor FROM swaps WHERE signature IN (SELECT value FROM json_each(?))",
                (json.dumps(sigs),)) if r[0]}
        finally:
            swaps.close()
    return actors


def own_actors(keypair_path):
    """Our funded Solana wallet, the desk's signer too (desk entries run through the funded runner),
    derived from the runtime's own keypair file exactly as the runner loads it. Only the public key
    leaves this function. None when it cannot be read: the labeller then refuses to run."""
    try:
        from sniper.execution.solana_wallet import load_keypair
        return [str(load_keypair(keypair_path).pubkey())]
    except Exception:
        return None


def main(argv=None):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true", required=True)
    p.add_argument("--exclude-actor", action="append", default=[], help="an extra actor to exclude")
    a = p.parse_args(argv)
    import os
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root, state_path
    load_env_files(str(state_root()))
    desk_dir = Path(state_root()) / "data" / "desk"
    ledger = os.getenv("DB_PATH") or str(state_path("sniper_tracker.db"))
    ours, source = sorted(fill_actors(ledger, desk_dir)), "fills"
    if not ours:
        # before our first fill on the tape: the runtime keypair, public key only
        source = "keypair"
        ours = own_actors(os.getenv("MEMECOIN_SOLANA_KEYPAIR") or str(state_path("data", "solana_keypair.json")))
    if not ours:
        # our own desk buys would lift the post-E path on exactly the shortlisted coins (Gamma)
        print("[desk-ranker-labels] refused: our Solana wallet is unresolved (no fill on the tape, "
              "MEMECOIN_SOLANA_KEYPAIR unreadable)")
        return 2
    print("[desk-ranker-labels] own actors from %s: %d" % (source, len(ours)))
    desk_dir.mkdir(parents=True, exist_ok=True)
    lock = open(desk_dir / "ranker_labels.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-ranker-labels] another labeller holds the lock; exiting")
        return 0
    actors = sorted(set(ours) | set(a.exclude_actor))
    print(json.dumps(run(desk_dir, now=time.time(), exclude_actors=actors), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
