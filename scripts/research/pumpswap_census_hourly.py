#!/usr/bin/env python3
"""Hourly PumpSwap census roll-up (Alpha 2026-10-23): the raw tape keeps only
MEMECOIN_DESK_PUMPSWAP_RETENTION_H hours (default 6), so the census is taken one COMPLETE UTC hour at
a time and appended to data/desk/pumpswap/census_hourly.jsonl, which accumulates past the raw window.

Each run: (1) pumpswap_pool_mints.fill reads accounts for pools not yet in pool_mints.db (research key,
pacer); a failure there is recorded on the row as pool_mints_error and the census still runs, so the
posthoc_* fields of that row are a lower bound; (2) pumpswap_census.census over [H-1h, H) where H is the
current hour boundary; (3) one JSON line per hour, written once: an hour already in the file is skipped,
so a rerun or a double cron fire never duplicates. Hours the cron missed are NOT backfilled here; run
with --hour <epoch> while the raw rows are still inside the retention window.

    python3 scripts/research/pumpswap_census_hourly.py            # cron at minute 5
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "scripts" / "research")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pumpswap_census as C        # noqa: E402
import pumpswap_pool_mints as M    # noqa: E402


BUDGET_S = 3000


def done_hours(path):
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text().splitlines():
        try:
            out.add(int(json.loads(line)["hour"]))
        except (ValueError, KeyError, TypeError):
            continue                                  # a torn last line from a crash is ignored, not fatal
    return out


DECODE_CAUSES = ("decode_loss", "virtual_quote_unproven")


def _q(xs, q):
    return xs[min(len(xs) - 1, max(0, -(-int(q * 100) * len(xs) // 100) - 1))] if xs else None


CLASSES = ("sol85", "sub_threshold", "quote_token", "unknown")


def _rwin(hour, end):
    """The indexed received_at window a block_time hour is read through (swaps_ps has NO block_time index; a full scan of the 44 GB tape ran 49 min on 2026-10-28)."""
    return hour - C.RECEIVED_PRE_S, end + C.RECEIVED_MARGIN_S


def curve_coverage(tape, hour, end):
    """CREATE_POOL CAPTURE COVERAGE (Gamma A1 gate G-cov), per population. graduations_seen / graduations_mapped above
    are both read from `pools` (a captured create_pool), so they cannot see a graduation whose create_pool was lost.
    The real denominator is the CURVE side: ps_grads holds the Pump curve stream's migration events (pool derived from
    the mint) by receipt hour, each with grad_class from the mint's last curve row:
      sol85          a SOL curve that ran to its threshold (>= 84 SOL real quote): the desk's population
      sub_threshold  a SOL curve far below it (coins that reach PumpSwap after 1-3 curve swaps)
      quote_token    a curve quoted in another token
      unknown        no curve row / NULL real_quote / a row from before grad_class existed
    Fields: curve_graduations / curve_grads_with_create_pool / create_pool_coverage over ALL classes (kept), by_class
    {class: {graduations, with_create_pool, coverage}} and the G-cov inputs sol85_graduations / sol85_with_create_pool /
    sol85_create_pool_coverage. Coverage is None when there were none OR the tape predates ps_grads / grad_class (unknown,
    never 0). derived_mapped_swaps = swaps_ps rows of the hour mapped ONLY through the curve ('mapped_curve_derived')."""
    out = {"curve_graduations": None, "curve_grads_with_create_pool": None, "create_pool_coverage": None,
           "derived_mapped_swaps": None, "by_class": None, "sol85_graduations": None, "sol85_with_create_pool": None,
           "sol85_create_pool_coverage": None}
    has = ("EXISTS (SELECT 1 FROM pools p WHERE p.pool = g.pool AND p.canonical = 1)")
    try:
        n, both = tape.execute(
            "SELECT COUNT(DISTINCT g.pool), COUNT(DISTINCT CASE WHEN %s THEN g.pool END) FROM ps_grads g "
            "WHERE g.received_at >= ? AND g.received_at < ?" % has, (hour, end)).fetchone()
        out.update(curve_graduations=n, curve_grads_with_create_pool=both,
                   create_pool_coverage=(both / n if n else None),
                   derived_mapped_swaps=tape.execute(
                       "SELECT COUNT(*) FROM swaps_ps WHERE received_at >= ? AND received_at < ? AND map_status = 'mapped_curve_derived' "
                       "AND block_time >= ? AND block_time < ?", (*_rwin(hour, end), hour, end)).fetchone()[0])
    except sqlite3.OperationalError:
        return out                                        # the tape predates ps_grads: unknown
    try:
        by = {c: {"graduations": 0, "with_create_pool": 0, "coverage": None} for c in CLASSES}
        for cls, k, both in tape.execute(
                "SELECT COALESCE(g.grad_class, 'unknown'), COUNT(DISTINCT g.pool), COUNT(DISTINCT CASE WHEN %s THEN g.pool END) "
                "FROM ps_grads g WHERE g.received_at >= ? AND g.received_at < ? GROUP BY 1" % has, (hour, end)):
            row = by.setdefault(cls, {"graduations": 0, "with_create_pool": 0, "coverage": None})
            row.update(graduations=k, with_create_pool=both, coverage=(both / k if k else None))
        out["by_class"] = by
        s85 = by["sol85"]
        out.update(sol85_graduations=s85["graduations"], sol85_with_create_pool=s85["with_create_pool"],
                   sol85_create_pool_coverage=s85["coverage"])
    except sqlite3.OperationalError:
        pass                                              # ps_grads without grad_class: per-class unknown, totals kept
    return out


def venue_totals(tape, hour, end):
    """Whole-venue totals from ps_pool_hours (every PumpSwap swap SEEN, stored or not; a gRPC replay after a reconnect counts twice:
    events seen, not distinct txs). None when the tape has no such table (before the write filter) - then swaps_ps is the venue."""
    try:
        n, pools = tape.execute("SELECT COALESCE(SUM(swaps), 0), COUNT(DISTINCT pool) FROM ps_pool_hours WHERE hour >= ? AND hour < ?",
                                (hour, end)).fetchone()
    except sqlite3.OperationalError:
        return {"venue_swaps": None, "venue_pools": None}
    return {"venue_swaps": n, "venue_pools": pools}


def hour_facts(tape, hour):
    """Gamma's floor inputs for [hour, hour+3600): swaps and pools by block_time, graduations by the canonical
    create_pool's block_time (mapped = its pool has a mapped swap anywhere in the tape), per-pool swap-count
    quantiles, decode failures (gaps_ps causes in DECODE_CAUSES, by receipt time) as a count, and every other
    gap (worker_start, ring_overflow, write_error, disk_cap, ...) listed with its start and an end: the first
    swap received after it, None when none was. Decode failures are counted, not listed: ~4,000 an hour."""
    end = hour + 3600
    counts = sorted(n for (n,) in tape.execute(
        "SELECT COUNT(*) FROM swaps_ps WHERE received_at >= ? AND received_at < ? AND block_time >= ? AND block_time < ? GROUP BY pool",
        (*_rwin(hour, end), hour, end)))
    grads = [pool for (pool,) in tape.execute(
        "SELECT DISTINCT pool FROM pools WHERE canonical = 1 AND block_time >= ? AND block_time < ?", (hour, end))]
    mapped = sum(1 for pool in grads if tape.execute(
        "SELECT 1 FROM swaps_ps WHERE pool = ? AND map_status = 'mapped' LIMIT 1", (pool,)).fetchone())
    marks = ",".join("?" * len(DECODE_CAUSES))
    decode = tape.execute("SELECT COUNT(*) FROM gaps_ps WHERE at >= ? AND at < ? AND cause IN (%s)" % marks,
                          (hour, end, *DECODE_CAUSES)).fetchone()[0]
    gaps = [{"start": at, "cause": cause, "last_slot": last_slot,
             "end": tape.execute("SELECT MIN(received_at) FROM swaps_ps WHERE received_at > ?", (at,)).fetchone()[0]}
            for at, cause, last_slot in tape.execute(
                "SELECT at, cause, last_slot FROM gaps_ps WHERE at >= ? AND at < ? AND cause NOT IN (%s) ORDER BY at"
                % marks, (hour, end, *DECODE_CAUSES))]
    spans, spans_error = [], None                          # capture STOP spans overlapping the hour (an open span runs through its end)
    try:
        cols = {r[1] for r in tape.execute("PRAGMA table_info(ps_gap_spans)")}
        if cols and not {"start_at", "end_at"} <= cols:
            spans_error = "malformed:columns=%s" % ",".join(sorted(cols))      # a table that cannot be read as spans is UNKNOWN, never 'no outage'
        elif cols:
            sel = ", ".join(c if c in cols else "NULL" for c in ("start_at", "end_at", "cause", "ended_by"))
            spans = tape.execute("SELECT %s FROM ps_gap_spans WHERE start_at < ? AND (end_at IS NULL OR end_at >= ?) ORDER BY start_at" % sel,
                                 (end, hour)).fetchall()
        # no table at all: a tape from before spans existed, point gaps only (backward compatible, spans_error stays None)
    except sqlite3.Error as exc:
        spans, spans_error = [], "unreadable:%s" % type(exc).__name__
    gaps += [{"start": max(st, hour), "cause": "span:" + str(cause), "last_slot": None, "end": en, "span": True, "open": en is None,
              "ended_by": by} for st, en, cause, by in spans]
    cov = {**curve_coverage(tape, hour, end), **venue_totals(tape, hour, end)}
    return {**cov, "total_swaps": sum(counts), "distinct_pools": len(counts), "graduations_seen": len(grads),
            "graduations_mapped": mapped, "swaps_per_pool_p50": _q(counts, 0.5), "swaps_per_pool_p90": _q(counts, 0.9),
            "swaps_per_pool_p99": _q(counts, 0.99), "swaps_per_pool_max": counts[-1] if counts else None,
            "decode_failures": decode, "gap_events": gaps, "gap_spans_unreadable": spans_error}


def roll(base, hour, fetch, *, now=time.time):
    """Append the census row for [hour, hour+3600) to base/census_hourly.jsonl; return the row or None."""
    out = base / "census_hourly.jsonl"
    with open(base / "census_hourly.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)      # never queue behind an overrunning run
        except BlockingIOError:
            raise SystemExit("census_hourly: previous run still holds the lock")
        if hour in done_hours(out):
            return None
        tape = sqlite3.connect("file:%s?mode=ro" % (base / "tape.db"), uri=True, timeout=5)
        try:
            error = None
            side = sqlite3.connect(str(base / "pool_mints.db"), timeout=5)
            try:
                side.execute(M.SCHEMA)
                M.fill(tape, side, fetch, window=_rwin(hour, hour + 3600))
            except Exception as exc:                  # recorded on the row; the count still runs
                error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            finally:
                side.close()
            got = C.census(tape, start=hour, end=hour + 3600, pool_mints=C.load_pool_mints(base / "pool_mints.db"))
            extra = hour_facts(tape, hour)
        finally:
            tape.close()
        row = next((h for h in got["hours"] if h["hour"] == hour), {"hour": hour, "pools": 0, "swaps": 0})
        row = {**row, **extra, "gaps": got["gaps"], "no_block_time_rows": got["no_block_time_rows"],
               "pool_mints_error": error, "written_at": now(), "received_window": list(_rwin(hour, hour + 3600))}
        with open(out, "a") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        return row


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", default=None, help="default <state root>/data/desk/pumpswap")
    p.add_argument("--hour", type=int, default=None, help="hour start epoch (default: the last complete hour)")
    a = p.parse_args(argv)
    from sniper.util.envfiles import load_env_files
    root = os.environ.get("SNIPER_STATE_ROOT") or str(REPO)
    load_env_files(root)
    base = Path(a.base or Path(root) / "data" / "desk" / "pumpswap")
    if not (base / "tape.db").exists():
        raise SystemExit("no PumpSwap tape at %s" % base)
    hour = a.hour if a.hour is not None else int(time.time()) // 3600 * 3600 - 3600
    if hour % 3600:
        raise SystemExit("--hour must be an hour boundary")
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(SystemExit("census_hourly: %d s budget exceeded, hour %d not recorded" % (BUDGET_S, hour))))
    signal.alarm(BUDGET_S)                                       # a run must end before the next :05 cron
    row = roll(base, hour, M.research_fetch())
    print(json.dumps(row) if row else "hour %d already recorded" % hour)


if __name__ == "__main__":
    main()
