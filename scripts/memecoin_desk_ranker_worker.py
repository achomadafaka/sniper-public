#!/usr/bin/env python3
"""Desk cohort U runners ranker worker (sniper/services/memecoin_desk_ranker.py). Off the fill path:
its own process, read-only on producer U's files, one writer for data/desk/ranker/.

Each 60 s cycle (rank_at = the minute boundary, run 2 s after it so the tape has landed):
  1. candidates = producer-U emissions that verify (identity, pool, snapshot hash) and are at most
     MAX_LIFE_S old at rank_at; per mint only the newest emission (an older one is superseded);
  2. the tape at rank_at: head lag and gap rows, then per candidate its window swaps and band state;
  3. one row per in-band candidate (kept or cut) to ranker/<UTC day>.db rank_rows, the cycle to
     cycles, first sighting / shortlist / close to ranker/candidates.db with the SR draws.
A candidate's first cycle always writes a row, even when it already left band, so no emission is
missing from the cut side or the SR-all denominator. A missed cycle is counted, never back-filled.
Short read transactions only: a long read would pin the producer's WAL.

    python3 scripts/memecoin_desk_ranker_worker.py --once
    # cron: scripts/memecoin_desk_ranker_watchdog.sh (its header has the line); kill knob
    # MEMECOIN_DESK_RANKER_ENABLED=0 (default on)
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.services import memecoin_desk_ranker as R      # noqa: E402

SETTLE_S = 2.0
HEAD_SLOTS = 400          # ~160 s of slots; a head older than that is stale anyway

DDL_DAY = (
    "CREATE TABLE IF NOT EXISTS rank_rows(rank_at REAL NOT NULL, ranker_version TEXT NOT NULL, "
    "opportunity_id TEXT NOT NULL, mint TEXT, rank INTEGER, score REAL, inputs_hash TEXT, features TEXT, "
    "completeness TEXT NOT NULL, shortlisted INTEGER NOT NULL, emission_at REAL, "
    "PRIMARY KEY(rank_at, opportunity_id))",
    "CREATE TABLE IF NOT EXISTS cycles(rank_at REAL PRIMARY KEY, ranker_version TEXT NOT NULL, "
    "started_at REAL, finished_at REAL, tape TEXT, head_lag_s REAL, n_rows INTEGER, n_scored INTEGER, "
    "n_shortlisted INTEGER, n_unscored INTEGER, refused TEXT, cycles_missed INTEGER)",
)
DDL_CAND = (
    "CREATE TABLE IF NOT EXISTS candidates(opportunity_id TEXT PRIMARY KEY, mint TEXT, emission_at REAL, "
    "ranker_version TEXT NOT NULL, u_sr_all REAL, u_sr_post REAL, sr_all_draw INTEGER, sr_post_draw INTEGER, "
    "first_rank_at REAL, first_shortlisted_at REAL, closed_at REAL, close_reason TEXT)",
)


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def ro(path):
    if not Path(path).exists():
        return None
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)


# columns added after a table's first version: an existing day file gains them in place (Codex,
# f5a030786: a 10-column rank_rows made the 11-value INSERT raise)
ADDED = {"rank_rows": (("emission_at", "REAL"),)}


def rw(path, ddl):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in ddl:
        conn.execute(stmt)
    for table, cols in ADDED.items():
        have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        for name, kind in cols:
            if have and name not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, kind))
    return conn


def days(rank_at):
    return [utc_day(rank_at), utc_day(rank_at - 86400)]


def emissions(desk_dir, rank_at, refused):
    """({mint: newest genuine emission known at rank_at}, {opportunity_id: older emission of a mint
    that emitted again}). Only emissions inside [rank_at - MAX_LIFE_S, rank_at] are verified."""
    from sniper.services.memecoin_desk_safety import verify_u_emission
    newest, superseded = {}, {}
    for d in reversed(days(rank_at)):
        path = desk_dir / "candidate_emissions" / ("%s.jsonl" % d)
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
                if row.get("producer") != "U":
                    continue
                if row.get("venue", "pumpfun") != "pumpfun" or row.get("schema") == "desk-candidate/pumpswap-v1":
                    raise ValueError("emission_venue")
                at = row["snapshot"]["emission_at"]
                if not rank_at - R.MAX_LIFE_S <= at <= rank_at:
                    continue                   # expired, or not yet known at rank_at
                verify_u_emission(row, now=rank_at, max_age_s=R.MAX_LIFE_S)
            except ValueError as exc:      # a torn line, a forged row, too old or not yet known
                reason = str(exc.args[0]) if exc.args and str(exc.args[0]).startswith("emission_") \
                    else "emission_unparsed"
                refused[reason] = refused.get(reason, 0) + 1
                continue
            except (TypeError, AttributeError, KeyError):
                refused["emission_shape"] = refused.get("emission_shape", 0) + 1
                continue
            old = newest.get(row["mint"])
            if old is None or row["snapshot"]["emission_at"] > old["snapshot"]["emission_at"]:
                if old is not None:
                    superseded[old["ticket_id"]] = old
                newest[row["mint"]] = row
            elif row["ticket_id"] != old["ticket_id"]:
                superseded[row["ticket_id"]] = row
    return newest, superseded


def tape_health(conns, gap_conns, rank_at):
    """('ok' | 'tape_stale' | 'tape_gap', head lag s). Head = newest swap received by rank_at."""
    head = None
    for conn in conns:
        # the newest swap received by rank_at, searched over the top HEAD_SLOTS slots on the slot
        # index: at ~50 swaps/s a row-count window (LIMIT 200) held only rows received after rank_at
        # the top slot is also bounded by rank_at (Codex, 7b777bd22): a swap received later must
        # never change a past cycle's health
        row = conn.execute("SELECT MAX(received_at) FROM swaps WHERE slot >= "
                           "(SELECT slot FROM swaps WHERE received_at <= ? ORDER BY slot DESC LIMIT 1) - ? "
                           "AND received_at <= ?", (rank_at, HEAD_SLOTS, rank_at)).fetchone()
        if row and row[0] is not None:
            head = row[0]
            break
    lag = None if head is None else round(rank_at - head, 3)
    if lag is None or lag > R.HEAD_MAX_LAG_S:
        return "tape_stale", lag
    for conn in gap_conns:
        n = conn.execute("SELECT COUNT(*) FROM gaps WHERE at > ? AND at <= ?",
                         (rank_at - R.LOOKBACK_S, rank_at)).fetchone()[0]
        if n:
            return "tape_gap", lag
    return "ok", lag


def mint_tape(conns, mint, rank_at, signature, ordinal):
    """(window swaps, base row, emission price) for one mint from today's then yesterday's file."""
    lo = rank_at - R.LOOKBACK_S
    window, base, em_price = [], None, None
    for conn in conns:
        for r in conn.execute("SELECT received_at, is_buy, quote_atomic, actor, virtual_quote, virtual_token, "
                              "real_token FROM swaps WHERE mint=? AND received_at > ? AND received_at <= ?",
                              (mint, lo, rank_at)):
            window.append(r)
        if base is None:
            base = conn.execute("SELECT received_at, is_buy, quote_atomic, actor, virtual_quote, virtual_token, "
                                "real_token FROM swaps WHERE mint=? AND received_at <= ? "
                                "ORDER BY slot DESC, ordinal DESC LIMIT 1", (mint, lo)).fetchone()
        if em_price is None:
            e = conn.execute("SELECT virtual_quote, virtual_token FROM swaps WHERE signature=? AND ordinal=?",
                             (signature, ordinal)).fetchone()
            em_price = R.price(*e) if e else None
    return window, base, em_price


_BAND = {}


def producer_band():
    """The producer U band, from the same source as the universe worker (the active cohort's frozen band, else the env):
    the ranker ranks what the producer emits. Read once per process, like the producer's."""
    if "band" not in _BAND:
        from sniper.services.memecoin_desk_universe import cohort_band
        from sniper.util.state_paths import state_path
        got = cohort_band(state_path("data", "memecoin_desk.db"), os.environ.get("MEMECOIN_DESK_U_COHORT_ID"))
        if got["source"] in ("cohort_unreadable", "cohort_missing"):
            print(json.dumps({"band_source": got["source"], "band": got["band"], "error": got.get("error")}), flush=True)
        _BAND["band"] = got["band"]
    return _BAND["band"]


def candidate_view(cand, conns, rank_at, tape):
    """(completeness, features | None, band) for one candidate at rank_at."""
    window, base, em_price = mint_tape(conns, cand["mint"], rank_at, cand["trigger_signature"],
                                       cand["trigger_ordinal"])
    window.sort(key=lambda r: r[0])
    last = window[-1] if window else base
    last_price = R.price(last[4], last[5]) if last else None
    band = R.band_state(emission_mcap_usd=(cand["snapshot"]["fields"].get("mcap_usd") or {}).get("value"),
                        emission_price=em_price, last_price=last_price,
                        last_real_token=last[6] if last else None, band=producer_band())
    if tape != "ok":
        return tape, None, None            # no band verdict from an unhealthy tape: never closes
    if last_price is None:
        return "no_price", None, band
    if band != "inside":
        return band, None, band
    base_price = R.price(base[4], base[5]) if base else em_price
    swaps = [(r[0], bool(r[1]), int(r[2] or 0), r[3], R.price(r[4], r[5])) for r in window]
    return "complete", R.features(swaps, rank_at=rank_at, base_price=base_price), band


def reconcile(cand_db, desk_dir, rank_at):
    """Registry truth from the durable rows (Codex, d55441c22): rows are written first, and a crash
    before the registry write is restarted by cron at a LATER minute, so a cycle that shortlisted a
    candidate would otherwise leave first_shortlisted_at NULL and the JUDGE would cut it forever.
    Inserts a missing candidate and fills only NULL first_rank_at / first_shortlisted_at /
    emission_at from the rows of the last MAX_LIFE + 3 cycles; never touches a set field."""
    since = rank_at - R.MAX_LIFE_S - 3 * R.CYCLE_S
    agg = {}
    for d in days(rank_at):
        conn = ro(Path(desk_dir) / "ranker" / ("%s.db" % d))
        if conn is None:
            continue
        try:
            # a day file from before emission_at reads it as NULL, never drops its rows (Codex, f5a030786)
            has_em = "emission_at" in {r[1] for r in conn.execute("PRAGMA table_info(rank_rows)")}
            for oid, mint, first, short, em in conn.execute(
                    "SELECT opportunity_id, MAX(mint), MIN(rank_at), MIN(CASE WHEN shortlisted=1 THEN rank_at END), "
                    "%s FROM rank_rows WHERE rank_at >= ? GROUP BY opportunity_id"
                    % ("MAX(emission_at)" if has_em else "NULL"), (since,)):
                a = agg.setdefault(oid, [mint, first, short, em])
                a[1] = min(a[1], first)
                a[2] = short if a[2] is None else (a[2] if short is None else min(a[2], short))
                a[3] = a[3] if a[3] is not None else em
        except sqlite3.Error:
            continue                                   # a day file the ranker never finished: none
        finally:
            conn.close()
    if not agg:
        return
    with cand_db:
        for oid, (mint, first, short, em) in agg.items():
            d = R.sr_draws(oid)
            cand_db.execute("INSERT OR IGNORE INTO candidates(opportunity_id, mint, emission_at, ranker_version, "
                            "u_sr_all, u_sr_post, sr_all_draw, sr_post_draw, first_rank_at) VALUES(?,?,?,?,?,?,?,?,?)",
                            (oid, mint, em, R.RANKER_VERSION, d["u_sr_all"], d["u_sr_post"],
                             int(d["sr_all_draw"]), int(d["sr_post_draw"]), first))
            cand_db.execute("UPDATE candidates SET first_rank_at=COALESCE(first_rank_at, ?), "
                            "first_shortlisted_at=COALESCE(first_shortlisted_at, ?), "
                            "emission_at=COALESCE(emission_at, ?) WHERE opportunity_id=?", (first, short, em, oid))


def run_cycle(desk_dir, rank_at, *, now_fn=time.time, cycles_missed=0):
    desk_dir = Path(desk_dir)
    out = desk_dir / "ranker"
    started, refused = now_fn(), {}
    newest, superseded = emissions(desk_dir, rank_at, refused)
    conns = [c for c in (ro(desk_dir / "swaps" / ("%s.db" % d)) for d in days(rank_at)) if c]
    gap_conns = [c for c in (ro(desk_dir / "gaps" / ("%s.db" % d)) for d in days(rank_at)) if c]
    try:
        tape, lag = tape_health(conns, gap_conns, rank_at)
        cand_db = rw(out / "candidates.db", DDL_CAND)
        reconcile(cand_db, desk_dir, rank_at)
        known = {r[0]: r[1] for r in cand_db.execute("SELECT opportunity_id, closed_at FROM candidates")}
        views, first, closes = [], [], []
        for oid, cand in superseded.items():
            # an older emission of a mint that re-entered band: it had left. Never seen before, it
            # still gets its one row so it stays in the denominator; open, it closes.
            if oid not in known:
                first.append(cand)
                views.append({"opportunity_id": oid, "mint": cand["mint"], "completeness": "left_band",
                              "features": None})
            closes.append((oid, "superseded"))
        for cand in newest.values():
            oid = cand["ticket_id"]
            if known.get(oid) is not None:
                continue                                   # closed: never ranked again
            completeness, feats, band = candidate_view(cand, conns, rank_at, tape)
            is_first = oid not in known
            if is_first:
                first.append(cand)
            if band == "left_band":
                closes.append((oid, "left_band"))
                if not is_first:
                    continue                               # no row once it has left band
            views.append({"opportunity_id": oid, "mint": cand["mint"], "completeness": completeness,
                          "features": feats})
    finally:
        for c in conns + gap_conns:
            c.close()
    rows = R.rank_cycle(views)
    emitted = {c["ticket_id"]: c["snapshot"]["emission_at"] for c in list(newest.values()) + list(superseded.values())}
    day_db = rw(out / ("%s.db" % utc_day(rank_at)), DDL_DAY)
    try:
        # ROWS FIRST (Codex, 7b777bd22): the registry closes a candidate for good, so a crash after
        # it and before the rows lost the first-cycle denominator row forever. Both writes are
        # idempotent (INSERT OR IGNORE / IS NULL guards): a crash between them replays the cycle.
        with day_db:
            day_db.executemany(
                "INSERT OR IGNORE INTO rank_rows(rank_at, ranker_version, opportunity_id, mint, rank, score, "
                "inputs_hash, features, completeness, shortlisted, emission_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(rank_at, R.RANKER_VERSION, r["opportunity_id"], r["mint"], r["rank"], r["score"],
                  r["inputs_hash"], json.dumps(r["features"], sort_keys=True) if r["features"] else None,
                  r["completeness"], int(r["shortlisted"]), emitted.get(r["opportunity_id"])) for r in rows])
            # totals from the durable rows, never the in-memory ones: a replayed or repeated cycle
            # writes nothing new and must still report what is stored (Codex, d55441c22)
            n_rows, n_scored, n_short, n_unscored = day_db.execute(
                "SELECT COUNT(*), COALESCE(SUM(rank IS NOT NULL), 0), COALESCE(SUM(shortlisted), 0), "
                "COALESCE(SUM(rank IS NULL), 0) FROM rank_rows WHERE rank_at=?", (rank_at,)).fetchone()
            stats = {"rank_at": rank_at, "ranker_version": R.RANKER_VERSION, "tape": tape, "head_lag_s": lag,
                     "n_rows": n_rows, "n_scored": n_scored, "n_shortlisted": n_short, "n_unscored": n_unscored,
                     "refused": refused, "cycles_missed": cycles_missed}
            day_db.execute("INSERT OR REPLACE INTO cycles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           (rank_at, R.RANKER_VERSION, started, now_fn(), tape, lag, stats["n_rows"],
                            stats["n_scored"], stats["n_shortlisted"], stats["n_unscored"],
                            json.dumps(refused, sort_keys=True), cycles_missed))
        with cand_db:
            for cand in first:
                d = R.sr_draws(cand["ticket_id"])
                cand_db.execute("INSERT OR IGNORE INTO candidates(opportunity_id, mint, emission_at, ranker_version, "
                                "u_sr_all, u_sr_post, sr_all_draw, sr_post_draw, first_rank_at) "
                                "VALUES(?,?,?,?,?,?,?,?,?)",
                                (cand["ticket_id"], cand["mint"], cand["snapshot"]["emission_at"],
                                 R.RANKER_VERSION, d["u_sr_all"], d["u_sr_post"], int(d["sr_all_draw"]),
                                 int(d["sr_post_draw"]), rank_at))
            for row in rows:
                if row["shortlisted"]:
                    cand_db.execute("UPDATE candidates SET first_shortlisted_at=? WHERE opportunity_id=? "
                                    "AND first_shortlisted_at IS NULL", (rank_at, row["opportunity_id"]))
            open_ids = {r[0] for r in cand_db.execute(
                "SELECT opportunity_id FROM candidates WHERE closed_at IS NULL")}
            live = {c["ticket_id"] for c in newest.values()}
            closes += [(oid, "max_life") for oid in open_ids - live - set(superseded)]
            for oid, reason in closes:
                cand_db.execute("UPDATE candidates SET closed_at=?, close_reason=? WHERE opportunity_id=? "
                                "AND closed_at IS NULL", (rank_at, reason, oid))
    finally:
        day_db.close()
        cand_db.close()
    return stats


def shortlisted(desk_dir, opportunity_id) -> bool:
    """The consumer read: was this opportunity in the top TOP_N of any cycle while in band."""
    conn = ro(Path(desk_dir) / "ranker" / "candidates.db")
    if conn is None:
        return False
    try:
        row = conn.execute("SELECT first_shortlisted_at FROM candidates WHERE opportunity_id=?",
                           (opportunity_id,)).fetchone()
    finally:
        conn.close()
    return bool(row and row[0] is not None)


def main(argv=None):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    a = p.parse_args(argv)
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))                 # cron has no environment; not at import
    if os.getenv("MEMECOIN_DESK_RANKER_ENABLED", "1").strip() == "0":
        print("[desk-ranker] MEMECOIN_DESK_RANKER_ENABLED=0; exiting")
        return 0
    desk_dir = Path(state_root()) / "data" / "desk"
    (desk_dir / "ranker").mkdir(parents=True, exist_ok=True)
    lock = open(desk_dir / "ranker" / "ranker.lock", "w")
    try:                                              # one writer: a second start exits
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-ranker] another ranker holds the lock; exiting")
        return 0
    last = None
    while True:
        rank_at = R.cycle_start(time.time() - SETTLE_S)
        if rank_at != last:
            missed = 0 if last is None else max(0, int(round((rank_at - last) / R.CYCLE_S)) - 1)
            stats = run_cycle(desk_dir, rank_at, cycles_missed=missed)
            last = rank_at
            status = desk_dir / "ranker" / "status.json.tmp"
            status.write_text(json.dumps(dict(stats, at=time.time(), pid=os.getpid()), sort_keys=True))
            status.replace(desk_dir / "ranker" / "status.json")
            print(json.dumps(stats, sort_keys=True), flush=True)
            if a.once:
                return 0
        time.sleep(max(0.2, rank_at + R.CYCLE_S + SETTLE_S - time.time()))


if __name__ == "__main__":
    raise SystemExit(main())
