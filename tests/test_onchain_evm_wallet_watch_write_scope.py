"""The EVM wallet watcher must not hold a write transaction on the funded ledger across RPC calls.

Before 2026-09-05 it inserted one row per transaction inside the decode loop and committed after
the loop, so the ledger's write lock sat open across every RPC round-trip -- and, on a mid-loop
failure, across the next sleep as well. The runner's own writes then failed with
`database is locked` about once every two hours.
"""
import sqlite3

import pytest

import scripts.research.onchain_evm_wallet_watch as w
from thread_local_time import patch_local  # noqa: E402  (2026-09-29)


class _Recorder(sqlite3.Connection):
    calls = None

    def execute(self, sql, *a, **k):
        if sql.lstrip().upper().startswith("INSERT OR IGNORE INTO ONCHAIN_WALLET_DETECTIONS"):
            self.calls.append("write")
        return super().execute(sql, *a, **k)

    def close(self):
        # run() closes the ledger in its finally; keep the in-memory DB alive for the assertions
        # and record whether a write transaction was still open at that moment.
        self.calls.append("close:" + ("open-txn" if self.in_transaction else "clean"))


def _drive(monkeypatch, tmp_path, *, fail_on_second_receipt=False):
    calls = []
    conn = sqlite3.connect(":memory:", factory=_Recorder)
    conn.calls = calls
    # THE TEST'S OWN CLOCK, ONLY ON THE TEST'S OWN THREAD, AND THE REAL SLEEP (2026-09-29). `w.time`
    # IS the process-wide time module, so every thread in the xdist worker read the old 1,002-value
    # iterator while the test ran, and under the full deploy run it ran out inside run_poll. Any other
    # thread now reads the real clock; this one gets 0, 0, then 1e9 for ever (one poll pass). The poll
    # runs at period 0, so it never needed sleep patched, and a no-op sleep only turned any other
    # thread's sleeping loop into a spin.
    import itertools
    clock = itertools.chain([0.0, 0.0], itertools.repeat(1e9))
    patch_local(monkeypatch, "monotonic", lambda: next(clock))
    monkeypatch.setattr(w, "get_connection", lambda path: conn)
    monkeypatch.setattr(w, "_verified_addresses", lambda c: {"0xabc": ("uid-1", "hnd")})
    monkeypatch.setattr(w, "_require_addresses", lambda c: {"0xabc": ("uid-1", "hnd")})
    monkeypatch.setattr(w, "quote_usd_price", lambda chain: 4000.0)
    monkeypatch.setattr(w, "erc20_decimals", lambda mint, url: 18)
    # No receipt, no buy: the real decoder returns None for a None receipt (an unread one).
    monkeypatch.setattr(w, "direct_buy_from_receipt", lambda receipt, *a, **k: None if receipt is None else {
        "mint": "0xm", "token_delta": 1.0, "source_size_usd": 10.0, "source_price": 1.0})
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_4663", "http://rpc.test")
    receipts = {"n": 0}

    def rpc(url, method, params):
        calls.append("rpc:" + method)
        if method == "eth_blockNumber":
            return {"result": "0x64"}
        if method == "eth_getLogs":
            return {"result": [{"transactionHash": "0xt1", "blockTimestamp": "0x0"},
                               {"transactionHash": "0xt2", "blockTimestamp": "0x0"}]}
        if method == "eth_getTransactionByHash":
            return {"result": {"from": "0xabc"}}
        if method == "eth_getTransactionReceipt":
            receipts["n"] += 1
            # EVERY read from the second on (2026-10-07): a one-off failure is now re-read like a
            # null and recovers inside the tick; a persistent one must still hold the whole tick.
            if fail_on_second_receipt and receipts["n"] >= 2:
                raise TimeoutError("receipt timed out")
            return {"result": {}}
        raise AssertionError(method)
    monkeypatch.setattr(w, "_rpc", rpc)
    w.run(duration_s=1.0, period_s=0.0)
    rows = conn.execute("SELECT COUNT(*) FROM onchain_wallet_detections").fetchone()[0]
    return calls, conn, rows


def test_every_write_lands_after_the_last_rpc_call(monkeypatch, tmp_path):
    calls, conn, rows = _drive(monkeypatch, tmp_path)
    assert rows == 2
    last_rpc = max(i for i, c in enumerate(calls) if c.startswith("rpc:"))
    writes = [i for i, c in enumerate(calls) if c == "write"]
    assert writes and min(writes) > last_rpc, calls
    assert "close:clean" in calls


def test_mid_loop_failure_leaves_no_open_transaction_and_no_rows(monkeypatch, tmp_path):
    calls, conn, rows = _drive(monkeypatch, tmp_path, fail_on_second_receipt=True)
    assert rows == 0
    assert "write" not in calls
    assert "close:clean" in calls


# --- a test that patches the process clock must survive the other threads alive in its worker ------
# 2026-09-29, deploy of 0aaab9a5 REFUSED: this file's `_drive` patched the GLOBAL time.monotonic with
# a 1,002-value iterator and the global time.sleep with a no-op. In the full xdist run another thread
# in the same worker read the clock ~1,000 times while it ran and the iterator ran out inside run_poll
# (StopIteration). Passes alone; fails beside whatever the scheduler puts next to it.

def test_a_thread_reading_the_clock_meanwhile_cannot_break_the_drive(monkeypatch, tmp_path):
    import threading
    import time as _real_time
    stop, reads = threading.Event(), [0]

    def reader():
        while not stop.is_set():
            _real_time.monotonic()               # the attribute _drive patches, read from elsewhere
            reads[0] += 1
            if reads[0] >= 5_000:
                stop.wait(0.01)
    t = threading.Thread(target=reader, name="clock-reader", daemon=True)
    t.start()
    try:
        calls, conn, rows = _drive(monkeypatch, tmp_path)
    finally:
        stop.set()
        t.join(2.0)
    assert rows == 2


def test_run_stops_the_threads_it_started(monkeypatch, tmp_path):
    """run() started the native-price and payment-price-warm threads and never stopped them, so
    every test that calls it left two live threads in its xdist worker for the rest of the run."""
    import threading
    monkeypatch.setattr(w, "_refresh_payment_prices", lambda: None)
    before = set(threading.enumerate())
    _drive(monkeypatch, tmp_path)
    mine = [t for t in threading.enumerate() if t not in before
            and t.name in ("native-price", "payment-price-warm")]
    for t in mine:
        t.join(2.0)
    assert [t.name for t in mine if t.is_alive()] == []
