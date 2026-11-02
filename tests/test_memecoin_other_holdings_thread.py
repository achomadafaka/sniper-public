"""The Solana other-holdings pass runs on its own thread (2026-09-27, Alpha GO C1).

Inline in the funded main loop the "other_holdings" maint stage reached 6.2 s: the snapshot's
token-name urlopen has a socket timeout that does not bound DNS. The sell code and its non-blocking
wallet lock are unchanged; only where the pass runs changes.
"""
import json
import os
import threading
import time

import pytest

from sniper.services import memecoin_other_holdings as oh
from sniper.services import memecoin_runner as run

SRC = open(os.path.join(os.path.dirname(__file__), "..", "scripts",
                        "memecoin_runner_loop.py")).read()
CTX = {"exit_quote_fn": object(), "confirm_once_fn": object(), "block_height_fn": object()}


class _Conn:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def _fresh_worker(monkeypatch):
    monkeypatch.setattr(oh, "_WORKER", {"thread": None, "last_sell_try": 0.0,
                                        "pass_started_at": None, "last_pass_s": None,
                                        "last_snapshot_at": None, "failures": 0})


def _start(conn, logs, **due):
    kw = dict(snapshot_due=False, sell_due=False, resolve_due=False)
    kw.update(due)
    return oh.start_pass(CTX, keypair=object(), pubkey="PK", rpc_url="rpc", rpc_candidates=["rpc"],
                         min_sell_usd=1.0, now_fn=lambda: "2026-09-27T15:00:00Z",
                         log_fn=logs.append, connect=lambda: conn, **kw)


def _join():
    oh._WORKER["thread"].join(5)
    assert not oh._WORKER["thread"].is_alive()


def test_a_hung_snapshot_leaves_the_loop_and_shows_in_the_status(monkeypatch):
    gate, seen, conn, logs = threading.Event(), [], _Conn(), []

    def snapshot(c, **kw):
        seen.append((c, kw["owner"]))
        gate.wait(5)
        return {"state": "ok"}
    monkeypatch.setattr(oh, "snapshot_once", snapshot)
    t0 = time.monotonic()
    assert _start(conn, logs, snapshot_due=True) is True
    assert time.monotonic() - t0 < 0.5
    status = oh.worker_status()
    assert status["alive"] and status["pass_started_at"] == "2026-09-27T15:00:00Z"
    assert _start(conn, logs, snapshot_due=True) is False          # single flight
    gate.set()
    _join()
    assert seen == [(conn, "PK")] and conn.closed == 1
    status = oh.worker_status()
    assert not status["alive"] and status["last_snapshot_at"] == "2026-09-27T15:00:00Z"
    assert status["last_pass_s"] is not None and status["failures"] == 0


def test_a_sell_that_acted_rescans_in_the_same_pass_and_a_busy_wallet_waits(monkeypatch):
    calls, conn, logs = [], _Conn(), []
    state = {"sell": "sold"}
    monkeypatch.setattr(oh, "sell_once", lambda c, **kw: (calls.append(("sell", c)),
                                                           {"state": state["sell"]})[1])
    monkeypatch.setattr(oh, "snapshot_once", lambda c, **kw: (calls.append(("scan", c)),
                                                              {"state": "ok"})[1])
    assert _start(conn, logs, sell_due=True) is True
    _join()
    assert calls == [("sell", conn), ("scan", conn)]
    assert logs and "other holdings sell" in logs[0]

    calls.clear(), logs.clear()
    state["sell"] = "wallet_busy"
    oh._WORKER["last_sell_try"] = 0.0
    assert _start(conn, logs, sell_due=True) is True
    _join()
    assert calls == [("sell", conn)] and logs == []                # quiet: no log, no rescan
    assert _start(conn, logs, sell_due=True) is False              # retried after SELL_RETRY_S


def test_a_resolve_that_settled_rescans_and_no_work_starts_no_thread(monkeypatch):
    calls, conn, logs = [], _Conn(), []
    monkeypatch.setattr(oh, "resolve_once", lambda c, **kw: (calls.append("resolve"), {7: "ok"})[1])
    monkeypatch.setattr(oh, "snapshot_once", lambda c, **kw: (calls.append("scan"),
                                                              {"state": "ok"})[1])
    assert _start(conn, logs) is False and oh._WORKER["thread"] is None
    assert _start(conn, logs, resolve_due=True) is True
    _join()
    assert calls == ["resolve", "scan"]


def test_a_failed_pass_counts_and_closes_its_connection(monkeypatch):
    conn = _Conn()

    def boom(c, **kw):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(oh, "snapshot_once", boom)
    assert _start(conn, [], snapshot_due=True) is True
    _join()
    assert oh.worker_status()["failures"] == 1 and conn.closed == 1
    assert oh.worker_status()["last_snapshot_at"] is None


def test_the_flag_off_keeps_the_pass_inline():
    assert oh.thread_enabled({"MEMECOIN_SOLANA_HOLDINGS_THREAD": "0"}) is False
    assert oh.thread_enabled({}) is True
    start = SRC.index("if _oh.thread_enabled():")
    block = SRC[start:SRC.index('_p("[err] other holdings failed:', start)]
    threaded, inline = block.split("\n                    else:\n", 1)
    assert "_oh.start_pass(" in threaded and "_oh.snapshot_once(" not in threaded
    for call in ("_oh.snapshot_once(", "_oh.sell_once(", "_oh.resolve_once("):
        assert call in inline


def test_the_heartbeat_carries_the_holdings_thread(tmp_path):
    assert "other_holdings=_other_holdings_status()" in SRC
    path = tmp_path / "hb.json"
    run.write_heartbeat({}, True, path=str(path), other_holdings={
        "alive": True, "pass_started_at": "t", "last_pass_s": 0.4, "last_snapshot_at": "t",
        "failures": 0, "evm_ready_chains": ["56", "4663"], "not_listed": 1})
    hb = json.loads(path.read_text())["other_holdings"]
    assert hb["alive"] is True and "not_listed" not in hb
    assert hb["evm_ready_chains"] == ["56", "4663"]


def test_the_residue_passes_run_on_the_snapshot_cadence_and_never_skip_the_snapshot(monkeypatch):
    """Quote-residue credits (2026-10-07): read exits, then credit sells, on the worker, with the
    ledger connection and the funded receipt reader; a failure there still runs the snapshot."""
    from sniper.services import memecoin_quote_residue as Q
    from sniper.settings import memecoin as cfg
    conn, logs, calls = _Conn(), [], []
    receipt = object()
    monkeypatch.setattr(oh, "snapshot_once", lambda c, **kw: calls.append("snapshot") or {"state": "ok"})
    monkeypatch.setattr(Q, "read_exit_residue_once",
                        lambda c, **kw: calls.append(("read", c, kw["receipt_fn"], kw["owner"])) or [])

    def credit(c, **kw):
        calls.append("credit")
        raise RuntimeError("boom")
    monkeypatch.setattr(Q, "credit_sells_once", credit)
    monkeypatch.setattr(cfg, "MEMECOIN_QUOTE_RESIDUE_CREDITS", True, raising=False)
    ctx = {**CTX, "receipt_fn": receipt}
    assert oh.start_pass(ctx, keypair=object(), pubkey="PK", rpc_url="rpc", rpc_candidates=["rpc"],
                         snapshot_due=True, sell_due=False, resolve_due=False, min_sell_usd=1.0,
                         now_fn=lambda: "2026-10-07T16:00:00Z", log_fn=logs.append, connect=lambda: conn)
    _join()
    assert calls == [("read", conn, receipt, "PK"), "credit", "snapshot"]
    calls.clear()
    monkeypatch.setattr(cfg, "MEMECOIN_QUOTE_RESIDUE_CREDITS", False, raising=False)
    assert oh.start_pass(ctx, keypair=object(), pubkey="PK", rpc_url="rpc", rpc_candidates=["rpc"],
                         snapshot_due=True, sell_due=False, resolve_due=False, min_sell_usd=1.0,
                         now_fn=lambda: "2026-10-07T16:05:00Z", log_fn=logs.append, connect=lambda: conn)
    _join()
    assert calls == ["snapshot"]                                    # flag off: no reads at all


def test_the_seams_context_exposes_the_funded_receipt_reader():
    src = open(os.path.join(os.path.dirname(__file__), "..", "sniper", "execution",
                            "solana_real_seams.py")).read()
    assert '"receipt_fn": _entry_receipt' in src
