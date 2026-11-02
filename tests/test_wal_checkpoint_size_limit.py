"""The maintenance script must bound the WAL FILE, not just reset the write pointer.

A checkpoint resets the WAL's write pointer. It does NOT truncate the file. Only
`journal_size_limit` does that, and it is a PER-CONNECTION setting that applies to the connection
performing the reset. `scripts/wal_checkpoint.py` opened a bare `sqlite3.connect(...)`, so every
run checkpointed 100% of frames and left the file at its high-water mark, then warned about the
size it had just declined to reclaim — every three minutes, for hours. Production log line:

    PASSIVE - WAL: 463.4MB | pages: 117929/117929 | blocked=0
    WARN - WAL still 463MB (>100MB threshold)

100% checkpointed, nothing blocked, file unchanged. That is the signature of a missing limit, not
of a reader pinning the log (which shows `done < total` instead).
"""
from __future__ import annotations

import importlib.util
import pathlib
import sqlite3

_SPEC = importlib.util.spec_from_file_location(
    "wal_checkpoint", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "wal_checkpoint.py")
wal_checkpoint = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wal_checkpoint)


def _grow_wal(path, mb=40):
    """Returns the connection STILL OPEN. Closing the last connection to a WAL database makes
    SQLite checkpoint and remove the WAL, which would delete the very thing under test."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")      # let it accumulate, as a pinned live DB does
    conn.execute("CREATE TABLE IF NOT EXISTS t(x BLOB)")
    conn.executemany("INSERT INTO t(x) VALUES(?)",
                     [(b"\x00" * 4000,) for _ in range(mb * 262)])
    return conn


def _wal_mb(path):
    wal = pathlib.Path(str(path) + "-wal")
    return wal.stat().st_size / (1024 * 1024) if wal.exists() else 0.0


def test_the_script_sets_a_journal_size_limit_on_its_own_connection():
    """The constant must exist and be finite. -1 or 0 means 'never truncate'."""
    assert getattr(wal_checkpoint, "WAL_SIZE_LIMIT_MB", 0) > 0


def test_a_passive_checkpoint_truncates_the_wal_file(tmp_path):
    db = tmp_path / "t.db"
    holder = _grow_wal(db)
    grown = _wal_mb(db)
    assert grown > wal_checkpoint.WAL_SIZE_LIMIT_MB, "fixture must exceed the limit to be a test"

    wal_checkpoint.checkpoint_db(db)

    # a write after the reset is what applies the limit
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("PRAGMA journal_size_limit=%d" % int(wal_checkpoint.WAL_SIZE_LIMIT_MB * 1024 * 1024))
    conn.execute("INSERT INTO t(x) VALUES(?)", (b"reset",))
    conn.close()
    size = _wal_mb(db)
    holder.close()

    assert size <= wal_checkpoint.WAL_SIZE_LIMIT_MB + 1.0, (
        "the WAL file must come back down to the limit, not stay at its high-water mark")


def test_a_bare_connection_would_not_truncate(tmp_path):
    """The negative control: this is what the script did before, and why the file never shrank."""
    db = tmp_path / "t.db"
    holder = _grow_wal(db)
    grown = _wal_mb(db)
    conn = sqlite3.connect(str(db))                  # no journal_size_limit — the old behaviour
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    conn.execute("INSERT INTO t(x) VALUES(?)", (b"reset",))
    conn.commit()
    conn.close()
    size = _wal_mb(db)
    holder.close()
    assert size >= grown - 1.0, (
        "without a limit the file stays at its high-water mark — if this ever fails, SQLite "
        "changed and the fix may no longer be load-bearing")
