#!/usr/bin/env python3
"""PumpSwap archive growth control (operator GO via Alpha 2026-10-28, 'clear data as fast as it grows'). Data is never
deleted; size is controlled. Two subcommands on data/archive/pumpswap/keep-<day>.db, CLOSED days only (mode 0444: the
universe worker chmods a day read-only when it moves past it; the current day stays writable and is never touched):

  strip-b64  one-time: rebuild each closed day without swaps_ps.payload_b64 through migrate_ps_tape.convert_drop_b64
             (the same complete copy and both-ways verification as the live tape); the verified copy replaces the day
             file, which stays 0444. A failed verification keeps the original and leaves keep-<day>.new.db for inspection.
  compress   daily: zstd each SEALED closed day to keep-<day>.db.zst; the .db is removed only after the decompressed
             stream's sha256 equals the original's. Sealed = at least --min-age-days old (2 UTC days) AND the live
             tape (--tape) holds no ps_keep window whose keep_until falls on that day (a stuck or late window would
             still archive into it). An unreadable tape seals nothing. The universe worker refuses to open a plain
             day beside its .zst (archive_day_sealed), and open_day refuses a day present in both forms.

Dry run by default; --confirm acts. Runs at nice 19 / idle I/O. Read compressed days with
sniper.services.memecoin_desk_ps_archive.open_day.

    ionice -c3 nice -n19 python3 scripts/ps_archive_maintenance.py {strip-b64|compress} [--archive-dir DIR] [--confirm]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAY = re.compile(r"^keep-(\d{4}-\d{2}-\d{2})\.db$")
CHUNK = 1 << 20


def _converter():
    spec = importlib.util.spec_from_file_location("migrate_ps_tape", ROOT / "scripts/migrate_ps_tape.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def closed_days(archive_dir):
    """keep-<day>.db files that are closed (0444) and not being built, oldest first."""
    out = []
    for p in sorted(Path(archive_dir).glob("keep-*.db")):
        if DAY.match(p.name) and (p.stat().st_mode & 0o222) == 0:
            out.append(p)
    return out


def stripped(path):
    """True when the day already went through the b64 strip (its verified conversion marker is present)."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ps_b64_conversion'").fetchone()
        return bool(row) and conn.execute("SELECT MAX(verified) FROM ps_b64_conversion").fetchone()[0] == 1
    finally:
        conn.close()


def strip_day(path, *, log=print):
    """Replace one closed day with its verified b64-free copy. Refuses a day another process holds open."""
    M = _converter()
    path = Path(path)
    held = M.holders(path)
    if held:
        raise SystemExit("%s is open in pids %s: not touching it" % (path, held))
    new_name = path.name[:-3] + ".new.db"
    before = path.stat().st_size
    M.convert_drop_b64(path.parent, log=log, old_name=path.name, new_name=new_name)   # raises before any replace
    new = path.parent / new_name
    os.chmod(new, 0o444)
    os.replace(new, path)
    log("stripped %s: %.2f GB -> %.2f GB" % (path.name, before / 1e9, path.stat().st_size / 1e9))


def pending_window_days(tape):
    """UTC days that still have a ps_keep window in the live tape (the worker would archive into them), or None when the
    tape cannot be read (then nothing counts as sealed)."""
    import datetime as dt
    try:
        conn = sqlite3.connect(Path(tape).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            ends = [r[0] for r in conn.execute("SELECT keep_until FROM ps_keep")]
        finally:
            conn.close()
        return {dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat() for t in ends}
    except (sqlite3.Error, ValueError, TypeError, OverflowError, OSError):
        return None                                   # unreadable or a malformed end (NULL, NaN, inf): seal nothing


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_zst(path):
    h = hashlib.sha256()
    proc = subprocess.Popen(["zstd", "-dcq", str(path)], stdout=subprocess.PIPE)
    for chunk in iter(lambda: proc.stdout.read(CHUNK), b""):
        h.update(chunk)
    if proc.wait() != 0:
        raise RuntimeError("zstd decompress failed: %s" % path)
    return h.hexdigest()


def compress_day(path, *, level=6, log=print):
    """keep-<day>.db -> keep-<day>.db.zst (0444); the .db goes only after the decompressed sha256 equals the original.
    Holds the day's seal lock (shared with the universe worker's _ps_archive) through verify, publish and unlink; a
    day the worker holds raises BlockingIOError (skip it, retry later)."""
    import fcntl
    path = Path(path)
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _compress_locked(path, level=level, log=log)
    finally:
        os.close(fd)


def _compress_locked(path, *, level, log):
    out = Path(str(path) + ".zst")
    if out.exists():
        raise SystemExit("%s exists: refusing to overwrite" % out)
    want = _sha256_file(path)
    tmp = Path(str(out) + ".part")
    subprocess.run(["zstd", "-q", "-%d" % level, "-T2", "-f", "-o", str(tmp), str(path)], check=True)
    if _sha256_zst(tmp) != want:
        tmp.unlink()
        raise SystemExit("compressed %s does not decompress equal: original kept" % path.name)
    os.chmod(tmp, 0o444)
    os.replace(tmp, out)
    size = path.stat().st_size
    path.unlink()
    log("compressed %s: %.2f GB -> %.2f GB" % (path.name, size / 1e9, out.stat().st_size / 1e9))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=("strip-b64", "compress"))
    p.add_argument("--archive-dir", default=None)
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--level", type=int, default=6)
    p.add_argument("--min-age-days", type=int, default=2)
    p.add_argument("--tape", default=None, help="the live PumpSwap tape (default <state>/data/desk/pumpswap/tape.db)")
    a = p.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    from sniper.util.state_paths import state_path
    archive = Path(a.archive_dir or state_path("data", "archive", "pumpswap")).resolve()
    days = closed_days(archive)
    import datetime as dt
    newest = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=a.min_age_days)).isoformat()
    if a.command == "strip-b64":
        todo = [d for d in days if not stripped(d)]
    else:
        open_days = pending_window_days(Path(a.tape or state_path("data", "desk", "pumpswap", "tape.db")))
        todo = [d for d in days if DAY.match(d.name).group(1) <= newest and open_days is not None
                and DAY.match(d.name).group(1) not in open_days]
        if open_days is None:
            print("live tape unreadable: no day is sealed, nothing compressed")
    for d in todo:
        print("%s %s %.2f GB" % (a.command, d.name, d.stat().st_size / 1e9))
    if not a.confirm:
        print("dry run: %d closed day(s); nothing changed (re-run with --confirm)" % len(todo))
        return 0
    try:
        os.nice(19)
        subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], check=False, capture_output=True)
    except OSError:
        pass
    for d in todo:
        try:
            (strip_day if a.command == "strip-b64" else compress_day)(d)
        except BlockingIOError:
            print("%s busy (the universe worker holds it): skipped, retried next run" % d.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
