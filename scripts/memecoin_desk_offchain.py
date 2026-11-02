#!/usr/bin/env python3
"""Desk offchain heat, per-coin collector (sniper/services/memecoin_desk_offchain.py). COLLECT AND REPORT
ONLY: no provider call, no model, nothing on a JUDGE sheet, no money path.

Every 60 s it reads, read-only, the DexScreener context producer (data/desk/dexscreener/<day>.db), the X
producer (data/desk/x/<day>.db and its heartbeat) and the CRAWLER (data/desk/crawler/crawls.db). For every
Solana mint any of them saw in the last hour it writes a row to its own DB, data/desk/offchain/offchain.db,
only when the row's meaning changed. Every row carries known_at (our clock when computed), the
promoter-list version and OFFCHAIN_VERSION.

The promoter list is the operator's: data/desk/offchain/promoters.json, {"version": "promoter-list-vN",
"handles": [...]}. Shipped as promoter-list-v0, empty, seeded when absent. Any edit must bump the version.

    MEMECOIN_DESK_OFFCHAIN_ENABLED=1 python3 scripts/memecoin_desk_offchain.py --once | --loop
Cron line (operator installs): see scripts/memecoin_desk_offchain_watchdog.sh.
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

from sniper.services import memecoin_desk_offchain as O      # noqa: E402

PASS_S = 60.0
DDL = ("CREATE TABLE IF NOT EXISTS coin_heat(id INTEGER PRIMARY KEY, mint TEXT NOT NULL, network_id TEXT NOT NULL, "
       "known_at REAL NOT NULL, window_end REAL NOT NULL, paid_boost_units REAL, paid_ads INTEGER, paid_profile INTEGER, "
       "listing_takeover INTEGER, dex_state TEXT NOT NULL, x_mentions_15m INTEGER, x_accounts_15m INTEGER, "
       "x_promoter_mentions_15m INTEGER, x_state TEXT NOT NULL, crawler_intent TEXT, crawler_known_at REAL, "
       "promoter_list_version TEXT NOT NULL, offchain_version TEXT NOT NULL)",
       "CREATE INDEX IF NOT EXISTS coin_heat_mint ON coin_heat(mint, known_at)",
       "CREATE TABLE IF NOT EXISTS passes(id INTEGER PRIMARY KEY, at REAL NOT NULL, mints INTEGER, written INTEGER, "
       "dex_state TEXT, x_state TEXT, crawler_state TEXT)")


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def _ro(path):
    path = Path(path)
    if not path.is_file():
        return None
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)


def promoters(desk_dir):
    """(version, set of lower-cased handles). Seeds promoter-list-v0, empty, when absent."""
    path = Path(desk_dir) / "offchain" / "promoters.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": "promoter-list-v0", "handles": []}, indent=1) + "\n")
    data = json.loads(path.read_text())
    version = data.get("version")
    if not isinstance(version, str) or not version.startswith("promoter-list-v"):
        raise SystemExit("refused: %s needs a 'promoter-list-vN' version" % path)
    return version, {str(h).lower().lstrip("@") for h in data.get("handles") or []}


def read_inputs(desk_dir, now):
    """{mint: {dex: [...], x: [...], crawl: (intent, known_at)}}, plus source health."""
    desk_dir, days = Path(desk_dir), sorted({utc_day(now - O.WINDOW_S), utc_day(now)})
    coins, polls, dex_seen = {}, [], False
    for d in days:
        conn = _ro(desk_dir / "dexscreener" / ("%s.db" % d))
        if conn is None:
            continue
        dex_seen = True
        try:
            polls += conn.execute("SELECT endpoint, received_at, status FROM polls WHERE received_at > ?",
                                  (now - O.DEX_FRESH_S,)).fetchall()
            for ep, t, chain, token, payload in conn.execute(
                    "SELECT endpoint, received_at, chain_id, token_address, payload_json FROM items "
                    "WHERE received_at > ? AND received_at <= ?", (now - O.WINDOW_S, now)):
                if chain == "solana" and token:
                    coins.setdefault(token, {"dex": [], "x": [], "crawl": None})["dex"].append((ep, t, payload))
        finally:
            conn.close()
    dex_ok = dex_seen and O.dex_healthy(polls, now=now)
    beat = desk_dir / "x" / "producer.heartbeat"
    try:
        x_ok = now - float(json.loads(beat.read_text())["ts"]) <= O.X_FRESH_S
    except (OSError, ValueError, KeyError, TypeError):
        x_ok = False
    for d in days:
        conn = _ro(desk_dir / "x" / ("%s.db" % d))
        if conn is None:
            continue
        try:
            for tweet, mint, chain, received, handle in conn.execute(
                    "SELECT m.tweet_id, m.mint, m.chain, m.received_at, p.handle FROM token_meta m "
                    "LEFT JOIN posts p ON p.tweet_id = m.tweet_id WHERE m.received_at > ? AND m.received_at <= ?",
                    (now - O.WINDOW_S, now)):
                if mint and (chain is None or str(chain).lower().startswith("sol")):
                    coins.setdefault(mint, {"dex": [], "x": [], "crawl": None})["x"].append((tweet, handle, received))
        finally:
            conn.close()
    crawler_ok = False
    conn = _ro(desk_dir / "crawler" / "crawls.db")
    if conn is not None:
        crawler_ok = True
        try:
            for mint, intent, known in conn.execute(
                    "SELECT mint, intent, MAX(known_at) FROM crawls WHERE intent IS NOT NULL AND known_at <= ? "
                    "AND known_at > ? AND network_id = 'solana' GROUP BY mint", (now, now - O.CRAWL_MAX_AGE_S)):
                coins.setdefault(mint, {"dex": [], "x": [], "crawl": None})["crawl"] = (intent, known)
        finally:
            conn.close()
    return coins, dex_ok, x_ok, crawler_ok


def run_pass(desk_dir, *, now_fn=time.time):
    desk_dir = Path(desk_dir)
    version, handles = promoters(desk_dir)
    now = now_fn()
    coins, dex_ok, x_ok, crawler_ok = read_inputs(desk_dir, now)
    out = sqlite3.connect(str(desk_dir / "offchain" / "offchain.db"), timeout=30)
    out.execute("PRAGMA journal_mode=WAL")
    for stmt in DDL:
        out.execute(stmt)
    written = 0
    try:
        with out:
            for mint, inp in sorted(coins.items()):
                row = O.coin_row(mint, now=now, dex_items=inp["dex"], dex_ok=dex_ok, x_links=inp["x"], x_ok=x_ok,
                                 promoters=handles, crawl=inp["crawl"])
                last = out.execute("SELECT " + ", ".join(O.SIGNATURE) + ", promoter_list_version FROM coin_heat "
                                   "WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,)).fetchone()
                sig = O.signature(row)
                if last is not None and tuple(_norm(v) for v in last[:-1]) == tuple(_norm(v) for v in sig) \
                        and last[-1] == version:
                    continue
                out.execute("INSERT INTO coin_heat(mint, network_id, known_at, window_end, paid_boost_units, paid_ads, "
                            "paid_profile, listing_takeover, dex_state, x_mentions_15m, x_accounts_15m, "
                            "x_promoter_mentions_15m, x_state, crawler_intent, crawler_known_at, promoter_list_version, "
                            "offchain_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (mint, row["network_id"], now_fn(), now, *(_norm(row[k]) for k in O.SIGNATURE[:]), version,
                             O.OFFCHAIN_VERSION))
                written += 1
            out.execute("INSERT INTO passes(at, mints, written, dex_state, x_state, crawler_state) VALUES(?,?,?,?,?,?)",
                        (now, len(coins), written, "ok" if dex_ok else "missing", "ok" if x_ok else "missing",
                         "ok" if crawler_ok else "missing"))
    finally:
        out.close()
    return {"mints": len(coins), "written": written, "dex": dex_ok, "x": x_ok, "crawler": crawler_ok}


def _norm(v):
    return int(v) if isinstance(v, bool) else v


def main(argv=None):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    a = p.parse_args(argv)
    if os.getenv("MEMECOIN_DESK_OFFCHAIN_ENABLED", "0").strip() != "1":
        print("[desk-offchain] MEMECOIN_DESK_OFFCHAIN_ENABLED is not 1; exiting")
        return 0
    desk_dir = Path(state_root()) / "data" / "desk"
    (desk_dir / "offchain").mkdir(parents=True, exist_ok=True)
    lock = open(desk_dir / "offchain" / "offchain.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-offchain] another collector holds the lock; exiting")
        return 0
    while True:
        started = time.time()
        print(json.dumps(run_pass(desk_dir)), flush=True)
        if a.once:
            return 0
        time.sleep(max(1.0, PASS_S - (time.time() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
