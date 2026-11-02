"""Curve coverage requires successful source progress and durable discontinuities."""
import asyncio
import json
import sqlite3

import pytest

from sniper.util import clock
from test_memecoin_desk_universe_worker import W, frame_meta, frame_tx, worker


def beat(w):
    w.heartbeat(True)
    return json.loads((w.base / "universe.heartbeat").read_text())


@pytest.fixture
def source(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(W.time, "time", lambda: now[0])
    monkeypatch.setattr(clock, "offset_s", lambda: 0.0)
    monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_CAPTURE", "0")
    w, _ = worker(tmp_path)
    return w, now


def test_idle_commits_never_create_or_advance_source_coverage(source):
    w, now = source
    w.commit_files()
    assert beat(w)["curve_committed_through"] is None
    w.process(frame_meta(10, 95), received_at=95.0)
    w.commit_files()
    assert (beat(w)["curve_committed_through"], beat(w)["curve_capture_from"]) == (95.0, 95.0)
    now[0] = 120.0
    w.commit_files()
    assert (beat(w)["curve_committed_through"], beat(w)["curve_capture_from"]) == (95.0, 95.0)


def test_receipt_cut_waits_for_a_drained_ring_and_keeps_the_original_receipt(source):
    w, _now = source
    w.process(frame_meta(10, 95), received_at=95.0)
    w.ring.append((b"pending", 96.0))
    w.commit_files()
    assert beat(w)["curve_committed_through"] is None
    w.ring.clear()
    w.commit_files()
    assert beat(w)["curve_committed_through"] == 95.0


def test_delayed_chain_head_caps_the_receipt_cut_in_host_units(source, monkeypatch):
    w, now = source
    now[0] = 250.0
    monkeypatch.setattr(clock, "offset_s", lambda: 120.0)
    w.process(frame_meta(10, 100), received_at=245.0)
    w.commit_files()
    assert (beat(w)["curve_committed_through"], beat(w)["curve_capture_from"]) == (220.0, 245.0)


def test_non_block_frames_cannot_refresh_a_stalled_source_head(source):
    w, now = source
    w.process(frame_meta(10, 95), received_at=95.0)
    w.commit_files()
    now[0] = 120.0
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    ping = pb.SubscribeUpdate()
    ping.ping.SetInParent()
    w.process(ping.SerializeToString(), received_at=119.0)
    w.commit_files()
    assert beat(w)["curve_committed_through"] == 95.0


def test_failed_curve_commit_revokes_proof_until_a_new_source_block(source, monkeypatch):
    w, now = source
    w.process(frame_meta(10, 95), received_at=95.0)
    w.commit_files()
    now[0] = 101.0
    w.process(frame_meta(11, 100), received_at=100.0)
    real = w.files.commit

    def fail(**kwargs):
        raise sqlite3.OperationalError("offline commit failure")

    monkeypatch.setattr(w.files, "commit", fail)
    with pytest.raises(sqlite3.OperationalError):
        w.commit_files()
    assert beat(w)["curve_committed_through"] is None
    monkeypatch.setattr(w.files, "commit", real)
    w.commit_files()
    assert beat(w)["curve_committed_through"] is None
    w.process(frame_meta(12, 101), received_at=101.0)
    w.commit_files()
    assert (beat(w)["curve_committed_through"], beat(w)["curve_capture_from"]) == (101.0, 101.0)


def test_failed_gap_write_blocks_proof_and_retries_before_new_continuity(source, monkeypatch):
    w, now = source
    w.process(frame_meta(10, 95), received_at=95.0)
    w.commit_files()
    real = w.files.conn

    def no_gaps(stream, at):
        if stream == "gaps":
            raise sqlite3.OperationalError("offline gap write failure")
        return real(stream, at)

    monkeypatch.setattr(w.files, "conn", no_gaps)
    now[0] = 96.0
    try:
        w.gap("ring_overflow")
    except sqlite3.Error:
        pass
    assert beat(w)["curve_committed_through"] is None
    now[0] = 98.0
    w.process(frame_meta(11, 97), received_at=97.0)
    w.commit_files()
    assert beat(w)["curve_committed_through"] is None
    monkeypatch.setattr(w.files, "conn", real)
    w.commit_files()
    assert (beat(w)["curve_committed_through"], beat(w)["curve_capture_from"]) == (97.0, 97.0)
    assert real("gaps", 96.0).execute("SELECT at,cause FROM gaps").fetchall() == [(96.0, "ring_overflow")]


def test_failed_dequeued_curve_frame_leaves_a_durable_gap_and_no_proof(source, monkeypatch):
    w, now = source
    w.process(frame_meta(10, 95), received_at=95.0)
    w.commit_files()
    now[0] = 100.0
    w.push(b"offline malformed frame", received_at=99.0)
    monkeypatch.setattr(w, "check_disk", lambda: None)
    original = w.heartbeat

    def stop(connected):
        original(connected)
        w.stop = True

    monkeypatch.setattr(w, "heartbeat", stop)
    asyncio.run(W.processor(w))
    saved = json.loads((w.base / "universe.heartbeat").read_text())
    assert saved["curve_committed_through"] is None and saved["curve_capture_from"] is None
    assert w.files.conn("gaps", now[0]).execute("SELECT cause FROM gaps").fetchall() == [("process_error",)]


def test_unclassified_curve_decode_loss_is_a_gap_not_successful_processing(source):
    w, _now = source
    w.process(frame_meta(10, 95), received_at=95.0)
    w.commit_files()
    w.process(frame_tx(11, 1, bytes(8) + b"unsupported"), received_at=99.0)
    w.commit_files()
    assert beat(w)["curve_committed_through"] is None
    assert w.files.conn("gaps", 100.0).execute("SELECT cause FROM gaps").fetchall() == [("decode_blocked",)]


@pytest.fixture
def ps_source(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(W.time, "time", lambda: now[0])
    monkeypatch.setattr(clock, "offset_s", lambda: 0.0)
    monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_CAPTURE", "1")
    monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_EMISSIONS", "0")
    w, _ = worker(tmp_path)
    return w, now


def test_ps_idle_commit_cannot_create_or_refresh_its_source_cut(ps_source):
    w, now = ps_source
    w.commit_files()
    assert beat(w)["ps_committed_through"] is None
    w.process(frame_meta(10, 95), received_at=95.0)
    w.commit_files()
    assert (beat(w)["ps_committed_through"], beat(w)["ps_capture_from"]) == (95.0, 95.0)
    now[0] = 120.0
    w.commit_files()
    assert beat(w)["ps_committed_through"] == 95.0


def test_ps_cut_waits_until_its_own_ring_is_drained(ps_source):
    w, _now = ps_source
    w.process(frame_meta(10, 95), received_at=95.0)
    w.ps_ring.append((b"pending", 96.0))
    w.commit_files()
    assert beat(w)["ps_committed_through"] is None
    w.ps_ring.clear()
    w.commit_files()
    assert beat(w)["ps_committed_through"] == 95.0


def test_ps_stop_and_restart_need_a_new_source_block(ps_source):
    w, now = ps_source
    w.process(frame_meta(10, 95), received_at=95.0)
    w.commit_files()
    now[0] = 96.0
    w.ps_gap("disk_cap", 96.0)
    w.ps_capture = False
    w.commit_files()
    assert (beat(w)["ps_committed_through"], beat(w)["ps_capture_from"]) == (None, None)
    w.ps_capture = True
    w.commit_files()
    assert beat(w)["ps_committed_through"] is None
    now[0] = 100.0
    w.process(frame_meta(11, 99), received_at=99.0)
    w.commit_files()
    assert (beat(w)["ps_committed_through"], beat(w)["ps_capture_from"]) == (99.0, 99.0)


def test_ps_failed_commit_revokes_the_current_proof(ps_source, monkeypatch):
    w, _now = ps_source
    w.process(frame_meta(10, 95), received_at=95.0)
    w.commit_files()

    def fail(**kwargs):
        kwargs["ps_error"](sqlite3.OperationalError("offline PS commit failure"))

    monkeypatch.setattr(w.files, "commit", fail)
    w.commit_files()
    assert (beat(w)["ps_committed_through"], beat(w)["ps_capture_from"]) == (None, None)


def test_failed_dequeued_ps_frame_blinds_ps_without_claiming_a_curve_gap(ps_source, monkeypatch):
    w, now = ps_source
    w.process(frame_meta(10, 95), received_at=95.0)
    w.commit_files()
    w.push(b"offline malformed PS frame", received_at=99.0, pumpswap=True)
    monkeypatch.setattr(w, "check_disk", lambda: None)
    original = w.heartbeat

    def stop(connected):
        original(connected)
        w.stop = True

    monkeypatch.setattr(w, "heartbeat", stop)
    asyncio.run(W.processor(w))
    saved = json.loads((w.base / "universe.heartbeat").read_text())
    assert saved["ps_committed_through"] is None and saved["ps_capture_from"] is None
    assert saved["curve_committed_through"] == 95.0
    assert w.files.conn("gaps_ps", now[0]).execute("SELECT cause FROM gaps_ps").fetchall() == [("process_error",)]


def test_pool_scoped_decode_losses_are_durable_without_resetting_global_ps_continuity(ps_source):
    w, now = ps_source
    w.process(frame_meta(10, 95), received_at=95.0)
    w.commit_files()
    w.ps_gap("decode_loss", 96.0)
    now[0] = 100.0
    w.process(frame_meta(11, 99), received_at=99.0)
    w.commit_files()
    assert (beat(w)["ps_committed_through"], beat(w)["ps_capture_from"]) == (99.0, 95.0)
    assert w.files.conn("gaps_ps", 100.0).execute("SELECT cause FROM gaps_ps").fetchall() == [("decode_loss",)]
