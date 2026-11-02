"""Read one PumpSwap archive day (data/archive/pumpswap/keep-<day>.db or its compressed keep-<day>.db.zst) for research.

A compressed day is decompressed on demand into a scratch directory on disk (never /tmp: it is RAM on this host), read
through a read-only handle, and the copy is removed when the block exits."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def open_day(archive_dir, day: str, *, scratch_dir=None):
    """Yield a read-only sqlite3 connection on keep-<day>; FileNotFoundError when neither form exists, ValueError
    archive_day_ambiguous when both do (a late plain file beside a packed day must never hide the packed rows)."""
    archive_dir = Path(archive_dir)
    plain, packed = archive_dir / ("keep-%s.db" % day), archive_dir / ("keep-%s.db.zst" % day)
    copy = None
    if plain.exists() and packed.exists():
        raise ValueError("archive_day_ambiguous: %s" % day)
    if plain.exists():
        path = plain
    elif packed.exists():
        scratch = Path(scratch_dir or archive_dir / ".scratch")
        scratch.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="keep-%s." % day, suffix=".db", dir=scratch)   # unique per call
        os.close(fd)
        copy = Path(name)
        try:
            subprocess.run(["zstd", "-dqf", "-o", str(copy), str(packed)], check=True)
        except BaseException:
            copy.unlink(missing_ok=True)
            raise
        path = copy
    else:
        raise FileNotFoundError("no archive day %s in %s" % (day, archive_dir))
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()
        if copy is not None:
            copy.unlink(missing_ok=True)
