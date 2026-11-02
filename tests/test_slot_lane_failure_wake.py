"""A slot-lane attempt's terminal result wakes the on-chain lane's deferred sweep (Alpha, 2026-10-02).

A detection the slot lane still owns is held in the on-chain lane's `deferred` dict until the slot
attempt is conclusive. Before this, only the lane's 1 s poll re-checked it: on 63 production
fallbacks whose detection was already held when the slot send failed (09-20..10-02), slot attempt
completed -> fallback attempt started was p50 615 ms, p90 851 ms, max 941 ms. The lane now sets the
on-chain lane's existing wake after every finished live attempt, AFTER the release and the attempt's
terminal row are committed, so the sweep it wakes reads the conclusive outcome."""
import contextlib
import sqlite3
import threading
import time

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_onchain_lane as ol
from sniper.services import memecoin_slot_lane as lane

from test_memecoin_slot_lane import _buy, _snap, CURVE, CURVE_EVENT

NOW = "2026-09-26T20:30:00+00:00"
DETECTED = "2026-09-26T20:30:01+00:00"


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "ledger.db"
    c = _connect(path)
    m.ensure_memecoin_tables(c)
    c.commit()
    c.close()
    return path


def _connect(path):
    c = sqlite3.connect(path, timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _slot_lane(conn, tmp_path, monkeypatch, *, execute, wake):
    from sniper.execution import exec_lock
    monkeypatch.setattr(exec_lock, "_lock_path",
                        lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    return lane.SlotLane(
        conn=conn, executor={"slot_execute_fn": execute},
        build_fn=lambda buy, clip, snap: ("MESSAGE", {}),
        snapshot_fn=lambda: _snap(time.monotonic()), armed_ids_fn=lambda: {"w1"},
        kill_tripped_fn=lambda: False, now_iso_fn=lambda: NOW, lock_key="sol-test",
        mode="live", shadow_clip_fn=lambda: 25.0, wake=wake)


def _failed_send(conn, pending, message, **_k):
    """The executor's landed-and-failed path: release with the chain's reason, then return."""
    m.release_pending(conn, pending["id"], "chain_error:AccountNotInitialized")
    return {"status": "failed", "reason": "chain_error:AccountNotInitialized"}


class _ReadsTheOutcome:
    """A wake whose set() reads what a woken sweep would read, on ANOTHER connection."""

    def __init__(self, path):
        self.path, self.seen = path, []

    def set(self):
        with contextlib.closing(_connect(self.path)) as other:
            self.seen.append(ol.slot_lane_outcome(other, CURVE["signature"], DETECTED))


def _scaled(monkeypatch):
    import dataclasses
    from sniper.services import memecoin_market_state as ms
    real = ms.classify_funded_entry
    monkeypatch.setattr(ms, "classify_funded_entry",
                        lambda *a, **k: dataclasses.replace(real(*a, **k), risk_scale=0.75))


@pytest.mark.parametrize("case", ["send_failed", "scaled_clip"])
def test_the_wake_fires_once_after_the_failure_is_committed(ledger, tmp_path, monkeypatch, case):
    # send_failed: the release commits inside the executor. scaled_clip: nothing is reserved and
    # only the attempt's terminal row (trace.finish) makes it `failed`, so a wake set before that
    # commit would wake a sweep that still reads in_flight and goes back to sleep for a whole poll.
    if case == "scaled_clip":
        _scaled(monkeypatch)
    wake = _ReadsTheOutcome(ledger)
    with contextlib.closing(_connect(ledger)) as conn:
        result = _slot_lane(conn, tmp_path, monkeypatch, execute=_failed_send, wake=wake).handle(_buy())
    assert result["status"] in ("failed", "rejected"), result
    assert wake.seen == ["failed"]


def test_a_fill_wakes_too_so_the_held_row_is_dropped_at_once(ledger, tmp_path, monkeypatch):
    def filled(conn, pending, message, **_k):
        conn.execute("UPDATE memecoin_pending_entries SET status='confirmed' WHERE id=?",
                     (pending["id"],))
        conn.commit()
        return {"status": "filled"}
    wake = _ReadsTheOutcome(ledger)
    with contextlib.closing(_connect(ledger)) as conn:
        _slot_lane(conn, tmp_path, monkeypatch, execute=filled, wake=wake).handle(_buy())
    assert wake.seen == ["filled"]


def test_a_refusal_before_any_attempt_row_sets_no_wake(ledger, tmp_path, monkeypatch):
    # Nothing was written, so the on-chain lane never deferred this buy: no sweep to wake.
    wake = _ReadsTheOutcome(ledger)
    with contextlib.closing(_connect(ledger)) as conn:
        ln = _slot_lane(conn, tmp_path, monkeypatch, execute=_failed_send, wake=wake)
        ln.armed_ids_fn = lambda: set()
        assert ln.handle(_buy())["status"] == "rejected"
    assert wake.seen == []


def test_the_held_detection_gets_its_one_fallback_within_the_poll(ledger, tmp_path, monkeypatch):
    """End to end on two threads and two connections, with a 5 s poll: the on-chain lane holds the
    detection while the slot send is in flight, and the slot failure releases it to ONE ordinary
    attempt long before the poll would have."""
    wake = threading.Event()
    in_flight, go, fallback_ran = threading.Event(), threading.Event(), threading.Event()
    calls, stop = [], threading.Event()

    def slow_failed_send(conn, pending, message, **k):
        in_flight.set()
        assert go.wait(5)
        return _failed_send(conn, pending, message, **k)

    def real_hook(conn, ev):
        calls.append((ev.delivery_lane, time.monotonic()))
        fallback_ran.set()
        return {"status": "filled"}

    def onchain_lane():
        with contextlib.closing(_connect(ledger)) as oconn:
            cursor = 0
            while not stop.is_set():
                cursor, _hits = ol.sweep_onchain_detections(
                    oconn, real_hook, cursor=cursor, now_fn=lambda: DETECTED, max_age_s=45,
                    approved_ids_fn=lambda c, n: {"w1"}, deferred=deferred)
                oconn.commit()
                ol.wait_for_next_sweep(wake, 5.0)

    deferred = {}
    with contextlib.closing(_connect(ledger)) as conn:
        ln = _slot_lane(conn, tmp_path, monkeypatch, execute=slow_failed_send, wake=wake)
        slot = threading.Thread(target=ln.handle, args=(_buy(),))
        slot.start()
        assert in_flight.wait(5)
        conn2 = _connect(ledger)
        conn2.execute(
            "INSERT INTO onchain_wallet_detections(signature, source_user_id, handle, address, "
            "network_id, mint, source_size_usd, detected_at, created_at) "
            "VALUES (?, 'w1', 'h', ?, 'solana', ?, 300, ?, ?)",
            (CURVE["signature"], CURVE_EVENT["user"], CURVE_EVENT["mint"], DETECTED, DETECTED))
        conn2.commit()
        conn2.close()
        lane_thread = threading.Thread(target=onchain_lane, daemon=True)
        lane_thread.start()
        deadline = time.monotonic() + 5
        while not deferred and time.monotonic() < deadline:
            time.sleep(0.005)
        assert list(deferred) == [(CURVE["signature"], "w1", "solana", CURVE_EVENT["mint"])] and not calls
        released = time.monotonic()
        go.set()
        assert fallback_ran.wait(1.0), "the fallback waited for the poll"
        slot.join(5)
        stop.set()
        wake.set()
        lane_thread.join(5)
    assert [lane_ for lane_, _t in calls] == [ol.FALLBACK_LANE]
    assert calls[0][1] - released < 1.0 and not deferred


def test_the_lane_is_built_with_the_onchain_wake_only_while_that_lane_runs(monkeypatch):
    from test_slot_lane_routed import _lane_with
    seen, real = [], lane.SlotLane

    class Spy(real):
        def __init__(self, **kw):
            seen.append(kw.get("wake"))
            super().__init__(**kw)
    monkeypatch.setattr(lane, "SlotLane", Spy)
    wake = threading.Event()
    _lane_with(monkeypatch, mode="shadow", wake=wake)
    _lane_with(monkeypatch, mode="shadow", wake=wake, onchain_enabled=False)
    assert seen == [wake, None]
