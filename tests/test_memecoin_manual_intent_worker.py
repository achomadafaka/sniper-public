"""Build step 2 (spec 2026-10-21-desk-execution-plumbing §2.4 'Drain worker'): manual and desk
intents drain on their own woken thread instead of waiting for the main loop's pass (historical
claim lag p50 3.54 s / p90 11.93 s, n=445). Operator intents are claimed before desk intents."""
from __future__ import annotations

import ast
import pathlib
import sqlite3
import threading
import time

from test_memecoin_desk_origin import _metadata

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_manual_intents as mi
from sniper.services.memecoin_manual_exit_intents import ManualExitWorker
from sniper.services.memecoin_onchain_wake import LaneWake
import sniper.settings.memecoin as cfg
from _source_guard import code_of

GOOD = {"tp_pct": 60.0, "sl_pct": 25.0, "trail_pct": 15.0}


def _iso(offset_s=0.0):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


def _file_conn(path):
    c = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ledger.db"
    c = _file_conn(path)
    m._ensure_memecoin_tables_in_transaction(c)
    c.commit()
    c.close()
    return path


def test_enqueue_wakes_the_intent_worker(db, monkeypatch):
    sent = []
    monkeypatch.setattr(mi, "send_wake", sent.append)
    c = _file_conn(db)
    mi.enqueue(c, mint="M", network_id="solana", now_iso=_iso())
    assert sent == [mi.wake_address()]
    assert mi.wake_address().endswith("-manual-intents")


def test_operator_intents_are_claimed_before_desk_intents(db, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CHAINS", frozenset({"solana"}))
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CLIP_USD", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RAIL_BOUNDS",
                        cfg.parse_desk_rail_bounds("tp_pct:15-150,sl_pct:10-40,trail_pct:8-30"))
    c = _file_conn(db)
    desk = mi.enqueue(c, mint="D", network_id="solana", now_iso=_iso(-5), size_usd=25.0,
                      rails=GOOD, rails_mode="custom", signal_source="desk", desk_ticket_id="t", desk_metadata=_metadata(c))
    manual = mi.enqueue(c, mint="O", network_id="solana", now_iso=_iso())
    assert mi.claim_next(c, now_iso=_iso())["intent_id"] == manual
    assert mi.claim_next(c, now_iso=_iso())["intent_id"] == desk


def test_the_worker_claims_within_a_wake_not_a_main_loop_pass(db, tmp_path, monkeypatch):
    address = str(tmp_path / "wake-manual-intents")
    monkeypatch.setattr(mi, "wake_address", lambda: address)
    claimed = threading.Event()

    def buy_fn(conn, mint, uid=None, **kw):
        claimed.set()
        return {"status": "refused", "reason": "test"}

    worker = ManualExitWorker(
        connect=lambda: _file_conn(db), observe=lambda: _file_conn(db),
        drain_fn=lambda conn: mi.drain(conn, buy_fn, now_fn=_iso),
        pending_fn=mi.pending_count, name="manual-intents", wake=LaneWake(address))
    worker.start()
    try:
        time.sleep(0.2)                     # the worker is idle, waiting on its wake
        c = _file_conn(db)
        started = time.monotonic()
        mi.enqueue(c, mint="M", network_id="solana", now_iso=_iso())
        assert claimed.wait(2.0)
        assert time.monotonic() - started < 0.5, "claim waited for a poll, not the wake"
    finally:
        worker.stop()


def test_the_runner_drains_intents_on_the_worker_with_a_main_loop_fallback():
    body = code_of(pathlib.Path("scripts/memecoin_runner_loop.py"))
    assert "name='manual-intents'" in body
    assert "LaneWake(_mi_wake_address())" in body
    tree = ast.parse(body)
    falls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "_manual_intents.fallback"]
    assert len(falls) == 1, "the main loop must keep exactly one fallback pass"


def test_the_heartbeat_carries_the_intent_worker_status():
    from sniper.services import memecoin_runner
    import inspect
    src = inspect.getsource(memecoin_runner)
    assert 'payload["manual_intents"]' in src


def test_a_stalled_intent_worker_pages_once_per_stall():
    """Gamma on cdfed0d31: a stalled pass holds pass_lock, so the main-loop fallback cannot cover
    it; the operator must hear about it, once per stall rather than every cycle."""
    body = code_of(pathlib.Path("scripts/memecoin_runner_loop.py"))
    assert "MANUAL-INTENTS-STALLED" in body
    assert "if _mi_stalled and (not _mi_stall_paged[0]):" in body
    assert "_mi_stall_paged[0] = _mi_stalled" in body
