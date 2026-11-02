"""The EVM gas top-up pass runs on its own thread (2026-09-27, Alpha GO B1).

Inline in the funded main loop it took 58.8 s and 40.6 s at 12:35Z: hedged 12 s reads against a
failing endpoint set, with the 8 s read budget checked only between reads.
"""
import json
import os

from sniper.services import memecoin_evm_gas_topup as G
from sniper.services import memecoin_runner as run

SRC = open(os.path.join(os.path.dirname(__file__), "..", "scripts",
                        "memecoin_runner_loop.py")).read()


class _Conn:
    def __init__(self):
        self.rolled_back = 0

    def rollback(self):
        self.rolled_back += 1


def _worker(pass_fn, logs):
    return G.EvmGasTopupWorker(pass_fn=pass_fn, connect=_Conn, now_iso=lambda: "2026-09-27T13:40:00Z",
                               period_s=15, log=logs.append)


def test_a_pass_runs_on_the_workers_connection_and_is_reported():
    logs, seen = [], []
    w = _worker(lambda conn, now: (seen.append((conn, now)),
                                   {"state": "topup", "status": "confirmed"})[1], logs)
    conn = _Conn()
    w.run_once(conn)
    assert seen == [(conn, "2026-09-27T13:40:00Z")]
    assert w.status()["passes"] == 1 and w.status()["last_state"] == "topup"
    assert logs and "EVM gas top-up" in logs[0]


def test_a_quiet_state_is_not_logged_and_a_failure_rolls_back_and_counts():
    logs = []
    assert not _worker(lambda c, n: {"state": "above_low_water"}, logs).run_once(_Conn()) is None
    assert logs == []

    def boom(conn, now):
        raise RuntimeError("rpc down")
    w, conn = _worker(boom, logs), _Conn()
    w.run_once(conn)
    assert conn.rolled_back == 1 and w.status()["failures"] == 1
    assert w.status()["last_error"] == "RuntimeError"


def test_the_flag_off_keeps_the_pass_inline():
    assert G.thread_enabled({"MEMECOIN_EVM_GAS_TOPUP_THREAD": "0"}) is False
    assert G.thread_enabled({}) is True


def test_the_loop_no_longer_runs_the_pass_when_the_thread_does():
    start = SRC.index("_egt_mod.EvmGasTopupWorker(")
    assert "_egt_mod.thread_enabled()" in SRC[start - 400:start]
    assert 'name="memecoin-evm-gas-topup"' in SRC[start:start + 800]
    inline = SRC.index("_egt = run_evm_gas_topup_pass(")
    assert "_evm_gas_topup_worker is None" in SRC[inline - 300:inline]
    assert "evm_gas_topup=(_evm_gas_topup_worker.status()" in SRC


def test_the_heartbeat_carries_the_top_up_thread(tmp_path):
    path = tmp_path / "hb.json"
    run.write_heartbeat({}, True, path=str(path), evm_gas_topup={
        "last_pass_at": "t", "last_pass_s": 0.4, "last_state": "topup", "passes": 3,
        "failures": 0, "last_error": None, "not_listed": 1})
    hb = json.loads(path.read_text())["evm_gas_topup"]
    assert hb["passes"] == 3 and "not_listed" not in hb


def test_a_ledger_connect_failure_is_logged_once_and_retried_not_fatal():
    logs, attempts, sleeps = [], [], []

    def connect():
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("database is locked")
        return _Conn()
    w = G.EvmGasTopupWorker(pass_fn=lambda c, n: {}, connect=connect, now_iso=lambda: "t",
                            period_s=15, log=logs.append, sleep=sleeps.append)
    conn = w.connect_with_retry()
    assert isinstance(conn, _Conn) and len(attempts) == 3 and sleeps == [15.0, 15.0]
    assert len([line for line in logs if "connect failed" in line]) == 1
    assert w.status()["failures"] == 2 and w.status()["last_error"] == "connect_failed"
