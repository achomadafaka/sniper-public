"""Cross-process serialization of real execution.

CRITICAL bug (2026-07-15 review): real fills are priced by whole-wallet balance deltas
(`spent = usdc_before - usdc_after`), and THREE uncoordinated actors touch the same wallet with no
lock — the runner main thread, the follow-activity thread (wired to the real leg THIS session), and
the SEPARATE web-server process. A trade sits inside its before/after window for tens of seconds
(confirm polling). An overlapping trade corrupts the delta -> mis-signed realized_pnl -> which feeds
the daily-loss breaker, so a masked loss can keep the breaker from tripping (fails OPEN).

Fix: serialize every real execution behind a per-(wallet, chain) advisory FILE lock (flock), which
is cross-process (a threading.Lock would not cover the web process). Base and 4663 share one wallet
ADDRESS but independent balances, so they key separately and never block each other.
"""
import sqlite3
import threading
import time

import pytest

from sniper.execution.exec_lock import wallet_exec_lock, serialize_executor
from sniper.persistence import db_memecoin as m


def test_concurrent_calls_on_the_same_key_do_not_overlap(tmp_path):
    overlap = {"max": 0, "cur": 0}
    lock = threading.Lock()

    def raw(_conn, _pid, _mint, _size, _now):
        with lock:
            overlap["cur"] += 1
            overlap["max"] = max(overlap["max"], overlap["cur"])
        time.sleep(0.05)                     # hold the "balance window" open
        with lock:
            overlap["cur"] -= 1
        return {"status": "filled"}

    wrapped = serialize_executor(raw, "solPUBKEY", lock_dir=str(tmp_path))
    threads = [threading.Thread(target=wrapped, args=(None, 1, "M", 30.0, "t"))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlap["max"] == 1, "executions on the same wallet must be serialized, saw %d concurrent" % overlap["max"]


def test_different_keys_run_in_parallel(tmp_path):
    """Base (8453) and 4663 have independent balances — they must NOT block each other."""
    started = []
    gate = threading.Event()

    def raw(_c, _p, _m, _s, _n):
        started.append(1)
        gate.wait(timeout=1.0)               # both must be able to be in-flight at once
        return {"status": "filled"}

    a = serialize_executor(raw, "evm-8453-0xADDR", lock_dir=str(tmp_path))
    b = serialize_executor(raw, "evm-4663-0xADDR", lock_dir=str(tmp_path))
    ta = threading.Thread(target=a, args=(None, 1, "M", 30.0, "t"))
    tb = threading.Thread(target=b, args=(None, 2, "N", 30.0, "t"))
    ta.start(); tb.start()
    time.sleep(0.2)
    assert len(started) == 2, "different-key executors must run in parallel, saw %d" % len(started)
    gate.set()
    ta.join(); tb.join()


def test_wrapper_is_transparent_to_args_and_return(tmp_path):
    seen = {}

    def raw(conn, pid, mint, size, now, extra=None):
        seen.update(dict(conn=conn, pid=pid, mint=mint, size=size, now=now, extra=extra))
        return {"status": "filled", "sig": "0xabc"}

    wrapped = serialize_executor(raw, "k", lock_dir=str(tmp_path))
    out = wrapped("CONN", 7, "MINT", 42.0, "NOW", extra="x")
    assert out == {"status": "filled", "sig": "0xabc"}
    assert seen == dict(conn="CONN", pid=7, mint="MINT", size=42.0, now="NOW", extra="x")


def test_lock_released_even_if_the_executor_raises(tmp_path):
    def boom(*a, **k):
        raise RuntimeError("rpc down")

    wrapped = serialize_executor(boom, "k", lock_dir=str(tmp_path))
    try:
        wrapped(None, 1, "M", 30.0, "t")
    except RuntimeError:
        pass
    # the lock must be free now — a second acquire returns immediately
    t0 = time.time()
    with wallet_exec_lock("k", lock_dir=str(tmp_path)):
        pass
    assert time.time() - t0 < 0.5, "lock was not released after the executor raised"


def test_exit_exception_releases_only_the_unsigned_claim(tmp_path):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(
        c, source_user_id="u", source_handle="u", mint="M", ticker="M",
        entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
        entry_lag_s=0.0, their_size_usd=10.0, opened_at="t",
        execution_mode="real", network_id="4663",
    )

    def boom(conn, position, _reason, now_iso):
        claim_id = m.claim_position_close(conn, position["id"], now_iso, "b_tp")
        assert claim_id
        raise RuntimeError("provider failed before signing")

    wrapped = serialize_executor(
        boom, "evm-4663-wallet", lock_dir=str(tmp_path), release_unsigned_close_on_error=True,
    )
    with pytest.raises(RuntimeError, match="before signing"):
        wrapped(c, m.get_position(c, pid), "b_tp", "t2")

    assert m.get_position(c, pid)["close_claim_id"] is None
    assert m.claim_position_close(c, pid, "t3", "b_tp")

    signed_pid = m.open_position(
        c, source_user_id="u", source_handle="u", mint="S", ticker="S",
        entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
        entry_lag_s=0.0, their_size_usd=10.0, opened_at="t",
        execution_mode="real", network_id="4663",
    )

    def signed_boom(conn, position, _reason, now_iso):
        claim_id = m.claim_position_close(conn, position["id"], now_iso, "b_tp")
        assert claim_id
        conn.execute("UPDATE memecoin_positions SET close_sig='0xsig' WHERE id=?", (position["id"],))
        conn.commit()
        raise RuntimeError("provider failed after signing")

    signed = serialize_executor(
        signed_boom, "evm-4663-wallet", lock_dir=str(tmp_path),
        release_unsigned_close_on_error=True,
    )
    with pytest.raises(RuntimeError, match="after signing"):
        signed(c, m.get_position(c, signed_pid), "b_tp", "t4")

    assert m.get_position(c, signed_pid)["close_claim_id"]
    assert m.claim_position_close(c, signed_pid, "t5", "b_tp") is None


def test_flock_is_cross_process_exclusive(tmp_path):
    """The whole point: two DIFFERENT file descriptors (i.e. what two processes would hold) cannot
    both own the lock. Verified with a non-blocking probe from a second fd."""
    import fcntl
    import os
    with wallet_exec_lock("k", lock_dir=str(tmp_path)):
        path = os.path.join(str(tmp_path), "real_exec_k.lock")
        fd2 = os.open(path, os.O_RDWR | os.O_CREAT)
        try:
            got = True
            try:
                fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                got = False
            assert got is False, "a second fd acquired the lock while it was held — not exclusive"
        finally:
            os.close(fd2)
