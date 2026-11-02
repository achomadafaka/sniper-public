#!/usr/bin/env python3
"""Forward measurement: is a 4663 memecoin signal still on its launchpad curve at signal time, and
if so what does the runner do and how does the curve price move next?

Read-only research loop, no funded RPC, no funded DB write. Polls `memecoin_entry_attempts`
(READ-ONLY handle on the funded ledger) for new chain-4663 rows, and for each new mint (deduped
per clock hour) reads the launchpad's own `0x3cf28b5a` info() call at the chain's LATEST block --
seconds after the signal, close enough to "at signal time" that no historical-archive endpoint is
needed (the constraint that forced the earlier one-shot measurement's window down to ~3.5 hours).
Classifies native / USDG / stock counter and curve-phase / graduated, records the runner's own
`terminal_status`/`terminal_reason` for that attempt, and for a curve-phase mint schedules two more
reads of the curve's own `getReserves()` mid price at +5 and +15 minutes.

Everything is written to its own sqlite database under `data/` -- never the funded ledger.

Requested by Alpha, 2026-09-17. Runs for a fixed 72-hour research window (`--duration`), then
writes a completion marker and exits; the watchdog (`scripts/memecoin_curve_phase_watch_watchdog.sh`)
restarts it on a crash or a stale heartbeat within that window, and refuses to restart it once the
marker exists.

    python3 scripts/research/memecoin_curve_phase_watch_loop.py [--once] [--duration 259200]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv                                                # noqa: E402
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), ".env"))

from sniper.persistence.db_core import get_readonly_connection                # noqa: E402
from sniper.services.memecoin_runner import acquire_run_lock                  # noqa: E402
from sniper.util.clock import real_utc_now_iso                                # noqa: E402
from sniper.util.state_paths import state_path                                # noqa: E402

logger = logging.getLogger("memecoin_curve_phase_watch")

CHAIN = "4663"
LAUNCHPAD = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
NATIVE = "0x" + "00" * 20
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
# A FREE, no-key, no-signup public endpoint -- never the funded custody RPC pool this process must
# not compete with (verified 2026-09-17: the chain-owner endpoint refuses historical state and
# publicnode gates archive calls behind a paid token; "latest"-only reads need neither here, but
# this stays the dedicated research endpoint regardless so it never shares the funded budget).
RPC_URL = "https://robinhood.drpc.org"
POLL_S = 20.0
HOUR_DEDUPE = True
OFFSETS_S = {"+0": 0, "+5": 300, "+15": 900}


def _rpc(method, params, timeout=10):
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(
        RPC_URL, data=body, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
            if "error" in resp:
                return None, resp["error"], False
            return resp.get("result"), None, False
    except urllib.error.HTTPError as e:
        is_429 = e.code == 429
        try:
            err = json.loads(e.read())
            return None, err.get("error", {"code": e.code}), is_429
        except Exception:
            return None, {"code": e.code, "message": "HTTP %s" % e.code}, is_429
    except Exception as exc:
        return None, {"message": str(exc)}, False


def eth_call_latest(to, data):
    return _rpc("eth_call", [{"to": to, "data": data}, "latest"])


def curve_state(mint):
    """{"counter", "state", "curve"} at the current chain head, or None."""
    arg = mint[2:].lower().rjust(64, "0")
    result, err, is_429 = eth_call_latest(LAUNCHPAD, "0x3cf28b5a" + arg)
    if is_429:
        raise Rate429()
    if err or not result:
        return None
    body = result[2:] if result.startswith("0x") else result
    words = [body[i:i + 64] for i in range(0, len(body) - len(body) % 64, 64)]
    if len(words) < 11:
        return None
    coin = "0x" + words[0][-40:].lower()
    if coin != mint.lower():
        return None
    curve = "0x" + words[1][-40:].lower()
    counter = "0x" + words[4][-40:].lower()
    state = int(words[10], 16)
    return {"curve": curve, "counter": counter, "state": state}


def curve_mid(curve):
    """(reserve_counter, reserve_coin, mid) at the current chain head, or None."""
    result, err, is_429 = eth_call_latest(curve, "0x0902f1ac")
    if is_429:
        raise Rate429()
    if err or not result:
        return None
    body = result[2:] if result.startswith("0x") else result
    if len(body) < 128:
        return None
    reserve_counter = int(body[0:64], 16)
    reserve_coin = int(body[64:128], 16)
    mid = (reserve_counter / reserve_coin) if reserve_coin else None
    return reserve_counter, reserve_coin, mid


def shape_of(counter):
    if counter == NATIVE:
        return "native"
    if counter == USDG:
        return "usdg"
    return "stock"


class Rate429(Exception):
    pass


# --- research database (own file, never the funded ledger) --------------------------------------

def connect(path):
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS signals (
        attempt_id TEXT PRIMARY KEY,
        mint TEXT NOT NULL,
        decision_started_at TEXT NOT NULL,
        detected_at TEXT NOT NULL,
        terminal_status TEXT,
        terminal_reason TEXT,
        curve_found INTEGER NOT NULL,
        curve TEXT,
        counter TEXT,
        shape TEXT,
        state INTEGER,
        curve_phase INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS price_observations (
        attempt_id TEXT NOT NULL,
        mint TEXT NOT NULL,
        offset_label TEXT NOT NULL,
        due_at_epoch REAL NOT NULL,
        observed_at TEXT,
        reserve_counter TEXT,
        reserve_coin TEXT,
        mid REAL,
        PRIMARY KEY (attempt_id, offset_label))""")
    conn.commit()
    return conn


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()


def record_signal(conn, *, attempt_id, mint, decision_started_at, terminal_status,
                  terminal_reason, info):
    curve_found = info is not None
    conn.execute(
        "INSERT OR IGNORE INTO signals(attempt_id, mint, decision_started_at, detected_at, "
        "terminal_status, terminal_reason, curve_found, curve, counter, shape, state, curve_phase)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (attempt_id, mint, decision_started_at, real_utc_now_iso(), terminal_status,
         terminal_reason, int(curve_found),
         info["curve"] if info else None, info["counter"] if info else None,
         shape_of(info["counter"]) if info else None, info["state"] if info else None,
         int(bool(info and info["state"] == 0))))
    conn.commit()


def schedule_observations(conn, attempt_id, mint, now_epoch):
    for label, delay in OFFSETS_S.items():
        conn.execute(
            "INSERT OR IGNORE INTO price_observations(attempt_id, mint, offset_label, due_at_epoch)"
            " VALUES(?,?,?,?)", (attempt_id, mint, label, now_epoch + delay))
    conn.commit()


def due_observations(conn, now_epoch):
    return conn.execute(
        "SELECT attempt_id, mint, offset_label FROM price_observations "
        "WHERE observed_at IS NULL AND due_at_epoch <= ? ORDER BY due_at_epoch LIMIT 20",
        (now_epoch,)).fetchall()


def record_observation(conn, attempt_id, mint, offset_label, reading):
    if reading is None:
        conn.execute(
            "UPDATE price_observations SET observed_at=? WHERE attempt_id=? AND offset_label=?",
            (real_utc_now_iso(), attempt_id, offset_label))
    else:
        reserve_counter, reserve_coin, mid = reading
        conn.execute(
            "UPDATE price_observations SET observed_at=?, reserve_counter=?, reserve_coin=?, mid=? "
            "WHERE attempt_id=? AND offset_label=?",
            (real_utc_now_iso(), str(reserve_counter), str(reserve_coin), mid,
             attempt_id, offset_label))
    conn.commit()


# --- funded-ledger polling (read-only) -----------------------------------------------------------

def poll_new_attempts(ledger, after_rowid, limit=100):
    rows = ledger.execute(
        "SELECT rowid, attempt_id, mint, decision_started_at, terminal_status, terminal_reason "
        "FROM memecoin_entry_attempts "
        "WHERE rowid > ? AND network_id = ? AND mint IS NOT NULL "
        "ORDER BY rowid LIMIT ?",
        (after_rowid, CHAIN, limit)).fetchall()
    return rows


def current_max_rowid(ledger):
    row = ledger.execute("SELECT MAX(rowid) FROM memecoin_entry_attempts").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one pass, then exit (testing)")
    ap.add_argument("--duration", type=float, default=72 * 3600.0,
                    help="total research window in seconds from FIRST start (default 72h)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    db_path = state_path("data", "memecoin_curve_phase_watch.db")
    done_marker = state_path("data", "memecoin_curve_phase_watch.done")
    heartbeat = state_path("data", "memecoin_curve_phase_watch.heartbeat")

    if done_marker.exists():
        logger.info("completion marker present (%s) -- the 72h research window is over", done_marker)
        return 0

    lock = acquire_run_lock(str(state_path("data", "memecoin_curve_phase_watch.worker.lock")))
    if lock is None:
        return 0                                    # another instance holds it; silent, matches
                                                      # the refusal-capture loop's own convention

    conn = connect(db_path)
    started_at = get_meta(conn, "started_at_epoch")
    if started_at is None:
        started_at = time.time()
        set_meta(conn, "started_at_epoch", started_at)
        logger.info("research window started at %s, duration=%.0fs", real_utc_now_iso(), args.duration)
    else:
        started_at = float(started_at)
        logger.info("resuming research window started at epoch %.0f", started_at)

    ledger = get_readonly_connection(str(state_path("sniper_tracker.db")))
    stored_after_rowid = get_meta(conn, "after_rowid")
    if stored_after_rowid is None:
        after_rowid = current_max_rowid(ledger)          # fresh start: watch forward only, never replay history
        set_meta(conn, "after_rowid", after_rowid)
        logger.info("fresh start -- seeding after_rowid to current max %d (watching forward only)",
                   after_rowid)
    else:
        after_rowid = int(stored_after_rowid)
    hour_seen = {}                                   # (mint, hour_bucket) -> True, per-process dedupe

    while True:
        now_epoch = time.time()
        if now_epoch - started_at >= args.duration:
            logger.info("72h research window complete -- writing the completion marker and exiting")
            done_marker.write_text(json.dumps({
                "started_at_epoch": started_at, "ended_at": real_utc_now_iso()}))
            return 0

        try:
            heartbeat.write_text(json.dumps({
                "ts": real_utc_now_iso(), "pid": os.getpid(), "after_rowid": after_rowid,
                "elapsed_s": round(now_epoch - started_at, 0)}))
        except Exception:
            logger.exception("could not write the heartbeat")

        try:
            rows = poll_new_attempts(ledger, after_rowid)
        except Exception:
            logger.exception("read of memecoin_entry_attempts failed; will retry next pass")
            rows = []

        for rowid, attempt_id, mint, decision_started_at, terminal_status, terminal_reason in rows:
            hour_bucket = str(decision_started_at)[:13]           # "YYYY-MM-DDTHH"
            key = (mint.lower(), hour_bucket)
            if HOUR_DEDUPE and hour_seen.get(key):
                after_rowid = max(after_rowid, rowid)
                continue
            try:
                info = curve_state(mint)
            except Rate429:
                logger.warning("429 from %s -- pausing 120s", RPC_URL)
                time.sleep(120)
                break  # Retry this row before the cursor advances.
            except Exception:
                logger.exception("curve_state failed for mint=%s", mint)
                info = None
            record_signal(conn, attempt_id=attempt_id, mint=mint,
                         decision_started_at=decision_started_at,
                         terminal_status=terminal_status, terminal_reason=terminal_reason,
                         info=info)
            logger.info("mint=%s attempt=%s shape=%s state=%s status=%s reason=%s",
                       mint, attempt_id, shape_of(info["counter"]) if info else "n/a",
                       info["state"] if info else "n/a", terminal_status, terminal_reason)
            if info is not None and info["state"] == 0:
                schedule_observations(conn, attempt_id, mint, now_epoch)
            hour_seen[key] = True
            after_rowid = max(after_rowid, rowid)

        set_meta(conn, "after_rowid", after_rowid)

        try:
            for attempt_id, mint, offset_label in due_observations(conn, now_epoch):
                row = conn.execute(
                    "SELECT curve FROM signals WHERE attempt_id=?", (attempt_id,)).fetchone()
                curve = row[0] if row else None
                if not curve:
                    record_observation(conn, attempt_id, mint, offset_label, None)
                    continue
                try:
                    reading = curve_mid(curve)
                except Rate429:
                    logger.warning("429 from %s during observation read -- pausing 120s", RPC_URL)
                    time.sleep(120)
                    break
                except Exception:
                    logger.exception("curve_mid failed for mint=%s curve=%s", mint, curve)
                    reading = None
                record_observation(conn, attempt_id, mint, offset_label, reading)
                logger.info("observation mint=%s offset=%s reading=%s", mint, offset_label, reading)
        except Exception:
            logger.exception("observation pass failed")

        if args.once:
            return 0
        time.sleep(POLL_S)


if __name__ == "__main__":
    raise SystemExit(main())
