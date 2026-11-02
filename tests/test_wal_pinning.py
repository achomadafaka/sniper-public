"""WO-10 — WAL-pinning root cause, reproduced and regression-guarded.

The 2026-07-24 incident: the main DB WAL grew to 400-830MB and every fix was a bounce. Root cause
(reproduced here, not guessed): a long-lived connection that sits with an OPEN transaction pins the
WAL — a TRUNCATE checkpoint can't reset the WAL past that connection's snapshot, so it returns busy=1
and the WAL grows without bound. Python's default `isolation_level=""` holds a read transaction open
across statements once one begins, and auto-BEGINs on any DML and holds it until commit/rollback.

The fix: `get_readonly_connection` opens in autocommit (`isolation_level=None`) + `query_only=1`, so a
reader's snapshot ends the instant each statement finishes and an idle reader pins nothing.

Regression contract (the WO's bar): a service-shaped reader must not block a TRUNCATE for >2s.
"""
import os
import sqlite3
import time

from sniper.persistence import db_core


def _writer(path):
    c = sqlite3.connect(path, timeout=2)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA journal_size_limit=67108864")
    return c


def _wal_bytes(path):
    return os.path.getsize(path + "-wal") if os.path.exists(path + "-wal") else 0


def _grow(w, n=3000):
    for _ in range(n):
        w.execute("INSERT INTO t(b) VALUES(?)", ("x" * 400,))
    w.commit()


def _seed(tmp_path):
    path = str(tmp_path / "wal.db")
    w = _writer(path)
    w.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, b TEXT)")
    w.commit()
    return path, w


def test_open_transaction_pins_wal(tmp_path):
    """Reproduce the incident: a reader holding an open transaction blocks TRUNCATE and the WAL
    cannot shrink. This is the failure that a bounce papered over."""
    path, w = _seed(tmp_path)
    pinner = sqlite3.connect(path, timeout=1, isolation_level="")
    pinner.execute("BEGIN")
    pinner.execute("SELECT * FROM t").fetchall()      # snapshot held open, never ended
    _grow(w)
    w.execute("PRAGMA busy_timeout=800")
    res = w.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert res[0] == 1, f"expected TRUNCATE blocked (busy=1), got {res}"   # pinned
    assert _wal_bytes(path) > 0, "WAL should still hold frames while pinned"
    pinner.close()
    w.close()


def test_autocommit_reader_does_not_pin_wal(tmp_path):
    """The fix: an idle autocommit read-only connection holds no snapshot, so TRUNCATE completes fast
    and the WAL resets to zero — the WO's <2s / non-blocking contract."""
    path, w = _seed(tmp_path)
    _grow(w, 200)
    w.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    reader = db_core.get_readonly_connection(path)     # autocommit + query_only (the fix)
    reader.execute("SELECT COUNT(*) FROM t").fetchall()  # a real read, then idle
    _grow(w)

    w.execute("PRAGMA busy_timeout=2000")
    t0 = time.time()
    res = w.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    dt = time.time() - t0
    assert res[0] == 0, f"reader pinned the WAL: checkpoint busy, {res}"
    assert dt < 2.0, f"TRUNCATE blocked {dt:.1f}s (>2s bar)"
    assert _wal_bytes(path) == 0, f"WAL not reclaimed: {_wal_bytes(path)} bytes"
    reader.close()
    w.close()


def test_readonly_connection_is_query_only(tmp_path):
    """Belt-and-braces: the read handle can never auto-begin a write transaction."""
    path, w = _seed(tmp_path)
    w.close()
    reader = db_core.get_readonly_connection(path)
    try:
        reader.execute("INSERT INTO t(b) VALUES('x')")
        assert False, "read-only connection accepted a write"
    except sqlite3.OperationalError:
        pass
    reader.close()


# --- WAL high-water-mark reclaim (2026-07-29) --------------------------------
# Second, distinct failure mode from the pinning incident above: with NO pinner at
# all, `PRAGMA wal_checkpoint(PASSIVE)` reports the WAL fully checkpointed
# (`58/58, blocked=0`) yet the file stays at its all-time peak, because SQLite
# recycles the WAL in place from frame 0 and only truncates it when
# `journal_size_limit` is set. Measured on the live box: 262MB of WAL across 8
# databases whose live page counts summed to a fraction of that, e.g. the Radar
# hot DB at 6.6MB live / 64.9MB WAL.

# The bound has to be well under the observed bloat to reclaim anything: db_core's 64MiB would
# have trimmed the Radar's 64.9MB WAL to 64MiB and called it fixed. It also has to stay clear of
# `wal_autocheckpoint=1000` (4MB at a 4096B page) so routine operation never touches the truncate
# path — the limit is there for the burst that outran checkpointing, not for the steady state.
WAL_SIZE_LIMIT_BYTES = 16777216   # 16MiB = 4x the autocheckpoint watermark


def test_unlimited_journal_size_limit_never_shrinks_the_wal(tmp_path):
    """Mechanism: journal_size_limit=-1 (SQLite's default) leaves a fully-checkpointed
    WAL parked at its high-water mark forever. No reader, no pinner, nothing busy."""
    path = str(tmp_path / "unbounded.db")
    w = sqlite3.connect(path, timeout=2)
    w.execute("PRAGMA journal_mode=WAL")
    assert w.execute("PRAGMA journal_size_limit").fetchone()[0] == -1   # the default
    w.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, b TEXT)")
    w.commit()
    _grow(w, 20000)
    peak = _wal_bytes(path)
    assert peak > 4_000_000, f"test did not build a large enough WAL: {peak}"

    busy, log, done = w.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    assert busy == 0 and log == done, f"expected a clean full checkpoint, got {(busy, log, done)}"
    assert _wal_bytes(path) == peak, "unbounded WAL unexpectedly shrank"

    w.execute("INSERT INTO t(b) VALUES('x')")   # frames restart at 0, file is reused in place
    w.commit()
    assert _wal_bytes(path) == peak, "file should still sit at the high-water mark"
    w.close()


def test_journal_size_limit_reclaims_the_wal_down_to_the_limit(tmp_path):
    """The fix: with the limit set, the same clean checkpoint truncates the file back to the
    limit. No TRUNCATE checkpoint, no checkpoint lock, no cron escalation.

    Note the limit is a floor, not a target — SQLite trims *down to* it. That is why the bound
    must sit under the observed bloat: 64MiB against the Radar's 64.9MB WAL reclaims 0.9MB.
    """
    limit = 1 << 20   # 1MiB — small so the test stays fast; the mechanism is size-independent
    path = str(tmp_path / "bounded.db")
    w = sqlite3.connect(path, timeout=2)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute(f"PRAGMA journal_size_limit={limit}")
    w.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, b TEXT)")
    w.commit()
    _grow(w, 20000)
    peak = _wal_bytes(path)
    assert peak > 4 * limit, f"test did not build a WAL past the limit: {peak}"

    # Measured trigger: the trim lands on the first write AFTER a checkpoint has reset the WAL,
    # not inside the checkpoint. PASSIVE/RESTART both leave the file at its peak until that write.
    w.execute("PRAGMA wal_checkpoint(PASSIVE)")
    assert _wal_bytes(path) == peak, "checkpoint itself should not trim"
    w.execute("INSERT INTO t(b) VALUES('y')")
    w.commit()
    assert _wal_bytes(path) <= limit, f"WAL not reclaimed: {_wal_bytes(path)} of {peak}"
    w.close()


def _bounded(conn):
    limit = conn.execute("PRAGMA journal_size_limit").fetchone()[0]
    conn.close()
    return limit


def test_live_wal_writers_bound_their_journal_size(tmp_path):
    """Contract: every connect path that turns on WAL must also bound the WAL file.
    These are exactly the writers whose WALs were stuck at their peaks on 2026-07-29."""
    from sniper.hyperlive import intel_store
    from sniper.persistence import db_exit_state, db_memecoin_v6_paper, db_ticks
    from sniper.research.memecoin_opportunity import insights as op_insights
    from sniper.research.memecoin_opportunity import store as op_store

    factories = {
        "main_ledger": lambda: db_core.get_connection(str(tmp_path / "ledger.db")),
        "opportunity_hot": lambda: op_store.connect(tmp_path / "op.db"),
        "opportunity_insights": lambda: op_insights.connect_insights(tmp_path / "op_ins.db"),
        "ticks": lambda: db_ticks.connect_ticks(str(tmp_path / "ticks.db")),
        "sol_tape": lambda: db_ticks.connect_sol_tape(str(tmp_path / "sol_tape.db")),
        "exit_state": lambda: db_exit_state.connect_exit_state(str(tmp_path / "exit.db")),
        "v6_paper": lambda: db_memecoin_v6_paper.connect(str(tmp_path / "v6.db")),
        "hl_intel": lambda: intel_store.connect(str(tmp_path / "intel.db")),
    }
    limits = {name: _bounded(build()) for name, build in factories.items()}
    unbounded = {n: v for n, v in limits.items() if v <= 0}
    assert not unbounded, f"WAL writers with no journal_size_limit: {unbounded}"
    too_loose = {n: v for n, v in limits.items() if v > WAL_SIZE_LIMIT_BYTES}
    assert not too_loose, f"bound too loose to reclaim the observed bloat: {too_loose}"
