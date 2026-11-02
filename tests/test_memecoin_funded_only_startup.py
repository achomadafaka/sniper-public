"""Funded-only startup removes paper work without removing the money hooks."""
import collections
import ast
from pathlib import Path
import sqlite3

import pytest

import scripts.memecoin_runner_loop as loop
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_paper_fanout as fanout
from sniper.services import memecoin_paper_ticks as ticks
from sniper.services import memecoin_runner as runner
from sniper.services.memecoin_follow_activity import follow_activity_pass
from sniper.settings import memecoin as cfg


NOW = "2026-10-12T09:00:00+00:00"


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


class _Auth:
    access, refresh, ca_id = "test", "test", "test"


def _item(eid, uid, side="buy"):
    return {"id": eid, "type": "large_" + side, "userId": uid,
            "tokenAddress": "MINT", "networkId": 1399811149, "createdAt": NOW,
            "body": {"userHandle": uid, "ticker": "T", "price": 1,
                     "marketCap": 100_000, "currentSizeUsd": 3_000,
                     "isFirstBuy": True}}


def _no_paper(*args, **kwargs):
    raise AssertionError("funded-only entered paper work")


def _float_startup(contexts, funded_only, logs):
    # Run the real startup block without starting the rest of the funded runner.
    tree = ast.parse(Path(loop.__file__).read_text())
    block = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                 and any(isinstance(child, ast.ImportFrom)
                         and child.module == "sniper.services.memecoin_native_float"
                         for child in ast.walk(node)))
    namespace = {"contexts": contexts, "_FUNDED_ONLY": funded_only, "_p": logs.append,
                 "traceback": __import__("traceback")}
    exec(compile(ast.Module(body=[block], type_ignores=[]), loop.__file__, "exec"), namespace)


@pytest.mark.parametrize("funded_only", [True, False])
def test_funded_wallet_display_sampler_survives_funded_only_startup(monkeypatch, funded_only):
    from sniper.services import memecoin_native_float as nf
    calls, logs = [], []
    monkeypatch.setattr(nf, "start_sampler", lambda **kw: calls.append(kw) or object())
    _float_startup({"solana": {"address": "SOL-wallet"}, "56": {"address": "EVM-wallet"}},
                   funded_only, logs)
    assert calls == [{"evm_address": "EVM-wallet"}]
    assert any("native float sampler started" in message for message in logs)


def test_wallet_display_sampler_keeps_no_context_and_failure_startup_safe(monkeypatch):
    from sniper.services import memecoin_native_float as nf
    monkeypatch.setattr(nf, "start_sampler", _no_paper)
    logs = []
    _float_startup({}, True, logs)
    assert logs == []
    _float_startup({"solana": {"address": "SOL-wallet"}}, True, logs)
    assert any("native float sampler did not start" in message for message in logs)


def test_funded_only_startup_never_creates_paper_or_capture_workers(monkeypatch):
    started = []
    monkeypatch.setattr(ticks, "start", _no_paper)
    monkeypatch.setattr(ticks, "start_exit_decision_writer",
                        lambda **kwargs: started.append("funded_decisions"), raising=False)
    monkeypatch.setattr(fanout, "PaperFanout", _no_paper)
    monkeypatch.setattr("sniper.services.memecoin_research_capture.ResearchCaptureWorker", _no_paper)

    capture = loop.start_paper_workers(funded_only=True, period_s=30,
                                      connect=_no_paper, now_fn=lambda: NOW,
                                      log_fn=lambda message: None)

    assert capture is None
    assert started == ["funded_decisions"]
    assert fanout.active() is None


def test_legacy_startup_keeps_paper_ticks_fanout_and_capture(monkeypatch):
    started = []
    monkeypatch.setattr(ticks, "start", lambda **kwargs: started.append("ticks"))
    monkeypatch.setattr(ticks, "start_exit_decision_writer", _no_paper, raising=False)
    monkeypatch.setattr(fanout, "enabled", lambda: True)
    monkeypatch.setattr(fanout, "_ACTIVE", None)

    class Thread:
        def __init__(self, *, target, name=None, daemon=False, **kwargs):
            self.name = name
        def start(self):
            started.append(self.name)
        def is_alive(self):
            return True

    monkeypatch.setattr(loop.threading, "Thread", Thread)
    capture = loop.start_paper_workers(funded_only=False, period_s=30,
                                      connect=_no_paper, now_fn=lambda: NOW,
                                      log_fn=lambda message: None)

    assert capture is not None
    assert started == ["ticks", "memecoin-paper-fanout", "research-capture"]
    assert isinstance(fanout.active(), fanout.PaperFanout)


def test_decision_writer_start_failure_does_not_restart_paper_or_abort_funded(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("thread creation refused")

    monkeypatch.setattr(ticks, "start_exit_decision_writer", fail)
    monkeypatch.setattr(ticks, "start", _no_paper)
    logs = []
    assert loop.start_paper_workers(funded_only=True, period_s=30, connect=_no_paper,
                                    now_fn=lambda: NOW, log_fn=logs.append) is None
    assert any("funded decision mark writer" in message and "NOT started" in message
               for message in logs)


def test_funded_only_feed_keeps_armed_buy_and_real_holder_sell_without_paper(monkeypatch):
    conn = _db()
    for uid in ("armed", "watch", "holder"):
        m.add_wallet(conn, uid, uid)
    m.set_wallet_real_approved(conn, "armed", True, chains=["solana"], force=True)
    pid = m.open_position(conn, source_user_id="holder", source_handle="holder",
                          mint="MINT", ticker="T", entry_price=1, entry_notional=50,
                          entry_fill_source="receipt", entry_lag_s=0, their_size_usd=3_000,
                          opened_at=NOW, strategy="control", execution_mode="real",
                          network_id="solana")
    assert pid
    monkeypatch.setattr(cfg, "MEMECOIN_FUNDED_ONLY", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROSTER_GATE_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", False)
    monkeypatch.setattr(fanout, "_ACTIVE", None)
    monkeypatch.setattr("sniper.services.memecoin_copy_trader.copy_event_to_arms", _no_paper)
    monkeypatch.setattr(runner, "_capture_pump_outcome", _no_paper)
    monkeypatch.setattr(runner, "_capture_theses", _no_paper)
    monkeypatch.setattr("sniper.services.memecoin_capture_v2.register_candidate_cohorts", _no_paper)
    seen = []
    items = [_item("a", "armed"), _item("w", "watch"), _item("s", "holder", "sell")]
    kwargs = dict(http=lambda *a, **k: (200, {"feed": items}, None),
                  now_fn=lambda: NOW, real_now_fn=lambda: NOW,
                  real_hook=lambda c, event: seen.append((event.fomo_event_id, event.side))
                  or {"status": "filled"}, candidate_hook=_no_paper, fill_fn=_no_paper)

    result = runner.run_once(conn, _Auth(), 50, **kwargs)
    runner.run_once(conn, _Auth(), 50, **kwargs)

    assert seen == [("a", "buy"), ("s", "sell")]
    assert result["real"] == {"filled": 2}
    assert result["opened"] == result["exits"] == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='paper'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_events WHERE fomo_event_id='w'").fetchone()[0] == 0


def test_funded_decision_writer_drains_real_marks_without_paper_or_network(monkeypatch):
    conn = _db()
    pid = m.open_position(conn, source_user_id="armed", source_handle="armed", mint="MINT",
                          ticker="T", entry_price=1, entry_notional=50,
                          entry_fill_source="receipt", entry_lag_s=0, their_size_usd=3_000,
                          opened_at=NOW, strategy="control", execution_mode="real",
                          network_id="solana")
    monkeypatch.setattr(ticks, "_DECISIONS", collections.deque())
    monkeypatch.setattr(ticks, "DECISION_COUNTS", collections.Counter())
    monkeypatch.setattr(ticks, "_RUNNING", {})
    monkeypatch.setattr(ticks, "PaperTickEngine", _no_paper)
    monkeypatch.setattr("sniper.services.memecoin_paper_sol_ticks.start", _no_paper)
    target = []

    class Thread:
        def __init__(self, **kwargs):
            target.append(kwargs["target"])
        def start(self):
            pass

    class Stop(BaseException):
        pass

    def sleep(_seconds):
        raise Stop()

    monkeypatch.setattr(ticks.threading, "Thread", Thread)
    assert ticks.queue_exit_decision(pid, '{"engine":"funded","reason":"hold"}')
    ticks.start_exit_decision_writer(connect=lambda: conn, sleep=sleep)
    with pytest.raises(Stop):
        target[0]()

    row = conn.execute("SELECT exit_decision_json FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
    assert row[0] == '{"engine":"funded","reason":"hold"}'
    assert ticks.DECISION_COUNTS["written"] == 1
    assert not ticks._DECISIONS


def test_funded_only_follow_skips_watch_wallet_reads_and_keeps_funded_hook(monkeypatch):
    conn = _db()
    for uid in ("armed", "watch"):
        m.add_wallet(conn, uid, uid)
    m.set_wallet_real_approved(conn, "armed", True, chains=["solana"], force=True)
    monkeypatch.setattr(cfg, "MEMECOIN_FUNDED_ONLY", True)
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", False)
    monkeypatch.setattr(fanout, "_ACTIVE", None)
    monkeypatch.setenv("MEMECOIN_PAPER_FANOUT_THREAD", "0")
    monkeypatch.setattr("sniper.services.memecoin_copy_trader.copy_event_to_arms", _no_paper)
    reads, funded = [], []
    swap = {"id": "swap-a", "inTokenAddress": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "outTokenAddress": "MINT", "humanUsdAmountIn": "3000",
            "humanUsdAmountOut": "3000", "inHumanAmount": "3000", "outHumanAmount": "3000",
            "createdAt": NOW, "isOffPlatform": "False", "networkId": 1399811149}

    result = follow_activity_pass(
        conn, None, 50, now_fn=lambda: NOW, real_now_fn=lambda: NOW,
        swaps_fn=lambda uid: reads.append(uid) or [swap], sleep_fn=lambda seconds: None,
        fill_fn=_no_paper, real_hook=lambda c, ev: funded.append(ev.source_user_id)
        or {"status": "filled"})

    assert reads == funded == ["armed"]
    assert result["checked"] == 1
    assert result["copied"] == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 0


def test_funded_only_preserves_explicit_roster_gate_off_admission(monkeypatch):
    conn = _db()
    m.add_wallet(conn, "watch", "watch")
    monkeypatch.setattr(cfg, "MEMECOIN_FUNDED_ONLY", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROSTER_GATE_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", False)
    seen = []

    def hook(conn, ev):
        seen.append(ev.fomo_event_id)
        conn.execute("UPDATE memecoin_events SET real_decision='filled' WHERE fomo_event_id=?",
                     (ev.fomo_event_id,))
        return {"status": "filled"}

    kwargs = dict(http=lambda *a, **k: (200, {"feed": [_item("w", "watch")]}, None),
                  now_fn=lambda: NOW, real_now_fn=lambda: NOW, real_hook=hook, fill_fn=_no_paper)
    runner.run_once(conn, _Auth(), 50, **kwargs)
    runner.run_once(conn, _Auth(), 50, **kwargs)

    assert seen == ["w"]


@pytest.mark.parametrize("with_success", [False, True])
def test_funded_mark_error_releases_writer_transaction_before_sleep(tmp_path, monkeypatch,
                                                                  with_success):
    path = tmp_path / "marks.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE memecoin_positions(id INTEGER PRIMARY KEY, exit_decision_json TEXT);
        INSERT INTO memecoin_positions(id) VALUES (1), (2);
        CREATE TRIGGER refuse_mark BEFORE UPDATE OF exit_decision_json ON memecoin_positions
        WHEN NEW.exit_decision_json = 'bad'
        BEGIN SELECT RAISE(FAIL, 'local mark refused'); END;
    """)
    conn.commit()
    other = sqlite3.connect(path, timeout=0.01)
    monkeypatch.setattr(ticks, "_DECISIONS", collections.deque())
    monkeypatch.setattr(ticks, "DECISION_COUNTS", collections.Counter())
    monkeypatch.setattr(ticks, "_RUNNING", {})
    target = []

    class Thread:
        def __init__(self, **kwargs):
            target.append(kwargs["target"])
        def start(self):
            pass

    class Stop(BaseException):
        pass

    def sleep(_seconds):
        raise Stop()

    monkeypatch.setattr(ticks.threading, "Thread", Thread)
    ticks.queue_exit_decision(1, "bad")
    if with_success:
        ticks.queue_exit_decision(2, "good")
    ticks.start_exit_decision_writer(connect=lambda: conn, sleep=sleep)
    try:
        with pytest.raises(Stop):
            target[0]()
        other.execute("UPDATE memecoin_positions SET exit_decision_json='funded' WHERE id=1")
        other.commit()
        assert conn.in_transaction is False
        assert other.execute("SELECT exit_decision_json FROM memecoin_positions WHERE id=1").fetchone()[0] == "funded"
        assert ticks.DECISION_COUNTS["errors"] == 1
        assert ticks.DECISION_COUNTS["written"] == int(with_success)
        assert conn.execute("SELECT exit_decision_json FROM memecoin_positions WHERE id=2").fetchone()[0] == ("good" if with_success else None)
    finally:
        conn.close()
        other.close()
