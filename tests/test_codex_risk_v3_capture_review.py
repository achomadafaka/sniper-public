"""Offline witnesses for the capture proof consumed by RISK. Root owns execution."""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from test_desk_regime import day_of, make_tape
from test_desk_risk_break_v2 import FILL, P, W, path, pre_history
from test_memecoin_desk_pumpswap_worker import W as U, worker


def _beat(desk, now, **fields):
    (desk / "universe.heartbeat").write_text(json.dumps({
        "ts": now, "connected": True, "sol_usd": 100.0, "sol_age_s": 0.0,
        "curve_committed_through": now, "curve_capture_from": now - 600, **fields,
    }))


def test_an_idle_drained_ring_does_not_prove_that_the_source_stream_is_current(tmp_path, monkeypatch):
    monkeypatch.setattr(U.time, "time", lambda: 1000.0)
    w = worker(tmp_path, monkeypatch, enabled=False)
    # No frame or source block has ever arrived. This is the periodic processor path during a silent stream stall.
    w.commit_files()
    w.heartbeat(True)
    assert W.capture_healthy(tmp_path, 1000.0) is False


def test_a_dequeued_curve_processing_failure_invalidates_capture_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(U.time, "time", lambda: 1000.0)
    w = worker(tmp_path, monkeypatch, enabled=False)
    w.push(b"offline-failed-frame", received_at=999.0)

    def fail(*args, **kwargs):
        raise RuntimeError("offline decoder failure")

    monkeypatch.setattr(w, "process", fail)
    monkeypatch.setattr(w, "check_disk", lambda: None)
    real_beat = w.heartbeat

    def stop_after_beat(connected):
        real_beat(connected)
        w.stop = True

    monkeypatch.setattr(w, "heartbeat", stop_after_beat)
    asyncio.run(U.processor(w))
    assert w.stats["process_errors"] == 1
    assert W.capture_healthy(tmp_path, 1000.0) is False


def test_structure_break_cannot_close_a_bucket_beyond_the_committed_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "SB_CLOSES", True)
    monkeypatch.setattr(W, "pre_fill_bars", lambda *a: pre_history())
    pos = {"mint": "M", "opened_at": datetime.fromtimestamp(FILL, timezone.utc).isoformat()}
    # The second low bucket only has seconds10/11 committed. Its missing seconds12..14 can reclaim.
    bars = [dict(b, venue="curve") for b in path([P] * 5 + [P * .7] * 7, buy=1.0, sell=1.0)]
    now, committed = float(FILL + 17), float(FILL + 12)
    _beat(tmp_path, now, curve_committed_through=committed)
    assert W.structure_break_of(pos, bars, tmp_path, now, 0.0) is None


def test_a_future_committed_cut_is_unknown_after_a_host_clock_step(tmp_path):
    _beat(tmp_path, 1000.0, curve_committed_through=1120.0)
    assert W.capture_healthy(tmp_path, 1000.0) is False


@pytest.mark.parametrize("committed", [None, 1_792_000_005.0 - 60.0])
def test_regime_cannot_call_silent_coin_quiet_without_current_committed_capture(tmp_path, monkeypatch, committed):
    monkeypatch.setattr(W, "REGIME", True)
    hi = 1_792_000_005.0
    desk = make_tape(tmp_path, "M", [(hi - 200, 1e-9, True)], day=day_of(hi))
    with sqlite3.connect(desk / "swaps" / (day_of(hi) + ".db")) as c:
        c.execute("INSERT INTO swaps VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (hi - 1, 100, "other", 0, "OTHER", 1, 1_000_000_000, "a", 1_000_000, 1_000_000_000_000, 0))
    _beat(desk, hi + 2, curve_committed_through=committed)
    assert W.regime_of({"mint": "M"}, desk, hi + 2)["regime"] == "unknown"


def test_old_writer_mark_without_a_continuity_start_is_not_proof(tmp_path):
    _beat(tmp_path, 1000.0, curve_capture_from=None)
    assert W.capture_healthy(tmp_path, 1000.0) is False
