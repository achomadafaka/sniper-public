#!/usr/bin/env python3
"""One-time OFFLINE converter for the PumpSwap tape (operator 2026-10-23, '20 GB is insane'; worker change in
scripts/memecoin_desk_universe_worker.py). Run with the universe worker STOPPED.

It builds data/desk/pumpswap/tape.new.db with the lean layout from the old tape.db:
- swaps_ps: only the pools that matter (M = captured canonical create_pool pools U ps_grads pools U ps_keep pools), row for row;
- every other table (pools, gaps_ps, pricing_ps, ps_keep, ps_grads, ps_decode_losses, ps_pool_hours) copied whole, by column name;
- ps_pool_hours: if the old tape has none, rebuilt from the OLD swaps_ps (every swap, per pool and UTC hour) so the venue totals of
  the history survive the drop of the non-matter rows;
- one gaps_ps row cause 'tape_conversion' (a hole marker), and a ps_conversion row (what was kept and dropped).
It then VERIFIES (M-pool rows identical both ways with EXCEPT, small tables equal, aggregate totals equal the old row count) and, only with
--swap, archives the old file to <archive-dir>/tape.pre-shrink.db (default data/archive/pumpswap, outside data/desk so neither disk cap
counts it) and renames the new one to tape.db. Delete the archive only after Gamma's reads on the new file.

    ionice -c3 nice -n19 python3 scripts/migrate_ps_tape.py [--base data/desk/pumpswap] [--archive-dir data/archive/pumpswap] [--swap] [--allow-live]
Refuses while the universe worker (data/desk/universe.pid) is alive, unless --allow-live (tests).

--drop-payload-b64 (operator via Alpha 2026-10-28): a DIFFERENT build, convert_drop_b64: a complete copy of every table and row with
the same DDL, swaps_ps.payload_b64 written NULL (pools keeps its raw event), verified both ways on every other column and on row
counts; the fresh file has no freelist. With --swap the old file goes to <archive-dir>/tape.pre-shrink-<UTC day>.db; the disk space
comes back only when the operator deletes that archive.
    ionice -c3 nice -n19 python3 scripts/migrate_ps_tape.py --drop-payload-b64 [--swap]

FLOW. (a) PREFERRED, keeps the whole history: stop the worker AND comment out its cron lines (the universe watchdog restarts a stopped worker
within a minute: `* * * * *` and the @reboot twin; Alpha does crons), verify with ps that no worker runs, run this with --swap from the branch checkout, then start
the NEW code on the new file. The old file has no ps_pool_hours yet, so the venue totals are rebuilt for ALL hours. (b) If the new worker already ran
on the old file, ps_pool_hours holds counts since that restart only and 2 h retention has started deleting old non-matter rows: the rebuild is MERGED
(per pool-hour the larger of the existing and the rebuilt count) but hours whose rows were already pruned stay undercounted. --swap alone swaps an
already built and verified tape.new.db when the old tape has not changed since; otherwise it rebuilds first. Two full scans were one too many: the
old rows are scanned once (a TEMP aggregate), and the process runs at nice 19 / idle I/O priority.
ps_pool_hours counts EVENTS SEEN: a gRPC replay after a reconnect is counted twice. It is a descriptive venue total, not a distinct-tx count.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SMALL = ("pools", "gaps_ps", "pricing_ps", "ps_keep", "ps_grads", "ps_decode_losses", "ps_pool_hours", "ps_extend_shadow", "ps_extend_shadow_errors", "ps_sell_fee_witness")


def _worker_module():
    spec = importlib.util.spec_from_file_location("ps_universe_worker", ROOT / "scripts/memecoin_desk_universe_worker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def worker_alive(pid_file):
    try:
        pid = int(Path(pid_file).read_text().strip())
        return Path("/proc/%d" % pid).exists() and "memecoin_desk_universe_worker" in Path("/proc/%d/cmdline" % pid).read_text()
    except (OSError, ValueError):
        return False


def holders(path):
    """Pids of OTHER processes (same user, readable /proc/<pid>/fd) that have `path` or its -wal/-shm open: a writer must not exist while the
    tape is archived. Best effort: an unreadable /proc/<pid>/fd is skipped (not ours)."""
    targets = {os.path.realpath(str(path) + suf) for suf in ("", "-wal", "-shm")}
    out = []
    for proc in Path("/proc").glob("[0-9]*"):
        if int(proc.name) == os.getpid():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                try:
                    if os.path.realpath(os.readlink(fd)) in targets:
                        out.append(int(proc.name))
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return out


def assert_quiet(base, pid_file=None):
    """The tape must have no live writer: the universe worker is not running and no other process holds the files. The universe watchdog
    (cron, every minute) restarts a stopped worker, so this is checked again right before the rename, not only at the start."""
    if pid_file is not None and worker_alive(pid_file):
        raise SystemExit("the universe worker is running (the watchdog may have restarted it): stop it and comment out its cron line first")
    held = holders(Path(base) / "tape.db")
    if held:
        raise SystemExit("another process has the tape open (pids %s): not touching it" % held)


def cols(conn, schema, table):
    return [r[1] for r in conn.execute("PRAGMA %s.table_info(%s)" % (schema, table))]


def convert(base, *, log=print, _tamper=None):
    base = Path(base)
    old_path, new_path = base / "tape.db", base / "tape.new.db"
    if not old_path.is_file():
        raise SystemExit("no tape at %s" % old_path)
    if new_path.exists():
        raise SystemExit("%s exists: remove it first" % new_path)
    W = _worker_module()
    new = sqlite3.connect(str(new_path))
    new.execute("PRAGMA journal_mode=WAL")
    for table in W.DayFiles.PS_STREAMS:
        new.execute(W.DayFiles.DDL[table])
        for ddl in W.DayFiles.INDEXES.get(table, ()):
            new.execute(ddl)
        have = {r[1] for r in new.execute("PRAGMA table_info(%s)" % table)}
        for name, kind in W.DayFiles.ADDED.get(table, ()):
            if name not in have:
                new.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, kind))
    new.execute("CREATE UNIQUE INDEX IF NOT EXISTS swaps_ps_sig_ord ON swaps_ps(signature, ordinal)")
    new.commit()
    new.close()
    new = sqlite3.connect("file:%s?mode=rwc" % new_path, uri=True)
    new.execute("ATTACH DATABASE ? AS old", ("file:%s?mode=ro" % old_path,))
    exists_old = {r[0] for r in new.execute("SELECT name FROM old.sqlite_master WHERE type='table'")}
    report = {"copied": {}}
    for table in SMALL:
        if table not in exists_old:
            continue
        common = [c for c in cols(new, "main", table) if c in cols(new, "old", table)]
        lst = ", ".join(common)
        n = new.execute("INSERT OR IGNORE INTO main.%s(%s) SELECT %s FROM old.%s" % (table, lst, lst, table)).rowcount
        report["copied"][table] = n
    matter = set()
    for table, where in (("pools", " WHERE canonical=1"), ("ps_grads", ""), ("ps_keep", "")):
        if table in exists_old:
            matter.update(r[0] for r in new.execute("SELECT pool FROM old.%s%s" % (table, where)))
    report["m_pools"] = len(matter)
    scols = [c for c in cols(new, "main", "swaps_ps") if c in cols(new, "old", "swaps_ps")]
    lst = ", ".join(scols)
    kept = 0
    for i, pool in enumerate(sorted(matter)):
        kept += new.execute("INSERT OR IGNORE INTO main.swaps_ps(%s) SELECT %s FROM old.swaps_ps WHERE pool=? ORDER BY received_at"
                            % (lst, lst), (pool,)).rowcount
    report["m_rows"] = kept
    # ONE scan of the old swaps_ps: a TEMP per-(pool, hour) aggregate. old_rows is its sum (no second COUNT(*) scan). The existing
    # ps_pool_hours rows (copied above when the new worker already ran on the old file) are MERGED, never skipped: per pool-hour the larger
    # of the existing count and the rebuilt count, so history is only undercounted where retention already deleted rows.
    new.execute("CREATE TEMP TABLE agg_old AS SELECT pool, CAST(received_at / 3600 AS INTEGER) * 3600 AS hour, COUNT(*) AS swaps, "
                "SUM(is_buy) AS buys, MIN(slot) AS first_slot, MAX(slot) AS last_slot, MIN(received_at) AS first_received, "
                "MAX(received_at) AS last_received FROM old.swaps_ps GROUP BY 1, 2")
    old_rows = new.execute("SELECT COALESCE(SUM(swaps), 0) FROM temp.agg_old").fetchone()[0]
    new.execute("INSERT INTO main.ps_pool_hours SELECT pool, hour, swaps, buys, first_slot, last_slot, first_received, last_received FROM temp.agg_old WHERE true "
                "ON CONFLICT(pool, hour) DO UPDATE SET swaps=MAX(swaps, excluded.swaps), buys=MAX(buys, excluded.buys), "
                "first_slot=MIN(first_slot, excluded.first_slot), last_slot=MAX(last_slot, excluded.last_slot), "
                "first_received=MIN(first_received, excluded.first_received), last_received=MAX(last_received, excluded.last_received)")
    report["old_rows"] = old_rows
    agg_total = new.execute("SELECT COALESCE(SUM(swaps), 0) FROM main.ps_pool_hours").fetchone()[0]
    old_max = new.execute("SELECT MAX(last_received) FROM temp.agg_old").fetchone()[0]
    now = time.time()
    new.execute("INSERT INTO main.gaps_ps(at, cause, last_slot) VALUES(?, 'tape_conversion', NULL)", (now,))
    new.execute("CREATE TABLE IF NOT EXISTS ps_conversion(done_at REAL, old_rows INTEGER, m_pools INTEGER, m_rows INTEGER, agg_swaps INTEGER, "
                "old_max_received REAL, verified INTEGER)")
    new.execute("INSERT INTO ps_conversion VALUES(?,?,?,?,?,?,0)", (now, old_rows, len(matter), kept, agg_total, old_max))
    new.commit()
    if _tamper is not None:                                  # tests only: corrupt the new file to prove the verification catches it
        _tamper(new)
        new.commit()
    # VERIFY: the M-pool rows are identical both ways, and the venue totals add up to the old row count
    ph = ",".join("?" for _ in matter) or "''"
    args = tuple(sorted(matter))
    a = new.execute("SELECT COUNT(*) FROM (SELECT %s FROM old.swaps_ps WHERE pool IN (%s) EXCEPT SELECT %s FROM main.swaps_ps)" % (lst, ph, lst), args).fetchone()[0]
    b = new.execute("SELECT COUNT(*) FROM (SELECT %s FROM main.swaps_ps EXCEPT SELECT %s FROM old.swaps_ps WHERE pool IN (%s))" % (lst, lst, ph), args).fetchone()[0]
    m_old = new.execute("SELECT COUNT(*) FROM old.swaps_ps WHERE pool IN (%s)" % ph, args).fetchone()[0]
    report.update(missing_in_new=a, extra_in_new=b, m_rows_old=m_old, agg_swaps=agg_total)
    # the venue totals must COVER every row the new tape stores, per M pool-hour, and the grand total must cover the old row count
    short = new.execute("SELECT COUNT(*) FROM (SELECT s.pool, CAST(s.received_at / 3600 AS INTEGER) * 3600 AS h, COUNT(*) AS n FROM main.swaps_ps s "
                        "GROUP BY 1, 2) r LEFT JOIN main.ps_pool_hours a ON a.pool = r.pool AND a.hour = r.h WHERE COALESCE(a.swaps, 0) < r.n").fetchone()[0]
    report.update(pool_hours_short=short)
    ok = a == 0 and b == 0 and m_old == kept and short == 0 and agg_total >= old_rows
    report["ok"] = ok
    if ok:
        new.execute("UPDATE main.ps_conversion SET verified=1")
        new.commit()
    new.execute("DETACH DATABASE old")
    new.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    new.close()
    log("convert: %s" % report)
    if not ok:
        raise SystemExit("verification FAILED: %s (tape.new.db kept for inspection, nothing swapped)" % report)
    return report


B64_TABLE = "ps_b64_conversion"


def _wal_frontier(wal):
    """(valid frames, last valid frame header) of a WAL: frames carry the header's salts only from the current cycle, so the
    contiguous run of matching frames is the logical log, and the last header's cumulative checksum covers every frame in it.
    A commit appended inside a retained physical tail (no restart, same size) moves it (Delta HOLD 798106b0f)."""
    import struct
    with open(wal, "rb") as f:
        head = f.read(32)
        if len(head) < 32:
            return 0, b""
        page = struct.unpack(">I", head[8:12])[0]
        salts, n, last = head[16:24], 0, b""
        while True:
            f.seek(32 + n * (24 + page))
            frame = f.read(24)
            if len(frame) < 24 or frame[8:16] != salts:
                return n, last
            n, last = n + 1, frame


def source_fence(base, name="tape.db"):
    """A cheap identity of the WHOLE old tape. Any committed write changes it: the database header, size and mtime (a
    checkpoint rewrites pages in place and, in WAL mode, leaves header and size unchanged, but not the mtime), and the WAL
    header, size, mtime and logical frontier (Delta P1/P2 on c179cacff, HOLD on 798106b0f)."""
    import hashlib
    old = Path(base) / name
    wal = Path(str(old) + "-wal")
    h = hashlib.sha256()
    with open(old, "rb") as f:
        h.update(f.read(100))
    st = old.stat()
    h.update(("%d:%d:%d" % (st.st_size, st.st_mtime_ns, st.st_ino)).encode())
    if wal.exists():
        st = wal.stat()
        with open(wal, "rb") as f:
            h.update(f.read(32))
        n, last = _wal_frontier(wal)
        h.update(("%d:%d:%d:" % (st.st_size, st.st_mtime_ns, n)).encode() + last)
    return h.hexdigest()


def _rowid_tables(conn, schema):
    """Tables whose hidden rowid is row identity that must survive the copy (gaps_ps.rowid is ps_loss_batches.gap_rowid):
    ordinary rowid tables without an INTEGER PRIMARY KEY alias. WITHOUT ROWID tables and alias tables copy their keys as
    columns already."""
    out = set()
    for name, sql in conn.execute("SELECT name, sql FROM %s.sqlite_master WHERE type='table' AND sql IS NOT NULL" % schema):
        if "WITHOUT ROWID" in sql.upper():
            continue
        pk = [r for r in conn.execute("PRAGMA %s.table_info(%s)" % (schema, name)) if r[5]]
        if not (len(pk) == 1 and pk[0][2].upper() == "INTEGER"):
            out.add(name)
    return out


def convert_drop_b64(base, *, log=print, _tamper=None, old_name="tape.db", new_name="tape.new.db"):
    """--drop-payload-b64 (operator via Alpha 2026-10-28, 'clean the 38 GB of all bloat'): tape.new.db is a COMPLETE copy of tape.db,
    same DDL (tables and indexes from the old sqlite_master, so the same layout), every table and row, with only swaps_ps.payload_b64
    written NULL (pools keeps its raw create_pool event). A fresh file has no freelist: that, plus the dropped column, is the disk
    reclaim. VERIFY, both ways with EXCEPT: every table's rows equal (swaps_ps on every column except payload_b64) and equal row
    counts. Rollback journal while building, so no WAL the size of the tape; the worker turns WAL back on when it opens the file."""
    base = Path(base)
    old_path, new_path = base / old_name, base / new_name
    if not old_path.is_file():
        raise SystemExit("no tape at %s" % old_path)
    if new_path.exists():
        raise SystemExit("%s exists: remove it first" % new_path)
    new = sqlite3.connect("file:%s?mode=rwc" % new_path, uri=True)
    new.execute("PRAGMA journal_mode=DELETE")
    new.execute("ATTACH DATABASE ? AS old", ("file:%s?mode=ro" % old_path,))
    schema = new.execute("SELECT type, name, sql FROM old.sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                         "ORDER BY type = 'index', rowid").fetchall()
    tables = [n for t, n, _ in schema if t == "table" and n != B64_TABLE]
    fence = source_fence(base, old_name)
    rowid = _rowid_tables(new, "old")
    for t, n, sql in schema:
        if t == "table":
            new.execute(sql)
    report = {"rows": {}}
    for table in tables:
        names = (["rowid"] if table in rowid else []) + cols(new, "old", table)
        sel = ", ".join("NULL" if (table == "swaps_ps" and c == "payload_b64") else c for c in names)
        report["rows"][table] = new.execute("INSERT INTO main.%s(%s) SELECT %s FROM old.%s"
                                            % (table, ", ".join(names), sel, table)).rowcount
        new.commit()
    for t, n, sql in schema:
        if t != "table":
            new.execute(sql)                        # indexes after the data: one sorted build each
    old_max = new.execute("SELECT MAX(received_at) FROM old.swaps_ps").fetchone()[0]
    new.execute("CREATE TABLE IF NOT EXISTS %s(done_at REAL, old_max_received REAL, verified INTEGER, fence TEXT)" % B64_TABLE)
    new.execute("INSERT INTO %s VALUES(?,?,0,?)" % B64_TABLE, (time.time(), old_max, fence))
    new.commit()
    if _tamper is not None:                         # tests only: corrupt the new file to prove the verification catches it
        _tamper(new)
        new.commit()
    bad = {}
    for table in tables:
        names = (["rowid"] if table in rowid else []) + [c for c in cols(new, "old", table)
                                                          if not (table == "swaps_ps" and c == "payload_b64")]
        lst = ", ".join(names)
        a = new.execute("SELECT COUNT(*) FROM (SELECT %s FROM old.%s EXCEPT SELECT %s FROM main.%s)" % (lst, table, lst, table)).fetchone()[0]
        b = new.execute("SELECT COUNT(*) FROM (SELECT %s FROM main.%s EXCEPT SELECT %s FROM old.%s)" % (lst, table, lst, table)).fetchone()[0]
        n_old = new.execute("SELECT COUNT(*) FROM old.%s" % table).fetchone()[0]
        n_new = new.execute("SELECT COUNT(*) FROM main.%s" % table).fetchone()[0]
        if a or b or n_old != n_new or cols(new, "main", table) != cols(new, "old", table):
            bad[table] = {"missing_in_new": a, "extra_in_new": b, "old": n_old, "new": n_new}
    left = new.execute("SELECT COUNT(*) FROM main.swaps_ps WHERE payload_b64 IS NOT NULL").fetchone()[0]
    if left:
        bad["payload_b64_left"] = left
    if source_fence(base, old_name) != fence:
        bad["source_changed_during_build"] = True
    report.update(bad=bad, ok=not bad)
    if not bad:
        new.execute("UPDATE main.%s SET verified=1" % B64_TABLE)
        new.commit()
    new.execute("DETACH DATABASE old")
    new.close()
    log("convert_drop_b64: %s" % report)
    if bad:
        raise SystemExit("verification FAILED: %s (tape.new.db kept for inspection, nothing swapped)" % report)
    return report


def prepared(base, table="ps_conversion"):
    """(True, '') when base/tape.new.db is a VERIFIED conversion of the CURRENT tape.db (nothing was appended to the old tape since: the
    newest receipt in the old swaps_ps equals the one recorded at conversion), else (False, reason)."""
    new_path, old_path = Path(base) / "tape.new.db", Path(base) / "tape.db"
    if not new_path.is_file():
        return False, "no tape.new.db"
    try:
        c = sqlite3.connect("file:%s?mode=ro" % new_path, uri=True)
        row = c.execute("SELECT verified, old_max_received FROM %s ORDER BY done_at DESC LIMIT 1" % table).fetchone()
        c.close()
        o = sqlite3.connect("file:%s?mode=ro" % old_path, uri=True)
        now_max = o.execute("SELECT MAX(received_at) FROM swaps_ps").fetchone()[0]       # index range: no table scan
        o.close()
    except sqlite3.Error as exc:
        return False, "unreadable: %s" % type(exc).__name__
    if not row or not row[0]:
        return False, "tape.new.db was never verified"
    if table == B64_TABLE:
        try:
            c = sqlite3.connect("file:%s?mode=ro" % new_path, uri=True)
            fence = c.execute("SELECT fence FROM %s ORDER BY done_at DESC LIMIT 1" % table).fetchone()[0]
            c.close()
        except sqlite3.Error as exc:
            return False, "unreadable: %s" % type(exc).__name__
        if fence != source_fence(base):
            return False, "the old tape changed after the conversion (source fence differs): remove tape.new.db and rebuild"
        return True, ""
    if row[1] != now_max:
        return False, "the old tape changed after the conversion (newest receipt %s != %s): remove tape.new.db and rebuild" % (now_max, row[1])
    return True, ""


def swap(base, archive_dir, pid_file=None, name="tape.pre-shrink.db", marker=None):
    base, archive = Path(base), Path(archive_dir)
    assert_quiet(base, pid_file)                  # F6: the check at the start of the run is not enough, a conversion takes minutes
    if marker is not None:                        # Delta P2: re-prove the build is of the CURRENT old tape right before the rename
        ok, why = prepared(base, marker)
        if not ok:
            raise SystemExit("not swapping: %s" % why)
    old, new = base / "tape.db", base / "tape.new.db"
    archive.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(old))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    target = archive / name
    if target.exists():
        raise SystemExit("%s exists: refusing to overwrite an archive" % target)
    os.replace(old, target)
    for suffix in ("-wal", "-shm"):
        p = Path(str(old) + suffix)
        if p.exists():
            p.unlink()                                  # emptied by the TRUNCATE checkpoint above
    os.replace(new, old)
    for suffix in ("-wal", "-shm"):
        p = Path(str(new) + suffix)
        if p.exists():
            p.unlink()
    return target


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=None)
    p.add_argument("--archive-dir", default=None)
    p.add_argument("--swap", action="store_true")
    p.add_argument("--allow-live", action="store_true")
    p.add_argument("--drop-payload-b64", action="store_true",
                   help="complete copy with swaps_ps.payload_b64 NULL; archive tape.pre-shrink-<UTC day>.db")
    a = p.parse_args(argv)
    from sniper.util.state_paths import state_path
    base = Path(a.base or state_path("data", "desk", "pumpswap"))
    archive = Path(a.archive_dir or state_path("data", "archive", "pumpswap"))
    pid_file = state_path("data", "desk", "universe.pid")
    if not a.allow_live:
        assert_quiet(base, pid_file)
    try:                                            # the converter must not compete with the funded runner for CPU / disk
        os.nice(19)
        import subprocess
        subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], check=False, capture_output=True)
    except OSError:
        pass
    build = convert_drop_b64 if a.drop_payload_b64 else convert
    marker = B64_TABLE if a.drop_payload_b64 else "ps_conversion"
    name = ("tape.pre-shrink-%s.db" % time.strftime("%Y-%m-%d", time.gmtime())) if a.drop_payload_b64 else "tape.pre-shrink.db"
    if a.swap:
        ok, why = prepared(base, marker)
        if ok:
            report = None                               # F2: a verified build of the CURRENT old tape: swap only
        elif (base / "tape.new.db").exists():
            raise SystemExit("tape.new.db exists but cannot be swapped: %s" % why)
        else:
            report = build(base)
    else:
        report = build(base)
    if a.swap:
        target = swap(base, archive, None if a.allow_live else pid_file, name,
                      marker=B64_TABLE if a.drop_payload_b64 else None)
        print("swapped: old tape archived at %s; new tape is %s (%.2f GB)" % (target, base / "tape.db", (base / "tape.db").stat().st_size / 1e9))
    else:
        print("built %s (not swapped; re-run with --swap)" % (base / "tape.new.db"))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
